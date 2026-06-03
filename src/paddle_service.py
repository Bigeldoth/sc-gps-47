"""Lifecycle management for the PaddleOCR sidecar worker.

The host process (Python 3.10-3.14) spawns `scripts/paddle_worker.py` inside
a separate Python 3.12 venv (`.venv-paddle/`) where PaddleOCR is installed,
and communicates over stdin/stdout using newline-delimited JSON.

It uses pipe-based IPC (newline-delimited JSON over stdin/stdout) — no port to
manage, no extra HTTP server dependency in the sidecar venv.

The module is intentionally process-only (no Qt) so it can be imported from
the worker thread as well as the UI.

Protocol — see scripts/paddle_worker.py for the request/response shapes.
"""
from __future__ import annotations

import atexit
import base64
import json
import logging
import subprocess
import sys
import threading
import time
from typing import Any

import numpy as np

from app_paths import scripts_dir, user_data_dir

logger = logging.getLogger(__name__)

# Sidecar lives under user data so it survives across app updates and never
# requires admin rights to (re)install. The worker script ships read-only
# with the bundle (or the repo in dev) and is resolved via scripts_dir().
VENV_DIR = user_data_dir() / ".venv-paddle"
if sys.platform == "win32":
    VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
else:
    VENV_PYTHON = VENV_DIR / "bin" / "python"
WORKER_SCRIPT = scripts_dir() / "paddle_worker.py"

DEFAULT_TIMEOUT = 30.0
DEFAULT_UPSCALE = 3


def venv_installed() -> bool:
    """True if the dedicated Paddle venv exists with a python executable."""
    return VENV_PYTHON.is_file()


class PaddleServiceError(RuntimeError):
    """Raised when the sidecar fails to start, crashes, or returns ok=false."""


