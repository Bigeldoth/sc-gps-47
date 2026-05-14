"""Lifecycle management for the PaddleOCR-VL sidecar server.

The VL pipeline relies on the `paddleocr genai_server` CLI which spawns a
Vision-Language model (PaddleOCR-VL-1.5-0.9B by default) behind an HTTP
endpoint. To keep the VL deps isolated from the rest of the app (transformers
/ vLLM frequently conflict with the standard paddleocr stack), we install
it into a dedicated venv at `<repo_root>/.venv-paddle-vl` and launch the
server from there.

This module is intentionally process-only — no Qt dependencies — so it can be
imported from the worker thread as well as the UI.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# Filesystem layout. Resolved relative to the repo root so the same paths work
# from src/, tools/, and the PyInstaller bundle.
REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = REPO_ROOT / ".venv-paddle-vl"
if sys.platform == "win32":
    VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
    VENV_PADDLEOCR = VENV_DIR / "Scripts" / "paddleocr.exe"
else:
    VENV_PYTHON = VENV_DIR / "bin" / "python"
    VENV_PADDLEOCR = VENV_DIR / "bin" / "paddleocr"

DEFAULT_PORT = 8118
DEFAULT_MODEL = "PaddleOCR-VL-1.5-0.9B"
DEFAULT_BACKEND = "transformers"  # 'transformers' | 'vllm' | 'sglang'


def venv_installed() -> bool:
    """True if the dedicated VL venv exists and has paddleocr installed."""
    return VENV_PADDLEOCR.is_file()


def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    """Quick TCP connect check — True if `port` is accepting connections."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


class PaddleVLService:
    """Owns one paddleocr genai_server subprocess.

    Single-instance per app: callers should reuse the same object across the
    process lifetime. `start()` is idempotent and returns immediately if the
    service is already up.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        backend: str = DEFAULT_BACKEND,
        port: int = DEFAULT_PORT,
        host: str = "127.0.0.1",
    ):
        self.model = model
        self.backend = backend
        self.port = port
        self.host = host
        self._proc: subprocess.Popen | None = None
        self._log_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}"

    def is_running(self) -> bool:
        """True if the subprocess is alive AND the port is responding.

        We check both because `paddleocr genai_server` can take 20-40s to
        finish loading the VL model after process start.
        """
        proc_alive = self._proc is not None and self._proc.poll() is None
        return proc_alive and is_port_open(self.port, self.host)

    def start(self, blocking: bool = False, timeout: float = 90.0) -> bool:
        """Launches the sidecar if it's not already running.

        Returns True if the service is up at the end of the call. When
        `blocking=False`, returns immediately after spawning — the caller
        should use `wait_ready()` if they need to know when inference will
        actually work.
        """
        with self._lock:
            # Reuse an externally-started sidecar on the same port.
            if is_port_open(self.port, self.host):
                logger.info("paddle-vl: existing service detected on %s", self.endpoint)
                return True
            if self._proc is not None and self._proc.poll() is None:
                logger.debug("paddle-vl: process already running, port not open yet")
            else:
                if not venv_installed():
                    raise FileNotFoundError(
                        f"PaddleOCR-VL venv not found at {VENV_DIR}. "
                        f"Install it via Manage engines… → 'Install Paddle-VL'."
                    )
                cmd = [
                    str(VENV_PADDLEOCR),
                    "genai_server",
                    "--model_name", self.model,
                    "--backend", self.backend,
                    "--host", self.host,
                    "--port", str(self.port),
                ]
                logger.info("paddle-vl: spawning sidecar: %s", " ".join(cmd))
                # Detach so the sidecar survives if the main process is
                # killed abruptly — but pipe stdout so we can stream the
                # boot log into the app log file for diagnostics.
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(VENV_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        if sys.platform == "win32"
                        else 0
                    ),
                )
                self._log_thread = threading.Thread(
                    target=self._pipe_logs, daemon=True,
                )
                self._log_thread.start()
        if blocking:
            return self.wait_ready(timeout=timeout)
        return True

    def wait_ready(self, timeout: float = 90.0, poll_interval: float = 1.0) -> bool:
        """Polls the port until the sidecar accepts connections or times out."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_running():
                logger.info("paddle-vl: service ready on %s", self.endpoint)
                return True
            if self._proc is not None and self._proc.poll() is not None:
                logger.error(
                    "paddle-vl: sidecar exited prematurely (code=%s)",
                    self._proc.returncode,
                )
                return False
            time.sleep(poll_interval)
        logger.warning("paddle-vl: timed out after %.0fs waiting for sidecar", timeout)
        return False

    def stop(self):
        """Terminates the sidecar subprocess. Idempotent."""
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception as exc:
            logger.warning("paddle-vl: stop() error: %s", exc)

    def health(self) -> bool:
        """Optional active health check by hitting the OpenAI-style probe.

        Some paddleocr backends expose /health and some don't — we fall back
        to the TCP port check so this never gives a false negative on
        well-behaved servers that just don't implement the route.
        """
        if not is_port_open(self.port, self.host):
            return False
        try:
            with urllib.request.urlopen(f"{self.endpoint}/health", timeout=2):
                return True
        except urllib.error.HTTPError as e:
            # HTTP error (404 / 405) means the port is alive but the route
            # is not implemented — that's still a "healthy" state for us.
            return e.code < 500
        except Exception:
            # Network-level failure — port may have just closed.
            return is_port_open(self.port, self.host)

    # ─── Internal ────────────────────────────────────────────────────

    def _pipe_logs(self):
        """Reads the sidecar stdout and forwards lines to the app logger.

        Runs in a daemon thread for the lifetime of the subprocess.
        """
        if self._proc is None or self._proc.stdout is None:
            return
        for line in self._proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            # Tag every line so a `grep paddle-vl` in spacedrive.log shows
            # the sidecar's lifecycle.
            logger.info("paddle-vl │ %s", line)


# Module-level singleton. main.py / OCRProcessor share one instance.
_service: PaddleVLService | None = None


def get_service(
    model: str = DEFAULT_MODEL,
    backend: str = DEFAULT_BACKEND,
    port: int = DEFAULT_PORT,
) -> PaddleVLService:
    """Returns the shared `PaddleVLService` instance, instantiating on first call.

    If the user changes model/backend/port via Options, we tear down the
    existing instance and rebuild — caller is expected to call this in the
    config-reload path after Options is saved.
    """
    global _service
    if (
        _service is None
        or _service.model != model
        or _service.backend != backend
        or _service.port != port
    ):
        if _service is not None:
            _service.stop()
        _service = PaddleVLService(model=model, backend=backend, port=port)
    return _service
