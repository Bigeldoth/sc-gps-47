import math
import json
import os
import re
import sys
import time
import logging
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


# Duration during which we tolerate OCR failing to find the zone name
# before considering that the player has left the target zone.
_ZONE_GRACE_PERIOD_S = 180.0  # 3 minutes

# Similarity threshold to consider two zone names identical
# (tolerance for OCR variations: OOC_Stanton1 vs 0OC_Stanton1 vs OOC Stanton1).
_ZONE_MATCH_THRESHOLD = 0.85


def _normalize_zone(name):
    """Normalize a zone name for robust comparison against OCR variations.

    Lowercase + strip all non-alphanumeric characters.
    E.g.: ``OOC_Stanton1_Hurston`` and ``OOC Stanton1-Hurston`` → ``oocstanton1hurston``.
    """
    if not name:
        return ""
    return re.sub(r'[^a-z0-9]+', '', name.lower())


def _zones_match(a, b, threshold=_ZONE_MATCH_THRESHOLD):
    """True if two zone names are considered the same zone.

    Tolerates OCR confusions (O/0, I/1, spaces/underscores) via
    difflib SequenceMatcher.
    """
    if not a or not b:
        return False
    a_norm = _normalize_zone(a)
    b_norm = _normalize_zone(b)
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return True
    # Containment fallback: paddle frequently truncates noisy zone reads to
    # the trailing landmark (`Zone:OOC_Stanton_3_ArcCorp` ↔ `ArcCorp`).
    # If one normalized name is a substring of the other AND that shared
    # segment is at least 5 chars (avoids spurious 2-char matches), treat
    # as the same zone. SequenceMatcher would score 0.28 here and miss it.
    short, long_ = (a_norm, b_norm) if len(a_norm) <= len(b_norm) else (b_norm, a_norm)
    if len(short) >= 5 and short in long_:
        return True
    return SequenceMatcher(None, a_norm, b_norm).ratio() >= threshold


def format_distance(distance_km):
    """Format a distance (in km) for display.

    Returns a string in metres below 1 km (no decimal), in kilometres
    above (two decimals). ``None`` becomes ``"---"``.
    """
    if distance_km is None:
        return "---"
    if distance_km < 1.0:
        return f"{distance_km * 1000:.0f} m"
    return f"{distance_km:.2f} km"


def normalize_angle_signed(deg):
    """Map an angle (degrees) into the interval ]-180, +180]."""
    if deg is None:
        return None
    return ((deg + 180.0) % 360.0) - 180.0


def ema_angle(prev, new, alpha):
    """EMA over angles with correct ±180° wrap-around handling."""
    if prev is None:
        return new
    diff = normalize_angle_signed(new - prev)
    return normalize_angle_signed(prev + alpha * diff)


def _effective_dz(target, current_pos):
    """Return the Z delta to use for distance/bearing calculations.

    Surface POIs (``kind == "surface"``) return 0.0: altitude does not
    contribute to the displayed remaining distance or to the pitch arrow.
    Space POIs (default) return the true Z difference.
    """
    if target.get("kind") == "surface":
        return 0.0
    return target["z"] - current_pos["z"]


def calculate_absolute_bearing(current_pos, target):
    """Absolute heading (world frame) from the current position to the target.

    Use when the player is stationary: the player's orientation is unknown
    but the world vector to reach can still be indicated.

    Args:
        current_pos: dict with ``x/y/z`` (km).
        target: dict with ``x/y/z`` (km).

    Returns:
        Dict ``{dx, dy, dz, yaw_deg, pitch_deg, distance_km}`` in world
        frame, or ``None`` if not computable.
    """
    if not target or current_pos is None:
        return None
    if current_pos.get("x") is None:
        return None

    dx = target["x"] - current_pos["x"]
    dy = target["y"] - current_pos["y"]
    dz = _effective_dz(target, current_pos)
    distance = math.sqrt(dx * dx + dy * dy + dz * dz)

    horiz = math.hypot(dx, dy)
    if dx == 0 and dy == 0:
        yaw = 0.0
    else:
        # In SC (OOC frame), the X axis is inverted relative to the standard
        # navigation convention (X+ points left, X- points right).
        # We negate dx to get: Y+ = forward (↑), X- = right (→), X+ = left (←).
        yaw = math.degrees(math.atan2(-dx, dy))
    if horiz == 0:
        pitch = 90.0 if dz > 0 else (-90.0 if dz < 0 else 0.0)
    else:
        pitch = math.degrees(math.atan2(dz, horiz))

    return {
        "dx": dx, "dy": dy, "dz": dz,
        "yaw_deg": yaw, "pitch_deg": pitch,
        "distance_km": distance,
    }


