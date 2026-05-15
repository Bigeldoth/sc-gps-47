# Testing and training guide

Reference document for every test, evaluation, and training workflow available
in SpaceDrive GPS. Read this before adding a new path — most needs are already
covered.

## TL;DR by intent

| I want to… | Use |
|---|---|
| Verify a code change didn't regress core logic | `pytest tests/` |
| Diagnose a "red overlay / bad coordinate" gameplay issue | Run app + read `spacedrive.log` — see §3 |
| Improve glyph-level recognition (digits, letters) | Retrain TinyGlyphCNN — see §4 |
| Improve full-line OCR (faster engine, full HUD reads) | Fine-tune PaddleOCR rec — see §5 |
| A/B-test two OCR engines on the same scene | `tools/paddle_diagnose.py` or record + replay (see §6) |

Always activate the venv first: `.venv\Scripts\Activate.ps1` (Windows) or
`source .venv/bin/activate` (POSIX). PaddleOCR + paddlepaddle require Python
3.12 in this venv.

---

## 1. Unit tests (`tests/test_*.py`)

Pure-Python tests, no GPU, no Tesseract binary required for most of them
(except `test_ocr_camdir` which imports `pytesseract`).

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

Files:

- `test_bearing.py` — `normalize_angle_signed`, `ema_angle`,
  `calculate_relative_bearing`. SC X-axis inversion lives here.
- `test_navigation_format.py` — `format_distance` thresholds (m / km / Mm),
  `NavigationEngine.calculate_distance` corner cases.
- `test_velocity_tracker.py` — EMA smoothing, `is_moving` gate, reset on time
  gap, `calculate_velocity_bearing` against synthetic trajectories.
- `test_ocr_camdir.py` — `_RE_CAMDIR_TAG` tolerance to OCR variants
  (missing `C`, missing `:`), Pos filtering against absolute-frame lines.

Add new tests next to the module they exercise. Keep them dependency-free
when possible — if you need cv2/numpy/torch, isolate the fixture so a
contributor without those libs can still run the rest of the suite.

---

## 2. Sample-image regression fixtures

`tests/sample_daylight_microtech 580.4026km -14.7753km 815.8260km.png` —
ground-truth-labeled in its filename. Use it to verify a code change still
recovers `X=580.4026, Y=-14.7753, Z=815.8260` from a static frame. Add new
samples by capturing a clean HUD region (top-right 600×45) and naming the
file with the expected coordinates space-separated. A future automated
suite can glob `tests/sample_*.png` and assert against the filename.

---

## 3. Live gameplay diagnostic

The primary "is this fix working" loop is run-the-app + read-the-log.

### Run

```powershell
.venv\Scripts\python.exe src\main.py
```

### Log location

`spacedrive.log` (project root). Level controlled by `config.ini → [Logging]
level = DEBUG`. The default is `DEBUG` while developing.

### Things to grep for

| Pattern | Meaning |
|---|---|
| `[paddle-stats] frames=N accepted=…` | Cumulative Paddle accept/reject counters, emitted every 50 frames. Surfaces `no_text / below_conf / regex_fail / out_of_range / consensus_hits`. |
| `Multi-pass consensus: X=… Y=… Z=…` | Two or more sources (OTSU Tesseract + ADAPTIVE Tesseract + ONNX vote) converged within `_CONSENSUS_TOL_KM` and were averaged. The healthy state. |
| `ONNX sanity gate:` | Tesseract's best pass disagreed by >50 km with ONNX → ONNX preferred. Indicates Tesseract is hallucinating. |
| `OCR rejection: sign-flip on X` | A `-X.XXX → +X.XXX` mirror-flip was caught and rejected (F2 gate). |
| `OCR rejection: implausible X-axis speed` | Per-axis 50 km/s cap fired (F1 gate). |
| `OCR rejection: implausible 3D speed` | 3D-norm backstop fired (F1 gate). |
| `Position extracted: …` | Pre-consensus, per-pass extraction. Don't trust as the "final" value. |
| `Position recovered via …regex` | A relaxed regex saved the frame after the strict `_RE_POS` failed. |

### Quick stats over a session

```bash
grep -c "Position extracted" spacedrive.log    # total per-pass reads
grep -c "Multi-pass consensus" spacedrive.log  # consensus hits
grep -c "OCR rejection" spacedrive.log         # rejected by velocity/sign gates
grep "paddle-stats" spacedrive.log | tail -1   # final accumulated counters
```

Healthy session targets: consensus hits > 50 % of acceptances, rejections
under 10 %, no cascade of >5 consecutive `OCR rejection` lines (cascades
mean the buffer is stuck).

---

## 4. Glyph-level training: `TinyGlyphCNN` → ONNX

