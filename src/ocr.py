"""OCR pipeline pour le HUD debug Star Citizen.

Pipeline :
  1. Capture (cf. capture.py) → 3 passes binaires
  2. Tesseract OEM3 PSM6 sur chaque passe (parallèle, ThreadPoolExecutor)
  3. Normalisation post-OCR (Pos:_, lkm/Km, underscores parasites)
  4. Regex Pos stricte 3-4 décimales
  5. Si la regex échoue : tentative de récupération du '.' manquant
  6. Validation par plage géographique (|coord| < 30000 km)
  7. Consensus multi-pass : si ≥2 passes convergent à ±0.1 km, moyenne ;
     sinon, meilleur score

Structure du HUD r_DisplayInfo 3 (3 lignes Pos:) :
  Ligne 1 : Zone: SolarSystem_XXXXX Pos: X Y Z  → frame absolue, rejetée
  Ligne 2 : Root Pos: X Y Z                     → frame absolue, rejetée
  Ligne 3 : {ZoneName} Pos: X Y Z               → frame relative, CIBLE

La 3ème ligne est systématiquement scannée sans filtre sur le préfixe du nom
de zone : OOC_Hurston, GrimHex, StantonIV-9, etc. sont tous acceptés.
Seuls Root/SolarSystem sont rejetés (frame absolue ~14 M km).

Phase D : Pipeline hybride NCC custom
  - Tesseract pour les noms (Zone:SolarSystem)
  - NCC custom pour les coordonnées numériques (si templates disponibles)
"""
import pytesseract
import re
import cv2
import os
import sys
import logging
from concurrent.futures import ThreadPoolExecutor
from sc_ocr.templates import TemplateLibrary
from sc_ocr.segment import find_glyph_regions
from sc_ocr.classify import classify_batch
import numpy as np

logger = logging.getLogger(__name__)

# ─── Regex ────────────────────────────────────────────────────────────

_RE_ZONE = re.compile(r'Zone:\s*SolarSystem[_-]?([\d\w]+?)(?:Pos|Zone|$|\s)', re.IGNORECASE)
_RE_POS = re.compile(
    # Exige 3-4 décimales : SC affiche toujours 4 décimales (10cm de précision).
    # Si Tesseract perd des chiffres, la lecture est rejetée plutôt que d'enregistrer
    # une position approximative qui causerait des erreurs de ~10m à l'arrivée.
    r'[Pp]os:?\s*(-?\d+\.\d{3,4})\s*km\s*(-?\d+\.\d{3,4})\s*km\s*(-?\d+\.\d{3,4})\s*km',
    re.IGNORECASE,
)
# Identifie une ligne CamDir même si l'OCR rate le ':' ou le 'C' initial.
_RE_CAMDIR_TAG = re.compile(r'amdir', re.IGNORECASE)
# Rejette les lignes Root/SolarSystem (frame absolue ~14 M km, inutilisable).
_RE_POS_SYSTEM_FRAME = re.compile(r'(r[o0e]{1,3}t|solar\s*system)', re.IGNORECASE)

# Extrait le nom de zone avant "Pos:" — accepte tout format (OOC_, GrimHex, etc.)
_RE_ZONE_NAME = re.compile(r'^(.*?)\s*[Pp]os:?\s*', re.IGNORECASE)

# Pour la récupération du '.' manquant : 7 ou 8 chiffres avant 'km'.
# Ex : '41335653km' (7 chiffres = 4 entiers + 3-4 décimales potentielles).
_RE_DIGITS_KM = re.compile(r'(-?)(\d{6,9})\s*km', re.IGNORECASE)


# ─── Validation géographique (Phase B) ────────────────────────────────

# Une coordonnée OOC plausible reste sous 30 000 km en absolu.
# Le système Stanton fait ~60 000 km de diamètre, et un POI sur une
# planète/lune/station est toujours dans ce rayon. Au-delà → hallucination
# OCR (typiquement la frame Root/SolarSystem qui s'est glissée).
_OOC_COORD_MAX_KM = 30_000.0


def _coords_in_range(x, y, z):
    """Vrai si (x, y, z) est dans la plage plausible OOC."""
    return (
        abs(x) < _OOC_COORD_MAX_KM
        and abs(y) < _OOC_COORD_MAX_KM
        and abs(z) < _OOC_COORD_MAX_KM
    )


