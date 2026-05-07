import mss
import numpy as np
import cv2
import configparser
import logging
import threading
import time

logger = logging.getLogger(__name__)

CAPTURE_WIDTH = 400
CAPTURE_HEIGHT = 200


class ScreenCapture:
    def __init__(self):
        self.monitor_index = 1

        self._clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))

        config = configparser.ConfigParser()
        config.read('config.ini')
        self._save_debug = config.getboolean('Debug', 'save_ocr_images', fallback=False)
        test_path = config.get('Debug', 'test_screenshot', fallback='').strip()
        self._test_screenshot = test_path if test_path else None

        self._frame = None
        self._frame_lock = threading.Lock()
        self._running = True

        if self._test_screenshot:
            logger.info(f"Mode test : lecture depuis {self._test_screenshot}")
            img = cv2.imread(self._test_screenshot)
            if img is None:
                raise FileNotFoundError(f"Screenshot introuvable : {self._test_screenshot}")
            with self._frame_lock:
                self._frame = img
        else:
            self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
            self._capture_thread.start()

    def _capture_loop(self):
        sct = mss.mss()
        monitor = sct.monitors[self.monitor_index]
        region = {
            "top": monitor["top"] + 10,
            "left": monitor["left"] + monitor["width"] - CAPTURE_WIDTH - 10,
            "width": CAPTURE_WIDTH,
            "height": CAPTURE_HEIGHT,
        }
        logger.info(f"Capture continue : {CAPTURE_WIDTH}x{CAPTURE_HEIGHT} top-right, region={region}")

        while self._running:
            raw = sct.grab(region)
            img = np.array(raw, dtype=np.uint8)
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            with self._frame_lock:
                self._frame = img
            time.sleep(0.2)

    def capture(self):
        with self._frame_lock:
            img = self._frame

        if img is None:
            return {}

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

        denoised = cv2.bilateralFilter(gray, 5, 50, 50)
        enhanced = self._clahe.apply(denoised)

        images = {}

        _, thresh1 = cv2.threshold(enhanced, 180, 255, cv2.THRESH_BINARY)
        images['pass1'] = thresh1

        inverted = cv2.bitwise_not(enhanced)
        _, thresh2 = cv2.threshold(inverted, 80, 255, cv2.THRESH_BINARY)
        images['pass2'] = thresh2

        _, thresh3 = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        images['pass3'] = thresh3

        gamma = np.power(enhanced / 255.0, 0.5) * 255
        thresh4 = cv2.adaptiveThreshold(
            gamma.astype(np.uint8), 255,
            cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 21, 5
        )
        images['pass4'] = thresh4

        if self._save_debug:
            cv2.imwrite("debug_capture_processed.png", thresh1)
            cv2.imwrite("debug_capture_original.png", img)

        return images

    def stop(self):
        self._running = False


if __name__ == "__main__":
    cap = ScreenCapture()
    time.sleep(0.1)
    images = cap.capture()
    if images:
        cv2.imwrite("test_capture.png", images.get('pass1'))
        print("Capture de test effectuée : test_capture.png")
    else:
        print("Pas de frame capturée.")
    cap.stop()
