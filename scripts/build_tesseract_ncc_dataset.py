"""Build a Tesseract LSTM training dataset using ONNX glyph validation.

For each frame of each input video:

1. Crop the top-right HUD strip.
2. Apply the same preprocessing the live runtime does
   (channel isolation + x3 upscale + CLAHE + Otsu).
3. Split into horizontal text bands with ``OCRProcessor._find_text_rows``.
4. For each band, segment glyphs (``find_glyph_regions``) and classify them
   with the ``TinyGlyphCNN`` ONNX model.
5. Keep rows where:
     - At least N glyphs are classified with confidence >= --min-confidence
     - The reconstructed digit string (after a virtual ``Pos: `` prefix)
       matches our strict Pos regex (3-4 decimal places per coord, ``km``
       suffixes).
6. Crop the row image from the first confidently-classified glyph onwards
   (skips the alphabetic ``Zone: ...Pos:`` prefix that the ONNX vocabulary
   can't validate) and write a ``<stem>.png`` + ``<stem>.gt.txt`` pair
   ready for ``tools/train_tesseract.py``.

The resulting labels are character-exact on the digit portion (ONNX
confidence floor guarantees that), so Tesseract LSTM training has a clean
target. Run with several gameplay videos at once via ``--video v1.mp4 v2.mp4 ...``
or ``--videos-dir <dir>``.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from capture import UPSCALE_FACTOR  # noqa: E402
from sc_ocr.preprocess import isolate_channel  # noqa: E402
from sc_ocr.segment import find_glyph_regions  # noqa: E402
from sc_ocr.templates import TemplateLibrary  # noqa: E402
from sc_ocr.onnx_classifier import ONNXGlyphClassifier  # noqa: E402
from ocr import OCRProcessor, _RE_POS, _normalize_ooc_line  # noqa: E402

logger = logging.getLogger(__name__)

HUD_W, HUD_H = 600, 45


def _preprocess(hud_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Mirrors src/capture.py preprocessing. Returns (otsu_binary, enhanced)."""
    channel = isolate_channel(hud_bgr)
    channel = cv2.resize(channel, None, fx=UPSCALE_FACTOR, fy=UPSCALE_FACTOR,
                         interpolation=cv2.INTER_LINEAR)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(channel)
    _, otsu = cv2.threshold(enhanced, 0, 255,
                            cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return otsu, enhanced


def _label_row(
    row_otsu: np.ndarray,
    row_enhanced: np.ndarray,
    classifier: ONNXGlyphClassifier,
    template_lib: TemplateLibrary,
    min_glyphs: int = 8,
) -> tuple[str, int] | None:
    """Classify glyphs in a row crop. Returns (label, start_x_offset) or None.

    The label is the ONNX-validated digit reconstruction with an implicit
    ``Pos: `` prefix; ``start_x_offset`` is the x position in the row crop
    where the first confidently-classified glyph begins, so the caller can
    crop the image to start exactly at the digits (no alphabetic prefix).
    """
    seg = find_glyph_regions(row_otsu)
    glyphs = seg.get("glyphs", []) if isinstance(seg, dict) else []
    if len(glyphs) < min_glyphs:
        return None

    # Sort glyphs by x so the reconstructed string follows reading order.
    glyphs_sorted = sorted(glyphs, key=lambda g: g["x"])

    # Build crops from the enhanced grayscale (matches the runtime NCC path).
    glyph_imgs = []
    for g in glyphs_sorted:
        x, y, w, h = g["x"], g["y"], g["w"], g["h"]
        crop = row_enhanced[y:y + h, x:x + w]
        if crop.size > 0:
            glyph_imgs.append((g["id"], crop))

    cls = classifier.classify_batch(
        glyph_imgs, template_lib, glyphs_meta=glyphs_sorted,
    )
    char_by_id = {c["glyph_id"]: c["char"] for c in cls}

    # Build the reconstruction from confident glyphs ONLY (skipping the
    # alphabetic prefix "Zone:OOC_Stanton_…Pos:" that ONNX can't classify
    # — its vocabulary is just digits + ``k``/``m`` + ``-``). Spaces are
    # inserted between confident chars whenever the horizontal gap between
    # them exceeds a small threshold, which is how we recover the visual
    # ``X<space>Y<space>Z`` layout of the Pos line.
    confident: list[tuple[int, str]] = []  # (x_position, char)
    for g in glyphs_sorted:
        c = char_by_id.get(g["id"])
        if c is None:
            continue
        confident.append((g["x"], c))
    if len(confident) < 6:
        return None

    chars: list[str] = []
    prev_x_end = None
    first_x = confident[0][0]
    for g in glyphs_sorted:
        c = char_by_id.get(g["id"])
        if c is None:
            continue  # skip unclassifiable glyphs
        if prev_x_end is not None and (g["x"] - prev_x_end) > 18:
            chars.append(" ")
        chars.append(c)
        prev_x_end = g["x"] + g["w"]
    reconstructed = "".join(chars).strip()
    # Virtual prefix so the existing _RE_POS regex matches.
    normalized = _normalize_ooc_line("Pos: " + reconstructed)
    m = _RE_POS.search(normalized)
    if not m:
        return None

    x_s, y_s, z_s = m.group(1), m.group(2), m.group(3)
    # Canonical form: `Pos:<x>km <y>km <z>km` — no Zone prefix, no Microtech
    # blah-blah; Tesseract only has to learn the digit grammar.
    label = f"Pos:{x_s}km {y_s}km {z_s}km"
    return label, first_x


def build_dataset(
    videos: list[Path],
    output_dir: Path,
    max_frames: int,
    frame_stride: int,
    min_confidence: float,
    val_ratio: float,
    seed: int,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    classifier = ONNXGlyphClassifier(
        str(ROOT / "models" / "spacedrive_ocr.onnx"),
        str(ROOT / "models" / "spacedrive_ocr.classes.json"),
        confidence_threshold=min_confidence,
    )
    template_lib = TemplateLibrary()  # required by classify_batch signature

    pairs_log = output_dir / "ncc_pairs.tsv"
    written_stems: list[str] = []
    if pairs_log.is_file():
        for line in pairs_log.read_text(encoding="utf-8").splitlines():
            if "\t" in line:
                written_stems.append(line.split("\t", 1)[0])
        if written_stems:
            logger.info("Resuming: %d pairs already in %s",
                        len(written_stems), pairs_log)
    log_fh = pairs_log.open("a", encoding="utf-8")

    seen_prefixes = {Path(s).stem.rsplit("_", 2)[0] for s in written_stems}

    total_kept = len(written_stems)
    try:
        for video in videos:
            prefix = video.stem.replace(" ", "_")[:32]
            if prefix in seen_prefixes:
                logger.info("[%s] already done — skipping", video.name)
                continue
            cap = cv2.VideoCapture(str(video))
            if not cap.isOpened():
                logger.warning("Cannot open %s", video)
                continue
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            kept_for_video = 0
            idx = 0
            sampled = 0
            seen_hashes: set[int] = set()
            while idx < total_frames and sampled < max_frames:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                fh, fw = frame.shape[:2]
                hud = frame[:HUD_H, max(0, fw - HUD_W):]
                otsu, enhanced = _preprocess(hud)
                bands = OCRProcessor._find_text_rows(otsu)
                for row_i, (y0, y1) in enumerate(bands):
                    pad = 4
                    yA = max(0, y0 - pad)
                    yB = min(otsu.shape[0], y1 + pad)
                    row_otsu = otsu[yA:yB]
                    row_enh = enhanced[yA:yB]
                    # Re-base glyph y to the row crop (find_glyph_regions
                    # returns global y but we just sliced — pass the crop).
                    result = _label_row(row_otsu, row_enh, classifier, template_lib)
                    if result is None:
                        continue
                    label, first_x = result
                    # Dedup by hash so identical bands across frames are
                    # not written twice (saves disk + avoids overfit).
                    img_to_save = row_otsu[:, max(0, first_x - 8):]
                    if img_to_save.size == 0:
                        continue
                    h = hash(img_to_save.tobytes())
                    if h in seen_hashes:
                        continue
                    seen_hashes.add(h)
                    stem = f"{prefix}_{idx:06d}_row{row_i}"
                    cv2.imwrite(str(output_dir / f"{stem}.png"), img_to_save)
                    (output_dir / f"{stem}.gt.txt").write_text(
                        label, encoding="utf-8")
                    written_stems.append(stem)
                    log_fh.write(f"{stem}\t{label}\n")
                    log_fh.flush()
                    kept_for_video += 1
                    total_kept += 1
                sampled += 1
                idx += frame_stride
            cap.release()
            logger.info("[%s] kept %d rows", video.name, kept_for_video)
    finally:
        log_fh.close()

    if total_kept == 0:
        raise RuntimeError("No usable rows produced — check ONNX threshold "
                           "or input videos.")

    # Build train.stems.txt / eval.stems.txt for tools/train_tesseract.py
    import random
    rng = random.Random(seed)
    shuffled = list(written_stems)
    rng.shuffle(shuffled)
    n_val = max(1, int(len(shuffled) * val_ratio))
    val = shuffled[:n_val]
    train = shuffled[n_val:]
    (output_dir / "train.stems.txt").write_text("\n".join(train), encoding="utf-8")
    (output_dir / "eval.stems.txt").write_text("\n".join(val), encoding="utf-8")
    logger.info("Dataset built: %d train + %d eval pairs → %s",
                len(train), len(val), output_dir)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--video", type=Path, nargs="+",
                   help="One or more gameplay video files.")
    p.add_argument("--videos-dir", type=Path,
                   help="Directory of .mp4 files to sample.")
    p.add_argument("--output", type=Path, required=True,
                   help="Tesseract pair layout destination.")
    p.add_argument("--max-frames", type=int, default=1500)
    p.add_argument("--frame-stride", type=int, default=15)
    p.add_argument("--min-confidence", type=float, default=0.85,
                   help="ONNX glyph confidence floor.")
    p.add_argument("--val-ratio", type=float, default=0.1)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()
    videos: list[Path] = list(args.video or [])
    if args.videos_dir:
        videos.extend(sorted(args.videos_dir.glob("*.mp4")))
    if not videos:
        p.error("Pass at least one --video or --videos-dir.")
    build_dataset(
        videos=videos,
        output_dir=args.output.resolve(),
        max_frames=args.max_frames,
        frame_stride=args.frame_stride,
        min_confidence=args.min_confidence,
        val_ratio=args.val_ratio,
        seed=args.seed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
