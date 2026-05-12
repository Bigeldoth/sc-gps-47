"""Identification of Star Citizen HUD font via NCC scoring.

Compares candidate fonts (Electrolize, Aldrich, Eurostile-like, Good Times,
Orbitron) against existing NCC templates from data/templates/{0..9}/ to propose
the best substitute font for use in synthetic generation (tools/dataset_synthetic.py).

Usage:
    python tools/find_sc_font.py [--fonts-dir tools/fonts] [--templates data/templates]

TTF files must be manually placed in tools/fonts/. Script prints public URLs
for download (Google Fonts / GitHub mirrors).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Add project root to PYTHONPATH to import src/
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sc_ocr.templates import TemplateLibrary  # noqa: E402

# Public candidate fonts visually close to SC HUD.
# Manual download suggested: place .ttf files in tools/fonts/.
CANDIDATE_FONTS: dict[str, str] = {
    "Electrolize-Regular.ttf": "https://fonts.google.com/specimen/Electrolize",
    "Aldrich-Regular.ttf": "https://fonts.google.com/specimen/Aldrich",
    "Orbitron-Regular.ttf": "https://fonts.google.com/specimen/Orbitron",
    "GoodTimes-Regular.ttf": "https://www.dafont.com/good-times.font",
    "Eurostile-Regular.ttf": "https://www.cufonfonts.com/font/eurostile",
}

DIGITS: tuple[str, ...] = tuple("0123456789")

GLYPH_W = 16
GLYPH_H = 24


def render_char(font: ImageFont.FreeTypeFont, char: str, size: tuple[int, int]) -> np.ndarray:
    """Render white character on black background, cropped to bounding box."""
    w, h = size
    canvas = Image.new("L", (w * 4, h * 4), color=0)
    draw = ImageDraw.Draw(canvas)
    # Approximate centered position
    bbox = draw.textbbox((0, 0), char, font=font)
    cx = (w * 4 - (bbox[2] - bbox[0])) // 2 - bbox[0]
    cy = (h * 4 - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((cx, cy), char, fill=255, font=font)
    arr = np.asarray(canvas, dtype=np.uint8)

    # Crop to non-zero pixels
    ys, xs = np.where(arr > 32)
    if ys.size == 0:
        return np.zeros((h, w), dtype=np.uint8)
    y1, y2 = ys.min(), ys.max() + 1
    x1, x2 = xs.min(), xs.max() + 1
    crop = arr[y1:y2, x1:x2]
    resized = cv2.resize(crop, (w, h), interpolation=cv2.INTER_AREA)
    # Simple binarization (rendered font = anti-aliased gray levels)
    _, binary = cv2.threshold(resized, 96, 255, cv2.THRESH_BINARY)
    return binary.astype(np.uint8)


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    """Scalar NCC between two same-sized images."""
    a_f = a.astype(np.float32) / 255.0
    b_f = b.astype(np.float32) / 255.0
    a_c = a_f - a_f.mean()
    b_c = b_f - b_f.mean()
    denom = np.sqrt(np.sum(a_c * a_c) * np.sum(b_c * b_c))
    if denom == 0:
        return 0.0
    return float(np.sum(a_c * b_c) / denom)


def score_font(font_path: Path, library: TemplateLibrary, point_size: int = 28) -> dict:
    """Compute average NCC score of a font on digits 0-9."""
    try:
        font = ImageFont.truetype(str(font_path), point_size)
    except OSError as exc:
        return {"font": font_path.name, "error": str(exc)}

    scores: dict[str, float] = {}
    for digit in DIGITS:
        template = library.get_template(digit)
        if template is None:
            continue
        rendered = render_char(font, digit, (GLYPH_W, GLYPH_H))
        scores[digit] = _ncc(rendered, template)

    if not scores:
        return {"font": font_path.name, "error": "No digit templates available"}

    mean = float(np.mean(list(scores.values())))
    return {"font": font_path.name, "scores": scores, "mean": mean}


def discover_fonts(fonts_dir: Path) -> Iterable[Path]:
    if not fonts_dir.is_dir():
        return []
    return sorted(p for p in fonts_dir.iterdir() if p.suffix.lower() in {".ttf", ".otf"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fonts-dir", default="tools/fonts", type=Path)
    parser.add_argument("--templates", default="data/templates", type=str)
    parser.add_argument("--point-size", default=28, type=int)
    args = parser.parse_args()

    library = TemplateLibrary(template_dir=args.templates)
    if not library.has_templates():
        print(f"[ERROR] No templates loaded from {args.templates}")
        return 1

    args.fonts_dir.mkdir(parents=True, exist_ok=True)
    fonts = list(discover_fonts(args.fonts_dir))

    if not fonts:
        print(f"[INFO] No fonts in {args.fonts_dir}. Candidate URLs:")
        for name, url in CANDIDATE_FONTS.items():
            print(f"  - {name:30s} -> {url}")
        print(f"\nPlace .ttf files in {args.fonts_dir} and rerun.")
        return 0

    results = [score_font(p, library, args.point_size) for p in fonts]
    results.sort(key=lambda r: r.get("mean", -1.0), reverse=True)

    print(f"\n=== Font ranking (size {args.point_size} pt, {GLYPH_W}x{GLYPH_H}) ===\n")
    for r in results:
        if "error" in r:
            print(f"  {r['font']:30s}  ERROR: {r['error']}")
            continue
        per_digit = "  ".join(f"{d}:{r['scores'][d]:.2f}" for d in DIGITS if d in r["scores"])
        print(f"  {r['font']:30s}  mean={r['mean']:.3f}   ({per_digit})")
    print()

    if results and "mean" in results[0]:
        print(f"=> Recommended font: {results[0]['font']}  (mean NCC {results[0]['mean']:.3f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
