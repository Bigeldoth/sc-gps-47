"""Tests for great-circle (orthodromic) surface navigation (roadmap A7).

Surface POIs derive their remaining distance from the over-the-surface
great-circle arc length (``R * theta``) instead of a flat 2D Euclidean
distance, and their world arrow points along the great-circle initial
heading. Space POIs keep the legacy 3D Euclidean behaviour.

The SC OOC frame has the planet/moon centre at the origin (km), so a
surface point's vector magnitude is roughly ``body_radius + altitude``.
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from navigation import (
    NavigationEngine,
    great_circle_distance,
    great_circle_bearing,
    calculate_absolute_bearing,
    calculate_velocity_bearing,
    normalize_angle_signed,
)


# A realistic SC moon radius (km). Lyria/Aberdeen-class bodies sit around
# 150-450 km; using ~300 km keeps the curvature meaningful.
R = 300.0


def _surface_point(lat_deg, lon_deg, radius=R, altitude=0.0):
    """Build an OOC-frame {x, y, z} vector for a point on a sphere.

    Latitude is measured from the equatorial (X/Y) plane toward +Z; longitude
    is measured in the X/Y plane from +Y toward +X. The exact mapping does not
    matter for the tests — only that points sit on a sphere of given radius.
    """
    r = radius + altitude
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    z = r * math.sin(lat)
    horiz = r * math.cos(lat)
    x = horiz * math.sin(lon)
    y = horiz * math.cos(lon)
    return {"x": x, "y": y, "z": z}


# ---- (a) great-circle distance noticeably > flat distance when far apart ----

def test_great_circle_far_exceeds_flat_distance():
    """For widely separated surface points the arc length is clearly longer
    than the straight (chord-projected flat) distance."""
    a = _surface_point(0.0, 0.0)
    b = _surface_point(0.0, 120.0)  # 120 deg of longitude apart on the equator

    gc = great_circle_distance(a, b)

    # Flat horizontal distance in the planet-relative frame (old behaviour).
    flat = math.hypot(b["x"] - a["x"], b["y"] - a["y"])

    # Expected arc length: 120 deg of a great circle of radius R.
    expected = R * math.radians(120.0)
    assert abs(gc - expected) < 1e-6
    # Curvature makes the arc meaningfully longer than the flat chord distance.
    # At 120 deg the arc/chord ratio is ~1.21, so require a clear >15% margin.
    assert gc > flat * 1.15


# ---- (b) near points: great-circle ≈ flat distance (small-angle agreement) ----

def test_great_circle_near_matches_flat():
    """For closely spaced surface points the arc length agrees with the flat
    distance to within a tight relative tolerance (small-angle limit)."""
    a = _surface_point(0.0, 0.0)
    b = _surface_point(0.0, 0.5)  # half a degree of longitude (~2.6 km on R=300)

    gc = great_circle_distance(a, b)
    flat = math.hypot(b["x"] - a["x"], b["y"] - a["y"])

    assert flat > 0.0
    # Within 0.1% for such a small separation.
    assert abs(gc - flat) / flat < 1e-3


# ---- (c) space POIs unchanged vs the old 3D Euclidean distance ----

def test_space_poi_distance_unchanged():
    nav = NavigationEngine()
    nav.set_target(3.0, 4.0, 100.0, "Station", ooc="Hurston", kind="space")
    dist = nav.calculate_distance({"x": 0.0, "y": 0.0, "z": 0.0, "ooc": "Hurston"})
    # sqrt(9 + 16 + 10000) = sqrt(10025)
    assert abs(dist - math.sqrt(10025.0)) < 1e-6


def test_space_poi_full_3d_far_apart():
    """A far-apart space POI keeps the plain 3D chord distance (no arc)."""
    a = _surface_point(0.0, 0.0)
    b = _surface_point(0.0, 120.0)
    nav = NavigationEngine()
    nav.set_target(b["x"], b["y"], b["z"], "Deep", ooc="Z", kind="space")
    dist = nav.calculate_distance({**a, "ooc": "Z"})
    chord = math.sqrt(
        (b["x"] - a["x"]) ** 2 + (b["y"] - a["y"]) ** 2 + (b["z"] - a["z"]) ** 2
    )
    assert abs(dist - chord) < 1e-6
    # And the chord must be shorter than the great-circle arc.
    assert dist < great_circle_distance(a, b)


# ---- (d) identical points → 0 ----

def test_great_circle_identical_points_zero():
    a = _surface_point(12.0, 34.0)
    assert great_circle_distance(a, dict(a)) == 0.0


def test_calculate_distance_surface_same_point_zero():
    p = _surface_point(12.0, 34.0)
    nav = NavigationEngine()
    nav.set_target(p["x"], p["y"], p["z"], "Outpost", ooc="Z", kind="surface")
    dist = nav.calculate_distance({**p, "ooc": "Z"})
    assert abs(dist) < 1e-9


# ---- (e) bearing sanity given the SC X-inversion ----

def test_bearing_target_ahead_is_up():
    """A target directly ahead (greater +Y, same X) yields yaw ~0 (up arrow)."""
    a = _surface_point(0.0, 0.0)
    b = _surface_point(0.0, 0.4)  # small +X, +Y shift but mostly forward
    # Make it purely forward: a point on the same meridian, slightly toward +Y.
    a = {"x": 0.0, "y": R, "z": 0.0}
    b = {"x": 0.0, "y": R, "z": 5.0}  # straight up in Z -> tangent toward +Z
    # For a meridian move (Z increases, X stays 0) the horizontal tangent has
    # tx == 0, so yaw collapses to 0 (forward) per the convention.
    yaw = great_circle_bearing(a, b)
    assert abs(yaw) < 1e-6


def test_bearing_target_east_is_right():
    """SC X-inversion: a target toward +X (game 'left' axis) must read as a
    LEFT turn (negative yaw), and toward -X as a RIGHT turn (positive yaw)."""
    a = {"x": 0.0, "y": R, "z": 0.0}
    # Tangent toward +X: move b in +X while staying on the sphere region.
    b_plus_x = {"x": 50.0, "y": R, "z": 0.0}
    yaw_plus = great_circle_bearing(a, b_plus_x)
    assert yaw_plus < 0.0  # +X -> left -> negative yaw

    b_minus_x = {"x": -50.0, "y": R, "z": 0.0}
    yaw_minus = great_circle_bearing(a, b_minus_x)
    assert yaw_minus > 0.0  # -X -> right -> positive yaw


def test_bearing_matches_absolute_bearing_convention_small_angle():
    """For close points the great-circle yaw matches calculate_absolute_bearing
    (the legacy flat yaw) within a small tolerance."""
    a = _surface_point(5.0, 10.0)
    b = _surface_point(5.2, 10.3)
    gc_yaw = great_circle_bearing(a, b)
    flat = calculate_absolute_bearing(a, {**b, "kind": "space"})
    assert abs(((gc_yaw - flat["yaw_deg"] + 180.0) % 360.0) - 180.0) < 1.0


def test_calculate_absolute_bearing_surface_uses_great_circle():
    """calculate_absolute_bearing on a surface POI returns the great-circle
    distance/yaw and a zeroed pitch (altitude ignored)."""
    a = _surface_point(0.0, 0.0)
    b = _surface_point(0.0, 90.0)
    res = calculate_absolute_bearing(a, {**b, "kind": "surface"})
    assert res is not None
    assert abs(res["distance_km"] - R * math.radians(90.0)) < 1e-6
    assert res["pitch_deg"] == 0.0
    assert abs(res["yaw_deg"] - great_circle_bearing(a, b)) < 1e-9


# ---- (f) degenerate guards do not crash ----

def test_distance_origin_falls_back_to_flat():
    """Player at the body centre (magnitude 0) -> flat horizontal fallback,
    must not divide by zero or raise."""
    a = {"x": 0.0, "y": 0.0, "z": 0.0}
    b = {"x": 3.0, "y": 4.0, "z": 100.0}
    assert great_circle_distance(a, b) == 5.0


def test_bearing_origin_falls_back():
    a = {"x": 0.0, "y": 0.0, "z": 0.0}
    b = {"x": 0.0, "y": 10.0, "z": 0.0}
    # Forward target from origin -> yaw 0.
    assert great_circle_bearing(a, b) == 0.0


def test_bearing_identical_points_no_crash():
    p = _surface_point(1.0, 2.0)
    assert great_circle_bearing(p, dict(p)) == 0.0


def test_distance_antipodal_no_crash():
    """Antipodal points: theta ~ pi, half the circumference, no crash."""
    a = _surface_point(0.0, 0.0)
    b = {"x": -a["x"], "y": -a["y"], "z": -a["z"]}  # exact antipode
    gc = great_circle_distance(a, b)
    assert abs(gc - R * math.pi) < 1e-6


def test_bearing_antipodal_no_crash():
    a = _surface_point(0.0, 0.0)
    b = {"x": -a["x"], "y": -a["y"], "z": -a["z"]}
    # Tangent is undefined (colinear) -> fallback to delta heading, no crash.
    yaw = great_circle_bearing(a, b)
    assert isinstance(yaw, float)


def test_surface_distance_legacy_origin_inputs_preserved():
    """The existing surface tests pass origin-relative coords; great-circle must
    degrade to the same flat distance so legacy behaviour is preserved."""
    nav = NavigationEngine()
    nav.set_target(3.0, 4.0, 100.0, "Outpost", ooc="Hurston", kind="surface")
    dist = nav.calculate_distance({"x": 0.0, "y": 0.0, "z": 0.0, "ooc": "Hurston"})
    assert dist == 5.0


# ---- moving guidance: velocity bearing follows the great circle on surfaces ----

def test_velocity_bearing_surface_uses_great_circle_heading():
    """For a far surface target the moving turn arrow must aim along the
    great-circle initial heading (consistent with the stationary world arrow),
    not the straight chord heading."""
    a = _surface_point(0.0, 0.0)
    b = _surface_point(40.0, 70.0)  # far enough that chord != arc heading
    target = {"x": b["x"], "y": b["y"], "z": b["z"], "kind": "surface"}
    velocity = (0.05, 0.10, -0.02)  # arbitrary movement (km/s)

    yaw_off, _pitch_off = calculate_velocity_bearing(velocity, a, target)

    vx, vy, _vz = velocity
    vel_yaw = math.degrees(math.atan2(-vx, vy))
    expected = normalize_angle_signed(great_circle_bearing(a, target) - vel_yaw)
    assert abs(yaw_off - expected) < 1e-6

    # And it genuinely differs from the old chord-based heading for a far target.
    dx, dy = target["x"] - a["x"], target["y"] - a["y"]
    chord_off = normalize_angle_signed(math.degrees(math.atan2(-dx, dy)) - vel_yaw)
    assert abs(normalize_angle_signed(yaw_off - chord_off)) > 1.0


def test_velocity_bearing_space_unchanged_chord_heading():
    """Space POIs keep the straight chord heading in the moving arrow."""
    a = _surface_point(0.0, 0.0)
    b = _surface_point(40.0, 70.0)
    target = {"x": b["x"], "y": b["y"], "z": b["z"], "kind": "space"}
    velocity = (0.05, 0.10, -0.02)

    yaw_off, _ = calculate_velocity_bearing(velocity, a, target)

    vx, vy, _vz = velocity
    vel_yaw = math.degrees(math.atan2(-vx, vy))
    dx, dy = target["x"] - a["x"], target["y"] - a["y"]
    expected = normalize_angle_signed(math.degrees(math.atan2(-dx, dy)) - vel_yaw)
    assert abs(yaw_off - expected) < 1e-6
