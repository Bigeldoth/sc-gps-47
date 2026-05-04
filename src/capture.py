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
        """Définit la zone de capture (haut droite de l'écran pour r_displayinfo 3)"""
        monitor = self.sct.monitors[self.monitor_index]
        
        # Adaptation automatique selon la résolution
        screen_width = monitor["width"]
        screen_height = monitor["height"]
        
        # Calcul proportionnel basé sur 1920x1080 comme référence
        # Pour 2560x1440: ratio = 1.33
        width_ratio = screen_width / 1920
        height_ratio = screen_height / 1080
        
        # Zone de capture adaptée à la résolution
        width = int(600 * width_ratio)
        height = int(250 * height_ratio)
        
        return {
            "top": monitor["top"] + int(10 * height_ratio),
            "left": monitor["left"] + monitor["width"] - width - int(10 * width_ratio),
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
        
        # Prétraitement optimisé pour le petit texte blanc de Star Citizen
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        
        # Upscaling x2 pour améliorer la lisibilité du petit texte
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        
        # Débruitage léger avec filtre bilateral (préserve les bords)
        denoised = cv2.bilateralFilter(gray, 5, 50, 50)
        
        # Amélioration du contraste avec CLAHE
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(denoised)
        
        # Seuillage simple pour isoler le texte blanc sur fond sombre
        # Utilise un seuil fixe car le texte est toujours blanc
        _, thresh = cv2.threshold(enhanced, 180, 255, cv2.THRESH_BINARY)
        
        # Sauvegarde pour debug si activé dans config
        try:
            import configparser
            config = configparser.ConfigParser()
            config.read('config.ini')
            if config.getboolean('Debug', 'save_ocr_images', fallback=False):
                cv2.imwrite("debug_capture_processed.png", thresh)
                cv2.imwrite("debug_capture_original.png", img)
        except:
            pass
        
        return thresh

if __name__ == "__main__":
    cap = ScreenCapture()
    img = cap.capture()
    cv2.imwrite("test_capture.png", img)
    print("Capture de test effectuée : test_capture.png")
