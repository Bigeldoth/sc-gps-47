"""Rename all glyphs to avoid conflicts when copying to templates.

Problem: if you collect multiple sessions, files have same names
  (0000_r0.png, 0001_r0.png, ...) and overlap when copying to
  data/templates/.

Solution: prefix each file with parent folder's timestamp.

Before:
  data/glyphs/20260510_143025_456/0000_r0.png
  data/glyphs/20260510_143025_456/0001_r0.png
  data/glyphs/20260510_150630_789/0000_r0.png  ← conflict!

After:
  data/glyphs/20260510_143025_456/20260510_143025_456_0000_r0.png
  data/glyphs/20260510_143025_456/20260510_143025_456_0001_r0.png
  data/glyphs/20260510_150630_789/20260510_150630_789_0000_r0.png  ✅ unique!
"""
import os
import sys
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def rename_glyphs_in_session(session_dir):
    """Rename all PNGs in a session with timestamp prefix.

    Args:
        session_dir: path to YYYYMMDD_HHMMSS_mmm folder

    Returns:
        (count of renamed files, error list)
    """
    if not os.path.isdir(session_dir):
        logger.error(f"Folder not found: {session_dir}")
        return 0, [session_dir]

    # Extract timestamp from folder name
    session_name = os.path.basename(session_dir)
    if not session_name or len(session_name) < 17:
        logger.error(f"Invalid folder name: {session_name}")
        return 0, [session_name]

    # Validate YYYYMMDD_HHMMSS(_mmm) format
    parts = session_name.split('_')
    if len(parts) < 2:
        logger.warning(f"Unexpected format (ignore): {session_name}")
        return 0, []

    renamed_count = 0
    errors = []

    # Iterate over all PNGs
    for filename in os.listdir(session_dir):
        if not filename.lower().endswith('.png'):
            continue

        # Check if file already has timestamp prefix
        if filename.startswith(session_name):
            logger.debug(f"Already renamed: {filename}")
            continue

        old_path = os.path.join(session_dir, filename)
        new_filename = f"{session_name}_{filename}"
        new_path = os.path.join(session_dir, new_filename)

        try:
            os.rename(old_path, new_path)
            logger.debug(f"[OK] {filename} -> {new_filename}")
            renamed_count += 1
        except OSError as e:
            logger.error(f"[ERR] Rename error {filename}: {e}")
            errors.append(filename)

    return renamed_count, errors


def rename_all_glyphs(base_dir='data/glyphs'):
    """Rename glyphs in ALL session folders.

    Args:
        base_dir: parent directory (data/glyphs)

    Returns:
        dict with stats
    """
    if not os.path.isdir(base_dir):
        logger.error(f"Directory {base_dir} not found")
        return None

    # List all session folders
    session_dirs = [
        os.path.join(base_dir, d)
        for d in os.listdir(base_dir)
        if os.path.isdir(os.path.join(base_dir, d))
    ]

    if not session_dirs:
        logger.warning(f"No session folders in {base_dir}")
        return {'total_sessions': 0, 'total_renamed': 0, 'errors': []}

    logger.info(f"Found {len(session_dirs)} collection session(s)")

    total_renamed = 0
    all_errors = []

    for session_dir in sorted(session_dirs):
        session_name = os.path.basename(session_dir)
        logger.info(f"\n--- Session: {session_name} ---")

        count, errors = rename_glyphs_in_session(session_dir)
        total_renamed += count

        if errors:
            all_errors.extend([(session_name, e) for e in errors])

        logger.info(f"Renamed: {count}")

    return {
        'total_sessions': len(session_dirs),
        'total_renamed': total_renamed,
        'errors': all_errors,
    }


def print_report(stats):
    """Print rename report.

    Args:
        stats: dict returned by rename_all_glyphs()
    """
    print("\n" + "=" * 60)
    print("GLYPH RENAME REPORT")
    print("=" * 60)
    print(f"Number of sessions: {stats['total_sessions']}")
    print(f"Total renamed: {stats['total_renamed']}")

    if stats['errors']:
        print(f"\nErrors: {len(stats['errors'])}")
        for session, filename in stats['errors'][:10]:
            print(f"  [{session}] {filename}")
    else:
        print("\nNo errors [OK]")

    print("=" * 60)
    print("\nReady to copy glyphs to data/templates/!")
    print("Files now have unique names per session.")
    print("=" * 60 + "\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Rename collected glyphs to avoid conflicts"
    )
    parser.add_argument(
        "--glyphs-dir",
        type=str,
        default="data/glyphs",
        help="Parent directory (default: data/glyphs)"
    )
    parser.add_argument(
        "--single-session",
        type=str,
        default=None,
        help="Rename single session (full path). Otherwise: all."
    )

    args = parser.parse_args()

    if args.single_session:
        logger.info(f"Single session rename: {args.single_session}")
        count, errors = rename_glyphs_in_session(args.single_session)
        stats = {
            'total_sessions': 1,
            'total_renamed': count,
            'errors': errors,
        }
    else:
        logger.info(f"Renaming in: {args.glyphs_dir}")
        stats = rename_all_glyphs(args.glyphs_dir)

    if stats is None:
        sys.exit(1)

    print_report(stats)

    if stats['errors']:
        sys.exit(1)


if __name__ == "__main__":
    main()
