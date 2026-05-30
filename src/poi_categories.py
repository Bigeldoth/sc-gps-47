"""
POI category taxonomy shared with the SpaceDrive Community web hub.

Slugs are ASCII lowercase (stable wire format for export / API).
The empty slug "" is the default for uncategorized POIs.

Category set mirrors SPACEDRIVE_TYPES in the Community Hub's pois.js.
"""
from __future__ import annotations

# Canonical categories (order = UI display order in combos and chip bar).
POI_CATEGORIES: list[tuple[str, str]] = [
    ("",         "Uncategorized"),
    ("hidden",   "Hidden"),
    ("cave",     "Cave"),
    ("circuit",  "Circuit"),
    ("tactical", "Tactical"),
    ("industry", "Industry"),
    ("logistics","Logistics"),
    ("loot",     "Loot"),
    ("racing",   "Racing"),
]

# Colors from Community Hub pois.js SPACEDRIVE_TYPES.
CATEGORY_COLORS: dict[str, str] = {
    "":          "#7E8B97",  # muted grey   — uncategorized
    "hidden":    "#19C28A",  # emerald      — hidden/secret
    "cave":      "#6FE8FF",  # neon         — cave/underground
    "circuit":   "#D9A368",  # copper       — circuit/route
    "tactical":  "#E5484D",  # danger red   — tactical/hostile
    "industry":  "#F97316",  # orange       — industry/mining
    "logistics": "#A78BFA",  # violet       — logistics/trade
    "loot":      "#FBBF24",  # amber        — loot/containers
    "racing":    "#86EFAC",  # light green  — racing/circuits
}

# Legacy slugs from v1 (before Community Hub alignment) mapped to their
# closest v2 equivalent. Kept in CATEGORY_SLUGS for import validation
# backward-compat so old exported POIs still parse without error.
_LEGACY: dict[str, str] = {
    "exploration":            "cave",
    "logistics_black_market": "logistics",
    "hostile_combat":         "tactical",
}

# Full set of accepted slugs (canonical + legacy).
CATEGORY_SLUGS: frozenset[str] = frozenset(
    slug for slug, _ in POI_CATEGORIES
) | frozenset(_LEGACY)

_SLUG_TO_LABEL: dict[str, str] = dict(POI_CATEGORIES)


def label_for(slug: str | None) -> str:
    """Return the UI label for a slug, applying legacy migration if needed."""
    if not slug:
        return _SLUG_TO_LABEL[""]
    canonical = _LEGACY.get(slug, slug)
    return _SLUG_TO_LABEL.get(canonical, _SLUG_TO_LABEL[""])


def color_for(slug: str | None) -> str:
    """Return the PADEK Community Hub color for a category slug."""
    if not slug:
        return CATEGORY_COLORS[""]
    canonical = _LEGACY.get(slug, slug)
    return CATEGORY_COLORS.get(canonical, CATEGORY_COLORS[""])


def is_valid_slug(slug: str) -> bool:
    return slug in CATEGORY_SLUGS
