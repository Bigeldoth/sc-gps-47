"""Loading and management of template library for NCC.

Templates stored in data/templates/ organized by character:
  data/templates/0/, data/templates/1/, ... data/templates/9/
  data/templates/-/, data/templates/k/, data/templates/m/

Each template is a grayscale uint8 PNG image.

On loading:
  1. All PNGs in a folder are resized to 16×24
  2. Averaged to produce a stable synthetic template
  3. Centered (mean=0) and L2-normalized to accelerate vectorized NCC
"""
import os
import cv2
import numpy as np
import logging

logger = logging.getLogger(__name__)

# Expected characters for numeric coordinates
# (the '.' is optional: if absent, the heuristic in classify.py detects it)
EXPECTED_CHARS = set('0123456789.-km')

GLYPH_TARGET_WIDTH = 16
GLYPH_TARGET_HEIGHT = 24


def _is_safe_char_dir(name):
    """Checks that a folder name is a valid character (handles Windows)."""
    return len(name) == 1 and name in EXPECTED_CHARS


class TemplateLibrary:
    def __init__(self, template_dir='data/templates'):
        self.template_dir = template_dir
        self.templates = {}       # {char: image (H, W) uint8}
        self.char_list = []       # liste ordonnée des chars
        self.centered_stack = None  # tenseur (N, H, W) float32 centré + L2-normalisé
        self._load_templates()
        self._build_stack()

    def _load_templates(self):
        if not os.path.exists(self.template_dir):
            logger.warning(f"Template directory not found: {self.template_dir}")
            return

        for char in sorted(EXPECTED_CHARS):
            char_dir = os.path.join(self.template_dir, char)
            if not os.path.isdir(char_dir):
                continue

            images = []
            for filename in os.listdir(char_dir):
                if not filename.lower().endswith(('.png', '.jpg', '.bmp')):
                    continue
                filepath = os.path.join(char_dir, filename)
                try:
                    img = cv2.imread(filepath, cv2.IMREAD_GRAYSCALE)
                    if img is None or img.size == 0:
                        continue
                    # Resize to standard size before averaging
                    resized = cv2.resize(
                        img, (GLYPH_TARGET_WIDTH, GLYPH_TARGET_HEIGHT),
                        interpolation=cv2.INTER_LINEAR,
                    )
                    images.append(resized)
                except Exception as e:
                    logger.warning(f"Error loading template {filepath}: {e}")

            if images:
                stacked = np.stack(images, axis=0).astype(np.float32)
                avg = np.mean(stacked, axis=0).astype(np.uint8)
                self.templates[char] = avg
                logger.debug(f"Char '{char}': {len(images)} templates averaged")
            else:
                # '.' is handled by heuristic in classify.py, not by template → debug only.
                logger.debug(f"No valid templates for '{char}'")

        if not self.templates:
            logger.warning(
                "No templates loaded. Verify that data/templates/{0-9,-,k,m}/ "
                "contain PNG files."
            )

    def _build_stack(self):
        """Pre-computes the centered+normalized tensor for batch NCC."""
        if not self.templates:
            self.char_list = []
            self.centered_stack = None
            return

        self.char_list = sorted(self.templates.keys())
        n = len(self.char_list)
        stack = np.zeros((n, GLYPH_TARGET_HEIGHT, GLYPH_TARGET_WIDTH), dtype=np.float32)

        for i, char in enumerate(self.char_list):
            t = self.templates[char].astype(np.float32) / 255.0
            t_centered = t - t.mean()
            norm = np.sqrt(np.sum(t_centered * t_centered))
            if norm > 0:
                stack[i] = t_centered / norm
            else:
                stack[i] = t_centered

        self.centered_stack = stack
        logger.info(f"NCC stack pre-computed: {n} templates {GLYPH_TARGET_HEIGHT}x{GLYPH_TARGET_WIDTH}")

    def get_template(self, char):
        return self.templates.get(char)

    def has_templates(self):
        return len(self.templates) > 0

    def list_chars(self):
        return set(self.templates.keys())

    def stats(self):
        return {
            'total_chars': len(self.templates),
            'chars': sorted(self.templates.keys()),
        }
