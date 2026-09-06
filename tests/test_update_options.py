"""Update UI failures, compatibility and shutdown never claim early success."""

import importlib.util
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

import app_paths
from config_manager import ConfigManager
from ui import options


INSTALLER_URL = "https://padek-interactive.tech/releases/SpaceDrive-Setup-v1.1.0.exe"
MANIFEST = {
    "version": "v1.1.0", "url": INSTALLER_URL,
    "delta_v2": {"available": True, "base_version": "v1.0.0", "compatible": True,
                 "url": "https://padek-interactive.tech/releases/delta.zip",
                 "checksum": "a" * 64, "size_bytes": 4096},
}


class OfferManager:
    """Model backend decisions so the UI cannot trust delta.available alone."""

    def __init__(self, *args):
        pass

    def can_apply_delta(self, manifest):
        return bool(manifest.get("delta_v2", {}).get("compatible"))

    def get_delta_info(self, manifest):
        return manifest.get("delta_v2", {}) if self.can_apply_delta(manifest) else {}

    def get_full_installer_url(self, manifest):
        return manifest.get("url", "") if manifest.get("url") == INSTALLER_URL else ""


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    bundle, user = tmp_path / "bundle", tmp_path / "user"
    bundle.mkdir()
    user.mkdir()
    monkeypatch.setattr(app_paths, "bundle_dir", lambda: bundle)
    monkeypatch.setattr(app_paths, "user_data_dir", lambda: user)
    return bundle, user


@pytest.fixture
def backend(isolated_paths, monkeypatch):
    import update_manager
    monkeypatch.setattr(update_manager, "UpdateManager", OfferManager)
    monkeypatch.setattr(update_manager, "UPDATE_PENDING", "pending_exit", raising=False)
    return update_manager


@pytest.fixture
def window(app, backend, monkeypatch):
    monkeypatch.setattr(options, "list_capture_monitors", lambda: [])
    monkeypatch.setattr(options.OptionsWindow, "_start_gpu_availability_probe", lambda self: None)
    monkeypatch.setattr(options.OptionsWindow, "_get_current_app_version", lambda self: "v1.0.0")
    cfg = ConfigManager()
    hotkeys = SimpleNamespace(update_hotkey=lambda *args: None)
    dialog = options.OptionsWindow(cfg, hotkeys)
    yield dialog
    dialog._update_busy = False
    dialog._update_prepared = False
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_check_failure_is_distinct_from_up_to_date(backend, monkeypatch):
    class FailingManager(OfferManager):
        def check_for_update(self):
            raise ValueError("Invalid release manifest")

    monkeypatch.setattr(backend, "UpdateManager", FailingManager)
    worker = options._UpdateCheckThread("v1.0.0")
    successes, failures = [], []
    worker.check_complete.connect(lambda *value: successes.append(value))
    worker.check_failed.connect(failures.append)

    worker.run()

    assert successes == []
    assert failures == ["Invalid release manifest"]


@pytest.mark.parametrize("outcome", ["failure", "up_to_date"])
def test_new_result_discards_every_previous_download(window, monkeypatch, outcome):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    window._on_update_check_complete("v1.1.0", MANIFEST)
    assert window._latest_delta_url
    if outcome == "failure":
        window._on_update_check_failed("Network unavailable")
        assert window.latest_version_label.text() == "Check failed"
    else:
        window._on_update_check_complete("", {})
        assert window.latest_version_label.text() == "Up to date"
    assert window._available_update_version == ""
    assert window._latest_full_url == ""
    assert window._latest_delta_url == ""
    assert window._latest_delta_checksum == ""
    assert not window.update_now_button.isEnabled()
    assert not window.skip_button.isEnabled()


