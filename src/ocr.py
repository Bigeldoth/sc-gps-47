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
            
    def extract_data(self, image):
        """Extrait les coordonnées et le lieu de l'image traitée"""
        # Configuration optimisée pour le texte blanc de Star Citizen
        # PSM 6 = bloc de texte uniforme, OEM 3 = mode par défaut
        custom_config = r'--oem 3 --psm 6 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz:._- '
        text = pytesseract.image_to_string(image, config=custom_config)
        
        logger.debug(f"Texte OCR brut : {text[:200]}...")  # Log les 200 premiers caractères
        
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
                logger.debug(f"Ligne Zone détectée : {line}")
                # Extraction du nom du système (ex: "Zone: SolarSystem_Stanton" -> "Stanton")
                # S'arrête avant "Pos:" ou autres mots-clés
                zone_match = re.search(r'Zone:\s*SolarSystem[_-]?([\d\w]+?)(?:Pos|Zone|$|\s)', line, re.IGNORECASE)
                if zone_match:
                    system_id = zone_match.group(1).strip()
                    
                    # Matching intelligent : cherche si l'ID contient un ID connu
                    matched_name = None
                    for known_id, name in self.SYSTEM_ID_MAP.items():
                        if known_id in system_id or system_id in known_id:
                            matched_name = name
                            break
                    
                    data["location"] = matched_name if matched_name else system_id
                    logger.info(f"Système détecté : ID={system_id}, Nom={data['location']}")
                else:
                    # Si pas de nom spécifique, on prend "SolarSystem"
                    data["location"] = "SolarSystem"
                    logger.warning("Zone SolarSystem détectée mais pas d'ID extrait")
            
            # Recherche des coordonnées (format Pos: 123.4km 567.8km 910.1km)
            elif "Pos:" in line or "pos:" in line.lower():
                logger.debug(f"Ligne Pos détectée : {line}")
                # Regex TRÈS tolérante pour gérer toutes les erreurs OCR
                # Accepte : espaces, underscores, km/kn/k/an, pas d'espace après Pos:
                coord_match = re.search(
                    r'[Pp]os:?\s*(-?\d+\.?\d*)[_\s]*[kKaA][mnMN]?[_\s]*(-?\d+\.?\d*)[_\s]*[kKaA][mnMN]?[_\s-]*(-?\d+\.?\d*)[_\s]*[kKaA][mnMN]?', 
                    line, 
                    re.IGNORECASE
                )
                if coord_match:
                    try:
                        data["x"] = float(coord_match.group(1))
                        data["y"] = float(coord_match.group(2))
                        data["z"] = float(coord_match.group(3))
                        logger.info(f"Coordonnées extraites : X={data['x']}, Y={data['y']}, Z={data['z']}")
                    except ValueError as e:
                        logger.error(f"Erreur conversion coordonnées : {e}")
                else:
                    logger.warning(f"Ligne Pos détectée mais regex non matchée : {line}")
                
        return data
    
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
