"""Synthetic glyph generation for pre-training / data augmentation.

Renders each target character via TTF font (ideally identified by
tools/find_sc_font.py) with aggressive jitter simulating SC HUD conditions:
translation ±2 px, scale 0.95-1.05, rotation ±1°, brightness ±30%, Gaussian
bloom on saturated pixels, additive noise.

Output: dataset/synthetic/{char_safe}/{i:04d}.png (16×24 binary uint8).

Usage:
    python tools/dataset_synthetic.py --font tools/fonts/Electrolize-Regular.ttf \\
        --samples-per-char 200 --chars "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ.-:/km"
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tools.dataset_builder import safe_dir_name  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dataset_synthetic")

GLYPH_W = 16
GLYPH_H = 24


def render_base(font: ImageFont.FreeTypeFont, char: str, oversample: int = 4) -> np.ndarray:
    """Render anti-aliased character on oversampled canvas, cropped."""
    w = GLYPH_W * oversample
    h = GLYPH_H * oversample
    canvas = Image.new("L", (w, h), color=0)
    draw = ImageDraw.Draw(canvas)
    bbox = draw.textbbox((0, 0), char, font=font)
    cx = (w - (bbox[2] - bbox[0])) // 2 - bbox[0]
    cy = (h - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((cx, cy), char, fill=255, font=font)
    return np.asarray(canvas, dtype=np.uint8)


def augment_and_binarize(
    base: np.ndarray,
    rng: random.Random,
) -> np.ndarray:
    """Apply jitter + bloom + noise then binarize to 16×24."""
    h, w = base.shape

    angle = rng.uniform(-1.5, 1.5)
    scale = rng.uniform(0.92, 1.08)
    tx = rng.uniform(-3, 3) * (w / GLYPH_W) / 4
    ty = rng.uniform(-3, 3) * (h / GLYPH_H) / 4

    center = (w / 2.0, h / 2.0)
    rot = cv2.getRotationMatrix2D(center, angle, scale)
    rot[0, 2] += tx
    rot[1, 2] += ty
    transformed = cv2.warpAffine(base, rot, (w, h),
                                 flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_CONSTANT,
                                 borderValue=0)

    if rng.random() < 0.6:
        bloom = cv2.GaussianBlur(transformed, (0, 0), sigmaX=rng.uniform(1.5, 4.0))
        bloom_weight = rng.uniform(0.3, 0.8)
        transformed = np.clip(transformed.astype(np.float32)
                              + bloom.astype(np.float32) * bloom_weight, 0, 255).astype(np.uint8)

    if rng.random() < 0.4:
        sigma = rng.uniform(0.4, 1.2)
        transformed = cv2.GaussianBlur(transformed, (0, 0), sigmaX=sigma)

    brightness = rng.uniform(0.7, 1.3)
    transformed = np.clip(transformed.astype(np.float32) * brightness, 0, 255).astype(np.uint8)

    if rng.random() < 0.5:
        noise = rng.gauss(0, 1) * 8 + np.random.randn(h, w) * rng.uniform(2, 12)
        transformed = np.clip(transformed.astype(np.float32) + noise, 0, 255).astype(np.uint8)

    resized = cv2.resize(transformed, (GLYPH_W, GLYPH_H), interpolation=cv2.INTER_AREA)

    thresh = rng.randint(64, 128)
    _, binary = cv2.threshold(resized, thresh, 255, cv2.THRESH_BINARY)
    return binary.astype(np.uint8)


def generate_for_char(
    char: str,
    font: ImageFont.FreeTypeFont,
    n_samples: int,
    out_root: Path,
    rng: random.Random,
) -> int:
    """Generate n_samples variations of a character. Returns count written."""
    base = render_base(font, char)
    if base.max() < 32:
        logger.warning("Character '%s' rendered empty — incompatible font? Skip.", char)
        return 0

    target_dir = out_root / safe_dir_name(char)
    target_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for i in range(n_samples):
        sample = augment_and_binarize(base, rng)
        if sample.max() == 0:
            continue
        cv2.imwrite(str(target_dir / f"{i:04d}.png"), sample)
        written += 1
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--font", required=True, type=Path)
    parser.add_argument("--point-size", type=int, default=28)
    parser.add_argument("--samples-per-char", type=int, default=200)
    parser.add_argument("--out", type=Path, default=Path("dataset/synthetic"))
    parser.add_argument(
        "--chars",
        default="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.-:/+km",
        help="Characters to generate (plain string)",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    if not args.font.is_file():
        logger.error("Font not found: %s", args.font)
        return 1

    try:
        font = ImageFont.truetype(str(args.font), args.point_size)
    except OSError as exc:
        logger.error("Cannot load font: %s", exc)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    np.random.seed(args.seed)
    rng = random.Random(args.seed)

    total = 0
    for char in args.chars:
        n = generate_for_char(char, font, args.samples_per_char, args.out, rng)
        total += n
        logger.info("'%s' : %d samples → %s/", char, n, safe_dir_name(char))

    logger.info("=== DONE === %d samples generated in %s", total, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