# ─── Configuration Tesseract (Phase C) ────────────────────────────────

# `classify_bln_numeric_mode=1` force la normalisation numérique baseline,
# réduisant la confusion 5↔S, 0↔O, 1↔l sur les chiffres du HUD.
_TESSERACT_CONFIG_BASE = (
    r'--oem 3 --psm 6 '
    r'-c preserve_interword_spaces=1 '
    r'-c classify_bln_numeric_mode=1 '
    r'-c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz:._- '
)


def _build_tesseract_config():
    """Construit la config Tesseract avec user_words/patterns si présents.

    `data/user_words.txt` : un mot par ligne (Stanton, Hurston, OOC_X...).
    `data/user_patterns.txt` : un pattern par ligne (\n* pour digit, etc.).

    Tesseract pondère les hypothèses qui matchent ces dictionnaires/patterns,
    réduisant les hallucinations sur les noms connus.
    """
    config = _TESSERACT_CONFIG_BASE
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    user_words = os.path.join(base_dir, 'data', 'user_words.txt')
    user_patterns = os.path.join(base_dir, 'data', 'user_patterns.txt')
    if os.path.exists(user_words):
        config += f' -c user_words_file={user_words}'
        logger.debug(f"Tesseract user_words : {user_words}")
    if os.path.exists(user_patterns):
        config += f' -c user_patterns_file={user_patterns}'
        logger.debug(f"Tesseract user_patterns : {user_patterns}")
    return config


# ─── Parsing CamDir ───────────────────────────────────────────────────

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
    fov_idx = re.search(r'FOV', payload, re.IGNORECASE)
    if fov_idx:
        payload = payload[:fov_idx.start()]
    payload = re.sub(r'(\d)-', r'\1 -', payload)
    tokens = re.findall(r'-?\d+', payload)
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
            return None
    if len(out) >= 3:
        return out[:3]
    return None


# ─── Corrections post-OCR ─────────────────────────────────────────────

_OCR_CORRECTIONS = {
    'Zore:': 'Zone:',
    'SolarSystern': 'SolarSystem',
    'So1arSystem': 'SolarSystem',
    'SovarSysten': 'SolarSystem',
    'SolarSysten': 'SolarSystem',
    'Camdir': 'CamDir',
    'CarmDir': 'CamDir',
    'CarnDir': 'CamDir',
    'Cam0ir': 'CamDir',
    'CarnOir': 'CamDir',
    'RoetPos': 'RootPos',
    'Roet_Pos': 'Root_Pos',
    'R0ot': 'Root',
    'Rcot': 'Root',
}


def _normalize_ooc_line(line):
    """Normalise une ligne OOC avant d'appliquer _RE_POS.

    Corrige les artefacts Tesseract courants dans l'ordre :
    1. Pos:_ → Pos:  (underscore/espaces multiples après les deux-points)
    2. lkm/Ikm/kn/KM/kh → km  (variantes OCR de l'unité, après chiffre)
    3. km_-529 → km -529  (underscore entre coordonnées)
    """
    line = re.sub(r'(Pos:?)[\s_]+', r'\1 ', line)
    line = re.sub(r'(?<=[\d.])[lLiI1]?[kK][mMnNhH](?=[\s_\-\d]|$)', 'km', line)
    line = re.sub(r'km[\s_]+(-?\d)', r'km \1', line)
    return line


# ─── Récupération du '.' manquant (Phase B) ──────────────────────────

def _try_recover_decimal(digits_str, sign_str=''):
    """Tente d'insérer un '.' à différentes positions dans une suite de chiffres.

    SC affiche les coords avec exactement 4 décimales (ex: '4133.5653').
    Si Tesseract a fusionné le point ('41335653'), on tente d'insérer
    un '.' à la position qui donne une valeur dans la plage plausible.

    Args:
        digits_str: chaîne de chiffres uniquement (ex: '41335653').
        sign_str: '-' ou '' pour le signe.

    Returns:
        Float si une position donne une valeur dans la plage OOC, None sinon.
    """
    if len(digits_str) < 4:
        return None
    sign_factor = -1.0 if sign_str == '-' else 1.0
    # Position privilégiée : 4 décimales (format SC standard).
    # On tente d'abord cette position, puis 3 (cas où une décimale a été
    # perdue), puis on élargit.
    candidate_positions = []
    n = len(digits_str)
    for n_decimals in (4, 3, 2):
        pos = n - n_decimals
        if 1 <= pos < n:
            candidate_positions.append(pos)
    for pos in candidate_positions:
        with_dot = digits_str[:pos] + '.' + digits_str[pos:]
        try:
            val = sign_factor * float(with_dot)
        except ValueError:
            continue
        if abs(val) < _OOC_COORD_MAX_KM:
            return val
    return None


