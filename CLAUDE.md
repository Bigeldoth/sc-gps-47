# SpaceDrive GPS — Project Instructions

## Language rules
- **User communication**: French (the user speaks French — always respond in French)
- **Code (comments, docstrings, log messages)**: English only
- **Documentation (.md files)**: English only
- **Application UI**: English only — SpaceDrive GPS is an English-only application.
  All widget labels, button text, window titles, placeholders, tooltips, and
  QMessageBox strings must be in English. No French strings in any UI widget.

All new code, comments, docstrings, and documentation must be written in English.

## Window positioning — center + focus
All dialog and secondary windows (Options, POI Manager, Engine Manager, Save POI dialog)
**must open centered on the primary screen** and **receive focus immediately**.

Rationale (user decision): Star Citizen is a flight sim where mouse movement controls
the ship. Opening a window off-center forces the player to move the mouse across the
screen to interact, which triggers ship input and risks a crash. Centering the dialog
means the mouse is already near the controls without needing to move.

Implementation: call `GPSOverlay._center_on_screen(dialog)` (or a shared equivalent)
after `dialog.adjustSize()` and before `dialog.show()`. Then `raise_()` +
`activateWindow()` via `QTimer.singleShot(0, ...)` for deferred focus.

## Localization (i18n) — FR / EN

The desktop UI must support two languages: **French (FR, default)** and **English (EN)**.
All user-visible strings (labels, button text, window titles, placeholders, tooltips,
messages) must go through a central translation layer — never hardcode a French or
English string directly in a widget.

Design contract (mirrors the SpaceDrive Community Hub `LangContext.jsx`):
- Source of truth: `src/i18n.py` — a `TRANSLATIONS` dict with `"FR"` and `"EN"` keys,
  plus a `t(key: str) -> str` helper.
- `config.ini` → `[UI] language = FR` (or `EN`). Default: `FR`.
- `ConfigManager` exposes `get_language()` / `set_language(lang)`.
- `t()` is imported at the top of every UI module; all `QLabel`, `QPushButton`, window
  title, and placeholder strings call `t("key")`.
- Adding a new string: add to both `FR` and `EN` in `src/i18n.py`, then use the key.
- Category labels are translated through `i18n.py` (keys: `cat_industry`, `cat_exploration`, etc.).
- The Options window (General tab) exposes a Language combo (FR / EN); saving reloads all
  open windows or prompts the user to restart.

**This spec applies to all future code changes.** Every PR touching UI must use `t()`.

## Project overview
Star Citizen GPS overlay that reads in-game HUD coordinates via OCR and provides
navigation guidance to user-defined POIs.

## Navigation design
- **Velocity-based guidance (car-GPS style)**: derives movement direction from consecutive
  OCR position samples (VelocityTracker, EMA-smoothed). Shows `↑` / `←N°` / `→N°` arrow
  indicating required turn toward target when moving.
- **Stationary world compass** (`_world_arrow`): 8-direction compass rose fallback when
  player is stationary.

