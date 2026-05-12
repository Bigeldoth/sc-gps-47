"""OCR pipeline for the Star Citizen HUD debug overlay.

Pipeline:
  1. Capture (see capture.py) → 3 binary passes
  2. Tesseract OEM3 PSM6 on each pass (parallel, ThreadPoolExecutor)
  3. Post-OCR normalization (Pos:_, lkm/Km, stray underscores)
  4. Strict Pos regex with 3-4 decimal places
  5. If the regex fails: attempt to recover the missing '.'
  6. Geographic range validation (|coord| < 30000 km)
  7. Multi-pass consensus: if ≥2 passes converge within ±0.1 km, average;
     otherwise, best score

HUD r_DisplayInfo 3 structure (3 Pos: lines):
  Line 1: Zone: SolarSystem_XXXXX Pos: X Y Z  → absolute frame, rejected
  Line 2: Root Pos: X Y Z                     → absolute frame, rejected
  Line 3: {ZoneName} Pos: X Y Z               → relative frame, TARGET

The 3rd line is always scanned without filtering on the zone name prefix:
OOC_Hurston, GrimHex, StantonIV-9, etc. are all accepted.
Only Root/SolarSystem are rejected (absolute frame ~14 M km).

Phase D: Hybrid NCC custom pipeline
  - Tesseract for names (Zone:SolarSystem)
  - Custom NCC for numeric coordinates (if templates available)
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
    # Require 3-4 decimal places: SC always displays 4 decimals (10 cm precision).
    # If Tesseract drops digits, the reading is rejected rather than recording
    # an approximate position that would cause ~10 m errors at destination.
    r'[Pp]os:?\s*(-?\d+\.\d{3,4})\s*km\s*(-?\d+\.\d{3,4})\s*km\s*(-?\d+\.\d{3,4})\s*km',
    re.IGNORECASE,
)
# Identifies a CamDir line even if OCR misses the ':' or leading 'C'.
_RE_CAMDIR_TAG = re.compile(r'amdir', re.IGNORECASE)
# Rejects Root/SolarSystem lines (absolute frame ~14 M km, unusable).
_RE_POS_SYSTEM_FRAME = re.compile(r'(r[o0e]{1,3}t|solar\s*system)', re.IGNORECASE)

# Extracts the zone name before "Pos:" — accepts any format (OOC_, GrimHex, etc.)
_RE_ZONE_NAME = re.compile(r'^(.*?)\s*[Pp]os:?\s*', re.IGNORECASE)

# For missing '.' recovery: 7 or 8 digits before 'km'.
# e.g. '41335653km' (7 digits = 4 integer + 3-4 potential decimals).
_RE_DIGITS_KM = re.compile(r'(-?)(\d{6,9})\s*km', re.IGNORECASE)


# ─── Geographic validation (Phase B) ────────────────────────────────────

# A plausible OOC coordinate stays below 30 000 km in absolute value.
# The Stanton system is ~60 000 km in diameter, and any POI on a
# planet/moon/station is always within that radius. Beyond → OCR hallucination
# (typically the Root/SolarSystem frame that slipped through).
_OOC_COORD_MAX_KM = 30_000.0


def _coords_in_range(x, y, z):
    """True if (x, y, z) is within the plausible OOC range."""
    return (
        abs(x) < _OOC_COORD_MAX_KM
        and abs(y) < _OOC_COORD_MAX_KM
        and abs(z) < _OOC_COORD_MAX_KM
    )


# ─── Tesseract configuration (Phase C) ────────────────────────────────

# `classify_bln_numeric_mode=1` forces numeric baseline normalization,
# reducing 5↔S, 0↔O, 1↔l confusion on HUD digits.
_TESSERACT_CONFIG_BASE = (
    r'--oem 3 --psm 6 '
    r'-c preserve_interword_spaces=1 '
    r'-c classify_bln_numeric_mode=1 '
    r'-c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz:._- '
)


def _build_tesseract_config():
    """Builds the Tesseract config with user_words/patterns if present.

    `data/user_words.txt`: one word per line (Stanton, Hurston, OOC_X...).
    `data/user_patterns.txt`: one pattern per line (\\n* for digit, etc.).

    Tesseract weights hypotheses that match these dictionaries/patterns,
    reducing hallucinations on known names.
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
        logger.debug(f"Tesseract user_words: {user_words}")
    if os.path.exists(user_patterns):
        config += f' -c user_patterns_file={user_patterns}'
        logger.debug(f"Tesseract user_patterns: {user_patterns}")
    return config


