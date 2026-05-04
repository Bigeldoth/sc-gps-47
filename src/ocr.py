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
        # Configuration optimisée pour le texte blanc de Star Citizen
        # PSM 6 = bloc de texte uniforme, OEM 3 = mode par défaut
        custom_config = r'--oem 3 --psm 6 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz:._- '
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
        
        # Nettoyage du texte et correction des erreurs courantes d'OCR
        text = self._correct_ocr_errors(text)
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        
        for line in lines:
            # Recherche de la ligne "Zone: SolarSystem_XXX"
            if "Zone:" in line and "SolarSystem" in line:
                # Extraction du nom du système (ex: "Zone: SolarSystem_Stanton" -> "Stanton")
                zone_match = re.search(r'Zone:\s*SolarSystem[_-]?(\w+)', line, re.IGNORECASE)
                if zone_match:
                    data["location"] = zone_match.group(1)
                else:
                    # Si pas de nom spécifique, on prend "SolarSystem"
                    data["location"] = "SolarSystem"
            
            # Recherche des coordonnées (format Pos: 123.4km 567.8km 910.1km)
            elif "Pos:" in line:
                # Regex plus tolérante pour gérer les espaces et variations
                coord_match = re.search(
                    r'Pos:\s*(-?\d+\.?\d*)\s*km\s*(-?\d+\.?\d*)\s*km\s*(-?\d+\.?\d*)\s*km', 
                    line, 
                    re.IGNORECASE
                )
                if coord_match:
                    try:
                        data["x"] = float(coord_match.group(1))
                        data["y"] = float(coord_match.group(2))
                        data["z"] = float(coord_match.group(3))
                    except ValueError:
                        pass  # Ignore les erreurs de conversion
                
        return data
    
    def _correct_ocr_errors(self, text):
        """Corrige les erreurs courantes de reconnaissance OCR"""
        # Corrections courantes : O->0, l->1 dans les contextes numériques
        corrections = {
            'Pos:': 'Pos:',  # S'assurer que Pos: est correct
            'Zone:': 'Zone:',  # S'assurer que Zone: est correct
            'SolarSystern': 'SolarSystem',  # Erreur courante m->n
            'So1arSystem': 'SolarSystem',  # l->1
        }
        
        for wrong, correct in corrections.items():
            text = text.replace(wrong, correct)
        
        return text

if __name__ == "__main__":
    # Test rapide si une image existe
    processor = OCRProcessor()
    img = cv2.imread("test_capture.png")
    if img is not None:
        result = processor.extract_data(img)
        print(f"Résultat OCR : {result}")
    else:
        print("Image test_capture.png non trouvée.")