def test_incompatible_available_delta_uses_validated_full_installer(window, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    manifest = {**MANIFEST, "delta_v2": {**MANIFEST["delta_v2"], "compatible": False}}

    window._on_update_check_complete("v1.1.0", manifest)

    assert window._latest_delta_url == ""
    assert window._latest_full_url == INSTALLER_URL
    assert window.update_now_button.text() == "Download Installer"


def test_development_checkout_uses_installer_even_when_delta_is_compatible(window, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    window._on_update_check_complete("v1.1.0", MANIFEST)

    assert window._latest_delta_url == ""
    assert "source checkout" in window.update_status_label.text()


def test_invalid_fallback_url_never_opens_browser(window, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    calls = []
    monkeypatch.setattr(options.QDesktopServices, "openUrl", calls.append)
    manifest = {**MANIFEST, "url": "file:///malicious.exe"}

    window._on_update_check_complete("v1.1.0", manifest)
    window._open_full_installer()

    assert calls == []
    assert not window.update_now_button.isEnabled()


def test_prepared_update_waits_for_thread_finish_and_requests_clean_shutdown(window):
    shutdown = []
    window.shutdown_for_update_requested.connect(lambda: shutdown.append(True))
    config_before = Path(window.config_manager.config_path).read_bytes()
    window._update_busy = True

    window._on_update_complete(True, "pending_exit")

    assert shutdown == []
    assert window._update_busy is True
    window._on_update_thread_finished()
    assert shutdown == [True]
    assert "prepared" in window.update_status_label.text().lower()
    assert "completed" not in window.update_status_label.text().lower()
    assert Path(window.config_manager.config_path).read_bytes() == config_before
    assert window.current_version_label.text() == "v1.0.0"


def test_failed_preparation_keeps_application_open(window):
    shutdown = []
    window.shutdown_for_update_requested.connect(lambda: shutdown.append(True))
    window._on_update_complete(False, "Checksum mismatch")
    window._on_update_thread_finished()
    assert shutdown == []
    assert window.save_button.isEnabled()
    assert "Checksum mismatch" in window.update_status_label.text()


def test_apply_worker_preserves_pending_state_and_verifies_before_apply(backend, monkeypatch, tmp_path):
    calls, results = [], []

    class PreparingManager(OfferManager):
        def set_update_manifest(self, manifest):
            calls.append("manifest")
            assert manifest is MANIFEST

        def download_delta(self, *args):
            calls.append("download")
            return tmp_path / "delta.zip"

        def verify_delta(self, *args):
            calls.append("verify")
            return True

        def apply_delta(self, *args):
            calls.append("prepare")
            return True, "pending_exit"

    monkeypatch.setattr(backend, "UpdateManager", PreparingManager)
    worker = options._UpdateApplyThread(None, "v1.0.0", "v1.1.0", "url", "a" * 64, MANIFEST)
    worker.complete.connect(lambda *result: results.append(result))
    worker.run()
    assert calls == ["manifest", "download", "verify", "prepare"]
    assert results == [(True, "pending_exit")]


def test_dialog_cannot_close_during_preparation(window):
    window.show()
    window._update_busy = True
    window.reject()
    assert window.isVisible()
    assert not window.close()
    window._update_busy = False
    assert window.close()


def test_closed_dialog_does_not_own_or_destroy_running_worker(window, app):
    gate = threading.Event()

    class WaitingThread(QThread):
        def run(self):
            gate.wait(3)

    worker = options.retain_background_task(WaitingThread())
    window._update_check_thread = worker
    worker.start()
    try:
        assert worker.parent() is app
        window.close()
        assert options.background_tasks_running()
        assert worker in options._BACKGROUND_TASKS
    finally:
        gate.set()
        assert worker.wait(3000)
        app.processEvents()
    assert worker not in options._BACKGROUND_TASKS


@pytest.mark.parametrize("action", ["resume", "rollback", "installer"])
def test_recovery_calls_the_selected_backend_operation(action):
    calls, results = [], []
    metadata = object()
    manager = SimpleNamespace(
        resume_interrupted_update=lambda: (calls.append("resume") or True, "pending_exit"),
        rollback_interrupted_update=lambda: (calls.append("rollback") or True, "pending_exit"),
        get_recovery_installer_url=lambda selected: calls.append(("installer", selected)) or INSTALLER_URL,
    )
    worker = options._UpdateRecoveryThread(manager, action, metadata)
    worker.complete.connect(lambda *result: results.append(result))
    worker.run()
    assert calls == ([("installer", metadata)] if action == "installer" else [action])
    assert results == [(True, INSTALLER_URL if action == "installer" else "pending_exit")]


@pytest.fixture
def main_module(isolated_paths):
    spec = importlib.util.spec_from_file_location("update_ui_test_main", ROOT / "src/main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shutdown_waits_for_options_workers_before_finishing(main_module, monkeypatch):
    calls = []
    overlay = SimpleNamespace(
        _quitting=True,
        _finish_quit=lambda: None,
        _telemetry=SimpleNamespace(close=lambda: calls.append("telemetry")),
        tray_icon=SimpleNamespace(hide=lambda: calls.append("tray")),
    )
    monkeypatch.setattr(main_module, "background_tasks_running", lambda: True)
    monkeypatch.setattr(main_module.QTimer, "singleShot", lambda delay, callback: calls.append("wait"))
    monkeypatch.setattr(main_module.QApplication, "quit", lambda: calls.append("quit"))
    main_module.GPSOverlay._finish_quit(overlay)
    assert calls == ["wait"]
    calls.clear()
    monkeypatch.setattr(main_module, "background_tasks_running", lambda: False)
    main_module.GPSOverlay._finish_quit(overlay)
    assert calls == ["telemetry", "tray", "quit"]


def test_recovery_pending_uses_the_normal_shutdown_path(main_module, backend):
    calls = []
    overlay = SimpleNamespace(
        _recovery_result=(True, "pending_exit"),
        _quitting=False,
        quit_application=lambda: calls.append("clean_shutdown"),
    )

    main_module.GPSOverlay._on_update_recovery_finished(overlay)

    assert calls == ["clean_shutdown"]
    assert overlay._recovery_busy is False


def test_startup_with_active_helper_releases_application_files(main_module, backend, monkeypatch):
    calls = []

    class ActiveManager(OfferManager):
        def detect_incomplete_update(self):
            return SimpleNamespace(state="applying")

        def is_update_running(self, metadata):
            return True

    monkeypatch.setattr(backend, "UpdateManager", ActiveManager)
    overlay = SimpleNamespace(
        config_manager=None,
        _get_current_app_version=lambda: "v1.0.0",
        tray_icon=SimpleNamespace(showMessage=lambda *args: calls.append("notice")),
        quit_application=lambda: calls.append("clean_shutdown"),
    )
    main_module.GPSOverlay._check_incomplete_update(overlay)
    assert calls == ["notice", "clean_shutdown"]


def test_startup_check_is_opt_in_and_persists_from_options(window, monkeypatch):
    assert window.config_manager.get_check_on_startup() is False
    assert not window.check_updates_on_startup.isChecked()
    monkeypatch.setattr(options.QMessageBox, "information", lambda *args: None)
    window.check_updates_on_startup.setChecked(True)
    window._save_and_close()
    assert ConfigManager().get_check_on_startup() is True


@pytest.mark.parametrize("enabled,pending,running,expected", [
    (False, False, False, 0),
    (True, False, False, 1),
    (True, True, False, 0),
    (True, False, True, 0),
])
def test_startup_network_worker_requires_opt_in_and_no_recovery(
    main_module, monkeypatch, enabled, pending, running, expected
):
    started = []

    class CheckThread:
        def __init__(self, version):
            self.check_complete = SimpleNamespace(connect=lambda callback: None)
            self.check_failed = SimpleNamespace(connect=lambda callback: None)
            self.finished = SimpleNamespace(connect=lambda callback: None)

        def start(self):
            started.append(True)

    monkeypatch.setattr(main_module, "_UpdateCheckThread", CheckThread)
    monkeypatch.setattr(main_module, "retain_background_task", lambda worker: worker)
    config = ConfigManager()
    if enabled:
        config.set_check_on_startup(True)
    overlay = SimpleNamespace(
        _quitting=False, config_manager=config,
        _get_current_app_version=lambda: "v1.0.0",
        _on_startup_update_available=lambda *args: None,
        _on_startup_update_failed=lambda *args: None,
        _on_startup_update_finished=lambda: None,
    )
    manager = SimpleNamespace(
        detect_incomplete_update=lambda: object() if pending else None,
        is_update_running=lambda: running,
    )
    main_module.GPSOverlay._check_startup_update(overlay, manager)
    main_module.GPSOverlay._check_startup_update(overlay, manager)
    assert len(started) == expected


@pytest.mark.parametrize("skipped,expected", [("", 1), ("v1.1.0", 0), ("1.1.0", 0)])
def test_startup_notification_respects_skipped_version(main_module, skipped, expected):
    notices = []
    config = ConfigManager()
    config.set_check_on_startup(True)
    config.set_last_skipped_version(skipped)
    overlay = SimpleNamespace(
        _quitting=False, config_manager=config,
        tray_icon=SimpleNamespace(showMessage=lambda *args: notices.append(args)),
    )
    main_module.GPSOverlay._on_startup_update_available(overlay, "v1.1.0", MANIFEST)
    assert len(notices) == expected
    if notices:
        assert "Open Options > Updates" in notices[0][1]


def test_startup_network_error_is_logged_without_notification(main_module, monkeypatch):
    logs = []
    monkeypatch.setattr(main_module.logger, "warning", lambda *args: logs.append(args))
    main_module.GPSOverlay._on_startup_update_failed(SimpleNamespace(), "Network unavailable")
    assert logs == [("Automatic update check failed: %s", "Network unavailable")]


def test_manual_check_still_offers_a_skipped_version(window, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    window.config_manager.set_last_skipped_version("v1.1.0")
    window._on_update_check_complete("v1.1.0", MANIFEST)
    assert window.update_now_button.isEnabled()
    assert window._available_update_version == "v1.1.0"
