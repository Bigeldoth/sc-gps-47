import mss
import numpy as np
import cv2
import configparser
import logging

logger = logging.getLogger(__name__)


class ScreenCapture:
    def __init__(self):
        self.sct = mss.mss()
        self.monitor_index = 1

        self._clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))

        self._cached_zone = None
        self._cached_monitor_key = None

        config = configparser.ConfigParser()
        config.read('config.ini')
        self._save_debug = config.getboolean('Debug', 'save_ocr_images', fallback=False)

    def _monitor_key(self, monitor):
        return (monitor["top"], monitor["left"], monitor["width"], monitor["height"])

    def get_capture_zone(self):
        monitor = self.sct.monitors[self.monitor_index]
        key = self._monitor_key(monitor)
        if self._cached_zone is not None and self._cached_monitor_key == key:
            return self._cached_zone

        screen_width = monitor["width"]
        screen_height = monitor["height"]

        width_ratio = screen_width / 1920
        height_ratio = screen_height / 1080

        width = int(600 * width_ratio)
        height = int(250 * height_ratio)

        zone = {
            "top": monitor["top"] + int(10 * height_ratio),
            "left": monitor["left"] + monitor["width"] - width - int(10 * width_ratio),
            "width": width,
            "height": height,
        }
        self._cached_zone = zone
        self._cached_monitor_key = key
        return zone

    def capture(self):
        region = self.get_capture_zone()
        screenshot = self.sct.grab(region)

        img = np.array(screenshot)
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

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


if __name__ == "__main__":
    cap = ScreenCapture()
    img = cap.capture()
    cv2.imwrite("test_capture.png", img)
    print("Capture de test effectuée : test_capture.png")
