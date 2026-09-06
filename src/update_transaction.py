"""Validated delta packages and recoverable file transactions.

The frozen Windows application prepares the same journal contract for the
standalone PowerShell helper. Python execution is reserved for isolated tests or
explicit non-frozen installation paths; it never updates a running bundle.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import uuid
import zipfile


class UpdateError(RuntimeError):
    """An update could not be safely checked, prepared or applied."""


logger = logging.getLogger(__name__)


_VERSION = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(?:alpha|beta|rc)\.[0-9]+)?\Z")
_SHA256 = re.compile(r"[a-fA-F0-9]{64}\Z")
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def normalize_version(value: str) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise UpdateError("Invalid release version")
    return 'v' + value.removeprefix('v')


def valid_checksum(value) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, data: dict) -> None:
    """Replace metadata atomically, preserving the previous file on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def relative_path(value: str) -> str:
    """Require canonical POSIX paths which also have an unambiguous Windows form."""
    if not isinstance(value, str) or not value or len(value) > 2000:
        raise UpdateError('Invalid package path')
    if '\\' in value or value.startswith('/'):
        raise UpdateError(f'Unsafe package path: {value!r}')
    parts = value.split('/')
    for part in parts:
        if (part in ('', '.', '..') or part[-1:] in (' ', '.')
                or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
                or _RESERVED.match(part)):
            raise UpdateError(f'Unsafe package path: {value!r}')
    return str(PurePosixPath(value))


def installation_path(value: str) -> str:
    value = relative_path(value)
    lower = value.casefold()
    top = lower.split('/')[0]
    if (lower in ('config.ini', 'data/user_poi.json')
            or top in ('logs', 'updates', '__pycache__', '.git')
            or top.startswith(('.env', '.venv'))):
        raise UpdateError(f'Package would overwrite user data: {value}')
    return value


def safe_child(root: Path, relative: str) -> Path:
    """Reject traversal and existing symlinks/junctions before every file operation."""
    relative = relative_path(relative)
    root = Path(root).absolute()
    current = root
    # Include the root's ancestors: a junction must not redirect an elevated write.
    for item in [*reversed(root.parents), root]:
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise UpdateError(f'Reparse point in update path: {item}')
    for part in relative.split('/'):
        current = current / part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise UpdateError(f'Reparse point in update path: {current}')
    if not current.resolve().is_relative_to(root.resolve()):
        raise UpdateError('Update path escaped its directory')
    return current


def _unique_paths(paths):
    seen = set()
    for path in paths:
        key = path.casefold()
        if key in seen:
            raise UpdateError(f'Duplicate package path: {path}')
        seen.add(key)