# ─── CamDir parsing ───────────────────────────────────────────────────

def _parse_camdir_values(line, max_abs=180):
    """Extracts (pitch, roll, yaw) from a CamDir line, handles missing spaces.

    Strategy:
      1. Isolate the payload after 'amdir' up to 'FOV' (or end of line).
      2. Insert a space before any '-' that follows a digit, to separate
         consecutive negative values ('25-5177' → '25 -5177').
      3. Extract tokens via re.findall(r'-?\\d+').
      4. For each token whose |value| > max_abs, greedily split from the
         left: cut at the shortest prefix that stays within [-max_abs, +max_abs]
         and whose remainder also does.
         e.g. '-5177' → ['-5', '177'].
      5. Return the list of the first 3 valid ints or None.
    """
    if not line:
        return None
    m = _RE_CAMDIR_TAG.search(line)
    if not m:
        return None
    payload = line[m.end():]
    # Fuzzy detection of the FOV delimiter: Tesseract often corrupts "FOV" as
    # "gfOV", "SOV", "FGV", "FQV", "F0V"... Look for a 2-3 letter token
    # containing at least 'O' or '0' preceded by a character ≈ 'F'.
    fov_idx = re.search(r'[FfGgSs][oO0O][vVbB]', payload)
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


# ─── Post-OCR corrections ─────────────────────────────────────────────

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
    """Normalizes an OOC line before applying _RE_POS.

    Fixes common Tesseract artifacts:
    1. Missing space before Pos: (zone glued: 'OOC_L2Pos:' → 'OOC_L2 Pos:')
    2. Pos:_ → Pos:  (underscore/multiple spaces after the colon)
    3. lkm/Ikm/kn/KM/kh → km  (OCR variants of the unit, after a digit)
    4. km_-529 → km -529  (underscore between coordinates)
    """
    # Insert a space before Pos: if it is glued to a non-space character.
    line = re.sub(r'(?<=[^\s])([Pp]os:)', r' \1', line)
    line = re.sub(r'(Pos:?)[\s_]+', r'\1 ', line)
    line = re.sub(r'(?<=[\d.])[lLiI1]?[kK][mMnNhH](?=[\s_\-\d]|$)', 'km', line)
    line = re.sub(r'km[\s_]+(-?\d)', r'km \1', line)
    return line


def _is_meter_line(line: str) -> bool:
    """True if the line expresses coordinates in meters (not km).

    Sub-zone lines (PlayerContainer, HabPos...) use 'm' as the unit.
    They are rejected: they do not correspond to navigable OOC coordinates.
    """
    return bool(re.search(r'\d+\.\d+\s*m\b(?!\s*k)', line, re.IGNORECASE))


# ─── Missing '.' recovery (Phase B) ──────────────────────────────────

