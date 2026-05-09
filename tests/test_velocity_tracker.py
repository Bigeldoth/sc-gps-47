"""Tests pour VelocityTracker et calculate_velocity_bearing."""
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
    # Avec un seul sample, pas de vélocité encore
    assert vt.velocity is None


def test_second_sample_basic_velocity():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(1.0, 0.0, 0.0, t=1.0)  # 1 km en 1 s = 1 km/s
    vx, vy, vz = vt.velocity
    assert abs(vx - 1.0) < 1e-6
    assert vy == 0.0
    assert vz == 0.0
    assert abs(vt.speed_km_s - 1.0) < 1e-6
    assert vt.is_moving is True


def test_diagonal_velocity():
    vt = VelocityTracker(smoothing_alpha=1.0)  # pas de lissage pour test simple
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(3.0, 4.0, 0.0, t=1.0)  # vitesse 5 km/s (3-4-5 triangle)
    assert abs(vt.speed_km_s - 5.0) < 1e-6


def test_stationary_below_threshold():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(0.001, 0.0, 0.0, t=1.0)  # 1 m/s = 0.001 km/s, sous 0.05
    assert vt.is_moving is False


def test_dt_too_large_resets():
    vt = VelocityTracker()
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(100.0, 0.0, 0.0, t=10.0)  # dt=10 > MAX_DT_S=3 → reset
    # Après un reset, un seul sample → vélocité None
    assert vt.velocity is None


def test_dt_zero_or_negative_ignored():
    vt = VelocityTracker(smoothing_alpha=1.0)
    vt.add_sample(0.0, 0.0, 0.0, t=1.0)
    vt.add_sample(1.0, 0.0, 0.0, t=2.0)
    v_before = vt.velocity
    vt.add_sample(2.0, 0.0, 0.0, t=2.0)  # même t → ignoré
    assert vt.velocity == v_before


def test_smoothing_ema():
    # alpha=0.5 : nouveau sample compte pour 50%
    vt = VelocityTracker(smoothing_alpha=0.5)
    vt.add_sample(0.0, 0.0, 0.0, t=0.0)
    vt.add_sample(2.0, 0.0, 0.0, t=1.0)  # vel inst = 2
    # Premier vel : pas lissé encore (initialisé direct)
    assert abs(vt.velocity[0] - 2.0) < 1e-6
    vt.add_sample(2.0, 0.0, 0.0, t=2.0)  # vel inst = 0, lissé = 0.5*0 + 0.5*2 = 1
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
    # On va vers +Y, cible à -X → cible à gauche (yaw_off négatif)
    yaw, _ = calculate_velocity_bearing(
        (0, 1, 0), {"x": 0, "y": 0, "z": 0}, {"x": -10, "y": 0, "z": 0}
    )
    assert abs(yaw + 90.0) < 1e-6


def test_bearing_target_above():
    # On va horizontal, cible plus haute → pitch_off positif
    _, pitch = calculate_velocity_bearing(
        (0, 1, 0), {"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 10, "z": 10}
    )
    assert abs(pitch - 45.0) < 1e-6


def test_bearing_180_behind():
    # On va vers +Y, cible à -Y (derrière) → yaw_off ±180
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