def _try_recover_pos_line(line):
    """Tente de récupérer (x, y, z) si _RE_POS échoue à cause d'un '.' manquant.

    Cherche 3 occurrences de `(\\d{6,9})km` dans la ligne et tente d'insérer
    un '.' à la position des 4 dernières chiffres pour chacune.
    """
    matches = list(_RE_DIGITS_KM.finditer(line))
    if len(matches) < 3:
        return None
    coords = []
    for m in matches[:3]:
        sign_str = m.group(1)
        digits = m.group(2)
        val = _try_recover_decimal(digits, sign_str)
        if val is None:
            return None
        coords.append(val)
    if len(coords) == 3 and _coords_in_range(*coords):
        return tuple(coords)
    return None


# ─── Consensus multi-pass (Phase B) ──────────────────────────────────

# Tolérance pour considérer que deux passes convergent.
_CONSENSUS_TOL_KM = 0.1


def _consensus_coords(pass_results):
    """Cherche un consensus entre les passes qui ont extrait des coordonnées.

    Args:
        pass_results: liste de tuples (pass_name, score, data) où data contient
            potentiellement (x, y, z) non None.

    Returns:
        Tuple (consensus_data, consensus_score) si ≥2 passes convergent
        à ±0.1 km, None sinon. Le consensus_data est la moyenne des passes
        qui convergent.
    """
    valid = [
        (name, score, data) for name, score, data in pass_results
        if data.get("x") is not None and _coords_in_range(data["x"], data["y"], data["z"])
    ]
    if len(valid) < 2:
        return None
    # Pour chaque passe valide, compte combien d'autres passes sont à ±tol.
    best_cluster = None
    best_size = 1
    for i, (_, _, ref) in enumerate(valid):
        cluster = [valid[i]]
        for j, (_, _, other) in enumerate(valid):
            if i == j:
                continue
            if (
                abs(other["x"] - ref["x"]) <= _CONSENSUS_TOL_KM
                and abs(other["y"] - ref["y"]) <= _CONSENSUS_TOL_KM
                and abs(other["z"] - ref["z"]) <= _CONSENSUS_TOL_KM
            ):
                cluster.append(valid[j])
        if len(cluster) > best_size:
            best_size = len(cluster)
            best_cluster = cluster
    if best_cluster is None or best_size < 2:
        return None
    # Moyenne sur le cluster
    n = len(best_cluster)
    avg_x = sum(d["x"] for _, _, d in best_cluster) / n
    avg_y = sum(d["y"] for _, _, d in best_cluster) / n
    avg_z = sum(d["z"] for _, _, d in best_cluster) / n
    # Prendre les autres champs (ooc, location, camdir) de la passe la plus haut score
    best = max(best_cluster, key=lambda t: t[1])
    consensus = dict(best[2])
    consensus["x"] = avg_x
    consensus["y"] = avg_y
    consensus["z"] = avg_z
    consensus_score = max(s for _, s, _ in best_cluster)
    return consensus, consensus_score


# ─── OCRProcessor ─────────────────────────────────────────────────────

