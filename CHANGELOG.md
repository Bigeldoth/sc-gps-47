# Changelog — SpaceDrive GPS

All notable changes to this project. Format inspired by [Keep a Changelog](https://keepachangelog.com/), versioning [SemVer](https://semver.org/).

---

## [Unreleased]

### Plan
- Phase A of OCR optimization plan: color channel preprocessing (inspired by SC_OCR).
- See [`docs/OCR_OPTIMIZATION_PLAN.md`](docs/OCR_OPTIMIZATION_PLAN.md).

---

## [0.6.0] — 2026-05-10

### Added
- **Star Citizen MFD-style frame**: fine amber border + separator, semi-transparent black background.
- **Progressive temporal color** on coordinates: linear RGB interpolation green→yellow→orange→red over 12 s, independent of scan count.
- **Dedicated 150 ms timer** for visual refresh — transition stays smooth even if OCR slows or misses scans.
- **Snapshot hotkey**: `Shift+F3` freezes coordinates at time T for quick POI save. Refuses with overlay message if data is red (> 9 s).
- **3D bearing arrow** `_world_arrow`: combines world yaw and pitch in compact arrow (`↑↗→↘↓↙←↖` + `▲`/`▼`).
- **4 thresholding passes**: fixed threshold + Otsu + adaptive + white HSV mask — covers varied backgrounds (space, lit cockpit, planetary surfaces).

### Modified
- **Pos regex requires 3-4 decimals** (`\d{3,4}`) to reject degraded Tesseract readings that caused ~17 m error on saved POIs.
- **Snap-on-large-jump** on distance: if relative gap > 30%, bypass EMA (prevents distance lagging after abrupt arrival).
- **Capture height** reduced to 150 px (first 3 HUD lines are enough).
- **Humanized OOC format**: `Stanton_1_Hurston` → `Stanton 1 Hurston` on display.
- **Unrecognized numeric system IDs** → `Unknown` (instead of displaying `9948564368677`).

### Removed
- `MODE: NAVIGATION` line and orange `SYSTÈME` line from overlay.
- Split-on-km fallback: produced too many false partial extractions (e.g., `X=2` captured from `L2`).

### Documentation
- Complete redesign of `README.md`.
- New `docs/OCR_OPTIMIZATION_PLAN.md` (4-phase plan inspired by SC_OCR).
- `requirements.txt` enriched with minimum versions and comments.
- `setup.py` fixed (dependencies synchronized with requirements).

---

## [0.5.0] — 2026-04 (estimated)

### Added
- `velocity_tracker.py` module: velocity estimator via finite difference on successive OCR positions.
- `calibration.py` module: camera yaw ↔ world calibration.
- Display `Δ X / Y / Z` per axis for stationary guidance.
- Transition from Root to **OOC (planet-relative)** frame for POIs — invariant to planet orbits.

### Modified
- Capture from y=0 to include CamDir line of debug overlay.
- Tolerant CamDir parser for concatenated values (`25-5177` → `[25, -5177]`).

### Tests
- `test_bearing.py`, `test_calibration.py`, `test_navigation_format.py`, `test_ocr_camdir.py`, `test_velocity_tracker.py`.

---

## [1.4.0] — 2026-05-05

### Fixed
- Ultra-tolerant Pos regex: underscore, `kn`, `Km`, `k` alone, no space after `Pos:`.
- Extended OCR corrections: `Zore:` → `Zone:`, variants `SovarSysten/SolarSysten` → `SolarSystem`.

---

## [1.3.0] — 2026-05-04

### Fixed
- Zone regex: properly captures system ID, no longer eats `Pos` at end.
- Smart system matching: partial search in `SYSTEM_ID_MAP`.
- Permanent coordinate display with `---` placeholders when not detected.

---

## [1.2.0] — 2026-05-04

### Fixed
- Support 2560×1440 resolution: automatic capture zone calculation.
- Simplified OCR preprocessing: fixed thresholding at 180, removed aggressive sharpness filter.
- Tolerant `km` regex for variations.
- Tesseract logs via `logger` (not `print`).

### Added
- Debug mode `[Debug] save_ocr_images` in `config.ini`.

---

## [1.1.0] — 2026-05-04

### Fixed
- `Shift+F2` crash (use `QCursor.pos()` instead of `mapToGlobal`).
- Stanton recognition via numeric ID: addition of `SYSTEM_ID_MAP`.

### Added
- CLAHE clipLimit 3.0 → 5.0.
- Detailed OCR process logs (raw text, zones, coordinates).

---

## [1.0.0]

Initial release.