def extract_delta(archive: Path, destination: Path, current: str, target: str) -> tuple[list, list]:
    """Validate all members and all hashes before extracting a schema-2 package."""
    current, target = normalize_version(current), normalize_version(target)
    allowed = {'manifest.json', 'version.txt', 'checksum.sha256', 'DELETED.txt'}
    with zipfile.ZipFile(archive) as package:
        entries = {}
        total = 0
        for info in package.infolist():
            name = relative_path(info.filename.rstrip('/') if info.is_dir() else info.filename)
            mode = stat.S_IFMT(info.external_attr >> 16)
            if info.flag_bits & 1 or mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise UpdateError(f'Unsupported archive member: {name}')
            if info.external_attr & 0x400:
                raise UpdateError(f'Reparse archive member: {name}')
            if info.is_dir():
                if name != 'FILES' and not name.startswith('FILES/'):
                    raise UpdateError(f'Unexpected archive directory: {name}')
                continue
            if name not in allowed and not name.startswith('FILES/'):
                raise UpdateError(f'Unexpected archive file: {name}')
            if name in entries:
                raise UpdateError(f'Duplicate archive file: {name}')
            entries[name] = info
            total += info.file_size
            if total > 4 * 1024 ** 3 or len(entries) > 100000:
                raise UpdateError('Delta archive exceeds supported limits')
        _unique_paths(entries)
        if not allowed.issubset(entries):
            raise UpdateError('Delta must contain manifest.json, version.txt, checksum.sha256 and DELETED.txt')
        for name in allowed:
            if entries[name].file_size > 8 * 1024 ** 2:
                raise UpdateError('Oversized delta manifest')
        manifest = json.loads(package.read('manifest.json').decode('utf-8-sig'))
        if (not isinstance(manifest, dict) or manifest.get('schema_version') != 2
                or normalize_version(manifest.get('from_version')) != current
                or normalize_version(manifest.get('to_version')) != target
                or normalize_version(package.read('version.txt').decode('utf-8-sig').strip()) != target):
            raise UpdateError('Delta version does not match its exact source and target')
        checksums = {}
        for line in package.read('checksum.sha256').decode('utf-8-sig').splitlines():
            if not line:
                continue
            match = re.fullmatch(r'([a-fA-F0-9]{64})  (.+)', line)
            if not match:
                raise UpdateError('Invalid checksum manifest line')
            name = installation_path(match[2])
            if name in checksums:
                raise UpdateError(f'Duplicate checksum path: {name}')
            checksums[name] = match[1].lower()
        files = {installation_path(name[6:]): info for name, info in entries.items() if name.startswith('FILES/')}
        if set(files) != set(checksums):
            raise UpdateError('Checksum manifest must cover every payload file exactly')
        deleted = [installation_path(line) for line in package.read('DELETED.txt').decode('utf-8-sig').splitlines() if line]
        _unique_paths([*files, *deleted])
        for name, info in files.items():
            digest = hashlib.sha256()
            with package.open(info) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != checksums[name]:
                raise UpdateError(f'Payload checksum mismatch: {name}')
        destination.mkdir(parents=True, exist_ok=False)
        for name, info in entries.items():
            output = safe_child(destination, name)
            output.parent.mkdir(parents=True, exist_ok=True)
            with package.open(info) as source, output.open('xb') as dest:
                shutil.copyfileobj(source, dest, 1024 * 1024)
        return ([{'path': name, 'sha256': checksums[name]} for name in sorted(files)], sorted(deleted))


def validate_plan(plan: dict) -> None:
    if plan.get('schema_version') != 1 or plan.get('action') not in ('apply', 'rollback'):
        raise UpdateError('Unsupported update transaction')
    normalize_version(plan.get('current_version'))
    normalize_version(plan.get('target_version'))
    app = Path(plan['app_dir']).absolute()
    transaction = Path(plan['transaction_dir']).absolute()
    data = Path(plan['data_dir']).absolute()
    if not app.is_dir() or transaction == app or transaction.is_relative_to(app):
        raise UpdateError('Update staging must be separate from the installation')
    if not transaction.is_relative_to(data / 'updates' / 'staging'):
        raise UpdateError('Transaction escaped the update staging directory')
    expected = {'extract_dir': transaction / 'extract', 'backup_dir': transaction / 'backup',
                'journal_file': transaction / 'journal.json', 'metadata_file': data / 'updates' / 'staging' / 'metadata.json'}
    for field, path in expected.items():
        if Path(plan[field]).absolute() != path:
            raise UpdateError(f'Unexpected transaction path: {field}')
    safe_child(app, '.update-path-check')
    safe_child(data, '.update-path-check')
    paths = []
    for item in plan['files']:
        paths.append(installation_path(item['path']))
        if not valid_checksum(item.get('sha256')):
            raise UpdateError('Invalid payload checksum')
    paths.extend(installation_path(name) for name in plan['deleted'])
    _unique_paths(paths)
    for name in paths:
        destination = safe_child(app, name)
        if destination.exists() and not destination.is_file():
            raise UpdateError(f'File update targets a directory: {name}')


def _installed_version(plan):
    path = safe_child(Path(plan['app_dir']), '_internal/VERSION')
    if not path.is_file():
        raise UpdateError('This installation has no version marker. Use the full installer.')
    return normalize_version(path.read_text(encoding='utf-8-sig').strip())


def _save_state(plan, state, error=None):
    path = Path(plan['metadata_file'])
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    data.update(state=state, current_version=plan['current_version'], target_version=plan['target_version'])
    if error:
        data.setdefault('errors', []).append(str(error))
    atomic_json(path, data)


def _best_effort_state(plan, state, error=None):
    try:
        _save_state(plan, state, error)
    except Exception:
        logger.exception('Could not persist update state %s', state)


