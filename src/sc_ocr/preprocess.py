"""OCR pre-processing: channel isolation, thresholding, denoising.

Pipeline:
  1. isolate_channel() → picks R/G/B/max based on stats
  2. otsu_threshold() → pure NumPy Otsu ~0.3 ms
  3. denoise_if_needed() → 3×3 morphology only if std > 45
"""
import numpy as np
import cv2
import logging

logger = logging.getLogger(__name__)

_NOISE_STD_THRESHOLD = 45.0


def isolate_channel(image_bgr):
    """Picks the best channel to maximise text/background separation.

    Strategy (cf. capture.py isolate_channel_auto):
      - Luminance > 140 → invert grayscale (bright room)
      - R - G > 15 → R channel (red text)
      - G - R > 15 → G channel (green text)
      - Otherwise → max(R, G, B) (white text — SC case)
    """
    if image_bgr.ndim == 2:
        return image_bgr

    b = image_bgr[..., 0]
    g = image_bgr[..., 1]
    r = image_bgr[..., 2]

    lum = image_bgr.mean()
    r_mean = r.mean()
    g_mean = g.mean()

    if lum > 140:
        gray = image_bgr.mean(axis=2)
        return (255 - gray).astype(np.uint8)
    if r_mean - g_mean > 15:
        return r.astype(np.uint8)
    if g_mean - r_mean > 15:
        return g.astype(np.uint8)
    return image_bgr.max(axis=2).astype(np.uint8)


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
