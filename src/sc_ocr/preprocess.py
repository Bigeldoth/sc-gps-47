"""OCR pre-processing: channel isolation, thresholding, denoising.

Pipeline:
  1. isolate_channel() → picks R/G/B/max (text kept bright, never inverted)
  2. flatten_background() → white-tophat strips bright daylight backgrounds
  3. otsu_threshold() → pure NumPy Otsu ~0.3 ms
  4. denoise_if_needed() → 3×3 morphology only if std > 45
"""
import numpy as np
import cv2
import logging

logger = logging.getLogger(__name__)

_NOISE_STD_THRESHOLD = 45.0


def isolate_channel(image_bgr):
    """Picks the best channel to maximise text/background separation.

    The returned channel always keeps the HUD text BRIGHT (never inverted), so a
    downstream white-tophat (`flatten_background`) can strip the background:
      - R - G > 15 → R channel (red/orange text)
      - G - R > 15 → G channel (green/cyan text)
      - Otherwise → max(R, G, B) (white text — default SC HUD)

    The former ``luminance > 140 → invert grayscale`` branch was removed: on real
    daylight frames (light HUD text over a bright textured desert) inverting a
    light-on-light frame still leaves the text drowning in the background, so OCR
    got *worse*, not better. Background suppression is now done structurally by
    `flatten_background`, which is robust regardless of scene brightness.
    """
    if image_bgr.ndim == 2:
        return image_bgr

    b = image_bgr[..., 0]
    g = image_bgr[..., 1]
    r = image_bgr[..., 2]

    r_mean = r.mean()
    g_mean = g.mean()

    if r_mean - g_mean > 15:
        return r.astype(np.uint8)
    if g_mean - r_mean > 15:
        return g.astype(np.uint8)
    return image_bgr.max(axis=2).astype(np.uint8)


def flatten_background(channel_gray, kernel_px):
    """White-tophat background flattening for daylight HUD frames.

    SC HUD text is thin and bright; in daylight it sits over a bright, textured,
    slowly-varying background (desert, terrain gradients) that a single global
    Otsu — or a small-block adaptive threshold — cannot separate from the text
    (the background floods white, or its edges are picked up as glyph strokes).

    A morphological white-tophat (``img - opening(img, kernel)``) keeps only the
    bright structures THINNER than the kernel (the text strokes) and removes
    everything larger (the background and its gradients). Sizing the kernel just
    above the upscaled stroke thickness leaves the text intact on a flat ~black
    field, so the subsequent Otsu pass separates it trivially.

    Args:
        channel_gray : single-channel uint8 image where text is bright.
        kernel_px    : ellipse kernel diameter in pixels (forced odd, >= 3).

    Returns:
        uint8 image: text bright, background flattened to ~0.
    """
    if channel_gray.ndim != 2:
        raise ValueError("flatten_background expects a single-channel image")
    k = max(3, int(kernel_px) | 1)  # force odd, >= 3
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    tophat = cv2.morphologyEx(channel_gray, cv2.MORPH_TOPHAT, kernel)
    if tophat.max() == tophat.min():
        return tophat  # uniform input (no bright text) — nothing to stretch
    return cv2.normalize(tophat, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)


def otsu_threshold(image_gray):
    """Pure NumPy Otsu thresholding (not OpenCV) for reproducibility.

    Returns:
        binary image (0/255)
    """
    if image_gray.dtype != np.uint8:
        image_gray = image_gray.astype(np.uint8)

    hist = np.histogram(image_gray, bins=256, range=(0, 256))[0]
    hist = hist.astype(np.float32)

    total_pixels = image_gray.size
    sum_total = np.sum(np.arange(256) * hist)
    sum_back = 0
    weight_back = 0
    max_variance = 0
    threshold = 0

    for t in range(256):
        weight_back += hist[t]
        if weight_back == 0:
            continue

        weight_fore = total_pixels - weight_back
        if weight_fore == 0:
            break

        sum_back += t * hist[t]
        mean_back = sum_back / weight_back
        mean_fore = (sum_total - sum_back) / weight_fore

        variance = weight_back * weight_fore * (mean_back - mean_fore) ** 2
        if variance > max_variance:
            max_variance = variance
            threshold = t

    return np.where(image_gray >= threshold, 255, 0).astype(np.uint8)


def denoise_if_needed(binary_image, std_threshold=_NOISE_STD_THRESHOLD):
    """Applies a 3×3 open (erosion + dilation) if std is high.

    Saves CPU under normal conditions (skips denoising).

    Args:
        binary_image : binary image 0/255
        std_threshold : standard deviation threshold to activate denoising

    Returns:
        denoised binary image
    """
    if binary_image.std() < std_threshold:
        return binary_image

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    return cv2.morphologyEx(binary_image, cv2.MORPH_OPEN, kernel, iterations=1)


def preprocess_image(image_bgr):
    """Full pre-processing pipeline.

    Args:
        image_bgr : BGR image (H, W, 3) uint8

    Returns:
        dict with:
            - 'binary' : thresholded binary image 0/255
            - 'channel' : isolated channel before thresholding (for debug)
    """
    channel = isolate_channel(image_bgr)
    binary = otsu_threshold(channel)
    binary = denoise_if_needed(binary)

    return {
        'binary': binary,
        'channel': channel,
    }
