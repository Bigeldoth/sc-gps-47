"""Build a PaddleOCR recognition dataset from a gameplay video.

For each sampled frame:
  1. Crop the HUD region (top-right).
  2. Segment text rows via the existing `find_glyph_regions` pipeline.
  3. For each row, run the configured OCR engine on the row crop to obtain
     a transcript. Rows whose transcript is empty or fails a sanity check
     (no digits and no expected HUD tokens) are skipped.
  4. Save the row crop as a PNG and append `{relpath}\t{transcript}` to
     train.txt / val.txt (90/10 split, deterministic via seed).

The resulting layout is the PP-OCRv4 recognition format consumed by
`train_paddle.py`:

    <output_dir>/
        images/
            frame_000123_row0.png
            …
        train.txt
        val.txt
        labels.txt   # union of characters seen in transcripts

Usage:
    python scripts/prepare_paddle_dataset.py --video gameplay.mp4 \
        --output dataset/paddle_rec --max-frames 600
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
from pathlib import Path

import cv2
import numpy as np

# Make `src/` and `tools/` importable when run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from sc_ocr.segment import find_glyph_regions  # noqa: E402
from sc_ocr.preprocess import isolate_channel  # noqa: E402

logger = logging.getLogger(__name__)

HUD_W, HUD_H = 600, 45


def _preprocess(frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Returns (binary_for_segmentation, raw_upscaled_for_crop)."""
    channel = isolate_channel(frame_bgr)
    channel = cv2.resize(channel, None, fx=2, fy=2, interpolation=cv2.INTER_LINEAR)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(channel)
    _, binary = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary, enhanced


