"""Segmentation des glyphes : projection horizontale + composantes connexes.

Pipeline :
  1. Projection horizontale → identifie les bandes de texte
  2. Pour chaque bande : composantes connexes + fusion proximité 2 px
  3. Retourne les régions de bounding box des glyphes (x, y, w, h)
  4. Optionnel : sauvegarde les glyphes segmentés pour collecte de templates
"""
import numpy as np
import cv2
import logging
from datetime import datetime
import os

logger = logging.getLogger(__name__)


def find_text_rows(binary_image, min_row_height=3):
    """Projection horizontale : identifie les bandes de texte.

    Args:
        binary_image : image binaire 0/255
        min_row_height : hauteur minimale d'une bande en pixels

    Returns:
        list de tuples (row_start, row_end) pour chaque bande détectée
    """
    # Projection horizontale : somme pixels blancs par ligne
    proj = np.sum(binary_image > 128, axis=1)
    rows = []
    in_text = False
    start = 0

    for i, count in enumerate(proj):
        if count > 0 and not in_text:
            start = i
            in_text = True
        elif count == 0 and in_text:
            if i - start >= min_row_height:
                rows.append((start, i))
            in_text = False

    if in_text and len(proj) - start >= min_row_height:
        rows.append((start, len(proj)))

    return rows


def find_glyphs_in_row(row_image, min_glyph_width=4, proximity_threshold=2):
    """Composantes connexes dans une bande de texte, avec fusion proximité.

    Args:
        row_image : image binaire d'une bande (H, W)
        min_glyph_width : largeur minimale d'un glyphe
        proximity_threshold : fusionner les glyphes distants de moins de N pixels

    Returns:
        list de tuples (x, y, w, h) bounding boxes des glyphes
    """
    _, labels = cv2.connectedComponents(row_image, connectivity=8)
    components = []

    for label in range(1, labels.max() + 1):
        mask = (labels == label).astype(np.uint8)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        if not contours:
            continue

        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w >= min_glyph_width:
                components.append((x, y, w, h))

    if not components:
        return []

    # Fusion proximité : si deux glyphes sont distants de < threshold px,
    # les fusionner en un seul bounding box.
    components.sort(key=lambda c: c[0])
    merged = [components[0]]

    for comp in components[1:]:
        last = merged[-1]
        x_gap = comp[0] - (last[0] + last[2])

        if x_gap < proximity_threshold:
            # Fusionner : élargir le bounding box
            x_min = min(last[0], comp[0])
            y_min = min(last[1], comp[1])
            x_max = max(last[0] + last[2], comp[0] + comp[2])
            y_max = max(last[1] + last[3], comp[1] + comp[3])
            merged[-1] = (x_min, y_min, x_max - x_min, y_max - y_min)
        else:
            merged.append(comp)

    return merged


def find_glyph_regions(binary_image):
    """Segmentation complète : projection horizontale → glyphes.

    Args:
        binary_image : image binaire 0/255 (H, W)

    Returns:
        dict :
            - 'rows' : list de tuples (row_start, row_end)
            - 'glyphs' : list de dicts {
                'x': int,
                'y': int,
                'w': int,
                'h': int,
                'row_idx': int (index de la bande parent)
              }
    """
    rows = find_text_rows(binary_image)
    glyphs = []
    glyph_id = 0

    for row_idx, (row_start, row_end) in enumerate(rows):
        row_image = binary_image[row_start:row_end, :]
        row_glyphs = find_glyphs_in_row(row_image)

        for x, y, w, h in row_glyphs:
            glyphs.append({
                'id': glyph_id,
                'x': x,
                'y': y + row_start,  # y absolu dans l'image
                'w': w,
                'h': h,
                'row_idx': row_idx,
            })
            glyph_id += 1

    return {
        'rows': rows,
        'glyphs': glyphs,
    }


def save_glyph_crops(binary_image, glyphs, output_dir='data/glyphs', prefix=''):
    """Sauvegarde les glyphes segmentés pour collecte de templates.

    Structure de sortie :
        data/glyphs/{timestamp}/{glyph_id}_...png

    Args:
        binary_image : image binaire source
        glyphs : list de dicts glyph (de find_glyph_regions)
        output_dir : répertoire de sortie
        prefix : préfixe pour le timestamp
    """
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
    glyph_dir = os.path.join(output_dir, f"{prefix}{timestamp}")
    os.makedirs(glyph_dir, exist_ok=True)

    for glyph in glyphs:
        x, y, w, h = glyph['x'], glyph['y'], glyph['w'], glyph['h']
        # Padding léger pour contexte
        pad = 2
        y1 = max(0, y - pad)
        y2 = min(binary_image.shape[0], y + h + pad)
        x1 = max(0, x - pad)
        x2 = min(binary_image.shape[1], x + w + pad)

        crop = binary_image[y1:y2, x1:x2]
        filename = os.path.join(glyph_dir, f"{glyph['id']:04d}_r{glyph['row_idx']}.png")

        cv2.imwrite(filename, crop)

    logger.info(f"Glyphes sauvegardés : {len(glyphs)} dans {glyph_dir}")
