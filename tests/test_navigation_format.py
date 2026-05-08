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
    nav.set_target(3.0, 4.0, 0.0, "Pythagore")
    dist = nav.calculate_distance({"x": 0.0, "y": 0.0, "z": 0.0})
    assert dist == 5.0


def test_calculate_distance_no_target():
    nav = NavigationEngine()
    nav.target = None
    assert nav.calculate_distance({"x": 0.0, "y": 0.0, "z": 0.0}) is None


def test_calculate_distance_no_position():
    nav = NavigationEngine()
    nav.set_target(1.0, 2.0, 3.0, "Cible")
    assert nav.calculate_distance({"x": None, "y": None, "z": None}) is None


def test_calculate_distance_same_point():
    nav = NavigationEngine()
    nav.set_target(100.0, 200.0, 300.0, "Cible")
    assert nav.calculate_distance({"x": 100.0, "y": 200.0, "z": 300.0}) == 0.0