def _row_bboxes(binary: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Returns one bbox per detected text row, sorted top-to-bottom."""
    seg = find_glyph_regions(binary)
    glyphs = seg.get("glyphs", [])
    if not glyphs:
        return []
    by_row: dict[int, list[dict]] = {}
    for g in glyphs:
        by_row.setdefault(g["row_idx"], []).append(g)
    bboxes = []
    for row_idx in sorted(by_row.keys()):
        row = by_row[row_idx]
        x1 = max(0, min(g["x"] for g in row) - 2)
        y1 = max(0, min(g["y"] for g in row) - 2)
        x2 = min(binary.shape[1], max(g["x"] + g["w"] for g in row) + 2)
        y2 = min(binary.shape[0], max(g["y"] + g["h"] for g in row) + 2)
        if x2 > x1 and y2 > y1:
            bboxes.append((x1, y1, x2, y2))
    return bboxes


def _ensure_tesseract_cmd() -> None:
    """Points pytesseract at the tesseract binary on Windows if the PATH
    lookup fails. Mirrors the search performed by src/ocr.py."""
    import pytesseract
    import shutil
    if shutil.which("tesseract"):
        return
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    import os
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "Tesseract-OCR", "tesseract.exe"))
    for path in candidates:
        if Path(path).exists():
            pytesseract.pytesseract.tesseract_cmd = path
            return


def _transcribe(crop: np.ndarray, engine: str) -> str:
    """Best-effort transcript for a single row crop using the chosen engine."""
    if engine == "tesseract":
        import pytesseract
        _ensure_tesseract_cmd()
        text = pytesseract.image_to_string(
            crop,
            config=(
                "--oem 3 --psm 7 "
                "-c tessedit_char_whitelist="
                "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "abcdefghijklmnopqrstuvwxyz:._- "
            ),
        )
    elif engine == "paddle":
        from paddle_adapter import PaddleAdapter
        global _PADDLE
        try:
            _PADDLE
        except NameError:
            _PADDLE = PaddleAdapter(device="cpu")
        # Paddle expects 3-channel BGR. The row crop arrives as 1-channel
        # grayscale (sliced from the CLAHE-enhanced channel) — convert.
        if crop.ndim == 2:
            crop = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)
        text = _PADDLE.recognize(crop)
    else:
        raise ValueError(f"Unknown labeling engine: {engine}")
    return text.strip().replace("\n", " ")


def _looks_useful(text: str) -> bool:
    """Reject obviously empty or garbage transcripts."""
    if not text or len(text) < 3:
        return False
    has_digit = any(c.isdigit() for c in text)
    has_token = any(tok in text.lower() for tok in ("pos", "zone", "cam", "fov", "km"))
    return has_digit or has_token


def _sample_video(
    video: Path,
    images_dir: Path,
    max_frames: int,
    frame_stride: int,
    label_engine: str,
    seen_hashes: set[int],
    name_prefix: str,
) -> list[tuple[str, str]]:
    """Extract + label HUD rows from one video. Returns (relpath, text) pairs.

    `seen_hashes` is shared across all videos so the same identical crop in
    two clips is only labelled (and saved) once. `name_prefix` namespaces
    the output PNGs so frame indices don't collide between videos.
    """
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {video}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    pairs: list[tuple[str, str]] = []
    idx = 0
    sampled = 0
    while idx < total and sampled < max_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        fh, fw = frame.shape[:2]
        hud = frame[:HUD_H, max(0, fw - HUD_W):]
        binary, enhanced = _preprocess(hud)
        bboxes = _row_bboxes(binary)
        for row_i, (x1, y1, x2, y2) in enumerate(bboxes):
            row_crop = enhanced[y1:y2, x1:x2]
            if row_crop.size == 0:
                continue
            h = hash(row_crop.tobytes())
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            text = _transcribe(row_crop, label_engine)
            if not _looks_useful(text):
                continue
            name = f"{name_prefix}_{idx:06d}_row{row_i}.png"
            cv2.imwrite(str(images_dir / name), row_crop)
            pairs.append((f"images/{name}", text))
        sampled += 1
        idx += frame_stride
    cap.release()
    return pairs


def build_dataset(
    videos: list[Path],
    output_dir: Path,
    max_frames: int,
    frame_stride: int,
    label_engine: str,
    val_ratio: float,
    seed: int,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir = output_dir / "images"
    images_dir.mkdir(exist_ok=True)

    rng = random.Random(seed)
    seen_hashes: set[int] = set()
    pairs: list[tuple[str, str]] = []
    for video in videos:
        prefix = video.stem.replace(" ", "_")[:32]
        v_pairs = _sample_video(
            video, images_dir, max_frames, frame_stride,
            label_engine, seen_hashes, prefix,
        )
        logger.info("[%s] %d labelled rows", video.name, len(v_pairs))
        pairs.extend(v_pairs)

    if not pairs:
        raise RuntimeError("No labeled rows produced — check video and engine setup")

    rng.shuffle(pairs)
    n_val = max(1, int(len(pairs) * val_ratio))
    val, train = pairs[:n_val], pairs[n_val:]

    (output_dir / "train.txt").write_text(
        "\n".join(f"{p}\t{t}" for p, t in train), encoding="utf-8"
    )
    (output_dir / "val.txt").write_text(
        "\n".join(f"{p}\t{t}" for p, t in val), encoding="utf-8"
    )

    charset = sorted({c for _, t in pairs for c in t})
    # `labels.txt` kept for backward compat; `dict.txt` is what PaddleX 3.x
    # rec training actually reads (`Global.character_dict_path`).
    (output_dir / "labels.txt").write_text("\n".join(charset), encoding="utf-8")
    (output_dir / "dict.txt").write_text("\n".join(charset), encoding="utf-8")

    logger.info(
        "Dataset built: %d pairs (%d train / %d val) → %s",
        len(pairs), len(train), len(val), output_dir,
    )


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--video", type=Path, nargs="+",
                   help="One or more gameplay video files (passed as separate args).")
    p.add_argument("--videos-dir", type=Path,
                   help="Directory of .mp4 files to sample from (auto-discovery).")
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--max-frames", type=int, default=600,
                   help="Per-video frame budget (default: 600).")
    p.add_argument("--frame-stride", type=int, default=15,
                   help="Sample one frame every N frames (default: 15 ≈ 4 fps at 60 fps, 2 fps at 30 fps).")
    p.add_argument("--label-engine", choices=["tesseract", "paddle"],
                   default="tesseract",
                   help="Engine used to auto-label rows (Tesseract is faster).")
    p.add_argument("--val-ratio", type=float, default=0.1)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()
    videos: list[Path] = list(args.video or [])
    if args.videos_dir:
        videos.extend(sorted(args.videos_dir.glob("*.mp4")))
    if not videos:
        p.error("Pass at least one --video or --videos-dir.")
    logger.info("Sampling %d videos", len(videos))
    build_dataset(
        videos=videos,
        output_dir=args.output,
        max_frames=args.max_frames,
        frame_stride=args.frame_stride,
        label_engine=args.label_engine,
        val_ratio=args.val_ratio,
        seed=args.seed,
    )


if __name__ == "__main__":
    main()
