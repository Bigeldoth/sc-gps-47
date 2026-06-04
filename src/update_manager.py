"""Update manager for automatic delta updates.

Orchestrates version checking, delta download, apply, and rollback.
Users can check for updates on-demand from the Options dialog.
"""
import hashlib
import json
import logging
import shutil
import ssl
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from app_paths import user_data_dir
from config_manager import ConfigManager

logger = logging.getLogger(__name__)

RELEASES_BASE_URL = "https://padek-interactive.tech/releases"
LATEST_JSON_URL = f"{RELEASES_BASE_URL}/latest.json"
UPDATES_DIR = user_data_dir() / "updates"
DOWNLOADS_DIR = UPDATES_DIR / "downloads"
STAGING_DIR = UPDATES_DIR / "staging"
METADATA_FILE = STAGING_DIR / "metadata.json"


def _make_ssl_context() -> ssl.SSLContext:
    """Returns an SSLContext that works inside a PyInstaller bundle."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _download_file(url: str, dest: Path, on_progress=None) -> Tuple[bool, str]:
    """Streams a URL to dest with progress callback. Returns (success, message)."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        last_pct = -1
        with urllib.request.urlopen(url, timeout=60, context=_make_ssl_context()) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            written = 0
            with open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    if on_progress is not None and total > 0:
                        pct = int(written * 100 / total)
                        if pct != last_pct and pct % 5 == 0:
                            on_progress(pct, written, total)
                            last_pct = pct
    except Exception as exc:
        return False, f"download failed: {exc}"
    return True, f"downloaded {dest.name}"


def _version_normalize(v: str) -> str:
    """Strip 'v' prefix and normalize version string (e.g., 'v0.8.0' -> '0.8.0')."""
    return v.lstrip("v")


def _version_compare(v1: str, v2: str) -> int:
    """Compare two version strings. Returns -1 (v1 < v2), 0 (equal), +1 (v1 > v2)."""
    try:
        from packaging.version import Version
        ver1 = Version(_version_normalize(v1))
        ver2 = Version(_version_normalize(v2))
        if ver1 < ver2:
            return -1
        elif ver1 > ver2:
            return 1
        return 0
    except Exception as exc:
        logger.warning(f"version compare failed: {exc}; falling back to string compare")
        v1_norm = _version_normalize(v1)
        v2_norm = _version_normalize(v2)
        if v1_norm < v2_norm:
            return -1
        elif v1_norm > v2_norm:
            return 1
        return 0


def _version_gte(v1: str, v2: str) -> bool:
    """Returns True if v1 >= v2."""
    return _version_compare(v1, v2) >= 0


