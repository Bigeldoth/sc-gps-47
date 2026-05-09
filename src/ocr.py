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
# Identifie une ligne CamDir même si l'OCR rate le ':' ou le 'C' initial.
_RE_CAMDIR_TAG = re.compile(r'amdir', re.IGNORECASE)
# Lignes Pos: que l'on accepte (frame système). Rejette ObjectContainer, OOC, Habs.
_RE_POS_SYSTEM_FRAME = re.compile(r'(root|solar\s*system)', re.IGNORECASE)

_TESSERACT_CONFIG = (
    r'--oem 3 --psm 6 '
    r'-c preserve_interword_spaces=1 '
    r'-c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz:._- '
)


def _parse_camdir_values(line, max_abs=180):
    """Extrait (pitch, roll, yaw) d'une ligne CamDir, gère la perte d'espaces.

    Stratégie :
      1. Isole le payload après 'amdir' jusqu'à 'FOV' (ou fin de ligne).
      2. Insère un espace avant tout '-' qui suit un chiffre, pour séparer
         les valeurs négatives consécutives ('25-5177' → '25 -5177').
      3. Extrait les tokens via re.findall(r'-?\\d+').
      4. Pour chaque token dont |valeur| > max_abs, scinde gourmandement
         depuis la gauche : on coupe au préfixe le plus court qui reste
         dans [-max_abs, +max_abs] et dont le reste l'est aussi.
         Ex: '-5177' → ['-5', '177'].
      5. Retourne la liste des 3 premiers ints valides ou None.
    """
    if not line:
        return None
    m = _RE_CAMDIR_TAG.search(line)
    if not m:
        return None
    payload = line[m.end():]
    # Coupe à FOV si présent
    fov_idx = re.search(r'FOV', payload, re.IGNORECASE)
    if fov_idx:
        payload = payload[:fov_idx.start()]
    # Étape 2 : sépare un '-' qui colle à un chiffre précédent
    payload = re.sub(r'(\d)-', r'\1 -', payload)
    # Étape 3 : tokens signés
    tokens = re.findall(r'-?\d+', payload)
    # Étape 4 : split des tokens hors plage
    out = []
    for tok in tokens:
        if len(out) >= 3:
            break
        try:
            n = int(tok)
        except ValueError:
            continue
        if -max_abs <= n <= max_abs:
            out.append(n)
            continue
        # Hors plage : tente un split par la gauche
        sign = -1 if tok.startswith('-') else 1
        digits = tok.lstrip('-')
        split_found = False
        for i in range(1, len(digits)):
            head = sign * int(digits[:i])
            tail = digits[i:]
            if not tail:
                continue
            try:
                tail_n = int(tail)
            except ValueError:
                continue
            if -max_abs <= head <= max_abs and -max_abs <= tail_n <= max_abs:
                out.append(head)
                if len(out) < 3:
                    out.append(tail_n)
                split_found = True
                break
        if not split_found:
            # Token irrécupérable, on abandonne ce parse
            return None
    if len(out) >= 3:
        return out[:3]
    return None

_OCR_CORRECTIONS = {
    'Zore:': 'Zone:',
    'SolarSystern': 'SolarSystem',
    'So1arSystem': 'SolarSystem',
    'SovarSysten': 'SolarSystem',
    'SolarSysten': 'SolarSystem',
    # Variations courantes de "CamDir" causées par l'OCR
    'Camdir': 'CamDir',
    'CarmDir': 'CamDir',
    'CarnDir': 'CamDir',
    'Cam0ir': 'CamDir',
    'CarnOir': 'CamDir',
}


class OCRProcessor:
    SYSTEM_ID_MAP = {
        "9948564368677": "Stanton",
        "Stanton": "Stanton",
    }

    def __init__(self, tesseract_path=None, engine="tesseract"):
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

        self._pool = ThreadPoolExecutor(max_workers=2)

    def _init_tesseract(self, tesseract_path=None):
        if not tesseract_path:
            base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
            possible_paths = []

            if getattr(sys, 'frozen', False):
                possible_paths.append(os.path.join(os.path.dirname(sys.executable), "tesseract", "tesseract.exe"))
            else:
                possible_paths.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tesseract", "tesseract.exe"))

            if sys.platform == "win32":
                possible_paths.append(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
                possible_paths.append(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")
                user_local = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Tesseract-OCR', 'tesseract.exe')
                possible_paths.append(user_local)
            else:
                possible_paths.append("/opt/homebrew/bin/tesseract")
                possible_paths.append("/usr/local/bin/tesseract")
                possible_paths.append("/usr/bin/tesseract")

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
        try:
            from paddleocr import PaddleOCR

            self.paddle_ocr = PaddleOCR(
                use_angle_cls=True,
                lang='en',
                show_log=False,
                use_gpu=False
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
        if self.engine == "paddle" and self.paddle_ocr is not None:
            try:
                result = self.paddle_ocr.ocr(img, cls=True)
                text_lines = []
                if result and result[0]:
                    for line in result[0]:
                        if line and len(line) >= 2:
                            text_lines.append(line[1][0])
                return '\n'.join(text_lines)
            except Exception as e:
                logger.error(f"Erreur PaddleOCR : {e}, fallback vers Tesseract pour cette image")
                return pytesseract.image_to_string(img, config=self.tesseract_config)
        else:
            return pytesseract.image_to_string(img, config=self.tesseract_config)

    def extract_data(self, images):
        logger.debug(f"Extraction OCR à partir de {len(images)} passes avec moteur {self.engine}")
        return self._parse_images_parallel(images)

    def _ocr_single_pass(self, pass_name, img):
        ocr_text = self._ocr_image_to_text(img)
        ocr_text = self._correct_ocr_errors(ocr_text)
        lines = [line.strip() for line in ocr_text.split('\n') if line.strip()]

        data = {
            "location": "Unknown",
            "x": None, "y": None, "z": None,
            "cam_pitch": None, "cam_roll": None, "cam_yaw": None,
        }
        score = 0

        for line in lines:
            if _RE_CAMDIR_TAG.search(line):
                # Parser dédié qui gère la perte d'espaces entre valeurs.
                values = _parse_camdir_values(line)
                if values is not None:
                    pitch, roll, yaw = values
                    data["cam_pitch"] = float(pitch)
                    data["cam_roll"] = float(roll)
                    data["cam_yaw"] = float(yaw)
                    logger.debug(
                        f"[{pass_name}] CamDir extrait : pitch={pitch} roll={roll} yaw={yaw}"
                    )
                    score += 5
                else:
                    logger.warning(f"[{pass_name}] Ligne CamDir non parsable : {line!r}")
            elif "Zone:" in line and "SolarSystem" in line:
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
                # Filtre : on ne veut QUE le repère système (Zone:Root ou
                # Zone:SolarSystem). Rejette ObjectContainer, OOC_Stanton,
                # Habs etc. qui sont des sous-conteneurs en frame locale et
                # provoquent des sauts de distance énormes (4133 km vs 14M km).
                if not _RE_POS_SYSTEM_FRAME.search(line):
                    logger.debug(f"[{pass_name}] Pos ignoré (frame locale) : {line[:80]}")
                    continue
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
            best_data = {
                "location": "Unknown",
                "x": None, "y": None, "z": None,
                "cam_pitch": None, "cam_roll": None, "cam_yaw": None,
            }
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
