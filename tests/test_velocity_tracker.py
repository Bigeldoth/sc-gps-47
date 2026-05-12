"""Tests for VelocityTracker and calculate_velocity_bearing."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from velocity_tracker import VelocityTracker
from navigation import calculate_velocity_bearing


# ---- VelocityTracker ----

def test_initial_state():
    vt = VelocityTracker()
    assert vt.velocity is None
    assert vt.speed_km_s == 0.0
    assert vt.is_moving is False


def test_first_sample_no_velocity():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    # With single sample, no velocity yet
    assert vt.velocity is None


def test_second_sample_basic_velocity():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(1.0, 0.0, 0.0, t=1.0)  # 1 km in 1 s = 1 km/s
    vx, vy, vz = vt.velocity
    assert abs(vx - 1.0) < 1e-6
    assert vy == 0.0
    assert vz == 0.0
    assert abs(vt.speed_km_s - 1.0) < 1e-6
    assert vt.is_moving is True


def test_diagonal_velocity():
    vt = VelocityTracker(smoothing_alpha=1.0)  # no smoothing for simple test
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(3.0, 4.0, 0.0, t=1.0)  # speed 5 km/s (3-4-5 triangle)
    assert abs(vt.speed_km_s - 5.0) < 1e-6


def test_stationary_below_threshold():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(0.001, 0.0, 0.0, t=1.0)  # 1 m/s = 0.001 km/s, below 0.05
    assert vt.is_moving is False


def test_dt_too_large_resets():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(100.0, 0.0, 0.0, t=10.0)  # dt=10 > MAX_DT_S=3 → reset
    # After reset, single sample → velocity None
    assert vt.velocity is None


def test_dt_zero_or_negative_ignored():
    vt = VelocityTracker(smoothing_alpha=1.0)
    vt.add_sample(0.0, 0.0, 0.0, t=1.0)
    vt.add_sample(1.0, 0.0, 0.0, t=2.0)
    v_before = vt.velocity
    vt.add_sample(2.0, 0.0, 0.0, t=2.0)  # same t → ignored
    assert vt.velocity == v_before


def test_smoothing_ema():
    # alpha=0.5: new sample accounts for 50%
    vt = VelocityTracker(smoothing_alpha=0.5)
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(2.0, 0.0, 0.0, t=1.0)  # inst vel = 2
    # First vel: not smoothed yet (initialized directly)
    assert abs(vt.velocity[0] - 2.0) < 1e-6
    vt.add_sample(2.0, 0.0, 0.0, t=2.0)  # inst vel = 0, smoothed = 0.5*0 + 0.5*2 = 1
    assert abs(vt.velocity[0] - 1.0) < 1e-6


def test_reset():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(1.0, 0.0, 0.0, t=1.0)
    assert vt.velocity is not None
    vt.reset()
    assert vt.velocity is None
    assert vt.speed_km_s == 0.0


def test_none_coords_ignored():
    vt = VelocityTracker()
    vt.add_sample(None, None, None, t=0.0)
    assert vt.velocity is None


# ---- calculate_velocity_bearing ----

def test_bearing_no_velocity():
    assert calculate_velocity_bearing(
        None, {"x": 0, "y": 0, "z": 0}, {"x": 1, "y": 0, "z": 0}
    ) is None


def test_bearing_no_target():
    assert calculate_velocity_bearing(
        (1, 0, 0), {"x": 0, "y": 0, "z": 0}, None
    ) is None


def test_bearing_aligned():
    # On va vers +Y, cible aussi à +Y → offsets nuls
    yaw, pitch = calculate_velocity_bearing(
        (0, 1, 0), {"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 10, "z": 0}
    )
    assert abs(yaw) < 1e-6
    assert abs(pitch) < 1e-6


def test_bearing_target_to_right():
    # On va vers +Y, cible à +X → cible à droite (yaw_off positif)
    yaw, _ = calculate_velocity_bearing(
        (0, 1, 0), {"x": 0, "y": 0, "z": 0}, {"x": 10, "y": 0, "z": 0}
    )
    assert abs(yaw - 90.0) < 1e-6


def test_bearing_target_to_left():
    # We move toward +Y, target at -X → target to left (negative yaw_off)
    yaw, _ = calculate_velocity_bearing(
        (0, 1, 0), {"x": 0, "y": 0, "z": 0}, {"x": -10, "y": 0, "z": 0}
    )
    assert abs(yaw + 90.0) < 1e-6


def test_bearing_target_above():
    # We move horizontally, target higher → positive pitch_off
    _, pitch = calculate_velocity_bearing(
        (0, 1, 0), {"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 10, "z": 10}
    )
    assert abs(pitch - 45.0) < 1e-6


def test_bearing_180_behind():
    # We move toward +Y, target at -Y (behind) → yaw_off ±180
    yaw, _ = calculate_velocity_bearing(
        (0, 1, 0), {"x": 0, "y": 0, "z": 0}, {"x": 0, "y": -10, "z": 0}
    )
    assert abs(abs(yaw) - 180.0) < 1e-6


def test_bearing_zero_velocity_returns_none():
    assert calculate_velocity_bearing(
        (0, 0, 0), {"x": 0, "y": 0, "z": 0}, {"x": 10, "y": 0, "z": 0}
    ) is None


def test_bearing_at_target():
    # Position = target → offsets 0
    yaw, pitch = calculate_velocity_bearing(
        (1, 0, 0), {"x": 5, "y": 5, "z": 5}, {"x": 5, "y": 5, "z": 5}
    )
    assert yaw == 0.0
    assert pitch == 0.0


# ---- calculate_absolute_bearing ----

from navigation import calculate_absolute_bearing, format_axis_delta


def test_absolute_bearing_returns_deltas():
    res = calculate_absolute_bearing(
        {"x": 0, "y": 0, "z": 0}, {"x": 50, "y": -100, "z": 5}
    )
    assert res["dx"] == 50
    assert res["dy"] == -100
    assert res["dz"] == 5
    # distance ≈ sqrt(2500 + 10000 + 25) ≈ 112.05
    assert abs(res["distance_km"] - math.sqrt(2500 + 10000 + 25)) < 1e-6


def test_absolute_bearing_yaw_pitch():
    # Target straight ahead +Y → yaw ≈ 0, target higher → positive pitch
    res = calculate_absolute_bearing(
        {"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 10, "z": 10}
    )
    assert abs(res["yaw_deg"]) < 1e-6
    assert abs(res["pitch_deg"] - 45.0) < 1e-6


def test_absolute_bearing_target_to_right():
    # Target at +X (right in atan2(dx, dy) convention) → yaw = +90°
    res = calculate_absolute_bearing(
        {"x": 0, "y": 0, "z": 0}, {"x": 10, "y": 0, "z": 0}
    )
    assert abs(res["yaw_deg"] - 90.0) < 1e-6


def test_absolute_bearing_no_position():
    assert calculate_absolute_bearing({"x": None}, {"x": 1, "y": 1, "z": 1}) is None


def test_absolute_bearing_no_target():
    assert calculate_absolute_bearing({"x": 0, "y": 0, "z": 0}, None) is None


def test_absolute_bearing_pure_vertical():
    # Target directly above → pitch = 90°
    res = calculate_absolute_bearing(
        {"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 0, "z": 5}
    )
    assert abs(res["pitch_deg"] - 90.0) < 1e-6


def test_format_axis_delta_small():
    assert format_axis_delta(5.4) == "+5.4"
    assert format_axis_delta(-3.2) == "-3.2"


def test_format_axis_delta_medium():
    assert format_axis_delta(50) == "+50"
    assert format_axis_delta(-512) == "-512"


def test_format_axis_delta_large():
    assert format_axis_delta(1500) == "+1.5k"
    assert format_axis_delta(-14000) == "-14.0k"


def test_format_axis_delta_none():
    assert format_axis_delta(None) == "?"
