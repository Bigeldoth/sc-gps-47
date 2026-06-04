"""Upload build artifacts to VPS via SFTP and generate delta updates."""
import glob
import hashlib
import io
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import paramiko


def _load_private_key(key_content: str) -> paramiko.PKey:
    key_io = io.StringIO(key_content)
    for key_class in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            return key_class.from_private_key(key_io)
        except paramiko.SSHException:
            key_io.seek(0)
    raise ValueError("Unsupported SSH key type — use RSA, Ed25519, ECDSA, or DSS")


def _sftp_mkdir_p(sftp: paramiko.SFTPClient, remote_path: str) -> None:
    parts = [p for p in remote_path.split("/") if p]
    current = ""
    for part in parts:
        current = f"{current}/{part}"
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


def _extract_installer_files(exe_path: Path, extract_dir: Path) -> None:
    """Extract files from Inno Setup .exe (it's a ZIP archive at the end).

    Inno Setup creates a self-extracting executable where the actual files
    are stored in a ZIP archive embedded in the .exe. We extract those files
    to compare with newer versions.
    """
    # Inno Setup embeds a 7z/ZIP archive — find and extract it
    with open(exe_path, 'rb') as f:
        content = f.read()

    # Look for ZIP signature (PK\x03\x04) in the file
    zip_start = content.rfind(b'PK\x03\x04')
    if zip_start == -1:
        raise ValueError(f"No ZIP archive found in {exe_path}")

    # Extract the ZIP data
    zip_data = content[zip_start:]
    with tempfile.NamedTemporaryFile(delete=False, suffix='.zip') as tmp:
        tmp.write(zip_data)
        tmp_path = tmp.name

    try:
        with zipfile.ZipFile(tmp_path, 'r') as zf:
            zf.extractall(extract_dir)
    finally:
        os.unlink(tmp_path)


