"""Tests pour normalize_angle_signed, ema_angle, calculate_relative_bearing."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from navigation import (
    normalize_angle_signed,
    ema_angle,
    calculate_relative_bearing,
)


# ---- normalize_angle_signed ----

def test_normalize_zero():
    assert normalize_angle_signed(0.0) == 0.0


def test_normalize_180():
    # Convention : on autorise +180 OU -180 selon implémentation ; on accepte +180.
    assert abs(normalize_angle_signed(180.0)) == 180.0


def test_normalize_181():
    assert normalize_angle_signed(181.0) == -179.0


def test_normalize_negative_181():
    assert normalize_angle_signed(-181.0) == 179.0


def test_normalize_360():
    assert normalize_angle_signed(360.0) == 0.0


def test_normalize_540():
    # 540 = 360 + 180
    assert abs(normalize_angle_signed(540.0)) == 180.0


def test_normalize_minus_540():
    assert abs(normalize_angle_signed(-540.0)) == 180.0


def test_normalize_none():
    assert normalize_angle_signed(None) is None


# ---- ema_angle ----

def test_ema_angle_init():
    # Sans prev, retourne new
    assert ema_angle(None, 42.0, 0.5) == 42.0


def test_ema_angle_simple():
    # 0 -> 100, alpha 0.5 → 50
    assert abs(ema_angle(0.0, 100.0, 0.5) - 50.0) < 1e-6


def test_ema_angle_wrap_positive():
    # prev=170, new=-170 (donc le chemin court est +20°), alpha 0.5 →
    # résultat ~180° (ou -180°), pas 0°
    result = ema_angle(170.0, -170.0, 0.5)
    # Le diff signé est -340 normalisé à +20, donc prev + 0.5 * 20 = 180
    assert abs(abs(result) - 180.0) < 1e-6


def test_ema_angle_wrap_small_alpha():
    # prev=170, new=-170, alpha 0.1 → résultat ~172° (pas 136° qu'on aurait
    # avec un EMA naïf sur les bruts)
    result = ema_angle(170.0, -170.0, 0.1)
    assert abs(result - 172.0) < 0.5


# ---- calculate_relative_bearing ----

def test_bearing_no_target():
    assert calculate_relative_bearing(
        {"x": 0, "y": 0, "z": 0}, {"yaw": 0, "pitch": 0}, None, (1, 0)
    ) is None


def test_bearing_no_camdir():
    assert calculate_relative_bearing(
        {"x": 0, "y": 0, "z": 0}, None, {"x": 1, "y": 0, "z": 0}, (1, 0)
    ) is None


def test_bearing_no_pos():
    assert calculate_relative_bearing(
        {"x": None, "y": None, "z": None}, {"yaw": 0, "pitch": 0},
        {"x": 1, "y": 0, "z": 0}, (1, 0)
    ) is None


def test_bearing_no_calib():
    assert calculate_relative_bearing(
        {"x": 0, "y": 0, "z": 0}, {"yaw": 0, "pitch": 0},
        {"x": 1, "y": 0, "z": 0}, None
    ) is None


def test_bearing_aligned():
    # Cible droit devant : avec la convention sign=+1 offset=0,
    # cible à dx=0, dy=10 → world_yaw = atan2(0,10) = 0, cam aligné à 0
    yaw, pitch = calculate_relative_bearing(
        {"x": 0, "y": 0, "z": 0},
        {"yaw": 0.0, "pitch": 0.0},
        {"x": 0, "y": 10, "z": 0},
        (1, 0.0),
    )
    assert abs(yaw) < 1e-6
    assert abs(pitch) < 1e-6


def test_bearing_target_to_right():
    # Cible à dx=10, dy=0 → world_yaw = atan2(10,0) = +90°
    # Cam à yaw=0, calib sign=+1 offset=0 → target_cam_yaw=+90, yaw_off=+90
    yaw, _ = calculate_relative_bearing(
        {"x": 0, "y": 0, "z": 0},
        {"yaw": 0.0, "pitch": 0.0},
        {"x": 10, "y": 0, "z": 0},
        (1, 0.0),
    )
    assert abs(yaw - 90.0) < 1e-6


def test_bearing_wrap_around():
    # Cam à 170°, cible à world_yaw = -170° (donc dx<0 dy<0 légèrement)
    # On choisit dx, dy tels que atan2(dx,dy) ≈ -170°
    # tan(-170°) signe : sin(-170°) ≈ -0.174 (négatif), cos(-170°) ≈ -0.985 (négatif)
    # atan2(dx, dy) = -170° → dx ≈ sin(-170°), dy ≈ cos(-170°)
    dx = math.sin(math.radians(-170.0))
    dy = math.cos(math.radians(-170.0))
    yaw, _ = calculate_relative_bearing(
        {"x": 0, "y": 0, "z": 0},
        {"yaw": 170.0, "pitch": 0.0},
        {"x": dx, "y": dy, "z": 0},
        (1, 0.0),
    )
    # target_cam_yaw = -170, cam = +170, diff = -340 → normalisé à +20
    assert abs(yaw - 20.0) < 1e-3


def test_bearing_pitch_up():
    # Cible plus haute (z=10) à dy=10 → target_world_pitch = atan2(10, 10) = +45°
    # Cam pitch=0 → pitch_off = +45
    _, pitch = calculate_relative_bearing(
        {"x": 0, "y": 0, "z": 0},
        {"yaw": 0.0, "pitch": 0.0},
        {"x": 0, "y": 10, "z": 10},
        (1, 0.0),
    )
    assert abs(pitch - 45.0) < 1e-6


def test_bearing_calib_sign_inverse():
    # Si calib dit sign=-1 offset=0, alors target_cam_yaw = -world_yaw
    # Cible dx=10, dy=0 → world_yaw=+90 → target_cam_yaw=-90
    # cam=0 → yaw_off = -90 - 0 = -90
    yaw, _ = calculate_relative_bearing(
        {"x": 0, "y": 0, "z": 0},
        {"yaw": 0.0, "pitch": 0.0},
        {"x": 10, "y": 0, "z": 0},
        (-1, 0.0),
    )
    assert abs(yaw + 90.0) < 1e-6


def test_bearing_calib_offset():
    # Si calib offset=+45 sign=+1, alors target_cam_yaw = world_yaw + 45
    # Cible droit devant en world (world_yaw=0) → target_cam_yaw=+45
    # Si cam est à +45, yaw_off = 0 (cam regarde dans la direction calibrée)
    yaw, _ = calculate_relative_bearing(
        {"x": 0, "y": 0, "z": 0},
        {"yaw": 45.0, "pitch": 0.0},
        {"x": 0, "y": 10, "z": 0},
        (1, 45.0),
    )
    assert abs(yaw) < 1e-6
