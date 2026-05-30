"""
POI category taxonomy shared with the SpaceDrive Community web hub.

Slugs are ASCII lowercase (stable wire format for export / API); labels are the
English strings rendered in the desktop UI. The empty slug "" is the default for
legacy POIs and is displayed as "Uncategorized".
"""
from __future__ import annotations

# Order matters: combo box and table use this list directly.
POI_CATEGORIES: list[tuple[str, str]] = [
    ("",                       "Uncategorized"),
    ("industry",               "Industry"),
    ("exploration",            "Exploration"),
    ("logistics_black_market", "Logistics & Black Market"),
    ("hostile_combat",         "Combat & Hostile Zones"),
    ("loot",                   "Loot"),
    ("racing",                 "Racing"),
]

CATEGORY_SLUGS: frozenset[str] = frozenset(slug for slug, _ in POI_CATEGORIES)

_SLUG_TO_LABEL: dict[str, str] = dict(POI_CATEGORIES)

# Colors shared with the SpaceDrive Community Hub (pois.js SPACEDRIVE_TYPES).
# Slugs that don't map 1-to-1 use the closest semantic match.
CATEGORY_COLORS: dict[str, str] = {
    "":                       "#7E8B97",  # muted grey   — uncategorized
    "industry":               "#F97316",  # orange       — industry/mining
    "exploration":            "#6FE8FF",  # neon         — cave/hidden
    "logistics_black_market": "#A78BFA",  # violet       — logistics
    "hostile_combat":         "#E5484D",  # danger red   — tactical/hostile
    "loot":                   "#FBBF24",  # amber        — loot
    "racing":                 "#86EFAC",  # light green  — racing
}


def label_for(slug: str | None) -> str:
    """Return the UI label for a slug, or "Uncategorized" if unknown/None."""
    if slug is None:
        return _SLUG_TO_LABEL[""]
    return _SLUG_TO_LABEL.get(slug, _SLUG_TO_LABEL[""])


def color_for(slug: str | None) -> str:
    """Return the PADEK hex color for a category slug."""
    if slug is None:
        return CATEGORY_COLORS[""]
    return CATEGORY_COLORS.get(slug, CATEGORY_COLORS[""])


def is_valid_slug(slug: str) -> bool:
    return slug in CATEGORY_SLUGS