def format_axis_delta(km):
    """Format an axis delta for compact display.

    < 10 km: 1 decimal; < 1000 km: integer; >= 1000 km: 'k' notation.
    """
    if km is None:
        return "?"
    if abs(km) < 10:
        return f"{km:+.1f}"
    if abs(km) < 1000:
        return f"{km:+.0f}"
    return f"{km / 1000:+.1f}k"


def calculate_velocity_bearing(velocity, current_pos, target):
    """Signed (yaw, pitch) offsets in degrees between the movement direction
    and the target.

    Args:
        velocity: tuple ``(vx, vy, vz)`` in km/s, or ``None`` (stationary).
        current_pos: dict with ``x/y/z`` (km) — player position.
        target: dict with ``x/y/z`` (km) — destination.

    Returns:
        ``(yaw_off, pitch_off)`` in ]-180, +180], or ``None`` if velocity,
        position, or target are not defined.

        - Positive ``yaw_off``: target is to the right of the movement direction.
        - Positive ``pitch_off``: target is above the movement direction.
    """
    if velocity is None or target is None or current_pos is None:
        return None
    if current_pos.get("x") is None:
        return None

    vx, vy, vz = velocity
    if vx == 0 and vy == 0 and vz == 0:
        return None

    dx = target["x"] - current_pos["x"]
    dy = target["y"] - current_pos["y"]
    dz = _effective_dz(target, current_pos)
    if dx == 0 and dy == 0 and dz == 0:
        return 0.0, 0.0

    # Horizontal heading of velocity and target.
    # SC: X+ = left, X- = right → negate X to map to standard navigation frame.
    vel_yaw = math.degrees(math.atan2(-vx, vy)) if (vx or vy) else 0.0
    tgt_yaw = math.degrees(math.atan2(-dx, dy)) if (dx or dy) else 0.0
    yaw_off = normalize_angle_signed(tgt_yaw - vel_yaw)

    # Pitch (vertical component)
    vel_horiz = math.hypot(vx, vy)
    tgt_horiz = math.hypot(dx, dy)
    vel_pitch = math.degrees(math.atan2(vz, vel_horiz)) if vel_horiz > 0 else 0.0
    tgt_pitch = math.degrees(math.atan2(dz, tgt_horiz)) if tgt_horiz > 0 else 0.0
    pitch_off = normalize_angle_signed(tgt_pitch - vel_pitch)

    return yaw_off, pitch_off


