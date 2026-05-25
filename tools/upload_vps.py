"""Upload build artifacts to VPS via SFTP and prune old releases."""
import glob
import io
import json
import os
import sys
from datetime import datetime, timezone

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


def _cleanup_old_versions(client: paramiko.SSHClient, releases_path: str, keep: int) -> None:
    cmd = f"ls -t {releases_path}/SpaceDrive-Setup-*.exe 2>/dev/null"
    _, stdout, _ = client.exec_command(cmd)
    all_files = [line.strip() for line in stdout.readlines() if line.strip()]

    for old in all_files[keep:]:
        client.exec_command(f"rm -f {old}")
        print(f"Pruned: {os.path.basename(old)}")


def _upload_latest_json(
    sftp: paramiko.SFTPClient,
    releases_path: str,
    file_name: str,
    public_url: str,
) -> None:
    version = file_name.removeprefix("SpaceDrive-Setup-").removesuffix(".exe")
    payload = {
        "version": version,
        "url": f"{public_url.rstrip('/')}/{file_name}",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    data = json.dumps(payload, indent=2).encode()
    sftp.putfo(io.BytesIO(data), f"{releases_path}/latest.json")
    print(f"Updated latest.json → version {version}")


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

    pkey = _load_private_key(ssh_key_content)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, pkey=pkey)

    try:
        with client.open_sftp() as sftp:
            _sftp_mkdir_p(sftp, releases_path)
            for file_path in files:
                file_name = os.path.basename(file_path)
                remote = f"{releases_path}/{file_name}"
                sftp.put(file_path, remote)
                print(f"Uploaded: {file_name} -> {host}:{remote}")
                if public_url:
                    _upload_latest_json(sftp, releases_path, file_name, public_url)

        _cleanup_old_versions(client, releases_path, keep_versions)
    finally:
        client.close()


if __name__ == "__main__":
    main()
