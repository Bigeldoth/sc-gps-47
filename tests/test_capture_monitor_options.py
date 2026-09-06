"""Display choices survive refresh, disconnects and enumeration failures."""

import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PyQt6.QtWidgets import QApplication, QMessageBox

import app_paths
from config_manager import ConfigManager
from ui import options


DISPLAYS = [
    {"id": "primary", "label": "Main display — 2560 × 1440 — Primary",
     "left": 0, "top": 0, "width": 2560, "height": 1440, "is_primary": True},
    {"id": "secondary", "label": "Second display — 2560 × 1600",
     "left": -2560, "top": 0, "width": 2560, "height": 1600, "is_primary": False},
]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window_factory(app, tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    user = tmp_path / "user"
    bundle.mkdir()
    user.mkdir()
    monkeypatch.setattr(app_paths, "bundle_dir", lambda: bundle)
    monkeypatch.setattr(app_paths, "user_data_dir", lambda: user)
    monkeypatch.setattr(options, "list_capture_monitors", lambda: list(DISPLAYS))
    monkeypatch.setattr(options.OptionsWindow, "_start_gpu_availability_probe", lambda self: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    windows = []

    def create(monitor_id=""):
        config = ConfigManager()
        config.set_capture_monitor_id(monitor_id)
        assert config.save()
        hotkeys = SimpleNamespace(update_hotkey=lambda *args: None)
        window = options.OptionsWindow(config, hotkeys)
        windows.append(window)
        return window

    yield create

    for window in windows:
        window.close()
        window.deleteLater()
    app.processEvents()


def test_manual_display_choice_is_available_with_debug_disabled_and_persists(window_factory):
    window = window_factory()
    combo = window.capture_monitor_combo
    assert combo.currentData() == ""
    assert combo.itemText(0) == "Primary display (default)"
    assert combo.isEnabled()
    assert not window.show_capture_region_check.isChecked()

    combo.setCurrentIndex(combo.findData("secondary"))
    window._save_and_close()

    config = ConfigManager()
    assert config.get_capture_monitor_id() == "secondary"
    assert config.get_show_capture_region() is False


def test_refresh_keeps_unsaved_choice_when_display_order_changes(window_factory, monkeypatch):
    window = window_factory("primary")
    combo = window.capture_monitor_combo
    combo.setCurrentIndex(combo.findData("secondary"))
    monkeypatch.setattr(options, "list_capture_monitors", lambda: list(reversed(DISPLAYS)))

    window.refresh_displays_button.click()

    assert combo.currentData() == "secondary"
    assert ConfigManager().get_capture_monitor_id() == "primary"


def test_missing_saved_display_is_visible_and_stays_selected_on_save(window_factory):
    window = window_factory("disconnected")
    assert window.capture_monitor_combo.currentData() == "disconnected"
    assert "Unavailable" in window.capture_monitor_combo.currentText()
    assert "unavailable" in window.capture_monitor_status.text()

    window._save_and_close()

    assert ConfigManager().get_capture_monitor_id() == "disconnected"


def test_selecting_connected_display_clears_unavailable_status(window_factory):
    window = window_factory("disconnected")
    combo = window.capture_monitor_combo

    combo.setCurrentIndex(combo.findData("secondary"))

    assert window.capture_monitor_status.text() == ""
    combo.setCurrentIndex(combo.findData("disconnected"))
    assert "unavailable" in window.capture_monitor_status.text()


def test_disconnect_and_reconnect_keep_the_pending_display_choice(window_factory, monkeypatch):
    window = window_factory()
    combo = window.capture_monitor_combo
    combo.setCurrentIndex(combo.findData("secondary"))
    monkeypatch.setattr(options, "list_capture_monitors", lambda: DISPLAYS[:1])

    window.refresh_displays_button.click()

    assert combo.currentData() == "secondary"
    assert "Unavailable" in combo.currentText()
    monkeypatch.setattr(options, "list_capture_monitors", lambda: list(DISPLAYS))

    window.refresh_displays_button.click()

    assert combo.currentData() == "secondary"
    assert combo.currentText() == DISPLAYS[1]["label"]
    assert window.capture_monitor_status.text() == ""


def test_refresh_failure_preserves_pending_choice_and_shows_status(window_factory, monkeypatch):
    window = window_factory()
    combo = window.capture_monitor_combo
    combo.setCurrentIndex(combo.findData("secondary"))

    def fail():
        raise OSError("Display enumeration failed")

    monkeypatch.setattr(options, "list_capture_monitors", fail)
    window.refresh_displays_button.click()

    assert combo.currentData() == "secondary"
    assert combo.currentText() == DISPLAYS[1]["label"]
    assert "Could not refresh displays" in window.capture_monitor_status.text()


def test_initial_enumeration_failure_preserves_saved_display(window_factory, monkeypatch):
    def fail():
        raise OSError("Display enumeration failed")

    monkeypatch.setattr(options, "list_capture_monitors", fail)

    window = window_factory("disconnected")

    assert window.capture_monitor_combo.currentData() == "disconnected"
    assert "Unavailable" in window.capture_monitor_combo.currentText()
    assert "Could not refresh displays" in window.capture_monitor_status.text()
