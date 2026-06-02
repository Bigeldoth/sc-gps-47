"""Screenshot capture + OCR pre-processing.

Pipeline :

  1. mss BGR capture
  2. isolate_channel(auto) — picks the channel that maximises text/background
     separation based on R/G/B statistics (text kept BRIGHT, never inverted):
       - R - G > 15        → R channel (red/orange text)
       - G - R > 15        → G channel (green/cyan text)
       - otherwise         → max(R, G, B) (white text — default SC HUD)
  3. Upscale ×3 (brings HUD char height from ~16 px to ~48 px)
  4. flatten_background() — white-tophat removes the bright, slowly-varying
     daylight background (desert/terrain) so the thin HUD text survives global
     thresholding instead of drowning in it. Feeds the two binary passes only.
  5. 2 binary thresholding passes + enhanced grayscale :
       - pass_otsu         : Otsu on the flattened channel (text = white,
                             default). Used by Tesseract and as the segmentation
                             source for NCC/ONNX glyph classification.
       - pass_adaptive     : local adaptive threshold on the flattened channel
                             (Tesseract fallback for uniform background).
       - enhanced          : CLAHE grayscale (NOT binary; conditional 3×3 blur
                             if std > 45, then CLAHE clipLimit=3.0). Consumed by
                             NCC/ONNX for classification on crops located via
                             `otsu` segmentation — preserves the fine gradient
                             detail that binary thresholding destroys. NOT
                             tophat-flattened, so the trained classifier's input
                             distribution is unchanged.
       - raw               : original BGR crop straight from mss (or test file)
                             before any channel isolation or thresholding. Used
                             by full-image OCR engines (e.g. PaddleOCR) that
                             prefer to run their own detection on natural images.

The previous `lum > 140 → invert grayscale` branch was removed: on real daylight
frames it made OCR worse (inverting light-on-light still leaves the text in the
background); `flatten_background` suppresses bright scenes structurally instead.
The `otsu_inv` pass was removed earlier (it destroyed characters), as was the
HSV pass (automatic channel isolation made it redundant).
"""
import time
import mss
import numpy as np
import cv2
import configparser
import logging
from sc_ocr.segment import find_glyph_regions, save_glyph_crops
from sc_ocr.preprocess import isolate_channel, flatten_background

logger = logging.getLogger(__name__)

CAPTURE_WIDTH = 600
CAPTURE_HEIGHT = 45

# Canonical upscale factor applied to the HUD strip before OCR / classification.
# ×3 brings the average HUD char height from ~16 px native to ~48 px — the
# sweet spot for Tesseract's LSTM (trained around 36 px line height). ×2 left
# us at 32 px which mis-discriminated 8↔6 / 0↔6 where the difference is 1-2 px
# of stroke thickness. Larger factors hurt latency without improving accuracy.
#
# This value is the single source of truth: every preprocessing pipeline
# (runtime capture, Paddle adapter, training dataset builders, diagnostics)
# imports it from here so trained models always match runtime resolution.
UPSCALE_FACTOR = 3

# Threshold above which a GaussianBlur is applied (otherwise fast-path).
# Metric: standard deviation of the isolated channel. Above ~45, the background
# is genuinely noisy (asteroid texture, particles), a light blur helps.
_NOISE_STD_THRESHOLD = 45.0

# White-tophat kernel (px) for background flattening before binarisation.
# Sized just above the upscaled HUD stroke thickness (~3-6 px at
# UPSCALE_FACTOR=3): the morphological opening then removes everything larger
# (bright daylight terrain + gradients) while preserving the thin text. Field
# frames: 3 x UPSCALE_FACTOR = 9 px doubled daylight coordinate extraction with
# no night regression; larger kernels start leaking background back in.
_BG_FLATTEN_KERNEL_PX = 3 * UPSCALE_FACTOR

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
        # Stamp the frame instant up front so downstream velocity estimation
        # uses the *measurement* time, not the (variable) time the OCR result
        # is later handled on the UI thread. Monotonic clock: immune to wall
        # clock adjustments, and the only thing dt comparisons need.
        t_capture = time.monotonic()
        if self._test_screenshot:
            r = self._region
            img = self._test_img[r["top"]:r["top"] + r["height"],
                                  r["left"]:r["left"] + r["width"]]
        else:
            raw = self._sct.grab(self._region)
            img = np.asarray(raw, dtype=np.uint8)[:, :, :3]

        # Phase A: smart colour channel isolation (text kept bright, never inverted).
        channel = isolate_channel(img)
        # Costs ~+25 % per-frame latency, still well under scan_interval_ms=200.
        # See UPSCALE_FACTOR docstring above for the rationale on the value.
        channel = cv2.resize(
            channel, None, fx=UPSCALE_FACTOR, fy=UPSCALE_FACTOR,
            interpolation=cv2.INTER_LINEAR,
        )

        images = {}

        # Raw BGR crop (no preprocessing). Consumed by engines that run their
        # own detection on natural images (e.g. PaddleOCR in full_text mode).
        images['raw'] = img

        # Binary passes operate on a white-tophat-flattened channel. This strips
        # the slowly-varying bright background (daylight terrain + gradients)
        # that global Otsu / small-block adaptive cannot separate from the thin
        # HUD text, leaving the text on a flat ~black field. Field-validated to
        # roughly double daylight coordinate extraction with no night regression.
        flat = flatten_background(channel, _BG_FLATTEN_KERNEL_PX)

        # Pass1: Otsu on the flattened channel. Default white HUD case. Used both
        # for Tesseract and as the segmentation source for NCC/ONNX.
        _, otsu = cv2.threshold(flat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        images['otsu'] = otsu

        # Pass2: local adaptive threshold, safety net for uniformly bright background.
        adaptive = cv2.adaptiveThreshold(
            flat, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 21, -8
        )
        images['adaptive'] = adaptive

        # CLAHE-enhanced grayscale: used by NCC/ONNX for classification on the
        # crops located via the binary `otsu` segmentation. Preserves the fine
        # gradient detail that binary thresholding destroys. Kept on its own
        # (conditional blur + CLAHE) recipe — NOT the tophat — so the trained
        # classifier's input distribution is unchanged.
        enh_channel = channel
        if float(enh_channel.std()) > _NOISE_STD_THRESHOLD:
            enh_channel = cv2.GaussianBlur(enh_channel, (3, 3), 0)
        enhanced = self._clahe.apply(enh_channel)
        images['enhanced'] = enhanced

        if self._save_debug:
            cv2.imwrite("debug_capture_original.png", img)
            cv2.imwrite("debug_capture_channel.png", channel)
            cv2.imwrite("debug_capture_flat.png", flat)
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

        return images, glyph_data, t_capture

    def stop(self):
        pass


if __name__ == "__main__":
    cap = ScreenCapture()
    images, _glyph, _t_capture = cap.capture()
    if images:
        cv2.imwrite("test_capture.png", images.get('otsu'))
        print("Test capture done: test_capture.png")
    else:
        print("No frame captured.")
