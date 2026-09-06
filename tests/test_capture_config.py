"""The capture-area debug outline remains opt-in across config upgrades."""
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
    monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
    monkeypatch.setattr(app_paths, "user_data_dir", lambda: tmp_path)
    return tmp_path / "config.ini"


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
