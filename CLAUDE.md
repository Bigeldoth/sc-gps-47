# SpaceDrive GPS — Project Instructions

## Language rules
- **User communication**: French (the user speaks French — always respond in French)
- **Code (comments, docstrings, log messages)**: English only
- **Documentation (.md files)**: English only

All new code, comments, docstrings, and documentation must be written in English.

## Project overview
Star Citizen GPS overlay that reads in-game HUD coordinates via OCR and provides
navigation guidance to user-defined POIs.

## Architecture
- `src/main.py` — PyQt6 app, GPSOverlay + GPSWorker (QThread)
- `src/ocr.py` — OCR pipeline: screen capture → 3 binary passes → Tesseract → regex → consensus
- `src/capture.py` — mss screen capture + channel isolation + 3-pass thresholding
- `src/navigation.py` — bearing/distance calculations (SC coordinate frame: X-axis inverted)
- `src/velocity_tracker.py` — velocity estimation from successive OCR positions (EMA smoothed)
- `src/config_manager.py` — config.ini R/W wrapper
- `src/hotkey_listener.py` — global hotkeys via pynput
- `src/sc_ocr/` — glyph OCR sub-pipeline: segment → classify (NCC or ONNX CNN)

## SC coordinate system
In Star Citizen OOC (planet-relative) frames, the X axis is **inverted** vs. standard
navigation convention (X+ points left, X- points right). All bearing calculations
negate dx before atan2 to compensate.

## Glyph classifier
Active: ONNX CNN (`TinyGlyphCNN`, ~25k params, `models/spacedrive_ocr.onnx`).
Fallback: NCC template matching (NumPy, `data/templates/`).
Toggle via `config.ini` → `[OCR] glyph_engine = onnx|ncc`.

## Tools (offline, not shipped)
- `tools/dataset_builder.py` — auto-label glyphs from video/screen capture
- `tools/dataset_synthetic.py` — generate synthetic glyphs from TTF fonts
- `tools/train_model.py` — train TinyGlyphCNN + export ONNX
