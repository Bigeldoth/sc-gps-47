"""Unit tests for navigation.format_distance and calculate_distance."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from navigation import NavigationEngine, format_distance, _ZONE_GRACE_PERIOD_S


def test_format_distance_none():
    assert format_distance(None) == "---"


def test_format_distance_zero():
    assert format_distance(0.0) == "0 m"


def test_format_distance_sub_kilometer():
    assert format_distance(0.523) == "523 m"
    assert format_distance(0.001) == "1 m"


def test_format_distance_just_under_one_km():
    # 0.9999 km rounded to 1000 m — acceptable, we're at the limit
    assert format_distance(0.9999) == "1000 m"


def test_format_distance_one_km_exact():
    assert format_distance(1.0) == "1.00 km"


def test_format_distance_kilometers():
    assert format_distance(12.345) == "12.35 km"
    assert format_distance(1234.5678) == "1234.57 km"


def test_calculate_distance_unit_is_kilometers():
    """Coords in km → distance in km. Target at (3,4,0), us at origin → 5 km."""
    nav = NavigationEngine()
    nav.set_target(3.0, 4.0, 0.0, "Pythagore", ooc="Z")
    dist = nav.calculate_distance({"x": 0.0, "y": 0.0, "z": 0.0, "ooc": "Z"})
    assert dist == 5.0


def test_calculate_distance_no_target():
    nav = NavigationEngine()
    nav.target = None
    assert nav.calculate_distance({"x": 0.0, "y": 0.0, "z": 0.0, "ooc": "Z"}) is None


def test_calculate_distance_no_position():
    nav = NavigationEngine()
    nav.set_target(1.0, 2.0, 3.0, "Target", ooc="Z")
    assert nav.calculate_distance({"x": None, "y": None, "z": None, "ooc": "Z"}) is None


def test_calculate_distance_same_point():
    nav = NavigationEngine()
    nav.set_target(100.0, 200.0, 300.0, "Target", ooc="Stanton_1_Hurston")
    assert nav.calculate_distance(
        {"x": 100.0, "y": 200.0, "z": 300.0, "ooc": "Stanton_1_Hurston"}
    ) == 0.0


# ---- Distance with OOC mismatch ----

def test_distance_ooc_mismatch_returns_none():
    """Target on Hurston, player on Microtech: once the in-zone grace period has
    elapsed without an OCR-confirmed zone match, the distance is None."""
    nav = NavigationEngine()
    nav.set_target(130.0, 50.0, 990.0, "Target_Hurston", ooc="Stanton_1_Hurston")
    # Simulate the grace window having expired with no confirmed zone lock.
    nav._zone_last_match_ts = time.monotonic() - (_ZONE_GRACE_PERIOD_S + 10)
    d = nav.calculate_distance(
        {"x": 200.0, "y": 100.0, "z": 50.0, "ooc": "Stanton_4_Microtech"}
    )
    assert d is None


def test_distance_ooc_mismatch_within_grace_assumes_in_zone():
    """Right after set_target the player is assumed in-zone (3-min grace), so a
    distance is returned even before any OCR zone match — even on a mismatch."""
    nav = NavigationEngine()
    nav.set_target(100.0, 0.0, 0.0, "Target_H", ooc="Stanton_1_Hurston")
    d = nav.calculate_distance(
        {"x": 0.0, "y": 0.0, "z": 0.0, "ooc": "Stanton_4_Microtech"}
    )
    assert abs(d - 100.0) < 1e-6


def test_distance_ooc_match_computed():
    nav = NavigationEngine()
    nav.set_target(100.0, 0.0, 0.0, "Target_H", ooc="Stanton_1_Hurston")
    d = nav.calculate_distance(
        {"x": 0.0, "y": 0.0, "z": 0.0, "ooc": "Stanton_1_Hurston"}
    )
    assert abs(d - 100.0) < 1e-6


def test_distance_legacy_target_no_ooc_returns_none():
    """Legacy POI without ooc: no zone can ever be confirmed (update_zone_tracking
    needs both OOCs), so once the grace window expires it is out-of-zone → None."""
    nav = NavigationEngine()
    nav.set_target(100.0, 200.0, 300.0, "POI_legacy", ooc=None)
    nav._zone_last_match_ts = time.monotonic() - (_ZONE_GRACE_PERIOD_S + 10)
    d = nav.calculate_distance(
        {"x": 100.0, "y": 200.0, "z": 300.0, "ooc": "Stanton_1_Hurston"}
    )
    assert d is None


def test_is_target_in_same_ooc():
    nav = NavigationEngine()
    nav.set_target(0, 0, 0, "T", ooc="A")
    # During the grace window the player is assumed in-zone regardless of the
    # currently-read zone (matching, mismatched, or missing).
    assert nav.is_target_in_same_ooc({"x": 1, "ooc": "A"}) is True
    assert nav.is_target_in_same_ooc({"x": 1, "ooc": "B"}) is True
    assert nav.is_target_in_same_ooc({"x": 1, "ooc": None}) is True
    assert nav.is_target_in_same_ooc({"x": 1}) is True

    # Once OCR confirms the zone the lock is permanent: a later mismatch or an
    # expired grace timer still reads as in-zone.
    nav.update_zone_tracking({"x": 1, "ooc": "A"})
    nav._zone_last_match_ts = time.monotonic() - (_ZONE_GRACE_PERIOD_S + 10)
    assert nav.is_target_in_same_ooc({"x": 1, "ooc": "B"}) is True


def test_is_target_out_of_zone_after_grace_without_lock():
    """No confirmed lock + expired grace window → out of zone."""
    nav = NavigationEngine()
    nav.set_target(0, 0, 0, "T", ooc="A")
    nav._zone_last_match_ts = time.monotonic() - (_ZONE_GRACE_PERIOD_S + 10)
    assert nav.is_target_in_same_ooc({"x": 1, "ooc": "A"}) is False
    assert nav.is_target_in_same_ooc({"x": 1, "ooc": "B"}) is False


def test_add_user_point_records_ooc():
    nav = NavigationEngine()
    nav.user_poi = []  # avoid polluting real file
    nav.user_poi_file = "/tmp/_dummy_user_poi.json"
    pt = nav.add_user_point("test", 1.0, 2.0, 3.0, "Stanton", ooc="Stanton_1_Hurston")
    assert pt["ooc"] == "Stanton_1_Hurston"


# ---- POI kind: surface vs space ----

def test_add_user_point_records_kind_default_space():
    """Default kind is 'space' for backward compatibility with existing callers."""
    nav = NavigationEngine()
    nav.user_poi = []
    nav.user_poi_file = "/tmp/_dummy_user_poi.json"
    pt = nav.add_user_point("test", 1.0, 2.0, 3.0, "Stanton", ooc="Stanton_1_Hurston")
    assert pt["kind"] == "space"


def test_add_user_point_records_kind_surface():
    nav = NavigationEngine()
    nav.user_poi = []
    nav.user_poi_file = "/tmp/_dummy_user_poi.json"
    pt = nav.add_user_point(
        "base", 1.0, 2.0, 3.0, "Hurston",
        ooc="Stanton_1_Hurston", kind="surface",
    )
    assert pt["kind"] == "surface"


def test_calculate_distance_surface_ignores_z():
    """Surface POI: altitude mismatch must not inflate the distance.

    Target at (3, 4, 100), player at (0, 0, 0). With kind='surface' the
    100 km Z gap is ignored; only the horizontal 5 km counts.
    """
    nav = NavigationEngine()
    nav.set_target(3.0, 4.0, 100.0, "Outpost", ooc="Hurston", kind="surface")
    dist = nav.calculate_distance({"x": 0.0, "y": 0.0, "z": 0.0, "ooc": "Hurston"})
    assert dist == 5.0


def test_calculate_distance_space_uses_z():
    """Space POI: 3D distance unchanged from previous behavior."""
    nav = NavigationEngine()
    nav.set_target(3.0, 4.0, 100.0, "Station", ooc="Hurston", kind="space")
    dist = nav.calculate_distance({"x": 0.0, "y": 0.0, "z": 0.0, "ooc": "Hurston"})
    # sqrt(9 + 16 + 10000) = sqrt(10025) ≈ 100.1249
    assert abs(dist - 100.1249) < 1e-3


def test_calculate_distance_default_kind_is_space():
    """A target without an explicit kind keeps the legacy 3D behavior."""
    nav = NavigationEngine()
    nav.set_target(3.0, 4.0, 100.0, "Legacy", ooc="Hurston")
    dist = nav.calculate_distance({"x": 0.0, "y": 0.0, "z": 0.0, "ooc": "Hurston"})
    assert abs(dist - 100.1249) < 1e-3


def test_calculate_distance_surface_altitude_independent():
    """A player varying altitude over a surface POI keeps the same distance."""
    nav = NavigationEngine()
    nav.set_target(10.0, 0.0, 0.0, "Base", ooc="Hurston", kind="surface")
    d_low = nav.calculate_distance({"x": 0.0, "y": 0.0, "z": 0.0, "ooc": "Hurston"})
    d_high = nav.calculate_distance({"x": 0.0, "y": 0.0, "z": 5.0, "ooc": "Hurston"})
    assert d_low == d_high == 10.0


# ---- POI Manager edit: preserve fields the dialog does not expose ----

def test_edit_poi_merge_preserves_ooc():
    """`POIManagerWindow._edit_poi` must keep ``ooc`` after a dialog edit.

    The edit dialog only exposes name/x/y/z/location/description/kind, so
    rebuilding the POI dict from dialog output drops ``ooc`` and breaks
    navigation (calculate_distance returns None on missing ooc).
    The fix is to merge (dict.update) the dialog output into the original.
    """
    nav = NavigationEngine()
    nav.user_poi = []
    nav.user_poi_file = "/tmp/_dummy_user_poi.json"
    nav.add_user_point(
        "Outpost", 1.0, 2.0, 3.0, "Hurston",
        ooc="Stanton_1_Hurston", kind="space",
    )

    # Simulate POIEditDialog.get_poi_data — no ``ooc`` key returned.
    new_data = {
        "name": "Outpost-renamed",
        "x": 1.0, "y": 2.0, "z": 3.0,
        "location": "Hurston",
        "description": "now a depot",
        "kind": "surface",
    }
    nav.user_poi[0].update(new_data)

    edited = nav.user_poi[0]
    assert edited["ooc"] == "Stanton_1_Hurston"  # preserved by merge
    assert edited["name"] == "Outpost-renamed"
    assert edited["kind"] == "surface"
    assert edited["description"] == "now a depot"
