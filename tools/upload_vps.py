"""Publish verified local artifacts with an atomic latest.json commit over SFTP.

The installer, bundle manifest and optional v2 delta are immutable. They are
uploaded and checked before latest.json changes. Old clients see delta.available
false and must use the full installer to acquire the transactional updater.
"""

import argparse
import hashlib
import io
import json
import os
import re
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import paramiko

from versioning import ROOT, bundle_version, hash_file, hash_folder, normalize_version, verify_build

_SHA256 = re.compile(r'[0-9a-f]{64}')


def _version(value):
    return 'v' + normalize_version(value)


def _version_key(value):
    return tuple(map(int, normalize_version(value).split('.')))


def _load_private_key(content):
    for key_class in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            return key_class.from_private_key(io.StringIO(content))
        except paramiko.SSHException:
            pass
    raise ValueError('Unsupported SSH key; use RSA, Ed25519 or ECDSA')


def _safe_payload_path(value):
    if not isinstance(value, str) or not value or any(char in value for char in '\\:*?"<>|'):
        raise ValueError(f'Unsafe bundle path: {value!r}')
    parts = value.split('/')
    if any(part in ('', '.', '..') or part.endswith((' ', '.')) for part in parts):
        raise ValueError(f'Unsafe bundle path: {value!r}')
    if PurePosixPath(value).is_absolute() or any(ord(char) < 32 for char in value):
        raise ValueError(f'Unsafe bundle path: {value!r}')
    reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                *(f'lpt{i}' for i in range(1, 10))}
    if any(part.split('.')[0].lower() in reserved for part in parts):
        raise ValueError(f'Reserved Windows path: {value!r}')
    lower = value.lower()
    if (lower in ('config.ini', 'data/user_poi.json')
            or parts[0].lower() in ('logs', 'updates', '.git')
            or any(part.lower().startswith(('.env', '.venv')) or part == '__pycache__' for part in parts)):
        raise ValueError(f'User data must not be included in updates: {value}')
    return value


def _validate_manifest(manifest):
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError('Bundle manifest must contain files')
    seen = set()
    for name, digest in manifest.items():
        _safe_payload_path(name)
        if name.casefold() in seen or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError(f'Invalid or duplicate bundle manifest entry: {name}')
        seen.add(name.casefold())
    return manifest


def _hash_folder(folder):
    for path in folder.rglob('*'):
        if path.is_symlink():
            raise ValueError(f'Bundle symlinks are not supported: {path}')
    return _validate_manifest(hash_folder(folder))


def _generate_delta(old_manifest, new_dist_dir, old_version, new_version, output_dir):
    """Build a self-describing exact-base delta, including deleted files."""
    old_manifest = _validate_manifest(old_manifest)
    old_version, new_version = _version(old_version), _version(new_version)
    if _version_key(new_version) <= _version_key(old_version):
        raise ValueError('Delta target must be newer than its exact base')
    new_manifest = _hash_folder(new_dist_dir)
    # Installed bundles use the Windows case-insensitive namespace. A casing
    # change is an update of the same file, never an add followed by deletion.
    old_by_name = {name.casefold(): digest for name, digest in old_manifest.items()}
    new_names = {name.casefold() for name in new_manifest}
    changed = {name: digest for name, digest in new_manifest.items()
               if old_by_name.get(name.casefold()) != digest}
    deleted = sorted(name for name in old_manifest if name.casefold() not in new_names)
    if not changed and not deleted:
        raise ValueError('No bundle changes to publish')
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f'SpaceDrive-delta-{old_version}-to-{new_version}.zip'
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        def write_member(name, data):
            # A retry must recreate the same immutable delta bytes, including
            # ZIP headers. Wall-clock timestamps would otherwise change its hash.
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)

        write_member('manifest.json', json.dumps({
            'schema_version': 2, 'from_version': old_version, 'to_version': new_version,
        }, sort_keys=True))
        write_member('version.txt', new_version + '\n')
        write_member('DELETED.txt', ''.join(name + '\n' for name in deleted))
        write_member('checksum.sha256', ''.join(
            f'{digest}  {name}\n' for name, digest in sorted(changed.items())
        ))
        for name, expected in sorted(changed.items()):
            # Hash the exact bytes written into the archive, not a second read.
            data = (new_dist_dir / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError(f'Bundle changed while generating delta: {name}')
            write_member('FILES/' + name, data)
    return path, hash_file(path), path.stat().st_size


def _mkdir_p(sftp, path):
    current = ''
    for part in PurePosixPath(path).parts:
        if part == '/':
            continue
        current += '/' + part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


def _remote_hash(sftp, path):
    digest = hashlib.sha256()
    with sftp.open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(sftp, path, optional=False):
    try:
        with sftp.open(path, 'rb') as stream:
            data = stream.read(10 * 1024 * 1024 + 1)
    except FileNotFoundError:
        if optional:
            return None
        raise
    if len(data) > 10 * 1024 * 1024:
        raise ValueError('Remote release metadata is unexpectedly large')
    return json.loads(data.decode('utf-8'))


def _atomic_upload(sftp, local, remote, *, replace=False):
    """Verify a temporary upload, then rename; never expose a partial file."""
    expected_hash, expected_size = hash_file(local), local.stat().st_size
    if not replace:
        try:
            stat = sftp.stat(remote)
        except FileNotFoundError:
            pass
        else:
            if stat.st_size == expected_size and _remote_hash(sftp, remote) == expected_hash:
                return
            raise ValueError(f'Immutable release artifact already exists with different content: {remote}')
    temporary = remote + '.upload-' + uuid.uuid4().hex
    try:
        sftp.put(str(local), temporary, confirm=True)
        if sftp.stat(temporary).st_size != expected_size or _remote_hash(sftp, temporary) != expected_hash:
            raise ValueError(f'Remote upload verification failed: {remote}')
        if replace:
            # OpenSSH's extension replaces an existing pointer atomically. Do
            # not emulate it with remove + rename, which creates an outage.
            sftp.posix_rename(temporary, remote)
        else:
            sftp.rename(temporary, remote)
    finally:
        try:
            sftp.remove(temporary)
        except FileNotFoundError:
            pass


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding='utf-8')
    return path


