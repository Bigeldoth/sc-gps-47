"""Tests d'extraction CamDir tolérante aux variantes OCR."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ocr import OCRProcessor


def _parse_text(text):
    """Reproduit la logique de _ocr_single_pass sur du texte brut."""
    proc = OCRProcessor.__new__(OCRProcessor)  # bypass __init__ (pas de Tesseract)
    proc.engine = "tesseract"
    proc.paddle_ocr = None
    proc.tesseract_config = ""

    # On simule en injectant le texte ; _ocr_single_pass attend une image,
    # donc on extrait juste la logique parse via le code dupliqué ici.
    text = proc._correct_ocr_errors(text)
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    from ocr import _RE_CAMDIR_TAG, _RE_THREE_INTS
    data = {"cam_pitch": None, "cam_roll": None, "cam_yaw": None}
    for line in lines:
        if _RE_CAMDIR_TAG.search(line):
            m = _RE_THREE_INTS.search(line)
            if m:
                data["cam_pitch"] = float(m.group(1))
                data["cam_roll"] = float(m.group(2))
                data["cam_yaw"] = float(m.group(3))
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
