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
  6. 2 binary thresholding passes + enhanced grayscale :
       - pass_otsu         : Otsu on isolated channel (text = white, default).
                             Used by Tesseract and as the segmentation source
                             for NCC/ONNX glyph classification.
       - pass_adaptive     : local adaptive threshold (Tesseract fallback for
                             uniform background).
       - enhanced          : CLAHE grayscale (NOT binary). Consumed by NCC/ONNX
                             for classification on crops located via `otsu`
                             segmentation — preserves the fine gradient detail
                             that binary thresholding destroys.
       - raw               : original BGR crop straight from mss (or test file)
                             before any channel isolation or thresholding. Used
                             by full-image OCR engines (e.g. PaddleOCR) that
                             prefer to run their own detection on natural images.

The previous `otsu_inv` pass was removed: in all observed cases it destroyed
characters rather than recovering them, and Tesseract performed worst on it.
The HSV pass was removed earlier as automatic channel isolation made it redundant.
"""
import mss
import numpy as np
import cv2
import configparser
import logging
from sc_ocr.segment import find_glyph_regions, save_glyph_crops
from sc_ocr.preprocess import isolate_channel

logger = logging.getLogger(__name__)

CAPTURE_WIDTH = 600
CAPTURE_HEIGHT = 45

# Threshold above which a GaussianBlur is applied (otherwise fast-path).
# Metric: standard deviation of the isolated channel. Above ~45, the background
# is genuinely noisy (asteroid texture, particles), a light blur helps.
_NOISE_STD_THRESHOLD = 45.0

# Backward-compat alias for external callers (e.g. tools/dataset_builder.py
# pinned to the previous private name).
_isolate_channel_auto = isolate_channel


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
            h, w = self._test_img.shape[:2]
            self._region = {
                "top": 0,
                "left": max(0, w - CAPTURE_WIDTH),
                "width": min(CAPTURE_WIDTH, w),
                "height": min(CAPTURE_HEIGHT, h),
            }
            custom_left = config.getint('Debug', 'test_region_left', fallback=-1)
            custom_top = config.getint('Debug', 'test_region_top', fallback=-1)
            custom_w = config.getint('Debug', 'test_region_width', fallback=-1)
            custom_h = config.getint('Debug', 'test_region_height', fallback=-1)
            if custom_left >= 0 and custom_top >= 0 and custom_w > 0 and custom_h > 0:
                self._region = {
                    "top": custom_top,
                    "left": custom_left,
                    "width": custom_w,
                    "height": custom_h,
                }
            logger.info(f"Test mode: crop region={self._region} from {w}x{h} image")
        else:
            monitor = self._sct.monitors[self.monitor_index]
            self._region = {
                "top": monitor["top"],
                "left": monitor["left"] + monitor["width"] - CAPTURE_WIDTH,
                "width": CAPTURE_WIDTH,
                "height": CAPTURE_HEIGHT,
            }
            logger.info(f"Capture {CAPTURE_WIDTH}x{CAPTURE_HEIGHT} top-right, region={self._region}")

    def capture(self):
        if self._test_screenshot:
            r = self._region
            img = self._test_img[r["top"]:r["top"] + r["height"],
                                  r["left"]:r["left"] + r["width"]]
        else:
            raw = self._sct.grab(self._region)
            img = np.asarray(raw, dtype=np.uint8)[:, :, :3]

        # Phase A: smart colour channel isolation.
        channel = isolate_channel(img)
        # ×3 upscale brings the average HUD char height from ~16 px native to
        # ~48 px — right in the sweet spot for Tesseract's LSTM (trained around
        # 36 px line height). ×2 left us at 32 px which is just below and
        # measurably mis-discriminates 8↔6 / 0↔6 where the difference is 1-2 px
        # of stroke thickness. Costs ~+25 % per-frame latency, still well under
        # scan_interval_ms=200.
        channel = cv2.resize(channel, None, fx=3, fy=3, interpolation=cv2.INTER_LINEAR)

        # Conditional GaussianBlur: only if background is noisy.
        # Preserves sharpness of fine text under normal conditions.
        if float(channel.std()) > _NOISE_STD_THRESHOLD:
            channel = cv2.GaussianBlur(channel, (3, 3), 0)

        enhanced = self._clahe.apply(channel)

        images = {}

        # Raw BGR crop (no preprocessing). Consumed by engines that run their
        # own detection on natural images (e.g. PaddleOCR in full_text mode).
        images['raw'] = img

        # Pass1: Otsu on isolated channel. Default white HUD case. Used both
        # for Tesseract and as the segmentation source for NCC/ONNX.
        _, otsu = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        images['otsu'] = otsu

        # Pass2: local adaptive threshold, safety net for uniformly bright background.
        adaptive = cv2.adaptiveThreshold(
            enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 21, -8
        )
        images['adaptive'] = adaptive

        # CLAHE-enhanced grayscale: used by NCC/ONNX for classification on the
        # crops located via the binary `otsu` segmentation. Preserves the fine
        # gradient detail that binary thresholding destroys.
        images['enhanced'] = enhanced

        if self._save_debug:
            cv2.imwrite("debug_capture_original.png", img)
            cv2.imwrite("debug_capture_channel.png", channel)
            cv2.imwrite("debug_capture_enhanced.png", enhanced)
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