## Architecture
- `src/main.py` — PyQt6 app, GPSOverlay + GPSWorker (QThread)
- `src/ocr.py` — OCR pipeline: NCC-first on enhanced grayscale, text engine fallback (Tesseract or PaddleOCR), regex + consensus
- `src/paddle_adapter.py` — thin client preserving the historical `recognize` / `recognize_detailed` API
- `src/paddle_service.py` + `scripts/paddle_worker.py` — JSON IPC sidecar running PaddleOCR in `.venv-paddle/` (Python 3.12); lets the host app run on Python 3.10–3.14
- `src/engine_installer.py` + `src/ui/engine_manager.py` — on-demand install of Paddle into `.venv-paddle/`, CUDA detection
- `src/capture.py` — mss screen capture + channel isolation; emits 2 binary passes (otsu, adaptive) + CLAHE-enhanced grayscale + raw BGR crop (for Paddle's own detection net)
- `src/navigation.py` — bearing/distance calculations (SC coordinate frame: X-axis inverted)
- `src/velocity_tracker.py` — velocity estimation from successive OCR positions (EMA smoothed)
- `src/config_manager.py` — config.ini R/W wrapper
- `src/hotkey_listener.py` — global hotkeys via pynput
- `src/poi_io.py` — POI serialization + validation for clipboard/file exchange with the SpaceDrive Community hub
- `src/poi_categories.py` — shared taxonomy (6 category slugs + UI labels)
- `src/ui/poi_manager.py` — POI manager window; all POIs are user-owned (no system POIs)
- `src/sc_ocr/` — glyph OCR sub-pipeline: segment on otsu → classify (NCC or ONNX CNN) on enhanced grayscale

## SC coordinate system
In Star Citizen OOC (planet-relative) frames, the X axis is **inverted** vs. standard
navigation convention (X+ points left, X- points right). All bearing calculations
negate dx before atan2 to compensate.

## Glyph classifier
Active: ONNX CNN (`TinyGlyphCNN`, ~25k params, `models/spacedrive_ocr.onnx`).
Fallback: NCC template matching (NumPy, `data/templates/`).
Toggle via `config.ini` → `[OCR] glyph_engine = onnx|ncc`.

## Text engine
`[OCR] text_engine = tesseract|paddle`. Paddle is installed on demand from the
Options dialog (Manage engines…) — it lives in a Python 3.12 sidecar venv
(`.venv-paddle/`) and is invoked via JSON IPC, so the host app stays free to
run on Python 3.10–3.14. `[OCR] pipeline_mode = hybrid|full_text` chooses
between NCC/ONNX + text-engine fallback and a text-engine-only path.

## Logging & debug
- **All runtime logs live under `user_data_dir()/logs/`**: the application log
  (`logs/spacedrive.log`) and per-session navigation telemetry
  (`logs/telemetry-*.jsonl`). Every new log/trace type must go in this folder so
  the user has one coherent, readable location — never scatter logs at the repo
  root or in `user_data_dir()` directly.
- **Every `[Debug]` flag must be toggleable from the Options dialog.** The
  flags in `config.ini` `[Debug]` (`record_telemetry`, `save_ocr_images`,
  `save_glyph_crops`, `verbose_mode`, …) are user-facing
  switches: each must have a matching checkbox in `src/ui/options.py`, persisted
  via `ConfigManager`. Adding a new debug flag means adding its toggle too —
  never ini-edit-only.

## POI schema
All POIs are user-owned. `%LOCALAPPDATA%\SpaceDrive\data\user_poi.json` is the
single source of truth (no bundled system POI file). Exported POIs
share the same shape:

| Field         | Type            | Notes                                                        |
|---------------|-----------------|--------------------------------------------------------------|
| `name`        | string          | Required, non-empty                                          |
| `x`, `y`, `z` | number          | Required; SC OOC frame                                       |
| `location`    | string          | Required; planet/moon/system label, "Unknown" if not set     |
| `ooc`         | string \| null  | ObjectContainer ID (e.g. `Stanton_3a_Lyria`)                 |
| `kind`        | `"surface" \| "space"` | Surface ignores Z for distance/pitch                  |
| `category`    | string slug     | One of the 6 taxonomy slugs below, or `""` (Uncategorized)   |
| `description` | string          | Optional free-form                                           |

### Categories (SpaceDrive Community taxonomy)
Source of truth: `src/poi_categories.py`. Aligned with Community Hub `pois.js` `SPACEDRIVE_TYPES`.

| Slug        | Label      | Color     | Glyph | Description                                      |
|-------------|------------|-----------|-------|--------------------------------------------------|
| `hidden`    | Hidden     | `#19C28A` | ◆     | Secret locations, hidden caches, unmarked sites  |
| `cave`      | Cave       | `#6FE8FF` | ◯     | Caves, underground, underwater, derelict wrecks  |
| `circuit`   | Circuit    | `#D9A368` | ↻     | Routes, relay points, contraband drops           |
| `tactical`  | Tactical   | `#E5484D` | ◤     | Bunkers, PvP/PvE hotspots, hostile zones         |
| `industry`  | Industry   | `#F97316` | ⬡     | Mining nodes, refineries, gas clouds             |
| `logistics` | Logistics  | `#A78BFA` | ◈     | Trade routes, outposts, supply depots            |
| `loot`      | Loot       | `#FBBF24` | ◇     | Containers, medical/ammo caches, loose cargo     |
| `racing`    | Racing     | `#86EFAC` | ▶     | Race circuits, checkpoints, time-trial markers   |

Empty slug `""` = Uncategorized (legacy POIs without a category).

**Legacy slug migration** (`_LEGACY` in `poi_categories.py`):
- `exploration` → `cave`
- `logistics_black_market` → `logistics`
- `hostile_combat` → `tactical`

These old slugs remain valid for import/export backward-compat but are remapped to their v2 canonical slug on display.

### Import / export
- Right-click on a user POI in the POI Manager → "Copy to clipboard" / "Export to JSON file...".
- "Import from clipboard" button accepts either a single JSON object or a one-element array. Strict validation lives in `src/poi_io.parse_poi`.

## Git workflow
- Always work on a **feature branch** (`feat/<name>`, `fix/<name>`, etc.) — never commit directly to `main`.
- Push the branch and open a PR toward `main` with `gh pr create`.
- `main` is the trunk; source and target must differ for GitHub PRs.

## Release workflow
- Releases are **local**, not CI: `.\tools\release.ps1 -Version vX.Y.Z` chains build → SFTP upload to the VPS → git tag → GitHub Release.
- Bump `MyAppVersion` in `installer/spaceDrive.iss` to match the tag before releasing.
- The VPS hosts installers at `https://padek-interactive.tech/releases/` and a `latest.json` pointer; the 5 most recent versions are kept (older auto-pruned).
- CI (`.github/workflows/build.yml`) only runs on PRs as a smoke-test build — no upload, no artifact (keeps the free-plan storage quota clean).
- VPS credentials live in a gitignored `.env.local` at the repo root (see `docs/BUILD.md`).

## Tools (offline, not shipped)
- `tools/dataset_builder.py` — auto-label glyphs from video/screen capture
- `tools/dataset_synthetic.py` — generate synthetic glyphs from TTF fonts
- `tools/train_model.py` — train TinyGlyphCNN + export ONNX
- `tools/train_paddle.py` + `scripts/prepare_paddle_dataset.py` — fine-tune PP-OCRv4 recognition on a gameplay video
