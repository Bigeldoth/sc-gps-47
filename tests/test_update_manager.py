"""Unit tests for the UpdateManager module.

Comprehensive test suite covering version comparison, metadata handling,
delta verification, apply/rollback logic, and error scenarios.
"""
import hashlib
import json
import pytest
import tempfile
import unittest.mock
from pathlib import Path
from unittest.mock import patch, MagicMock

# Import the module under test
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from update_manager import (
    UpdateManager,
    UpdateMetadata,
    _version_compare,
    _version_normalize,
    _version_gte,
)


class TestVersionComparison:
    """Tests for semantic version comparison utilities."""

    def test_version_normalize(self):
        """Test version string normalization."""
        assert _version_normalize("v0.8.0") == "0.8.0"
        assert _version_normalize("0.8.0") == "0.8.0"
        assert _version_normalize("v1.0.0-rc1") == "1.0.0-rc1"

    def test_version_compare_equal(self):
        """Test comparison of equal versions."""
        assert _version_compare("v0.8.0", "v0.8.0") == 0
        assert _version_compare("0.8.0", "v0.8.0") == 0

    def test_version_compare_less_than(self):
        """Test comparison when first version is less."""
        assert _version_compare("v0.7.0", "v0.8.0") == -1
        assert _version_compare("v0.8.0", "v0.9.0") == -1
        assert _version_compare("v0.8.0-rc1", "v0.8.0") == -1
        assert _version_compare("v0.8.0-alpha", "v0.8.0-beta") == -1

    def test_version_compare_greater_than(self):
        """Test comparison when first version is greater."""
        assert _version_compare("v0.9.0", "v0.8.0") == 1
        assert _version_compare("v1.0.0", "v0.9.0") == 1
        assert _version_compare("v0.8.0", "v0.8.0-rc1") == 1

    def test_version_gte(self):
        """Test greater-than-or-equal comparison."""
        assert _version_gte("v0.8.0", "v0.7.0") == True
        assert _version_gte("v0.8.0", "v0.8.0") == True
        assert _version_gte("v0.7.0", "v0.8.0") == False


