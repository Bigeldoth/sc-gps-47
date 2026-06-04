"""Upload build artifacts to VPS via SFTP and generate delta updates.

Delta generation uses a manifest-based approach:
- After each build, a manifest (file->SHA256) of dist/spaceDrive/ is uploaded
  alongside the installer so the next release can compute a true delta.
- Inno Setup .exe files are NOT extracted (proprietary format); instead we
  compare the PyInstaller one-folder output (dist/spaceDrive/) directly.

Usage:
    python tools/upload_vps.py dist/SpaceDrive-Setup-v0.7.8.exe [--dist-dir dist/spaceDrive]

    --dist-dir   Path to the PyInstaller one-folder output. When supplied,
                 a file manifest is generated and a delta .zip is produced
                 by comparing against the previous version's manifest on VPS.
                 When omitted, delta generation is skipped gracefully.
"""
import glob
import hashlib
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import paramiko


# ---------------------------------------------------------------------------
# SSH / SFTP helpers
# ---------------------------------------------------------------------------

def _load_private_key(key_content: str) -> paramiko.PKey:
    key_io = io.StringIO(key_content)
    for key_class in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            return key_class.from_private_key(key_io)
        except paramiko.SSHException:
            key_io.seek(0)
    raise ValueError("Unsupported SSH key type -- use RSA, Ed25519, or ECDSA")


def _sftp_mkdir_p(sftp: paramiko.SFTPClient, remote_path: str) -> None:
    parts = [p for p in remote_path.split("/") if p]
    current = ""
    for part in parts:
        current = f"{current}/{part}"
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


# ---------------------------------------------------------------------------
# Manifest helpers  (replaces broken _extract_installer_files)
# ---------------------------------------------------------------------------

def _hash_folder(folder: Path) -> dict:
    """Return {posix_rel_path: sha256} for every file under folder."""
    manifest = {}
    for fp in sorted(folder.rglob("*")):
        if not fp.is_file():
            continue
        rel = fp.relative_to(folder).as_posix()
        with open(fp, "rb") as f:
            manifest[rel] = hashlib.sha256(f.read()).hexdigest()
    return manifest


def _download_manifest(sftp: paramiko.SFTPClient, manifests_path: str, version: str) -> dict | None:
    """Download the file manifest for *version* from VPS. Returns None if absent."""
    remote = f"{manifests_path}/SpaceDrive-manifest-{version}.json"
    try:
        buf = io.BytesIO()
        sftp.getfo(remote, buf)
        return json.loads(buf.getvalue())
    except FileNotFoundError:
        return None
    except Exception as exc:
        print(f"  [WARN] Could not download manifest for {version}: {exc}")
        return None


def _upload_manifest(sftp: paramiko.SFTPClient, manifests_path: str, version: str, manifest: dict) -> None:
    """Upload file manifest for *version* to VPS."""
    remote = f"{manifests_path}/SpaceDrive-manifest-{version}.json"
    data = json.dumps(manifest, indent=2, sort_keys=True).encode()
    sftp.putfo(io.BytesIO(data), remote)
    print(f"  [OK] Manifest uploaded: {len(manifest)} files -> {remote}")


# ---------------------------------------------------------------------------
# Delta generation
# ---------------------------------------------------------------------------

