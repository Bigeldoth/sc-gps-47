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

## Localization — English-only (settled decision)

**The desktop app is English-only. There is NO i18n / translation layer, and none
is planned.** All user-visible strings (labels, button text, window titles,
placeholders, tooltips, QMessageBox text) are plain English literals written
directly in the widgets — see `src/ui/options.py` for the canonical style.

Decision (2026-06-04): an earlier draft of this file mandated a FR/EN
translation layer (`src/i18n.py` + a `t()` helper + a `[UI] language` setting).
That layer was never built, the UI has always been hardcoded English, and the
Star Citizen player base is overwhelmingly English-speaking — so the FR/EN spec
was dropped to remove the contradiction with the "Application UI: English only"
rule above. Do **not** introduce `i18n.py`, a `t()` wrapper, or a language
selector. Reconsider only if the user explicitly asks to internationalize the
desktop app.

Note: the separate **SpaceDrive Community Hub** web project *is* bilingual
(FR/EN via `LangContext.jsx`); that is its own codebase and does not apply here.

## Project overview
Star Citizen GPS overlay that reads in-game HUD coordinates via OCR and provides
navigation guidance to user-defined POIs.

## Navigation design
- **Velocity-based guidance (car-GPS style)**: derives the movement direction from the OCR
  position stream via a per-axis constant-velocity Kalman filter (`VelocityTracker`). Shows
  `↑` (on course) / `→N°` / `←N°` (turn right/left) / `↓` (target behind — U-turn) for the
  required turn toward the target when moving.
- **Dead-reckoning + integrity**: on an OCR dropout (flare / occlusion) the filter coasts on
  the last velocity (`predict_only`) so the arrow survives a brief loss; integrity is
  annunciated FRESH / COASTING (a `DR` tag) / LOST. OCR misreads are rejected by the
  filter's innovation gate.
- **Stationary world compass** (`_world_arrow`): 8-direction compass rose fallback when the
  player is stationary.

## Architecture
- `src/main.py` — PyQt6 app, GPSOverlay + GPSWorker (QThread)
- `src/ocr.py` — OCR pipeline: NCC-first on enhanced grayscale, text engine fallback (Tesseract or PaddleOCR), regex + consensus
- `src/paddle_adapter.py` — thin client preserving the historical `recognize` / `recognize_detailed` API
- `src/paddle_service.py` + `scripts/paddle_worker.py` — JSON IPC sidecar running PaddleOCR in `.venv-paddle/` (Python 3.12); lets the host app run on Python 3.10–3.14
- `src/engine_installer.py` + `src/ui/engine_manager.py` — on-demand install of Paddle into `.venv-paddle/`, CUDA detection
- `src/capture.py` — mss screen capture + channel isolation; emits 2 binary passes (otsu, adaptive) + CLAHE-enhanced grayscale + raw BGR crop (for Paddle's own detection net)
- `src/navigation.py` — bearing/distance calculations (SC coordinate frame: X-axis inverted)
- `src/velocity_tracker.py` — per-axis constant-velocity Kalman filter (position + velocity); innovation gating rejects OCR misreads, predict-only coasting dead-reckons through dropouts
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

### Branch rules (non-negotiable)
- **NEVER push directly to `main`** — this is a hard rule, no exceptions.
- **ALWAYS create a feature branch** before any work: `feat/<name>`, `fix/<name>`, `chore/<name>`, etc.
- Push the branch and open a PR toward `main` with `gh pr create`.
- `main` is the trunk; source and target must differ for GitHub PRs.

### Pushing to main — explicit confirmation required
Pushing directly to `main` (via `git push origin main`) is **forbidden** unless:
1. The user explicitly asks for it in their message, **OR**
2. Claude proposes it, explains why, and the user confirms.

If unsure, always ask before pushing to `main`. The cost of asking is low; the cost of polluting `main` is high (no PR trail, no review, harder to revert).

### Workflow for every task
1. `git checkout -b feat/<name>` (or `fix/`, `chore/`, etc.) — **before** any code change
2. Commit incrementally on the feature branch
3. `git push origin feat/<name>`
4. `gh pr create --base main --head feat/<name>`
5. Never merge or push to `main` directly

## Release workflow
- Follow [the SemVer policy](docs/VERSIONING.md): incompatible changes require MAJOR, compatible new features require MINOR, and fixes alone require PATCH. Assess every change since the last published release, including features already on staging.
- `VERSION` is authoritative. Run `python tools/versioning.py sync --version vX.Y.Z`, check the mirrors and commit before building. Build/release commands never silently increment versions.
- Releases are **local**, not CI: `.\tools\release.ps1 -Version vX.Y.Z` validates the clean source/build, prepares the tag and draft, atomically publishes verified VPS artifacts, then publishes the GitHub draft.
- The VPS hosts installers at `https://padek-interactive.tech/releases/` and a `latest.json` pointer; the 5 most recent versions are kept (older auto-pruned).
- CI (`.github/workflows/build.yml`) only runs on PRs as a smoke-test build — no upload, no artifact (keeps the free-plan storage quota clean).
- VPS credentials live in a gitignored `.env.local` at the repo root (see `docs/BUILD.md`).

## Tools (offline, not shipped)
- `tools/dataset_builder.py` — auto-label glyphs from video/screen capture
- `tools/dataset_synthetic.py` — generate synthetic glyphs from TTF fonts
- `tools/train_model.py` — train TinyGlyphCNN + export ONNX
- `tools/train_paddle.py` + `scripts/prepare_paddle_dataset.py` — fine-tune PP-OCRv4 recognition on a gameplay video
