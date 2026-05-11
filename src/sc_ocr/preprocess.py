"""Pré-traitement OCR : isolation canal, seuillage, débruitage.

Pipeline :
  1. isolate_channel() → choisit R/G/B/max selon stats
  2. otsu_threshold() → Otsu pure NumPy ~0.3 ms
  3. denoise_if_needed() → morphologie 3×3 seulement si std > 45
"""
import numpy as np
import cv2
import logging

logger = logging.getLogger(__name__)

_NOISE_STD_THRESHOLD = 45.0


def isolate_channel(image_bgr):
    """Choisit le meilleur canal pour maximiser la séparation texte/fond.

    Stratégie (cf. capture.py isolate_channel_auto) :
      - Luminance > 140 → invert grayscale (pièce éclairée)
      - R - G > 15 → canal R (texte rouge)
      - G - R > 15 → canal G (texte vert)
      - Sinon → max(R, G, B) (texte blanc — cas SC)
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
    """Seuillage Otsu pur NumPy (pas OpenCV) pour reproducibilité.

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
    """Applique un open (érosion + dilatation) 3×3 si std élevée.

    Économise CPU en conditions normales (skip le débruitage).

    Args:
        binary_image : image binaire 0/255
        std_threshold : seuil d'écart-type pour activer le débruitage

    Returns:
        denoised binary image
    """
    if binary_image.std() < std_threshold:
        return binary_image

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    return cv2.morphologyEx(binary_image, cv2.MORPH_OPEN, kernel, iterations=1)


def preprocess_image(image_bgr):
    """Pipeline complet pré-traitement.

    Args:
        image_bgr : image BGR (H, W, 3) uint8

    Returns:
        dict avec :
            - 'binary' : image binaire seuillée 0/255
            - 'channel' : canal isolé avant seuillage (pour debug)
    """
    channel = isolate_channel(image_bgr)
    binary = otsu_threshold(channel)
    binary = denoise_if_needed(binary)

    return {
        'binary': binary,
        'channel': channel,
    }