class NavigationEngine:
    def __init__(self):
        from app_paths import user_data_dir
        self.user_poi_file = os.path.join(str(user_data_dir()), "data", "user_poi.json")

        # Create the user data subfolder if it does not exist.
        os.makedirs(os.path.dirname(self.user_poi_file), exist_ok=True)

        self.user_poi = self.load_user_poi()
        self.target = None

        # Zone tracking (see set_target/update_zone_tracking/is_target_in_same_ooc).
        # At navigation start we assume the player is already in the target zone.
        # If OCR confirms the zone at least once, we lock permanently.
        # Otherwise, after _ZONE_GRACE_PERIOD_S without a match, we consider out-of-zone.
        self._zone_match_locked = False
        self._zone_last_match_ts = None

    def load_user_poi(self):
        """Load points saved by the user."""
        if os.path.exists(self.user_poi_file):
            try:
                with open(self.user_poi_file, 'r') as f:
                    return json.load(f)
            except:
                return []
        return []

    def save_user_poi(self):
        """Save user points."""
        try:
            with open(self.user_poi_file, 'w') as f:
                json.dump(self.user_poi, f, indent=4)
            return True
        except:
            return False

    def add_user_point(self, name, x, y, z, location="Unknown", ooc=None, kind="space", category=""):
        """Add a new custom point.

        ``ooc`` (ObjectContainer name, e.g. ``Stanton_1_Hurston``) identifies
        the planet-relative frame. Required for navigation: distance and
        bearing can only be computed within the same OOC.

        ``kind`` is either ``"surface"`` (planet/moon — Z ignored for distance
        and pitch) or ``"space"`` (3D distance and pitch as usual).

        ``category`` is a SpaceDrive Community taxonomy slug (see
        ``poi_categories``). Empty string means "Uncategorized".
        """
        point = {
            "name": name,
            "x": x,
            "y": y,
            "z": z,
            "location": location,
            "ooc": ooc,
            "kind": kind,
            "category": category,
        }
        self.user_poi.append(point)
        self.save_user_poi()
        return point

    def export_points(self, export_path):
        """Export user points to a JSON file."""
        try:
            with open(export_path, 'w') as f:
                json.dump(self.user_poi, f, indent=4)
            return True
        except:
            return False

    def set_target(self, x, y, z, name="Destination", ooc=None, kind="space"):
        """Set the destination and start zone tracking.

        ``ooc`` is the expected zone name. It is assumed the user is already
        in that zone — navigation starts immediately.

        Zone tracking (``update_zone_tracking`` + ``is_target_in_same_ooc``)
        confirms this assumption on the first OCR match. If no match occurs
        within ``_ZONE_GRACE_PERIOD_S`` seconds, the player is considered
        out-of-zone.

        ``kind`` ("surface" or "space") controls whether the Z axis
        contributes to distance and pitch calculations. Surface POIs use
        horizontal-only navigation so altitude mismatch does not inflate
        the displayed remaining distance.
        """
        self.target = {"x": x, "y": y, "z": z, "name": name, "ooc": ooc, "kind": kind}
        self._zone_match_locked = False
        self._zone_last_match_ts = time.monotonic()
        if ooc:
            logger.info(f"Navigating to '{name}' (expected zone: {ooc!r}) — assuming in-zone")

    def clear_target(self):
        """Cancel navigation and reset zone tracking."""
        self.target = None
        self._zone_match_locked = False
        self._zone_last_match_ts = None

    def update_zone_tracking(self, current_pos):
        """Update zone tracking based on the current OCR scan result.

        Call on each OCR result. If the current zone name matches
        (per ``_zones_match``) the target zone, lock permanently.
        Otherwise, let the grace period run.
        """
        if not self.target or current_pos is None:
            return
        if self._zone_match_locked:
            return  # already locked, no further comparison needed
        cur_ooc = current_pos.get("ooc")
        tgt_ooc = self.target.get("ooc")
        if not cur_ooc or not tgt_ooc:
            return
        if _zones_match(cur_ooc, tgt_ooc):
            self._zone_match_locked = True
            self._zone_last_match_ts = time.monotonic()
            logger.info(
                f"Zone confirmed by OCR: '{cur_ooc}' ≈ '{tgt_ooc}' — permanent lock"
            )

    def is_target_in_same_ooc(self, current_pos):
        """True if the player is considered to be in the target zone.

        Logic:
          1. Permanent lock after the first OCR-confirmed match via ``update_zone_tracking``.
          2. Otherwise: tolerance for ``_ZONE_GRACE_PERIOD_S`` (3 min) from
             ``set_target`` — assume in-zone.
          3. Beyond that with no match: considered out-of-zone.
        """
        if not self.target:
            return False
        if self._zone_match_locked:
            return True
        if self._zone_last_match_ts is None:
            return False
        return (time.monotonic() - self._zone_last_match_ts) <= _ZONE_GRACE_PERIOD_S

    def time_until_zone_expired(self):
        """Seconds remaining before the zone expires.

        Returns ``None`` if permanently locked or if there is no active target.
        """
        if self._zone_match_locked or self.target is None or self._zone_last_match_ts is None:
            return None
        elapsed = time.monotonic() - self._zone_last_match_ts
        return max(0.0, _ZONE_GRACE_PERIOD_S - elapsed)

    def calculate_distance(self, current_pos):
        """Euclidean distance between the current position and the target.

        Coordinates in km in the **planet-relative** frame (OOC). Returns
        ``None`` if the target is not set, the current position is missing,
        OR the player and target are in different OOCs (distance has no
        meaning without a cross-zone transform).

        For surface POIs (``kind == "surface"``) Z is ignored: distance is
        horizontal only, so altitude mismatch does not inflate the result.
        """
        if not self.target or current_pos.get("x") is None:
            return None
        if not self.is_target_in_same_ooc(current_pos):
            return None

        dx = self.target["x"] - current_pos["x"]
        dy = self.target["y"] - current_pos["y"]
        dz = _effective_dz(self.target, current_pos)

        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def calculate_bearing(self, current_pos):
        """Simple direction vector computation (Pitch/Yaw)."""
        if not self.target or current_pos["x"] is None:
            return None

        dx = self.target["x"] - current_pos["x"]
        dy = self.target["y"] - current_pos["y"]
        dz = _effective_dz(self.target, current_pos)

        # In SC space navigation, visual alignment is typically used.
        # This engine returns deltas to help the overlay place a cursor.
        return {"dx": dx, "dy": dy, "dz": dz}