def _copy_atomic(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f'.{destination.name}.{uuid.uuid4().hex}.update')
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def rollback_transaction(plan: dict, *, mark=True):
    validate_plan(plan)
    try:
        installed = _installed_version(plan)
    except UpdateError:
        installed = None  # A verified journal can restore a damaged VERSION file.
    if installed is not None and installed not in (plan['current_version'], plan['target_version']):
        raise UpdateError('Another application version is installed; refusing stale rollback')
    journal = json.loads(Path(plan['journal_file']).read_text(encoding='utf-8'))
    if not journal.get('ready'):
        raise UpdateError('No complete backup is available')
    app, backup = Path(plan['app_dir']), Path(plan['backup_dir'])
    expected = {item['path'] for item in plan['files']} | set(plan['deleted'])
    if {item['path'] for item in journal['entries']} != expected:
        raise UpdateError('Backup journal does not match the update')
    for item in journal['entries']:
        name = installation_path(item['path'])
        safe_child(app, name)
        if item['existed']:
            source = safe_child(backup, name)
            if not valid_checksum(item.get('sha256')) or sha256_file(source) != item['sha256']:
                raise UpdateError(f'Backup checksum mismatch: {name}')
    _best_effort_state(plan, 'rolling_back')
    for item in journal['entries']:
        destination = safe_child(app, item['path'])
        if item['existed']:
            if not destination.is_file() or sha256_file(destination) != item['sha256']:
                _copy_atomic(safe_child(backup, item['path']), destination)
        else:
            destination.unlink(missing_ok=True)
    for name in reversed(journal.get('created_dirs', [])):
        directory = safe_child(app, name)
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    if mark:
        _save_state(plan, 'rolled_back')


def apply_transaction(plan: dict):
    """Apply all files or restore the complete previous state using the journal."""
    validate_plan(plan)
    app, files_root, backup = Path(plan['app_dir']), Path(plan['extract_dir']) / 'FILES', Path(plan['backup_dir'])
    for item in plan['files']:
        if sha256_file(safe_child(files_root, item['path'])) != item['sha256']:
            raise UpdateError(f'Staged payload checksum mismatch: {item["path"]}')
    version_file = safe_child(files_root, '_internal/VERSION')
    if (not version_file.is_file()
            or normalize_version(version_file.read_text(encoding='utf-8-sig').strip()) != plan['target_version']):
        raise UpdateError('Delta must contain the matching target VERSION marker')
    journal_file = Path(plan['journal_file'])
    if journal_file.exists():
        existing = json.loads(journal_file.read_text(encoding='utf-8'))
        if existing.get('ready'):
            rollback_transaction(plan, mark=False)
    if _installed_version(plan) != plan['current_version']:
        raise UpdateError('Installed version does not match the exact delta base')
    entries, created_dirs = [], set()
    for name in sorted({item['path'] for item in plan['files']} | set(plan['deleted'])):
        original = safe_child(app, name)
        item = {'path': name, 'existed': original.is_file()}
        if item['existed']:
            item['sha256'] = sha256_file(original)
            saved = safe_child(backup, name)
            _copy_atomic(original, saved)
            if sha256_file(saved) != item['sha256']:
                raise UpdateError(f'Backup verification failed: {name}')
        entries.append(item)
        for parent in original.parents:
            if parent == app:
                break
            if not parent.exists():
                created_dirs.add(parent.relative_to(app).as_posix())
    journal = {'ready': True, 'entries': entries, 'created_dirs': sorted(created_dirs, key=lambda name: (name.count('/'), name))}
    atomic_json(journal_file, journal)
    _save_state(plan, 'applying')
    try:
        for item in plan['files']:
            destination = safe_child(app, item['path'])
            _copy_atomic(safe_child(files_root, item['path']), destination)
            if sha256_file(destination) != item['sha256']:
                raise UpdateError(f'Installed file checksum mismatch: {item["path"]}')
        for name in plan['deleted']:
            safe_child(app, name).unlink(missing_ok=True)
        if _installed_version(plan) != plan['target_version']:
            raise UpdateError('Installed version verification failed')
        _save_state(plan, 'completed')
    except Exception as error:
        _best_effort_state(plan, 'failed', error)
        try:
            rollback_transaction(plan)
        except Exception as rollback_error:
            _best_effort_state(plan, 'rollback_failed', rollback_error)
            raise UpdateError(f'Update failed: {error}; rollback failed: {rollback_error}') from error
        raise UpdateError(f'Update failed and was rolled back: {error}') from error