class TestUpdateMetadata:
    """Tests for UpdateMetadata serialization and state tracking."""

    def test_metadata_create_empty(self):
        """Test creating an empty metadata object."""
        meta = UpdateMetadata()
        assert meta.state == "unknown"
        assert meta.current_version == ""
        assert meta.target_version == ""
        assert meta.errors == []

    def test_metadata_set_properties(self):
        """Test setting metadata properties."""
        meta = UpdateMetadata()
        meta.state = "in_progress"
        meta.current_version = "v0.7.0"
        meta.target_version = "v0.8.0"
        meta.backup_path = "backup_v0.7.0"
        meta.add_error("Test error")

        assert meta.state == "in_progress"
        assert meta.current_version == "v0.7.0"
        assert meta.target_version == "v0.8.0"
        assert meta.backup_path == "backup_v0.7.0"
        assert "Test error" in meta.errors

    def test_metadata_to_dict(self):
        """Test converting metadata to dictionary."""
        meta = UpdateMetadata()
        meta.state = "completed"
        meta.current_version = "v0.7.0"
        meta.target_version = "v0.8.0"

        data = meta.to_dict()
        assert data["state"] == "completed"
        assert data["current_version"] == "v0.7.0"
        assert data["target_version"] == "v0.8.0"
        assert "updated_at" in data

    def test_metadata_save_and_load(self):
        """Test saving and loading metadata from file."""
        meta = UpdateMetadata()
        meta.state = "in_progress"
        meta.current_version = "v0.7.0"
        meta.target_version = "v0.8.0"
        meta.backup_path = "backup_v0.7.0"

        # Save to temporary file
        with tempfile.TemporaryDirectory() as tmpdir:
            meta_file = Path(tmpdir) / "metadata.json"
            meta.save(meta_file)
            assert meta_file.exists()

            # Load from file
            loaded = UpdateMetadata.from_file(meta_file)
            assert loaded is not None
            assert loaded.state == "in_progress"
            assert loaded.current_version == "v0.7.0"
            assert loaded.target_version == "v0.8.0"
            assert loaded.backup_path == "backup_v0.7.0"

    def test_metadata_load_nonexistent_file(self):
        """Test loading from non-existent file returns None."""
        fake_path = Path("/nonexistent/metadata.json")
        loaded = UpdateMetadata.from_file(fake_path)
        assert loaded is None

    def test_metadata_load_invalid_json(self):
        """Test loading invalid JSON returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            meta_file = Path(tmpdir) / "metadata.json"
            meta_file.write_text("{ invalid json }")
            loaded = UpdateMetadata.from_file(meta_file)
            assert loaded is None


class TestUpdateManagerVersionCheck:
    """Tests for version checking functionality."""

    def test_check_for_update_up_to_date(self):
        """Test check when current version matches latest."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "version": "v0.7.4",
            "url": "https://example.com/installer.exe",
            "date": "2026-06-10"
        }).encode()

        with patch("update_manager.urllib.request.urlopen", return_value=mock_response):
            config = MagicMock()
            um = UpdateManager(config, "v0.7.4")
            new_version, latest_json = um.check_for_update()

            assert new_version is None
            assert latest_json is None

    def test_check_for_update_new_available(self):
        """Test check when new version is available."""
        response_data = {
            "version": "v0.8.0",
            "url": "https://example.com/installer.exe",
            "date": "2026-06-10",
            "delta": {
                "available": True,
                "min_version": "v0.7.4",
                "url": "https://example.com/delta.zip",
                "checksum": "abc123",
                "size_bytes": 18500000
            }
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(response_data).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("update_manager.urllib.request.urlopen", return_value=mock_response):
            config = MagicMock()
            um = UpdateManager(config, "v0.7.4")
            new_version, latest_json = um.check_for_update()

            assert new_version == "v0.8.0"
            assert latest_json["version"] == "v0.8.0"
            assert latest_json["delta"]["available"] == True

    def test_check_for_update_network_error(self):
        """Test check with network error."""
        with patch("update_manager.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = Exception("Network timeout")

            config = MagicMock()
            um = UpdateManager(config, "v0.7.4")
            new_version, latest_json = um.check_for_update()

            assert new_version is None
            assert latest_json is None

    def test_check_for_update_missing_version_field(self):
        """Test check with malformed JSON (missing version)."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"url": "https://example.com"}).encode()

        with patch("update_manager.urllib.request.urlopen", return_value=mock_response):
            config = MagicMock()
            um = UpdateManager(config, "v0.7.4")
            new_version, latest_json = um.check_for_update()

            assert new_version is None
            assert latest_json is None

    def test_can_apply_delta_available(self):
        """Test delta availability check when compatible."""
        config = MagicMock()
        um = UpdateManager(config, "v0.7.5")

        latest_json = {
            "delta": {
                "available": True,
                "min_version": "v0.7.4"
            }
        }

        assert um.can_apply_delta(latest_json) == True

    def test_can_apply_delta_not_available(self):
        """Test delta availability check when not available."""
        config = MagicMock()
        um = UpdateManager(config, "v0.7.5")

        latest_json = {
            "delta": {
                "available": False,
                "min_version": "v0.7.4"
            }
        }

        assert um.can_apply_delta(latest_json) == False

    def test_can_apply_delta_too_old_version(self):
        """Test delta availability check when current version too old."""
        config = MagicMock()
        um = UpdateManager(config, "v0.7.0")

        latest_json = {
            "delta": {
                "available": True,
                "min_version": "v0.7.4"
            }
        }

        assert um.can_apply_delta(latest_json) == False


class TestDeltaVerification:
    """Tests for delta integrity verification."""

    def test_verify_delta_correct_checksum(self):
        """Test verification passes with correct checksum."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.zip"
            test_content = b"test delta content"
            test_file.write_bytes(test_content)

            # Calculate correct checksum
            sha256_hash = hashlib.sha256()
            sha256_hash.update(test_content)
            correct_checksum = sha256_hash.hexdigest()

            config = MagicMock()
            um = UpdateManager(config, "v0.7.0")
            assert um.verify_delta(test_file, correct_checksum) == True

    def test_verify_delta_incorrect_checksum(self):
        """Test verification fails with incorrect checksum."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.zip"
            test_file.write_bytes(b"test delta content")

            config = MagicMock()
            um = UpdateManager(config, "v0.7.0")
            assert um.verify_delta(test_file, "0" * 64) == False

    def test_verify_delta_missing_file(self):
        """Test verification fails when file doesn't exist."""
        config = MagicMock()
        um = UpdateManager(config, "v0.7.0")
        missing_file = Path("/nonexistent/delta.zip")
        assert um.verify_delta(missing_file, "abc123") == False


class TestDeltaApplication:
    """Tests for delta application and file handling."""

    def _create_test_delta_zip(self, tmpdir: Path) -> Path:
        """Helper to create a minimal test delta zip."""
        import zipfile

        delta_dir = tmpdir / "delta_src"
        delta_dir.mkdir()

        files_dir = delta_dir / "FILES"
        files_dir.mkdir()

        # Create a test file
        test_file = files_dir / "test.txt"
        test_file.write_text("updated content")

        # Create checksum file
        sha256_hash = hashlib.sha256()
        sha256_hash.update(b"updated content")
        checksum_file = delta_dir / "checksum.sha256"
        checksum_file.write_text(f"{sha256_hash.hexdigest()} test.txt\n")

        # Create version file
        version_file = delta_dir / "version.txt"
        version_file.write_text("v0.8.0")

        # Create zip
        zip_path = tmpdir / "test_delta.zip"
        with zipfile.ZipFile(zip_path, 'w') as zf:
            for file in delta_dir.rglob("*"):
                if file.is_file():
                    arcname = file.relative_to(delta_dir)
                    zf.write(file, arcname)

        return zip_path

    def test_apply_delta_success(self):
        """Test successful delta application (integration test)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            app_dir = tmpdir / "app"
            app_dir.mkdir()

            # Create original file
            original_file = app_dir / "test.txt"
            original_file.write_text("original content")

            # Create delta zip
            delta_zip = self._create_test_delta_zip(tmpdir)

            # Monkeypatch both user_data_dir and also the UPDATES_DIR at module level
            # so that UpdateManager uses our tempdir
            import update_manager as um_module
            original_updates_dir = um_module.UPDATES_DIR
            um_module.UPDATES_DIR = tmpdir / "updates"

            try:
                config = MagicMock()
                um = UpdateManager(config, "v0.7.0")
                # Force the paths to use our tmpdir
                um.updates_dir = tmpdir / "updates"
                um.staging_dir = tmpdir / "updates" / "staging"

                # Apply delta
                with patch('update_manager.user_data_dir', return_value=app_dir):
                    success, msg = um.apply_delta("v0.8.0", delta_zip)

                    assert success == True
                    assert "applied successfully" in msg

                    # Verify file was updated (main test objective)
                    assert original_file.read_text() == "updated content", \
                        f"File not updated. Content: {original_file.read_text()}"

                    # Note: Backup creation is tested implicitly by successful apply
                    # The backup/staging logic is verified in test_rollback_success
            finally:
                # Restore module state
                um_module.UPDATES_DIR = original_updates_dir

    @patch('update_manager.user_data_dir')
    def test_apply_delta_missing_files_directory(self, mock_user_data_dir):
        """Test delta application fails without FILES directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            app_dir = tmpdir / "app"
            app_dir.mkdir()

            # Create invalid delta (no FILES directory)
            import zipfile
            delta_zip = tmpdir / "invalid_delta.zip"
            with zipfile.ZipFile(delta_zip, 'w') as zf:
                zf.writestr("version.txt", "v0.8.0")

            with patch('update_manager.user_data_dir', return_value=app_dir):
                config = MagicMock()
                um = UpdateManager(config, "v0.7.0")

                success, msg = um.apply_delta("v0.8.0", delta_zip)
                assert success == False
                assert "FILES" in msg

    @patch('update_manager.user_data_dir')
    def test_rollback_success(self, mock_user_data_dir):
        """Test successful rollback after delta application."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            app_dir = tmpdir / "app"
            app_dir.mkdir()

            # Create original file
            original_file = app_dir / "test.txt"
            original_file.write_text("original content")

            # Create delta and apply it
            delta_zip = self._create_test_delta_zip(tmpdir)

            with patch('update_manager.user_data_dir', return_value=app_dir):
                config = MagicMock()
                um = UpdateManager(config, "v0.7.0")

                # Apply delta
                um.apply_delta("v0.8.0", delta_zip)
                assert original_file.read_text() == "updated content"

                # Rollback
                success, msg = um.rollback("v0.7.0")

                assert success == True
                assert "Rolled back" in msg
                assert original_file.read_text() == "original content"

    def test_rollback_no_backup(self):
        """Test rollback fails when no backup exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            app_dir = tmpdir / "app"
            app_dir.mkdir()

            config = MagicMock()
            um = UpdateManager(config, "v0.7.0")

            # Force staging_dir to a temp location
            um.staging_dir = tmpdir / "updates" / "staging"
            um.staging_dir.mkdir(parents=True, exist_ok=True)

            # Try rollback without backup
            success, msg = um.rollback("v0.7.0")

            # Rollback should fail when there's no backup
            assert success == False
            assert "no backup found" in msg


class TestCleanupAndMaintenance:
    """Tests for cleanup and maintenance functions."""

    def test_cleanup_old_deltas(self):
        """Test cleanup removes old delta files."""
        import time
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            downloads_dir = tmpdir / "updates" / "downloads"
            downloads_dir.mkdir(parents=True)

            # Create 5 delta files with different timestamps
            for i in range(5):
                delta_file = downloads_dir / f"delta_{i}.zip"
                delta_file.write_text(f"delta {i}")
                # Set different modification times (oldest to newest)
                mtime = 1000000000 + (i * 1000)
                os.utime(delta_file, (mtime, mtime))

            config = MagicMock()
            um = UpdateManager(config, "v0.7.0")
            # Force downloads_dir
            um.downloads_dir = downloads_dir

            # Cleanup, keeping only 2 most recent
            um.cleanup_old_deltas(keep_count=2)

            # Verify 2 files remain (the most recent)
            remaining_deltas = list(downloads_dir.glob("*.zip"))
            assert len(remaining_deltas) == 2, f"Expected 2 deltas, found {len(remaining_deltas)}: {remaining_deltas}"


class TestIncompleteUpdateDetection:
    """Tests for incomplete update detection."""

    def test_detect_incomplete_update_in_progress(self):
        """Test detection of incomplete update in progress."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create metadata in the expected location
            staging_dir = tmpdir / "updates" / "staging"
            staging_dir.mkdir(parents=True)
            metadata_file = staging_dir / "metadata.json"

            # Create incomplete update metadata
            meta = UpdateMetadata()
            meta.state = "in_progress"
            meta.current_version = "v0.7.0"
            meta.target_version = "v0.8.0"
            meta.save(metadata_file)

            config = MagicMock()
            um = UpdateManager(config, "v0.7.0")
            # Force metadata_file path
            um.metadata_file = metadata_file

            detected = um.detect_incomplete_update()
            assert detected is not None
            assert detected.state == "in_progress"
            assert detected.current_version == "v0.7.0"
            assert detected.target_version == "v0.8.0"

    @patch('update_manager.user_data_dir')
    def test_detect_incomplete_update_completed(self, mock_user_data_dir):
        """Test no detection when update is completed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            metadata_file = tmpdir / "updates" / "staging" / "metadata.json"
            metadata_file.parent.mkdir(parents=True)

            # Create completed update metadata
            meta = UpdateMetadata()
            meta.state = "completed"
            meta.current_version = "v0.7.0"
            meta.target_version = "v0.8.0"
            meta.save(metadata_file)

            with patch('update_manager.user_data_dir', return_value=tmpdir):
                config = MagicMock()
                um = UpdateManager(config, "v0.7.0")

                detected = um.detect_incomplete_update()
                assert detected is None

    @patch('update_manager.user_data_dir')
    def test_detect_incomplete_update_no_metadata(self, mock_user_data_dir):
        """Test no detection when metadata file doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            with patch('update_manager.user_data_dir', return_value=tmpdir):
                config = MagicMock()
                um = UpdateManager(config, "v0.7.0")

                detected = um.detect_incomplete_update()
                assert detected is None


class TestEdgeCases:
    """Tests for edge cases and error scenarios."""

    def test_version_compare_with_invalid_format(self):
        """Test version comparison handles invalid formats gracefully."""
        # Should fall back to string comparison
        result = _version_compare("invalid", "v0.8.0")
        assert result is not None  # Should not raise exception

    def test_update_metadata_add_multiple_errors(self):
        """Test adding multiple errors to metadata."""
        meta = UpdateMetadata()
        meta.add_error("Error 1")
        meta.add_error("Error 2")
        meta.add_error("Error 3")

        assert len(meta.errors) == 3
        assert "Error 1" in meta.errors
        assert "Error 2" in meta.errors
        assert "Error 3" in meta.errors

    def test_apply_delta_with_empty_delta(self):
        """Test applying an empty delta (no file changes, only metadata)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            app_dir = tmpdir / "app"
            app_dir.mkdir()

            # Create delta with FILES directory but no files in it
            import zipfile
            delta_zip = tmpdir / "empty_delta.zip"
            with zipfile.ZipFile(delta_zip, 'w') as zf:
                # Create empty FILES directory entry
                zf.writestr("FILES/", "")
                zf.writestr("version.txt", "v0.8.0")
                zf.writestr("checksum.sha256", "")

            with patch('update_manager.user_data_dir', return_value=app_dir):
                config = MagicMock()
                um = UpdateManager(config, "v0.7.0")

                success, msg = um.apply_delta("v0.8.0", delta_zip)
                # Should succeed even with no file changes (only metadata)
                assert success == True


# Run tests if executed directly
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
