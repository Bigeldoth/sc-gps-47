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
        # Zone typique pour r_displayinfo 3 en haut à droite
        # Augmenté pour capturer plus d'informations
        width = 600
        height = 250
        
        return {
            "top": monitor["top"] + 10,
            "left": monitor["left"] + monitor["width"] - width - 10,
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
        
        # Débruitage avec filtre bilateral (préserve les bords)
        denoised = cv2.bilateralFilter(gray, 9, 75, 75)
        
        # Amélioration du contraste avec CLAHE (augmenté pour meilleur contraste)
        clahe = cv2.createCLAHE(clipLimit=5.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(denoised)
        
        # Ajout d'un filtre de netteté pour améliorer la lisibilité
        kernel_sharpening = np.array([[-1,-1,-1],
                                       [-1, 9,-1],
                                       [-1,-1,-1]])
        sharpened = cv2.filter2D(enhanced, -1, kernel_sharpening)
        
        # Seuillage adaptatif pour isoler le texte blanc
        # Paramètres optimisés pour texte blanc sur fond sombre
        thresh = cv2.adaptiveThreshold(
            sharpened, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
            cv2.THRESH_BINARY, 15, -2
        )
        
        # Méthode alternative : seuillage Otsu comme backup
        # Utile si le contraste est très variable
        _, thresh_otsu = cv2.threshold(sharpened, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        # Combiner les deux méthodes (OR logique) pour maximiser la détection
        combined = cv2.bitwise_or(thresh, thresh_otsu)
        
        return combined

if __name__ == "__main__":
    cap = ScreenCapture()
    img = cap.capture()
    cv2.imwrite("test_capture.png", img)
    print("Capture de test effectuée : test_capture.png")
