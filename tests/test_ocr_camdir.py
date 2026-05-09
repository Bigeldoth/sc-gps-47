"""Tests d'extraction CamDir tolérante aux variantes OCR + filtre Pos."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ocr import (
    OCRProcessor,
    _RE_CAMDIR_TAG,
    _RE_POS,
    _RE_POS_SYSTEM_FRAME,
    _parse_camdir_values,
)


def _parse_text(text):
    """Reproduit la logique de _ocr_single_pass sur du texte brut."""
    proc = OCRProcessor.__new__(OCRProcessor)  # bypass __init__ (pas de Tesseract)
    proc.engine = "tesseract"
    proc.paddle_ocr = None
    proc.tesseract_config = ""

    text = proc._correct_ocr_errors(text)
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    data = {
        "cam_pitch": None, "cam_roll": None, "cam_yaw": None,
        "x": None, "y": None, "z": None,
    }
    for line in lines:
        if _RE_CAMDIR_TAG.search(line):
            values = _parse_camdir_values(line)
            if values is not None:
                data["cam_pitch"] = float(values[0])
                data["cam_roll"] = float(values[1])
                data["cam_yaw"] = float(values[2])
        elif "Pos:" in line or "pos:" in line.lower():
            if not _RE_POS_SYSTEM_FRAME.search(line):
                continue
            m = _RE_POS.search(line)
            if m:
                try:
                    data["x"] = float(m.group(1))
                    data["y"] = float(m.group(2))
                    data["z"] = float(m.group(3))
                except ValueError:
                    pass
    return data


def test_camdir_canonical():
    text = "CamDir: 27 27 112 FOV: 60 Focal: 0.30 FStop: 0.5"
    d = _parse_text(text)
    assert d["cam_pitch"] == 27
    assert d["cam_roll"] == 27
    assert d["cam_yaw"] == 112


def test_camdir_no_colon():
    text = "CamDir 27 27 112 FOV: 60"
    d = _parse_text(text)
    assert d["cam_yaw"] == 112


def test_camdir_lowercase():
    text = "camdir: -45 0 -170 FOV: 60"
    d = _parse_text(text)
    assert d["cam_pitch"] == -45
    assert d["cam_yaw"] == -170


def test_camdir_negative_values():
    text = "CamDir: -90 -179 -180 FOV: 60"
    d = _parse_text(text)
    assert d["cam_pitch"] == -90
    assert d["cam_roll"] == -179
    assert d["cam_yaw"] == -180


def test_camdir_ocr_error_carndir():
    text = "CarnDir: 12 5 90 FOV: 60"  # n→m typo OCR
    d = _parse_text(text)
    assert d["cam_pitch"] == 12
    assert d["cam_yaw"] == 90


def test_camdir_ocr_error_cam0ir():
    text = "Cam0ir: 12 5 90 FOV: 60"  # D→0 typo OCR
    d = _parse_text(text)
    assert d["cam_pitch"] == 12
    assert d["cam_yaw"] == 90


def test_camdir_missing():
    text = "Zone: SolarSystem 1234 Pos: 100 200 300\nFPS 60"
    d = _parse_text(text)
    assert d["cam_pitch"] is None
    assert d["cam_yaw"] is None


def test_camdir_with_lost_c():
    text = "amdir: 27 27 112 FOV: 60"  # le 'C' initial perdu par OCR
    d = _parse_text(text)
    assert d["cam_yaw"] == 112


# ---- Cas réels du log : OCR colle les valeurs ----

def test_camdir_concat_negatives():
    # 'CamDir:-15-32-92_FOV' : tous négatifs cascadés
    assert _parse_camdir_values("CamDir:-15-32-92_FOV:59") == [-15, -32, -92]


def test_camdir_pos_then_negs():
    # 'CamDir:18-7-179FOV:59' : 18, -7, -179
    assert _parse_camdir_values("CamDir:18-7-179FOV:59") == [18, -7, -179]


def test_camdir_split_neg_run_5177():
    # 'CamDir:25-5177FOV:59' : -5177 doit être splitté en -5 + 177
    assert _parse_camdir_values("CamDir:25-5177FOV:59") == [25, -5, 177]


def test_camdir_partial_space():
    # 'CamDir: 8-29-128FOV' : 8 -29 -128
    assert _parse_camdir_values("CamDir: 8-29-128FOV:59") == [8, -29, -128]


def test_camdir_3pos_no_separator_unrecoverable():
    # '2727112' purement positifs collés : ambigu, on retourne None
    assert _parse_camdir_values("CamDir:2727112FOV:59") is None


def test_camdir_canonical_via_parser():
    # Espaces préservés
    assert _parse_camdir_values("CamDir: 27 27 112 FOV: 60") == [27, 27, 112]


def test_camdir_negative_first_positives_after():
    # 'CamDir:-90 45 90' : tous valides
    assert _parse_camdir_values("CamDir:-90 45 90 FOV:60") == [-90, 45, 90]


def test_camdir_payload_too_short():
    assert _parse_camdir_values("CamDir:") is None


def test_camdir_no_tag():
    assert _parse_camdir_values("Zone:Root Pos: 14 -1 0") is None


# ---- Filtre Pos: par zone (Bug 2) ----

def test_pos_root_frame_accepted():
    # Zone:Root → coords système ~14M extraites correctement
    text = "Zone:RootPos:14139636.4135km-1964.1304km-529.892km"
    d = _parse_text(text)
    assert abs(d["x"] - 14139636.4135) < 1e-3
    assert abs(d["y"] - (-1964.1304)) < 1e-3


def test_pos_solarsystem_frame_accepted():
    text = "Zone:SolarSystem9830472323442Pos:14139636.4054km-1964.101km-529.9106km"
    d = _parse_text(text)
    assert abs(d["x"] - 14139636.4054) < 1e-3


def test_pos_local_frame_rejected():
    # Zone:OOC_Stanton1_L2 → frame locale (~4133km), DOIT être rejeté
    text = "Zone:OOC_Stanton1_L2Pos:4133.5658km-1964.1279km-529.8936km"
    d = _parse_text(text)
    assert d["x"] is None


def test_pos_objectcontainer_rejected():
    # Habs : sous-conteneur en mètres
    text = "Zone:ObjectContainer_HabsPos:21.61m-11.39m57.98s"
    d = _parse_text(text)
    assert d["x"] is None


def test_pos_mixed_only_root_kept():
    # Lignes mixtes : seule Root doit donner les coords
    text = "\n".join([
        "Zone:ObjectContainer_HabsPos:21.61m-11.39m57.98s",
        "Zone:OOC_Stanton1_L2Pos:4133.5658km-1964.1279km-529.8936km",
        "Zone:SolarSystem_9830Pos:14139636.4135km-1964.1304km-529.892km",
        "Zone:RootPos:14139636.4135km-1964.1304km-529.892km",
    ])
    d = _parse_text(text)
    # On veut la valeur 14M, pas 4133, pas 21
    assert d["x"] is not None and d["x"] > 1_000_000
