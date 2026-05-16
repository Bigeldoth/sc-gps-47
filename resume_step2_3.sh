#!/usr/bin/env bash
set -e
log() { echo "[$(date +'%H:%M:%S')] $*"; }
PY=".venv/Scripts/python.exe"

log "=== Step 2a: Convert Paddle dataset → Tesseract layout ==="
rm -rf dataset/tesseract
$PY scripts/paddle_to_tesseract_dataset.py \
    --paddle-dir dataset/paddle_rec \
    --output dataset/tesseract

log "=== Step 2b: Tesseract LSTM training ==="
$PY tools/train_tesseract.py --skip-prepare --max-iterations 6000

log "=== Step 3: Paddle rec fine-tune (GPU) ==="
$PY tools/train_paddle.py --skip-prepare --epochs 30 --device gpu

log "=== ALL DONE ==="
