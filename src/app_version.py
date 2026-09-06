"""Read the immutable application version shipped with the bundle."""

import re

from app_paths import bundle_dir


def get_app_version() -> str:
    """Return vX.Y.Z or unknown; user configuration never controls this value."""
    try:
        version = (bundle_dir() / 'VERSION').read_text(encoding='utf-8-sig').strip()
    except OSError:
        return 'unknown'
    return f'v{version}' if re.fullmatch(r'(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)', version) else 'unknown'
