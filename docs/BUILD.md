# Build and deployment

## TL;DR — build the installer

```powershell
.\tools\build_installer.ps1
# → dist\SpaceDrive-Setup-v0.7.0.exe
```

That single command runs PyInstaller (one-folder bundle into `dist\spaceDrive\`)
then Inno Setup (wraps the bundle into a Windows installer in `dist\`). The
installer downloads Tesseract OCR on first run if it's not already on the
machine, and the in-app *Engine Manager* takes care of PaddleOCR (CPU/GPU)
post-install — including auto-downloading Python 3.12 for the sidecar venv
when needed.

### Prerequisites on the build host

- Python 3.10+ in the active venv with `pip install -r requirements.txt`
- `pip install pyinstaller`
- **Inno Setup 6** installed — https://jrsoftware.org/isdl.php
  (the build script looks for `iscc` on PATH then falls back to the default
  install location `C:\Program Files (x86)\Inno Setup 6\ISCC.exe`)

---

## End-to-end test in Windows Sandbox

Windows Sandbox spins up a disposable Windows VM that is wiped on close —
perfect for validating the installer on a clean system.

1. Enable Windows Sandbox once:
   *Control Panel → Programs → Turn Windows features on or off → check
   "Windows Sandbox"* (Win 10/11 **Pro or Enterprise**).
2. Build the installer: `.\tools\build_installer.ps1`
3. Double-click [`installer\test_in_sandbox.wsb`](../installer/test_in_sandbox.wsb).
   The Sandbox boots, mounts `dist\` read-only, and auto-launches the
   installer wizard. Click through, then start the app from the Start menu.

### What the Sandbox test validates

- Installer downloads Tesseract OCR (~70 MB from UB-Mannheim GitHub) and runs
  it silently
- App launches without an existing Python on the machine (PyInstaller bundles
  its own runtime)
- Default OCR works out of the box (Tesseract + NCC + ONNX)
- If you click *Options → Manage engines… → Install PaddleOCR (CPU)* inside
  the Sandbox, Engine Manager downloads Python 3.12 from python.org, runs the
  silent installer, then provisions `%LOCALAPPDATA%\SpaceDrive\.venv-paddle\`.

### What the Sandbox CANNOT validate

- **PaddleOCR GPU / Blackwell**: Windows Sandbox has no NVIDIA driver
  passthrough, so CUDA is unavailable inside the VM. Test those installer
  paths on a real machine with a GPU.
- **Hotkey overlay over Star Citizen**: SC is not installed in the Sandbox.
  The app launches and the Options dialog is fully functional, but you
  cannot exercise the live OCR loop there.

### Heads-up: don't leave the Sandbox open during a rebuild

The Sandbox mounts `dist\` in read-only mode, but the mount still keeps a
file handle open on the host side. If you try to rebuild the installer
(`iscc installer\spaceDrive.iss`) while the Sandbox window is still open,
Inno Setup fails with `EndUpdateResource failed (error 32)` — the resulting
`.exe` cannot be written because the host can't replace it. **Close the
Sandbox window first**, then rebuild.

Close the Sandbox window to discard everything.

---

## What ships in the installer

| Component | Source | Location after install |
|---|---|---|
| App executable + Python runtime | PyInstaller bundle | `C:\Program Files\SpaceDrive\` |
| ONNX glyph classifier | `models\spacedrive_ocr.onnx` | `C:\Program Files\SpaceDrive\models\` |
| NCC templates | `data\templates\` | `C:\Program Files\SpaceDrive\data\` |
| Default `config.ini` | `config.ini` | `C:\Program Files\SpaceDrive\` |
| Sidecar provisioners | `scripts\paddle_worker.py`, `scripts\install_paddle.ps1` | `C:\Program Files\SpaceDrive\scripts\` |
| Tesseract OCR | Downloaded from UB-Mannheim during install | `C:\Program Files\Tesseract-OCR\` |

What the installer does **NOT** ship (handled in-app post-install):

| Component | Trigger | Lives at |
|---|---|---|
| Python 3.12 (for Paddle sidecar) | First *Install PaddleOCR* click | per-user (no admin) |
| `.venv-paddle/` + paddleocr + paddlepaddle (CPU) | *Install PaddleOCR (CPU)* | `%LOCALAPPDATA%\SpaceDrive\.venv-paddle\` |
| `.venv-paddle/` + paddlepaddle-gpu | *Install PaddleOCR (GPU)* or *(Blackwell)* | same |

---

## User data layout (per-user, writable)

`%LOCALAPPDATA%\SpaceDrive\` (e.g. `C:\Users\<you>\AppData\Local\SpaceDrive\`):

```
SpaceDrive\
├── .venv-paddle\         # PaddleOCR sidecar (Python 3.12)        [optional]
├── downloads\            # Python 3.12 installer cache             [transient]
├── config.ini            # User overrides (read-write)             [first run]
├── user_poi.json         # User-defined POIs                       [first run]
└── logs\                 # App log + telemetry (spacedrive.log, …) [runtime]
```

The bundle in `Program Files\SpaceDrive\` stays read-only — clean uninstall
removes only the bundle, not the user data.

---

## Versioning

[Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

- Git tag per release: `git tag -a v0.7.0 -m "Sidecar installer"`
- `setuptools_scm` reads the tag and exposes the version at runtime.
- **Update `MyAppVersion` in [`installer\spaceDrive.iss`](../installer/spaceDrive.iss)** to match.
  (Future improvement: read it from `setup.py` automatically.)

---

## Manual build steps (without the orchestrator)

```powershell
# 1. PyInstaller (one-folder)
python -m PyInstaller --clean spaceDrive.spec
# → dist\spaceDrive\spaceDrive.exe + sidecar files

# 2. Inno Setup
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\spaceDrive.iss
# → dist\SpaceDrive-Setup-v0.7.0.exe
```

---

## Releasing a new version

Releases are produced **locally** by [`tools/release.ps1`](../tools/release.ps1).
GitHub Actions is intentionally kept to a smoke-test build on PRs only, to avoid
consuming the free-plan storage quota with installer artifacts.

### One-shot release command

```powershell
.\tools\release.ps1 -Version v0.7.4
.\tools\release.ps1 -Version v0.7.4 -SkipBuild   # if dist\ is already fresh
```

The script chains three steps:

1. **Build** — runs [`tools/build_installer.ps1`](../tools/build_installer.ps1)
   (PyInstaller + Inno Setup)
2. **Upload to VPS** — SFTPs the installer to the project VPS, regenerates
   `latest.json`, and prunes old versions (keeps the 5 most recent)
3. **GitHub Release** — pushes the git tag and creates the release with a link
   pointing back to the VPS-hosted installer

### Prerequisites

- All the build prerequisites listed above
- [`paramiko`](https://pypi.org/project/paramiko/) (`pip install paramiko`) for the SFTP upload
- [`gh` CLI](https://cli.github.com/) authenticated (`gh auth login`)
- A `.env.local` file at the repo root (gitignored) with the VPS credentials:

  ```ini
  VPS_HOST=padek-interactive.tech
  VPS_USER=root
  VPS_SSH_KEY=C:\Users\<you>\.ssh\spacedrive_vps   # path to private key
  VPS_PUBLIC_URL=https://padek-interactive.tech/releases
  ```

### What lives on the VPS

```
/var/www/spacedrive/releases/
├── SpaceDrive-Setup-v0.7.4.exe   ← last 5 versions kept, older auto-pruned
├── SpaceDrive-Setup-v0.7.3.exe
├── ...
└── latest.json                   ← { "version": "...", "url": "...", "date": "..." }
```

Served by nginx behind Traefik (Dokploy stack) at
`https://padek-interactive.tech/releases/`. The `latest.json` pointer is what
the community website fetches to surface the current download link.

### CI on PRs

[`.github/workflows/build.yml`](../.github/workflows/build.yml) runs on every PR
against `main`: it installs Python, Tesseract, Inno Setup, and runs
`build_installer.ps1` end-to-end. **No artifact is stored, nothing is uploaded** —
the job exists solely to catch broken builds before merge.

---

## Local debugging

- App log: `%LOCALAPPDATA%\SpaceDrive\logs\spacedrive.log` (level via `[Logging] level` in `config.ini`).
- Navigation telemetry: `%LOCALAPPDATA%\SpaceDrive\logs\telemetry-*.jsonl` when `[Debug] record_telemetry = True`.
- Debug captures: enable `[Debug] save_ocr_images = True` → writes
  `debug_capture_*.png` next to the log on each scan.
- Replay a static capture without the game:
  `[Debug] test_screenshot = path\to\screenshot.png` in `config.ini`.
- Sidecar Paddle logs: lines prefixed with `paddle |` in `logs/spacedrive.log`.

---

## Tests

```powershell
python -m pytest tests/ -v
```

Covers navigation, bearing, velocity, metadata parsing, and the Paddle service
client (mocked subprocess).
