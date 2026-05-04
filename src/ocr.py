import pytesseract
import re
import cv2
import os
import sys

class OCRProcessor:
    def __init__(self, tesseract_path=None):
        # Pour le mode exécutable "clé en main"
        if not tesseract_path:
            # On cherche tesseract dans un dossier 'tesseract' à côté de l'exécutable
            base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
            # Liste des chemins possibles pour Tesseract
            possible_paths = []
            
            # 1. Dossier local 'tesseract' (pour l'exécutable portable)
            if getattr(sys, 'frozen', False):
                possible_paths.append(os.path.join(os.path.dirname(sys.executable), "tesseract", "tesseract.exe"))
            else:
                possible_paths.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tesseract", "tesseract.exe"))
            
            # 2. Installation standard Program Files
            possible_paths.append(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
            possible_paths.append(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")
            
            # 3. Chemin utilisateur local
            user_local = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Tesseract-OCR', 'tesseract.exe')
            possible_paths.append(user_local)

            found = False
            for path in possible_paths:
                if os.path.exists(path):
                    pytesseract.pytesseract.tesseract_cmd = path
                    found = True
                    break
            
            if not found:
                print("ATTENTION: Tesseract-OCR non trouvé dans les emplacements standards.")
        else:
            pytesseract.pytesseract.tesseract_cmd = tesseract_path
            
    def extract_data(self, image):
        """Extrait les coordonnées et le lieu de l'image traitée"""
        # Utilisation de config pour optimiser la reconnaissance des chiffres et symboles
        custom_config = r'--oem 3 --psm 6'
        text = pytesseract.image_to_string(image, config=custom_config)
        
        return self.parse_text(text)

    def parse_text(self, text):
        """Analyse le texte brut pour trouver X, Y, Z et le lieu"""
        data = {
            "location": "Unknown",
            "x": None,
            "y": None,
            "z": None
        }
        
        # Nettoyage du texte
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        
        for line in lines:
            # Recherche des coordonnées (format X: 123.4 Y: 567.8 Z: 910.1)
            coord_match = re.search(r'X:\s*(-?\d+\.?\d*).*Y:\s*(-?\d+\.?\d*).*Z:\s*(-?\d+\.?\d*)', line, re.IGNORECASE)
            if coord_match:
                data["x"] = float(coord_match.group(1))
                data["y"] = float(coord_match.group(2))
                data["z"] = float(coord_match.group(3))
            elif len(line) > 3 and not any(c in line for c in [':', '=']):
                # On assume que la ligne sans ':' est le nom du lieu
                data["location"] = line
                
        return data

if __name__ == "__main__":
    # Test rapide si une image existe
    processor = OCRProcessor()
    img = cv2.imread("test_capture.png")
    if img is not None:
        result = processor.extract_data(img)
        print(f"Résultat OCR : {result}")
    else:
        print("Image test_capture.png non trouvée.")
