"""Semi-automatic building of glyph dataset for CNN training.

Source: MP4 video or live screen capture. Reuses existing preprocessing +
segmentation + NCC classification pipeline to auto-label detected glyphs with
dual threshold:

  - score >= --high-threshold (default 0.85) → dataset/raw/{char}/         (auto-validated)
  - --low-threshold <= score < high          → dataset/raw/_to_review/{char}/ (to sort)
  - score < --low-threshold                  → ignored

Deduplication via SHA1 of normalized binary crop to avoid exact duplicates.

Usage:
    python tools/dataset_builder.py --source video --video path/to/clip.mp4
    python tools/dataset_builder.py --source screen --preview
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import random
import sys
import time
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from capture import CAPTURE_WIDTH, CAPTURE_HEIGHT, UPSCALE_FACTOR  # noqa: E402
from sc_ocr.classify import classify_single_glyph, normalize_glyph  # noqa: E402
from sc_ocr.preprocess import isolate_channel, otsu_threshold  # noqa: E402
from sc_ocr.segment import find_glyph_regions  # noqa: E402
from sc_ocr.templates import TemplateLibrary  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dataset_builder")

# Characters whose folder names are forbidden on Windows or prone to confusion.
_PATH_SAFE_MAP: dict[str, str] = {
    "/": "_slash",
    "\\": "_bslash",
    ":": "_colon",
    "*": "_star",
    "?": "_qmark",
    '"': "_dquote",
    "<": "_lt",
    ">": "_gt",
    "|": "_pipe",
    ".": "_dot",
    "-": "_dash",
    " ": "_space",
    # Uppercase letters need explicit aliases on Windows: NTFS is case-insensitive,
    # so 'A/' and 'a/' resolve to the same directory and would collide.
    **{c: f"_up{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
}


def safe_dir_name(char: str) -> str:
    """Maps a character to a Windows-compatible folder name."""
    return _PATH_SAFE_MAP.get(char, char)


def preprocess_bgr(image_bgr: np.ndarray) -> np.ndarray:
    """Replicate main preprocessing pipeline on raw BGR frame.

    Steps: auto channel isolation → UPSCALE_FACTOR upscale → CLAHE → Otsu.
    """
    channel = isolate_channel(image_bgr)
    upscaled = cv2.resize(
        channel, None, fx=UPSCALE_FACTOR, fy=UPSCALE_FACTOR,
        interpolation=cv2.INTER_LINEAR,
    )
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(upscaled)
    return otsu_threshold(enhanced)


def crop_glyph(binary: np.ndarray, glyph: dict, pad: int = 2) -> np.ndarray:
    """Crop a glyph with padding, clamped to image borders."""
    x, y, w, h = glyph["x"], glyph["y"], glyph["w"], glyph["h"]
    y1 = max(0, y - pad)
    y2 = min(binary.shape[0], y + h + pad)
    x1 = max(0, x - pad)
    x2 = min(binary.shape[1], x + w + pad)
    return binary[y1:y2, x1:x2]


def crop_hud_region(frame: np.ndarray, hud_w: int, hud_h: int) -> np.ndarray:
    """Crop HUD region (top-right) from video frame."""
    fh, fw = frame.shape[:2]
    x0 = max(0, fw - hud_w)
    return frame[:hud_h, x0:]


def frame_iter_video(
    path: Path,
    skip_min: int,
    skip_max: int,
    hud_w: int,
    hud_h: int,
) -> Iterator[tuple[int, np.ndarray]]:
    """Iterate over video with random skipping, HUD top-right cropping."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {path}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    rng = random.Random(42)
    idx = 0
    try:
        while idx < total:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            yield idx, crop_hud_region(frame, hud_w, hud_h)
            idx += rng.randint(skip_min, skip_max)
    finally:
        cap.release()


def frame_iter_screen(interval_ms: int) -> Iterator[tuple[int, np.ndarray]]:
    """Iterate over live screen captures via mss (ROI top-right SC HUD)."""
    import mss
    sct = mss.mss()
    monitor = sct.monitors[1]
    region = {
        "top": monitor["top"],
        "left": monitor["left"] + monitor["width"] - CAPTURE_WIDTH,
        "width": CAPTURE_WIDTH,
        "height": CAPTURE_HEIGHT,
    }
    idx = 0
    while True:
        raw = sct.grab(region)
        frame = np.asarray(raw, dtype=np.uint8)[:, :, :3]  # BGRA → BGR (first 3 channels)
        yield idx, frame
        idx += 1
        time.sleep(interval_ms / 1000.0)


def draw_overlay(
    frame_bgr: np.ndarray,
    binary: np.ndarray,
    classifications: list[tuple[dict, str | None, float]],
    high: float,
    low: float,
) -> np.ndarray:
    """Draw colored bboxes + labels + scores on original frame (resized 2×), limited to 900px wide."""
    canvas = cv2.resize(frame_bgr, (binary.shape[1], binary.shape[0]), interpolation=cv2.INTER_LINEAR)
    # Reduce for display if too wide
    max_w = 900
    if canvas.shape[1] > max_w:
        scale = max_w / canvas.shape[1]
        canvas = cv2.resize(canvas, (max_w, int(canvas.shape[0] * scale)), interpolation=cv2.INTER_LINEAR)
    for glyph, char, score in classifications:
        x, y, w, h = glyph["x"], glyph["y"], glyph["w"], glyph["h"]
        if score >= high:
            color = (0, 255, 0)
        elif score >= low:
            color = (0, 255, 255)
        else:
            color = (0, 0, 255)
        cv2.rectangle(canvas, (x, y), (x + w, y + h), color, 1)
        label = f"{char or '?'}:{score:.2f}"
        cv2.putText(canvas, label, (x, max(0, y - 3)),
                    cv2.FONT_HERSHEY_PLAIN, 0.7, color, 1, cv2.LINE_AA)
    return canvas


