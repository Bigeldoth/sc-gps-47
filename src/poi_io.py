"""
Serialization and validation for POI exchange with the SpaceDrive Community hub.

Pure Python, no Qt dependency, so it can be unit-tested and reused on the server
side. Schema: name, x, y, z, location, ooc, kind, category, description.
"""
from __future__ import annotations

import json
from typing import Any

from poi_categories import CATEGORY_SLUGS

REQUIRED_FIELDS: tuple[str, ...] = ("name", "x", "y", "z", "location", "kind")
OPTIONAL_FIELDS: tuple[str, ...] = ("ooc", "category", "description")
VALID_KINDS: frozenset[str] = frozenset({"surface", "space"})


def serialize_poi(poi: dict[str, Any]) -> dict[str, Any]:
    """Project an internal POI dict down to the wire schema (no extra keys)."""
    return {
        "name": str(poi.get("name", "")),
        "x": float(poi.get("x", 0.0)),
        "y": float(poi.get("y", 0.0)),
        "z": float(poi.get("z", 0.0)),
        "location": str(poi.get("location") or "Unknown"),
        "ooc": poi.get("ooc"),
        "kind": str(poi.get("kind") or "space"),
        "category": str(poi.get("category") or ""),
        "description": str(poi.get("description") or ""),
    }


def to_json(poi: dict[str, Any]) -> str:
    return json.dumps(serialize_poi(poi), indent=2, ensure_ascii=False)


def parse_poi(text: str) -> dict[str, Any]:
    """
    Parse and validate a POI from a JSON string.

    Accepts either a single object or a one-element array (tolerant of common
    copy-paste shapes from the community site).

    Raises ValueError with a user-readable message on any validation failure.
    """
    if text is None or not text.strip():
        raise ValueError("Clipboard is empty.")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Not a valid JSON document ({e.msg}).") from e

    if isinstance(data, list):
        if len(data) != 1:
            raise ValueError("JSON array must contain exactly one POI.")
        data = data[0]

    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object (or a one-element array).")

    for field in REQUIRED_FIELDS:
        if field not in data:
            raise ValueError(f"Missing required field: '{field}'.")

    name = str(data["name"]).strip()
    if not name:
        raise ValueError("Field 'name' must not be empty.")

    try:
        x = float(data["x"])
        y = float(data["y"])
        z = float(data["z"])
    except (TypeError, ValueError) as e:
        raise ValueError("Fields x/y/z must be numeric.") from e

    kind = str(data["kind"]).strip().lower()
    if kind not in VALID_KINDS:
        raise ValueError(f"Field 'kind' must be 'surface' or 'space' (got '{kind}').")

    location = str(data["location"] or "Unknown").strip() or "Unknown"

    ooc_raw = data.get("ooc")
    ooc = None if ooc_raw in (None, "") else str(ooc_raw).strip()

    category = str(data.get("category") or "").strip()
    if category and category not in CATEGORY_SLUGS:
        raise ValueError(
            f"Unknown category '{category}'. "
            f"Expected one of: {', '.join(sorted(s for s in CATEGORY_SLUGS if s))}."
        )

    description = str(data.get("description") or "").strip()

    return {
        "name": name,
        "x": x,
        "y": y,
        "z": z,
        "location": location,
        "ooc": ooc,
        "kind": kind,
        "category": category,
        "description": description,
    }
