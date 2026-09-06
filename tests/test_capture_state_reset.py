"""A new capture source must not inherit cached OCR or a previous zone lock."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from navigation import NavigationEngine
from ocr import OCRProcessor


def test_source_reset_forces_recognition_of_an_identical_frame():
    processor = OCRProcessor.__new__(OCRProcessor)
    processor.engine = "tesseract"
    processor.pipeline_mode = "hybrid"
    processor.reset_capture_state()
    processor._try_ncc_full_extraction = lambda *args: None
    calls = []

    def recognize(images):
        calls.append(images)
        return {"x": len(calls), "y": 2.0, "z": 3.0, "ooc": "TestZone"}

    processor._parse_images_parallel = recognize
    images = {"otsu": np.zeros((60, 600), dtype=np.uint8)}
    assert processor.extract_data(images)["x"] == 1
    assert processor.extract_data(images)["x"] == 1
    assert len(calls) == 1
    processor.reset_capture_state()
    assert processor.extract_data(images)["x"] == 2
    assert len(calls) == 2


def test_source_reset_preserves_target_but_requires_new_zone_confirmation():
    navigation = NavigationEngine.__new__(NavigationEngine)
    navigation.set_target(1.0, 2.0, 3.0, "Saved destination", "TestZone", "space")
    target = navigation.target
    navigation.update_zone_tracking({"ooc": "TestZone"})
    assert navigation._zone_match_locked
    navigation.reset_zone_tracking()
    assert navigation.target is target
    assert not navigation._zone_match_locked
    assert navigation._zone_last_match_ts is not None
    navigation.update_zone_tracking({"ooc": "OtherZone"})
    assert not navigation._zone_match_locked
    navigation.update_zone_tracking({"ooc": "TestZone"})
    assert navigation._zone_match_locked
