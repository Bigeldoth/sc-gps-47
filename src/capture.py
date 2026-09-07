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
import app_paths
from capture_monitors import list_capture_monitors
from sc_ocr.segment import find_glyph_regions, save_glyph_crops
from sc_ocr.preprocess import isolate_channel, flatten_background

logger = logging.getLogger(__name__)

# Horizontal dimensions at the historical 1920-pixel reference resolution.
CAPTURE_WIDTH = 600
CAPTURE_REFERENCE_WIDTH = 1920
CAPTURE_RIGHT_MARGIN = 10
CAPTURE_HEIGHT = 60

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


def capture_region(width, height, *, left=0, top=0):
    """Scale the HUD strip horizontally in physical pixels; keep its height fixed."""
    crop_width = min(width, max(1, CAPTURE_WIDTH * width // CAPTURE_REFERENCE_WIDTH))
    right_margin = min(width - crop_width,
                       CAPTURE_RIGHT_MARGIN * width // CAPTURE_REFERENCE_WIDTH)
    return {
        'left': left + width - crop_width - right_margin,
        'top': top,
        'width': crop_width,
        'height': min(CAPTURE_HEIGHT, height),
    }


class CaptureMonitorUnavailable(RuntimeError):
    """The selected physical display cannot currently be captured."""


class ScreenCapture:
    def __init__(self, config=None):
        # Construct, configure, capture and stop this object on the same thread:
        # MSS owns native display resources which must not cross Qt threads.
        self._sct = None
        self._clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))

        if config is None:
            config = configparser.ConfigParser()
            config.read(
                [app_paths.bundle_dir() / 'config.ini',
                 app_paths.user_data_dir() / 'config.ini'],
                encoding='utf-8-sig',
            )
        self._save_debug = config.getboolean('Debug', 'save_ocr_images', fallback=False)
        self._save_glyph_crops = config.getboolean('Debug', 'save_glyph_crops', fallback=False)
        test_path = config.get('Debug', 'test_screenshot', fallback='').strip()
        self._test_screenshot = test_path if test_path else None
        try:
            self.monitor_id = config.get('Capture', 'monitor_id', fallback='').strip()
        except configparser.InterpolationError:
            self.monitor_id = config.get('Capture', 'monitor_id', fallback='', raw=True).strip()
        self._source_identity = ('monitor', self.monitor_id, None)
        self._availability_error = None
        self._last_refresh = float('-inf')

        self._region = None
        if self._test_screenshot:
            logger.info(f"Test mode: reading from {self._test_screenshot}")
            self._test_img = cv2.imread(self._test_screenshot)
            if self._test_img is None:
                raise FileNotFoundError(f"Screenshot not found: {self._test_screenshot}")
            h, w = self._test_img.shape[:2]
            self._region = capture_region(w, h)
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
            self._source_identity = (
                'screenshot', self._test_screenshot,
                *(self._region[key] for key in ('left', 'top', 'width', 'height')),
            )
        else:
            self.refresh_region(force=True)

    @property
    def source_identity(self):
        """Identify the selection and current bounds for stale-frame rejection."""
        return self._source_identity

    @property
    def availability_error(self):
        """Return an English status message while the selected display is absent."""
        return self._availability_error

    def configure_monitor(self, monitor_id):
        """Apply a persisted display choice between captures in the owning thread."""
        self.monitor_id = str(monitor_id or '').strip()
        return self.refresh_region(force=True)

    def _clear_display(self, message):
        self._region = None
        self._source_identity = ('monitor', self.monitor_id, None)
        self._availability_error = message
        self.stop()

    def refresh_region(self, force=False):
        """Refresh physical topology at most once per second without grabbing pixels.

        Missing saved IDs never fall back to another screen. Re-enumeration lets
        the same selection resume after reconnection without changing preferences.
        Return whether the source changed, including availability transitions.
        """
        if self._test_screenshot:
            return False
        now = time.monotonic()
        if not force and now - self._last_refresh < 1.0:
            return False
        self._last_refresh = now
        previous = self._source_identity
        try:
            monitors = list_capture_monitors()
            if self.monitor_id:
                matches = [monitor for monitor in monitors if monitor['id'] == self.monitor_id]
                if len(matches) != 1:
                    self._clear_display('Selected display unavailable')
                    return previous != self._source_identity
                monitor = matches[0]
            else:
                monitor = next((m for m in monitors if m['is_primary']), None)
                if monitor is None:
                    self._clear_display('Primary display unavailable')
                    return previous != self._source_identity
            region = capture_region(
                monitor['width'], monitor['height'],
                left=monitor['left'], top=monitor['top'],
            )
            identity = (
                'monitor', self.monitor_id, monitor['id'],
                *(monitor[key] for key in ('left', 'top', 'width', 'height')),
            )
            if identity != previous or self._sct is None:
                self.stop()
                self._sct = mss.mss()
            self._region = region
            self._source_identity = identity
            self._availability_error = None
            if identity != previous:
                logger.info('Capture source: %s, region=%s', monitor['label'], region)
        except Exception:
            logger.warning('Capture display enumeration failed', exc_info=True)
            self._clear_display('Capture displays unavailable')
        return previous != self._source_identity

    @property
    def screen_region(self):
        """Return the live capture rectangle in physical desktop pixels.

        File-backed screenshots have image coordinates, not a desktop region.
        Return a copy so debug UI consumers cannot change the capture source.
        """
        if self._test_screenshot or self._region is None:
            return None
        return dict(self._region)

    def capture(self, *, refresh=True):
        if not self._test_screenshot:
            if refresh:
                self.refresh_region()
            if self._region is None or self._sct is None:
                raise CaptureMonitorUnavailable(
                    self._availability_error or 'Selected display unavailable'
                )
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
            try:
                raw = self._sct.grab(self._region)
            except Exception as exc:
                # Invalidate the debug outline and let the next scan retry a
                # fresh native handle after a display mode/disconnection error.
                self._clear_display('Capture display unavailable')
                self._last_refresh = float('-inf')
                raise CaptureMonitorUnavailable(self._availability_error) from exc
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
        """Release native resources in the same thread that created them."""
        if self._sct is not None:
            sct, self._sct = self._sct, None
            sct.close()


if __name__ == "__main__":
    cap = ScreenCapture()
    images, _glyph, _t_capture = cap.capture()
    if images:
        cv2.imwrite("test_capture.png", images.get('otsu'))
        print("Test capture done: test_capture.png")
    else:
        print("No frame captured.")