def _try_recover_decimal(digits_str, sign_str=''):
    """Attempts to insert a '.' at various positions in a digit string.

    SC displays coords with exactly 4 decimal places (e.g. '4133.5653').
    If Tesseract merged the dot ('41335653'), we try to insert a '.'
    at a position that gives a value within the plausible range.

    Args:
        digits_str: digit-only string (e.g. '41335653').
        sign_str: '-' or '' for the sign.

    Returns:
        Float if a position yields a value in the OOC range, None otherwise.
    """
    if len(digits_str) < 4:
        return None
    sign_factor = -1.0 if sign_str == '-' else 1.0
    # Preferred position: 4 decimal places (standard SC format).
    # Try that position first, then 3 (if one decimal was lost),
    # then widen the search.
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
    """Attempts to recover (x, y, z) when _RE_POS fails due to a missing '.'.

    Looks for 3 occurrences of `(\\d{6,9})km` in the line and attempts to
    insert a '.' at the last-4-digits position for each one.
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


# ─── Multi-pass consensus (Phase B) ──────────────────────────────────

# Tolerance to consider two passes converging.
_CONSENSUS_TOL_KM = 0.1


def _consensus_coords(pass_results):
    """Looks for a consensus among passes that extracted coordinates.

    Args:
        pass_results: list of tuples (pass_name, score, data) where data may
            contain (x, y, z) that are not None.

    Returns:
        Tuple (consensus_data, consensus_score) if ≥2 passes converge
        within ±0.1 km, None otherwise. consensus_data is the average of
        the converging passes.
    """
    valid = [
        (name, score, data) for name, score, data in pass_results
        if data.get("x") is not None and _coords_in_range(data["x"], data["y"], data["z"])
    ]
    if len(valid) < 2:
        return None
    # For each valid pass, count how many other passes are within ±tol.
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
    # Average over the cluster
    n = len(best_cluster)
    avg_x = sum(d["x"] for _, _, d in best_cluster) / n
    avg_y = sum(d["y"] for _, _, d in best_cluster) / n
    avg_z = sum(d["z"] for _, _, d in best_cluster) / n
    # Take other fields (ooc, location, camdir) from the highest-scoring pass
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

    def __init__(
        self,
        tesseract_path=None,
        engine="tesseract",
        glyph_engine="ncc",
        onnx_model_path="models/spacedrive_ocr.onnx",
        onnx_classes_path="models/spacedrive_ocr.classes.json",
        onnx_confidence_threshold=0.85,
    ):
        self.engine = engine.lower()
        self.tesseract_config = _build_tesseract_config()

        if self.engine == "tesseract":
            self._init_tesseract(tesseract_path)
        else:
            logger.warning(f"Unknown OCR engine '{engine}', falling back to Tesseract")
            self.engine = "tesseract"
            self._init_tesseract(tesseract_path)

        self._pool = ThreadPoolExecutor(max_workers=3)

        # Phase D: load the template library for NCC
        self.template_lib = TemplateLibrary()
        if self.template_lib.has_templates():
            logger.info(f"NCC templates loaded: {self.template_lib.stats()}")
        else:
            logger.info("No NCC templates found, falling back to Tesseract for coordinates")

        # Glyph classifier selection (pure NumPy NCC or ONNX CNN)
        self.glyph_engine = glyph_engine.lower()
        self._glyph_classifier = self._build_glyph_classifier(
            onnx_model_path, onnx_classes_path, onnx_confidence_threshold,
        )

        # Frame dedup cache: avoids re-running Tesseract if the HUD has not changed.
        self._last_frame_hash: int | None = None
        self._last_result: dict | None = None

    def _build_glyph_classifier(self, onnx_model_path, onnx_classes_path, onnx_threshold):
        """Builds the classification callable (classify_batch signature)."""
        if self.glyph_engine == "onnx":
            try:
                from sc_ocr.onnx_classifier import ONNXGlyphClassifier
                onnx_clf = ONNXGlyphClassifier(
                    onnx_model_path, onnx_classes_path, confidence_threshold=onnx_threshold,
                )
                logger.info("Glyph classifier: ONNX (model %s)", onnx_model_path)
                return onnx_clf.classify_batch
            except Exception as exc:
                logger.error("ONNX classifier init failed (%s) — falling back to NCC.", exc)
                self.glyph_engine = "ncc"

        logger.info("Glyph classifier: NCC template matching")
        return classify_batch

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
                logger.debug(f"Searching for Tesseract at: {path}")
                if os.path.exists(path):
                    pytesseract.pytesseract.tesseract_cmd = path
                    logger.info(f"Tesseract found: {path}")
                    found = True
                    break

            if not found:
                logger.error("ERROR: Tesseract-OCR not found in standard locations!")
                logger.error("Paths tried: " + ", ".join(possible_paths))
        else:
            pytesseract.pytesseract.tesseract_cmd = tesseract_path

        logger.info("Tesseract OCR initialized")

    def _ocr_image_to_text(self, img):
        return pytesseract.image_to_string(img, config=self.tesseract_config)

    def extract_data(self, images):
        logger.debug(f"OCR extraction from {len(images)} passes with engine {self.engine}")

        # Frame deduplication: fast hash on downsampled pixels.
        # If the HUD is identical to the previous tick, return the cached result
        # without re-running Tesseract (~0.1 ms instead of ~150 ms).
        frame_hash = hash(
            b''.join(img[::4, ::4].tobytes() for img in images.values())
        )
        if frame_hash == self._last_frame_hash and self._last_result is not None:
            logger.debug("Identical frame — cached OCR result reused (Tesseract skipped)")
            return self._last_result

        result = self._parse_images_parallel(images)
        self._last_frame_hash = frame_hash
        self._last_result = result
        return result

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
                        f"[{pass_name}] CamDir extracted: pitch={pitch} roll={roll} yaw={yaw}"
                    )
                    score += 5
                else:
                    logger.warning(f"[{pass_name}] Unparseable CamDir line: {line!r}")
            elif "Zone:" in line and "SolarSystem" in line:
                logger.debug(f"[{pass_name}] Zone line detected: {line}")
                zone_match = _RE_ZONE.search(line)
                if zone_match:
                    system_id = zone_match.group(1).strip()
                    matched_name = None
                    for known_id, name in self.SYSTEM_ID_MAP.items():
                        if known_id in system_id or system_id in known_id:
                            matched_name = name
                            break
                    data["location"] = matched_name if matched_name else "Unknown"
                    logger.info(f"[{pass_name}] System detected: ID={system_id}, Name={data['location']}")
                    score += 10
            elif "Pos:" in line or "pos:" in line.lower():
                # Reject meter lines (sub-zone HabPos, PlayerContainer...).
                if _is_meter_line(line):
                    logger.debug(f"[{pass_name}] Meter line ignored: {line[:80]}")
                    continue
                # Reject Root/SolarSystem (absolute frame ~14 M km).
                if _RE_POS_SYSTEM_FRAME.search(line):
                    logger.debug(f"[{pass_name}] Root/SolarSystem line rejected: {line[:80]}")
                    continue

                # Extract the zone name = everything before "Pos:"
                zone_match = _RE_ZONE_NAME.match(line)
                zone_name = zone_match.group(1).strip() if zone_match else ""
                logger.debug(f"[{pass_name}] Zone line: zone={zone_name!r} | {line[:120]}")

                # Phase D: try NCC first (faster + more accurate if templates available)
                coords = self._extract_coords_via_ncc(img, pass_name)

                # Fallback: Tesseract on the normalized line if NCC did not work
                if coords is None:
                    normalized = _normalize_ooc_line(line)
                    coords = self._extract_coords_from_line(normalized, pass_name)

                if coords is not None:
                    x, y, z = coords
                    # Geographic range validation (Phase B).
                    if not _coords_in_range(x, y, z):
                        logger.debug(
                            f"[{pass_name}] Coords out of range (absolute frame?): "
                            f"X={x:.1f} Y={y:.1f} Z={z:.1f} km — ignored"
                        )
                        continue
                    data["x"], data["y"], data["z"] = x, y, z
                    data["ooc"] = zone_name or "Unknown"
                    logger.info(
                        f"[{pass_name}] Position extracted: zone={zone_name!r} "
                        f"X={x} Y={y} Z={z}"
                    )
                    score += 10

        if any(v is not None for v in (data["x"], data["y"], data["z"])):
            score += 5
        if "km" in ocr_text.lower():
            score += 5

        return score, data

    def _extract_coords_via_ncc(self, binary_image, pass_name):
        """Extracts coordinates via custom NCC (Phase D).

        Line-by-line strategy:
          1. Segment into text bands (rows)
          2. For each row, classify glyphs via NCC
          3. Reconstruct the string and apply the Pos regex
          4. Geographic range validation (implicitly rejects Root/SolarSystem
             which have coords ~14 M km)
          5. Return the first row that yields valid coordinates

        Args:
            binary_image: binary image 0/255
            pass_name: pass name (for logs)

        Returns:
            tuple (x, y, z) or None
        """
        # ONNX classifier does not need templates; NCC does.
        if self.glyph_engine != "onnx" and not self.template_lib.has_templates():
            return None

        try:
            seg_result = find_glyph_regions(binary_image)
            glyphs = seg_result['glyphs']
            if not glyphs:
                return None

            # Group glyphs by row
            glyphs_by_row = {}
            for g in glyphs:
                glyphs_by_row.setdefault(g['row_idx'], []).append(g)

            # Process each row in order, return the first with valid coordinates
            for row_idx in sorted(glyphs_by_row.keys()):
                coords = self._extract_coords_from_row_ncc(
                    binary_image, glyphs_by_row[row_idx], row_idx, pass_name
                )
                if coords is not None and _coords_in_range(*coords):
                    return coords

            return None

        except Exception as e:
            logger.error(f"[{pass_name}] NCC error: {e}")
            return None

    def _extract_coords_from_row_ncc(self, binary_image, row_glyphs, row_idx, pass_name):
        """Classifies glyphs in a row and attempts to extract (x, y, z)."""
        row_glyphs = sorted(row_glyphs, key=lambda g: g['x'])

        # Prepare crops in x order
        glyph_images = []
        for g in row_glyphs:
            x, y, w, h = g['x'], g['y'], g['w'], g['h']
            crop = binary_image[y:y+h, x:x+w]
            if crop.size > 0:
                glyph_images.append((g['id'], crop))

        if not glyph_images:
            return None

        classifications = self._glyph_classifier(
            glyph_images, self.template_lib, glyphs_meta=row_glyphs
        )

        # Reconstruct the string with a space if horizontal gap > 6 px
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

        # Virtually prepend "Pos: " for the regex (NCC does not read letters)
        candidate = "Pos: " + reconstructed
        normalized = _normalize_ooc_line(candidate)

        # Strict match only: NCC + heuristic '.' must reconstruct the decimal
        # point correctly. If the strict regex fails, it is likely an absolute
        # frame (Root/SolarSystem ~14 M km without '.') — skip this row instead
        # of risking a false match via recovery.
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
        """Extracts (x, y, z) from a normalized OOC line.

        Tries the strict regex first (4 decimal places), then missing '.'
        recovery if it fails. Returns None if neither method yields a valid
        triplet.
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
                logger.error(f"[{pass_name}] Coordinate conversion error: {e}")
                return None
        # Attempt to recover the missing '.' (Phase B).
        recovered = _try_recover_pos_line(normalized_line)
        if recovered is not None:
            logger.info(
                f"[{pass_name}] Position recovered via '.' insertion: "
                f"X={recovered[0]} Y={recovered[1]} Z={recovered[2]}"
            )
            return recovered
        logger.warning(f"[{pass_name}] Pos regex not matched: {normalized_line[:120]!r}")
        return None

    def _parse_images_parallel(self, images):
        """Runs OCR in parallel and applies multi-pass consensus.

        Strategy:
          1. All passes run in parallel.
          2. Collect all results (pass_name, score, data).
          3. If ≥2 passes converge within ±0.1 km → use the average.
          4. Otherwise → take the pass with the best score (previous behavior).
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

        # Attempt multi-pass consensus (Phase B).
        consensus = _consensus_coords(pass_results)
        if consensus is not None:
            consensus_data, _ = consensus
            logger.debug(
                f"Multi-pass consensus: X={consensus_data['x']:.4f} "
                f"Y={consensus_data['y']:.4f} Z={consensus_data['z']:.4f}"
            )
            return consensus_data

        # Fallback: best score.
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
        print(f"OCR result: {result}")
    else:
        print("Image test_capture.png not found.")
