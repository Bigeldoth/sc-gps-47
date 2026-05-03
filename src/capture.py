import mss
import numpy as np
import cv2
from PIL import Image

class ScreenCapture:
    def __init__(self):
        self.sct = mss.mss()
        # Coordonnées typiques pour le coin bas-droite en 1920x1080
        # À ajuster selon la résolution détectée
        self.monitor_index = 1
        
    def get_capture_zone(self):
        """Définit la zone de capture (bas droite de l'écran)"""
        monitor = self.sct.monitors[self.monitor_index]
        width = 450
        height = 150
        
        # Positionnement dans le coin inférieur droit
        return {
            "top": monitor["top"] + monitor["height"] - height - 50,
            "left": monitor["left"] + monitor["width"] - width - 20,
            "width": width,
            "height": height
        }

    def capture(self):
        """Capture la zone définie et retourne une image OpenCV traitée"""
        region = self.get_capture_zone()
        screenshot = self.sct.grab(region)
        
        # Conversion en format OpenCV (BGR)
        img = np.array(screenshot)
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        
        # Prétraitement pour l'OCR
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        # Augmentation du contraste et seuillage pour isoler le texte blanc
        _, thresh = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
        
        return thresh

if __name__ == "__main__":
    cap = ScreenCapture()
    img = cap.capture()
    cv2.imwrite("test_capture.png", img)
    print("Capture de test effectuée : test_capture.png")
