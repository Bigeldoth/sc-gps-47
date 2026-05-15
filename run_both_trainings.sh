#!/usr/bin/env bash
set -e
log() { echo "[$(date +'%H:%M:%S')] $*"; }

VIDEOS_DIR="/c/Users/patri/.cursor/projects/spaceDrive/tests"
PY=".venv/Scripts/python.exe"

log "=== Step 1/3: Building Paddle dataset (shared) ==="
rm -rf dataset/paddle_rec dataset/tesseract
$PY scripts/prepare_paddle_dataset.py \
    --videos-dir "$VIDEOS_DIR" \
    --output dataset/paddle_rec \
    --max-frames 1500 \
    --label-engine paddle

log "=== Step 2/3: Tesseract LSTM training ==="
$PY tools/train_tesseract.py \
    --skip-prepare \
    --max-iterations 6000

log "=== Step 3/3: Paddle rec fine-tune ==="
$PY tools/train_paddle.py \
    --skip-prepare \
    --epochs 30

log "=== ALL DONE ==="
ls -la models/tessdata/*.traineddata models/paddle/rec_finetuned/best_accuracy/inference/ 2>&1 | tail -20
