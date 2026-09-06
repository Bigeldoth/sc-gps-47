"""Capture geometry reaches the debug UI even when OCR cannot read a frame."""
import importlib.util
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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


def make_worker(module, monkeypatch, *, fail_ocr=False):
    class Source:
        def __init__(self):
            self.region = {"left": -600, "top": 1440, "width": 600, "height": 60}
            self.monitor_id = ''
            self.availability_error = None
            self.grabs = []
            self.lifecycle = []

        @property
        def screen_region(self):
            return dict(self.region) if self.region is not None else None

        @property
        def source_identity(self):
            return (self.monitor_id, tuple(self.region.values()) if self.region else None)

        def refresh_region(self):
            return False

        def configure_monitor(self, monitor_id):
            self.monitor_id = monitor_id

        def capture(self, *, refresh=True):
            assert refresh is False
            self.lifecycle.append(('grab', threading.get_ident()))
            self.grabs.append(self.screen_region)
            if self.availability_error:
                raise module.CaptureMonitorUnavailable(self.availability_error)
            return {}, None, 12.0

        def stop(self):
            self.lifecycle.append(('stop', threading.get_ident()))

    source = Source()
    ocr = SimpleNamespace(
        extract_data=Mock(side_effect=RuntimeError('No text engine available')) if fail_ocr
        else Mock(return_value={'x': 1.0, 'y': 2.0, 'z': 3.0}),
        reset_capture_state=Mock(), shutdown=Mock(),
    )

    def create_source(cfg):
        source.lifecycle.append(('create', threading.get_ident()))
        return source

    monkeypatch.setattr(module, 'ScreenCapture', create_source)
    monkeypatch.setattr(module, '_build_ocr_processor', lambda cfg: ocr)
    worker = module.GPSWorker()
    return worker, source, ocr


def acknowledge(worker, change):
    worker.acknowledge_capture_region(change['epoch'], change['generation'])


def test_worker_waits_for_outline_ack_before_grab_even_on_ocr_failure(overlay_module, monkeypatch):
    worker, source, ocr = make_worker(overlay_module, monkeypatch, fail_ocr=True)
    assert worker.capture is None and not source.lifecycle
    regions, results = [], []
    worker.capture_region_changed.connect(regions.append)
    worker.result_ready.connect(results.append)
    worker.process()
    worker.process()
    assert len(regions) == 1 and not source.grabs and not results
    assert regions[0]['region'] == source.region
    acknowledge(worker, regions[0])
    assert len(source.grabs) == 1
    assert "error" in results[0]

    source.region['top'] = -100
    worker.process()
    assert len(regions) == 2 and len(source.grabs) == 1
    assert regions[0]['region']['top'] == 1440
    assert regions[1]['region']['top'] == -100
    ocr.reset_capture_state.assert_called_once()
    acknowledge(worker, regions[-1])
    assert source.grabs[-1]['top'] == -100


def test_new_selection_ignores_old_ack_and_resumes_only_current_generation(overlay_module, monkeypatch):
    worker, source, _ = make_worker(overlay_module, monkeypatch)
    changes, results = [], []
    worker.capture_region_changed.connect(changes.append)
    worker.result_ready.connect(results.append)
    worker.process()
    old = changes[-1]
    worker.configure_capture('external-display', 1)
    acknowledge(worker, old)
    assert not source.grabs
    acknowledge(worker, changes[-1])
    assert len(source.grabs) == 1
    assert results[-1]['capture_epoch'] == 1
    assert source.monitor_id == 'external-display'


def test_saved_percent_in_display_id_is_decoded_at_worker_start(overlay_module, monkeypatch):
    overlay_module.config.add_section('Capture')
    overlay_module.config.set('Capture', 'monitor_id', 'display%%identifier')
    worker, source, _ = make_worker(overlay_module, monkeypatch)
    worker.process()
    assert source.monitor_id == 'display%identifier'


