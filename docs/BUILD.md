# Build and deployment

## Local build (PyInstaller)

```powershell
# From project root
python -m PyInstaller --clean spaceDrive.spec
```

**Output**: `dist/spaceDrive.exe` (~80 MB).

The executable is portable: copy it where you want, double-click to launch. Tesseract OCR must be installed separately on the target machine (or bundled in `tesseract/` folder next to exe — see spec).

## Build via GitHub Actions

The `.github/workflows/build.yml` workflow automatically generates a Windows executable:
- On every push to `main` or `develop`.
- Available in the GitHub Actions run artifacts.

## Versioning

[Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

- Git tag for each release: `git tag -a v0.6.0 -m "MFD redesign + OCR precision"`
- `setuptools_scm` reads the tag and exposes version at runtime.

## Local configuration

```powershell
# 1. Copy config.ini.example to config.ini if absent
# (config.ini is versioned by default with sensible values)

# 2. Verify Tesseract is installed
tesseract --version
```

## Tests

```powershell
python -m pytest tests/ -v
```

Covers: navigation, bearing calculation, CamDir parsing, calibration, velocity.

## Debugging

- Logs: `spacedrive.log` (level via `[Logging] level` in `config.ini`).
- Debug captures: enable `[Debug] save_ocr_images = True` → writes `debug_capture_original.png` and `debug_capture_processed.png` on each scan.
- To analyze a static capture without the game: point `[Debug] test_screenshot = path/to/screenshot.png` in `config.ini`.

## Project structure

```
spaceDrive/
├── src/
│   ├── main.py              # GPSOverlay + OCR worker
│   ├── capture.py           # Screen capture + preprocessing
│   ├── ocr.py               # OCRProcessor (Tesseract/Paddle)
│   ├── navigation.py        # NavigationEngine + POI
│   ├── velocity_tracker.py  # Velocity estimation
│   ├── calibration.py       # Camera yaw ↔ world calibration
│   ├── config_manager.py    # config.ini wrapper
│   ├── hotkey_listener.py   # pynput → Qt signals
│   └── ui/
│       ├── options.py       # Options window
│       └── poi_manager.py   # POI window
├── data/
│   ├── poi.json             # System POIs (versioned)
│   └── user_poi.json        # User POIs (local)
├── tests/                   # pytest
├── docs/                    # Documentation
├── config.ini               # Runtime configuration
├── requirements.txt
├── setup.py
└── spaceDrive.spec          # PyInstaller spec
```
