# SpaceDrive GPS

> GPS navigation overlay for Star Citizen, based on OCR of the debug HUD `r_DisplayInfo 2`.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE.txt)
[![Python: 3.10–3.14](https://img.shields.io/badge/Python-3.10%E2%80%933.14-yellow.svg)](https://www.python.org/)
[![Anti-cheat](https://img.shields.io/badge/EAC-safe-green.svg)](#anti-cheat-security)
[![Download](https://img.shields.io/github/v/release/Bigeldoth/sc-gps-47?label=download&include_prereleases)](https://github.com/Bigeldoth/sc-gps-47/releases/latest)

> **Installer hosted on the project VPS** — see the [latest release](https://github.com/Bigeldoth/sc-gps-47/releases/latest) for the direct download link, or fetch the always-current pointer at [`/releases/latest.json`](https://padek-interactive.tech/releases/latest.json).

SpaceDrive continuously reads the coordinates displayed by the game's debug HUD (`Zone:OOC_X Pos: X.XXXX km Y.XXXX km Z.XXXX km`) and provides an always-on-top overlay with distance, heading and data freshness indicator. No memory reading — 100% screenshot-based.

---

## Overview

```
┌─────────────────────────────────┐  ← MFD-style amber border
│ X:   4133.56   Y:  -1964.13    │  ← color evolves with freshness
│ Z:   -529.89                   │     (green → yellow → orange → red)
├─────────────────────────────────┤
│ ▶ asop hurL2                   │  ← current target
│   17 m   ↗▲   →42° ↑12°        │  ← distance + 3D arrow + relative heading
└─────────────────────────────────┘
```

**Indicator legend:**
- **Coordinate color**: continuous time interpolation. Green = fresh data (<3 s), yellow (3-7 s), orange (7-12 s), red = needs refresh.
- **3D arrow `↗▲`**: combined world direction. 8 cardinal directions (`↑↗→↘↓↙←↖`) in X/Y, plus `▲`/`▼` if Z elevation is significant.
- **Relative heading `→42° ↑12°`**: yaw/pitch offset from direction of movement (only when moving).

---

## Features

### Robust OCR reading
- **NCC-first** glyph classifier (pure NumPy template matching, or ONNX `TinyGlyphCNN`) runs once per frame on the CLAHE-enhanced grayscale. Segmentation is performed on a binary `otsu` pass (reliable bounding boxes); classification is performed on the gradient-rich grayscale (preserves fine character detail).
- **Tesseract OEM3 fallback** on 2 binary thresholding passes (Otsu / adaptive) in parallel, used for zone names and metadata. Skipped entirely once NCC reconstructs coordinates.
- **Strict 3-4 decimal regex**: rejects degraded readings that caused ~17 m errors on saved POIs.
- **Post-OCR normalization**: fixes common artifacts (`Pos:_`, variants `lkm/Km/kn`, parasitic underscores).
- **Capture region**: 600×60 px at the top right of the capture monitor.
- **Capture display**: choose **Options → General → Star Citizen display**, then
  save to switch the capture monitor without restarting. The default follows the
  primary display. Explicit choices are remembered by display identity rather
  than list order. Use **Refresh displays** after changing your display setup.
  If the selected display is unavailable, capture pauses until it reconnects or
  you choose another display. Use fullscreen or borderless mode so the HUD is at
  the top right of the selected display; window detection is not automatic.
- **Capture debug outline**: enable **Options → Debug → Show capture region** and save
  to show the actual OCR capture bounds. Disabled by default. The cyan outline
  stays outside the captured pixels, passes clicks through, and never takes focus.
  Edges outside the desktop are clipped. It follows the overlay visibility shortcut
  and is hidden in screenshot test mode. Save the option unchecked to remove it.

### Navigation
- **User POIs** stored in `%LOCALAPPDATA%\SpaceDrive\data\user_poi.json` — 8 community categories (hidden, cave, circuit, tactical, industry, logistics, loot, racing) shared with the SpaceDrive Community Hub.
- **3D Euclidean distance** in **planet-relative (OOC)** frame — invariant to planet orbits, unlike Root/SolarSystem frame.
- **Cross-OOC calculation refusal**: if target and player are not in the same ObjectContainer, overlay indicates this instead of showing false distance.
- **Import / export**: clipboard or JSON file, compatible with the SpaceDrive Community Hub.

### Quick snapshot hotkey
- `Shift+F3` freezes coordinates **at the exact moment of press** in a snapshot — value does not drift while dialog remains open.
- Refuses with overlay message if data is stale (red, > 9 s).

### Star Citizen MFD-style overlay
- Frame with fine amber border, semi-transparent black background.
- `WindowTransparentForInput` → never captures game mouse click.
- `WindowStaysOnTopHint` → stays visible over Star Citizen.
- Coordinate color progresses linearly with elapsed time since last valid OCR (dedicated 150 ms timer, independent of capture cycle).
- **Support on Tipeee** in the system-tray menu or the red button with the Tipeee
  logo in **Options → General** opens
  [Bigeldoth's support page](https://fr.tipeee.com/bigeldoth/) in your default browser.

---

## Installation

### Recommended: Windows installer (no Python required)

1. Go to the [latest release](https://github.com/Bigeldoth/sc-gps-47/releases/latest) and download **`SpaceDrive-Setup-vX.Y.Z.exe`** (~70 MB).
2. Double-click the installer and accept the UAC prompt.
3. The wizard installs SpaceDrive into `C:\Program Files\SpaceDrive\`. If Tesseract OCR is not already present, it is automatically downloaded from UB-Mannheim and installed silently — no extra step on your side.
4. Launch *SpaceDrive GPS* from the Start menu.

Tesseract + the ONNX glyph classifier work out of the box. **PaddleOCR** (CPU / GPU / Blackwell) is optional and can be added later from *Options → Manage engines…*; the Engine Manager will also auto-download Python 3.12 if your machine doesn't have it.

User data (POIs, log, optional sidecar venvs) lives in `%LOCALAPPDATA%\SpaceDrive\` so it survives future installs.

Requirements: Windows 10 / 11 (x64), internet during installation, ~150 MB of disk.

### From source (developers)

For contributors who want to run from a checkout instead of the installer.

Prerequisites:
- **Python 3.10–3.14** ([python.org](https://www.python.org/downloads/) — check "Add to PATH" at installation)
- **Tesseract OCR** ([UB-Mannheim build for Windows](https://github.com/UB-Mannheim/tesseract/wiki)) installed to `C:\Program Files\Tesseract-OCR\` (auto-detected)
- Optional **PaddleOCR**: installed on demand from *Options → Manage engines…* into a dedicated `.venv-paddle/` (Python 3.12) — see [`docs/BUILD.md`](docs/BUILD.md) for details

```powershell
git clone https://github.com/Bigeldoth/sc-gps-47.git
cd sc-gps-47
python -m pip install -r requirements.txt
python src/main.py
```

### Build the installer yourself

```powershell
# Prerequisite: Inno Setup 6 (https://jrsoftware.org/isdl.php)
.\tools\build_installer.ps1
# -> dist\SpaceDrive-Setup-vX.Y.Z.exe
```

See [`docs/BUILD.md`](docs/BUILD.md) for the full build + Sandbox-test workflow.

---

## Application updates

Open **Options → Updates → Check for Updates** to check for a release. Optional startup checks are disabled by default; enabling them only notifies you when a newer, unskipped version is available.

- **Update Now** downloads and verifies a differential package for the exact installed version. Other versions use **Download Installer**. Existing releases need the full installer once to acquire the corrected updater.
- SpaceDrive GPS prepares the update, requests administrator permission when needed, and closes safely. A separate Windows helper replaces files after the application exits.
- **Wait for the completion confirmation, then reopen SpaceDrive GPS.** Starting the helper does not mean installation has completed.
- Replacements, additions and deletions are backed up or journaled. An application failure triggers restoration; an interrupted operation offers verified recovery actions or the full installer on the next launch.
- Preferences and personal POIs are preserved. Update logs are in `%LOCALAPPDATA%\SpaceDrive\logs\`; a source checkout is never patched in place.

See [the update audit](docs/UPDATE_AUDIT.md) for compatibility, validation and release details.

Releases follow [Semantic Versioning](https://semver.org/): compatible features increment MINOR, corrections alone increment PATCH, and incompatible changes increment MAJOR. See [the project versioning policy](docs/VERSIONING.md).

---

## In-game configuration

The overlay requires Star Citizen's **debug HUD** to be displayed:

1. Open the game console: **`** key (left of `1` on US keyboard, below `Esc` on FR keyboard).
2. Type `r_DisplayInfo 2` then Enter.
3. Debug HUD appears top right with `CamDir`, `Zone`, `Pos`, `FPS`, etc.

The overlay captures this area automatically.

---

## Keyboard shortcuts (default)

| Shortcut | Action |
|---|---|
| `Shift+F1` | Show/hide overlay |
| `Shift+F2` | Open options window |
| `Shift+F3` | **Quick snapshot** of current coordinates (POI saved at time T) |
| `Shift+F4` | Open POI manager |
| `Shift+F5` | **Stop navigation** (clear current target) |

Shortcuts are reconfigurable via `Shift+F2`.

---

## Configuration (`config.ini`)

| Section | Key | Default value | Description |
|---|---|---|---|
| `[OCR]` | `text_engine` | `tesseract` | `tesseract` or `paddle`. PaddleOCR is more accurate on the HUD font but heavier — installable on demand from the Options dialog. |
| `[OCR]` | `pipeline_mode` | `hybrid` | `hybrid` = NCC/ONNX glyph stage + text engine fallback (fast). `full_text` = skip glyphs and run the text engine alone. |
| `[OCR]` | `paddle_device` | `cpu` | `cpu` or `gpu`. GPU requires a CUDA-enabled `paddlepaddle-gpu` build; the Options dialog disables this when no CUDA runtime is detected. |
| `[OCR]` | `paddle_model_dir` | *(empty)* | Path to a fine-tuned PaddleOCR recognition model (see [tools/train_paddle.py](tools/train_paddle.py)). Empty → use the official PP-OCRv4 weights. |
| `[OCR]` | `scan_interval_ms` | `50` | Interval between two captures (ms) |
| `[Logging]` | `level` | `DEBUG` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `[Debug]` | `save_ocr_images` | `False` | Saves preprocessed images (`debug_capture_*.png`) on each scan |

---

## Architecture

```
┌──────────────────┐
│ ScreenCapture    │  mss → BGR → isolate_channel(auto) → upscale ×3 → CLAHE
│ (capture.py)     │  → 2 binary passes (Otsu / adaptive) + enhanced + raw BGR
└────────┬─────────┘
         ▼
┌──────────────────┐
│ OCRProcessor     │  NCC-first: segment on Otsu, classify on enhanced grayscale
│ (ocr.py)         │  → Tesseract fallback only for labels NCC can't reconstruct
└────────┬─────────┘  → strict Pos regex + range validation
         ▼
┌──────────────────┐
│ NavigationEngine │  Load system + user POIs → set_target → distance
│ (navigation.py)  │  euclidienne planet-relative
└────────┬─────────┘
         ▼
┌──────────────────┐
│ VelocityTracker  │  Per-axis constant-velocity Kalman filter →
│ (velocity_tracker.py) │ movement heading + dead-reckoning (FRESH/COASTING/LOST)
└────────┬─────────┘
         ▼
┌──────────────────┐
│ GPSOverlay       │  PyQt6 transparent always-on-top → MFD frame
│ (main.py)        │  → color based on OCR age
└──────────────────┘
```

**Threading:**
- OCR worker on separate `QThread` (capture + OCR do not block UI).
- `HotkeyListener` (pynput) on its own thread → Qt signals with mandatory `QueuedConnection`.

**Coordinate frame:**
- SC HUD displays **two types of Pos**: `Root/SolarSystem` (relative to system, but planets orbit → unstable for fixed POIs) and `OOC_X` (relative to planet-bound ObjectContainer, **stable**).
- SpaceDrive uses **only OOC coordinates** for POIs and navigation.

---

## Anti-cheat security

SpaceDrive is **100% external**:
- Screenshot capture via `mss` (equivalent to Windows screenshot).
- No reading/writing in Star Citizen process.
- No DLL injection, hooks, or game file modification.
- PyQt6 overlay is a standard Windows window, transparent to clicks.

Star Citizen has used **Easy Anti-Cheat (EAC)** since November 2021. Any memory-reading-based solution (like Sanderling for EVE) would result in a ban. Our OCR approach is the only compatible path.

---

## Contributing

1. Fork → branch `feat/...` or `fix/...`
2. Tests: `python -m pytest tests/`
3. Commits in English, conventional format (`feat:`, `fix:`, `refactor:`)
4. PR to `main`

User-facing code (displayed messages, logs) is in English. Code comments are also in English.

Bug reports and feature requests: [GitHub issues](https://github.com/Bigeldoth/sc-gps-47/issues). OCR pipeline design notes: [`docs/OCR_OPTIMIZATION_PLAN.md`](docs/OCR_OPTIMIZATION_PLAN.md).

---

## License

GNU General Public License v3.0 or later — see [LICENSE.txt](LICENSE.txt).

SpaceDrive depends on PyQt6 (GPL-3) for the overlay; the project is therefore
distributed under the same license to remain compatible.

---

## Credits

- **Tesseract OCR** — main OCR engine
- **mss** — fast screenshot capture
- **PyQt6** — overlay
- **pynput** — global hotkeys

Star Citizen community — `o7`
