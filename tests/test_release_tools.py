"""Release preparation and publication are exercised without a network connection."""

import hashlib
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import versioning
import upload_vps as publish


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.STDOUT)


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'installer').mkdir()
    (tmp_path / 'VERSION').write_text('1.0.0\n')
    (tmp_path / 'config.ini').write_text('[App]\napp_version = v1.0.0\n')
    (tmp_path / 'installer/spaceDrive.iss').write_text('  #define MyAppVersion "1.0.0"\n')
    (tmp_path / '.gitignore').write_text('dist/\n')
    git(tmp_path, 'init')
    git(tmp_path, 'add', '.')
    git(tmp_path, '-c', 'user.name=Release tests', '-c', 'user.email=tests@example.invalid',
        'commit', '-m', 'Fixture sources')
    return tmp_path


def build_fixture(root):
    versioning.snapshot_build(root)
    bundle = root / 'dist/spaceDrive'
    (bundle / '_internal').mkdir(parents=True)
    (bundle / 'spaceDrive.exe').write_bytes(b'fixture executable')
    (bundle / '_internal/VERSION').write_text('1.0.0\n')
    versioning.record_bundle(root)
    (root / 'dist/SpaceDrive-Setup-v1.0.0.exe').write_bytes(b'fixture installer')
    versioning.record_build(root)


def test_versions_are_synchronized_by_explicit_command_and_checked_before_build(project):
    assert versioning.check_versions(project, 'v1.0.0') == '1.0.0'
    with pytest.raises(ValueError, match='differs'):
        versioning.check_versions(project, 'v1.0.1')
    (project / 'config.ini').write_text('[App]\napp_version = v0.7.12\n')
    with pytest.raises(ValueError, match='mirrors'):
        versioning.check_versions(project)
    versioning.sync_versions('v1.0.0', project)
    assert versioning.check_versions(project) == '1.0.0'


@pytest.mark.parametrize('value', ['v1.0.0-extra', 'v1.0.0\n', '1.0', '../1.0.0', 'v1.0.0.exe', 'v01.0.0'])
def test_version_validation_rejects_partial_matches(value):
    with pytest.raises(ValueError):
        versioning.normalize_version(value)


def test_skip_build_requires_matching_sources_installer_and_bundle(project):
    build_fixture(project)
    assert versioning.verify_build(project, 'v1.0.0', require_clean=True)['source']['clean']
    installer = project / 'dist/SpaceDrive-Setup-v1.0.0.exe'
    installer.write_bytes(b'modified binary')
    with pytest.raises(ValueError, match='Installer changed'):
        versioning.verify_build(project, require_clean=True)


def test_build_provenance_detects_dirty_source_and_stale_bundle(project):
    build_fixture(project)
    (project / 'config.ini').write_text('[App]\napp_version = v1.0.0\n# changed\n')
    with pytest.raises(ValueError, match='Stale'):
        versioning.verify_bundle(project)
    with pytest.raises(ValueError, match='Commit'):
        versioning.release_preflight(project)


def test_build_refuses_inputs_changed_after_compilation_started(project):
    versioning.snapshot_build(project)
    (project / 'config.ini').write_text('[App]\napp_version = v1.0.0\n# changed\n')
    with pytest.raises(ValueError, match='Source changed'):
        versioning.record_bundle(project)


def test_release_refuses_tag_pointing_to_other_sources(project, monkeypatch):
    head = versioning.git_output(project, 'rev-parse', 'HEAD')
    original = versioning.git_output

    def fake_tag(root, *args):
        if args[:2] == ('tag', '--list'):
            return 'v1.0.0'
        if args == ('rev-parse', 'v1.0.0^{commit}'):
            return 'a' * 40 if head != 'a' * 40 else 'b' * 40
        return original(root, *args)

    monkeypatch.setattr(versioning, 'git_output', fake_tag)
    with pytest.raises(ValueError, match='different commit'):
        versioning.release_preflight(project)


