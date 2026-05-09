"""Tests unitaires pour navigation.format_distance et calculate_distance."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from navigation import NavigationEngine, format_distance


def test_format_distance_none():
    assert format_distance(None) == "---"


def test_format_distance_zero():
    assert format_distance(0.0) == "0 m"


def test_format_distance_sub_kilometer():
    assert format_distance(0.523) == "523 m"
    assert format_distance(0.001) == "1 m"


def test_format_distance_just_under_one_km():
    # 0.9999 km arrondi à 1000 m — acceptable, on est à la limite
    assert format_distance(0.9999) == "1000 m"


def test_format_distance_one_km_exact():
    assert format_distance(1.0) == "1.00 km"


def test_format_distance_kilometers():
    assert format_distance(12.345) == "12.35 km"
    assert format_distance(1234.5678) == "1234.57 km"


def test_calculate_distance_unit_is_kilometers():
    """Coords en km → distance en km. Cible à (3,4,0), nous à origine → 5 km."""
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
    nav.set_target(1.0, 2.0, 3.0, "Cible", ooc="Z")
    assert nav.calculate_distance({"x": None, "y": None, "z": None, "ooc": "Z"}) is None


def test_calculate_distance_same_point():
    nav = NavigationEngine()
    nav.set_target(100.0, 200.0, 300.0, "Cible", ooc="Stanton_1_Hurston")
    assert nav.calculate_distance(
        {"x": 100.0, "y": 200.0, "z": 300.0, "ooc": "Stanton_1_Hurston"}
    ) == 0.0


# ---- Distance avec OOC mismatch ----

def test_distance_ooc_mismatch_returns_none():
    """Cible Hurston, joueur sur Microtech : distance n'a pas de sens."""
    nav = NavigationEngine()
    nav.set_target(130.0, 50.0, 990.0, "Cible_Hurston", ooc="Stanton_1_Hurston")
    d = nav.calculate_distance(
        {"x": 200.0, "y": 100.0, "z": 50.0, "ooc": "Stanton_4_Microtech"}
    )
    assert d is None


def test_distance_ooc_match_computed():
    nav = NavigationEngine()
    nav.set_target(100.0, 0.0, 0.0, "Cible_H", ooc="Stanton_1_Hurston")
    d = nav.calculate_distance(
        {"x": 0.0, "y": 0.0, "z": 0.0, "ooc": "Stanton_1_Hurston"}
    )
    assert abs(d - 100.0) < 1e-6


def test_distance_legacy_target_no_ooc_returns_none():
    """POI legacy sans ooc → on ne peut pas naviguer."""
    nav = NavigationEngine()
    nav.set_target(100.0, 200.0, 300.0, "POI_legacy", ooc=None)
    d = nav.calculate_distance(
        {"x": 100.0, "y": 200.0, "z": 300.0, "ooc": "Stanton_1_Hurston"}
    )
    assert d is None


def test_is_target_in_same_ooc():
    nav = NavigationEngine()
    nav.set_target(0, 0, 0, "T", ooc="A")
    assert nav.is_target_in_same_ooc({"x": 1, "ooc": "A"}) is True
    assert nav.is_target_in_same_ooc({"x": 1, "ooc": "B"}) is False
    assert nav.is_target_in_same_ooc({"x": 1, "ooc": None}) is False
    assert nav.is_target_in_same_ooc({"x": 1}) is False


def test_add_user_point_records_ooc():
    nav = NavigationEngine()
    nav.user_poi = []  # éviter de polluer le fichier réel
    nav.user_poi_file = "/tmp/_dummy_user_poi.json"
    pt = nav.add_user_point("test", 1.0, 2.0, 3.0, "Stanton", ooc="Stanton_1_Hurston")
    assert pt["ooc"] == "Stanton_1_Hurston"