class OCRProcessor:
    SYSTEM_ID_MAP = {
        "9948564368677": "Stanton",
        "Stanton": "Stanton",
    }

    def __init__(self, tesseract_path=None, engine="tesseract"):
        self.engine = engine.lower()
        self.paddle_ocr = None
        self.tesseract_config = _build_tesseract_config()

        if self.engine == "tesseract":
            self._init_tesseract(tesseract_path)
        elif self.engine == "paddle":
            self._init_paddle()
        else:
            logger.warning(f"Moteur OCR inconnu '{engine}', fallback vers Tesseract")
            self.engine = "tesseract"
            self._init_tesseract(tesseract_path)

        self._pool = ThreadPoolExecutor(max_workers=3)

        # Phase D : charger la bibliothèque de templates pour NCC
        self.template_lib = TemplateLibrary()
        if self.template_lib.has_templates():
            logger.info(f"Templates NCC chargés : {self.template_lib.stats()}")
        else:
            logger.info("Aucun template NCC trouvé, fallback vers Tesseract pour coordonnées")

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
            "ooc": None,
            "cam_pitch": None, "cam_roll": None, "cam_yaw": None,
        }
        score = 0

        for line in lines:
            if _RE_CAMDIR_TAG.search(line):
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
                    data["location"] = matched_name if matched_name else "Unknown"
                    logger.info(f"[{pass_name}] Système détecté : ID={system_id}, Nom={data['location']}")
                    score += 10
            elif "Pos:" in line or "pos:" in line.lower():
                # Rejeter Root/SolarSystem (frame absolue ~14 M km).
                if _RE_POS_SYSTEM_FRAME.search(line):
                    logger.debug(f"[{pass_name}] Ligne Root/SolarSystem rejetée : {line[:80]}")
                    continue

                # Extraire le nom de zone = tout ce qui précède "Pos:"
                zone_match = _RE_ZONE_NAME.match(line)
                zone_name = zone_match.group(1).strip() if zone_match else ""
                logger.debug(f"[{pass_name}] Ligne zone : zone={zone_name!r} | {line[:120]}")

                # Phase D : essayer NCC en premier (plus rapide + précis si templates dispo)
                coords = self._extract_coords_via_ncc(img, pass_name)

                # Fallback : Tesseract sur la ligne normalisée si NCC n'a pas marché
                if coords is None:
                    normalized = _normalize_ooc_line(line)
                    coords = self._extract_coords_from_line(normalized, pass_name)

                if coords is not None:
                    x, y, z = coords
                    # Validation de plage géographique (Phase B).
                    if not _coords_in_range(x, y, z):
                        logger.warning(
                            f"[{pass_name}] Coords hors plage : "
                            f"X={x} Y={y} Z={z} (max ±{_OOC_COORD_MAX_KM} km)"
                        )
                        continue
                    data["x"], data["y"], data["z"] = x, y, z
                    data["ooc"] = zone_name or "Unknown"
                    logger.info(
                        f"[{pass_name}] Position extraite : zone={zone_name!r} "
                        f"X={x} Y={y} Z={z}"
                    )
                    score += 10

        if any(v is not None for v in (data["x"], data["y"], data["z"])):
            score += 5
        if "km" in ocr_text.lower():
            score += 5

        return score, data

    def _extract_coords_via_ncc(self, binary_image, pass_name):
        """Extrait les coordonnées via NCC custom (Phase D).

        Stratégie ligne par ligne :
          1. Segmenter en bandes de texte (rows)
          2. Pour chaque row, classifier les glyphes via NCC
          3. Reconstruire la chaîne et appliquer la regex Pos
          4. Validation plage géographique (rejette implicitement Root/SolarSystem
             qui ont des coords ~14 M km)
          5. Retourner la première row qui donne des coords valides

        Args:
            binary_image : image binaire 0/255
            pass_name : nom de la passe (pour logs)

        Returns:
            tuple (x, y, z) ou None
        """
        if not self.template_lib.has_templates():
            return None

        try:
            seg_result = find_glyph_regions(binary_image)
            glyphs = seg_result['glyphs']
            if not glyphs:
                return None

            # Grouper les glyphs par row
            glyphs_by_row = {}
            for g in glyphs:
                glyphs_by_row.setdefault(g['row_idx'], []).append(g)

            # Traiter chaque row dans l'ordre, retourner la première avec coords valides
            for row_idx in sorted(glyphs_by_row.keys()):
                coords = self._extract_coords_from_row_ncc(
                    binary_image, glyphs_by_row[row_idx], row_idx, pass_name
                )
                if coords is not None and _coords_in_range(*coords):
                    return coords

            return None

        except Exception as e:
            logger.error(f"[{pass_name}] Erreur NCC : {e}")
            return None

    def _extract_coords_from_row_ncc(self, binary_image, row_glyphs, row_idx, pass_name):
        """Classifie les glyphs d'une row et tente d'extraire (x, y, z)."""
        row_glyphs = sorted(row_glyphs, key=lambda g: g['x'])

        # Préparer les crops dans l'ordre x
        glyph_images = []
        for g in row_glyphs:
            x, y, w, h = g['x'], g['y'], g['w'], g['h']
            crop = binary_image[y:y+h, x:x+w]
            if crop.size > 0:
                glyph_images.append((g['id'], crop))

        if not glyph_images:
            return None

        classifications = classify_batch(
            glyph_images, self.template_lib, glyphs_meta=row_glyphs
        )

        # Reconstruire la chaîne avec un espace si gap horizontal > 6 px
        char_by_id = {c['glyph_id']: c['char'] for c in classifications}
        chars = []
        prev_x_end = None
        for g in row_glyphs:
            c = char_by_id.get(g['id'])
            if c is None:
                c = '?'
            if prev_x_end is not None and (g['x'] - prev_x_end) > 6:
                chars.append(' ')
            chars.append(c)
            prev_x_end = g['x'] + g['w']

        reconstructed = ''.join(chars)
        if not reconstructed.strip():
            return None

        logger.debug(f"[{pass_name}] NCC row{row_idx}: {reconstructed!r}")

        # Préfixer "Pos: " virtuellement pour la regex (NCC ne lit pas les lettres)
        candidate = "Pos: " + reconstructed
        normalized = _normalize_ooc_line(candidate)

        # Match strict uniquement : NCC + heuristique '.' doivent reconstruire
        # le point décimal correctement. Si la regex stricte échoue, c'est
        # probablement une frame absolue (Root/SolarSystem ~14 M km sans '.')
        # → on saute cette row au lieu de risquer un faux match via recovery.
        coord_match = _RE_POS.search(normalized)
        if coord_match:
            try:
                return (
                    float(coord_match.group(1)),
                    float(coord_match.group(2)),
                    float(coord_match.group(3)),
                )
            except ValueError:
                pass

        return None

    def _extract_coords_from_line(self, normalized_line, pass_name):
        """Extrait (x, y, z) depuis une ligne OOC normalisée.

        Tente d'abord la regex stricte (4 décimales), puis la récupération
        du '.' manquant si elle échoue. Retourne None si aucune méthode
        ne donne une triplet valide.
        """
        coord_match = _RE_POS.search(normalized_line)
        if coord_match:
            try:
                return (
                    float(coord_match.group(1)),
                    float(coord_match.group(2)),
                    float(coord_match.group(3)),
                )
            except ValueError as e:
                logger.error(f"[{pass_name}] Erreur conversion coordonnées : {e}")
                return None
        # Tentative de récupération du '.' manquant (Phase B).
        recovered = _try_recover_pos_line(normalized_line)
        if recovered is not None:
            logger.info(
                f"[{pass_name}] Position récupérée via insertion '.' : "
                f"X={recovered[0]} Y={recovered[1]} Z={recovered[2]}"
            )
            return recovered
        logger.warning(f"[{pass_name}] Regex Pos non matchée : {normalized_line[:120]!r}")
        return None

    def _parse_images_parallel(self, images):
        """Lance l'OCR en parallèle, applique le consensus multi-pass.

        Stratégie :
          1. Toutes les passes tournent en parallèle.
          2. On collecte tous les résultats (pass_name, score, data).
          3. Si ≥2 passes convergent à ±0.1 km → on utilise la moyenne.
          4. Sinon → on prend la passe au meilleur score (comportement
             antérieur).
        """
        futures = {
            self._pool.submit(self._ocr_single_pass, pass_name, img): pass_name
            for pass_name, img in images.items()
        }
        pass_results = []
        for future in futures:
            pass_name = futures[future]
            score, data = future.result()
            pass_results.append((pass_name, score, data))

        # Tentative de consensus multi-pass (Phase B).
        consensus = _consensus_coords(pass_results)
        if consensus is not None:
            consensus_data, _ = consensus
            logger.debug(
                f"Consensus multi-pass : X={consensus_data['x']:.4f} "
                f"Y={consensus_data['y']:.4f} Z={consensus_data['z']:.4f}"
            )
            return consensus_data

        # Fallback : meilleur score.
        best_data = None
        best_score = -1
        for _, score, data in pass_results:
            if score > best_score:
                best_score = score
                best_data = data
        if best_data is None:
            best_data = {
                "location": "Unknown",
                "x": None, "y": None, "z": None,
                "ooc": None,
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
