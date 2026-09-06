"""Central version synchronization and local build provenance checks.

Run ``python tools/versioning.py sync --version vX.Y.Z`` before committing a
version change. Build and release only check versions; they never bump them.
"""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def normalize_version(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'v?(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)', value):
        raise ValueError('Version must match vX.Y.Z exactly')
    return value.removeprefix('v')


def read_version(root=ROOT) -> str:
    return normalize_version((root / 'VERSION').read_text(encoding='utf-8-sig').strip())


def check_versions(root=ROOT, expected=None) -> str:
    version = read_version(root)
    if expected is not None and normalize_version(expected) != version:
        raise ValueError(f'Requested version {expected} differs from VERSION ({version})')
    config = (root / 'config.ini').read_text(encoding='utf-8-sig')
    installer = (root / 'installer/spaceDrive.iss').read_text(encoding='utf-8-sig')
    config_versions = re.findall(r'^app_version\s*=\s*(\S+)\s*$', config, re.M)
    installer_versions = re.findall(r'^\s*#define MyAppVersion "([^"]+)"', installer, re.M)
    if config_versions != [f'v{version}'] or installer_versions != [version]:
        raise ValueError('Version mirrors differ; run tools/versioning.py sync, review and commit')
    return version


def sync_versions(version: str, root=ROOT):
    version = normalize_version(version)
    mirrors = [
        (root / 'config.ini', r'^app_version\s*=.*$', f'app_version = v{version}'),
        (root / 'installer/spaceDrive.iss', r'(^\s*#define MyAppVersion )"[^"]+"',
         rf'\g<1>"{version}"'),
    ]
    updates = []
    for path, pattern, replacement in mirrors:
        text = path.read_text(encoding='utf-8-sig')
        updated, count = re.subn(pattern, replacement, text, flags=re.M)
        if count != 1:
            raise ValueError(f'Expected exactly one version declaration in {path.name}')
        updates.append((path, updated))
    for path, text in updates:
        path.write_text(text, encoding='utf-8')
    (root / 'VERSION').write_text(version + '\n', encoding='utf-8')


def git_output(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args]).decode('utf-8').strip()


def source_state(root=ROOT) -> dict:
    """Hash tracked inputs and record whether the checkout is release-ready."""
    paths = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).split(b'\0')
    digest = hashlib.sha256()
    for raw in sorted(path for path in paths if path):
        path = root / raw.decode('utf-8')
        digest.update(raw + b'\0')
        digest.update(path.read_bytes() if path.is_file() else b'<missing>')
        digest.update(b'\0')
    return {
        'commit': git_output(root, 'rev-parse', 'HEAD'),
        'source_sha256': digest.hexdigest(),
        'clean': not bool(git_output(root, 'status', '--porcelain', '--untracked-files=normal')),
    }


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def hash_folder(path: Path) -> dict:
    return {item.relative_to(path).as_posix(): hash_file(item)
            for item in sorted(path.rglob('*')) if item.is_file()}


def bundle_version(bundle: Path) -> str:
    # Current PyInstaller places data under _internal; accept older flat builds.
    path = bundle / '_internal/VERSION'
    if not path.is_file():
        path = bundle / 'VERSION'
    return normalize_version(path.read_text(encoding='utf-8-sig').strip())


def receipt_path(root, kind):
    return root / 'dist' / f'{kind}-provenance.json'


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding='utf-8')
    temporary.replace(path)


def snapshot_build(root=ROOT, expected=None):
    write_json(receipt_path(root, 'source'), {
        'version': check_versions(root, expected), 'source': source_state(root),
    })


def record_bundle(root=ROOT, expected=None):
    version = check_versions(root, expected)
    source = source_state(root)
    snapshot = json.loads(receipt_path(root, 'source').read_text(encoding='utf-8'))
    if snapshot != {'version': version, 'source': source}:
        raise ValueError('Source changed during build; rebuild before packaging')
    bundle = root / 'dist/spaceDrive'
    if not (bundle / 'spaceDrive.exe').is_file() or bundle_version(bundle) != version:
        raise ValueError('Bundle executable or bundled VERSION is invalid')
    write_json(receipt_path(root, 'bundle'), {
        'version': version, 'source': source, 'files': hash_folder(bundle),
    })


def verify_bundle(root=ROOT, expected=None) -> dict:
    version = check_versions(root, expected)
    receipt = json.loads(receipt_path(root, 'bundle').read_text(encoding='utf-8'))
    bundle = root / 'dist/spaceDrive'
    if (receipt.get('version') != version or receipt.get('source') != source_state(root)
            or receipt.get('files') != hash_folder(bundle) or bundle_version(bundle) != version):
        raise ValueError('Stale or modified bundle; run a fresh build without -SkipPyInstaller')
    return receipt


def record_build(root=ROOT, expected=None):
    bundle = verify_bundle(root, expected)
    installer = root / 'dist' / f"SpaceDrive-Setup-v{bundle['version']}.exe"
    if not installer.is_file() or not installer.stat().st_size:
        raise ValueError('Expected versioned installer was not produced')
    write_json(receipt_path(root, 'build'), {
        **bundle, 'installer': installer.name, 'installer_sha256': hash_file(installer),
        'installer_size': installer.stat().st_size,
    })


def verify_build(root=ROOT, expected=None, require_clean=False) -> dict:
    bundle = verify_bundle(root, expected)
    receipt = json.loads(receipt_path(root, 'build').read_text(encoding='utf-8'))
    installer = root / 'dist' / f"SpaceDrive-Setup-v{bundle['version']}.exe"
    if require_clean and not bundle['source']['clean']:
        raise ValueError('Release requires a clean committed checkout and a build from that commit')
    for key in ('version', 'source', 'files'):
        if receipt.get(key) != bundle[key]:
            raise ValueError('Installer provenance differs from the bundle')
    if (receipt.get('installer') != installer.name
            or receipt.get('installer_sha256') != hash_file(installer)
            or receipt.get('installer_size') != installer.stat().st_size):
        raise ValueError('Installer changed since build; rebuild before publishing')
    return receipt


def release_preflight(root=ROOT, expected=None):
    version = check_versions(root, expected)
    state = source_state(root)
    if not state['clean']:
        raise ValueError('Commit version changes and all source files before releasing')
    tag = f'v{version}'
    if git_output(root, 'tag', '--list', tag):
        commit = git_output(root, 'rev-parse', f'{tag}^{{commit}}')
        if commit != state['commit']:
            raise ValueError(f'{tag} already identifies a different commit; do not reuse a release version')
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=(
        'show', 'check', 'sync', 'snapshot-build', 'record-bundle', 'verify-bundle',
        'record-build', 'verify-build', 'release-preflight',
    ))
    parser.add_argument('--version')
    parser.add_argument('--release', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'show':
            print('v' + check_versions(expected=args.version))
        elif args.action == 'sync':
            sync_versions(args.version or read_version())
        elif args.action == 'check':
            check_versions(expected=args.version)
        elif args.action == 'verify-build':
            verify_build(expected=args.version, require_clean=args.release)
        else:
            globals()[args.action.replace('-', '_')](expected=args.version)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Version/build validation failed: {exc}\n')


if __name__ == '__main__':
    main()
