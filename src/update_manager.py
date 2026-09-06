"""Checked downloads, isolated staging and recoverable application updates."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid
from app_paths import bundle_dir, install_dir, user_data_dir
from update_transaction import (UpdateError, apply_transaction, atomic_json, extract_delta,
    normalize_version, rollback_transaction, safe_child, sha256_file, valid_checksum, validate_plan)

logger = logging.getLogger(__name__)
RELEASES_BASE_URL = 'https://padek-interactive.tech/releases'
LATEST_JSON_URL = RELEASES_BASE_URL + '/latest.json'
UPDATE_PENDING = 'pending_exit'
UpdateCheckError = UpdateError


def _make_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _release_url(value, suffix):
    if not isinstance(value, str):
        raise UpdateError('Missing release download URL')
    parsed = urllib.parse.urlsplit(value)
    decoded = urllib.parse.unquote(parsed.path)
    if (parsed.scheme != 'https' or parsed.hostname != 'padek-interactive.tech'
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not decoded.startswith('/releases/')
            or not decoded.lower().endswith(suffix) or '\\' in decoded
            or any(part in ('', '.', '..') for part in decoded.split('/')[1:])):
        raise UpdateError('Release download URL is not trusted')
    return value


def _download_file(url, dest, on_progress=None):
    """Commit complete downloads atomically and remove partial transfers."""
    dest = Path(dest)
    partial = dest.with_name(f'.{dest.name}.{uuid.uuid4().hex}.part')
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=60, context=_make_ssl_context()) as response:
            _release_url(response.geturl(), '.zip')
            total = int(response.headers.get('Content-Length', 0))
            written, last_percent = 0, -1
            with partial.open('xb') as stream:
                while chunk := response.read(64 * 1024):
                    stream.write(chunk)
                    written += len(chunk)
                    if on_progress is not None and total:
                        percent = min(100, written * 100 // total)
                        if percent != last_percent:
                            on_progress(percent, written, total)
                            last_percent = percent
                stream.flush()
                os.fsync(stream.fileno())
            if total and total != written:
                raise UpdateError('Incomplete delta download')
            os.replace(partial, dest)
        return True, f'Downloaded {dest.name}'
    except Exception as error:
        return False, f'Download failed: {error}'
    finally:
        partial.unlink(missing_ok=True)


def _version_normalize(value):
    return normalize_version(value).removeprefix('v')


def _version_compare(first, second):
    from packaging.version import Version
    first, second = Version(_version_normalize(first)), Version(_version_normalize(second))
    return (first > second) - (first < second)


def _version_gte(first, second):
    return _version_compare(first, second) >= 0


def _is_writable(path):
    probe = Path(path) / f'.spacedrive-probe-{uuid.uuid4().hex}'
    try:
        with probe.open('xb'):
            pass
        return True
    except OSError:
        return False
    finally:
        if probe.exists():
            probe.unlink()


class UpdateMetadata:
    """Durable status from the process that actually applies the update."""
    def __init__(self, data=None):
        self.data = dict(data or {})

    def _property(key, default=''):
        return property(lambda self: self.data.get(key, default),
                        lambda self, value: self.data.__setitem__(key, value))

    state = _property('state', 'unknown')
    current_version = _property('current_version')
    target_version = _property('target_version')
    backup_path = _property('backup_path')

    @property
    def errors(self):
        return self.data.get('errors', [])

    def add_error(self, error):
        self.data.setdefault('errors', []).append(str(error))

    def to_dict(self):
        self.data['updated_at'] = datetime.now(timezone.utc).isoformat()
        return dict(self.data)

    def save(self, path):
        atomic_json(Path(path), self.to_dict())

    @classmethod
    def from_file(cls, path):
        path = Path(path)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict) or not isinstance(data.get('state'), str):
                raise ValueError('Invalid update metadata')
            return cls(data)
        except Exception as error:
            return cls({'state': 'failed', 'corrupt_metadata': True,
                        'errors': [f'Cannot read update recovery metadata: {error}']})


class UpdateManager:
    def __init__(self, config_manager, current_version, *, install_path=None, data_path=None):
        self.config = config_manager
        self.current_version = 'unknown' if current_version == 'unknown' else normalize_version(current_version)
        self.install_path = Path(install_path if install_path is not None else install_dir()).absolute()
        self.data_path = Path(data_path if data_path is not None else user_data_dir()).absolute()
        self._explicit_install_path = install_path is not None
        self.updates_dir = self.data_path / 'updates'
        self.downloads_dir = self.updates_dir / 'downloads'
        self.staging_dir = self.updates_dir / 'staging'
        self.metadata_file = self.staging_dir / 'metadata.json'
        self._verified_archives = {}
        self._last_manifest = None

    def _fetch_latest(self):
        try:
            with urllib.request.urlopen(LATEST_JSON_URL, timeout=15, context=_make_ssl_context()) as response:
                _release_url(response.geturl(), '.json')
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise UpdateError('Release manifest is too large')
            manifest = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(manifest, dict):
                raise UpdateError('Invalid release manifest')
            normalize_version(manifest.get('version'))
            self.get_full_installer_url(manifest)
            self._last_manifest = manifest
            return manifest
        except Exception as error:
            raise UpdateError(f'Could not check for updates: {error}') from error

    def check_for_update(self):
        manifest = self._fetch_latest()
        version = normalize_version(manifest['version'])
        return (version, manifest) if self.current_version == 'unknown' or _version_compare(version, self.current_version) > 0 else (None, None)

    def get_full_installer_url(self, manifest):
        return _release_url(manifest.get('url'), '.exe')

    def get_delta_info(self, manifest):
        """Use the new field; old applications see delta.available=false."""
        try:
            target = normalize_version(manifest['version'])
            info = manifest.get('delta_v2', manifest.get('delta', {}))
            if not isinstance(info, dict) or info.get('available') is not True:
                return {}
            source = normalize_version(info.get('from_version', info.get('min_version')))
            if (source != self.current_version or _version_compare(target, source) <= 0
                    or normalize_version(info.get('to_version', target)) != target
                    or not valid_checksum(info.get('checksum'))):
                return {}
            _release_url(info.get('url'), '.zip')
            return dict(info)
        except (KeyError, TypeError, ValueError, UpdateError):
            return {}

    def can_apply_delta(self, manifest):
        return bool(self.get_delta_info(manifest))

    def set_update_manifest(self, manifest):
        """Carry validated release metadata across the UI's download/apply workers."""
        normalize_version(manifest.get('version'))
        self.get_full_installer_url(manifest)
        self._last_manifest = dict(manifest)

    def get_recovery_installer_url(self, metadata=None):
        metadata = metadata or UpdateMetadata.from_file(self.metadata_file)
        if metadata and metadata.data.get('installer_url'):
            return _release_url(metadata.data['installer_url'], '.exe')
        return self.get_full_installer_url(self._last_manifest or self._fetch_latest())

    def download_delta(self, url, target_version, on_progress=None):
        target = normalize_version(target_version)
        _release_url(url, '.zip')
        if _version_compare(target, self.current_version) <= 0:
            raise UpdateError('Delta target must be newer than the installed version')
        destination = self.downloads_dir / f'SpaceDrive-delta-{self.current_version}-to-{target}.zip'
        success, message = _download_file(url, destination, on_progress)
        if not success:
            logger.error(message)
            return None
        return destination

    def verify_delta(self, archive, expected_checksum):
        if not valid_checksum(expected_checksum):
            return False
        try:
            archive = Path(archive).resolve()
            if sha256_file(archive) != expected_checksum.lower():
                return False
            self._verified_archives[str(archive)] = expected_checksum.lower()
            return True
        except OSError:
            return False

    def _read_plan(self, metadata=None):
        metadata = metadata or UpdateMetadata.from_file(self.metadata_file)
        if not metadata or metadata.data.get('corrupt_metadata'):
            raise UpdateError('No valid update recovery metadata')
        plan_path = safe_child(self.staging_dir, metadata.data.get('plan_path', ''))
        if not valid_checksum(metadata.data.get('plan_sha256')) or sha256_file(plan_path) != metadata.data['plan_sha256']:
            raise UpdateError('Update plan integrity check failed')
        plan = json.loads(plan_path.read_text(encoding='utf-8-sig'))
        validate_plan(plan)
        if Path(plan['app_dir']) != self.install_path or Path(plan['data_dir']) != self.data_path:
            raise UpdateError('Update plan belongs to another installation')
        return plan

    def _save_plan(self, plan, metadata):
        path = Path(plan['transaction_dir']) / f'plan-{uuid.uuid4().hex}.json'
        atomic_json(path, plan)
        metadata.data['plan_path'] = path.relative_to(self.staging_dir).as_posix()
        metadata.data['plan_sha256'] = sha256_file(path)
        metadata.save(self.metadata_file)
        return path

    @contextmanager
    def _preparation_lock(self):
        """Serialize checks, staging and launch across application instances.

        The helper owns a different lock, so it can start while the parent is
        finishing preparation and still wait for that parent's normal shutdown.
        """
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        path = safe_child(self.updates_dir, 'prepare.lock')
        stream = path.open('a+b')
        acquired = False
        try:
            try:
                if sys.platform == 'win32':
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as error:
                raise UpdateError('Another update operation is being prepared. Try again shortly.') from error
            yield
        finally:
            if acquired:
                if sys.platform == 'win32':
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_UN)
            stream.close()

    def apply_delta(self, target_version, zip_path, *, expected_checksum=None):
        try:
            with self._preparation_lock():
                return self._apply_delta_locked(target_version, zip_path, expected_checksum=expected_checksum)
        except (UpdateError, OSError) as error:
            return False, str(error)

    def _apply_delta_locked(self, target_version, zip_path, *, expected_checksum=None):
        if not getattr(sys, 'frozen', False) and not self._explicit_install_path:
            return False, 'Use the packaged application or the full installer to apply updates.'
        if self.is_update_running() or self.detect_incomplete_update():
            return False, 'Resolve the previous update before starting another update.'
        try:
            target = normalize_version(target_version)
            if _version_compare(target, self.current_version) <= 0:
                raise UpdateError('Delta target must be newer than the installed version')
            archive = Path(zip_path).resolve()
            checksum = expected_checksum or self._verified_archives.get(str(archive))
            if not checksum or not self.verify_delta(archive, checksum):
                raise UpdateError('Delta archive must pass its release SHA256 check before applying')
            transaction = self.staging_dir / uuid.uuid4().hex
            files, deleted = extract_delta(archive, transaction / 'extract', self.current_version, target)
            plan = {
                'schema_version': 1, 'action': 'apply', 'current_version': self.current_version,
                'target_version': target, 'app_dir': str(self.install_path), 'data_dir': str(self.data_path),
                'transaction_dir': str(transaction), 'extract_dir': str(transaction / 'extract'),
                'backup_dir': str(transaction / 'backup'), 'journal_file': str(transaction / 'journal.json'),
                'metadata_file': str(self.metadata_file), 'files': files, 'deleted': deleted,
                'archive_sha256': checksum, 'parent_pid': os.getpid(),
                'parent_started_filetime': self._process_start_filetime(), 'wait_timeout_seconds': 180,
                'notify_user': bool(getattr(sys, 'frozen', False)),
            }
            validate_plan(plan)
            metadata = UpdateMetadata({'state': 'prepared', 'current_version': self.current_version,
                'target_version': target, 'backup_path': str(transaction / 'backup'), 'errors': [],
                'installer_url': self.get_full_installer_url(self._last_manifest) if self._last_manifest else ''})
            self._save_plan(plan, metadata)
            return self._execute_plan(plan, metadata)
        except Exception as error:
            logger.error('Update preparation failed: %s', error)
            return False, str(error)

    @staticmethod
    def _process_start_filetime():
        if sys.platform != 'win32':
            return 0
        import ctypes
        from ctypes import wintypes
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.GetCurrentProcess.restype = wintypes.HANDLE
        api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        if not api.GetProcessTimes(api.GetCurrentProcess(), ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            raise UpdateError('Cannot identify the application process')
        return (created.dwHighDateTime << 32) | created.dwLowDateTime

    def _launch_helper(self, plan_path, plan_sha256, transaction):
        """A successfully started helper is pending, never completed."""
        if sys.platform != 'win32':
            raise UpdateError('Packaged updates currently require Windows')
        source = bundle_dir() / 'scripts' / 'apply_update.ps1'
        writable = _is_writable(self.install_path)
        if writable:
            helper = Path(transaction) / 'apply_update.ps1'
            shutil.copy2(source, helper)
        else:
            # An elevated process must never execute a script from user-writable
            # staging. PowerShell parses the protected installed script in memory.
            try:
                relative = source.absolute().relative_to(self.install_path).as_posix()
                helper = safe_child(self.install_path, relative)
            except ValueError as error:
                raise UpdateError('Cannot locate a protected updater. Use the full installer.') from error
            if _is_writable(helper.parent):
                raise UpdateError('Updater script is not protected. Use the full installer.')
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-WindowStyle', 'Hidden', '-File', str(helper), '-PlanPath', str(plan_path), '-PlanSha256', plan_sha256]
        if writable:
            process = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return process.pid
        import ctypes
        result = ctypes.windll.shell32.ShellExecuteW(None, 'runas', command[0],
            subprocess.list2cmdline(command[1:]), str(transaction), 0)
        if result <= 32:
            raise UpdateError('Update elevation was cancelled or could not start')
        return None

    def _execute_plan(self, plan, metadata):
        if self.is_update_running(metadata):
            return False, 'An update helper is still running. Wait for its result before recovering.'
        if getattr(sys, 'frozen', False):
            metadata.state = 'pending'
            metadata.data['launch_requested_at'] = datetime.now(timezone.utc).isoformat()
            plan['parent_pid'] = os.getpid()
            plan['parent_started_filetime'] = self._process_start_filetime()
            path = self._save_plan(plan, metadata)
            try:
                self._launch_helper(path, metadata.data['plan_sha256'], plan['transaction_dir'])
            except Exception as error:
                metadata.state = 'failed'
                metadata.add_error(error)
                metadata.save(self.metadata_file)
                return False, str(error)
            return True, UPDATE_PENDING
        if not self._explicit_install_path:
            return False, 'Direct source-tree updates are disabled.'
        try:
            if plan['action'] == 'rollback':
                rollback_transaction(plan)
                return True, f'Rolled back to {plan["current_version"]}'
            apply_transaction(plan)
            return True, f'Update to {plan["target_version"]} applied successfully'
        except Exception as error:
            current = UpdateMetadata.from_file(self.metadata_file) or metadata
            if current.state not in ('rolled_back', 'rollback_failed'):
                current.state = 'failed'
                current.add_error(error)
                current.save(self.metadata_file)
            return False, str(error)

    def detect_incomplete_update(self):
        metadata = UpdateMetadata.from_file(self.metadata_file)
        if metadata and metadata.state in ('prepared', 'pending', 'in_progress', 'applying', 'rolling_back', 'failed', 'rollback_failed'):
            return metadata
        return None

    detect_interrupted_update = detect_incomplete_update

    def is_update_running(self, metadata=None):
        """Probe the helper's exclusive lock before touching any recovery metadata."""
        lock = self.updates_dir / 'apply.lock'
        if lock.exists():
            try:
                with lock.open('a+b'):
                    pass
            except OSError:
                return True
        metadata = metadata or UpdateMetadata.from_file(self.metadata_file)
        if metadata and metadata.state == 'pending' and metadata.data.get('launch_requested_at'):
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(metadata.data['launch_requested_at'])).total_seconds()
                # Cover the small interval between process creation and lock acquisition.
                if age < 10:
                    return True
            except (TypeError, ValueError):
                return True
        return False

    def can_resume_interrupted_update(self, metadata):
        try:
            if self.is_update_running(metadata):
                return False
            plan = self._read_plan(metadata)
            return all(sha256_file(safe_child(Path(plan['extract_dir']) / 'FILES', item['path'])) == item['sha256'] for item in plan['files'])
        except Exception:
            return False

    def can_rollback_interrupted_update(self, metadata):
        try:
            if self.is_update_running(metadata):
                return False
            plan = self._read_plan(metadata)
            journal = json.loads(Path(plan['journal_file']).read_text(encoding='utf-8-sig'))
            if not journal.get('ready'):
                return False
            expected = {item['path'] for item in plan['files']} | set(plan['deleted'])
            if {item['path'] for item in journal['entries']} != expected:
                return False
            for item in journal['entries']:
                if item['existed']:
                    saved = safe_child(Path(plan['backup_dir']), item['path'])
                    if not valid_checksum(item.get('sha256')) or sha256_file(saved) != item['sha256']:
                        return False
            return True
        except Exception:
            return False

    def resume_interrupted_update(self):
        try:
            with self._preparation_lock():
                return self._resume_interrupted_update_locked()
        except (UpdateError, OSError) as error:
            return False, str(error)

    def _resume_interrupted_update_locked(self):
        metadata = self.detect_incomplete_update()
        if not metadata or not self.can_resume_interrupted_update(metadata):
            return False, 'The staged update is unavailable or invalid. Use the full installer.'
        plan = self._read_plan(metadata)
        plan['action'] = 'apply'
        metadata.data['result_acknowledged'] = False
        return self._execute_plan(plan, metadata)

    def rollback_interrupted_update(self):
        try:
            with self._preparation_lock():
                return self._rollback_interrupted_update_locked()
        except (UpdateError, OSError) as error:
            return False, str(error)

    def _rollback_interrupted_update_locked(self):
        metadata = UpdateMetadata.from_file(self.metadata_file)
        if not metadata or not self.can_rollback_interrupted_update(metadata):
            return False, 'No verified backup is available. Use the full installer.'
        plan = self._read_plan(metadata)
        plan['action'] = 'rollback'
        metadata.data['result_acknowledged'] = False
        return self._execute_plan(plan, metadata)

    def rollback(self, current_version):
        normalize_version(current_version)
        return self.rollback_interrupted_update()

    def get_last_update_result(self):
        metadata = UpdateMetadata.from_file(self.metadata_file)
        if (metadata and metadata.state in ('completed', 'rolled_back', 'failed', 'rollback_failed')
                and not metadata.data.get('result_acknowledged')):
            return metadata
        return None

    def acknowledge_update_result(self):
        metadata = UpdateMetadata.from_file(self.metadata_file)
        if metadata:
            metadata.data['result_acknowledged'] = True
            metadata.save(self.metadata_file)

    def cleanup_old_deltas(self, keep_count=3):
        if not self.downloads_dir.exists():
            return
        keep_count = max(1, int(keep_count))
        files = sorted((path for path in self.downloads_dir.glob('*.zip') if path.is_file() and not path.is_symlink()),
            key=lambda path: path.stat().st_mtime, reverse=True)
        for path in files[keep_count:]:
            safe_child(self.downloads_dir, path.name).unlink()
        # Keep journals/backups until an explicit recovery or maintenance action.