def _generate_delta(
    old_manifest: dict,
    new_dist_dir: Path,
    old_version: str,
    new_version: str,
    output_dir: Path,
) -> tuple:
    """Generate a delta .zip by comparing *old_manifest* to *new_dist_dir*.

    Returns: (delta_zip_path, sha256_checksum, size_bytes)
    """
    new_manifest = _hash_folder(new_dist_dir)

    # Find files that are new or changed
    delta_files = []
    for rel_posix, new_hash in new_manifest.items():
        if old_manifest.get(rel_posix) != new_hash:
            abs_path = new_dist_dir / Path(rel_posix)
            delta_files.append((rel_posix, abs_path))

    total = len(new_manifest)
    changed = len(delta_files)
    print(f"  {changed} changed/new files out of {total} total")

    if not delta_files:
        raise ValueError("No changed files between versions -- delta would be empty")

    delta_zip_name = f"SpaceDrive-delta-{old_version}-to-{new_version}.zip"
    delta_zip_path = output_dir / delta_zip_name

    checksums = {}
    with zipfile.ZipFile(delta_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("version.txt", new_version.lstrip("v"))

        for rel_posix, abs_path in sorted(delta_files):
            zf.write(abs_path, f"FILES/{rel_posix}")
            with open(abs_path, "rb") as f:
                checksums[rel_posix] = hashlib.sha256(f.read()).hexdigest()

        checksum_data = "\n".join(f"{h} {p}" for p, h in sorted(checksums.items()))
        zf.writestr("checksum.sha256", checksum_data)

    with open(delta_zip_path, "rb") as f:
        zip_hash = hashlib.sha256(f.read()).hexdigest()

    size_bytes = delta_zip_path.stat().st_size
    print(f"  [OK] Delta: {delta_zip_name} ({size_bytes / (1024*1024):.1f} MB)")
    return delta_zip_path, zip_hash, size_bytes


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------

def _cleanup_old_versions(client: paramiko.SSHClient, releases_path: str, keep: int) -> None:
    """Prune old installer .exe files, keeping the *keep* most recent."""
    cmd = f"ls -t {releases_path}/SpaceDrive-Setup-*.exe 2>/dev/null"
    _, stdout, _ = client.exec_command(cmd)
    all_files = [ln.strip() for ln in stdout.readlines() if ln.strip()]
    for old in all_files[keep:]:
        client.exec_command(f"rm -f {old}")
        print(f"  Pruned installer: {os.path.basename(old)}")


def _cleanup_old_deltas(client: paramiko.SSHClient, deltas_path: str, keep: int) -> None:
    """Prune old delta .zip files, keeping the *keep* most recent."""
    cmd = f"ls -t {deltas_path}/SpaceDrive-delta-*.zip 2>/dev/null | tail -n +{keep+1}"
    _, stdout, _ = client.exec_command(cmd)
    old_deltas = [ln.strip() for ln in stdout.readlines() if ln.strip()]
    for old in old_deltas:
        client.exec_command(f"rm -f {old}")
        print(f"  Pruned delta: {os.path.basename(old)}")


def _get_latest_installer_version(client: paramiko.SSHClient, releases_path: str) -> str | None:
    """Return the version string of the most recent installer already on VPS."""
    cmd = f"ls -t {releases_path}/SpaceDrive-Setup-*.exe 2>/dev/null | head -1"
    _, stdout, _ = client.exec_command(cmd)
    lines = stdout.readlines()
    if not lines:
        return None
    file_name = os.path.basename(lines[0].strip())
    return file_name.removeprefix("SpaceDrive-Setup-").removesuffix(".exe")


# ---------------------------------------------------------------------------
# latest.json
# ---------------------------------------------------------------------------

def _upload_latest_json(
    sftp: paramiko.SFTPClient,
    releases_path: str,
    file_name: str,
    public_url: str,
    delta_info: dict | None = None,
) -> None:
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
    print(f"  [OK] latest.json -> version {version}")
    if delta_info:
        size_mb = delta_info.get("size_bytes", 0) / (1024 * 1024)
        print(f"       delta: {delta_info['url'].split('/')[-1]} ({size_mb:.1f} MB)")
    else:
        print(f"       (no delta -- full installer only)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    host = os.environ["VPS_HOST"]
    user = os.environ["VPS_USER"]
    ssh_key_content = os.environ["VPS_SSH_KEY"]
    releases_path = os.environ.get("VPS_RELEASES_PATH", "/var/www/spacedrive/releases")
    keep_versions = int(os.environ.get("VPS_KEEP_VERSIONS", "5"))
    public_url = os.environ.get("VPS_PUBLIC_URL", "")

    # Parse args: positional = installer glob, optional --dist-dir <path>
    args = sys.argv[1:]
    dist_dir: Path | None = None
    installer_pattern = "dist/SpaceDrive-Setup-*.exe"

    i = 0
    while i < len(args):
        if args[i] == "--dist-dir" and i + 1 < len(args):
            dist_dir = Path(args[i + 1])
            i += 2
        else:
            installer_pattern = args[i]
            i += 1

    files = glob.glob(installer_pattern)
    if not files:
        print(f"[ERROR] No files matched '{installer_pattern}'", file=sys.stderr)
        sys.exit(1)

    print(f"[CONFIG] VPS Configuration:")
    print(f"   Host: {host}")
    print(f"   User: {user}")
    print(f"   Releases path: {releases_path}")
    print(f"   Public URL: {public_url}")
    if dist_dir:
        print(f"   Dist dir: {dist_dir} (delta generation enabled)")
    else:
        print(f"   Dist dir: not provided (delta generation DISABLED)")
    print(f"\n[FILES] Files to upload: {[os.path.basename(f) for f in files]}\n")

    pkey = _load_private_key(ssh_key_content)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    print(f"[SSH] Connecting to {user}@{host}...")
    try:
        client.connect(host, username=user, pkey=pkey, timeout=30)
        print(f"[OK] Connected!\n")
    except TimeoutError:
        print(f"[ERROR] Connection timeout to {host}", file=sys.stderr)
        print(f"        This is expected in isolated CI environments.", file=sys.stderr)
        print(f"        Use local release script: .\\tools\\release.ps1 -Version vX.Y.Z", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"[ERROR] SSH Connection failed: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with client.open_sftp() as sftp:
            _sftp_mkdir_p(sftp, releases_path)
            deltas_path = f"{releases_path}/deltas"
            manifests_path = f"{releases_path}/manifests"
            _sftp_mkdir_p(sftp, deltas_path)
            _sftp_mkdir_p(sftp, manifests_path)

            for file_path in files:
                file_name = os.path.basename(file_path)
                remote = f"{releases_path}/{file_name}"
                new_version = file_name.removeprefix("SpaceDrive-Setup-").removesuffix(".exe")

                # --- Fetch old version info BEFORE uploading ---
                old_version = _get_latest_installer_version(client, releases_path)
                old_manifest: dict | None = None
                if old_version and old_version != new_version and dist_dir:
                    old_manifest = _download_manifest(sftp, manifests_path, old_version)
                    if old_manifest:
                        print(f"  [OK] Old manifest found for {old_version} ({len(old_manifest)} files)")
                    else:
                        print(f"  [WARN] No manifest found for {old_version} -- delta skipped this release")

                # --- Upload new installer ---
                print(f"\n[UPLOAD] Uploading: {file_name}")
                sftp.put(file_path, remote)
                print(f"  [OK] Uploaded to {host}:{remote}")

                # --- Upload manifest for new version ---
                delta_info = None
                if dist_dir and dist_dir.is_dir():
                    print(f"\n[MANIFEST] Generating manifest for {new_version}...")
                    new_manifest = _hash_folder(dist_dir)
                    _upload_manifest(sftp, manifests_path, new_version, new_manifest)

                    # --- Generate delta if we have old manifest ---
                    if old_manifest:
                        print(f"\n[DELTA] Generating delta: {old_version} -> {new_version}")
                        try:
                            import tempfile
                            with tempfile.TemporaryDirectory() as tmpdir:
                                delta_zip, delta_checksum, delta_size = _generate_delta(
                                    old_manifest,
                                    dist_dir,
                                    old_version,
                                    new_version,
                                    Path(tmpdir),
                                )
                                delta_name = delta_zip.name
                                delta_remote = f"{deltas_path}/{delta_name}"
                                sftp.put(str(delta_zip), delta_remote)
                                print(f"  [OK] Delta uploaded: {delta_remote}")

                            delta_info = {
                                "available": True,
                                "min_version": old_version,
                                "url": f"{public_url.rstrip('/')}/deltas/{delta_name}",
                                "checksum": delta_checksum,
                                "size_bytes": delta_size,
                                "notes": f"Delta update from {old_version} to {new_version}",
                            }
                        except Exception as exc:
                            print(f"  [WARN] Delta generation failed: {exc}")
                            print(f"  Full installer only for this release.")
                else:
                    if dist_dir:
                        print(f"  [WARN] --dist-dir {dist_dir} not found, skipping manifest/delta")

                # --- Update latest.json ---
                if public_url:
                    print(f"\n[LATEST] Updating latest.json...")
                    _upload_latest_json(sftp, releases_path, file_name, public_url, delta_info)

            # --- Cleanup ---
            print(f"\n[CLEANUP] Keeping {keep_versions} most recent installers...")
            _cleanup_old_versions(client, releases_path, keep_versions)
            print(f"[CLEANUP] Keeping 3 most recent deltas...")
            _cleanup_old_deltas(client, deltas_path, keep=3)

    finally:
        client.close()

    print(f"\n[SUCCESS] Release complete!")


if __name__ == "__main__":
    main()
