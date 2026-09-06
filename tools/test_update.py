"""Exercise the real delta producer and updater in disposable installations.

Run ``python tools/test_update.py --mode all`` from the repository. The HTTP
transport is simulated; files, ZIP checksums, transactions and the Windows
PowerShell helper are real. No installed application, user profile, production
server, UAC prompt or release version is changed. Version 1.0.1 is fixture data.
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools'))

from update_manager import LATEST_JSON_URL, UpdateManager, UpdateMetadata
from upload_vps import _generate_delta, _hash_folder

CURRENT_VERSION = 'v1.0.0'
TARGET_VERSION = 'v1.0.1'
BASE_URL = 'https://padek-interactive.tech/releases'
MODES = ('direct', 'bad-checksum', 'rollback', 'native')


class Response(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url
        self.headers = {'Content-Length': str(len(data))}

    def geturl(self):
        return self.url


def snapshot(directory):
    """Include empty directories so rollback cannot leave added folders behind."""
    return {path.relative_to(directory).as_posix(): path.read_bytes() if path.is_file() else None
            for path in directory.rglob('*')}


def write_files(directory, files):
    for name, data in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def prepare_fixture(root):
    install, bundle, data = root / 'app with spaces', root / 'new bundle', root / 'user data'
    original = {
        'spaceDrive.exe': b'old executable fixture',
        '_internal/VERSION': b'1.0.0\n',
        '_internal/library.dll': b'old library fixture',
        '_internal/obsolete-a.dll': b'obsolete A',
        '_internal/obsolete-b.dll': b'obsolete B',
        '_internal/unchanged.dat': b'unchanged',
    }
    updated = {
        'spaceDrive.exe': b'new executable fixture',
        '_internal/VERSION': b'1.0.1\n',
        '_internal/library.dll': b'new library fixture',
        '_internal/new folder/new.pyd': b'new module fixture',
        '_internal/unchanged.dat': b'unchanged',
    }
    write_files(install, original)
    write_files(bundle, updated)
    old_manifest = _hash_folder(install)
    archive, checksum, size = _generate_delta(old_manifest, bundle, CURRENT_VERSION, TARGET_VERSION, root / 'artifacts')
    protected = {'config.ini': b'[App]\napp_version = v0.7.12\n', 'data/user_poi.json': b'[{"name":"Keep me"}]'}
    write_files(install, protected)
    write_files(data, protected)
    manifest = {
        'schema_version': 2, 'version': TARGET_VERSION,
        'url': f'{BASE_URL}/SpaceDrive-Setup-{TARGET_VERSION}.exe',
        'delta': {'available': False},
        'delta_v2': {
            'available': True, 'from_version': CURRENT_VERSION, 'to_version': TARGET_VERSION,
            'url': f'{BASE_URL}/deltas/{archive.name}', 'checksum': checksum, 'size_bytes': size,
        },
    }
    manager = UpdateManager(None, CURRENT_VERSION, install_path=install, data_path=data)
    expected = {**updated, **protected}
    return manager, archive, manifest, expected


def download_fixture(manager, archive, manifest):
    """Use production discovery, compatibility and download code with fake HTTP."""
    responses = {LATEST_JSON_URL: json.dumps(manifest).encode(),
                 manifest['delta_v2']['url']: archive.read_bytes()}

    def urlopen(url, **kwargs):
        return Response(responses[url], url)

    with patch('update_manager.urllib.request.urlopen', side_effect=urlopen):
        version, latest = manager.check_for_update()
        assert version == TARGET_VERSION and manager.can_apply_delta(latest)
        progress = []
        downloaded = manager.download_delta(latest['delta_v2']['url'], version, lambda *values: progress.append(values))
        assert downloaded and progress[-1][0] == 100
        assert manager.verify_delta(downloaded, latest['delta_v2']['checksum'])
        return downloaded


def run_native(manager, action):
    metadata = UpdateMetadata.from_file(manager.metadata_file)
    plan = manager._read_plan(metadata)
    plan.update(action=action, parent_pid=2147483647, parent_started_filetime=0, notify_user=False)
    path = manager._save_plan(plan, metadata)
    result = subprocess.run([
        'powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-WindowStyle', 'Hidden', '-File', str(ROOT / 'scripts/apply_update.ps1'),
        '-PlanPath', str(path), '-PlanSha256', metadata.data['plan_sha256'],
    ], capture_output=True, text=True, timeout=45,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        log = manager.data_path / 'logs/update-helper.log'
        details = log.read_text(encoding='utf-8-sig') if log.exists() else 'No helper log was created.'
        raise AssertionError(f'Native {action} failed: {result.stdout}\n{result.stderr}\n{details}')


def run_mode(mode):
    if mode == 'native' and sys.platform != 'win32':
        print('[SKIP] Native helper requires Windows.')
        return
    with tempfile.TemporaryDirectory(prefix='spacedrive-pipeline-') as temporary:
        root = Path(temporary)
        manager, archive, manifest, expected = prepare_fixture(root)
        before = snapshot(manager.install_path)
        user_before = {name: (manager.data_path / name).read_bytes() for name in ('config.ini', 'data/user_poi.json')}
        downloaded = download_fixture(manager, archive, manifest)
        if mode == 'bad-checksum':
            downloaded.write_bytes(downloaded.read_bytes() + b'corruption after verification')
            success, _ = manager.apply_delta(TARGET_VERSION, downloaded)
            assert not success and snapshot(manager.install_path) == before
        elif mode == 'rollback':
            original_unlink = Path.unlink
            failed = []
            failure_path = manager.install_path / '_internal/obsolete-b.dll'

            def fail_second_deletion(path, *args, **kwargs):
                if path == failure_path and not failed:
                    failed.append(True)
                    raise PermissionError('Injected failure after replacements, additions and a deletion')
                return original_unlink(path, *args, **kwargs)

            with patch.object(Path, 'unlink', fail_second_deletion):
                success, _ = manager.apply_delta(TARGET_VERSION, downloaded)
            assert failed and not success
            assert UpdateMetadata.from_file(manager.metadata_file).state == 'rolled_back'
            assert snapshot(manager.install_path) == before
        else:
            if mode == 'native':
                with patch.object(manager, '_execute_plan', return_value=(True, 'prepared')):
                    success, message = manager.apply_delta(TARGET_VERSION, downloaded)
                assert success, message
                assert snapshot(manager.install_path) == before
                run_native(manager, 'apply')
            else:
                success, message = manager.apply_delta(TARGET_VERSION, downloaded)
                assert success, message
            assert UpdateMetadata.from_file(manager.metadata_file).state == 'completed'
            installed_files = {name: content for name, content in snapshot(manager.install_path).items() if content is not None}
            assert installed_files == expected
            if mode == 'native':
                run_native(manager, 'rollback')
                assert UpdateMetadata.from_file(manager.metadata_file).state == 'rolled_back'
                assert snapshot(manager.install_path) == before
        for name, content in user_before.items():
            assert (manager.data_path / name).read_bytes() == content
    print(f'[PASS] {mode}: disposable installation and user data verified.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('all', *MODES), default='all')
    args = parser.parse_args()
    for mode in MODES if args.mode == 'all' else (args.mode,):
        run_mode(mode)


if __name__ == '__main__':
    main()
