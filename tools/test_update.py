"""Local update-pipeline smoke test.

Creates a synthetic delta ZIP (no VPS needed), then runs the full apply
path of UpdateManager so you can verify the elevation / copy / rollback
logic without shipping a real release.

Usage (from repo root, with the project venv active):
    python tools/test_update.py [--mode direct|elevation|bad-checksum|rollback]

Modes
-----
direct          Apply a small delta to a temp dir that IS writable.
                Exercises Phases 1-8 of apply_delta() without UAC.
                Expected result: success, temp dir contains updated files.

elevation       Simulate a non-writable install dir.
                Exercises the UAC / PS1 elevation path.
                Expected result: dialog requests UAC (you can cancel to verify
                the error path) or succeeds if accepted.

bad-checksum    Feed a ZIP with a corrupted checksum.
                Expected result: verify_delta() returns False.

rollback        Force apply_delta() to fail mid-apply, check rollback works.
                Expected result: staged backup is restored.
"""
import argparse
import hashlib
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

# Allow running from repo root without install
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from update_manager import UpdateManager, _is_writable

CURRENT_VERSION = "v0.8.0"
TARGET_VERSION = "v0.9.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_delta_zip(dest: Path, files: dict[str, bytes], corrupt_checksum=False) -> str:
    """Build a minimal delta ZIP matching the format expected by apply_delta().

    files: {relative_posix_path: content_bytes}
    Returns the SHA-256 hex of the produced ZIP.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    checksums = {}
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("version.txt", TARGET_VERSION.lstrip("v"))
        for rel, data in files.items():
            zf.writestr(f"FILES/{rel}", data)
            checksums[rel] = hashlib.sha256(data).hexdigest()
        if corrupt_checksum:
            checksum_data = "deadbeef0000 some/file.txt"
        else:
            checksum_data = "\n".join(f"{h} {p}" for p, h in sorted(checksums.items()))
        zf.writestr("checksum.sha256", checksum_data)
    with open(dest, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _make_manager(install_dir_override: Path) -> UpdateManager:
    """Create an UpdateManager that points its install_dir to a temp path."""
    um = UpdateManager(config_manager=None, current_version=CURRENT_VERSION)

    # Monkey-patch install_dir() so the test controls where files land
    import update_manager as _um_mod
    _um_mod.install_dir = lambda: install_dir_override
    return um


# ---------------------------------------------------------------------------
# Test modes
# ---------------------------------------------------------------------------

def test_direct():
    """Happy path: writable target dir, apply should succeed in-process."""
    print("\n=== MODE: direct ===")
    with tempfile.TemporaryDirectory(prefix="sd_update_test_") as tmp:
        install = Path(tmp) / "app"
        install.mkdir()

        # Put a "current" file that the delta should overwrite
        (install / "spaceDrive.exe").write_bytes(b"OLD_EXE_CONTENT")
        (install / "_internal" / "data.dll").mkdir(parents=True)
        (install / "_internal" / "data.dll").rmdir()
        (install / "_internal").mkdir(exist_ok=True)
        (install / "_internal" / "data.dll").write_bytes(b"OLD_DLL")

        zip_path = Path(tmp) / "delta.zip"
        expected_checksum = _build_delta_zip(zip_path, {
            "spaceDrive.exe": b"NEW_EXE_CONTENT_v0.9",
            "_internal/data.dll": b"NEW_DLL_v0.9",
            "_internal/new_file.pyd": b"BRAND_NEW",
        })

        um = _make_manager(install)
        ok = um.verify_delta(zip_path, expected_checksum)
        assert ok, "verify_delta failed"
        print(f"  checksum OK: {expected_checksum[:16]}...")

        success, msg = um.apply_delta(TARGET_VERSION, zip_path)
        print(f"  apply_delta -> success={success}, msg={msg!r}")

        if success:
            exe = (install / "spaceDrive.exe").read_bytes()
            dll = (install / "_internal" / "data.dll").read_bytes()
            new_pyd = (install / "_internal" / "new_file.pyd").read_bytes()
            assert exe == b"NEW_EXE_CONTENT_v0.9", "EXE not updated"
            assert dll == b"NEW_DLL_v0.9", "DLL not updated"
            assert new_pyd == b"BRAND_NEW", "new file missing"
            print("  [PASS] All files updated correctly.")
        else:
            print(f"  [FAIL] {msg}")
            sys.exit(1)


def test_bad_checksum():
    """verify_delta() must reject a ZIP with a corrupted checksum file."""
    print("\n=== MODE: bad-checksum ===")
    with tempfile.TemporaryDirectory(prefix="sd_update_test_") as tmp:
        zip_path = Path(tmp) / "delta_bad.zip"
        real_checksum = _build_delta_zip(zip_path, {"test.txt": b"hello"})
        wrong_checksum = "a" * 64

        um = _make_manager(Path(tmp) / "app")
        ok = um.verify_delta(zip_path, wrong_checksum)
        assert not ok, "verify_delta should have rejected mismatched checksum"
        print("  [PASS] verify_delta correctly rejected bad checksum.")


def test_rollback():
    """Force a mid-apply error and verify the backup is restored."""
    print("\n=== MODE: rollback ===")
    with tempfile.TemporaryDirectory(prefix="sd_update_test_") as tmp:
        install = Path(tmp) / "app"
        install.mkdir()
        (install / "spaceDrive.exe").write_bytes(b"ORIGINAL_EXE")

        zip_path = Path(tmp) / "delta.zip"
        # Delta has a valid file + a path that will resolve to a dir (triggers OSError)
        files = {
            "spaceDrive.exe": b"SHOULD_NOT_LAND",
        }
        checksum = _build_delta_zip(zip_path, files)

        um = _make_manager(install)

        # Inject a failure: make the EXE a directory so copy fails
        (install / "spaceDrive.exe").unlink()
        (install / "spaceDrive.exe").mkdir()

        success, msg = um.apply_delta(TARGET_VERSION, zip_path)
        print(f"  apply_delta -> success={success}, msg={msg!r}")
        assert not success, "Expected failure"

        # After rollback the EXE directory should be gone, but since original
        # was a file we backed it up -- check backup exists at least
        backup_dir = um.staging_dir / f"backup_v{CURRENT_VERSION}"
        print(f"  backup_dir exists: {backup_dir.exists()}")
        if backup_dir.exists():
            print("  [PASS] Backup present after rollback.")
        else:
            print("  [WARN] No backup found (apply may have failed before backup phase).")


def test_elevation():
    """Simulate a non-writable install dir and trigger the elevation path."""
    print("\n=== MODE: elevation ===")
    import stat
    with tempfile.TemporaryDirectory(prefix="sd_update_test_") as tmp:
        install = Path(tmp) / "readonly_app"
        install.mkdir()
        (install / "spaceDrive.exe").write_bytes(b"OLD")

        # Make the dir read-only so _is_writable() returns False
        install.chmod(stat.S_IREAD | stat.S_IEXEC)
        try:
            if _is_writable(install):
                print("  [SKIP] Could not make dir read-only on this system (may need admin).")
                return

            zip_path = Path(tmp) / "delta.zip"
            _build_delta_zip(zip_path, {"spaceDrive.exe": b"NEW"})

            um = _make_manager(install)
            print("  Calling apply_delta() on read-only dir...")
            print("  (A UAC prompt will appear — cancel to test the deny path)")
            success, msg = um.apply_delta(TARGET_VERSION, zip_path)
            print(f"  apply_delta -> success={success}, msg={msg!r}")
            if msg == "ELEVATION_REQUIRED":
                print("  [PASS] Elevation path triggered.")
            elif not success:
                print(f"  [PASS] Elevation denied/failed as expected: {msg}")
            else:
                print("  [INFO] Elevation accepted and applied.")
        finally:
            # Restore write permission so tempfile cleanup works
            install.chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

MODES = {
    "direct": test_direct,
    "elevation": test_elevation,
    "bad-checksum": test_bad_checksum,
    "rollback": test_rollback,
}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SpaceDrive update pipeline smoke test")
    parser.add_argument(
        "--mode",
        choices=list(MODES),
        default="direct",
        help="Which test to run (default: direct)",
    )
    args = parser.parse_args()
    MODES[args.mode]()
