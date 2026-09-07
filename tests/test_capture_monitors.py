"""Screen selection stays physical, persistent and safe across topology changes."""
import configparser
import copy
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import app_paths
import capture
from capture import CaptureMonitorUnavailable, ScreenCapture
from capture_monitors import list_capture_monitors


def display(unique_id, left=0, top=0, width=2560, height=1440, primary=False):
    return dict(unique_id=unique_id, name='Gaming monitor', left=left, top=top,
                width=width, height=height, is_primary=primary)


def settings(monitor_id=''):
    config = configparser.ConfigParser()
    config['Capture'] = {'monitor_id': monitor_id.replace('%', '%%')}
    return config


class FakeMSSFactory:
    """Model MSS's instance-local topology cache without reading the desktop."""

    def __init__(self):
        self.displays = [
            display('secondary', left=-1920, width=1920, height=1080),
            display('primary', primary=True),
        ]
        self.instances = []
        self.grabs = []
        self.fail_enumeration = False
        self.fail_grab = False

    def __call__(self):
        factory = self

        class FakeMSS:
            def __init__(self):
                self.closed = False
                self._monitors = [{}] + copy.deepcopy(factory.displays)

            @property
            def monitors(self):
                if factory.fail_enumeration:
                    raise RuntimeError('Display driver unavailable')
                return self._monitors

            def grab(self, region):
                assert not self.closed
                if factory.fail_grab:
                    raise RuntimeError('Display mode changed')
                factory.grabs.append(dict(region))
                return np.zeros((region['height'], region['width'], 4), dtype=np.uint8)

            def close(self):
                self.closed = True

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        instance = FakeMSS()
        self.instances.append(instance)
        return instance


@pytest.fixture
def screens(monkeypatch):
    factory = FakeMSSFactory()
    monkeypatch.setattr(capture.mss, 'mss', factory)
    return factory


def test_catalog_has_stable_identity_native_label_and_physical_coordinates(screens):
    monitors = list_capture_monitors()
    assert [m['id'] for m in monitors] == ['monitor:secondary', 'monitor:primary']
    assert monitors[0]['left'] == -1920
    assert 'Gaming monitor' in monitors[1]['label']
    assert '2560 × 1440' in monitors[1]['label']
    assert 'Primary' in monitors[1]['label']
    assert all(instance.closed for instance in screens.instances)


def test_primary_choice_uses_primary_flag_instead_of_mss_array_position(screens):
    source = ScreenCapture(settings())
    try:
        assert source.screen_region == dict(left=1760, top=0, width=800, height=60)
        images, _, _ = source.capture()
        assert screens.grabs == [source.screen_region]
        assert images['raw'].shape == (60, 800, 3)
        assert images['otsu'].shape == (180, 2400)
    finally:
        source.stop()
    assert all(instance.closed for instance in screens.instances)


def test_manual_negative_monitor_survives_list_reordering_and_layout_change(screens):
    source = ScreenCapture(settings('monitor:secondary'))
    try:
        assert source.screen_region == dict(left=-600, top=0, width=600, height=60)
        identity = source.source_identity
        screens.displays.reverse()
        assert source.refresh_region(force=True) is False
        assert source.source_identity == identity
        screens.displays[1].update(left=-1920, top=-1080)
        assert source.refresh_region(force=True) is True
        assert source.screen_region == dict(left=-600, top=-1080, width=600, height=60)
    finally:
        source.stop()


def test_selected_monitor_disconnection_pauses_and_reconnection_resumes(screens):
    source = ScreenCapture(settings('monitor:secondary'))
    try:
        secondary = screens.displays.pop(0)
        assert source.refresh_region(force=True) is True
        assert source.monitor_id == 'monitor:secondary'
        assert source.screen_region is None
        assert source.availability_error == 'Selected display unavailable'
        with pytest.raises(CaptureMonitorUnavailable, match='Selected display unavailable'):
            source.capture(refresh=False)
        assert not screens.grabs
        assert all(instance.closed for instance in screens.instances)
        screens.displays.append(secondary)
        assert source.refresh_region(force=True) is True
        assert source.availability_error is None
        source.capture(refresh=False)
        assert screens.grabs[-1]['left'] == -600
    finally:
        source.stop()


def test_manual_missing_id_at_start_never_captures_primary(screens):
    source = ScreenCapture(settings('monitor:absent'))
    assert source.screen_region is None
    with pytest.raises(CaptureMonitorUnavailable):
        source.capture()
    assert not screens.grabs
    assert all(instance.closed for instance in screens.instances)


def test_geometry_fallback_is_deterministic_and_does_not_follow_another_screen(screens):
    screens.displays[0].pop('unique_id')
    choice = list_capture_monitors()[0]['id']
    assert choice == 'geometry:-1920:0:1920:1080'
    source = ScreenCapture(settings(choice))
    try:
        screens.displays.reverse()
        assert source.refresh_region(force=True) is False
        screens.displays[1]['left'] = -2000
        assert source.refresh_region(force=True) is True
        assert source.screen_region is None
        assert source.monitor_id == choice
    finally:
        source.stop()


def test_small_display_clamps_capture_to_its_own_bounds(screens):
    screens.displays = [display('small', left=-320, top=-40, width=320, height=40, primary=True)]
    source = ScreenCapture(settings())
    try:
        assert source.screen_region == dict(left=-100, top=-40, width=100, height=40)
    finally:
        source.stop()


