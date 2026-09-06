"""Capture settings migrate safely and keep the debug outline opt-in."""
import configparser
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app_paths
from config_manager import ConfigManager


@pytest.fixture
def isolated_config_path(tmp_path, monkeypatch):
    """Redirect both config locations before creating a manager."""
    bundle = tmp_path / "bundle"
    user = tmp_path / "user"
    bundle.mkdir()
    user.mkdir()
    monkeypatch.setattr(app_paths, "bundle_dir", lambda: bundle)
    monkeypatch.setattr(app_paths, "user_data_dir", lambda: user)
    return user / "config.ini"


@pytest.mark.parametrize("contents", ["", "[Debug]\nverbose_mode = False\n"])
def test_new_and_existing_configs_keep_capture_outline_disabled(isolated_config_path, contents):
    if contents:
        isolated_config_path.write_text(contents, encoding="utf-8")

    cfg = ConfigManager()

    assert cfg.get_show_capture_region() is False
    saved = configparser.ConfigParser()
    saved.read(isolated_config_path, encoding="utf-8")
    assert saved.getboolean("Debug", "show_capture_region") is False


def test_capture_outline_choice_survives_reload_and_migration(isolated_config_path):
    cfg = ConfigManager()
    cfg.set_show_capture_region(True)
    assert cfg.save()

    reloaded = ConfigManager()
    assert reloaded.get_show_capture_region() is True

    reloaded.set_show_capture_region(False)
    assert reloaded.save()
    assert ConfigManager().get_show_capture_region() is False


@pytest.mark.parametrize("value", ["invalid", "", "%(missing)s"])
def test_invalid_capture_outline_values_remain_disabled(isolated_config_path, value):
    isolated_config_path.write_text(
        f"[Debug]\nshow_capture_region = {value}\n", encoding="utf-8"
    )

    assert ConfigManager().get_show_capture_region() is False


@pytest.mark.parametrize("contents", ["", "[Debug]\nverbose_mode = False\n"])
def test_capture_monitor_defaults_to_primary_for_new_and_existing_configs(
    isolated_config_path, contents
):
    if contents:
        isolated_config_path.write_text(contents, encoding="utf-8")

    cfg = ConfigManager()

    assert cfg.get_capture_monitor_id() == ""
    saved = configparser.ConfigParser()
    saved.read(isolated_config_path, encoding="utf-8")
    assert saved.get("Capture", "monitor_id") == ""
    assert cfg.get_show_capture_region() is False


@pytest.mark.parametrize("monitor_id", ["display-secondary", "display%2", "display%(id)s"])
def test_capture_monitor_selection_survives_reload_without_enabling_debug(
    isolated_config_path, monitor_id
):
    cfg = ConfigManager()
    cfg.set_capture_monitor_id(monitor_id)
    assert cfg.save()

    reloaded = ConfigManager()
    assert reloaded.get_capture_monitor_id() == monitor_id
    assert reloaded.get_show_capture_region() is False

    reloaded.set_capture_monitor_id("")
    assert reloaded.save()
    assert ConfigManager().get_capture_monitor_id() == ""


def test_capture_monitor_migration_keeps_unavailable_display_preference(isolated_config_path):
    isolated_config_path.write_text(
        "[Capture]\nmonitor_id = disconnected-display\n", encoding="utf-8"
    )

    assert ConfigManager().get_capture_monitor_id() == "disconnected-display"


def test_manually_written_display_id_keeps_literal_percent(isolated_config_path):
    isolated_config_path.write_text("[Capture]\nmonitor_id = display%2\n", encoding="utf-8")

    assert ConfigManager().get_capture_monitor_id() == "display%2"
