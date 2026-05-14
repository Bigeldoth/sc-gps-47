# SpaceDrive GPS

> GPS navigation overlay for Star Citizen, based on OCR of the debug HUD `r_DisplayInfo 3`.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
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
| `[OCR]` | `engine` | `tesseract` | `tesseract` or `paddle` (paddle is more accurate but much slower on CPU — not recommended for real-time scanning) |
| `[OCR]` | `scan_interval_ms` | `50` | Interval between two captures (ms) |
| `[Logging]` | `level` | `DEBUG` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `[Debug]` | `save_ocr_images` | `False` | Saves preprocessed images (`debug_capture_*.png`) on each scan |

---

## Architecture

```
┌──────────────────┐
│ ScreenCapture    │  mss → BGR → grayscale → upscale ×2 → CLAHE
│ (capture.py)     │  → 4 thresholding passes (fixed / Otsu / adaptive / HSV)
└────────┬─────────┘
         ▼
┌──────────────────┐
│ OCRProcessor     │  ThreadPoolExecutor → Tesseract on each pass
│ (ocr.py)         │  → best pass by score → strict Pos regex
└────────┬─────────┘  → normalization + extraction (x, y, z, ooc)
         ▼
┌──────────────────┐
│ NavigationEngine │  Load system + user POIs → set_target → distance
│ (navigation.py)  │  euclidienne planet-relative
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

**Coming:**
- Phase A — Color channel preprocessing (inspired by SC_OCR)
- Phase B — Geographic range validation + recovery of missing `.`
- Phase C — Tesseract tuning (`classify_bln_numeric_mode`, user_words/patterns)
- Phase D — Custom NCC template matching for digits (~10 ms/frame)

---

## Known bugs

### OCR pipeline

| # | Severity | Description |
|---|----------|-------------|
| 1 | **High** | **Tesseract `0 → 6` digit confusion on bright backgrounds.** When the green channel is selected (daylight scenes, e.g. MicroTech atmosphere), Tesseract occasionally reads `0` as `6` in the integer part of coordinates (e.g. `580` → `586`). This causes ~6 km position errors on affected frames. Not fixable by post-processing; relies on multi-frame consensus to average out. |
| 2 | **Medium** | **NCC/ONNX glyph classifier fails on green-channel frames.** Templates were trained on max_RGB (night/space) preprocessing. When the pipeline selects the green channel (G − R > 15), glyph appearance differs and confidence drops below threshold — nearly all glyphs return `?`. The pipeline falls back to Tesseract-only, losing the accuracy benefit of the custom classifier. |
| 3 | **Low** | **Trailing zero truncation on Z coordinate.** `815.8260` is extracted as `815.826` (3 decimals instead of 4). The strict regex accepts 3-4 decimals so the reading passes, but precision is reduced from 10 cm to 1 m. |

### Navigation

| # | Severity | Description |
|---|----------|-------------|
| 4 | **High** | **Cross-OOC distance check not enforced.** `NavigationEngine.calculate_distance()` returns a numeric distance even when target and player are in different ObjectContainers (e.g. Hurston vs MicroTech). Should return `None`. Related: `is_target_in_same_ooc()` always returns `True`. |
| 5 | **High** | **Velocity bearing sign inverted on X-axis.** `calculate_velocity_bearing()` and `calculate_absolute_bearing()` return inverted yaw when the target is along the X-axis (right/left swapped). The SC X-axis inversion (`-dx`) may be applied incorrectly or doubled. |
| 6 | **Medium** | **Legacy POIs without `ooc` field still navigable.** POIs saved before the OOC-aware update (missing `ooc` key) should be rejected, but `calculate_distance()` still computes a distance for them. |

### Test suite

| # | Severity | Description |
|---|----------|-------------|
| 7 | **Medium** | **`test_bearing.py` broken — stale import.** Imports `calculate_relative_bearing` which was renamed to `calculate_velocity_bearing`. Tests do not collect. |
| 8 | **Medium** | **`test_ocr_camdir.py` broken — stale import.** Imports `_RE_OOC_TAG` which no longer exists in `ocr.py`. Tests do not collect. |

### Documentation

| # | Severity | Description |
|---|----------|-------------|
| 9 | **Low** | **README Roadmap outdated.** Phases A–D are described as "Coming" but are already implemented (channel isolation, range validation, decimal recovery, NCC/ONNX classifiers). |
| 10 | **Low** | **README states 4 thresholding passes.** The pipeline now uses 3 passes (Otsu, Otsu-inv, adaptive). The HSV pass was removed. |

---

## Contributing

1. Fork → branch `feat/...` or `fix/...`
2. Tests: `python -m pytest tests/`
3. Commits in English, conventional format (`feat:`, `fix:`, `refactor:`)
4. PR to `main`

User-facing code (displayed messages, logs) is in English. Code comments are also in English.

---

## License

MIT — see [LICENSE](LICENSE).

---

## Credits

- **Tesseract OCR** — main OCR engine
- **mss** — fast screenshot capture
- **PyQt6** — overlay
- **pynput** — global hotkeys

Star Citizen community — `o7`