def process_frame(
    frame_bgr: np.ndarray,
    library: TemplateLibrary,
) -> tuple[np.ndarray, list[tuple[dict, str | None, float, np.ndarray]]]:
    """Complete pipeline on frame → classified glyphs.

    Returns:
        (binary_image, [(glyph_meta, char, score, normalized_crop), ...])
    """
    binary = preprocess_bgr(frame_bgr)
    regions = find_glyph_regions(binary)
    results: list[tuple[dict, str | None, float, np.ndarray]] = []
    for glyph in regions["glyphs"]:
        crop = crop_glyph(binary, glyph)
        normalized = normalize_glyph(crop)
        classified = classify_single_glyph(normalized, library)
        results.append((glyph, classified["char"], float(classified["score"]), normalized))
    return binary, results


def save_glyph(
    crop_normalized: np.ndarray,
    char: str,
    out_root: Path,
    review: bool,
    seen_hashes: set[str],
    source_tag: str,
    frame_idx: int,
    glyph_idx: int,
) -> bool:
    """Save normalized glyph if not duplicate. Returns True if written."""
    digest = hashlib.sha1(crop_normalized.tobytes()).hexdigest()[:8]
    if digest in seen_hashes:
        return False
    seen_hashes.add(digest)

    if review:
        target_dir = out_root / "_to_review" / safe_dir_name(char)
    else:
        target_dir = out_root / safe_dir_name(char)
    target_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{source_tag}_{frame_idx:06d}_{glyph_idx:03d}_{digest}.png"
    cv2.imwrite(str(target_dir / filename), crop_normalized)
    return True


def run(args: argparse.Namespace) -> int:
    library = TemplateLibrary(template_dir=args.templates)
    if not library.has_templates():
        logger.error("No NCC templates loaded — auto-labeling impossible.")
        return 1

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    if args.source == "video":
        if not args.video:
            logger.error("--video <path.mp4> required in video mode")
            return 2
        video_path = Path(args.video)
        source_tag = video_path.stem
        frames = frame_iter_video(video_path, args.skip_frames_min, args.skip_frames_max,
                                   args.hud_width, args.hud_height)
    else:
        source_tag = "screen"
        frames = frame_iter_screen(args.screen_interval_ms)

    seen_hashes: set[str] = set()
    stats = {"frames": 0, "auto": 0, "review": 0, "skipped": 0}

    try:
        for frame_idx, frame_bgr in frames:
            stats["frames"] += 1
            binary, results = process_frame(frame_bgr, library)

            overlay_pairs: list[tuple[dict, str | None, float]] = []
            for glyph_idx, (glyph, char, score, crop) in enumerate(results):
                overlay_pairs.append((glyph, char, score))
                if char is None or score < args.low_threshold:
                    stats["skipped"] += 1
                    continue
                review = score < args.high_threshold
                wrote = save_glyph(
                    crop, char, out_root, review, seen_hashes,
                    source_tag, frame_idx, glyph_idx,
                )
                if not wrote:
                    stats["skipped"] += 1
                    continue
                if review:
                    stats["review"] += 1
                else:
                    stats["auto"] += 1

            if args.preview:
                overlay = draw_overlay(frame_bgr, binary, overlay_pairs,
                                       args.high_threshold, args.low_threshold)
                cv2.imshow("dataset_builder", overlay)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    logger.info("Stop requested (q)")
                    break

            if stats["frames"] % 50 == 0:
                logger.info(
                    "frames=%d  auto=%d  review=%d  skipped=%d  uniques=%d",
                    stats["frames"], stats["auto"], stats["review"],
                    stats["skipped"], len(seen_hashes),
                )
    except KeyboardInterrupt:
        logger.info("Interrupted (Ctrl+C)")
    finally:
        if args.preview:
            cv2.destroyAllWindows()

    logger.info("=== DONE ===")
    logger.info("Frames processed : %d", stats["frames"])
    logger.info("Auto glyphs      : %d  (→ %s/)", stats["auto"], out_root)
    logger.info("Glyphs to review : %d  (→ %s/_to_review/)", stats["review"], out_root)
    logger.info("Skipped/dupes    : %d", stats["skipped"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=("video", "screen"), required=True)
    p.add_argument("--video", type=str, default=None, help="MP4 path (video mode)")
    p.add_argument("--out", type=str, default="dataset/raw")
    p.add_argument("--templates", type=str, default="data/templates")
    p.add_argument("--high-threshold", type=float, default=0.85, dest="high_threshold")
    p.add_argument("--low-threshold", type=float, default=0.65, dest="low_threshold")
    p.add_argument("--skip-frames-min", type=int, default=1, dest="skip_frames_min")
    p.add_argument("--skip-frames-max", type=int, default=15, dest="skip_frames_max")
    p.add_argument("--screen-interval-ms", type=int, default=200, dest="screen_interval_ms")
    p.add_argument("--hud-width", type=int, default=800, dest="hud_width",
                   help="HUD region width (px, from right edge). 600 for 1080p, 800 for 1440p.")
    p.add_argument("--hud-height", type=int, default=200, dest="hud_height",
                   help="HUD region height (px, from top). 150 for 1080p, 200 for 1440p.")
    p.add_argument("--preview", action="store_true", help="Display debug overlay (q to quit)")
    return p


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
