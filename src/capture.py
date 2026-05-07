import mss
import numpy as np
import cv2
import configparser
import logging

logger = logging.getLogger(__name__)

CAPTURE_WIDTH = 400
CAPTURE_HEIGHT = 200


class ScreenCapture:
    def __init__(self):
        self._sct = mss.mss()
        self.monitor_index = 1

        self._clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))

        config = configparser.ConfigParser()
        config.read('config.ini')
        self._save_debug = config.getboolean('Debug', 'save_ocr_images', fallback=False)
        test_path = config.get('Debug', 'test_screenshot', fallback='').strip()
        self._test_screenshot = test_path if test_path else None

        self._region = None
        if self._test_screenshot:
            logger.info(f"Mode test : lecture depuis {self._test_screenshot}")
            self._test_img = cv2.imread(self._test_screenshot)
            if self._test_img is None:
                raise FileNotFoundError(f"Screenshot introuvable : {self._test_screenshot}")
        else:
            monitor = self._sct.monitors[self.monitor_index]
            self._region = {
                "top": monitor["top"] + 10,
                "left": monitor["left"] + monitor["width"] - CAPTURE_WIDTH - 10,
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

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_LINEAR)

        denoised = cv2.GaussianBlur(gray, (3, 3), 0)
        enhanced = self._clahe.apply(denoised)

        images = {}

        _, thresh1 = cv2.threshold(enhanced, 180, 255, cv2.THRESH_BINARY)
        images['pass1'] = thresh1

        _, thresh2 = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        images['pass2'] = thresh2

        if self._save_debug:
            cv2.imwrite("debug_capture_processed.png", thresh1)
            cv2.imwrite("debug_capture_original.png", img)

        return images

    def stop(self):
        pass


if __name__ == "__main__":
    cap = ScreenCapture()
    images = cap.capture()
    if images:
        cv2.imwrite("test_capture.png", images.get('pass1'))
        print("Capture de test effectuée : test_capture.png")
    else:
        print("Pas de frame capturée.")
