# Build and deployment

## TL;DR — build the installer

```powershell
.\tools\build_installer.ps1
# → dist\SpaceDrive-Setup-v1.0.0.exe (version read from VERSION)
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
| ONNX glyph classifier | `models\spacedrive_ocr.onnx` | `C:\Program Files\SpaceDrive\_internal\models\` |
| NCC templates | `data\templates\` | `C:\Program Files\SpaceDrive\_internal\data\` |
| Default `config.ini` and immutable `VERSION` | Repository files | `C:\Program Files\SpaceDrive\_internal\` |
| Sidecar provisioners and update helper | `scripts\paddle_worker.py`, `scripts\install_paddle.ps1`, `scripts\apply_update.ps1` | `C:\Program Files\SpaceDrive\_internal\scripts\` |
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
├── data\user_poi.json    # User-defined POIs                       [first run]
├── updates\             # Downloaded updates and transaction backups
└── logs\                 # App log + telemetry (spacedrive.log, …) [runtime]
```

The bundle in `Program Files\SpaceDrive\` stays read-only — clean uninstall
removes only the bundle, not the user data.

---

## Versioning

[Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

`VERSION` is the source of truth. The installed application reads the bundled
file through `app_version.get_app_version()`; user configuration and Git tags do
not override the running binary's identity. A missing/invalid file gives
`unknown`, so the updater cannot incorrectly claim the app is current.

To intentionally change a version, replace `vX.Y.Z` below with the desired
release version, then review and commit all changes on the feature branch:

Follow [the SemVer policy](VERSIONING.md): new compatible features require a
MINOR increment, fixes alone require PATCH, and incompatible changes require
MAJOR. Consider the complete release since the last published tag.

```powershell
python tools/versioning.py sync --version vX.Y.Z
python tools/versioning.py check
```

`sync` updates `VERSION`, `[Updates] app_version` in `config.ini` (legacy mirror),
and `MyAppVersion` in `installer/spaceDrive.iss`. **Build/release scripts never
bump versions.** The prepared application version is `1.1.0`; the published
release remains `v1.0.0` until the local release workflow is run explicitly.

The build records source commit/fingerprint, bundle hashes and installer hash
under ignored `dist/*-provenance.json` files. `-SkipPyInstaller` and `-SkipBuild`
require these receipts and reject stale, modified or mismatched artifacts.
Release additionally requires a clean committed checkout, including no untracked
source files, and refuses a tag already attached to a different commit.

---

## Manual build steps (without the orchestrator)

```powershell
# 1. PyInstaller (one-folder)
python tools/versioning.py check
python tools/versioning.py snapshot-build
python -m PyInstaller --clean spaceDrive.spec
# → dist\spaceDrive\spaceDrive.exe + sidecar files
python tools/versioning.py record-bundle

# 2. Inno Setup
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\spaceDrive.iss
python tools/versioning.py record-build
# → installer matching VERSION, with a verified build receipt
```

---

## Releasing a new version

Releases are produced **locally** by [`tools/release.ps1`](../tools/release.ps1).
GitHub Actions is intentionally kept to a smoke-test build on PRs only, to avoid
consuming the free-plan storage quota with installer artifacts.

### One-shot release command

```powershell
.\tools\release.ps1 -Version vX.Y.Z
.\tools\release.ps1 -Version vX.Y.Z -SkipBuild   # only with matching build receipts
```

Use a new, intentionally synchronized and committed version. Already published
versions cannot be replaced. The script performs these steps:

1. Check versions, clean source state, local/remote tag identity and GitHub access.
2. Build with [`tools/build_installer.ps1`](../tools/build_installer.ps1), or
   validate the existing installer and bundle receipts when `-SkipBuild` is used.
3. Push a tag identifying that exact source commit and prepare a GitHub **draft**.
4. Publish verified artifacts over SFTP, then atomically replace `latest.json`.
5. Publish the GitHub draft. If that final step fails, rerun the same command;
   an identical completed VPS publication is safe to resume.

An upload failure leaves the old pointer intact and the GitHub release a draft.
Cleanup of old files runs after the pointer commits; cleanup failures are reported
as warnings and do not invalidate an otherwise complete publication.

### Prerequisites

- All the build prerequisites listed above
- [`paramiko`](https://pypi.org/project/paramiko/) (`pip install paramiko`) for the SFTP upload
- [`gh` CLI](https://cli.github.com/) authenticated (`gh auth login`)
- The VPS SSH host key recorded and verified in the host's `~/.ssh/known_hosts`.
  Unknown or changed server keys are rejected. `VPS_KNOWN_HOSTS` can optionally
  select an additional known-hosts file.
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
├── SpaceDrive-Setup-vX.Y.Z.exe   ← immutable; last 5 versions retained
├── deltas\                     ← schema-2 exact-base delta archives
├── manifests\                  ← complete bundle path → SHA256 manifests
└── latest.json                 ← atomic publication pointer
```

Served by nginx behind Traefik (Dokploy stack) at
`https://padek-interactive.tech/releases/`. The `latest.json` pointer is what
the community website fetches to surface the current download link.

The release pointer contains `schema_version: 2`, installer `version`, `url`,
`checksum` (SHA256), `size_bytes`, and `date`. An optional `delta_v2` has an exact
`from_version`, `to_version`, URL, SHA256 and size. `delta.available` is explicitly
`false`: **already-installed legacy clients must use the full installer**, because
their UI can otherwise activate the old unsafe delta path without checking the
base version. The current production metadata is not changed by source edits.

A v2 ZIP contains `manifest.json` (schema/from/to), `version.txt`, an exhaustive
`checksum.sha256` using two spaces and POSIX paths, `DELETED.txt` (possibly empty),
and changed files under `FILES/`. Deletion-only packages are valid. User data,
user config and runtime logs are forbidden; bundled defaults under `_internal/`
remain updateable. The previous published `latest.json`, never filesystem mtime,
selects the delta base. Without its bundle manifest, publication uses a full
installer only.

Each remote upload uses a temporary filename, verifies the remote SHA256 and
size, and is renamed before the pointer changes. Replacing `latest.json` requires
the OpenSSH atomic POSIX rename extension. Publication takes an SFTP directory
lock (`.publish-lock`). After an interrupted uploader, verify no release is
running and inspect the existing pointer/artifacts before removing a stale lock.

### CI on PRs

[`.github/workflows/build.yml`](../.github/workflows/build.yml) runs on every PR
against `main` or `staging`: it checks version consistency, runs the test suite,
and builds with Python, Tesseract and Inno Setup. **No artifact is stored,
nothing is uploaded.** Tags do not trigger a second publication workflow.

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