def test_monitor_change_resets_ocr_cache_without_rebuilding_models(overlay_module, monkeypatch):
    worker, _, ocr = make_worker(overlay_module, monkeypatch)
    factory = Mock(return_value=ocr)
    monkeypatch.setattr(overlay_module, '_build_ocr_processor', factory)
    changes = []
    worker.capture_region_changed.connect(changes.append)
    worker.process()
    acknowledge(worker, changes[-1])
    worker.configure_capture('external-display', 1)
    worker._reload_ocr()
    acknowledge(worker, changes[-1])
    assert factory.call_count == 1
    ocr.reset_capture_state.assert_called_once()
    overlay_module.config.add_section('OCR')
    overlay_module.config.set('OCR', 'text_engine', 'paddle')
    worker._reload_ocr()
    assert factory.call_count == 2


def test_disconnected_selection_clears_region_and_recovers_after_new_ack(overlay_module, monkeypatch):
    worker, source, _ = make_worker(overlay_module, monkeypatch)
    changes, results = [], []
    worker.capture_region_changed.connect(changes.append)
    worker.result_ready.connect(results.append)
    worker.process()
    acknowledge(worker, changes[-1])
    old_region = source.region
    source.region = None
    source.availability_error = 'Selected display is disconnected'
    worker.process()
    assert changes[-1]['region'] is None
    acknowledge(worker, changes[-1])
    assert results[-1]['capture_unavailable']
    assert len(source.grabs) == 1
    source.region = old_region
    source.availability_error = None
    worker.process()
    assert len(source.grabs) == 1
    acknowledge(worker, changes[-1])
    assert len(source.grabs) == 2 and 'error' not in results[-1]


def test_capture_lifecycle_stays_on_worker_thread(overlay_module, monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal

    app = QApplication.instance() or QApplication([])
    worker, source, _ = make_worker(overlay_module, monkeypatch)

    class Commands(QObject):
        scan = pyqtSignal()
        ack = pyqtSignal(int, int)
        stop = pyqtSignal()

    commands = Commands()
    thread = QThread()
    worker.moveToThread(thread)
    commands.scan.connect(worker.process, Qt.ConnectionType.QueuedConnection)
    commands.ack.connect(worker.acknowledge_capture_region, Qt.ConnectionType.QueuedConnection)
    commands.stop.connect(worker.stop, Qt.ConnectionType.QueuedConnection)
    worker.stopped.connect(thread.quit, Qt.ConnectionType.DirectConnection)
    worker.capture_region_changed.connect(
        lambda change: commands.ack.emit(change['epoch'], change['generation'])
    )
    results = []
    worker.result_ready.connect(results.append)
    thread.start()
    try:
        commands.scan.emit()
        deadline = time.monotonic() + 3
        while not results and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.002)
        assert results
    finally:
        commands.stop.emit()
        assert thread.wait(3000)
    assert [action for action, _ in source.lifecycle] == ['create', 'grab', 'stop']
    assert len({owner for _, owner in source.lifecycle}) == 1
    assert source.lifecycle[0][1] != threading.get_ident()


def test_old_inflight_result_does_not_release_new_selection_or_restore_position(overlay_module):
    state = SimpleNamespace(
        _quitting=False, _capture_epoch=2, _capture_generation=1,
        _worker_busy=True, current_data={'x': None},
    )
    overlay_module.GPSOverlay._on_worker_result(state, {
        'capture_epoch': 1, 'capture_generation': 1, 'x': 99,
    })
    assert state._worker_busy and state.current_data['x'] is None
    overlay_module.GPSOverlay._on_worker_result(state, {
        'capture_epoch': 2, 'capture_generation': 0, 'x': 99,
    })
    assert state._worker_busy and state.current_data['x'] is None


def test_missing_monitor_status_is_visible_even_without_a_target(overlay_module):
    state = SimpleNamespace(_capture_error='Disconnected', nav_label=Mock())
    overlay_module.GPSOverlay._refresh_nav_label(state)
    assert 'CAPTURE UNAVAILABLE' in state.nav_label.setText.call_args.args[0]