def test_runtime_version_does_not_read_mutable_user_configuration(project, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    import app_version
    monkeypatch.setattr(app_version, 'bundle_dir', lambda: project)
    (project / 'config.ini').write_text('[App]\napp_version = v99.0.0\n')
    assert app_version.get_app_version() == 'v1.0.0'
    (project / 'VERSION').unlink()
    assert app_version.get_app_version() == 'unknown'


class FakeSFTP:
    def __init__(self):
        self.files = {}
        self.dirs = {'/'}
        self.events = []
        self.fail_upload = None
        self.corrupt_upload = False
        self.fail_pointer = False
        self.fail_prune = False

    def stat(self, path):
        if path in self.files:
            return SimpleNamespace(st_size=len(self.files[path]))
        if path in self.dirs:
            return SimpleNamespace(st_size=0)
        raise FileNotFoundError(path)

    def mkdir(self, path):
        if path in self.dirs:
            raise FileExistsError(path)
        self.dirs.add(path)

    def rmdir(self, path):
        self.dirs.remove(path)

    def open(self, path, mode):
        if path not in self.files:
            raise FileNotFoundError(path)
        return io.BytesIO(self.files[path])

    def put(self, local, remote, confirm=True):
        if self.fail_upload and self.fail_upload in remote:
            raise OSError('Injected upload failure')
        self.files[remote] = b'corrupt' if self.corrupt_upload else Path(local).read_bytes()
        self.events.append(('upload', remote))

    def rename(self, source, destination):
        if destination in self.files:
            raise FileExistsError(destination)
        self.files[destination] = self.files.pop(source)
        self.events.append(('rename', destination))

    def posix_rename(self, source, destination):
        if self.fail_pointer:
            raise OSError('Injected atomic pointer failure')
        self.files[destination] = self.files.pop(source)
        self.events.append(('commit', destination))

    def remove(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]

    def listdir(self, path):
        if self.fail_prune:
            raise OSError('Injected cleanup failure')
        prefix = path + '/'
        return [name[len(prefix):] for name in self.files
                if name.startswith(prefix) and '/' not in name[len(prefix):]]


@pytest.fixture
def release_files(tmp_path):
    bundle = tmp_path / 'bundle'
    (bundle / '_internal').mkdir(parents=True)
    (bundle / 'spaceDrive.exe').write_bytes(b'new exe')
    (bundle / '_internal/VERSION').write_text('1.0.1')
    installer = tmp_path / 'SpaceDrive-Setup-v1.0.1.exe'
    installer.write_bytes(b'new installer')
    return installer, bundle


def prepare_previous(sftp):
    previous = {'version': 'v1.0.0', 'url': 'https://example.invalid/releases/old.exe'}
    sftp.files['/releases/latest.json'] = json.dumps(previous).encode()
    manifest = {'spaceDrive.exe': hashlib.sha256(b'old exe').hexdigest(),
                '_internal/obsolete.dll': hashlib.sha256(b'old dll').hexdigest()}
    sftp.files['/releases/manifests/SpaceDrive-manifest-v1.0.0.json'] = json.dumps(manifest).encode()
    return sftp.files['/releases/latest.json']


def run_publish(sftp, files):
    return publish.publish_release(sftp, *files, '/releases', 'https://padek-interactive.tech/releases')


def test_delta_exact_base_deletions_and_all_checksums_are_posix(tmp_path, release_files):
    _, bundle = release_files
    old = {'_internal/obsolete.dll': hashlib.sha256(b'old').hexdigest()}
    path, digest, size = publish._generate_delta(old, bundle, 'v1.0.0', 'v1.0.1', tmp_path)
    assert digest == versioning.hash_file(path) and size == path.stat().st_size
    with zipfile.ZipFile(path) as archive:
        assert archive.read('DELETED.txt') == b'_internal/obsolete.dll\n'
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest == {'schema_version': 2, 'from_version': 'v1.0.0', 'to_version': 'v1.0.1'}
        files = {name.removeprefix('FILES/'): archive.read(name)
                 for name in archive.namelist() if name.startswith('FILES/')}
        checksums = dict(line.split('  ', 1)[::-1]
                         for line in archive.read('checksum.sha256').decode().splitlines())
        assert set(files) == set(checksums)
        for name, data in files.items():
            assert '\\' not in name and hashlib.sha256(data).hexdigest() == checksums[name]


def test_produced_delta_is_accepted_by_runtime_validator(tmp_path, release_files):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from update_transaction import extract_delta
    _, bundle = release_files
    old = {'_internal/obsolete.dll': hashlib.sha256(b'old').hexdigest()}
    archive, _, _ = publish._generate_delta(old, bundle, 'v1.0.0', 'v1.0.1', tmp_path)
    files, deleted = extract_delta(archive, tmp_path / 'extracted', 'v1.0.0', 'v1.0.1')
    assert {item['path'] for item in files} == set(publish._hash_folder(bundle))
    assert deleted == ['_internal/obsolete.dll']


@pytest.mark.parametrize('new_content', [b'old dll', b'new dll'])
def test_case_only_windows_rename_never_deletes_the_updated_file(tmp_path, release_files, new_content):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from update_transaction import extract_delta
    _, bundle = release_files
    (bundle / '_internal/foo.dll').write_bytes(new_content)
    old = {'spaceDrive.exe': hashlib.sha256(b'new exe').hexdigest(),
           '_internal/VERSION': hashlib.sha256(b'1.0.0').hexdigest(),
           '_internal/Foo.dll': hashlib.sha256(b'old dll').hexdigest()}
    archive, _, _ = publish._generate_delta(old, bundle, 'v1.0.0', 'v1.0.1', tmp_path)
    files, deleted = extract_delta(archive, tmp_path / 'extracted', 'v1.0.0', 'v1.0.1')
    assert deleted == []
    changed = {item['path'] for item in files}
    assert ('_internal/foo.dll' in changed) == (new_content != b'old dll')
    assert '_internal/VERSION' in changed


@pytest.mark.parametrize('url', [
    'https://example.invalid/releases', 'http://padek-interactive.tech/releases',
    'https://padek-interactive.tech:444/releases', 'https://padek-interactive.tech/downloads',
    'https://padek-interactive.tech/releases?x=1', 'https://user@padek-interactive.tech/releases',
    'https://padek-interactive.tech/releases/../other',
])
def test_publisher_rejects_urls_which_installed_clients_cannot_use(url):
    with pytest.raises(ValueError):
        publish._validate_public_url(url)


def test_publisher_url_is_accepted_by_installed_client():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from update_manager import _release_url
    base = publish._validate_public_url('https://padek-interactive.tech/releases/')
    assert _release_url(base + '/SpaceDrive-Setup-v1.0.1.exe', '.exe')


def test_deletion_only_delta_remains_valid(tmp_path, release_files):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from update_transaction import extract_delta
    _, bundle = release_files
    old = {**publish._hash_folder(bundle), '_internal/obsolete.dll': hashlib.sha256(b'old').hexdigest()}
    archive, _, _ = publish._generate_delta(old, bundle, 'v1.0.0', 'v1.0.1', tmp_path)
    files, deleted = extract_delta(archive, tmp_path / 'extracted', 'v1.0.0', 'v1.0.1')
    assert files == [] and deleted == ['_internal/obsolete.dll']


def test_publication_commits_pointer_last_and_old_clients_get_full_installer(release_files):
    sftp = FakeSFTP()
    prepare_previous(sftp)
    # A stray newer file must not select the base; latest.json is authoritative.
    sftp.files['/releases/SpaceDrive-Setup-v9.0.0.exe'] = b'stray'
    result = run_publish(sftp, release_files)
    assert sftp.events[-1] == ('commit', '/releases/latest.json')
    assert result['delta'] == {'available': False}
    assert result['delta_v2']['from_version'] == 'v1.0.0'
    assert result['checksum'] == versioning.hash_file(release_files[0])
    assert json.loads(sftp.files['/releases/latest.json']) == result
    assert run_publish(sftp, release_files) == result


@pytest.mark.parametrize('failure', ['installer', 'manifest', 'delta', 'corruption', 'pointer'])
def test_failed_publication_leaves_old_pointer_intact(release_files, failure):
    sftp = FakeSFTP()
    old = prepare_previous(sftp)
    if failure in ('installer', 'manifest', 'delta'):
        sftp.fail_upload = {'installer': '/SpaceDrive-Setup-', 'manifest': '/manifests/', 'delta': '/deltas/'}[failure]
    sftp.corrupt_upload = failure == 'corruption'
    sftp.fail_pointer = failure == 'pointer'
    with pytest.raises((ValueError, OSError)):
        run_publish(sftp, release_files)
    assert sftp.files['/releases/latest.json'] == old
    assert not any('.upload-' in path for path in sftp.files)
    assert '/releases/.publish-lock' not in sftp.dirs


def test_cleanup_failure_does_not_invalidate_completed_publication(release_files, capsys):
    sftp = FakeSFTP()
    sftp.fail_prune = True
    result = run_publish(sftp, release_files)
    assert json.loads(sftp.files['/releases/latest.json']) == result
    assert 'Release is published' in capsys.readouterr().out


def test_interrupted_pointer_commit_can_resume_with_immutable_artifacts(release_files):
    sftp = FakeSFTP()
    prepare_previous(sftp)
    sftp.fail_pointer = True
    with pytest.raises(OSError):
        run_publish(sftp, release_files)
    old_delta = {name: data for name, data in sftp.files.items() if name.endswith('.zip')}
    sftp.fail_pointer = False
    result = run_publish(sftp, release_files)
    assert result['version'] == 'v1.0.1'
    assert old_delta == {name: data for name, data in sftp.files.items() if name.endswith('.zip')}


def test_immutable_artifact_cannot_be_overwritten(release_files):
    sftp = FakeSFTP()
    old = prepare_previous(sftp)
    sftp.files['/releases/' + release_files[0].name] = b'another build of same version'
    with pytest.raises(ValueError, match='different content'):
        run_publish(sftp, release_files)
    assert sftp.files['/releases/latest.json'] == old


def test_mismatched_bundle_cannot_publish_under_installer_filename(release_files):
    sftp = FakeSFTP()
    (release_files[1] / '_internal/VERSION').write_text('1.0.2')
    with pytest.raises(ValueError, match='filename and bundled VERSION'):
        run_publish(sftp, release_files)
    assert not sftp.events


@pytest.mark.parametrize('path', ['../file', 'C:/file', 'a\\b', 'config.ini', 'data/user_poi.json',
                                'logs/x', 'updates/x', '.env.local', 'CON.txt'])
def test_delta_rejects_user_data_or_unsafe_manifest_paths(path):
    with pytest.raises(ValueError):
        publish._validate_manifest({path: 'a' * 64})
