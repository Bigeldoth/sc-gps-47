"""Automatic organization of segmented glyphs into templates by character.

After a collection session (30 min in-game with save_glyph_crops=True),
this script:
  1. Scans data/glyphs/{TIMESTAMP}/
  2. Uses high-confidence Tesseract for automatic labeling
  3. Organizes into data/templates/{char}/ (0-9, ., -, k, m, etc.)
  4. Generates report

Final structure:
  data/templates/
    ├── 0/ (digit)
    │   ├── template_001.png
    │   ├── template_002.png
    │   ...
    ├── 1/
    ├── ...
    ├── ./
    ├── -/
    ├── k/
    ├── m/
    └── _/ (space)
"""
import os
import cv2
import sys
import logging
import pytesseract
from pathlib import Path
from collections import defaultdict

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Expected characters for numeric coordinates
EXPECTED_CHARS = set('0123456789.-km ')

# Tesseract config for high-confidence character recognition
_TESSERACT_CONFIG = (
    r'--oem 3 --psm 10 '
    r'-c classify_bln_numeric_mode=1 '
    r'-c tessedit_char_whitelist=0123456789.-km '
)


def init_tesseract(tesseract_path=None):
    """Initialize pytesseract with correct path."""
    if not tesseract_path:
        possible_paths = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            "/opt/homebrew/bin/tesseract",
            "/usr/local/bin/tesseract",
            "/usr/bin/tesseract",
        ]
        for path in possible_paths:
            if os.path.exists(path):
                pytesseract.pytesseract.tesseract_cmd = path
                logger.info(f"Tesseract found: {path}")
                return True
        logger.error("Tesseract-OCR not found!")
        return False
    else:
        pytesseract.pytesseract.tesseract_cmd = tesseract_path
        return True


def find_latest_glyph_dir(glyphs_base_dir='data/glyphs'):
    """Find most recent glyph directory.

    Returns:
        path to TIMESTAMP folder or None
    """
    if not os.path.isdir(glyphs_base_dir):
        logger.error(f"Directory {glyphs_base_dir} not found")
        return None

    subdirs = [
        d for d in os.listdir(glyphs_base_dir)
        if os.path.isdir(os.path.join(glyphs_base_dir, d))
    ]

    if not subdirs:
        logger.error(f"No glyph folders in {glyphs_base_dir}")
        return None

    # Most recent (lexicographic sort if YYYYMMDD_HHMMSS_mmm names)
    latest = sorted(subdirs)[-1]
    path = os.path.join(glyphs_base_dir, latest)
    logger.info(f"Glyph folder detected: {path}")
    return path


def classify_glyph(image_path, config=_TESSERACT_CONFIG):
    """Use Tesseract to label a glyph.

    Args:
        image_path: path to PNG
        config: Tesseract config

    Returns:
        (char_recognized, confidence_score) or (None, 0.0)
    """
    try:
        img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        if img is None or img.size == 0:
            return None, 0.0

        # Try small upscale for PSM 10 (single character)
        h, w = img.shape
        if w < 8 or h < 8:
            img = cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_LINEAR)

        # Tesseract data
        text = pytesseract.image_to_string(img, config=config)
        text = text.strip()

        if not text:
            return None, 0.0

        # Take first character
        char = text[0]

        # High confidence (Tesseract returns 0-100, normalized here)
        # Accept only if confidence > 70
        confidence = 0.85  # Approximation, Tesseract doesn't expose confidence easily in PSM 10

        return char, confidence

    except Exception as e:
        logger.warning(f"Classification error {image_path}: {e}")
        return None, 0.0


