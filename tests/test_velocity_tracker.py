"""Tests for VelocityTracker and calculate_velocity_bearing."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from velocity_tracker import VelocityTracker
from navigation import calculate_velocity_bearing


# ---- VelocityTracker (constant-velocity Kalman) ----

def test_initial_state():
    vt = VelocityTracker()
    assert vt.velocity is None
    assert vt.speed_km_s == 0.0
    assert vt.is_moving is False
    assert vt.position is None
    assert vt.integrity == "LOST"


def test_first_sample_no_velocity():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    # Single sample: position seeded, no velocity yet.
    assert vt.velocity is None
    assert vt.position == (0.0, 0.0, 0.0)


def test_second_sample_basic_velocity():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(1.0, 0.0, 0.0, t=1.0)  # 1 km in 1 s ~= 1 km/s
    vx, vy, vz = vt.velocity
    # Bounded-init prior => the first velocity estimate is close to (slightly
    # damped vs) the finite difference; it converges over the next few samples.
    assert abs(vx - 1.0) < 0.1
    assert abs(vy) < 0.05
    assert abs(vz) < 0.05
    assert abs(vt.speed_km_s - 1.0) < 0.1
    assert vt.is_moving is True


def test_diagonal_velocity():
    # 3-4-5 ratio at a physical speed (0.5 km/s, below the MAX_SPEED clamp).
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(0.3, 0.4, 0.0, t=1.0)  # speed 0.5 km/s
    assert abs(vt.speed_km_s - 0.5) < 0.05


def test_velocity_converges_constant():
    # Clean constant-velocity track: 0.2 km per 0.4 s step = 0.5 km/s.
    vt = VelocityTracker()
    for i in range(8):
        vt.add_sample(0.2 * i, 0.0, 0.0, t=0.4 * i)
    vx, _, _ = vt.velocity
    assert abs(vx - 0.5) < 1e-3


def test_stationary_below_threshold():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(0.001, 0.0, 0.0, t=1.0)  # 0.001 km/s, below 0.05
    assert vt.is_moving is False


def test_moving_hysteresis():
    # Directly exercise the hysteresis state machine (enter at MIN, exit at EXIT).
    vt = VelocityTracker()
    vt._has_velocity = True
    vt._fx.v, vt._fy.v, vt._fz.v = 0.04, 0.0, 0.0  # below MIN(0.05) from rest
    vt._update_moving()
    assert vt.is_moving is False
    vt._fx.v = 0.20                                 # above MIN -> moving
    vt._update_moving()
    assert vt.is_moving is True
    vt._fx.v = 0.04                                 # between EXIT(0.03) and MIN
    vt._update_moving()
    assert vt.is_moving is True                     # hysteresis keeps it moving
    vt._fx.v = 0.01                                 # below EXIT -> stops
    vt._update_moving()
    assert vt.is_moving is False


def test_dt_too_large_resets():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(100.0, 0.0, 0.0, t=10.0)  # dt=10 > MAX_DT_S=3 -> reset
    assert vt.velocity is None  # single seed after reset


def test_dt_zero_or_negative_ignored():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=1.0)
    vt.add_sample(1.0, 0.0, 0.0, t=2.0)
    v_before = vt.velocity
    accepted = vt.add_sample(2.0, 0.0, 0.0, t=2.0)  # same t -> ignored
    assert accepted is False
    assert vt.velocity == v_before


def test_innovation_gate_rejects_outlier():
    # Clean slow track, then a gross misread (+50 km): must be rejected.
    vt = VelocityTracker()
    for i in range(5):
        vt.add_sample(0.1 * i, 0.0, 0.0, t=0.4 * i)
    v_before = vt.velocity
    accepted = vt.add_sample(50.0, 0.0, 0.0, t=0.4 * 5)
    assert accepted is False
    assert abs(vt.velocity[0] - v_before[0]) < 0.05  # outlier did not enter


def test_gate_widens_after_gap():
    # Within the coast window the covariance grows, so a larger-than-usual but
    # plausible continued move is accepted (the gate widens), not rejected.
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(0.2, 0.0, 0.0, t=0.4)   # ~0.5 km/s
    vt.predict_only(1.4)                  # 1.0 s dropout, still within COAST_S
    accepted = vt.add_sample(0.7, 0.0, 0.0, t=1.6)
    assert accepted is True


def test_speed_never_exceeds_physical_max():
    # A2.1 hardening: whatever the filter does on a pathological track, the
    # reported speed must stay within the physical ceiling (no 17 km/s lock-up).
    vt = VelocityTracker()
    t = 0.0
    p = 0.0
    for _ in range(15):
        p += 3.0          # 3 km per 0.4 s step = 7.5 km/s (impossible)
        t += 0.4
        vt.add_sample(p, 0.0, 0.0, t=t)
        assert vt.speed_km_s <= VelocityTracker.MAX_SPEED_KM_S + 1e-9


def test_reseed_on_lost_recovery():
    # A2.1 hardening: a fix that arrives after the coast window expired must
    # re-seed (velocity not carried over) rather than correcting a diverged
    # filter — this is what fixed the field velocity lock-up.
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(0.4, 0.0, 0.0, t=1.0)            # ~0.4 km/s, FRESH
    assert vt.velocity is not None
    vt.predict_only(1.0 + VelocityTracker.COAST_S + 0.5)  # coast past the window
    assert vt.integrity == "LOST"
    accepted = vt.add_sample(0.42, 0.0, 0.0, t=1.0 + VelocityTracker.COAST_S + 0.9)
    assert accepted is True
    assert vt.integrity == "FRESH"      # recovered
    assert vt.velocity is None          # re-seeded: velocity not yet re-built
    assert vt.speed_km_s == 0.0


def test_predict_only_dead_reckons():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(0.4, 0.0, 0.0, t=1.0)   # 0.4 km/s
    assert vt.integrity == "FRESH"
    vt.predict_only(1.5)                  # coast 0.5 s, no measurement
    px, _, _ = vt.position
    assert abs(px - 0.6) < 0.05           # 0.4 + 0.4*0.5
    assert vt.integrity == "COASTING"


def test_integrity_goes_lost():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(0.4, 0.0, 0.0, t=1.0)
    vt.predict_only(1.0 + VelocityTracker.COAST_S + 0.5)  # beyond coast window
    assert vt.integrity == "LOST"


def test_reset():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(1.0, 0.0, 0.0, t=1.0)
    assert vt.velocity is not None
    vt.reset()
    assert vt.velocity is None
    assert vt.speed_km_s == 0.0
    assert vt.position is None


def test_none_coords_ignored():
    vt = VelocityTracker()
    assert vt.add_sample(None, None, None, t=0.0) is False
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
    # SC: X- = right. Moving +Y, target at X- = -10 → target to the right → yaw_off = +90
    yaw, _ = calculate_velocity_bearing(
        (0, 1, 0), {"x": 0, "y": 0, "z": 0}, {"x": -10, "y": 0, "z": 0}
    )
    assert abs(yaw - 90.0) < 1e-6


def test_bearing_target_to_left():
    # SC: X+ = left. Moving +Y, target at X+ = +10 → target to the left → yaw_off = -90
    yaw, _ = calculate_velocity_bearing(
        (0, 1, 0), {"x": 0, "y": 0, "z": 0}, {"x": +10, "y": 0, "z": 0}
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
    # SC: X- = right. Target at X- = -10 → yaw = +90° (right)
    res = calculate_absolute_bearing(
        {"x": 0, "y": 0, "z": 0}, {"x": -10, "y": 0, "z": 0}
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
