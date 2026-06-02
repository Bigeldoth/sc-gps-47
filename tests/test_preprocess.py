"""Tests for OCR pre-processing: bright-text channel isolation (no inversion)
and white-tophat background flattening for daylight HUD frames.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sc_ocr.preprocess import isolate_channel, flatten_background, otsu_threshold


_W = 300
_STROKE_W = 2


def _stroke_cols():
    # Kept off the extreme right edge to avoid morphology border effects.
    return list(range(20, _W - 15, 25))


def _thin_text_on_gradient(bg_lo, bg_hi, text_val=255):
    """Synthetic HUD strip: a bright horizontal gradient background with a few
    thin brighter vertical strokes standing in for HUD text. Contrast at the
    bright end is kept realistic (text comfortably above the local background —
    text dimmer than its background is unrecoverable by any method)."""
    h = 60
    bg = np.tile(np.linspace(bg_lo, bg_hi, _W).astype(np.uint8), (h, 1))
    img = bg.copy()
    for x in _stroke_cols():
        img[10:50, x:x + _STROKE_W] = text_val
    return img


# ---- isolate_channel: text stays bright (no inversion) ----

def test_isolate_channel_no_invert_on_bright_frame():
    # A bright BGR frame with a brighter text patch must NOT be inverted:
    # the text patch has to remain the brightest region after isolation.
    bgr = np.full((40, 120, 3), 200, np.uint8)
    bgr[10:30, 50:70] = 255
    ch = isolate_channel(bgr)
    assert ch[20, 60] > ch[2, 2]  # text brighter than background, not flipped


def test_isolate_channel_white_text_uses_max():
    # Neutral (white) text on a neutral background -> max(R,G,B) branch.
    bgr = np.full((40, 120, 3), 80, np.uint8)
    bgr[10:30, 40:80] = 240
    ch = isolate_channel(bgr)
    assert ch.max() == 240


# ---- flatten_background: suppresses bright background, keeps text ----

def test_flatten_background_suppresses_bright_gradient():
    img = _thin_text_on_gradient(120, 190)
    raw_white = float((otsu_threshold(img) > 127).mean())
    flat = flatten_background(img, 9)
    flat_white = float((otsu_threshold(flat) > 127).mean())
    # Flattening must drastically cut the flooded-white background...
    assert flat_white < raw_white
    assert flat_white < 0.20
    # ...while preserving the text strokes.
    binary = otsu_threshold(flat)
    for x in _stroke_cols():
        assert binary[10:50, x:x + _STROKE_W].max() == 255, f"stroke at x={x} erased"


def test_flatten_background_keeps_text_on_dark_frame():
    # Night-equivalent: dark background, bright text. Must not regress.
    img = _thin_text_on_gradient(20, 40, text_val=200)
    binary = otsu_threshold(flatten_background(img, 9))
    for x in _stroke_cols():
        assert binary[10:50, x:x + _STROKE_W].max() == 255


def test_flatten_background_uniform_input_is_safe():
    # No text, perfectly uniform field -> nothing to stretch, returns all-zero.
    flat = flatten_background(np.full((30, 90), 180, np.uint8), 9)
    assert int(flat.max()) == 0


def test_flatten_background_rejects_color_image():
    with pytest.raises(ValueError):
        flatten_background(np.zeros((10, 10, 3), np.uint8), 9)


def test_flatten_background_kernel_forced_odd_and_min():
    # Even / too-small kernel sizes are coerced (odd, >= 3) without raising.
    img = _thin_text_on_gradient(150, 230)
    for k in (0, 1, 2, 8):
        out = flatten_background(img, k)
        assert out.shape == img.shape
        assert out.dtype == np.uint8