Used by the NCC-first hybrid pipeline (`config.ini → [OCR] glyph_engine =
onnx`). Recognises one character at a time on a 24×16 binary crop.

### Inputs

- **Real labeled crops**: `dataset/raw/{char_safe}/*.png` — auto-collected
  with `tools/dataset_builder.py` from video or live capture, dual-threshold
  (high → auto, mid → `_to_review`). Source: gameplay MP4 or live screen.
- **Synthetic crops**: `dataset/synthetic/{char_safe}/*.png` — generated
  from a TTF that matches the SC HUD font (`tools/find_sc_font.py` helps).

### Pipeline

```powershell
# 1. Auto-label real glyphs from a gameplay video.
.venv\Scripts\python.exe tools\dataset_builder.py --source video --video path\to\clip.mp4

# 2. Manually sort dataset/raw/_to_review/ into dataset/raw/{char}/.
#    Skip this step if you trust the auto-labeling on a high threshold.

# 3. (Optional) Generate synthetic glyphs for class balance.
.venv\Scripts\python.exe tools\dataset_synthetic.py --font tools\fonts\Electrolize-Regular.ttf --samples-per-char 200

# 4. Train + export ONNX.
.venv\Scripts\python.exe tools\train_model.py --data-real dataset\raw --data-synth dataset\synthetic --out models\ --epochs 30
```

Produces `models/spacedrive_ocr.onnx` and `models/spacedrive_ocr.classes.json`,
both auto-loaded by the runtime. Validate the export: the script runs an
ONNXRuntime sanity check vs the PyTorch checkpoint at the end.

### When to retrain

- The HUD font changed (CIG patch).
- A new character must be recognised (e.g. dataset only had digits + caps;
  now we need `:` and lowercase).
- Confusion matrix shows a stubborn 6↔8 or 0↔6 → harvest more real samples
  for those classes, bump `real_weight`, retrain.

---

## 5. Full-line training: PaddleOCR rec fine-tune

Used when `config.ini → [OCR] text_engine = paddle`. The rec model reads an
entire row at once (Pos coordinates, Zone name, CamDir).

### One-shot orchestrator

```powershell
.venv\Scripts\python.exe tools\train_paddle.py --video C:\path\to\gameplay.mp4 --max-frames 1000 --epochs 20
```

The orchestrator:

1. `pip install paddlepaddle paddleocr` if missing.
2. Calls `scripts/prepare_paddle_dataset.py` to extract rows + auto-label.
3. Writes a PP-OCRv4 rec YAML config and launches `paddleocr.tools.train`.
4. Output: `models/paddle/rec_finetuned/inference/`.

### Manual two-step (preferred when iterating on label quality)

```powershell
# Step 1 — build the labeled dataset. Override default labeler to Paddle
#   (better than Tesseract on the SC font in our experience).
.venv\Scripts\python.exe scripts\prepare_paddle_dataset.py `
    --video C:\path\to\gameplay.mp4 --output dataset\paddle_rec `
    --max-frames 1000 --label-engine paddle

# Step 2 — train, skipping the prepare phase.
.venv\Scripts\python.exe tools\train_paddle.py `
    --video C:\path\to\gameplay.mp4 --dataset-dir dataset\paddle_rec `
    --skip-prepare --epochs 20
```

Dataset layout produced:

```
dataset/paddle_rec/
    images/frame_<idx:06d>_row<k>.png
    train.txt          # 90 % of pairs, `<relpath>\t<transcript>`
    val.txt            # 10 %
    labels.txt         # union of characters across transcripts
```

### Wiring the fine-tuned model

After training:

```ini
# config.ini
[OCR]
text_engine = paddle
paddle_model_dir = models/paddle/rec_finetuned/inference
paddle_min_confidence = 0.50    # drop per-region reads below this score
```

The Options dialog also exposes these fields (Options → OCR → Paddle device /
model fields). Restart of the OCR worker is automatic on save.

### Next iteration of the Paddle fine-tune (TODO for a dedicated session)

The first fine-tune attempt (`models/paddle/rec_finetuned/` from `record2.mp4`,
153 train / 16 val) suffered **catastrophic forgetting** — the model
collapsed to outputting `'0'` and spaces, accept rate dropped to 0 %. The
dataset was too small and too uniform (single clip, single zone, single
lighting condition) for a full SVTR_LCNet fine-tune.

For the next attempt:

- **Diversify the dataset**: capture 3-5 gameplay clips covering different
  zones (Stanton: ArcCorp, Hurston, MicroTech, Crusader), different lighting
  (day, night, atmosphere transit), and a mix of stationary vs in-motion
  positions. Target ≥1500 train / ≥150 val pairs.
- **Reduce learning rate to 1e-5** (10× lower than the default) and **freeze
  the backbone** for the first half of epochs — only the head/CTC layers
  adapt. Avoids wrecking the well-trained character priors.