def _generate_delta(
    old_exe: Path,
    new_exe: Path,
    old_version: str,
    new_version: str,
    output_dir: Path,
) -> tuple[Path, str, int]:
    """Generate a delta .zip between two installers.

    Returns: (delta_zip_path, sha256_checksum, size_bytes)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        old_extracted = tmpdir / "old"
        new_extracted = tmpdir / "new"
        old_extracted.mkdir()
        new_extracted.mkdir()

        print(f"  Extracting old version ({old_version})...")
        _extract_installer_files(old_exe, old_extracted)

        print(f"  Extracting new version ({new_version})...")
        _extract_installer_files(new_exe, new_extracted)

        # Find changed/new files
        delta_files = {}  # {relative_path: new_file_path}
        for file_path in new_extracted.rglob("*"):
            if not file_path.is_file():
                continue

            rel_path = file_path.relative_to(new_extracted)
            old_file = old_extracted / rel_path

            # Check if file is new or changed
            if not old_file.exists():
                delta_files[str(rel_path)] = file_path
            else:
                # Compare file hashes
                with open(file_path, 'rb') as f:
                    new_hash = hashlib.sha256(f.read()).hexdigest()
                with open(old_file, 'rb') as f:
                    old_hash = hashlib.sha256(f.read()).hexdigest()

                if new_hash != old_hash:
                    delta_files[str(rel_path)] = file_path

        # Create delta zip
        delta_zip_name = f"SpaceDrive-delta-{old_version}-to-{new_version}.zip"
        delta_zip_path = output_dir / delta_zip_name

        checksums = {}
        with zipfile.ZipFile(delta_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            # Add version file
            zf.writestr("version.txt", new_version.lstrip('v'))

            # Add changed files
            zf.mkdir("FILES")
            for rel_path, file_path in sorted(delta_files.items()):
                zf.write(file_path, f"FILES/{rel_path}")

                # Calculate checksum
                with open(file_path, 'rb') as f:
                    file_hash = hashlib.sha256(f.read()).hexdigest()
                checksums[rel_path] = file_hash

            # Add checksum manifest
            checksum_data = "\n".join(
                f"{hash_val} {path}"
                for path, hash_val in sorted(checksums.items())
            )
            zf.writestr("checksum.sha256", checksum_data)

        # Calculate delta zip checksum
        with open(delta_zip_path, 'rb') as f:
            zip_hash = hashlib.sha256(f.read()).hexdigest()

        size_bytes = delta_zip_path.stat().st_size
        print(f"  ✓ Delta created: {delta_zip_path.name} ({size_bytes // (1024*1024)} MB)")

        return delta_zip_path, zip_hash, size_bytes


def _cleanup_old_versions(client: paramiko.SSHClient, releases_path: str, keep: int) -> None:
    cmd = f"ls -t {releases_path}/SpaceDrive-Setup-*.exe 2>/dev/null"
    _, stdout, _ = client.exec_command(cmd)
    all_files = [line.strip() for line in stdout.readlines() if line.strip()]

    for old in all_files[keep:]:
        client.exec_command(f"rm -f {old}")
        print(f"Pruned: {os.path.basename(old)}")


def _cleanup_old_deltas(client: paramiko.SSHClient, deltas_path: str, keep: int) -> None:
    """Prune old delta files, keeping only the most recent."""
    cmd = f"ls -t {deltas_path}/SpaceDrive-delta-*.zip 2>/dev/null | tail -n +{keep+1}"
    _, stdout, _ = client.exec_command(cmd)
    old_deltas = [line.strip() for line in stdout.readlines() if line.strip()]

    for old_delta in old_deltas:
        client.exec_command(f"rm -f {old_delta}")
        print(f"Pruned delta: {os.path.basename(old_delta)}")


def _get_latest_installer_version(
    client: paramiko.SSHClient,
    releases_path: str,
) -> str | None:
    """Get the latest installer version from VPS."""
    cmd = f"ls -t {releases_path}/SpaceDrive-Setup-*.exe 2>/dev/null | head -1"
    _, stdout, _ = client.exec_command(cmd)
    lines = stdout.readlines()
    if not lines:
        return None

    file_path = lines[0].strip()
    file_name = os.path.basename(file_path)
    # Extract version from: SpaceDrive-Setup-v0.7.4.exe
    version = file_name.removeprefix("SpaceDrive-Setup-").removesuffix(".exe")
    return version


def _download_installer(
    sftp: paramiko.SFTPClient,
    remote_path: str,
    local_path: Path,
) -> None:
    """Download an installer from VPS."""
    sftp.get(remote_path, str(local_path))


def _upload_latest_json(
    sftp: paramiko.SFTPClient,
    releases_path: str,
    file_name: str,
    public_url: str,
    delta_info: dict | None = None,
) -> None:
    """Upload latest.json with optional delta metadata."""
    version = file_name.removeprefix("SpaceDrive-Setup-").removesuffix(".exe")
    payload = {
        "version": version,
        "url": f"{public_url.rstrip('/')}/{file_name}",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

    if delta_info:
        payload["delta"] = delta_info

    data = json.dumps(payload, indent=2).encode()
    sftp.putfo(io.BytesIO(data), f"{releases_path}/latest.json")
    print(f"Updated latest.json -> version {version}")
    if delta_info:
        print(f"  Delta: {delta_info.get('url', '').split('/')[-1]} "
              f"({delta_info.get('size_bytes', 0) // (1024*1024)} MB)")


def main() -> None:
    host = os.environ["VPS_HOST"]
    user = os.environ["VPS_USER"]
    ssh_key_content = os.environ["VPS_SSH_KEY"]
    releases_path = os.environ.get("VPS_RELEASES_PATH", "/var/www/spacedrive/releases")
    keep_versions = int(os.environ.get("VPS_KEEP_VERSIONS", "5"))
    public_url = os.environ.get("VPS_PUBLIC_URL", "")

    pattern = sys.argv[1] if len(sys.argv) > 1 else "dist/SpaceDrive-Setup-*.exe"
    files = glob.glob(pattern)
    if not files:
        print(f"Error: no files matched '{pattern}'", file=sys.stderr)
        sys.exit(1)

    print(f"🔧 VPS Configuration:")
    print(f"   Host: {host}")
    print(f"   User: {user}")
    print(f"   Releases path: {releases_path}")
    print(f"   Public URL: {public_url}")
    print(f"\n📦 Files to upload: {[os.path.basename(f) for f in files]}\n")

    pkey = _load_private_key(ssh_key_content)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    print(f"🔌 Connecting to {user}@{host}...")
    try:
        client.connect(host, username=user, pkey=pkey, timeout=30)
        print(f"✓ Connected!\n")
    except TimeoutError as e:
        print(f"❌ Connection timeout to {host}:{user}", file=sys.stderr)
        print(f"   This is expected in isolated CI environments (e.g., GitHub Actions)", file=sys.stderr)
        print(f"   Either:", file=sys.stderr)
        print(f"   1. Configure firewall to allow CI runner IPs", file=sys.stderr)
        print(f"   2. Use local release script: .\tools\release.ps1 -Version vX.Y.Z", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"❌ SSH Connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        with client.open_sftp() as sftp:
            _sftp_mkdir_p(sftp, releases_path)
            deltas_path = f"{releases_path}/deltas"
            _sftp_mkdir_p(sftp, deltas_path)

            for file_path in files:
                file_name = os.path.basename(file_path)
                remote = f"{releases_path}/{file_name}"
                new_version = file_name.removeprefix("SpaceDrive-Setup-").removesuffix(".exe")

                print(f"\n📦 Uploading: {file_name}")
                sftp.put(file_path, remote)
                print(f"   ✓ Uploaded to {host}:{remote}")

                # Generate delta if previous version exists
                delta_info = None
                old_version = _get_latest_installer_version(client, releases_path)
                if old_version and old_version != new_version:
                    print(f"\n🔄 Generating delta: {old_version} → {new_version}")
                    try:
                        with tempfile.TemporaryDirectory() as tmpdir:
                            tmpdir = Path(tmpdir)

                            # Download old installer
                            old_file = tmpdir / f"SpaceDrive-Setup-{old_version}.exe"
                            old_remote = f"{releases_path}/SpaceDrive-Setup-{old_version}.exe"
                            _download_installer(sftp, old_remote, old_file)

                            # Generate delta
                            delta_zip, delta_checksum, delta_size = _generate_delta(
                                old_file,
                                Path(file_path),
                                old_version,
                                new_version,
                                tmpdir,
                            )

                            # Upload delta
                            delta_name = delta_zip.name
                            delta_remote = f"{deltas_path}/{delta_name}"
                            sftp.put(str(delta_zip), delta_remote)
                            print(f"   ✓ Delta uploaded to {host}:{delta_remote}")

                            # Prepare delta metadata
                            delta_info = {
                                "available": True,
                                "min_version": old_version,
                                "url": f"{public_url.rstrip('/')}/deltas/{delta_name}",
                                "checksum": delta_checksum,
                                "size_bytes": delta_size,
                                "notes": f"Delta update from {old_version} to {new_version}",
                            }
                    except Exception as exc:
                        print(f"   ⚠️  Delta generation failed: {exc}")
                        print(f"   Using full installer only")

                # Upload latest.json
                if public_url:
                    _upload_latest_json(sftp, releases_path, file_name, public_url, delta_info)

            # Cleanup old versions
            print(f"\n🧹 Cleaning up old versions (keeping {keep_versions})...")
            _cleanup_old_versions(client, releases_path, keep_versions)

            # Cleanup old deltas
            print(f"🧹 Cleaning up old deltas (keeping 3)...")
            _cleanup_old_deltas(client, deltas_path, keep=3)

    finally:
        client.close()

    print(f"\n✅ Release complete!")


if __name__ == "__main__":
    main()
