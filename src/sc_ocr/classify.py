"""Shift-invariant NCC classification: glyph recognition.

NCC (Normalized Cross-Correlation) matching on 16×24 pixel templates.
Shift-invariant ±2 px horizontal, ±1 px vertical to absorb HUD wiggle.

Target latency: ~1 ms for 12 glyphs (coords X.XXXX km Y.YYYY km Z.ZZZZ km).

Heuristic `.`: if no `.` template is provided but a very small glyph
(h < 8 px, w ≤ 6 px) is found between two digits, it is labelled `.`.
"""
import numpy as np
import cv2
import logging

logger = logging.getLogger(__name__)

# Target width/height for glyph normalisation
GLYPH_TARGET_WIDTH = 16
GLYPH_TARGET_HEIGHT = 24

# NCC confidence threshold
NCC_THRESHOLD = 0.78

# Decimal point heuristic
DOT_MAX_HEIGHT = 8
DOT_MAX_WIDTH = 6


def normalize_glyph(crop, target_w=GLYPH_TARGET_WIDTH, target_h=GLYPH_TARGET_HEIGHT):
    """Resizes a glyph to the standard size."""
    if crop.size == 0:
        return np.zeros((target_h, target_w), dtype=np.uint8)
    return cv2.resize(crop, (target_w, target_h), interpolation=cv2.INTER_LINEAR).astype(np.uint8)


def _is_likely_dot(glyph_meta):
    """Heuristic: does this glyph look like a decimal point?

    A `.` in the SC HUD is:
      - Small (h < 8 px on the original binary image)
      - Narrow (w ≤ 6 px)

    Args:
        glyph_meta : dict with keys 'w', 'h' (original dimensions)

    Returns:
        bool
    """
    if glyph_meta is None:
        return False
    return (
        glyph_meta.get('h', 999) <= DOT_MAX_HEIGHT
        and glyph_meta.get('w', 999) <= DOT_MAX_WIDTH
    )


def _ncc_vectorized(patch_f32, templates_stack):
    """Batch NCC against all templates in a single vectorised operation.

    Args:
        patch_f32 : image patch (H, W) float32, already normalised
        templates_stack : tensor (N, H, W) float32 of centred templates

    Returns:
        scores : array (N,) with NCC score for each template
    """
    p_mean = patch_f32.mean()
    p_centered = patch_f32 - p_mean
    p_std = np.sqrt(np.sum(p_centered * p_centered))

    if p_std == 0:
        return np.zeros(len(templates_stack), dtype=np.float32)

    # For each template (already centred + std=1), score = sum(t * p_centered) / p_std
    dots = np.einsum('nhw,hw->n', templates_stack, p_centered)
    return dots / p_std


def classify_single_glyph(glyph_image, template_library, shift_range_x=2, shift_range_y=1):
    """Classifies a single glyph by shift-invariant NCC matching.

    Tests the glyph at positions ±shift_range_x, ±shift_range_y to absorb
    sub-pixel wiggle from the SC HUD.

    Returns:
        dict with 'char', 'score', 'scores'
    """
    normalized = normalize_glyph(glyph_image)

    # Stretch contrast to the full [0, 255] range. This is a no-op on pure
    # binary input (templates were collected from binary crops) but maps a
    # grayscale crop (e.g. CLAHE-enhanced) to the same dynamic range so the
    # NCC correlation with binary templates stays high.
    if normalized.size > 0:
        n_min, n_max = int(normalized.min()), int(normalized.max())
        if n_max > n_min:
            normalized = cv2.normalize(normalized, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

    base_f32 = normalized.astype(np.float32) / 255.0

    chars = template_library.char_list
    stack = template_library.centered_stack  # (N, H, W) float32, centred + L2-normalised

    if stack is None or len(chars) == 0:
        return {'char': None, 'score': 0.0, 'scores': {}}

    best_scores = np.full(len(chars), -1.0, dtype=np.float32)

    for dy in range(-shift_range_y, shift_range_y + 1):
        for dx in range(-shift_range_x, shift_range_x + 1):
            if dx == 0 and dy == 0:
                shifted = base_f32
            else:
                shifted = np.roll(base_f32, (dy, dx), axis=(0, 1))
            scores = _ncc_vectorized(shifted, stack)
            np.maximum(best_scores, scores, out=best_scores)

    best_idx = int(np.argmax(best_scores))
    best_score = float(best_scores[best_idx])
    best_char = chars[best_idx]
    all_scores = {c: float(s) for c, s in zip(chars, best_scores)}

    if best_score < NCC_THRESHOLD:
        return {'char': None, 'score': best_score, 'scores': all_scores}

    return {'char': best_char, 'score': best_score, 'scores': all_scores}


def classify_batch(glyphs_images, template_library, glyphs_meta=None):
    """Classifies a batch of glyphs.

    Args:
        glyphs_images : list of (glyph_id, image uint8)
        template_library : TemplateLibrary instance
        glyphs_meta : optional — list of dicts {'id', 'w', 'h', ...} to
                      enable the `.` heuristic on unclassified glyphs

    Returns:
        list of dicts {'glyph_id', 'char', 'score', 'raw_image'}
    """
    meta_by_id = {}
    if glyphs_meta is not None:
        meta_by_id = {g['id']: g for g in glyphs_meta}

    has_dot_template = '.' in template_library.templates
    results = []

    for glyph_id, glyph_image in glyphs_images:
        result = classify_single_glyph(glyph_image, template_library)

        # Fallback heuristic `.` if no template provided
        if result['char'] is None and not has_dot_template:
            meta = meta_by_id.get(glyph_id)
            if _is_likely_dot(meta):
                result['char'] = '.'
                result['score'] = 0.99  # heuristic confidence
                logger.debug(f"Glyph {glyph_id} labelled '.' by heuristic (w={meta.get('w')}, h={meta.get('h')})")

        results.append({
            'glyph_id': glyph_id,
            'char': result['char'],
            'score': result['score'],
            'raw_image': glyph_image,
        })

    return results
