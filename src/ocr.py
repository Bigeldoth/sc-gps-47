import pytesseract
import re
import cv2
import os
import sys
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_RE_ZONE = re.compile(r'Zone:\s*SolarSystem[_-]?([\d\w]+?)(?:Pos|Zone|$|\s)', re.IGNORECASE)
_RE_POS = re.compile(
    r'[Pp]os:?\s*(-?\d+\.?\d*)[_\s]*[kKaA][mnMN]?[_\s]*(-?\d+\.?\d*)[_\s]*[kKaA][mnMN]?[_\s]*(-?\d+\.?\d*)[_\s]*[kKaA][mnMN]?',
    re.IGNORECASE,
)

_TESSERACT_CONFIG = r'--oem 3 --psm 6 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz:._- '

_OCR_CORRECTIONS = {
    'Zore:': 'Zone:',
    'SolarSystern': 'SolarSystem',
    'So1arSystem': 'SolarSystem',
    'SovarSysten': 'SolarSystem',
    'SolarSysten': 'SolarSystem',
}


class OCRProcessor:
    """
    Processeur OCR avec support de plusieurs moteurs (Tesseract, PaddleOCR).
    Utilise un système de scoring pour choisir la meilleure passe d'image.
    """
    
    # Mapping des IDs système vers les noms de systèmes connus
    SYSTEM_ID_MAP = {
        "9948564368677": "Stanton",
        "Stanton": "Stanton",
    }
    
    def __init__(self, tesseract_path=None, engine="tesseract"):
        """
        Initialise le processeur OCR.
        
        Args:
            tesseract_path: Chemin vers l'exécutable Tesseract (optionnel)
            engine: Moteur OCR à utiliser ("tesseract" ou "paddle")
        """
        self.engine = engine.lower()
        self.paddle_ocr = None
        self.tesseract_config = _TESSERACT_CONFIG
        
        if self.engine == "tesseract":
            self._init_tesseract(tesseract_path)
        elif self.engine == "paddle":
            self._init_paddle()
        else:
            logger.warning(f"Moteur OCR inconnu '{engine}', fallback vers Tesseract")
            self.engine = "tesseract"
            self._init_tesseract(tesseract_path)
        
        self._pool = ThreadPoolExecutor(max_workers=4)
    
    def _init_tesseract(self, tesseract_path=None):
        """Initialise Tesseract OCR"""
        if not tesseract_path:
            base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
            possible_paths = []

            if getattr(sys, 'frozen', False):
                possible_paths.append(os.path.join(os.path.dirname(sys.executable), "tesseract", "tesseract.exe"))
            else:
                possible_paths.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tesseract", "tesseract.exe"))

            possible_paths.append(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
            possible_paths.append(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")

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
        
        logger.info("Tesseract OCR initialisé")
    
    def _init_paddle(self):
        """Initialise PaddleOCR"""
        try:
            from paddleocr import PaddleOCR
            
            # Initialiser PaddleOCR avec le modèle léger
            # use_angle_cls=True pour la détection d'orientation
            # lang='en' pour l'anglais (meilleur pour les chiffres)
            self.paddle_ocr = PaddleOCR(
                use_angle_cls=True,
                lang='en',
                show_log=False,
                use_gpu=False  # Mettre True si GPU disponible
            )
            logger.info("PaddleOCR initialisé avec succès")
        except ImportError:
            logger.error("PaddleOCR n'est pas installé. Installez-le avec: pip install paddleocr paddlepaddle")
            logger.warning("Fallback vers Tesseract")
            self.engine = "tesseract"
            self._init_tesseract(None)
        except Exception as e:
            logger.error(f"Erreur lors de l'initialisation de PaddleOCR : {e}")
            logger.warning("Fallback vers Tesseract")
            self.engine = "tesseract"
            self._init_tesseract(None)
    
    def _ocr_image_to_text(self, img):
        """
        Convertit une image en texte selon le moteur OCR configuré.
        
        Args:
            img: Image OpenCV (numpy array)
        
        Returns:
            str: Texte extrait
        """
        if self.engine == "paddle" and self.paddle_ocr is not None:
            try:
                # PaddleOCR retourne une liste de résultats
                result = self.paddle_ocr.ocr(img, cls=True)
                
                # Extraire le texte de tous les résultats
                text_lines = []
                if result and result[0]:
                    for line in result[0]:
                        if line and len(line) >= 2:
                            text_lines.append(line[1][0])  # line[1][0] contient le texte
                
                return '\n'.join(text_lines)
            except Exception as e:
                logger.error(f"Erreur PaddleOCR : {e}, fallback vers Tesseract pour cette image")
                return pytesseract.image_to_string(img, config=self.tesseract_config)
        else:
            # Utiliser Tesseract
            return pytesseract.image_to_string(img, config=self.tesseract_config)

    def extract_data(self, images):
        """Extrait les coordonnées et le lieu à partir d'un dictionnaire d'images pré-traitées"""
        logger.debug(f"Extraction OCR à partir de {len(images)} passes avec moteur {self.engine}")
        return self._parse_images_parallel(images)

    def _ocr_single_pass(self, pass_name, img):
        ocr_text = self._ocr_image_to_text(img)
        ocr_text = self._correct_ocr_errors(ocr_text)
        lines = [line.strip() for line in ocr_text.split('\n') if line.strip()]

        data = {"location": "Unknown", "x": None, "y": None, "z": None}
        score = 0

        for line in lines:
            if "Zone:" in line and "SolarSystem" in line:
                logger.debug(f"[{pass_name}] Ligne Zone détectée : {line}")
                zone_match = _RE_ZONE.search(line)
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
                coord_match = _RE_POS.search(line)
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

        if any(v is not None for v in (data["x"], data["y"], data["z"])):
            score += 5
        if "km" in ocr_text.lower():
            score += 5

        return score, data

    def _parse_images_parallel(self, images):
        futures = {
            self._pool.submit(self._ocr_single_pass, pass_name, img): pass_name
            for pass_name, img in images.items()
        }

        best_data = None
        best_score = -1

        for future in futures:
            score, data = future.result()
            if score > best_score:
                best_score = score
                best_data = data

        if best_data is None:
            best_data = {"location": "Unknown", "x": None, "y": None, "z": None}
        return best_data

    def _correct_ocr_errors(self, text):
        for wrong, correct in _OCR_CORRECTIONS.items():
            text = text.replace(wrong, correct)
        return text

    def shutdown(self):
        self._pool.shutdown(wait=False)


if __name__ == "__main__":
    processor = OCRProcessor()
    img = cv2.imread("test_capture.png")
    if img is not None:
        result = processor.extract_data({"test": img})
        print(f"Résultat OCR : {result}")
    else:
        print("Image test_capture.png non trouvée.")
