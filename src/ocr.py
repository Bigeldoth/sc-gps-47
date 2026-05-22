"""OCR pipeline for the Star Citizen HUD debug overlay.

Pipeline (NCC-first since Phase E):
  1. Capture (see capture.py) → 2 binary passes (otsu, adaptive) + CLAHE
     enhanced grayscale
  2. NCC/ONNX (once per frame): segment glyphs on 'otsu', classify crops on
     'enhanced' grayscale. If the reconstruction yields valid coords (and
     eventually zone once alphabetic templates exist), Tesseract is
     skipped entirely.
  3. Tesseract fallback OEM3 PSM6 on the binary passes in parallel
     (ThreadPoolExecutor). NCC coords are reused if available.
  4. Post-OCR normalization (Pos:_, lkm/Km, stray underscores)
  5. Strict Pos regex with 3-4 decimal places
  6. If the regex fails: attempt to recover the missing '.'
  7. Geographic range validation (|coord| < 30000 km)
  8. Multi-pass consensus: if ≥2 passes converge within ±0.1 km, average;
     otherwise, best score

HUD r_DisplayInfo 2 structure (3 Pos: lines):
  Line 1: Zone: SolarSystem_XXXXX Pos: X Y Z  → absolute frame, rejected
  Line 2: Root Pos: X Y Z                     → absolute frame, rejected
  Line 3: {ZoneName} Pos: X Y Z               → relative frame, TARGET

The 3rd line is always scanned without filtering on the zone name prefix:
OOC_Hurston, GrimHex, StantonIV-9, etc. are all accepted.
Only Root/SolarSystem are rejected (absolute frame ~14 M km).

Phase E architecture:
  - Segmentation: binary 'otsu' (good at locating bounding boxes even when
    characters are thickened).
  - Classification: CLAHE-enhanced grayscale (preserves the gradient detail
    that binary thresholding destroys).
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
# Per-axis pattern: either "<n>.<3-4 decimals> km" (full-km display) or
# "<n>.<1-2 decimals> m" (SC switches to meters when an axis is small,
# typically a few km from the zone origin — e.g. `5156.60m`). The two
# capture groups per axis are mutually exclusive: exactly one is populated
# per axis. `(?!k)` after `m` prevents the meter branch from gobbling the
# 'm' of 'km'.
_RE_AXIS = r'(?:(-?\d+\.\d{3,4})\s*km|(-?\d+\.\d{1,2})\s*m(?!k))'
# Relaxed last-axis: km allows 2-4 decimals (paddle truncates the right
# edge sometimes), m unchanged.
_RE_AXIS_LAX_LAST = r'(?:(-?\d+\.\d{2,4})\s*km|(-?\d+\.\d{1,2})\s*m(?!k))'

_RE_POS = re.compile(
    # Strict 3-axis Pos: line. Per axis: 3-4 decimals (km) OR 1-2 decimals
    # (m). Each match yields 6 groups: (km, m) × 3 axes. If Tesseract drops
    # a digit, the precision constraint rejects the noisy reading rather
    # than recording an approximate position.
    rf'[Pp]os:?\s*{_RE_AXIS}\s*{_RE_AXIS}\s*{_RE_AXIS}',
    re.IGNORECASE,
)
# Fallback regex used only when _RE_POS fails: relaxes the third axis km
# decimal count to 2-4 to catch paddle's right-edge truncation. First two
# axes stay strict so we never match a noisy partial reading by accident.
_RE_POS_RELAXED_LAST = re.compile(
    rf'[Pp]os:?\s*{_RE_AXIS}\s*{_RE_AXIS}\s*{_RE_AXIS_LAX_LAST}',
    re.IGNORECASE,
)
# Headless triple-coord regex: matches three axis blocks anywhere in the
# string, no `Pos:` prefix required. Used ONLY as a last resort on lines
# that already look like OOC navigation (contains an OOC zone token), to
# recover frames where Paddle lost the 'Pos:' keyword (or rendered it as
# digits, e.g. '905').
_RE_POS_HEADLESS = re.compile(
    rf'{_RE_AXIS}\s*{_RE_AXIS}\s*{_RE_AXIS_LAX_LAST}',
    re.IGNORECASE,
)


def _km_from_axis_groups(km_grp, m_grp):
    """Convert the (km, m) capture pair of an axis to a value in km.

    The 6-group Pos regexes alternate (km_grp, m_grp) per axis. Exactly one
    is non-None — call this helper once per axis. Returns None only if both
    are absent (shouldn't happen on a successful match).
    """
    if km_grp is not None:
        return float(km_grp)
    if m_grp is not None:
        return float(m_grp) / 1000.0
    return None


def _pos_match_to_coords(match):
    """Convert a successful _RE_POS / _RE_POS_RELAXED_LAST / _RE_POS_HEADLESS
    match to an (x, y, z) tuple in km, or None on conversion failure.
    """
    try:
        x = _km_from_axis_groups(match.group(1), match.group(2))
        y = _km_from_axis_groups(match.group(3), match.group(4))
        z = _km_from_axis_groups(match.group(5), match.group(6))
    except (ValueError, IndexError):
        return None
    if None in (x, y, z):
        return None
    return (x, y, z)
# Heuristic OOC line marker — covers the in-game zone names we know Paddle
# garbles. Kept loose because Paddle never reads 'OOC' or 'Stanton' clean.
# Variants observed in diagnostics: 'tanton', 'tantan', '5tanton', '5tantan',
# '5taatan', '5tant0n', '513nt0n', 'StanT0n', 'cor[orp', 'A5cCorp', 'ArcCorp',
# 'A5C5050', 'A5cC0r0', 'ArcC0r0', '45C5959', '4555959'. The pattern below
# accepts any digit-letter-soup with the first letter t-ish and 'n' two
# chars later, plus the recognizable 'corp/c0r0/c050/c059/5959' suffix.
_RE_OOC_HINT = re.compile(
    r'(?:[t5]\w{1,5}t[ao0]\w*?[n0o]|[ao4][r5]c\w?[cC50]\w*|microt|hurst|crusad|cellin|stanton|tanton)',
    re.IGNORECASE,
)
# Rejects Root/SolarSystem lines (absolute frame ~14 M km, unusable).
_RE_POS_SYSTEM_FRAME = re.compile(
    r'(?<![a-zA-Z])r[o0e]{1,3}t(?![a-zA-Z])|solar\s*system',
    re.IGNORECASE,
)

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
    'GGC': 'OOC',
    'Micratech': 'Microtech',
    # Paddle frequently confuses '0' with 'O' / 'o' on the OOC zone token,
    # and emits 'Zane:' / 'Zone;' / 'Zose;' for 'Zone:'.
    'Ooc ': 'OOC ',
    '00c ': 'OOC ',
    'O0c ': 'OOC ',
    '0oc ': 'OOC ',
    'Zone;': 'Zone:',
    'Zane:': 'Zone:',
    'Zane;': 'Zone:',
    'Zose;': 'Zone:',
    'Zaner': 'Zone:',
    'Zoner': 'Zone:',
}


def _normalize_ooc_line(line):
    """Normalizes an OOC line before applying _RE_POS.

    Fixes common Tesseract / Paddle artifacts. Empirical evidence comes from
    `diagnostics/paddle/*/summary.md`. The substitutions are aggressive but
    scoped — each replacement requires surrounding digits/structure so we
    don't corrupt normal English text in zone names.
    """
    # 1. Missing space before Pos: ('OOC_L2Pos:' → 'OOC_L2 Pos:')
    line = re.sub(r'(?<=[^\s])([Pp]os:)', r' \1', line)
    # 2. Pos:_ → Pos:  (underscore/multiple spaces after the colon)
    line = re.sub(r'(Pos:?)[\s_]+', r'\1 ', line)
    # 3. Paddle 'Pos' misreads. The HUD font's:
    #   - 'P' is confused with '9' (similar curve)
    #   - 'o' is confused with 0/a/9/Q
    #   - 's' is confused with 5/S/a/6
    #   - the trailing ':' is dropped or read as i/!/;/,/.
    # Canonicalise to 'Pos:' whenever the token is followed by ':' or any
    # close-enough terminator. The lookbehind only blocks letters (not
    # digits) — paddle frequently glues 'P05' onto a preceding digit run
    # like 'A5C5059P05', and `9P05` is still a real Pos token.
    line = re.sub(
        r'(?<![A-Za-z])[Pp9][0oOaA9qQ][sSa56][:\s;,.!iI|]?',
        lambda m: 'Pos:' if m.group(0)[-1] in ':;,.!iI|' else (
            'Pos ' if m.group(0)[-1] in ' \t' else 'Pos'
        ),
        line,
    )
    # 4. G/O → 0 in digit contexts (green channel artefact: 586.4G26 → 586.4026)
    line = re.sub(r'(?<=[\d.])[GO](?=[\d.])', '0', line)
    line = re.sub(r'(?<=\d)[GO](?=\s*km)', '0', line)
    # 4b. Z → 2 fix-up RUN EARLY (before km canon) so `-103.597Zk` first
    # becomes `-103.5972k` and the km rule below can then turn it into 'km'.
    line = re.sub(r'(?<=[\d.])Z(?=[\dk])', '2', line)
    line = re.sub(r'(?<=[-\s])Z(?=\d)', '2', line)
    # 5. Aggressive 'km' fix-up. After a digit, accept 'k' followed by ANY
    # single non-whitespace char (Paddle hallucinates m/n/h/d/a/1/l/I/R/K/T
    # and even punctuation like ©/®/}/{), as long as the surrounding context
    # still looks like a coordinate boundary (next char is whitespace, sign,
    # digit, or EOL).
    line = re.sub(
        r'(?<=\d)k[^\s\-\d](?=[\s\-\d]|$)', 'km', line,
    )
    line = re.sub(r'(?<=\d)k(?=[\s\-]|$)', 'km', line)
    # 5b. Stranded 'k' followed by 'm' on the next side after extra junk:
    # 'km7' → 'km' (drop trailing alnum hallucinated AFTER a valid km).
    line = re.sub(r'(?<=\dkm)[a-zA-Z0-9](?=\s|$)', '', line)
    # Legacy variants (Tesseract) — kept for back-compat.
    line = re.sub(r'(?<=[\d.])[lLiI1]?[kK][mMnNhH](?=[\s_\-\d]|$)', 'km', line)
    line = re.sub(r'km[\s_]+(-?\d)', r'km \1', line)
    # 6. Comma decimal separator (Paddle French locale): -748,258 → -748.258.
    line = re.sub(r'(?<=\d),(?=\d)', '.', line)
    # 7. J ↔ 7 in coord position (after sign / whitespace).
    line = re.sub(r'(?<=[-\s])J(?=\d)', '7', line)
    # 8. Z ↔ 2 — also already handled in step 4b but re-run here in case the
    # Pos canon / G→0 step turned an alnum context into a digit one.
    line = re.sub(r'(?<=[\d.])Z(?=[\dk])', '2', line)
    line = re.sub(r'(?<=[-\s])Z(?=\d)', '2', line)
    # 9. Space inside a coordinate ('-263 .8255' → '-263.8255') — happens
    # when Paddle's tokenizer splits a number around the decimal point.
    line = re.sub(r'(\d)\s+\.(\d)', r'\1.\2', line)
    # 9b. Space WHERE THE DECIMAL POINT WAS: paddle sometimes loses the dot
    # entirely. We can only safely insert it inside a Pos line where we
    # expect '<1-3 digit integer>.<3-4 digit fraction>km'. Constraint kept
    # tight so we don't merge legitimate space-separated tokens.
    line = re.sub(
        r'(?<=\d)\s+(\d{3,4})(?=\s*km)', r'.\1', line,
    )
    # 9c. Collapse space after a leading negative sign ('- 263' → '-263')
    # when the next token is clearly a coordinate.
    line = re.sub(r'(?<=\s)-\s+(?=\d+\.)', '-', line)
    # 10. Missing space before negative coord ('P05:-748' → 'P05: -748').
    line = re.sub(r'(?<=[Pp]os:)(-?\d)', r' \1', line)
    return line


# Loose detector for ANY paddle-mangled Pos token. Matches the same shapes
# the Pos canonicalisation in `_normalize_ooc_line` rewrites to 'Pos:', plus
# the same terminator set. Used by the orphan-join to recognise that a line
# is "a Pos line" before normalisation has had a chance to canonicalise it.
# Lookbehind only blocks letters (not digits) so glued tokens like 'A5C5059P05'
# still trigger.
_RE_POS_LIKE = re.compile(
    r'(?<![A-Za-z])[Pp9][0oOaA9qQ][sSa56][:\s;,.!iI|\-]',
    re.IGNORECASE,
)


def _join_orphan_pos_lines(text: str) -> str:
    """Glues `Pos:` and its values back together when an OCR detector returns
    them on consecutive lines.

    Triggered for paddle output like:
        Zone: Ooc Stanton 3 ArcCord Pos:
        -747.4760km -104.2975km -265.3238k
    which we want to coalesce into a single line so `_RE_POS` can fire.

    Also handles the more frequent paddle pattern where Pos already has 1 or
    2 coords on its line and the remaining 1 or 2 land on the next line(s):
        ... Pos: -748.2809km
        -103.5753km -263.8265
    Up to two continuation lines are absorbed (covers the 3-line split seen
    in `diagnostics/paddle/20260514_215532/frame_008`).
    """
    if not text:
        return text
    # Cheap heuristic to count "decimal coordinate-looking" tokens on a line.
    def _coord_count(s: str) -> int:
        return len(re.findall(r"-?\d+\.\d{1,4}", s))

    lines = text.split('\n')
    merged: list[str] = []
    i = 0
    while i < len(lines):
        current = lines[i]
        stripped = current.rstrip()
        lower = stripped.lower()
        # Case 1: line ends with the literal 'Pos:' marker (no values yet).
        if lower.rstrip().endswith('pos:') and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt and re.search(r'-?\d', nxt):
                merged.append(stripped + ' ' + nxt)
                i += 2
                continue
        # Case 2: line contains a Pos-like token AND has fewer than 3 decimal
        # numbers. Pull continuation lines that look like '<digits>.<digits>km
        # <…>' until we reach 3 coords or run out.
        #
        # IMPORTANT: we *skip* this join when the Pos line is a sub-zone /
        # interior Pos (meter unit, e.g. 'oc_a18_sp_int Pos: -0.67m -33').
        # Those values are valid but in meters — they MUST NOT be glued with
        # the next km-scale OOC Pos. Heuristic: if the line contains 'Pos:'
        # followed by a number+'m' (with no 'km'), treat it as a sub-zone
        # and leave it alone.
        is_pos_line = bool(_RE_POS_LIKE.search(stripped))
        # detect sub-zone Pos (meter unit, no km on the line yet)
        is_meter_subzone = bool(
            re.search(r"[Pp][0oOa9q][sSa56][:\s].*\d+\.?\d*\s*m\b", stripped)
            and "km" not in lower
        )
        if is_pos_line and not is_meter_subzone:
            current_total = _coord_count(stripped)
            j = i + 1
            absorbed: list[str] = []
            while current_total < 3 and j < len(lines) and len(absorbed) < 2:
                nxt = lines[j].strip()
                # Continuation must START with a number (possibly signed) so we
                # don't accidentally swallow the next zone line.
                if not re.match(r'-?\d', nxt):
                    break
                cnt = _coord_count(nxt)
                if cnt == 0:
                    break
                absorbed.append(nxt)
                current_total += cnt
                j += 1
            if absorbed:
                merged.append(stripped + ' ' + ' '.join(absorbed))
                i = j
                continue
        merged.append(current)
        i += 1
    return '\n'.join(merged)


def _is_meter_line(line: str) -> bool:
    """True if the line expresses ALL coordinates in meters (no km at all).

    Sub-zone lines (PlayerContainer, HabPos...) use 'm' for every axis —
    they are rejected: they do not correspond to navigable OOC coordinates.

    Mixed-unit lines (e.g. ``Pos: 5156.60m -280.9130km -42.5880km``) are
    legitimate OOC reads where one axis happens to be close enough to the
    zone origin that SC switched it to meters. These return False and are
    parsed normally — _RE_POS now accepts km|m per axis.
    """
    # If any km unit is present, this is an OOC-frame line (mixed or pure km).
    if re.search(r'\d+\.\d+\s*km\b', line, re.IGNORECASE):
        return False
    # No km + at least one m suffix → pure-meter sub-container line.
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

# F5 — ONNX sanity tolerance. When Tesseract's best pass disagrees with the
# ONNX/NCC result by more than this on any axis, we prefer ONNX. Sized to
# catch sign drops (~1500 km), digit hallucinations (~700 km) and 0↔6 in the
# hundreds (~60 km) without firing on regular sub-km Tesseract decimal noise.
_ONNX_SANITY_TOL_KM = 50.0

# Tolerance to consider two passes converging. Raised from 0.1 km to 1.0 km
# after a video-replay analysis showed the OTSU and ADAPTIVE Tesseract passes
# almost always agree on the integer and tens digits but disagree on the
# 3rd-4th decimal (centimeter-millimeter noise). A 0.1 km gate fired on only
# ~5 % of frames and the rest fell through to the brittle best-score fallback;
# a 1.0 km gate keeps consensus active while still rejecting the catastrophic
# misreads we care about (sign drops, lost digits, all > 50 km).
_CONSENSUS_TOL_KM = 1.0


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
        pipeline_mode="hybrid",
        paddle_device="cpu",
        paddle_model_dir="",
        paddle_lang="en",
        paddle_vl_endpoint="http://127.0.0.1:8118",
        paddle_vl_model="PaddleOCR-VL-1.5-0.9B",
        paddle_vl_backend="transformers",
        paddle_min_confidence=0.30,
        tesseract_lang="eng",
        tesseract_tessdata_dir="",
    ):
        self.paddle_min_confidence = float(paddle_min_confidence)
        # Tesseract language pack: 'eng' (stock) or 'spacedrive' (fine-tuned
        # LSTM produced by tools/train_tesseract.py). `tessdata_dir` lets
        # the runtime load `models/tessdata/spacedrive.traineddata` without
        # touching the system tessdata folder. Empty → use the default.
        self.tesseract_lang = (tesseract_lang or "eng").strip() or "eng"
        self.tesseract_tessdata_dir = (tesseract_tessdata_dir or "").strip()
        # Rejection counters surfaced via _log_paddle_stats() every N frames.
        # Helps diagnose why the overlay stays red when paddle is active.
        self._paddle_stats = {
            "frames": 0,
            "accepted": 0,
            "no_text": 0,
            "below_conf": 0,
            "regex_fail": 0,
            "out_of_range": 0,
            "consensus_hits": 0,
        }
        self._paddle_stats_last_log = 0
        self.engine = engine.lower()
        self.pipeline_mode = (pipeline_mode or "hybrid").lower()
        if self.pipeline_mode not in ("hybrid", "full_text"):
            logger.warning(
                "Unknown pipeline_mode '%s', falling back to 'hybrid'", pipeline_mode
            )
            self.pipeline_mode = "hybrid"
        self.tesseract_config = _build_tesseract_config()
        # `_paddle_adapter` is used uniformly for both `paddle` and `paddle-vl`
        # — only the concrete class behind it differs. `_paddle_vl_service`
        # is set only in the `paddle-vl` path so shutdown() can stop it.
        self._paddle_adapter = None
        self._paddle_vl_service = None

        if self.engine == "tesseract":
            self._init_tesseract(tesseract_path)
        elif self.engine == "paddle":
            try:
                self._init_paddle(paddle_device, paddle_model_dir, paddle_lang)
            except Exception as exc:
                logger.error(
                    "PaddleOCR init failed (%s) — falling back to Tesseract", exc
                )
                self.engine = "tesseract"
                self._init_tesseract(tesseract_path)
        elif self.engine == "paddle-vl":
            try:
                self._init_paddle_vl(
                    endpoint=paddle_vl_endpoint,
                    model=paddle_vl_model,
                    backend=paddle_vl_backend,
                )
            except Exception as exc:
                logger.error(
                    "PaddleOCR-VL init failed (%s) — falling back to paddle (CPU)",
                    exc,
                )
                # Best-effort fallback: try standard paddle on CPU. If that
                # also fails (no paddle install at all), drop to tesseract.
                try:
                    self._init_paddle(
                        device="cpu", model_dir=paddle_model_dir, lang=paddle_lang,
                    )
                    self.engine = "paddle"
                except Exception as exc2:
                    logger.error(
                        "Paddle fallback also failed (%s) — using Tesseract", exc2,
                    )
                    self.engine = "tesseract"
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

        # Per-frame CLAHE-enhanced grayscale, populated by extract_data().
        # NCC/ONNX classification uses this; Tesseract uses the binary passes.
        self._enhanced_image = None

        # Per-frame cached NCC coords, populated by extract_data() once and
        # reused across the parallel Tesseract passes in _ocr_single_pass.
        self._frame_ncc_coords = None

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

    def _init_paddle_vl(self, endpoint: str, model: str, backend: str):
        """Starts the PaddleOCR-VL sidecar (if needed) and points the adapter
        at its HTTP endpoint.

        The sidecar lives in its own venv (`.venv-paddle-vl/`) and exposes an
        OpenAI-style chat completions API. We block here for up to 90s so the
        worker thread can know recognize() will succeed by the time we return.
        """
        from paddle_vl_service import get_service
        from paddle_vl_adapter import PaddleVLAdapter

        # Parse host/port out of the endpoint so the service singleton can be
        # rebuilt on config changes (different port/backend → new service).
        from urllib.parse import urlparse
        parsed = urlparse(endpoint)
        port = parsed.port or 8118

        self._paddle_vl_service = get_service(
            model=model, backend=backend, port=port,
        )
        started = self._paddle_vl_service.start(blocking=True, timeout=90.0)
        if not started:
            raise RuntimeError(
                "PaddleOCR-VL sidecar did not become ready within 90s"
            )
        self._paddle_adapter = PaddleVLAdapter(
            endpoint=endpoint, model=model,
        )
        logger.info(
            "OCR engine: paddle-vl (endpoint=%s, model=%s, backend=%s)",
            endpoint, model, backend,
        )

    def _init_paddle(self, device: str, model_dir: str, lang: str):
        """Lazily creates the PaddleOCR adapter."""
        from paddle_adapter import PaddleAdapter
        self._paddle_adapter = PaddleAdapter(
            device=device,
            model_dir=model_dir if model_dir else None,
            lang=lang,
        )
        logger.info(
            "OCR engine: paddle (device=%s, model_dir=%s, lang=%s)",
            device, model_dir or "<pretrained>", lang,
        )

    def _ocr_image_to_text(self, img):
        # Both `paddle` and `paddle-vl` go through the same adapter interface.
        if self.engine in ("paddle", "paddle-vl") and self._paddle_adapter is not None:
            return self._paddle_adapter.recognize(img)
        return pytesseract.image_to_string(
            img,
            lang=self._resolve_tesseract_lang(),
            config=self._tesseract_config_with_tessdata(),
        )

    @staticmethod
    def _find_text_rows(binary_img, min_density=0.04, min_height=6):
        """Horizontal-projection row finder.

        Sums white pixels per Y row, marks bands where the density rises
        above ``min_density`` (fraction of width) and stays there for at
        least ``min_height`` rows. Returns ``[(y_start, y_end)]`` top-to-bottom.

        Beats ``sc_ocr.segment.find_glyph_regions`` for SC HUD row splitting:
        the inter-line gaps drop to near-zero density while the lines stay
        > 5 % full, so the histogram cleanly separates the 3 text bands.
        Glyph-bounding-box clustering misses the gap because connected
        components from ascenders/descenders bleed across the row boundary.
        """
        if binary_img is None or binary_img.size == 0:
            return []
        h, w = binary_img.shape[:2]
        if w == 0:
            return []
        density = (binary_img > 0).sum(axis=1) / float(w)
        bands: list[tuple[int, int]] = []
        in_band = False
        start = 0
        for y in range(h):
            if density[y] > min_density:
                if not in_band:
                    start = y
                    in_band = True
            else:
                if in_band:
                    if y - start >= min_height:
                        bands.append((start, y))
                    in_band = False
        if in_band and h - start >= min_height:
            bands.append((start, h))
        return bands

    def _ocr_image_to_text_per_row(self, binary_img):
        """Tesseract-only: cut into rows then OCR each row with ``--psm 7``.

        Cleaner than ``--psm 6`` on the whole HUD because Tesseract no longer
        has to figure out structure across CamDir / system-Pos / OOC-Pos
        lines that share the same column. PSM 7 = single text line,
        OEM 1 = LSTM only (no legacy engine fallback) — exactly what the
        thin monospaced HUD font wants.

        Falls back to the whole-image path when row detection yields fewer
        than 2 bands (HUD obscured / low contrast) so we never lose data.
        """
        bands = self._find_text_rows(binary_img)
        if len(bands) < 2:
            return self._ocr_image_to_text(binary_img)
        h, w = binary_img.shape[:2]
        cfg = (
            self._tesseract_config_with_tessdata()
            .replace("--psm 6", "--psm 7")
            .replace("--oem 3", "--oem 1")
        )
        lang = self._resolve_tesseract_lang()
        texts: list[str] = []
        pad_y = 4
        for y0, y1 in bands:
            crop = binary_img[max(0, y0 - pad_y):min(h, y1 + pad_y), :]
            try:
                t = pytesseract.image_to_string(crop, lang=lang, config=cfg)
            except Exception as exc:
                logger.debug("Per-row OCR failed at y=%d-%d: %s", y0, y1, exc)
                t = ""
            t = t.strip()
            if t:
                texts.append(t)
        return "\n".join(texts)

    def _resolve_tesseract_lang(self):
        """Returns the effective lang code, falling back to 'eng' if the
        configured custom language pack is missing from disk."""
        lang = self.tesseract_lang
        if lang == "eng":
            return lang
        tessdata = self.tesseract_tessdata_dir
        if tessdata:
            candidate = os.path.join(tessdata, f"{lang}.traineddata")
            if os.path.isfile(candidate):
                return lang
            logger.warning(
                "tesseract_lang='%s' but %s not found — falling back to 'eng'",
                lang, candidate,
            )
            return "eng"
        # No tessdata_dir override: defer to Tesseract's own search path.
        return lang

    def _tesseract_config_with_tessdata(self):
        """Augments the base config string with `--tessdata-dir` when the
        user pointed us at a custom location (e.g. shipped models/tessdata)."""
        tessdata = self.tesseract_tessdata_dir
        if not tessdata:
            return self.tesseract_config
        if not os.path.isdir(tessdata):
            logger.warning(
                "tesseract_tessdata_dir='%s' does not exist — ignored", tessdata,
            )
            return self.tesseract_config
        # Quote the path to survive spaces ("Program Files", "ProgramData", …).
        return f'{self.tesseract_config} --tessdata-dir "{tessdata}"'

    def extract_data(self, images):
        logger.debug(
            f"OCR extraction from {len(images)} passes "
            f"(engine={self.engine}, mode={self.pipeline_mode})"
        )

        # Separate the CLAHE-enhanced grayscale (used by NCC/ONNX for
        # classification) from the binary passes (used by Tesseract).
        # The enhanced image is NOT a Tesseract input.
        images = dict(images)  # avoid mutating caller's dict
        self._enhanced_image = images.pop('enhanced', None)
        raw_image = images.pop('raw', None)
        tesseract_images = {k: v for k, v in images.items() if k in ('otsu', 'adaptive')}

        # full_text mode: skip NCC/ONNX entirely and run the configured text
        # engine on whichever image source it prefers. Paddle / paddle-vl get
        # the raw BGR crop (both have their own detection); Tesseract keeps
        # the binary passes + multi-pass consensus.
        if self.pipeline_mode == "full_text":
            if self.engine in ("paddle", "paddle-vl"):
                source = raw_image if raw_image is not None else self._enhanced_image
                if source is None:
                    logger.warning("full_text/%s: no image source available", self.engine)
                    return self._empty_data()
                return self._extract_full_text_paddle(source, self._enhanced_image)
            self._frame_ncc_coords = None
            return self._parse_images_parallel(tesseract_images)

        # Frame deduplication: fast hash on downsampled pixels.
        # If the HUD is identical to the previous tick, return the cached result
        # without re-running Tesseract (~0.1 ms instead of ~150 ms).
        hash_sources = list(tesseract_images.values())
        if self._enhanced_image is not None:
            hash_sources.append(self._enhanced_image)
        frame_hash = hash(b''.join(img[::4, ::4].tobytes() for img in hash_sources))
        if frame_hash == self._last_frame_hash and self._last_result is not None:
            logger.debug("Identical frame — cached OCR result reused (Tesseract skipped)")
            return self._last_result

        # NCC-first: run glyph classification ONCE per frame (segment on otsu,
        # classify on enhanced). Cache the coords so _ocr_single_pass can reuse
        # them across the parallel Tesseract passes instead of re-running NCC.
        # If a full HUD reconstruction succeeds (coords + zone), we can skip
        # Tesseract entirely — gated on NCC actually producing a complete dict
        # (requires alphabetic templates, see commit 5).
        self._frame_ncc_coords = None
        otsu_image = tesseract_images.get('otsu')
        if otsu_image is not None:
            ncc_full = self._try_ncc_full_extraction(otsu_image, self._enhanced_image)
            if ncc_full is not None:
                self._frame_ncc_coords = (ncc_full["x"], ncc_full["y"], ncc_full["z"])
                if ncc_full.get("ooc") is not None:
                    logger.info("[ncc-first] Full HUD reconstructed via NCC, Tesseract skipped")
                    self._last_frame_hash = frame_hash
                    self._last_result = ncc_full
                    return ncc_full
                logger.debug("[ncc-first] NCC coords cached, Tesseract still needed for zone/metadata")

        if self.engine in ("paddle", "paddle-vl"):
            # Hybrid fallback for any paddle variant: single pass on the raw
            # BGR crop (paddle/paddle-vl have their own detection — no need
            # for binary thresholding nor multi-pass consensus).
            source = raw_image if raw_image is not None else self._enhanced_image
            if source is not None:
                result = self._extract_full_text_paddle(source, self._enhanced_image)
            else:
                result = self._empty_data()
        else:
            result = self._parse_images_parallel(tesseract_images)
        self._last_frame_hash = frame_hash
        self._last_result = result
        return result

    @staticmethod
    def _empty_data():
        return {
            "location": "Unknown",
            "x": None, "y": None, "z": None,
            "ooc": None,
        }

    def _extract_full_text_paddle(self, image, enhanced_image=None):
        """Runs PaddleOCR (one or two passes) and applies HUD parsing.

        For the local `paddle` engine, a second pass on the CLAHE-enhanced
        grayscale crop is run when available and the two passes are fed to
        the same `_consensus_coords` voting used by Tesseract. This compensates
        for Paddle's lack of native multi-pass: a single misrecognition
        (clipped `km`, mangled `Pos:`, etc.) would otherwise drop the frame.

        The remote `paddle-vl` engine is single-pass only — round-tripping
        through its HTTP sidecar twice per frame would blow the budget.

        Per-line confidences are exposed by `recognize_detailed()`. Lines
        below `self.paddle_min_confidence` are dropped before regex parsing.
        Rejection counters feed `_log_paddle_stats()` for diagnostics.
        """
        if self._paddle_adapter is None:
            logger.error("Paddle adapter not initialized")
            return self._empty_data()

        self._paddle_stats["frames"] += 1
        tag = self.engine  # 'paddle' or 'paddle-vl'

        # Single-pass only. The 2nd pass on the CLAHE-enhanced grayscale was
        # tried but never converged with the raw pass (consensus_hits=0 over
        # 300 frames in gameplay measurements): CLAHE introduces artifacts
        # that make the detector hallucinate different regions and the rec
        # texts disagree by more than _CONSENSUS_TOL_KM. Skipping it halves
        # the per-frame latency without losing accuracy.
        passes: list[tuple[str, "np.ndarray"]] = [("raw", image)]

        pass_results = []  # list of (pass_name, score, data) for consensus
        for pass_name, img in passes:
            score, data = self._paddle_single_pass(img, f"{tag}/{pass_name}")
            pass_results.append((pass_name, score, data))

        # Consensus across passes (≥2 within ±0.1 km → average). Reuses the
        # exact same function Tesseract uses, so the gameplay semantics stay
        # uniform across engines.
        if len(pass_results) >= 2:
            consensus = _consensus_coords(pass_results)
            if consensus is not None:
                consensus_data, _ = consensus
                self._paddle_stats["consensus_hits"] += 1
                self._paddle_stats["accepted"] += 1
                logger.info(
                    "[%s] Multi-pass consensus: X=%.4f Y=%.4f Z=%.4f",
                    tag,
                    consensus_data["x"], consensus_data["y"], consensus_data["z"],
                )
                self._log_paddle_stats()
                return consensus_data

        # No consensus → take the pass that actually yielded coordinates,
        # preferring the higher score. If none did, return empty.
        valid = [
            (name, score, data) for name, score, data in pass_results
            if data.get("x") is not None
        ]
        if valid:
            best = max(valid, key=lambda t: t[1])
            self._paddle_stats["accepted"] += 1
            self._log_paddle_stats()
            return best[2]

        # All passes failed — merge any zone/metadata partial info so the UI
        # at least keeps the location label even when coords were lost.
        merged = self._empty_data()
        for _, _, data in pass_results:
            if data.get("location") and data["location"] != "Unknown":
                merged["location"] = data["location"]
        self._log_paddle_stats()
        return merged

    def _paddle_single_pass(self, image, tag):
        """One Paddle inference + line parsing. Returns (score, data).

        `score` is the mean per-line confidence (0..1) across the lines that
        contributed to the parse — fed to `_consensus_coords` to break ties
        when multiple passes vote.
        """
        if self._paddle_adapter is None:
            return 0.0, self._empty_data()

        import time as _time
        t0 = _time.perf_counter()
        # Prefer recognize_detailed() (confidences exposed). Fall back to
        # recognize() if the adapter pre-dates it (e.g. paddle-vl adapter).
        if hasattr(self._paddle_adapter, "recognize_detailed"):
            detailed = self._paddle_adapter.recognize_detailed(image)
            texts = detailed.get("texts", []) or []
            scores = detailed.get("scores", []) or []
        else:
            raw_text = self._paddle_adapter.recognize(image)
            texts = [t for t in (raw_text or "").split("\n") if t]
            scores = [1.0] * len(texts)  # unknown — treat as fully confident
        dt_ms = (_time.perf_counter() - t0) * 1000.0
        logger.debug("[%s] inference: %.0f ms, %d region(s)", tag, dt_ms, len(texts))

        if not texts:
            self._paddle_stats["no_text"] += 1
            return 0.0, self._empty_data()

        # Drop low-confidence regions before parsing. Keeps the line count
        # but reduces the noise the regex / normalizer have to fight.
        threshold = self.paddle_min_confidence
        filtered_texts: list[str] = []
        filtered_scores: list[float] = []
        dropped_below = 0
        for text, score in zip(texts, scores + [0.0] * max(0, len(texts) - len(scores))):
            if score < threshold:
                dropped_below += 1
                logger.debug(
                    "[%s] drop low-conf region (%.2f<%.2f): %r",
                    tag, score, threshold, text,
                )
                continue
            filtered_texts.append(text)
            filtered_scores.append(score)
        if dropped_below:
            self._paddle_stats["below_conf"] += dropped_below

        if not filtered_texts:
            return 0.0, self._empty_data()

        ocr_text = "\n".join(filtered_texts)
        logger.debug("[%s] raw OCR text: %r", tag, ocr_text)
        ocr_text = self._correct_ocr_errors(ocr_text)
        # Paddle's detector frequently splits a HUD row across two boxes when
        # the trailing 'Pos:' is far from the leading 'Zone:' on the same
        # screen line. Re-attach a line that ends with 'Pos:' (no values)
        # to the next line, which holds the X/Y/Z values.
        ocr_text = _join_orphan_pos_lines(ocr_text)
        lines = [line.strip() for line in ocr_text.split("\n") if line.strip()]

        data = self._empty_data()
        coords_found = False
        for line in lines:
            if "Zone:" in line and "SolarSystem" in line:
                zone_match = _RE_ZONE.search(line)
                if zone_match:
                    system_id = zone_match.group(1).strip()
                    matched_name = None
                    for known_id, name in self.SYSTEM_ID_MAP.items():
                        if known_id in system_id or system_id in known_id:
                            matched_name = name
                            break
                    data["location"] = matched_name if matched_name else "Unknown"
            elif "Pos:" in line or "pos:" in line.lower():
                if _is_meter_line(line):
                    continue
                if _RE_POS_SYSTEM_FRAME.search(line):
                    continue
                zone_match = _RE_ZONE_NAME.match(line)
                zone_name = zone_match.group(1).strip() if zone_match else ""
                normalized = _normalize_ooc_line(line)
                coords = self._extract_coords_from_line(normalized, tag)
                if coords is None:
                    self._paddle_stats["regex_fail"] += 1
                    continue
                x, y, z = coords
                if not _coords_in_range(x, y, z):
                    self._paddle_stats["out_of_range"] += 1
                    continue
                data["x"], data["y"], data["z"] = x, y, z
                data["ooc"] = zone_name or "Unknown"
                coords_found = True
                logger.info(
                    "[%s] Position extracted: zone=%r X=%s Y=%s Z=%s",
                    tag, zone_name, x, y, z,
                )

        mean_score = (
            sum(filtered_scores) / len(filtered_scores) if filtered_scores else 0.0
        )
        # Reward passes that actually produced coordinates so consensus
        # voting picks a coord-bearing pass over a coord-less one on a tie.
        score = mean_score + (0.1 if coords_found else 0.0)
        return score, data

    def _log_paddle_stats(self):
        """Logs cumulative Paddle accept/reject counters every 50 frames."""
        if self._paddle_stats["frames"] - self._paddle_stats_last_log < 50:
            return
        self._paddle_stats_last_log = self._paddle_stats["frames"]
        s = self._paddle_stats
        denom = max(1, s["frames"])
        accept_pct = 100.0 * s["accepted"] / denom
        logger.info(
            "[paddle-stats] frames=%d accepted=%d (%.1f%%) "
            "consensus=%d no_text=%d below_conf=%d regex_fail=%d out_of_range=%d",
            s["frames"], s["accepted"], accept_pct,
            s["consensus_hits"], s["no_text"], s["below_conf"],
            s["regex_fail"], s["out_of_range"],
        )

    def _try_ncc_full_extraction(self, binary_image, enhanced_image):
        """One-shot NCC pass per frame. Segments on binary, classifies on enhanced.

        Returns a partial data dict (always with `x`, `y`, `z` if coords were
        found; `ooc` / `location` are populated when available). Returns None if
        NCC could not recover coordinates.
        """
        coords = self._extract_coords_via_ncc(binary_image, enhanced_image, "ncc-first")
        if coords is None:
            return None
        x, y, z = coords
        return {
            "location": "Unknown",
            "x": x, "y": y, "z": z,
            "ooc": None,
        }

    def _ocr_single_pass(self, pass_name, img):
        # Per-row PSM 7 + LSTM-only was measured to add ~80 % latency
        # without improving accuracy on the stock eng.traineddata — the
        # 0↔G / 8↔& confusions Tesseract makes are systemic to the model,
        # not the segmentation. `_ocr_image_to_text_per_row` is kept
        # available for future use once a fine-tuned spacedrive.traineddata
        # ships and per-line OCR can actually outperform the whole-HUD path.
        ocr_text = self._ocr_image_to_text(img)
        ocr_text = self._correct_ocr_errors(ocr_text)
        lines = [line.strip() for line in ocr_text.split('\n') if line.strip()]

        data = {
            "location": "Unknown",
            "x": None, "y": None, "z": None,
            "ooc": None,
        }
        score = 0

        for line in lines:
            if "Zone:" in line and "SolarSystem" in line:
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

                # Phase D: prefer the once-per-frame NCC result cached by
                # extract_data() (NCC-first pipeline). Falls back to Tesseract
                # parsing of the normalized line if NCC found nothing.
                coords = self._frame_ncc_coords
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

    def _extract_coords_via_ncc(self, binary_image, enhanced_image, pass_name):
        """Extracts coordinates via custom NCC (Phase D).

        Segmentation and classification are decoupled:
          - `binary_image` is used only to locate glyph bounding boxes
            (find_glyph_regions). Otsu thickens characters but keeps the
            connected components separate, making it good at segmentation.
          - `enhanced_image` (CLAHE grayscale) is used to crop glyphs for
            classification — it preserves the fine gradient detail that
            binary thresholding destroys.

        If `enhanced_image` is None, falls back to cropping from
        `binary_image` (backward-compat behavior).

        Line-by-line strategy:
          1. Segment into text bands (rows) on binary
          2. For each row, classify glyphs (NCC/ONNX) from the enhanced crop
          3. Reconstruct the string and apply the Pos regex
          4. Geographic range validation (implicitly rejects Root/SolarSystem
             which have coords ~14 M km)
          5. Return the first row that yields valid coordinates

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
                    binary_image, enhanced_image, glyphs_by_row[row_idx], row_idx, pass_name
                )
                if coords is not None and _coords_in_range(*coords):
                    return coords

            return None

        except Exception as e:
            logger.error(f"[{pass_name}] NCC error: {e}")
            return None

    def _extract_coords_from_row_ncc(self, binary_image, enhanced_image, row_glyphs, row_idx, pass_name):
        """Classifies glyphs in a row and attempts to extract (x, y, z).

        Crops are taken from `enhanced_image` when available (grayscale, rich
        in gradient detail) and fall back to `binary_image` otherwise.
        """
        row_glyphs = sorted(row_glyphs, key=lambda g: g['x'])

        classify_source = enhanced_image if enhanced_image is not None else binary_image

        # Prepare crops in x order
        glyph_images = []
        for g in row_glyphs:
            x, y, w, h = g['x'], g['y'], g['w'], g['h']
            crop = classify_source[y:y+h, x:x+w]
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

        # If NCC already recognized "Pos:" (letter templates available), use
        # the reconstructed string as-is; otherwise virtually prepend "Pos: "
        # so the coords regex can still match a digits-only reconstruction.
        if "Pos:" in reconstructed or "pos:" in reconstructed.lower():
            candidate = reconstructed
        else:
            candidate = "Pos: " + reconstructed
        normalized = _normalize_ooc_line(candidate)

        # Strict match only: NCC + heuristic '.' must reconstruct the decimal
        # point correctly. If the strict regex fails, it is likely an absolute
        # frame (Root/SolarSystem ~14 M km without '.') — skip this row instead
        # of risking a false match via recovery.
        coord_match = _RE_POS.search(normalized)
        if coord_match:
            coords = _pos_match_to_coords(coord_match)
            if coords is not None:
                return coords

        return None

    def _extract_coords_from_line(self, normalized_line, pass_name):
        """Extracts (x, y, z) from a normalized OOC line.

        Tries the strict regex first (4 decimal places), then missing '.'
        recovery if it fails. Returns None if neither method yields a valid
        triplet.
        """
        coord_match = _RE_POS.search(normalized_line)
        if coord_match:
            coords = _pos_match_to_coords(coord_match)
            if coords is not None:
                return coords
            logger.error(f"[{pass_name}] Coordinate conversion failed for match")
            return None
        # Fallback: relaxed third-coord (2-4 decimals) for paddle truncation
        # cases like `... -103.5786km -263.82` where the right edge of the
        # detection box clipped the trailing decimals.
        relaxed = _RE_POS_RELAXED_LAST.search(normalized_line)
        if relaxed:
            coords = _pos_match_to_coords(relaxed)
            if coords is not None:
                logger.info(
                    f"[{pass_name}] Position recovered via relaxed last-coord regex: "
                    f"X={coords[0]} Y={coords[1]} Z={coords[2]}"
                )
                return coords
        # Last resort: paddle sometimes loses the 'Pos:' keyword entirely
        # (e.g. reads it as '905' or simply drops it). If the line still
        # looks like an OOC line (zone-name hint) AND we can match three
        # consecutive '<num>km' blocks, accept that as the position. Gated
        # on the OOC hint so random number triples elsewhere can't poison it.
        if _RE_OOC_HINT.search(normalized_line):
            head = _RE_POS_HEADLESS.search(normalized_line)
            if head:
                coords = _pos_match_to_coords(head)
                if coords is not None and _coords_in_range(*coords):
                    logger.info(
                        f"[{pass_name}] Position recovered via headless triple-coord "
                        f"regex (Pos keyword was lost): X={coords[0]} Y={coords[1]} Z={coords[2]}"
                    )
                    return coords
        # Attempt to recover the missing '.' (Phase B).
        recovered = _try_recover_pos_line(normalized_line)
        if recovered is not None:
            logger.info(
                f"[{pass_name}] Position recovered via '.' insertion: "
                f"X={recovered[0]} Y={recovered[1]} Z={recovered[2]}"
            )
            return recovered
        # Lowered to debug: paddle frequently returns partial coord sets
        # (1 or 2 of 3 numbers detected) which legitimately fail the strict
        # regex. Logging every miss at WARNING level floods the log under
        # full_text/paddle mode.
        logger.debug(f"[{pass_name}] Pos regex not matched: {normalized_line[:120]!r}")
        return None

    def _parse_images_parallel(self, images):
        """Runs OCR in parallel and applies multi-pass consensus.

        Strategy:
          1. All passes run in parallel (otsu + adaptive Tesseract).
          2. Inject the cached ONNX/NCC coords as a synthetic third vote when
             available — so a single bad source can never propagate alone.
          3. If ≥2 sources converge within ±_CONSENSUS_TOL_KM → average.
          4. Otherwise → take the source with the best score.
          5. ONNX sanity gate: if the chosen result disagrees with the ONNX
             reading by > 50 km on any axis, prefer ONNX (more constrained
             classifier than free-form Tesseract regex).
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

        # F5 — add ONNX/NCC as a third vote when it produced coords this frame.
        # The score (0.85) is above the typical Tesseract-pass score so that on
        # disagreement it tips the consensus toward ONNX. Other fields (ooc,
        # location, metadata) are pulled from the best Tesseract pass so we
        # don't lose them.
        ncc_data_for_vote = None
        if self._frame_ncc_coords is not None:
            best_tess = max(pass_results, key=lambda t: t[1], default=None)
            template = dict(best_tess[2]) if best_tess else self._empty_data()
            ncc_data_for_vote = dict(template)
            ncc_data_for_vote["x"], ncc_data_for_vote["y"], ncc_data_for_vote["z"] = (
                self._frame_ncc_coords
            )
            pass_results.append(("onnx_vote", 0.85, ncc_data_for_vote))

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

        # F5 — ONNX sanity gate. If best_data exists and ncc_data is available
        # and they disagree by > _ONNX_SANITY_TOL_KM on any axis, prefer ONNX.
        if (
            best_data is not None
            and ncc_data_for_vote is not None
            and best_data.get("x") is not None
            and best_data is not ncc_data_for_vote
        ):
            disagreement = max(
                abs(best_data["x"] - ncc_data_for_vote["x"]),
                abs(best_data["y"] - ncc_data_for_vote["y"]),
                abs(best_data["z"] - ncc_data_for_vote["z"]),
            )
            if disagreement > _ONNX_SANITY_TOL_KM:
                logger.warning(
                    "ONNX sanity gate: tesseract best disagrees by %.2f km — "
                    "preferring ONNX (%.4f, %.4f, %.4f) over (%.4f, %.4f, %.4f)",
                    disagreement,
                    ncc_data_for_vote["x"], ncc_data_for_vote["y"], ncc_data_for_vote["z"],
                    best_data["x"], best_data["y"], best_data["z"],
                )
                best_data = ncc_data_for_vote
        if best_data is None:
            best_data = {
                "location": "Unknown",
                "x": None, "y": None, "z": None,
                "ooc": None,
            }
        return best_data

    def _correct_ocr_errors(self, text):
        for wrong, correct in _OCR_CORRECTIONS.items():
            text = text.replace(wrong, correct)
        return text

    def shutdown(self):
        self._pool.shutdown(wait=False)
        # Stop the VL sidecar if this processor owns it. We keep the service
        # alive on engine switches at runtime (it's expensive to restart), but
        # on app exit / engine reload we tear it down.
        if self._paddle_vl_service is not None:
            try:
                self._paddle_vl_service.stop()
            except Exception as exc:
                logger.warning("paddle-vl: stop() raised: %s", exc)


if __name__ == "__main__":
    processor = OCRProcessor()
    img = cv2.imread("test_capture.png")
    if img is not None:
        result = processor.extract_data({"test": img})
        print(f"OCR result: {result}")
    else:
        print("Image test_capture.png not found.")
