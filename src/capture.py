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
        """Capture la zone définie et retourne plusieurs images prétraitées"""
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
        
        # Création de plusieurs versions prétraitées pour le scoring
        images = {}
        
        # Pass 1 : Seuil fixe 180 (actuel)
        _, thresh1 = cv2.threshold(enhanced, 180, 255, cv2.THRESH_BINARY)
        images['pass1'] = thresh1
        
        # Pass 2 : Inversion + seuil (texte noir sur blanc)
        inverted = cv2.bitwise_not(enhanced)
        _, thresh2 = cv2.threshold(inverted, 80, 255, cv2.THRESH_BINARY)
        images['pass2'] = thresh2
        
        # Pass 3 : Otsu automatique
        _, thresh3 = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        images['pass3'] = thresh3
        
        # Pass 4 : Gamma correction + seuil adaptatif
        gamma = np.power(enhanced/255.0, 0.5) * 255  # Éclaircit les zones sombres
        thresh4 = cv2.adaptiveThreshold(gamma.astype(np.uint8), 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 21, 5)
        images['pass4'] = thresh4
        
        # Sauvegarde pour debug si activé dans config
        try:
            import configparser
            config = configparser.ConfigParser()
            config.read('config.ini')
            if config.getboolean('Debug', 'save_ocr_images', fallback=False):
                cv2.imwrite("debug_capture_processed.png", thresh1)  # Sauvegarde du premier pass
                cv2.imwrite("debug_capture_original.png", img)
        except:
            pass
        
        return images

if __name__ == "__main__":
    cap = ScreenCapture()
    img = cap.capture()
    cv2.imwrite("test_capture.png", img)
    print("Capture de test effectuée : test_capture.png")
