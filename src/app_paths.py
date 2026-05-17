"""Filesystem paths for both dev and PyInstaller-bundled runtimes.

PyInstaller's one-folder / one-file bundle moves `src/` into a temporary
extraction directory (`sys._MEIPASS`), so the historical
`REPO_ROOT = Path(__file__).resolve().parent.parent` idiom would resolve
to that temp dir at runtime — which gets wiped on the next launch.

This module centralises the path logic:

  * `bundle_dir()`   — read-only assets shipped with the app (worker scripts,
                       PS1 installers, ONNX model, templates). In dev that's
                       the repo root; in bundle that's `sys._MEIPASS`.
  * `user_data_dir()` — writable per-user data (sidecar venvs, user POIs,
                       logs, config overrides). In dev that's the repo root;
                       in bundle that's `%LOCALAPPDATA%\\SpaceDrive\\`.

Modules that used to do `Path(__file__).resolve().parent.parent` should now
ask `bundle_dir()` (for shipped resources) or `user_data_dir()` (for things
the user creates or modifies at runtime).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_APP_NAME = "SpaceDrive"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle (`sys.frozen` is set)."""
    return getattr(sys, "frozen", False)


def bundle_dir() -> Path:
    """Read-only assets shipped with the app.

    Dev:    `<repo>/`
    Bundle: `sys._MEIPASS` (PyInstaller's extract dir for the running app)
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        # Fall through to executable dir (one-folder bundle without onefile).
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    """Writable per-user app data — sidecar venvs, POIs, logs, overrides.

    Dev:    `<repo>/`           (so dev work keeps using the in-tree layout)
    Bundle: `%LOCALAPPDATA%\\SpaceDrive\\` on Windows
            `~/.local/share/SpaceDrive` on POSIX
    Created on first call.
    """
    if not is_frozen():
        return Path(__file__).resolve().parent.parent

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / _APP_NAME
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = Path(xdg) / _APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def scripts_dir() -> Path:
    """Directory containing the helper scripts (paddle_worker.py, install_paddle.ps1)."""
    return bundle_dir() / "scripts"
