"""Capture geometry reaches the debug UI even when OCR cannot read a frame."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import app_paths
from capture import ScreenCapture


@pytest.fixture
def overlay_module(tmp_path, monkeypatch):
    # main configures logging on import; keep it away from user settings/logs.
    monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
    monkeypatch.setattr(app_paths, "user_data_dir", lambda: tmp_path)
    spec = importlib.util.spec_from_file_location("capture_debug_test_main", ROOT / "src/main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_region_is_a_snapshot_and_screenshot_coordinates_are_not_desktop_bounds():
    capture = ScreenCapture.__new__(ScreenCapture)
    capture._test_screenshot = None
    capture._region = {"left": -600, "top": 1440, "width": 600, "height": 60}
    region = capture.screen_region
    region["left"] = 123
    assert capture.screen_region["left"] == -600

    capture._test_screenshot = "fixture.png"
    assert capture.screen_region is None


def test_worker_reports_changed_capture_bounds_even_on_ocr_failure(overlay_module, monkeypatch):
    source = ScreenCapture.__new__(ScreenCapture)
    source._test_screenshot = None
    source._region = {"left": -600, "top": 1440, "width": 600, "height": 60}
    source.capture = lambda: ({}, None, 12.0)

    def fail_ocr(images):
        raise RuntimeError("No text engine available")

    monkeypatch.setattr(overlay_module, "ScreenCapture", lambda: source)
    monkeypatch.setattr(
        overlay_module, "_build_ocr_processor",
        lambda cfg: SimpleNamespace(extract_data=fail_ocr),
    )
    worker = overlay_module.GPSWorker()
    regions, results = [], []
    worker.capture_region_changed.connect(regions.append)
    worker.result_ready.connect(results.append)
    worker.process()
    worker.process()
    assert len(regions) == 1
    assert regions[0] == source._region
    assert "error" in results[0]

    source._region["top"] = -100
    worker.process()
    assert len(regions) == 2
    assert regions[0]["top"] == 1440
    assert regions[1]["top"] == -100
    source._test_screenshot = "fixture.png"
    worker.process()
    assert regions[-1] is None


def test_outline_is_lazy_and_respects_setting_visibility_and_live_source(overlay_module, monkeypatch):
    instances = []

    class Outline:
        def __init__(self, parent):
            self.visible = False
            self.region = None
            instances.append(self)

        def set_capture_region(self, region):
            self.region = region

        def show(self):
            self.visible = True

        def hide(self):
            self.visible = False

    monkeypatch.setattr(overlay_module, "CaptureRegionOverlay", Outline)
    state = SimpleNamespace(
        enabled=False, is_visible=True, _capture_region_overlay=None,
        _capture_region={"left": 100, "top": 20, "width": 600, "height": 60},
    )
    state.config_manager = SimpleNamespace(get_show_capture_region=lambda: state.enabled)
    apply = lambda: overlay_module.GPSOverlay._apply_capture_region_setting(state)
    apply()
    assert not instances
    state.enabled = True
    apply()
    outline = instances[0]
    assert outline.visible and outline.region == state._capture_region
    state.is_visible = False
    apply()
    assert not outline.visible
    state.is_visible = True
    apply()
    assert outline.visible
    state.enabled = False
    apply()
    assert not outline.visible
    state.enabled = True
    state._capture_region = None
    apply()
    assert not outline.visible
    assert len(instances) == 1
