"""Tests for CamDir extraction tolerant to OCR variants + Pos filtering."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ocr import (
    OCRProcessor,
    _RE_CAMDIR_TAG,
    _RE_OOC_HINT,
    _RE_POS,
    _parse_camdir_values,
    _pos_match_to_coords,
)


def _parse_text(text):
    """Reproduces the logic of _ocr_single_pass on raw text."""
    proc = OCRProcessor.__new__(OCRProcessor)  # bypass __init__ (no Tesseract)
    proc.engine = "tesseract"
    proc.tesseract_config = ""

    text = proc._correct_ocr_errors(text)
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    data = {
        "cam_pitch": None, "cam_roll": None, "cam_yaw": None,
        "x": None, "y": None, "z": None, "ooc": None,
    }
    for line in lines:
        if _RE_CAMDIR_TAG.search(line):
            values = _parse_camdir_values(line)
            if values is not None:
                data["cam_pitch"] = float(values[0])
                data["cam_roll"] = float(values[1])
                data["cam_yaw"] = float(values[2])
        elif "Pos:" in line or "pos:" in line.lower():
            ooc_match = _RE_OOC_HINT.search(line)
            if not ooc_match:
                continue
            m = _RE_POS.search(line)
            if m:
                coords = _pos_match_to_coords(m)
                if coords is not None:
                    data["x"], data["y"], data["z"] = coords
                    data["ooc"] = ooc_match.group(1).strip().replace(" ", "_")
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
    text = "CarnDir: 12 5 90 FOV: 60"  # OCR typo: n→m
    d = _parse_text(text)
    assert d["cam_pitch"] == 12
    assert d["cam_yaw"] == 90


def test_camdir_ocr_error_cam0ir():
    text = "Cam0ir: 12 5 90 FOV: 60"  # OCR typo: D→0
    d = _parse_text(text)
    assert d["cam_pitch"] == 12
    assert d["cam_yaw"] == 90


def test_camdir_missing():
    text = "Zone: SolarSystem 1234 Pos: 100 200 300\nFPS 60"
    d = _parse_text(text)
    assert d["cam_pitch"] is None
    assert d["cam_yaw"] is None


def test_camdir_with_lost_c():
    text = "amdir: 27 27 112 FOV: 60"  # Initial 'C' lost by OCR
    d = _parse_text(text)
    assert d["cam_yaw"] == 112


# ---- Real log cases: OCR merges values ----

def test_camdir_concat_negatives():
    # 'CamDir:-15-32-92_FOV': all cascaded negatives
    assert _parse_camdir_values("CamDir:-15-32-92_FOV:59") == [-15, -32, -92]


def test_camdir_pos_then_negs():
    # 'CamDir:18-7-179FOV:59': 18, -7, -179
    assert _parse_camdir_values("CamDir:18-7-179FOV:59") == [18, -7, -179]


def test_camdir_split_neg_run_5177():
    # 'CamDir:25-5177FOV:59': -5177 must be split into -5 + 177
    assert _parse_camdir_values("CamDir:25-5177FOV:59") == [25, -5, 177]


def test_camdir_partial_space():
    # 'CamDir: 8-29-128FOV': 8 -29 -128
    assert _parse_camdir_values("CamDir: 8-29-128FOV:59") == [8, -29, -128]


def test_camdir_3pos_no_separator():
    # '2727112' has a unique 3-way greedy split: (27, 27, 112). The parser
    # now recovers it. (Previously returned None as "ambiguous"; in
    # practice OCR space-merge across all three values is common and the
    # greedy-left strategy succeeds.)
    assert _parse_camdir_values("CamDir:2727112FOV:59") == [27, 27, 112]


def test_camdir_3pos_merged_8439134():
    # User report: OCR fuses '84 39 134' (real value) into '8439134'.
    # Greedy-left 3-way split must recover [84, 39, 134].
    assert _parse_camdir_values("CamDir:8439134FOV:60") == [84, 39, 134]


def test_camdir_3pos_merged_user_log():
    # Verbatim from spacedrive.log — Tesseract reads digit-merged plus
    # some digit misreads. We just verify the parser produces a clean
    # 3-way split (downstream code does sanity checks on the values).
    assert _parse_camdir_values("CamDir:7712152FOV:59Focal:0.10FStop:64.0") == [77, 12, 152]


def test_camdir_canonical_via_parser():
    # Espaces préservés
    assert _parse_camdir_values("CamDir: 27 27 112 FOV: 60") == [27, 27, 112]


def test_camdir_negative_first_positives_after():
    # 'CamDir:-90 45 90': all valid
    assert _parse_camdir_values("CamDir:-90 45 90 FOV:60") == [-90, 45, 90]


def test_camdir_payload_too_short():
    assert _parse_camdir_values("CamDir:") is None


def test_camdir_no_tag():
    assert _parse_camdir_values("Zone:Root Pos: 14 -1 0") is None


# ---- Pos filter: prioritizes OOC lines (planet-relative) ----

def test_pos_objectcontainer_rejected():
    # Habs: sub-container in meters, not an OOC line → ignored
    text = "Zone:ObjectContainer_HabsPos:21.61m-11.39m57.98s"
    d = _parse_text(text)
    assert d["x"] is None


def test_pos_mixed_only_root_kept():
    # After OOC refactor, we keep OOC lines (not Root) — last OOC match wins.
    # Here we have only one OOC: Stanton1_L2 (4133km).
    text = "\n".join([
        "Zone:ObjectContainer_HabsPos:21.61m-11.39m57.98s",
        "Zone:OOC_Stanton1_L2Pos:4133.5658km-1964.1279km-529.8936km",
        "Zone:SolarSystem_9830Pos:14139636.4135km-1964.1304km-529.892km",
        "Zone:RootPos:14139636.4135km-1964.1304km-529.892km",
    ])
    d = _parse_text(text)
    # OOC zone captured → values ~4133 (planet-relative), not 14M
    assert d["x"] is not None
    assert abs(d["x"] - 4133.5658) < 1e-3
    assert d["ooc"] == "Stanton1_L2"


# ---- OCR: OOC name + coords extraction ----

def test_ooc_hurston_extracted():
    text = "Zone: OOC_Stanton_1_Hurston Pos: 130.9362km 52.8723km 990.0499km"
    d = _parse_text(text)
    assert d["ooc"] == "Stanton_1_Hurston"
    assert abs(d["x"] - 130.9362) < 1e-3
    assert abs(d["y"] - 52.8723) < 1e-3
    assert abs(d["z"] - 990.0499) < 1e-3


def test_ooc_no_underscore_in_oocname_compact():
    text = "Zone:OOC_Stanton1_L2 Pos:4133.5634km-1964.1276km-529.7565km"
    d = _parse_text(text)
    assert d["ooc"] == "Stanton1_L2"
    assert abs(d["x"] - 4133.5634) < 1e-3


# ---- Mixed-unit Pos lines: one axis in meters, others in km ----

def test_ooc_first_axis_in_meters():
    # User-observed: near zone origin, X drops to meters with 2 decimals
    # while Y/Z stay in km with 4 decimals. Must be accepted and converted.
    text = "Zone: OOC_Stanton_3b_Wala Pos: 5156.60m -280.9130km -42.5880km"
    d = _parse_text(text)
    assert d["ooc"] == "Stanton_3b_Wala"
    assert abs(d["x"] - 5.1566) < 1e-4   # 5156.60 m → 5.1566 km
    assert abs(d["y"] - (-280.9130)) < 1e-4
    assert abs(d["z"] - (-42.5880)) < 1e-4


def test_ooc_middle_axis_in_meters():
    text = "Zone:OOC_Stanton_1_Hurston Pos:130.9362km 800.50m -42.5880km"
    d = _parse_text(text)
    assert d["ooc"] == "Stanton_1_Hurston"
    assert abs(d["x"] - 130.9362) < 1e-4
    assert abs(d["y"] - 0.80050) < 1e-4   # 800.50 m → 0.80050 km
    assert abs(d["z"] - (-42.5880)) < 1e-4


def test_ooc_last_axis_in_meters():
    text = "Zone:OOC_Stanton_1_Hurston Pos:130.9362km -280.9130km 250.75m"
    d = _parse_text(text)
    assert abs(d["x"] - 130.9362) < 1e-4
    assert abs(d["y"] - (-280.9130)) < 1e-4
    assert abs(d["z"] - 0.25075) < 1e-4


def test_pos_all_meters_still_rejected():
    # Pure-meter line (Habs sub-container) must still be rejected via
    # _is_meter_line — no km anywhere = not a navigable OOC frame.
    from ocr import _is_meter_line
    line = "Zone:ObjectContainer_HabsPos:21.61m-11.39m57.98m"
    assert _is_meter_line(line) is True


def test_pos_mixed_unit_line_not_meter_only():
    # Mixed-unit lines must NOT be treated as meter-only lines.
    from ocr import _is_meter_line
    line = "Zone: OOC_Stanton_3b_Wala Pos: 5156.60m -280.9130km -42.5880km"
    assert _is_meter_line(line) is False


def test_ooc_objectcontainer_rejected_no_ooc_tag():
    # ObjectContainer_Habs is not an OOC line → ignored
    text = "Zone:ObjectContainer_HabsPos:21.61m-11.39m57.98s"
    d = _parse_text(text)
    assert d["x"] is None
    assert d["ooc"] is None


def test_ooc_root_only_no_ooc_rejected():
    # No OOC line, only Root → nothing extracted (Root is unstable
    # for planet-bound POIs)
    text = "Zone:RootPos:14139636.4135km-1964.1304km-529.892km"
    d = _parse_text(text)
    assert d["x"] is None
    assert d["ooc"] is None
