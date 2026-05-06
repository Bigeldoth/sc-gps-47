import pytesseract
import re
import cv2
import os
import sys
import logging

logger = logging.getLogger(__name__)

class OCRProcessor:
    # Mapping des IDs système vers les noms de systèmes connus
    SYSTEM_ID_MAP = {
        "9948564368677": "Stanton",
        "Stanton": "Stanton",
        # Ajoutez d'autres mappings ici si nécessaire
    }
    
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
                logger.debug(f"Recherche Tesseract dans : {path}")
                if os.path.exists(path):
                    pytesseract.pytesseract.tesseract_cmd = path
                    logger.info(f"Tesseract trouvé : {path}")
                    found = True
                    break
            
            if not found:
                logger.error("ERREUR: Tesseract-OCR non trouvé dans les emplacements standards!")
                logger.error("Chemins testés: " + ", ".join(possible_paths))
        else:
            pytesseract.pytesseract.tesseract_cmd = tesseract_path
            
    def extract_data(self, images):
        """Extrait les coordonnées et le lieu à partir d'un dictionnaire d'images pré‑traitées"""
        # images est un dict contenant les passes (pass1…pass4)
        # On conserve la configuration Tesseract dans self.tesseract_config pour la réutiliser
        self.tesseract_config = r'--oem 3 --psm 6 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz:._- '
        logger.debug(f"Extraction OCR à partir de {len(images)} passes")
        
        # Passer le dict complet à parse_text qui gérera le scoring
        return self.parse_text(images)

    def parse_text(self, text):
        """Analyse le texte brut pour trouver X, Y, Z et le lieu"""
        # Cette fonction a été adaptée pour gérer plusieurs images pré‑traitées.
        # Elle reçoit un dictionnaire d'images (pass1…pass4) et retourne les données
        # de la meilleure passe selon un score simple.
        best_data = None
        best_score = -1
        
        for pass_name, img in text.items():
            # Convertir l'image en texte OCR
            ocr_text = pytesseract.image_to_string(img, config=self.tesseract_config)
            ocr_text = self._correct_ocr_errors(ocr_text)
            lines = [line.strip() for line in ocr_text.split('\n') if line.strip()]
            data = {"location": "Unknown", "x": None, "y": None, "z": None}
            score = 0
            
            for line in lines:
                if "Zone:" in line and "SolarSystem" in line:
                    logger.debug(f"[{pass_name}] Ligne Zone détectée : {line}")
                    zone_match = re.search(r'Zone:\s*SolarSystem[_-]?([\d\w]+?)(?:Pos|Zone|$|\s)', line, re.IGNORECASE)
                    if zone_match:
                        system_id = zone_match.group(1).strip()
                        matched_name = None
                        for known_id, name in self.SYSTEM_ID_MAP.items():
                            if known_id in system_id or system_id in known_id:
                                matched_name = name
                                break
                        data["location"] = matched_name if matched_name else system_id
                        logger.info(f"[{pass_name}] Système détecté : ID={system_id}, Nom={data['location']}")
                        score += 10
                elif "Pos:" in line or "pos:" in line.lower():
                    logger.debug(f"[{pass_name}] Ligne Pos détectée : {line}")
                    coord_match = re.search(
                     r'[Pp]os:?\s*(-?\d+\.?\d*)[_\s]*[kKaA][mnMN]?[_\s]*(-?\d+\.?\d*)[_\s]*[kKaA][mnMN]?[_\s]*(-?\d+\.?\d*)[_\s]*[kKaA][mnMN]?', 
                     line, re.IGNORECASE)
                    if coord_match:
                        try:
                            data["x"] = float(coord_match.group(1))
                            data["y"] = float(coord_match.group(2))
                            data["z"] = float(coord_match.group(3))
                            logger.info(f"[{pass_name}] Coordonnées extraites : X={data['x']}, Y={data['y']}, Z={data['z']}")
                            score += 10
                        except ValueError as e:
                            logger.error(f"[{pass_name}] Erreur conversion coordonnées : {e}")
                    else:
                        logger.warning(f"[{pass_name}] Ligne Pos détectée mais regex non matchée : {line}")
            # Bonus si on a trouvé au moins un chiffre ou le suffixe km
            if any(v is not None for v in (data["x"], data["y"], data["z"])):
                score += 5
            if "km" in ocr_text.lower():
                score += 5
            
            if score > best_score:
                best_score = score
                best_data = data
        
        if best_data is None:
            best_data = {"location": "Unknown", "x": None, "y": None, "z": None}
        return best_data
    
    def _correct_ocr_errors(self, text):
        """Corrige les erreurs courantes de reconnaissance OCR"""
        # Corrections courantes : O->0, l->1 dans les contextes numériques
        corrections = {
            'Zore:': 'Zone:',  # Erreur courante Z->o
            'Pos:': 'Pos:',  # S'assurer que Pos: est correct
            'Zone:': 'Zone:',  # S'assurer que Zone: est correct
            'SolarSystern': 'SolarSystem',  # Erreur courante m->n
            'So1arSystem': 'SolarSystem',  # l->1
            'SovarSysten': 'SolarSystem',  # Erreur courante l->v, m->n
            'SolarSysten': 'SolarSystem',  # Erreur courante m->n
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
