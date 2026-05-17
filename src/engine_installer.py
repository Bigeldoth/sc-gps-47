"""Detection + on-demand installation of OCR engines.

Designed to be called from the Options dialog so users can install
PaddleOCR (CPU or GPU) without leaving the app. Tesseract on Windows
cannot be silently installed (no first-party MSI), so this module only
detects it and points users to the UB-Mannheim build.

PaddleOCR is installed into a dedicated Python 3.12 venv (`.venv-paddle/`)
rather than the host process — Paddle wheels stop at CPython 3.12 and we
want the app to run on 3.13/3.14. All paddle detection and install hits
that venv via subprocess.
"""
import ctypes
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Paddle sidecar venv layout. The host never imports paddle directly; every
# detection/install call below targets VENV_PYTHON via subprocess.
REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = REPO_ROOT / ".venv-paddle"
if sys.platform == "win32":
    VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
else:
    VENV_PYTHON = VENV_DIR / "bin" / "python"


def venv_python_exe() -> str:
    """Path-as-string for subprocess calls. Convenience wrapper."""
    return str(VENV_PYTHON)


def venv_installed() -> bool:
    """True if `.venv-paddle/` exists and has a Python interpreter."""
    return VENV_PYTHON.is_file()


def _resolve_python_312() -> str | None:
    """Returns the path to a CPython 3.12 interpreter on this machine, or None.

    Tries the Windows launcher `py -3.12` first (most reliable), then falls
    back to `python3.12` on PATH. The returned path is suitable for
    `subprocess.run([..., "-m", "venv", str(VENV_DIR)])`.
    """
    candidates: list[list[str]] = []
    if sys.platform == "win32":
        candidates.append(["py", "-3.12"])
    candidates.append(["python3.12"])
    for cmd in candidates:
        try:
            result = subprocess.run(
                [*cmd, "-c", "import sys; print(sys.executable)"],
                capture_output=True, text=True, timeout=5,
            )
        except (FileNotFoundError, OSError):
            continue
        if result.returncode == 0:
            path = result.stdout.strip()
            if path:
                return path
    return None