class PaddleService:
    """Owns one `paddle_worker.py` subprocess for the host's lifetime.

    Single-instance per (device, model_dir, lang) tuple. Callers should reuse
    the same object across the process lifetime — see `get_service()` for the
    module-level singleton.

    Thread-safety: `call()` is guarded by an internal Lock so concurrent
    callers serialize correctly. OCR is naturally one-frame-at-a-time so the
    Lock should never become a hot contention point.
    """

    def __init__(
        self,
        device: str = "cpu",
        model_dir: str | None = None,
        lang: str = "en",
        upscale: int = DEFAULT_UPSCALE,
    ):
        self.device = device
        self.model_dir = model_dir
        self.lang = lang
        self.upscale = upscale
        self._proc: subprocess.Popen | None = None
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._initialized = False

    # ─── Lifecycle ────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, blocking: bool = True, timeout: float = DEFAULT_TIMEOUT) -> bool:
        """Spawns the sidecar and sends the init message.

        With `blocking=True` (default), waits for the worker to confirm init
        before returning. Returns True if the worker is ready, False otherwise.
        """
        with self._lock:
            if self.is_running() and self._initialized:
                return True
            self._spawn_locked()
            if not blocking:
                return True
            return self._init_locked(timeout=timeout)

    def stop(self):
        """Terminates the sidecar subprocess. Idempotent."""
        with self._lock:
            proc = self._proc
            self._proc = None
            self._initialized = False
        if proc is None or proc.poll() is not None:
            return
        try:
            try:
                self._raw_send(proc, {"op": "shutdown"})
                proc.wait(timeout=3)
            except Exception:
                pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception as exc:
            logger.warning("paddle: stop() error: %s", exc)

    # ─── Public IPC ───────────────────────────────────────────────────────

    def call(self, op: str, **payload: Any) -> dict:
        """Sends one request and reads exactly one response.

        Recovers from a single sidecar crash by respawning + retrying once.
        """
        with self._lock:
            if not self.is_running() or not self._initialized:
                logger.warning("paddle: sidecar not running — attempting auto-start")
                self._spawn_locked()
                if not self._init_locked(timeout=DEFAULT_TIMEOUT):
                    raise PaddleServiceError("paddle sidecar failed to initialize")
            try:
                return self._call_locked(op, payload)
            except (BrokenPipeError, EOFError, json.JSONDecodeError) as exc:
                logger.warning("paddle: sidecar pipe broken (%s) — restarting once", exc)
                self._cleanup_proc_locked()
                self._spawn_locked()
                if not self._init_locked(timeout=DEFAULT_TIMEOUT):
                    raise PaddleServiceError("paddle sidecar restart failed") from exc
                return self._call_locked(op, payload)

    def recognize(self, image: np.ndarray) -> str:
        """Mirrors PaddleAdapter.recognize() — returns joined text."""
        if image is None or image.size == 0:
            return ""
        result = self.call("recognize", **_encode_image(image))
        return str(result.get("text", ""))

    def recognize_detailed(self, image: np.ndarray) -> dict:
        """Mirrors PaddleAdapter.recognize_detailed() — returns structured dict."""
        if image is None or image.size == 0:
            return {"texts": [], "scores": [], "polys": [], "upscale": 1, "shape": (0, 0, 0)}
        return self.call("recognize_detailed", **_encode_image(image))

    # ─── Internal ─────────────────────────────────────────────────────────

    def _spawn_locked(self):
        """Spawns the worker subprocess. Assumes self._lock is held."""
        if not venv_installed():
            raise PaddleServiceError(
                f"PaddleOCR sidecar venv not found at {VENV_DIR}. "
                f"Install it via Options -> Manage engines... or "
                f"`scripts\\install_paddle.ps1`."
            )
        if not WORKER_SCRIPT.is_file():
            raise PaddleServiceError(f"paddle_worker.py missing: {WORKER_SCRIPT}")

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

        logger.info("paddle: spawning sidecar with %s", VENV_PYTHON)
        self._proc = subprocess.Popen(
            [str(VENV_PYTHON), str(WORKER_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            cwd=str(user_data_dir()),
            creationflags=creationflags,
        )
        # Drain stderr first so the worker can never block on a full pipe.
        self._stderr_thread = threading.Thread(
            target=self._pipe_stderr_logs,
            args=(self._proc,),
            daemon=True,
        )
        self._stderr_thread.start()
        self._initialized = False

    def _init_locked(self, timeout: float) -> bool:
        """Sends the init message and waits for ok. Assumes self._lock is held."""
        if self._proc is None:
            return False
        try:
            response = self._call_locked(
                "init",
                {
                    "device": self.device,
                    "model_dir": self.model_dir,
                    "lang": self.lang,
                    "upscale": self.upscale,
                },
                timeout=timeout,
            )
        except Exception as exc:
            logger.error("paddle: init failed: %s", exc)
            self._cleanup_proc_locked()
            return False
        logger.info("paddle: sidecar ready — %s", response)
        self._initialized = True
        return True

    def _call_locked(
        self,
        op: str,
        payload: dict,
        timeout: float | None = None,
    ) -> dict:
        """Writes one request, reads exactly one response. Lock-held."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise EOFError("sidecar is not running")
        msg = {"op": op, **payload}
        self._raw_send(proc, msg)
        line = self._raw_recv(proc, timeout=timeout)
        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            raise PaddleServiceError(f"sidecar returned non-JSON: {line[:200]!r}")
        if not response.get("ok"):
            err = response.get("error", "<no message>")
            tb = response.get("traceback", "")
            raise PaddleServiceError(f"sidecar error: {err}\n{tb}")
        return response.get("result", {})

    @staticmethod
    def _raw_send(proc: subprocess.Popen, msg: dict) -> None:
        if proc.stdin is None:
            raise BrokenPipeError("sidecar stdin is closed")
        line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        proc.stdin.write(line)
        proc.stdin.flush()

    @staticmethod
    def _raw_recv(proc: subprocess.Popen, timeout: float | None = None) -> str:
        """Reads one NDJSON line from the sidecar stdout. Honors a soft timeout
        by polling proc.poll() between reads. The actual readline() is blocking
        — for paddle that's fine because the longest legitimate call is the
        first inference (~30s) and the caller already passes a generous timeout
        via the init path."""
        if proc.stdout is None:
            raise BrokenPipeError("sidecar stdout is closed")
        deadline = (time.monotonic() + timeout) if timeout else None
        while True:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"sidecar did not respond within {timeout}s")
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    raise EOFError(f"sidecar exited (code={proc.returncode})")
                raise EOFError("sidecar stdout EOF without exit")
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            return text

    @staticmethod
    def _pipe_stderr_logs(proc: subprocess.Popen) -> None:
        """Daemon thread: forwards sidecar stderr to the host logger.

        Tags every line with `paddle |` so a `grep paddle` in spacedrive.log
        shows the full sidecar lifecycle. Critical: must run continuously so
        the OS pipe buffer never fills (which would deadlock the worker).
        """
        if proc.stderr is None:
            return
        for raw in proc.stderr:
            try:
                line = raw.decode("utf-8", errors="replace").rstrip()
            except Exception:
                continue
            if line:
                logger.info("paddle | %s", line)

    def _cleanup_proc_locked(self):
        """Force-kills a dead/zombie process. Assumes self._lock is held."""
        proc = self._proc
        self._proc = None
        self._initialized = False
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


# ─── Image encoding helper ───────────────────────────────────────────────

def _encode_image(image: np.ndarray) -> dict:
    """Serializes a numpy image to the IPC payload (raw bytes + b64)."""
    image = np.ascontiguousarray(image)
    return {
        "img": base64.b64encode(image.tobytes()).decode("ascii"),
        "shape": list(image.shape),
        "dtype": str(image.dtype),
    }


# ─── Module-level singleton ──────────────────────────────────────────────

_service: PaddleService | None = None
_service_key: tuple | None = None


def get_service(
    device: str = "cpu",
    model_dir: str | None = None,
    lang: str = "en",
    upscale: int = DEFAULT_UPSCALE,
) -> PaddleService:
    """Returns the shared PaddleService instance, building it on first call.

    If any constructor argument changes versus the previous call, tears down
    the existing sidecar and rebuilds.
    """
    global _service, _service_key
    key = (device, model_dir, lang, upscale)
    if _service is not None and _service_key == key:
        return _service
    if _service is not None:
        logger.info("paddle: config changed, restarting sidecar")
        _service.stop()
    _service = PaddleService(device=device, model_dir=model_dir, lang=lang, upscale=upscale)
    _service_key = key
    return _service


def _atexit_stop():
    if _service is not None:
        try:
            _service.stop()
        except Exception:
            pass


atexit.register(_atexit_stop)