class UpdateMetadata:
    """Tracks update state (in-progress, completed, failed)."""

    def __init__(self, data: dict = None):
        self.data = data or {}

    @property
    def state(self) -> str:
        return self.data.get("state", "unknown")

    @state.setter
    def state(self, value: str):
        self.data["state"] = value

    @property
    def current_version(self) -> str:
        return self.data.get("current_version", "")

    @current_version.setter
    def current_version(self, value: str):
        self.data["current_version"] = value

    @property
    def target_version(self) -> str:
        return self.data.get("target_version", "")

    @target_version.setter
    def target_version(self, value: str):
        self.data["target_version"] = value

    @property
    def backup_path(self) -> str:
        return self.data.get("backup_path", "")

    @backup_path.setter
    def backup_path(self, value: str):
        self.data["backup_path"] = value

    @property
    def errors(self) -> list:
        return self.data.get("errors", [])

    def add_error(self, error: str):
        if "errors" not in self.data:
            self.data["errors"] = []
        self.data["errors"].append(error)

    def to_dict(self) -> dict:
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        return self.data

    @classmethod
    def from_file(cls, path: Path) -> Optional["UpdateMetadata"]:
        """Load metadata from JSON file, or None if not found."""
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return cls(data)
        except Exception as exc:
            logger.warning(f"failed to load metadata: {exc}")
            return None

    def save(self, path: Path):
        """Persist metadata to JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)


class UpdateManager:
    """Orchestrates version checking, delta download, apply, and rollback."""

    def __init__(self, config_manager: ConfigManager, current_version: str):
        self.config = config_manager
        self.current_version = current_version
        self.updates_dir = UPDATES_DIR
        self.downloads_dir = DOWNLOADS_DIR
        self.staging_dir = STAGING_DIR
        self.metadata_file = METADATA_FILE

    def check_for_update(self) -> Tuple[Optional[str], Optional[dict]]:
        """Fetch latest.json and compare with current version.

        Returns (new_version, latest_json) if update available, else (None, None).
        """
        try:
            with urllib.request.urlopen(
                LATEST_JSON_URL, timeout=10, context=_make_ssl_context()
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.error(f"failed to fetch latest.json: {exc}")
            return None, None

        new_version = data.get("version")
        if not new_version:
            logger.warning("latest.json has no 'version' field")
            return None, None

        if _version_compare(new_version, self.current_version) > 0:
            logger.info(f"update available: {self.current_version} -> {new_version}")
            return new_version, data
        else:
            logger.info(f"already up-to-date: {self.current_version}")
            return None, None

    def can_apply_delta(self, latest_json: dict) -> bool:
        """Check if delta is available and compatible with current version."""
        delta_info = latest_json.get("delta", {})
        if not delta_info.get("available"):
            return False
        min_version = delta_info.get("min_version")
        if not min_version:
            return False
        return _version_gte(self.current_version, min_version)

    def download_delta(
        self, url: str, target_version: str, on_progress=None
    ) -> Optional[Path]:
        """Download delta .zip to updates/downloads/. Returns path, or None on failure."""
        filename = f"SpaceDrive-delta-{self.current_version}-to-{target_version}.zip"
        dest = self.downloads_dir / filename
        success, msg = _download_file(url, dest, on_progress=on_progress)
        if not success:
            logger.error(f"delta download failed: {msg}")
            return None
        logger.info(f"delta downloaded: {dest}")
        return dest

    def verify_delta(self, zip_path: Path, expected_checksum: str) -> bool:
        """Verify zip integrity via SHA256."""
        try:
            sha256_hash = hashlib.sha256()
            with open(zip_path, "rb") as f:
                while chunk := f.read(8192):
                    sha256_hash.update(chunk)
            computed = sha256_hash.hexdigest()
            if computed != expected_checksum:
                logger.error(
                    f"checksum mismatch: expected {expected_checksum}, got {computed}"
                )
                return False
            logger.info(f"delta verified: {zip_path.name}")
            return True
        except Exception as exc:
            logger.error(f"delta verification failed: {exc}")
            return False

    def apply_delta(
        self, target_version: str, zip_path: Path
    ) -> Tuple[bool, str]:
        """Extract delta, back up current files, apply changes.

        Returns (success, message). On failure, automatically rollback.
        """
        import zipfile

        # Initialize metadata
        metadata = UpdateMetadata()
        metadata.state = "in_progress"
        metadata.current_version = self.current_version
        metadata.target_version = target_version
        backup_dir_name = f"backup_v{self.current_version}"
        metadata.backup_path = backup_dir_name
        self.metadata_file.parent.mkdir(parents=True, exist_ok=True)
        metadata.save(self.metadata_file)

        try:
            # Phase 1: Extract delta to staging
            extract_dir = self.staging_dir / "extract"
            if extract_dir.exists():
                shutil.rmtree(extract_dir)
            extract_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)
            logger.info(f"delta extracted to {extract_dir}")

            # Phase 2: Find FILES directory
            files_dir = extract_dir / "FILES"
            if not files_dir.exists():
                raise ValueError(f"delta zip missing FILES/ directory")

            # Phase 3: Verify checksums
            checksum_file = extract_dir / "checksum.sha256"
            if checksum_file.exists():
                with open(checksum_file, "r") as f:
                    checksums = {}
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        parts = line.split(None, 1)
                        if len(parts) == 2:
                            checksum, path = parts
                            checksums[path] = checksum

                for file_path in files_dir.rglob("*"):
                    if not file_path.is_file():
                        continue
                    rel_path = str(file_path.relative_to(files_dir))
                    if rel_path in checksums:
                        sha256_hash = hashlib.sha256()
                        with open(file_path, "rb") as f:
                            while chunk := f.read(8192):
                                sha256_hash.update(chunk)
                        computed = sha256_hash.hexdigest()
                        if computed != checksums[rel_path]:
                            raise ValueError(f"checksum mismatch for {rel_path}")
                logger.info("all delta files verified")

            # Phase 4: Determine app install directory
            # Typically %LOCALAPPDATA%\SpaceDrive
            app_dir = user_data_dir()
            if not app_dir.exists():
                raise ValueError(f"app directory not found: {app_dir}")

            # Phase 5: Back up files that will be replaced
            backup_dir = self.staging_dir / backup_dir_name
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            backup_dir.mkdir(parents=True, exist_ok=True)

            for file_path in files_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                rel_path = file_path.relative_to(files_dir)
                original = app_dir / rel_path
                if original.exists():
                    backup_file = backup_dir / rel_path
                    backup_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(original, backup_file)
            logger.info(f"files backed up to {backup_dir}")

            # Phase 6: Copy new files into app directory
            for file_path in files_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                rel_path = file_path.relative_to(files_dir)
                dest = app_dir / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file_path, dest)
            logger.info("delta files applied")

            # Phase 7: Handle deleted files
            deleted_file = extract_dir / "DELETED.txt"
            if deleted_file.exists():
                with open(deleted_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            to_delete = app_dir / line
                            if to_delete.exists():
                                to_delete.unlink()
                                logger.info(f"deleted: {line}")

            # Phase 8: Mark complete
            metadata.state = "completed"
            metadata.save(self.metadata_file)

            # Phase 9: Clean up extraction
            shutil.rmtree(extract_dir)
            logger.info("delta apply succeeded")
            return True, f"Update to {target_version} applied successfully"

        except Exception as exc:
            logger.error(f"delta apply failed: {exc}")
            metadata.add_error(str(exc))
            metadata.state = "failed"
            metadata.save(self.metadata_file)
            # Attempt rollback
            rollback_ok, rollback_msg = self.rollback(self.current_version)
            if rollback_ok:
                return False, f"Delta apply failed ({exc}). Rolled back: {rollback_msg}"
            else:
                return False, f"Delta apply failed ({exc}). Rollback also failed: {rollback_msg}"

    def rollback(self, current_version: str) -> Tuple[bool, str]:
        """Restore from backup if apply failed."""
        backup_dir_name = f"backup_v{current_version}"
        backup_dir = self.staging_dir / backup_dir_name
        if not backup_dir.exists():
            return False, "no backup found"

        try:
            app_dir = user_data_dir()
            for backup_file in backup_dir.rglob("*"):
                if not backup_file.is_file():
                    continue
                rel_path = backup_file.relative_to(backup_dir)
                original = app_dir / rel_path
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_file, original)
            logger.info(f"rolled back to {current_version}")
            # Mark metadata as rolled back
            metadata = UpdateMetadata()
            metadata.state = "rolled_back"
            metadata.current_version = current_version
            metadata.save(self.metadata_file)
            return True, f"Rolled back to {current_version}"
        except Exception as exc:
            logger.error(f"rollback failed: {exc}")
            return False, str(exc)

    def cleanup_old_deltas(self, keep_count: int = 3) -> None:
        """Prune old delta downloads and staging dirs, keeping the most recent."""
        if not self.downloads_dir.exists():
            return
        try:
            deltas = sorted(
                [f for f in self.downloads_dir.iterdir() if f.suffix == ".zip"],
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            )
            for delta in deltas[keep_count:]:
                delta.unlink()
                logger.info(f"cleaned up old delta: {delta.name}")

            # Clean staging directory
            if self.staging_dir.exists():
                staging_dirs = [
                    d for d in self.staging_dir.iterdir()
                    if d.is_dir() and d.name.startswith("backup_")
                ]
                for staging_dir in staging_dirs[keep_count:]:
                    shutil.rmtree(staging_dir)
                    logger.info(f"cleaned up old staging: {staging_dir.name}")
        except Exception as exc:
            logger.warning(f"cleanup failed: {exc}")

    def detect_incomplete_update(self) -> Optional[UpdateMetadata]:
        """Check if there's an incomplete update (in_progress state)."""
        metadata = UpdateMetadata.from_file(self.metadata_file)
        if metadata and metadata.state == "in_progress":
            return metadata
        return None