def _create_paddle_venv(on_line=None) -> tuple[bool, str]:
    """Creates `.venv-paddle/` from a system Python 3.12, idempotent.

    Returns (success, message). If the venv already exists with a usable
    python.exe, returns (True, "<already present>") without touching it.
    """
    if venv_installed():
        return True, f"venv already present at {VENV_DIR}\n"
    py312 = _resolve_python_312()
    if py312 is None:
        msg = (
            "Cannot create .venv-paddle/: no Python 3.12 found on this machine.\n"
            "Install it from https://www.python.org/downloads/ (or via Microsoft Store)\n"
            "then retry. PaddleOCR wheels only target CPython 3.8-3.12.\n"
        )
        if on_line is not None:
            on_line(msg.rstrip())
        return False, msg
    cmd = [py312, "-m", "venv", str(VENV_DIR)]
    logger.info("Creating paddle venv: %s", " ".join(cmd))
    if on_line is not None:
        on_line(f"-> Creating sidecar venv at {VENV_DIR} (using {py312})...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return False, f"venv creation failed: {exc}"
    if result.returncode != 0 or not venv_installed():
        return False, f"venv creation failed:\n{result.stdout}\n{result.stderr}"
    return True, f"Created {VENV_DIR}\n"

# PaddlePaddle GPU wheels are not on PyPI — they live on the project's own
# index, with a separate URL per CUDA major.minor. Keep this list ordered
# from newest to oldest so newer drivers prefer matching wheels.
_PADDLE_GPU_INDEXES = {
    "12.9": "https://www.paddlepaddle.org.cn/packages/stable/cu129/",
    "12.6": "https://www.paddlepaddle.org.cn/packages/stable/cu126/",
    "12.3": "https://www.paddlepaddle.org.cn/packages/stable/cu123/",
    "12.0": "https://www.paddlepaddle.org.cn/packages/stable/cu120/",
    "11.8": "https://www.paddlepaddle.org.cn/packages/stable/cu118/",
}

# Blackwell (sm_120, RTX 50) needs paddlepaddle-gpu from the cu129 index —
# wheels from cu126/cu123/cu118 miss the sm_120 kernels and silently return
# empty inference results, including the 3.3.x line which is published on
# cu126 only. The only wheel that currently bundles sm_120 kernels is
# paddlepaddle-gpu==3.2.1 from cu129. See:
#   https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PaddleOCR-VL-NVIDIA-Blackwell.html
#
# Note: we cannot reliably detect "this wheel has sm_120 kernels" via the
# `paddle.__version__` string alone — 3.3.1 > 3.2.1 lexicographically yet
# lacks sm_120 because it ships on cu126. To work around that, we drop a
# sentinel file inside the paddle install when we install through the
# Blackwell path; `detect_gpu_paddle_status()` reads that file.
_BLACKWELL_PADDLE_INDEX = _PADDLE_GPU_INDEXES["12.9"]
_BLACKWELL_PADDLE_VERSION = "paddlepaddle-gpu==3.2.1"
_BLACKWELL_SENTINEL_NAME = ".spacedrive-blackwell-cu129"


def _venv_query(code: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Runs `VENV_PYTHON -c <code>` and returns (success, stdout).

    Returns (False, "") if the venv is missing or the call fails. Used to
    introspect the paddle install inside the sidecar venv without importing
    paddle in the host process.
    """
    if not venv_installed():
        return False, ""
    try:
        result = subprocess.run(
            [str(VENV_PYTHON), "-c", code],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as exc:
        logger.debug("venv query failed: %s", exc)
        return False, ""
    if result.returncode != 0:
        return False, result.stdout or ""
    return True, result.stdout.strip()


def _paddle_install_dir() -> Path | None:
    """Filesystem location of the paddle package inside the sidecar venv."""
    ok, out = _venv_query(
        "import paddle, pathlib, sys; "
        "p = pathlib.Path(paddle.__file__).resolve().parent; "
        "sys.stdout.write(str(p))"
    )
    if not ok or not out:
        return None
    return Path(out)


def _has_blackwell_sentinel() -> bool:
    """True if we previously installed paddle via the Blackwell cu129 path."""
    pd = _paddle_install_dir()
    if pd is None:
        return False
    return (pd / _BLACKWELL_SENTINEL_NAME).is_file()


def _write_blackwell_sentinel() -> bool:
    """Marks the venv paddle install as Blackwell-capable. Best-effort."""
    pd = _paddle_install_dir()
    if pd is None:
        return False
    try:
        (pd / _BLACKWELL_SENTINEL_NAME).write_text(
            "Installed via spacedrive's install_paddleocr_gpu_blackwell.\n"
            f"Wheel: {_BLACKWELL_PADDLE_VERSION} from {_BLACKWELL_PADDLE_INDEX}\n",
            encoding="utf-8",
        )
        return True
    except Exception as exc:
        logger.warning("could not write Blackwell sentinel: %s", exc)
        return False


def _current_python_tag() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


@dataclass
class EngineStatus:
    installed: bool
    version: str = ""
    detail: str = ""


# ─── Detection ───────────────────────────────────────────────────────────

def detect_tesseract() -> EngineStatus:
    """Returns status for the Tesseract executable.

    Tries pytesseract first (respects the env tesseract_cmd), then falls back
    to PATH lookup, then a couple of standard Windows install paths.
    """
    try:
        import pytesseract
        version = str(pytesseract.get_tesseract_version())
        return EngineStatus(installed=True, version=version, detail=str(
            pytesseract.pytesseract.tesseract_cmd))
    except Exception:
        pass

    found = shutil.which("tesseract")
    if found:
        return EngineStatus(installed=True, version="(unknown)", detail=found)

    if sys.platform == "win32":
        for candidate in (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ):
            if os.path.exists(candidate):
                return EngineStatus(installed=True, version="(unknown)", detail=candidate)
    return EngineStatus(installed=False)


def detect_paddleocr() -> EngineStatus:
    """Returns status for paddleocr inside the sidecar venv."""
    if not venv_installed():
        return EngineStatus(installed=False, detail=f"venv missing at {VENV_DIR}")
    ok, out = _venv_query(
        "import paddleocr, sys; "
        "sys.stdout.write(getattr(paddleocr, '__version__', '(unknown)'))"
    )
    if not ok:
        return EngineStatus(installed=False, detail=f"paddleocr not importable in {VENV_DIR}")
    return EngineStatus(installed=True, version=out, detail=str(VENV_DIR))


def detect_paddlepaddle_gpu() -> bool:
    """True if the sidecar venv has a GPU-enabled paddlepaddle build."""
    ok, out = _venv_query(
        "import paddle, sys; sys.stdout.write('1' if paddle.is_compiled_with_cuda() else '0')"
    )
    return ok and out == "1"


def detect_gpu_compute_cap() -> str | None:
    """Returns the highest GPU compute capability on this machine (e.g. '12.0').
    Falls back to None if nvidia-smi is absent or fails."""
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return None
    try:
        result = subprocess.run(
            [nvidia_smi, "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    line = result.stdout.strip().splitlines()
    return line[0].strip() if line else None


def _paddle_version_tuple() -> tuple[int, int, int] | None:
    """Returns the paddle version inside the sidecar venv as (major, minor, patch)."""
    ok, out = _venv_query(
        "import paddle, sys; sys.stdout.write(str(getattr(paddle, '__version__', '')))"
    )
    if not ok or not out:
        return None
    parts = re.findall(r"\d+", out)
    if len(parts) < 3:
        return None
    return tuple(int(p) for p in parts[:3])  # type: ignore[return-value]


def _paddle_is_blackwell_capable() -> bool:
    """True if the installed paddle is the Blackwell-capable cu129 wheel.

    We rely on the sentinel file dropped by install_paddleocr_gpu_blackwell
    because `paddle.__version__` alone is ambiguous: paddle 3.3.1 (cu126)
    has a higher version string than the only Blackwell-capable wheel (3.2.1
    from cu129) but lacks the sm_120 kernels.
    """
    return _has_blackwell_sentinel()


def detect_gpu_paddle_status() -> tuple[str, str]:
    """Tristate GPU/Paddle compatibility check.

    Returns one of:
      - ('ok',                    '')                       GPU usable as-is.
      - ('no_gpu',                <msg>)                    No CUDA device.
      - ('needs_blackwell_wheel', <msg>)                    sm ≥ 12 but the
                                                            installed paddle
                                                            build lacks sm_120
                                                            kernels.
      - ('too_new',               <msg>)                    Compute cap > 12,
                                                            nothing we ship for.

    Used to drive the Engine Manager and Options UIs (status label, Migrate
    button visibility, GPU dropdown greying).
    """
    cap = detect_gpu_compute_cap()
    if not cap:
        return ("no_gpu", "No NVIDIA GPU detected (nvidia-smi missing or no devices)")
    try:
        major = int(cap.split(".")[0])
    except (ValueError, IndexError):
        return ("no_gpu", f"Could not parse GPU compute capability '{cap}'")

    if major < 12:
        # Pre-Blackwell (sm_70…sm_90) — supported by every paddle wheel we ship.
        return ("ok", "")

    if major > 12:
        return (
            "too_new",
            f"GPU compute capability {cap} is newer than any paddle wheel "
            f"we know about. Stick to CPU until paddle ships kernels for it.",
        )

    # Blackwell (sm_120) needs the cu129 wheel. We can't tell from the version
    # string alone (3.3.1 cu126 > 3.2.1 cu129 but lacks sm_120), so check the
    # sentinel file we drop during the Blackwell install.
    paddle_ver = _paddle_version_tuple()
    if paddle_ver is None:
        # Paddle not yet installed — `install_paddleocr_gpu` will route through
        # the Blackwell-aware path; the GPU is fine, just paddle is missing.
        return ("ok", "")
    if not _paddle_is_blackwell_capable():
        return (
            "needs_blackwell_wheel",
            f"GPU compute capability {cap} (Blackwell/RTX 50) requires the "
            f"cu129 / 3.2.1 paddlepaddle-gpu wheel. Installed paddle "
            f"{'.'.join(map(str, paddle_ver))} ships from cu126 and lacks "
            f"sm_120 kernels — inference will silently return empty results "
            f"until you migrate.",
        )
    return ("ok", "")


def detect_gpu_unsupported_by_paddle() -> str | None:
    """Backward-compatible wrapper around `detect_gpu_paddle_status()`.

    Returns a warning string when the status is anything other than 'ok' or
    'no_gpu' (since 'no_gpu' is already handled separately via detect_cuda()),
    else None.
    """
    status, msg = detect_gpu_paddle_status()
    if status in ("needs_blackwell_wheel", "too_new"):
        return msg
    return None


def detect_cuda() -> bool:
    """Best-effort check for a CUDA runtime on the machine.

    Used to grey out the GPU option in the Options dialog. We try, in order:
      1. Loading nvcuda.dll (Windows driver — present on any NVIDIA GPU host).
      2. Running `nvidia-smi` to confirm a working driver.
      3. Asking an already-installed paddlepaddle whether it was built with CUDA.
    """
    if sys.platform == "win32":
        try:
            ctypes.WinDLL("nvcuda.dll")
            return True
        except OSError:
            pass

    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            result = subprocess.run(
                [nvidia_smi, "-L"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and "GPU" in result.stdout:
                return True
        except Exception:
            pass

    return detect_paddlepaddle_gpu()


# ─── Installation ────────────────────────────────────────────────────────

def _pip_install(
    packages: list[str],
    extra_args: list[str] | None = None,
    on_line=None,
    python_exe: str | None = None,
) -> tuple[bool, str]:
    """Runs `<python_exe> -m pip install <packages>` in a subprocess.

    `python_exe` defaults to `sys.executable` (the host) but Paddle installs
    pass `str(VENV_PYTHON)` to install into the sidecar venv.

    Returns (success, combined_stdout_stderr). If `on_line` is provided it is
    called with each output line as pip emits it — letting the UI stream
    progress instead of waiting for the whole install to finish (relevant for
    paddlepaddle-gpu which pulls ~2 GB of CUDA wheels).
    """
    cmd = [python_exe or sys.executable, "-m", "pip", "install", "--upgrade",
           "--progress-bar", "off"]
    if extra_args:
        cmd.extend(extra_args)
    cmd.extend(packages)
    logger.info("Running %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as exc:
        return False, f"pip install failed to start: {exc}"

    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        if on_line is not None:
            try:
                on_line(line.rstrip())
            except Exception:
                pass
    proc.wait()
    return proc.returncode == 0, "".join(lines)


def detect_cuda_version() -> str | None:
    """Returns the installed CUDA major.minor version, or None if unknown.

    Parses `nvidia-smi` output (looks for the `CUDA Version: X.Y` field).
    """
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return None
    try:
        result = subprocess.run(
            [nvidia_smi], capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"CUDA Version:\s*(\d+\.\d+)", result.stdout)
    if not match:
        return None
    return match.group(1)


def _select_paddle_gpu_index(cuda_version: str | None) -> tuple[str, str]:
    """Picks the closest paddle-gpu index URL for the given CUDA version.

    Returns (chosen_cuda_label, index_url). Falls back to the highest available
    index if the detected CUDA version is newer than anything mapped, or to
    cu118 if detection failed.
    """
    if cuda_version:
        try:
            major, minor = (int(p) for p in cuda_version.split("."))
        except ValueError:
            major, minor = 0, 0
        # Prefer the highest mapped CUDA that does not exceed the installed one.
        best = None
        for label in _PADDLE_GPU_INDEXES:
            lm, ln = (int(p) for p in label.split("."))
            if (lm, ln) <= (major, minor):
                if best is None or (lm, ln) > tuple(int(p) for p in best.split(".")):
                    best = label
        if best:
            return best, _PADDLE_GPU_INDEXES[best]
    # Default fallback: CUDA 11.8 (widest compatibility).
    return "11.8", _PADDLE_GPU_INDEXES["11.8"]


def install_paddleocr_cpu(on_line=None) -> tuple[bool, str]:
    """Installs the CPU build of PaddleOCR into `.venv-paddle/`.

    Creates the venv from a system Python 3.12 if needed, then installs
    paddlepaddle + paddleocr inside it. The host process is never touched.
    """
    ok, msg = _create_paddle_venv(on_line=on_line)
    if not ok:
        return False, msg
    return _pip_install(
        ["paddlepaddle", "paddleocr"],
        on_line=on_line,
        python_exe=venv_python_exe(),
    )


def _pip_uninstall(
    packages: list[str],
    on_line=None,
    python_exe: str | None = None,
) -> tuple[bool, str]:
    """`pip uninstall -y` with streamed output. Targets `python_exe` if given."""
    cmd = [python_exe or sys.executable, "-m", "pip", "uninstall", "-y", *packages]
    logger.info("Running %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except Exception as exc:
        return False, f"pip uninstall failed to start: {exc}"
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        if on_line is not None:
            try:
                on_line(line.rstrip())
            except Exception:
                pass
    proc.wait()
    return proc.returncode == 0, "".join(lines)


def install_paddleocr_gpu_blackwell(on_line=None, header_extra: str = "") -> tuple[bool, str]:
    """Installs the Blackwell-capable paddlepaddle-gpu (cu129/3.2.1) + paddleocr.

    Shared by:
      - install_paddleocr_gpu() when the local GPU is sm_120
      - migrate_paddle_to_blackwell() when an older paddle is already installed

    `header_extra` is prepended to the user-facing output (e.g. "Migrating
    cu126 → cu129…") so the dialog message is clearer.
    """
    logger.info(
        "install_paddleocr_gpu_blackwell START (header=%r)", header_extra.strip()
    )

    ok_venv, venv_msg = _create_paddle_venv(on_line=on_line)
    if not ok_venv:
        return False, venv_msg

    header = (
        f"{header_extra}"
        f"Sidecar venv:  {VENV_DIR}\n"
        f"Target wheel:  {_BLACKWELL_PADDLE_VERSION}  (Blackwell sm_120 capable)\n"
        f"Index URL:     {_BLACKWELL_PADDLE_INDEX}\n"
        f"(see https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/"
        f"PaddleOCR-VL-NVIDIA-Blackwell.html)\n\n"
    )
    logger.info("Blackwell install header: %s", header.replace("\n", " | "))
    if on_line is not None:
        on_line(header.rstrip())

    py_exe = venv_python_exe()

    # Step 0: clean any existing paddle install (cu126 wheels conflict with
    # cu129 ones on the same site-packages because they bundle different
    # nvidia-* deps and a different paddle library tag).
    if _paddle_version_tuple() is not None:
        if on_line is not None:
            on_line("\n-> Removing existing paddlepaddle install before migration...")
        ok_u, out_u = _pip_uninstall(
            ["paddlepaddle", "paddlepaddle-gpu"], on_line=on_line,
            python_exe=py_exe,
        )
        if not ok_u:
            return False, header + out_u
    else:
        out_u = ""

    # Step 1: install pinned paddle from cu129.
    if on_line is not None:
        on_line("\n-> Installing paddlepaddle-gpu from cu129 index...")
    ok, output = _pip_install(
        [_BLACKWELL_PADDLE_VERSION],
        extra_args=["-i", _BLACKWELL_PADDLE_INDEX],
        on_line=on_line,
        python_exe=py_exe,
    )
    if not ok:
        return False, header + out_u + output

    # Step 2: ensure paddleocr is present (no-op if already installed at the
    # right version).
    if on_line is not None:
        on_line("\n-> Ensuring paddleocr is installed...")
    ok2, output2 = _pip_install(["paddleocr"], on_line=on_line, python_exe=py_exe)

    # Drop the sentinel so detect_gpu_paddle_status() can recognize this
    # install as Blackwell-capable without re-running an inference probe.
    if ok2 and _write_blackwell_sentinel():
        if on_line is not None:
            on_line(
                "→ Wrote Blackwell sentinel — GPU path will now report 'ok'."
            )
    return ok2, header + out_u + output + "\n" + output2


def migrate_paddle_to_blackwell(on_line=None) -> tuple[bool, str]:
    """Migrates an existing (older) paddlepaddle install to the cu129 Blackwell
    wheel. Same as `install_paddleocr_gpu_blackwell` but with a header line
    that makes the intent obvious in the dialog log."""
    paddle_ver = _paddle_version_tuple()
    cur = ".".join(map(str, paddle_ver)) if paddle_ver else "<not installed>"
    # Strip the `paddlepaddle-gpu==` prefix from the pin so the dialog shows
    # just the target version number (e.g. "3.2.1").
    target = _BLACKWELL_PADDLE_VERSION.split("==", 1)[-1]
    return install_paddleocr_gpu_blackwell(
        on_line=on_line,
        header_extra=(
            f"Migrating paddlepaddle: {cur} → {target} (cu129, Blackwell sm_120)\n"
        ),
    )


def install_paddleocr_gpu(on_line=None) -> tuple[bool, str]:
    """Installs the GPU build of PaddleOCR.

    paddlepaddle-gpu wheels are NOT published to PyPI; they live on the
    PaddlePaddle project index, with one URL per CUDA major.minor. We detect
    the local CUDA version via nvidia-smi and route pip to the matching index.
    paddleocr itself comes from PyPI as usual.

    Routing:
      - Blackwell (sm_120) GPU → cu129 + paddle 3.2.1 (sm_120 kernels)
      - Anything older         → highest cu* index ≤ the local CUDA version.
    """
    ok_venv, venv_msg = _create_paddle_venv(on_line=on_line)
    if not ok_venv:
        return False, venv_msg

    # Blackwell short-circuit: route through the pinned cu129 / 3.2.1 wheel.
    cap = detect_gpu_compute_cap()
    if cap:
        try:
            major = int(cap.split(".")[0])
        except (ValueError, IndexError):
            major = 0
        if major >= 12:
            logger.info(
                "paddle-gpu install: Blackwell sm_%d detected -> using cu129 wheel",
                major * 10,
            )
            return install_paddleocr_gpu_blackwell(on_line=on_line)

    cuda_version = detect_cuda_version()
    label, index_url = _select_paddle_gpu_index(cuda_version)
    detected = cuda_version or "unknown"
    logger.info(
        "paddle-gpu install: detected CUDA=%s -> using index %s (cu%s)",
        detected, index_url, label.replace(".", ""),
    )
    header = (
        f"Sidecar venv:  {VENV_DIR}\n"
        f"Detected CUDA: {detected}  ->  using {index_url}\n"
        f"(see https://www.paddlepaddle.org.cn/install/quick for other versions)\n\n"
    )
    if on_line is not None:
        on_line(header.rstrip())
    py_exe = venv_python_exe()
    # Step 1: install paddlepaddle-gpu using `-i` (replace index) — matches
    # Paddle's official install command exactly. We do NOT use
    # --extra-index-url because PyPI does not carry paddlepaddle-gpu, and
    # mixing indexes occasionally confuses pip's resolver.
    ok, output = _pip_install(
        ["paddlepaddle-gpu"],
        extra_args=["-i", index_url],
        on_line=on_line,
        python_exe=py_exe,
    )
    if not ok:
        return False, header + output
    # Step 2: install paddleocr from PyPI.
    ok2, output2 = _pip_install(["paddleocr"], on_line=on_line, python_exe=py_exe)
    return ok2, header + output + "\n" + output2


def uninstall_paddleocr(on_line=None) -> tuple[bool, str]:
    """Removes the entire `.venv-paddle/` directory tree.

    Cleaner than pip-uninstalling each package individually: the venv exists
    purely for paddle, so blowing it away leaves no orphan caches behind.
    """
    if not VENV_DIR.exists():
        return True, f"paddle sidecar was not installed -- nothing to do.\n"
    try:
        shutil.rmtree(VENV_DIR)
    except Exception as exc:
        return False, f"could not remove {VENV_DIR}: {exc}"
    if on_line is not None:
        on_line(f"Removed {VENV_DIR}")
    return True, f"Removed {VENV_DIR}\n"


TESSERACT_WINDOWS_URL = "https://github.com/UB-Mannheim/tesseract/wiki"


# ─── PaddleOCR-VL (advanced sidecar) ─────────────────────────────────────

def detect_paddle_vl() -> EngineStatus:
    """Returns status for the optional PaddleOCR-VL venv sidecar.

    Looks for `.venv-paddle-vl/Scripts/paddleocr.exe` next to the repo root.
    """
    from paddle_vl_service import VENV_PADDLEOCR
    if not VENV_PADDLEOCR.is_file():
        return EngineStatus(installed=False)
    return EngineStatus(
        installed=True,
        version="(see .venv-paddle-vl)",
        detail=str(VENV_PADDLEOCR),
    )


def install_paddle_vl(on_line=None, backend: str = "transformers") -> tuple[bool, str]:
    """Runs scripts/install_paddle_vl.ps1 to provision the VL sidecar venv.

    `backend` is forwarded to the script so the user can pick transformers
    (Windows-safe default) / vllm / sglang.
    """
    if sys.platform != "win32":
        return False, "paddle-vl install script is currently Windows-only"

    script = Path(__file__).resolve().parent.parent / "scripts" / "install_paddle_vl.ps1"
    if not script.is_file():
        return False, f"install script missing: {script}"

    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", str(script),
        "-Backend", backend,
    ]
    logger.info("paddle-vl install: %s", " ".join(cmd))
    if on_line is not None:
        on_line(f"→ Running {script.name} (backend={backend})…")
        on_line(
            "  (this venv is large — expect ~6 GB of downloads on first run)"
        )
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except Exception as exc:
        return False, f"paddle-vl install failed to start: {exc}"
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        if on_line is not None:
            try:
                on_line(line.rstrip())
            except Exception:
                pass
    proc.wait()
    return proc.returncode == 0, "".join(lines)


def uninstall_paddle_vl(on_line=None) -> tuple[bool, str]:
    """Deletes the .venv-paddle-vl directory tree."""
    from paddle_vl_service import VENV_DIR
    if not VENV_DIR.exists():
        return True, "paddle-vl venv was not installed — nothing to do."
    try:
        shutil.rmtree(VENV_DIR)
    except Exception as exc:
        return False, f"could not remove {VENV_DIR}: {exc}"
    if on_line is not None:
        on_line(f"Removed {VENV_DIR}")
    return True, f"Removed {VENV_DIR}\n"