def test_saving_monitor_selection_invalidates_inflight_epoch_before_queued_change(overlay_module):
    state = SimpleNamespace(
        config_manager=SimpleNamespace(get_capture_monitor_id=lambda: 'second'),
        _capture_monitor_id='first', _capture_epoch=3, _capture_generation=9,
        _capture_outline_ready=True, _capture_region={'left': 0}, _capture_error=None,
        _worker_busy=False, _apply_capture_region_setting=Mock(),
        _reset_capture_navigation=Mock(), capture_selection_requested=Mock(),
    )
    overlay_module.GPSOverlay._apply_capture_monitor_setting(state)
    assert state._capture_epoch == 4 and state._capture_generation == 0
    assert state._worker_busy and state._capture_region is None
    assert state._capture_outline_ready is False
    state.capture_selection_requested.emit.assert_called_once_with('second', 4)
    overlay_module.GPSOverlay._apply_capture_monitor_setting(state)
    assert state.capture_selection_requested.emit.call_count == 1


def test_region_handshake_hides_outline_before_deferred_ack(overlay_module, monkeypatch):
    events, pending = [], []
    monkeypatch.setattr(overlay_module.QTimer, 'singleShot', lambda delay, callback: pending.append(callback))
    state = SimpleNamespace(
        _quitting=False, _capture_epoch=1, _capture_region_overlay=SimpleNamespace(
            hide=lambda: events.append('hide'),
        ),
        _reset_capture_navigation=lambda: events.append('reset'),
        _apply_capture_region_setting=lambda: events.append('apply'),
        capture_region_acknowledged=SimpleNamespace(emit=lambda *token: events.append(token)),
    )
    overlay_module.GPSOverlay._on_capture_region_changed(state, {
        'epoch': 1, 'generation': 2, 'region': {'left': -100}, 'error': None,
    })
    assert events == ['hide', 'reset', 'apply']
    assert state._capture_outline_ready is False
    pending[0]()
    assert events[-1] == (1, 2)


def test_capture_reset_clears_measurements_and_zone_lock_but_preserves_target(overlay_module):
    from navigation import NavigationEngine
    from velocity_tracker import VelocityTracker

    nav = NavigationEngine.__new__(NavigationEngine)
    nav.set_target(1, 2, 3, name='Keep destination', ooc='Stanton_1_Hurston')
    target = nav.target
    nav.update_zone_tracking({'ooc': 'Stanton_1_Hurston'})
    tracker = VelocityTracker()
    tracker.add_sample(100, 200, 300, 10)
    state = SimpleNamespace(
        current_data={'x': 100, 'y': 200, 'z': 300}, _pos_buffer=[(100, 200, 300)],
        _velocity_tracker=tracker, nav=nav, _capture_error=None,
        _last_coord_ts=10, _last_known_ooc='Stanton_1_Hurston',
        _save_snapshot={'x': 100}, _overlay_message=('old', 100),
        _last_vel_heading=90, _heading_unstable_until=20,
        pos_label=Mock(), poi_data_updated=Mock(),
        _refresh_pos_color=Mock(), _refresh_nav_label=Mock(),
    )
    overlay_module.GPSOverlay._reset_capture_navigation(state)
    assert nav.target is target and not nav._zone_match_locked
    assert not state._pos_buffer and tracker.position is None
    assert state.current_data['x'] is None and state._last_coord_ts is None
    assert state._last_known_ooc is None and state._save_snapshot is None
    assert state._smoothed_distance_km is None and state._smoothed_yaw_off is None
    assert state._last_vel_heading is None and state._heading_unstable_until == 0


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
        _capture_outline_ready=True,
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
    state._capture_region = {"left": 100, "top": 20, "width": 600, "height": 60}
    state._capture_outline_ready = False
    apply()
    assert not outline.visible
