"""Screenshot capture + OCR pre-processing.

Pipeline :

  1. mss BGR capture
  2. isolate_channel(auto) — picks the channel that maximises text/background
     separation based on R/G/B statistics:
       - lum > 140         → invert grayscale (bright room)
       - R - G > 15        → R channel (red/orange text)
       - G - R > 15        → G channel (green/cyan text)
       - otherwise         → max(R, G, B) (white text — default SC HUD)
  3. Upscale ×2
  4. CLAHE clipLimit=3.0
  5. Conditional GaussianBlur : only if std(channel) > 45 (fast-path
     under normal conditions — saves CPU + better sharpness)
  6. 3 thresholding passes :
       - pass_otsu         : Otsu on isolated channel (text = white, default)
       - pass_otsu_inv     : inverted Otsu (residual dark-on-bright cases)
       - pass_adaptive     : local adaptive threshold (fallback for uniform background)

Compared to the old version (4 passes), the HSV pass was removed as it
produced noise in ~50% of cases — automatic colour channel isolation makes it redundant.
"""
import mss
import numpy as np
import cv2
import configparser
import logging
from sc_ocr.segment import find_glyph_regions, save_glyph_crops

logger = logging.getLogger(__name__)

CAPTURE_WIDTH = 600
CAPTURE_HEIGHT = 150

# Threshold above which a GaussianBlur is applied (otherwise fast-path).
# Metric: standard deviation of the isolated channel. Above ~45, the background
# is genuinely noisy (asteroid texture, particles), a light blur helps.
_NOISE_STD_THRESHOLD = 45.0


def _isolate_channel_auto(bgr):
    """Reduces a BGR image (H, W, 3) to a uint8 channel where text is bright.

    Automatic channel selection based on statistics:
      - Mean luminance > 140 → bright background (lit room). Invert grayscale
        so that dark text becomes bright.
      - R - G > 15 → dominant red/orange text: R channel.
      - G - R > 15 → dominant green/cyan text: G channel.
      - Otherwise → max(R, G, B), ideal for white text on varied background.

    OpenCV uses BGR (b=0, g=1, r=2). The SC HUD is white → the default
    is ``max(R, G, B)`` which maximises contrast for bright text.
    """
    if bgr.ndim == 2:
        return bgr  # already single channel
    b = bgr[..., 0]
    g = bgr[..., 1]
    r = bgr[..., 2]
    lum = bgr.mean()
    r_mean = r.mean()
    g_mean = g.mean()

    if lum > 140:
        # Globally bright background → relatively dark text. Invert grayscale.
        gray = bgr.mean(axis=2)
        return (255 - gray).astype(np.uint8)
    if r_mean - g_mean > 15:
        return r.astype(np.uint8)
    if g_mean - r_mean > 15:
        return g.astype(np.uint8)
    # White/mixed text on dark background — default Star Citizen case.
    return bgr.max(axis=2).astype(np.uint8)


class ScreenCapture:
    def __init__(self):
        self._sct = mss.mss()
        self.monitor_index = 1

        self._clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))

        config = configparser.ConfigParser()
        config.read('config.ini')
        self._save_debug = config.getboolean('Debug', 'save_ocr_images', fallback=False)
        self._save_glyph_crops = config.getboolean('Debug', 'save_glyph_crops', fallback=False)
        test_path = config.get('Debug', 'test_screenshot', fallback='').strip()
        self._test_screenshot = test_path if test_path else None

        self._region = None
        if self._test_screenshot:
            logger.info(f"Test mode: reading from {self._test_screenshot}")
            self._test_img = cv2.imread(self._test_screenshot)
            if self._test_img is None:
                raise FileNotFoundError(f"Screenshot not found: {self._test_screenshot}")
        else:
            monitor = self._sct.monitors[self.monitor_index]
            # The SC debug overlay (r_DisplayInfo 3) starts at pixel 0;
            # capture from the very top otherwise the CamDir line is cut off.
            self._region = {
                "top": monitor["top"],
                "left": monitor["left"] + monitor["width"] - CAPTURE_WIDTH,
                "width": CAPTURE_WIDTH,
                "height": CAPTURE_HEIGHT,
            }
            logger.info(f"Capture {CAPTURE_WIDTH}x{CAPTURE_HEIGHT} top-right, region={self._region}")

    def capture(self):
        if self._test_screenshot:
            img = self._test_img
        else:
            raw = self._sct.grab(self._region)
            img = np.asarray(raw, dtype=np.uint8)[:, :, :3]

        # Phase A: smart colour channel isolation.
        channel = _isolate_channel_auto(img)
        channel = cv2.resize(channel, None, fx=2, fy=2, interpolation=cv2.INTER_LINEAR)

        # Conditional GaussianBlur: only if background is noisy.
        # Preserves sharpness of fine text under normal conditions.
        if float(channel.std()) > _NOISE_STD_THRESHOLD:
            channel = cv2.GaussianBlur(channel, (3, 3), 0)

        enhanced = self._clahe.apply(channel)

        images = {}

        # Pass1: Otsu on isolated channel. Default white HUD case.
        _, otsu = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        images['otsu'] = otsu

        # Pass2: inverted Otsu for residual cases where the isolated channel
        # is insufficient (e.g.: scene transition, mid-luminance).
        _, otsu_inv = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        images['otsu_inv'] = otsu_inv

        # Pass3: local adaptive threshold, safety net for uniformly bright background.
        adaptive = cv2.adaptiveThreshold(
            enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 21, -8
        )
        images['adaptive'] = adaptive

        if self._save_debug:
            cv2.imwrite("debug_capture_original.png", img)
            cv2.imwrite("debug_capture_channel.png", channel)
            cv2.imwrite("debug_capture_otsu.png", otsu)
            cv2.imwrite("debug_capture_adaptive.png", adaptive)

        # Phase D: optional glyph segmentation for template collection
        glyph_data = None
        if self._save_glyph_crops:
            seg_result = find_glyph_regions(otsu)
            glyphs = seg_result['glyphs']
            if glyphs:
                save_glyph_crops(otsu, glyphs)
                glyph_data = seg_result

        return images, glyph_data

    def stop(self):
        pass


if __name__ == "__main__":
    cap = ScreenCapture()
    images = cap.capture()
    if images:
        cv2.imwrite("test_capture.png", images.get('otsu'))
        print("Test capture done: test_capture.png")
    else:
        print("No frame captured.")
