"""Update checks and transactions operate only inside explicit temporary roots."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import update_manager as updater
import update_transaction as transaction
from update_manager import UpdateError, UpdateManager, UpdateMetadata, _version_compare

URL = updater.RELEASES_BASE_URL


@pytest.fixture(autouse=True)
def isolated_default_paths(tmp_path, monkeypatch):
    app = tmp_path / 'installation'
    app.mkdir()
    (app / '_internal').mkdir()
    (app / '_internal' / 'VERSION').write_text('1.0.0\n', encoding='utf-8')
    monkeypatch.setattr(updater, 'install_dir', lambda: app)
    monkeypatch.setattr(updater, 'user_data_dir', lambda: tmp_path / 'userdata')
    monkeypatch.setattr(updater, 'bundle_dir', lambda: ROOT)
    monkeypatch.delattr(sys, 'frozen', raising=False)
    return app, tmp_path / 'userdata'


@pytest.fixture
def manager(isolated_default_paths):
    app, data = isolated_default_paths
    return UpdateManager(SimpleNamespace(), 'v1.0.0', install_path=app, data_path=data)


def make_delta(path, files=None, deleted=None, *, source='v1.0.0', target='v1.1.0', extras=None):
    files = dict(files if files is not None else {'app.txt': b'updated', 'new/deep/added.txt': b'new'})
    files.setdefault('_internal/VERSION', target.removeprefix('v').encode() + b'\n')
    members = {
        'manifest.json': json.dumps({'schema_version': 2, 'from_version': source, 'to_version': target}).encode(),
        'version.txt': target.encode(),
        'checksum.sha256': ''.join(f'{hashlib.sha256(content).hexdigest()}  {name}\n' for name, content in files.items()).encode(),
        'DELETED.txt': '\n'.join(deleted or []).encode(),
        **{f'FILES/{name}': content for name, content in files.items()},
    }
    members.update(extras or {})
    with zipfile.ZipFile(path, 'w') as package:
        for name, content in members.items():
            package.writestr(name, content)
    return path


def checked_apply(manager, archive, target='v1.1.0'):
    assert manager.verify_delta(archive, transaction.sha256_file(archive))
    return manager.apply_delta(target, archive)


def stage_plan(manager, archive, monkeypatch):
    with monkeypatch.context() as patcher:
        patcher.setattr(manager, '_execute_plan', lambda plan, metadata: (True, 'prepared'))
        assert checked_apply(manager, archive)[0]
    return manager._read_plan()


def run_helper(manager, plan, *, parent_pid=2147483647, started=0, timeout=10, background=False):
    plan = copy.deepcopy(plan)
    plan.update(parent_pid=parent_pid, parent_started_filetime=started, wait_timeout_seconds=timeout)
    metadata = UpdateMetadata.from_file(manager.metadata_file)
    path = manager._save_plan(plan, metadata)
    command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
               '-File', str(ROOT / 'scripts' / 'apply_update.ps1'), '-PlanPath', str(path),
               '-PlanSha256', transaction.sha256_file(path)]
    if background:
        return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return subprocess.run(command, capture_output=True, text=True, timeout=30,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


class Response(io.BytesIO):
    def __init__(self, content, url=URL + '/delta.zip', length=None):
        super().__init__(content)
        self.headers = {'Content-Length': str(len(content) if length is None else length)}
        self.url = url

    def geturl(self):
        return self.url


def release_manifest(**delta):
    return {'version': 'v1.1.0', 'url': URL + '/SpaceDrive-Setup-v1.1.0.exe',
            'delta': {'available': False}, 'delta_v2': {
                'available': True, 'from_version': 'v1.0.0', 'to_version': 'v1.1.0',
                'url': URL + '/delta.zip', 'checksum': 'a' * 64, **delta}}


def test_check_network_error_is_not_up_to_date(manager, monkeypatch):
    def offline(*args, **kwargs):
        raise OSError('offline')
    monkeypatch.setattr(updater.urllib.request, 'urlopen', offline)
    with pytest.raises(UpdateError, match='Could not check'):
        manager.check_for_update()


def test_check_new_and_equal_versions(manager, monkeypatch):
    payload = release_manifest()
    monkeypatch.setattr(updater.urllib.request, 'urlopen', lambda *a, **k: Response(json.dumps(payload).encode(), url=updater.LATEST_JSON_URL))
    assert manager.check_for_update()[0] == 'v1.1.0'
    manager.current_version = 'v1.1.0'
    assert manager.check_for_update() == (None, None)


@pytest.mark.parametrize('version', ['../x', 'vv1.2.3', '1.2', '1.2.3/../../x', 'invalid', 'v01.2.3', None])
def test_versions_never_fall_back_to_lexical_order(version):
    with pytest.raises(UpdateError):
        _version_compare(version, 'v1.0.0')


def test_delta_requires_exact_base_and_valid_checksum(manager):
    assert manager.can_apply_delta(release_manifest())
    assert not manager.can_apply_delta(release_manifest(from_version='v0.9.0'))
    assert not manager.can_apply_delta(release_manifest(from_version='v1.0.1'))
    assert not manager.can_apply_delta(release_manifest(checksum='abc'))
    assert not manager.can_apply_delta(release_manifest(url='https://example.com/evil.zip'))
    legacy = release_manifest()
    legacy['delta'] = legacy.pop('delta_v2')
    legacy['delta']['min_version'] = legacy['delta'].pop('from_version')
    assert manager.can_apply_delta(legacy)
    manager.current_version = 'v1.0.1'
    assert not manager.can_apply_delta(legacy)


@pytest.mark.parametrize('url', ['http://padek-interactive.tech/releases/x.exe',
    URL + '/../x.exe', URL + '/%2e%2e/x.exe', 'https://evil.test/x.exe',
    'https://padek-interactive.tech@evil.test/releases/x.exe', URL + '/x.exe?redirect=evil'])
def test_installer_urls_are_trusted(manager, url):
    with pytest.raises(UpdateError):
        manager.get_full_installer_url({'url': url})


def test_download_discards_partial_and_verify_is_required(manager, tmp_path, monkeypatch):
    monkeypatch.setattr(updater.urllib.request, 'urlopen', lambda *a, **k: Response(b'incomplete', length=100))
    assert manager.download_delta(URL + '/delta.zip', 'v1.1.0') is None
    assert not list(manager.downloads_dir.iterdir())
    archive = make_delta(tmp_path / 'delta.zip')
    assert not manager.apply_delta('v1.1.0', archive)[0]
    assert not manager.metadata_file.exists()


def test_verified_archive_is_rehashed_before_extract(manager, tmp_path):
    archive = make_delta(tmp_path / 'delta.zip')
    assert manager.verify_delta(archive, transaction.sha256_file(archive))
    archive.write_bytes(b'tampered')
    assert not manager.apply_delta('v1.1.0', archive)[0]
    assert not manager.metadata_file.exists()


@pytest.mark.parametrize('unsafe', ['../outside.txt', '/root.txt', 'C:/root.txt',
    'dir\\file.txt', 'file:stream', 'CON.txt', 'trailing. ', 'one/../two', 'config.ini', 'data/user_poi.json'])
def test_unsafe_payload_paths_are_rejected_before_install(manager, tmp_path, unsafe):
    archive = make_delta(tmp_path / 'bad.zip', {unsafe: b'bad'})
    assert not checked_apply(manager, archive)[0]
    assert (manager.install_path / '_internal/VERSION').read_text().strip() == '1.0.0'
    assert not manager.metadata_file.exists()


@pytest.mark.parametrize('extras', [
    {'checksum.sha256': b''}, {'FILES/unlisted.bin': b'x'},
    {'manifest.json': b'{"schema_version":2,"from_version":"v0.9.0","to_version":"v1.1.0"}'},
    {'version.txt': b'v1.2.0'}, {'DELETED.txt': b'../outside.txt'},
    {'FILES/app.txt': b'tampered'}, {'UNEXPECTED': b'bad'},
])
def test_malformed_package_is_rejected_before_install(manager, tmp_path, extras):
    archive = make_delta(tmp_path / 'bad.zip', extras=extras)
    assert not checked_apply(manager, archive)[0]
    assert not manager.metadata_file.exists()


def test_apply_and_rollback_restore_replaced_deleted_and_added_files(manager, tmp_path):
    (manager.install_path / 'app.txt').write_text('original')
    (manager.install_path / 'obsolete.txt').write_text('keep for rollback')
    archive = make_delta(tmp_path / 'delta.zip', deleted=['obsolete.txt'])
    assert checked_apply(manager, archive)[0]
    assert UpdateMetadata.from_file(manager.metadata_file).state == 'completed'
    assert (manager.install_path / 'app.txt').read_text() == 'updated'
    assert not (manager.install_path / 'obsolete.txt').exists()
    assert manager.rollback('v1.0.0')[0]
    assert (manager.install_path / 'app.txt').read_text() == 'original'
    assert (manager.install_path / 'obsolete.txt').read_text() == 'keep for rollback'
    assert not (manager.install_path / 'new').exists()
    assert (manager.install_path / '_internal/VERSION').read_text().strip() == '1.0.0'
    assert UpdateMetadata.from_file(manager.metadata_file).state == 'rolled_back'


def test_mid_apply_failure_automatically_rolls_back(manager, tmp_path, monkeypatch):
    (manager.install_path / 'app.txt').write_text('original')
    archive = make_delta(tmp_path / 'delta.zip')
    real_copy = transaction._copy_atomic
    failed = [False]
    def fail_once(source, destination):
        if destination == manager.install_path / 'app.txt' and not failed[0]:
            failed[0] = True
            raise OSError('simulated disk failure')
        real_copy(source, destination)
    monkeypatch.setattr(transaction, '_copy_atomic', fail_once)
    success, message = checked_apply(manager, archive)
    assert not success and 'rolled back' in message
    assert (manager.install_path / 'app.txt').read_text() == 'original'
    assert (manager.install_path / '_internal/VERSION').read_text().strip() == '1.0.0'


def test_metadata_atomic_write_preserves_previous_on_failure(tmp_path, monkeypatch):
    path = tmp_path / 'metadata.json'
    UpdateMetadata({'state': 'pending'}).save(path)
    def reject_replace(*args):
        raise OSError('disk full')
    monkeypatch.setattr(transaction.os, 'replace', reject_replace)
    with pytest.raises(OSError):
        UpdateMetadata({'state': 'completed'}).save(path)
    assert UpdateMetadata.from_file(path).state == 'pending'


def test_frozen_apply_is_pending_only_and_launch_failure_does_not_copy(manager, tmp_path, monkeypatch):
    archive = make_delta(tmp_path / 'delta.zip')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    calls = []
    monkeypatch.setattr(manager, '_launch_helper', lambda *args: calls.append(args))
    assert checked_apply(manager, archive) == (True, updater.UPDATE_PENDING)
    assert calls and UpdateMetadata.from_file(manager.metadata_file).state == 'pending'
    assert not (manager.install_path / 'app.txt').exists()
    assert not manager.resume_interrupted_update()[0]
    metadata = UpdateMetadata.from_file(manager.metadata_file)
    metadata.data['launch_requested_at'] = '2000-01-01T00:00:00+00:00'
    metadata.save(manager.metadata_file)
    def cancel(*args):
        raise UpdateError('UAC cancelled')
    monkeypatch.setattr(manager, '_launch_helper', cancel)
    assert not manager.resume_interrupted_update()[0]
    assert UpdateMetadata.from_file(manager.metadata_file).state == 'failed'


def test_unknown_installation_can_find_full_installer_but_not_delta(manager, monkeypatch):
    manager.current_version = 'unknown'
    payload = release_manifest()
    monkeypatch.setattr(updater.urllib.request, 'urlopen', lambda *a, **k: Response(json.dumps(payload).encode(), url=updater.LATEST_JSON_URL))
    assert manager.check_for_update()[0] == 'v1.1.0'
    assert not manager.can_apply_delta(payload)
    assert manager.get_recovery_installer_url() == payload['url']


def test_metadata_failure_after_partial_copy_still_restores_files(manager, tmp_path, monkeypatch):
    (manager.install_path / 'app.txt').write_text('original')
    archive = make_delta(tmp_path / 'delta.zip')
    real_copy, real_state = transaction._copy_atomic, transaction._save_state
    def fail_copy(source, destination):
        if destination == manager.install_path / 'app.txt':
            raise OSError('copy failed')
        return real_copy(source, destination)
    def fail_metadata(plan, state, error=None):
        if state in ('failed', 'rolling_back'):
            raise OSError('metadata disk full')
        return real_state(plan, state, error)
    monkeypatch.setattr(transaction, '_copy_atomic', fail_copy)
    monkeypatch.setattr(transaction, '_save_state', fail_metadata)
    success, message = checked_apply(manager, archive)
    assert not success and 'rolled back' in message
    assert (manager.install_path / '_internal/VERSION').read_text().strip() == '1.0.0'
    assert (manager.install_path / 'app.txt').read_text() == 'original'


def test_rollback_readiness_checks_backup_contents(manager, tmp_path):
    (manager.install_path / 'app.txt').write_text('original')
    assert checked_apply(manager, make_delta(tmp_path / 'delta.zip'))[0]
    metadata = UpdateMetadata.from_file(manager.metadata_file)
    assert manager.can_rollback_interrupted_update(metadata)
    plan = manager._read_plan(metadata)
    (Path(plan['backup_dir']) / 'app.txt').write_text('corrupted')
    assert not manager.can_rollback_interrupted_update(metadata)


def test_preparation_lock_rejects_apply_and_recovery_without_metadata_changes(manager, tmp_path, monkeypatch):
    archive = make_delta(tmp_path / 'delta.zip')
    stage_plan(manager, archive, monkeypatch)
    other = UpdateManager(None, 'v1.0.0', install_path=manager.install_path, data_path=manager.data_path)
    before = manager.metadata_file.read_bytes()
    with manager._preparation_lock():
        for operation in (lambda: checked_apply(other, archive), other.resume_interrupted_update,
                          other.rollback_interrupted_update):
            success, message = operation()
            assert not success and 'being prepared' in message
            assert manager.metadata_file.read_bytes() == before


def test_two_instances_cannot_prepare_overlapping_transactions(manager, tmp_path, monkeypatch):
    archive = make_delta(tmp_path / 'delta.zip')
    other = UpdateManager(None, 'v1.0.0', install_path=manager.install_path, data_path=manager.data_path)
    entered, release = threading.Event(), threading.Event()
    real_extract = updater.extract_delta
    results = []
    def slow_extract(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_extract(*args, **kwargs)
    monkeypatch.setattr(updater, 'extract_delta', slow_extract)
    monkeypatch.setattr(manager, '_execute_plan', lambda plan, metadata: (True, 'prepared'))
    first = threading.Thread(target=lambda: results.append(checked_apply(manager, archive)))
    first.start()
    try:
        assert entered.wait(3)
        success, message = checked_apply(other, archive)
        assert not success and 'being prepared' in message
        assert not manager.metadata_file.exists()
    finally:
        release.set()
        first.join(5)
    assert not first.is_alive() and results == [(True, 'prepared')]
    assert manager._read_plan()['current_version'] == 'v1.0.0'
    assert len([path for path in manager.staging_dir.iterdir() if path.is_dir()]) == 1


def test_default_source_install_cannot_apply(isolated_default_paths, tmp_path):
    manager = UpdateManager(None, 'v1.0.0')
    archive = make_delta(tmp_path / 'delta.zip')
    assert not checked_apply(manager, archive)[0]
    assert not manager.metadata_file.exists()


def test_corrupt_metadata_is_recovery_failure_not_no_update(manager):
    manager.metadata_file.parent.mkdir(parents=True)
    manager.metadata_file.write_text('{bad')
    metadata = manager.detect_incomplete_update()
    assert metadata.state == 'failed' and metadata.data['corrupt_metadata']
    assert not manager.can_resume_interrupted_update(metadata)
    assert not manager.can_rollback_interrupted_update(metadata)


NATIVE = pytest.mark.skipif(sys.platform != 'win32' or not shutil.which('powershell.exe'), reason='Windows PowerShell helper')


@NATIVE
def test_powershell_applies_and_rolls_back_real_temp_files(manager, tmp_path, monkeypatch):
    (manager.install_path / 'app.txt').write_text('original')
    (manager.install_path / 'obsolete.txt').write_text('obsolete original')
    plan = stage_plan(manager, make_delta(tmp_path / 'delta.zip', deleted=['obsolete.txt']), monkeypatch)
    result = run_helper(manager, plan)
    assert result.returncode == 0, result.stderr
    assert UpdateMetadata.from_file(manager.metadata_file).state == 'completed'
    assert (manager.install_path / 'app.txt').read_text() == 'updated'
    assert not (manager.install_path / 'obsolete.txt').exists()
    plan['action'] = 'rollback'
    result = run_helper(manager, plan)
    assert result.returncode == 0, result.stderr
    assert UpdateMetadata.from_file(manager.metadata_file).state == 'rolled_back'
    assert (manager.install_path / 'app.txt').read_text() == 'original'
    assert (manager.install_path / 'obsolete.txt').read_text() == 'obsolete original'
    assert not (manager.install_path / 'new').exists()
    assert (manager.data_path / 'logs/update-helper.log').exists()


@NATIVE
def test_powershell_accepts_windows_short_installation_paths(manager, tmp_path, monkeypatch):
    """tempfile can return RUNNER~1 paths on hosted Windows build agents."""
    import ctypes
    plan = stage_plan(manager, make_delta(tmp_path / 'delta.zip'), monkeypatch)
    short_buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(
        str(manager.install_path), short_buffer, len(short_buffer))
    assert length
    if short_buffer.value == str(manager.install_path):
        pytest.skip('8.3 path aliases are disabled on this volume')
    plan['app_dir'] = short_buffer.value
    result = run_helper(manager, plan)
    assert result.returncode == 0, result.stderr
    assert (manager.install_path / 'app.txt').read_text() == 'updated'
    plan['action'] = 'rollback'
    result = run_helper(manager, plan)
    assert result.returncode == 0, result.stderr
    assert not (manager.install_path / 'app.txt').exists()


@NATIVE
def test_powershell_rejects_changed_staged_payload_without_mutation(manager, tmp_path, monkeypatch):
    plan = stage_plan(manager, make_delta(tmp_path / 'delta.zip'), monkeypatch)
    (Path(plan['extract_dir']) / 'FILES/app.txt').write_text('tampered')
    result = run_helper(manager, plan)
    assert result.returncode != 0
    assert UpdateMetadata.from_file(manager.metadata_file).state == 'failed'
    assert not (manager.install_path / 'app.txt').exists()


@NATIVE
def test_powershell_waits_for_exact_parent_and_times_out_without_copy(manager, tmp_path, monkeypatch):
    plan = stage_plan(manager, make_delta(tmp_path / 'delta.zip'), monkeypatch)
    start = time.monotonic()
    result = run_helper(manager, plan, parent_pid=os.getpid(), started=manager._process_start_filetime(), timeout=1)
    assert result.returncode != 0 and time.monotonic() - start >= 1
    assert not (manager.install_path / 'app.txt').exists()
    assert UpdateMetadata.from_file(manager.metadata_file).state == 'failed'


@NATIVE
def test_active_helper_blocks_recovery_without_rewriting_metadata(manager, tmp_path, monkeypatch):
    plan = stage_plan(manager, make_delta(tmp_path / 'delta.zip'), monkeypatch)
    process = run_helper(manager, plan, parent_pid=os.getpid(),
                         started=manager._process_start_filetime(), timeout=3, background=True)
    try:
        deadline = time.monotonic() + 2
        while not manager.is_update_running() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert manager.is_update_running()
        previous = manager.metadata_file.read_bytes()
        metadata = manager.detect_incomplete_update()
        assert not manager.can_resume_interrupted_update(metadata)
        assert not manager.can_rollback_interrupted_update(metadata)
        assert not manager.resume_interrupted_update()[0]
        assert manager.metadata_file.read_bytes() == previous
        process.communicate(timeout=10)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


@NATIVE
def test_powershell_delete_failure_restores_the_whole_installation(manager, tmp_path, monkeypatch):
    (manager.install_path / 'app.txt').write_text('original')
    locked_file = manager.install_path / 'obsolete.txt'
    locked_file.write_text('still in use')
    ready = tmp_path / 'lock-ready'
    lock_script = tmp_path / 'hold-file.ps1'
    lock_script.write_text(
        'param([string]$LockedPath,[string]$ReadyPath)\n'
        '$handle = [IO.File]::Open($LockedPath,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)\n'
        'try { [IO.File]::WriteAllText($ReadyPath,"ready"); [Threading.Thread]::Sleep(30000) }\n'
        'finally { $handle.Dispose() }\n', encoding='utf-8')
    process = subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', str(lock_script), '-LockedPath', str(locked_file), '-ReadyPath', str(ready)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        plan = stage_plan(manager, make_delta(tmp_path / 'delta.zip', deleted=['obsolete.txt']), monkeypatch)
        result = run_helper(manager, plan)
        assert result.returncode != 0
        assert UpdateMetadata.from_file(manager.metadata_file).state == 'rolled_back', result.stderr
        assert (manager.install_path / 'app.txt').read_text() == 'original'
        assert (manager.install_path / '_internal/VERSION').read_text().strip() == '1.0.0'
        assert locked_file.read_text() == 'still in use'
        assert not (manager.install_path / 'new').exists()
    finally:
        process.terminate()
        process.wait(timeout=5)