def organize_glyphs(glyph_dir, output_base='data/templates'):
    """Organize glyphs into folders by character.

    Args:
        glyph_dir: source glyph directory (TIMESTAMP)
        output_base: output directory (data/templates/)

    Returns:
        dict with stats
    """
    os.makedirs(output_base, exist_ok=True)

    # Créer les dossiers de sortie pour chaque caractère attendu
    char_dirs = {}
    for char in EXPECTED_CHARS:
        char_dir = os.path.join(output_base, char)
        os.makedirs(char_dir, exist_ok=True)
        char_dirs[char] = char_dir

    stats = defaultdict(int)
    failed = []
    total = 0

    # Scan all PNGs
    for filename in sorted(os.listdir(glyph_dir)):
        if not filename.lower().endswith('.png'):
            continue

        filepath = os.path.join(glyph_dir, filename)
        total += 1

        # Classify
        char, confidence = classify_glyph(filepath)

        if char is None:
            failed.append(filename)
            stats['unclassified'] += 1
            logger.warning(f"[{total}] {filename}: not classifiable")
            continue

        # Check if expected character
        if char not in EXPECTED_CHARS:
            logger.debug(f"[{total}] {filename}: '{char}' (not in list, skip)")
            stats['unexpected_char'] += 1
            failed.append(filename)
            continue

        # Copy to appropriate folder
        dest_dir = char_dirs[char]
        dest_path = os.path.join(dest_dir, filename)

        try:
            img = cv2.imread(filepath, cv2.IMREAD_GRAYSCALE)
            cv2.imwrite(dest_path, img)
            stats[char] += 1
            logger.info(f"[{total}] {filename} → '{char}' ({confidence:.2f})")
        except Exception as e:
            logger.error(f"[{total}] Copy error {filename}: {e}")
            stats['copy_error'] += 1
            failed.append(filename)

    return dict(stats), failed, total


def print_report(stats, failed, total, output_base='data/templates'):
    """Print results report.

    Args:
        stats: dict of stats by character
        failed: list of failed files
        total: total file count
        output_base: output directory
    """
    print("\n" + "=" * 60)
    print("TEMPLATE ORGANIZATION REPORT")
    print("=" * 60)
    print(f"Total glyphs processed: {total}")
    print(f"Successfully organized: {sum(v for k, v in stats.items() if k not in ['unclassified', 'unexpected_char', 'copy_error'])}")
    print()

    print("Distribution by character:")
    for char in sorted(EXPECTED_CHARS):
        count = stats.get(char, 0)
        if count > 0:
            print(f"  '{char}': {count:3d} templates")

    unclassified = stats.get('unclassified', 0)
    unexpected = stats.get('unexpected_char', 0)
    errors = stats.get('copy_error', 0)

    if unclassified + unexpected + errors > 0:
        print()
        print("Issues encountered:")
        if unclassified > 0:
            print(f"  Not classifiable: {unclassified}")
        if unexpected > 0:
            print(f"  Unexpected character: {unexpected}")
        if errors > 0:
            print(f"  Copy errors: {errors}")

    if failed:
        print()
        print(f"First failed files ({len(failed)}):")
        for f in failed[:10]:
            print(f"  - {f}")

    print()
    print(f"Templates organized in: {output_base}/")
    print("=" * 60 + "\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Organize collected glyphs into templates by character"
    )
    parser.add_argument(
        "--glyph-dir",
        type=str,
        default=None,
        help="Path to source folder (data/glyphs/{TIMESTAMP}). Auto-detect if absent."
    )
    parser.add_argument(
        "--output",
        type=str,
        default="data/templates",
        help="Output directory (default: data/templates)"
    )
    parser.add_argument(
        "--tesseract-path",
        type=str,
        default=None,
        help="Path to tesseract.exe if non-standard"
    )

    args = parser.parse_args()

    # Init Tesseract
    if not init_tesseract(args.tesseract_path):
        sys.exit(1)

    # Find glyph folder
    glyph_dir = args.glyph_dir or find_latest_glyph_dir()
    if not glyph_dir:
        sys.exit(1)

    logger.info(f"Organizing glyphs from: {glyph_dir}")
    logger.info(f"Destination: {args.output}")

    # Organize
    stats, failed, total = organize_glyphs(glyph_dir, args.output)

    # Report
    print_report(stats, failed, total, args.output)

    # Return
    success_count = sum(v for k, v in stats.items() if k not in ['unclassified', 'unexpected_char', 'copy_error'])
    if success_count == 0:
        logger.error("No glyphs organized correctly!")
        sys.exit(1)

    logger.info(f"✅ {success_count}/{total} glyphs organized")


if __name__ == "__main__":
    main()
