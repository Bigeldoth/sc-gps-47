"""Glyph segmentation: horizontal projection + connected components.

Pipeline:
  1. Horizontal projection → identifies text bands
  2. For each band: connected components + proximity merge within 2 px
  3. Returns bounding box regions of glyphs (x, y, w, h)
  4. Optional: saves segmented glyphs for template collection
"""
import numpy as np
import cv2
import logging
from datetime import datetime
import os

logger = logging.getLogger(__name__)


def find_text_rows(binary_image, min_row_height=3):
    """Horizontal projection: identifies text bands.

    Args:
        binary_image : binary image 0/255
        min_row_height : minimum band height in pixels

    Returns:
        list of tuples (row_start, row_end) for each detected band
    """
    # Horizontal projection: sum of white pixels per row
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
    """Connected components within a text band, with proximity merging.

    Args:
        row_image : binary image of a band (H, W)
        min_glyph_width : minimum glyph width
        proximity_threshold : merge glyphs closer than N pixels apart

    Returns:
        list of tuples (x, y, w, h) bounding boxes of glyphs
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

    # Proximity merge: if two glyphs are < threshold px apart,
    # merge them into a single bounding box.
    components.sort(key=lambda c: c[0])
    merged = [components[0]]

    for comp in components[1:]:
        last = merged[-1]
        x_gap = comp[0] - (last[0] + last[2])

        if x_gap < proximity_threshold:
            # Merge: expand the bounding box
            x_min = min(last[0], comp[0])
            y_min = min(last[1], comp[1])
            x_max = max(last[0] + last[2], comp[0] + comp[2])
            y_max = max(last[1] + last[3], comp[1] + comp[3])
            merged[-1] = (x_min, y_min, x_max - x_min, y_max - y_min)
        else:
            merged.append(comp)

    return merged


def find_glyph_regions(binary_image):
    """Full segmentation: horizontal projection → glyphs.

    Args:
        binary_image : binary image 0/255 (H, W)

    Returns:
        dict:
            - 'rows' : list of tuples (row_start, row_end)
            - 'glyphs' : list of dicts {
                'x': int,
                'y': int,
                'w': int,
                'h': int,
                'row_idx': int (index of parent band)
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
                'y': y + row_start,  # absolute y in the image
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
    """Saves segmented glyphs for template collection.

    Output structure:
        data/glyphs/{timestamp}/{glyph_id}_...png

    Args:
        binary_image : source binary image
        glyphs : list of glyph dicts (from find_glyph_regions)
        output_dir : output directory
        prefix : prefix for the timestamp
    """
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
    glyph_dir = os.path.join(output_dir, f"{prefix}{timestamp}")
    os.makedirs(glyph_dir, exist_ok=True)

    for glyph in glyphs:
        x, y, w, h = glyph['x'], glyph['y'], glyph['w'], glyph['h']
        # Light padding for context
        pad = 2
        y1 = max(0, y - pad)
        y2 = min(binary_image.shape[0], y + h + pad)
        x1 = max(0, x - pad)
        x2 = min(binary_image.shape[1], x + w + pad)

        crop = binary_image[y1:y2, x1:x2]
        filename = os.path.join(glyph_dir, f"{glyph['id']:04d}_r{glyph['row_idx']}.png")

        cv2.imwrite(filename, crop)

    logger.info(f"Glyphs saved: {len(glyphs)} in {glyph_dir}")
