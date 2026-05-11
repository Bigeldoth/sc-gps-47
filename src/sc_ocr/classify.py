"""Classification NCC shift-invariant : reconnaissance des glyphes.

NCC (Normalized Cross-Correlation) matching sur templates 16×24 pixels.
Shift-invariant ±2 px horizontal, ±1 px vertical pour absorber le wiggle HUD.

Latence cible : ~1 ms pour 12 glyphes (coords X.XXXX km Y.YYYY km Z.ZZZZ km).

Heuristique `.` : si aucun template `.` n'est fourni mais qu'on trouve un
glyph très petit (h < 8 px, w ≤ 6 px) entre deux chiffres, on l'étiquette `.`.
"""
import numpy as np
import cv2
import logging

logger = logging.getLogger(__name__)

# Largeur/hauteur cible pour normaliser les glyphes
GLYPH_TARGET_WIDTH = 16
GLYPH_TARGET_HEIGHT = 24

# Seuil de confiance NCC
NCC_THRESHOLD = 0.78

# Heuristique point décimal
DOT_MAX_HEIGHT = 8
DOT_MAX_WIDTH = 6


def normalize_glyph(crop, target_w=GLYPH_TARGET_WIDTH, target_h=GLYPH_TARGET_HEIGHT):
    """Redimensionne un glyphe à la taille standard."""
    if crop.size == 0:
        return np.zeros((target_h, target_w), dtype=np.uint8)
    return cv2.resize(crop, (target_w, target_h), interpolation=cv2.INTER_LINEAR).astype(np.uint8)


def _is_likely_dot(glyph_meta):
    """Heuristique : ce glyph ressemble-t-il à un point décimal ?

    Un `.` du HUD SC est :
      - Petit (h < 8 px sur l'image binaire d'origine)
      - Étroit (w ≤ 6 px)

    Args:
        glyph_meta : dict avec clés 'w', 'h' (dimensions originales)

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
    """NCC batch contre tous les templates en une opération vectorisée.

    Args:
        patch_f32 : image patch (H, W) float32, déjà normalisé
        templates_stack : tenseur (N, H, W) float32 des templates centrés

    Returns:
        scores : array (N,) avec le NCC pour chaque template
    """
    p_mean = patch_f32.mean()
    p_centered = patch_f32 - p_mean
    p_std = np.sqrt(np.sum(p_centered * p_centered))

    if p_std == 0:
        return np.zeros(len(templates_stack), dtype=np.float32)

    # Pour chaque template (déjà centré + std=1), score = sum(t * p_centered) / p_std
    dots = np.einsum('nhw,hw->n', templates_stack, p_centered)
    return dots / p_std


def classify_single_glyph(glyph_image, template_library, shift_range_x=2, shift_range_y=1):
    """Classifie un seul glyphe par matching NCC shift-invariant.

    Test le glyphe aux positions ±shift_range_x, ±shift_range_y pour absorber
    le wiggle subpixel du HUD SC.

    Returns:
        dict avec 'char', 'score', 'scores'
    """
    normalized = normalize_glyph(glyph_image)
    base_f32 = normalized.astype(np.float32) / 255.0

    chars = template_library.char_list
    stack = template_library.centered_stack  # (N, H, W) float32, centré + L2 normalisé

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
    """Classifie un lot de glyphes.

    Args:
        glyphs_images : list de (glyph_id, image uint8)
        template_library : TemplateLibrary instance
        glyphs_meta : optionnel — list de dicts {'id', 'w', 'h', ...} pour
                      activer l'heuristique `.` sur les glyphs non-classifiés

    Returns:
        list de dicts {'glyph_id', 'char', 'score', 'raw_image'}
    """
    meta_by_id = {}
    if glyphs_meta is not None:
        meta_by_id = {g['id']: g for g in glyphs_meta}

    has_dot_template = '.' in template_library.templates
    results = []

    for glyph_id, glyph_image in glyphs_images:
        result = classify_single_glyph(glyph_image, template_library)

        # Heuristique fallback `.` si pas de template fourni
        if result['char'] is None and not has_dot_template:
            meta = meta_by_id.get(glyph_id)
            if _is_likely_dot(meta):
                result['char'] = '.'
                result['score'] = 0.99  # confiance heuristique
                logger.debug(f"Glyph {glyph_id} étiqueté '.' par heuristique (w={meta.get('w')}, h={meta.get('h')})")

        results.append({
            'glyph_id': glyph_id,
            'char': result['char'],
            'score': result['score'],
            'raw_image': glyph_image,
        })

    return results