- **Sanity-eval against pretrained**: before/after CER and accept rate on a
  held-out clip. If post-training is worse than pretrained, drop the fine-tune
  and keep using the official PP-OCRv4 weights.
- Document the run in `models/paddle/rec_finetuned/training_notes.md` so the
  next dataset/version is reproducible.

Until then the runtime defaults to the pretrained PP-OCRv4 rec; the custom
`paddle_model_dir` line in `config.ini` is left commented as a reminder.

### Known pitfalls

- **PaddleOCR requires Python 3.12** in our venv setup. Other Python versions
  fail at import time on Windows.
- **PaddleX 3.x training plumbing is fragile**. Required one-time setup:
  1. `mkdir -p .venv\Lib\site-packages\paddlex\repo_manager\repos` (the
     installer fails if this parent is missing).
  2. `python -m paddlex --install PaddleOCR -y` (clones PaddleOCR repo for
     module configs and pulls `paddlenlp`).
  3. Copy `repos\PaddleOCR\configs\rec\PP-OCRv4\en_PP-OCRv4_mobile_rec.yml`
     to `paddlex\repo_apis\PaddleOCR_api\configs\en_PP-OCRv4_mobile_rec.yaml`
     (note the `.yml`→`.yaml` rename). The installer doesn't do this
     automatically and `build_trainer()` hard-codes the `.yaml` path.
  4. Training itself goes via the Python API
     (`build_trainer(parse_config(yml))`) — the PaddleX CLI accepts only
     pipelines, not `-c module_config.yaml`. `tools/train_paddle.py` does
     this correctly; don't try `python -m paddlex -c …` directly.
- **Tesseract not on PATH**: `prepare_paddle_dataset.py` with
  `--label-engine tesseract` will crash unless the Tesseract binary is on
  PATH or in `C:\Program Files\Tesseract-OCR\`. The script falls back to
  the same standard locations as `src/ocr.py`.
- **Grayscale crops**: `prepare_paddle_dataset.py` slices crops from the
  CLAHE-enhanced channel (1-channel grayscale). Paddle needs BGR — the
  script converts before calling. Don't bypass this when extending.

---

## 6. Engine comparison / replay

### In-app Paddle diagnostic

Options → OCR → "Run PaddleOCR diagnostic". Captures N live HUD strips,
runs Paddle on each, saves results + bounding-box overlays in
`diagnostics/paddle/<timestamp>/`. The summary HTML is a human-readable A/B
between detection passes.

### `tools/paddle_diagnose.py`

CLI counterpart. Useful to batch-compare a fine-tuned model against the
pretrained one on a folder of saved HUD crops.

### Video replay (not yet automated)

We don't have a "feed a gameplay MP4 into the live pipeline" runner today.
Workaround: play the MP4 fullscreen on the same monitor + launch the app —
the screen-capture loop reads the video frames the same way it would read
the live game. `record2.mp4` (1440p 30 fps, ~71 s) is the reference clip
used while developing the F1-F5 noise-rejection gates.

If you want a deterministic replay (no timing jitter from the playback
window), see the open task to add a `--video` flag to `src/capture.py`.

---

## 7. Where to look when something is off

| Symptom | First grep / check |
|---|---|
| Overlay stays red | `[paddle-stats]` cumulative line + count of `OCR rejection` |
| Coordinates jump 1500 km | `OCR rejection: sign-flip` count (F2 should catch nearly all) |
| Coordinates jump ~60 km | `OCR rejection: implausible Y-axis` (0↔6 confusion) |
| Position oscillates by ~200 m | Expected — median smoothing absorbs ±1 LSB noise on the 4th decimal |
| Multi-pass consensus rate < 30 % | OTSU/ADAPTIVE disagree too much. Bump `_CONSENSUS_TOL_KM` or check binarisation thresholds |
| Paddle says nothing on every frame | `[paddle] inference: … ms, 0 region(s)` — detection net fails. Try a larger upscale factor in `paddle_adapter._UPSCALE_FACTOR` or feed BGR vs grayscale |

---

## 8. Files cheat sheet

```
tests/                          ← unit tests + this doc + sample fixtures
tools/dataset_builder.py        ← real glyph harvesting
tools/dataset_synthetic.py      ← TTF-based synthetic glyphs
tools/train_model.py            ← TinyGlyphCNN training + ONNX export
tools/train_paddle.py           ← Paddle rec end-to-end orchestrator
tools/paddle_diagnose.py        ← CLI Paddle diagnostic
scripts/prepare_paddle_dataset.py  ← video → labeled row dataset for Paddle
models/                         ← shipped checkpoints (ONNX + Paddle inference)
dataset/                        ← scratch space for labeled crops (gitignored)
```