@pytest.mark.parametrize('resolution, crop_width', [
    (1920, 600), (2560, 800), (3440, 1075), (3840, 1200),
    (1, 1),
])
def test_horizontal_scaling_stays_inside_selected_display(screens, resolution, crop_width):
    screens.displays = [display('game', left=-3840, top=1440,
                                width=resolution, height=2160, primary=True)]
    source = ScreenCapture(settings())
    try:
        assert source.screen_region == dict(
            left=-3840 + resolution - crop_width,
            top=1440, width=crop_width, height=60,
        )
        region = source.screen_region
        assert region['left'] + region['width'] == -3840 + resolution
    finally:
        source.stop()


def test_resolution_change_recomputes_width_and_offset(screens):
    source = ScreenCapture(settings())
    try:
        screens.displays[1]['width'] = 3840
        assert source.refresh_region(force=True) is True
        assert source.screen_region == dict(left=2640, top=0, width=1200, height=60)
        images, _, _ = source.capture(refresh=False)
        assert screens.grabs[-1] == source.screen_region
        assert images['raw'].shape == (60, 1200, 3)
    finally:
        source.stop()


def test_live_selection_changes_identity_even_with_identical_bounds(screens):
    source = ScreenCapture(settings())
    try:
        region = source.screen_region
        assert source.configure_monitor('monitor:primary') is True
        assert source.screen_region == region
        assert source.monitor_id == 'monitor:primary'
        assert source.configure_monitor('monitor:secondary') is True
        source.capture(refresh=False)
        assert screens.grabs[-1]['left'] == -600
    finally:
        source.stop()


def test_topology_refresh_is_throttled_but_rechecks_after_one_second(screens, monkeypatch):
    now = [10.0]
    monkeypatch.setattr(capture.time, 'monotonic', lambda: now[0])
    source = ScreenCapture(settings())
    try:
        instance_count = len(screens.instances)
        source.refresh_region()
        assert len(screens.instances) == instance_count
        screens.displays[1]['top'] = -1440
        now[0] += 1.1
        assert source.refresh_region() is True
        assert source.screen_region['top'] == -1440
    finally:
        source.stop()


def test_display_error_clears_region_and_recovers_with_fresh_handle(screens):
    source = ScreenCapture(settings())
    try:
        screens.fail_grab = True
        with pytest.raises(CaptureMonitorUnavailable, match='Capture display unavailable'):
            source.capture(refresh=False)
        assert source.screen_region is None
        assert all(instance.closed for instance in screens.instances)
        screens.fail_grab = False
        source.capture()
        assert source.screen_region is not None
    finally:
        source.stop()


def test_enumeration_failure_is_retryable_and_closes_all_resources(screens):
    screens.fail_enumeration = True
    source = ScreenCapture(settings())
    assert source.screen_region is None
    assert source.availability_error == 'Capture displays unavailable'
    assert all(instance.closed for instance in screens.instances)
    screens.fail_enumeration = False
    try:
        assert source.refresh_region(force=True) is True
        assert source.screen_region is not None
    finally:
        source.stop()


def test_screenshot_mode_never_initializes_mss_and_preserves_preprocessing(monkeypatch):
    def reject_mss():
        raise AssertionError('Image mode must not enumerate or capture displays')

    monkeypatch.setattr(capture.mss, 'mss', reject_mss)
    monkeypatch.setattr(capture.cv2, 'imread', lambda _: np.zeros((1440, 2560, 3), dtype=np.uint8))
    config = settings('monitor:absent')
    config['Debug'] = {'test_screenshot': 'fixture.png'}
    source = ScreenCapture(config)
    assert source.screen_region is None
    assert source.source_identity == ('screenshot', 'fixture.png', 1760, 0, 800, 60)
    assert source.configure_monitor('monitor:primary') is False
    images, _, _ = source.capture()
    assert images['raw'].shape == (60, 800, 3)
    assert images['otsu'].shape == (180, 2400)
    source.stop()


def test_standalone_config_reads_user_override_with_bom_independent_of_cwd(screens, tmp_path, monkeypatch):
    bundle = tmp_path / 'bundle'
    user = tmp_path / 'user'
    unrelated = tmp_path / 'cwd'
    for path in (bundle, user, unrelated):
        path.mkdir()
    (bundle / 'config.ini').write_text('[Capture]\nmonitor_id = monitor:primary\n', encoding='utf-8')
    (user / 'config.ini').write_text('[Capture]\nmonitor_id = monitor:secondary\n', encoding='utf-8-sig')
    (unrelated / 'config.ini').write_text('[Capture]\nmonitor_id = monitor:wrong\n', encoding='utf-8')
    monkeypatch.setattr(app_paths, 'bundle_dir', lambda: bundle)
    monkeypatch.setattr(app_paths, 'user_data_dir', lambda: user)
    monkeypatch.chdir(unrelated)
    source = ScreenCapture()
    try:
        assert source.monitor_id == 'monitor:secondary'
        assert source.screen_region['left'] == -600
    finally:
        source.stop()


def test_native_monitor_id_preserves_percent_characters(screens):
    screens.displays[0]['unique_id'] = 'screen%20name'
    source = ScreenCapture(settings('monitor:screen%20name'))
    try:
        assert source.monitor_id == 'monitor:screen%20name'
        assert source.screen_region['left'] == -600
    finally:
        source.stop()
