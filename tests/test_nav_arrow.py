"""Unit tests for the nav-arrow rendering helpers in main.py.

Covers A4 graded pitch: the vertical indicator now carries the angle
(``▲N°`` / ``▼N°``) instead of the old binary ``▲`` / ``▼``, with a dead-band
and surface-POI suppression.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from main import (
    _pitch_indicator,
    _velocity_arrow,
    _world_arrow,
    _PITCH_DEADBAND_DEG,
)


# ----- _pitch_indicator -----

def test_pitch_indicator_none_is_empty():
    # Surface POIs pass None — Z is ignored, no vertical cue.
    assert _pitch_indicator(None) == ""


def test_pitch_indicator_within_deadband_is_empty():
    assert _pitch_indicator(0.0) == ""
    assert _pitch_indicator(_PITCH_DEADBAND_DEG - 0.1) == ""
    assert _pitch_indicator(-(_PITCH_DEADBAND_DEG - 0.1)) == ""


def test_pitch_indicator_graded_up_and_down():
    assert _pitch_indicator(12.0) == "▲12°"
    assert _pitch_indicator(-8.5) == ""        # below dead-band
    assert _pitch_indicator(-30.4) == "▼30°"   # rounds to whole degrees
    assert _pitch_indicator(89.6) == "▲90°"


def test_pitch_indicator_boundary_is_shown():
    assert _pitch_indicator(_PITCH_DEADBAND_DEG) == f"▲{_PITCH_DEADBAND_DEG:.0f}°"


# ----- _velocity_arrow (graded pitch appended) -----

def test_velocity_arrow_on_course_no_pitch():
    assert _velocity_arrow(5.0, None) == "↑"


def test_velocity_arrow_turn_with_graded_pitch():
    assert _velocity_arrow(15.0, 12.0) == "↑ ▲12°"
    assert _velocity_arrow(40.0, -25.0) == "→40° ▼25°"
    assert _velocity_arrow(-40.0, 25.0) == "←40° ▲25°"


def test_velocity_arrow_uturn():
    assert _velocity_arrow(170.0, None) == "↓"


def test_velocity_arrow_surface_suppresses_pitch():
    # Even with a large vertical offset, surface POIs (pitch_off=None) get no cue.
    assert _velocity_arrow(40.0, None) == "→40°"


# ----- _world_arrow (stationary, graded pitch concatenated) -----

def test_world_arrow_graded_pitch():
    assert _world_arrow({"yaw_deg": 0.0, "pitch_deg": 30.0}) == "↑▲30°"
    assert _world_arrow({"yaw_deg": 0.0, "pitch_deg": -45.0}) == "↑▼45°"


def test_world_arrow_within_deadband_no_pitch():
    assert _world_arrow({"yaw_deg": 0.0, "pitch_deg": 2.0}) == "↑"


def test_world_arrow_surface_pitch_zero():
    # Surface POIs resolve to pitch 0 via _effective_dz → no vertical cue.
    assert _world_arrow({"yaw_deg": 90.0, "pitch_deg": 0.0}) == "→"


def test_world_arrow_empty_bearing():
    assert _world_arrow(None) == ""
