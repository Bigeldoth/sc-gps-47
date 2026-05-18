# SpaceDrive GPS

> GPS navigation overlay for Star Citizen, based on OCR of the debug HUD `r_DisplayInfo 3`.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-yellow.svg)](https://www.python.org/)
[![Anti-cheat](https://img.shields.io/badge/EAC-safe-green.svg)](#anti-cheat-security)

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
- **Tesseract OEM3 fallback** on 2 binary thresholding passes (Otsu / adaptive) in parallel, used for the labels NCC cannot yet recognise (zone names, `CamDir:`). Skipped entirely once NCC reconstructs the full HUD.
- **Strict 3-4 decimal regex**: rejects degraded readings that caused ~17 m errors on saved POIs.
- **Post-OCR normalization**: fixes common artifacts (`Pos:_`, variants `lkm/Km/kn`, parasitic underscores).
- **Capture region**: 600×150 px top right (first 3 HUD lines are enough).

### Navigation
- **System POIs** loaded from `data/poi.json` + **User POIs** in `data/user_poi.json`.
- **3D Euclidean distance** in **planet-relative (OOC)** frame — invariant to planet orbits, unlike Root/SolarSystem frame.
- **Cross-OOC calculation refusal**: if target and player are not in the same ObjectContainer, overlay indicates this instead of showing false distance.
- **Snap-on-large-jump** on distance: on abrupt arrival, bypass EMA to prevent lagging.

### Quick snapshot hotkey
- `Shift+F3` freezes coordinates **at the exact moment of press** in a snapshot — value does not drift while dialog remains open.
- Refuses with overlay message if data is stale (red, > 9 s).

### Star Citizen MFD-style overlay
- Frame with fine amber border, semi-transparent black background.
- `WindowTransparentForInput` → never captures game mouse click.
- `WindowStaysOnTopHint` → stays visible over Star Citizen.
- Coordinate color progresses linearly with elapsed time since last valid OCR (dedicated 150 ms timer, independent of capture cycle).

---

## Installation

### Prerequisites

- **Python 3.10+** ([python.org](https://www.python.org/downloads/) — check "Add to PATH" at installation)
- **Tesseract OCR** ([UB-Mannheim build for Windows](https://github.com/UB-Mannheim/tesseract/wiki))
  - Default installation in `C:\Program Files\Tesseract-OCR\` (auto-detected).
  - Optional alternative: **PaddleOCR** can be installed on demand from
    Options → OCR → *Manage engines…* (pip-only, no system binary).
- **Windows 10/11** (Tesseract Windows paths; Linux/macOS not tested).

### Procedure

```powershell
# 1. Clone the repo
git clone https://github.com/Bigeldoth/sc-gps-47.git
cd sc-gps-47

# 2. Install Python dependencies
python -m pip install -r requirements.txt

# 3. Launch
python src/main.py
```

### Build standalone executable (PyInstaller)

```powershell
python -m PyInstaller --clean spaceDrive.spec
# → dist/spaceDrive.exe
```

See [`docs/BUILD.md`](docs/BUILD.md) for build details.

---

## In-game configuration

The overlay requires Star Citizen's **debug HUD** to be displayed:

1. Open the game console: **`** key (left of `1` on US keyboard, below `Esc` on FR keyboard).
2. Type `r_DisplayInfo 3` then Enter.
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
│ (navigation.py)  │  euclidean planet-relative
└────────┬─────────┘
         ▼
┌──────────────────┐
│ VelocityTracker  │  Sample pos over 50 ms → EMA velocity →
│ (velocity_tracker.py) │ direction of movement
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

## Roadmap

Detailed OCR optimization plan: [`docs/OCR_OPTIMIZATION_PLAN.md`](docs/OCR_OPTIMIZATION_PLAN.md)

**Implemented:**
- Phase A — Color channel preprocessing (`isolate_channel(auto)` in [`src/capture.py`](src/capture.py))
- Phase B — Geographic range validation + missing-decimal recovery (in [`src/ocr.py`](src/ocr.py))
- Phase C — Tesseract tuning (numeric mode, user_words/patterns)
- Phase D — Custom glyph classifier: NCC template matching + `TinyGlyphCNN` ONNX
- Phase E — Decoupled segmentation (Otsu) and classification (CLAHE-enhanced grayscale)

**Known issues:** tracked in [GitHub issues](https://github.com/Bigeldoth/sc-gps-47/issues).

---

## Contributing

1. Fork → branch `feat/...` or `fix/...`
2. Tests: `python -m pytest tests/`
3. Commits in English, conventional format (`feat:`, `fix:`, `refactor:`)
4. PR to `main`

User-facing code (displayed messages, logs) is in English. Code comments are also in English.

---

## License

GNU General Public License v3.0 or later — see [LICENSE](LICENSE).

SpaceDrive depends on PyQt6 (GPL-3) for the overlay; the project is therefore
distributed under the same license to remain compatible.

---

## Credits

- **Tesseract OCR** — main OCR engine
- **mss** — fast screenshot capture
- **PyQt6** — overlay
- **pynput** — global hotkeys

Star Citizen community — `o7`