def _validate_public_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname != 'padek-interactive.tech'
            or parsed.port not in (None, 443) or parsed.path.rstrip('/') != '/releases'
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('VPS_PUBLIC_URL must be https://padek-interactive.tech/releases to match the client allowlist')
    return value.rstrip('/')


def _prune(sftp, directory, pattern, keep, protected):
    candidates = []
    for name in sftp.listdir(directory):
        match = re.fullmatch(pattern, name)
        if match:
            candidates.append((_version_key(match.group(1)), name))
    retained = {name for _, name in sorted(candidates, reverse=True)[:keep]} | protected
    for _, name in candidates:
        if name not in retained:
            sftp.remove(directory + '/' + name)


def publish_release(sftp, installer, dist_dir, releases_path, public_url, keep_versions=5):
    """Commit one prepared release. Callers must validate local provenance first."""
    public_url = _validate_public_url(public_url)
    if not releases_path.startswith('/') or '..' in PurePosixPath(releases_path).parts:
        raise ValueError('VPS_RELEASES_PATH must be an absolute POSIX directory')
    releases_path = releases_path.rstrip('/')
    if not releases_path:
        raise ValueError('Releases must not be published directly in the server root')
    if keep_versions < 1:
        raise ValueError('At least one release must be retained')
    match = re.fullmatch(r'SpaceDrive-Setup-(v\d+\.\d+\.\d+)\.exe', installer.name)
    if not match or not installer.is_file() or installer.stat().st_size <= 0:
        raise ValueError('Expected a nonempty, explicitly versioned installer')
    version = match.group(1)
    if bundle_version(dist_dir) != normalize_version(version):
        raise ValueError('Installer filename and bundled VERSION differ')
    manifest = _hash_folder(dist_dir)
    checksum = hash_file(installer)
    deltas_path, manifests_path = releases_path + '/deltas', releases_path + '/manifests'
    for directory in (releases_path, deltas_path, manifests_path):
        _mkdir_p(sftp, directory)
    lock_path = releases_path + '/.publish-lock'
    sftp.mkdir(lock_path)  # Existing lock prevents concurrent pointer updates.
    try:
        latest_path = releases_path + '/latest.json'
        previous = _read_json(sftp, latest_path, optional=True)
        if previous is not None and (not isinstance(previous, dict) or 'version' not in previous):
            raise ValueError('Published latest.json is invalid; inspect it before publishing')
        old_version = _version(previous['version']) if previous is not None else None
        if old_version and _version_key(old_version) > _version_key(version):
            raise ValueError('Refusing to downgrade the published release pointer')
        if old_version == version:
            if (previous.get('checksum') != checksum
                    or previous.get('size_bytes') != installer.stat().st_size
                    or previous.get('url') != public_url + '/' + installer.name
                    or _remote_hash(sftp, releases_path + '/' + installer.name) != checksum
                    or _read_json(sftp, manifests_path + f'/SpaceDrive-manifest-{version}.json') != manifest):
                raise ValueError('Published version cannot be replaced with different artifacts')
            return previous  # Resume a GitHub draft after a successful VPS commit.
        old_manifest = None
        if old_version:
            old_manifest = _read_json(sftp, manifests_path + f'/SpaceDrive-manifest-{old_version}.json', optional=True)
            if old_manifest is not None:
                _validate_manifest(old_manifest)
        payload = {
            'schema_version': 2, 'version': version,
            'url': public_url + '/' + installer.name,
            'checksum': checksum, 'size_bytes': installer.stat().st_size,
            'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            'delta': {'available': False},
        }
        with tempfile.TemporaryDirectory(prefix='spacedrive-publish-') as tmp:
            temporary = Path(tmp)
            delta = None
            if old_manifest is not None:
                delta, delta_hash, delta_size = _generate_delta(old_manifest, dist_dir, old_version, version, temporary)
                payload['delta_v2'] = {
                    'available': True, 'from_version': old_version, 'to_version': version,
                    'url': public_url + '/deltas/' + delta.name,
                    'checksum': delta_hash, 'size_bytes': delta_size,
                }
            # Prepare every artifact before changing the public pointer.
            _atomic_upload(sftp, installer, releases_path + '/' + installer.name)
            manifest_file = _write_json(temporary / 'manifest.json', manifest)
            _atomic_upload(sftp, manifest_file, manifests_path + f'/SpaceDrive-manifest-{version}.json')
            if delta is not None:
                _atomic_upload(sftp, delta, deltas_path + '/' + delta.name)
            if manifest != _hash_folder(dist_dir) or checksum != hash_file(installer):
                raise ValueError('Local artifacts changed during publication; latest.json remains unchanged')
            _atomic_upload(sftp, _write_json(temporary / 'latest.json', payload), latest_path, replace=True)
        # Pruning is maintenance after a completed transaction. A prune failure
        # must neither roll back the valid pointer nor claim publication failed.
        try:
            _prune(sftp, releases_path, r'SpaceDrive-Setup-(v\d+\.\d+\.\d+)\.exe', keep_versions, {installer.name})
            current_delta = {delta.name} if delta is not None else set()
            _prune(sftp, deltas_path, r'SpaceDrive-delta-v\d+\.\d+\.\d+-to-(v\d+\.\d+\.\d+)\.zip', 3, current_delta)
        except OSError as exc:
            print(f'[WARN] Release is published; old artifact cleanup failed: {exc}')
        return payload
    finally:
        try:
            sftp.rmdir(lock_path)
        except OSError as exc:
            print(f'[WARN] Could not remove publication lock: {exc}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('installer', type=Path)
    parser.add_argument('--dist-dir', type=Path, required=True)
    args = parser.parse_args()
    receipt = verify_build(ROOT, require_clean=True)
    expected = ROOT / 'dist' / receipt['installer']
    if args.installer.resolve() != expected.resolve() or args.dist_dir.resolve() != (ROOT / 'dist/spaceDrive').resolve():
        parser.error('Installer and bundle must be the artifacts covered by the local build receipt')
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if os.environ.get('VPS_KNOWN_HOSTS'):
        client.load_host_keys(os.environ['VPS_KNOWN_HOSTS'])
    # Reject unknown hosts rather than trusting an unauthenticated server key.
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(os.environ['VPS_HOST'], username=os.environ['VPS_USER'],
                       pkey=_load_private_key(os.environ['VPS_SSH_KEY']), timeout=30)
        with client.open_sftp() as sftp:
            result = publish_release(
                sftp, args.installer, args.dist_dir,
                os.environ.get('VPS_RELEASES_PATH', '/var/www/spacedrive/releases'),
                os.environ['VPS_PUBLIC_URL'], int(os.environ.get('VPS_KEEP_VERSIONS', '5')),
            )
        print(f"[SUCCESS] VPS release {result['version']} is fully published")
    finally:
        client.close()


if __name__ == '__main__':
    main()
