"""Tests pour YawCalibrator."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from calibration import YawCalibrator


def _make_sample(world_yaw_deg, sign, offset):
    """Construit un sample synthétique cohérent avec (sign, offset)."""
    cam_yaw = sign * world_yaw_deg + offset
    # Ramène cam_yaw dans ]-180, +180]
    cam_yaw = ((cam_yaw + 180.0) % 360.0) - 180.0
    # Pour les positions : on veut atan2(dx, dy) = world_yaw_deg
    # avec dx² + dy² > 0 et speed > 0.5 km/s
    dx = math.sin(math.radians(world_yaw_deg)) * 10.0  # 10 km en 1 s = 10 km/s
    dy = math.cos(math.radians(world_yaw_deg)) * 10.0
    prev = {"x": 0.0, "y": 0.0, "z": 0.0}
    curr = {"x": dx, "y": dy, "z": 0.0}
    return prev, curr, 1.0, cam_yaw  # dt = 1 s


def test_initial_state():
    cal = YawCalibrator()
    assert cal.state == "uncalibrated"
    assert cal.result is None


def test_uncalibrated_no_movement():
    cal = YawCalibrator()
    # Pas de delta → ignoré
    cal.add_sample({"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 0, "z": 0}, 1.0, 0.0)
    assert cal.state == "uncalibrated"


def test_uncalibrated_low_speed():
    cal = YawCalibrator()
    # 0.1 km en 1 s = 0.1 km/s < seuil 0.5 → ignoré
    cal.add_sample({"x": 0, "y": 0, "z": 0}, {"x": 0.1, "y": 0, "z": 0}, 1.0, 0.0)
    assert cal.state == "uncalibrated"


def test_calibrating_not_enough_yaw_range():
    cal = YawCalibrator()
    # 6 samples mais tous au même cap → range = 0 → ne calibre pas
    for _ in range(6):
        prev, curr, dt, cam_yaw = _make_sample(0.0, 1, 0.0)
        cal.add_sample(prev, curr, dt, cam_yaw)
        cal.try_solve()
    assert cal.state == "calibrating"
    assert cal.result is None


def test_calibrate_sign_plus_offset_zero():
    cal = YawCalibrator()
    # sign=+1, offset=0 : cam_yaw = world_yaw directement
    yaws = [-90.0, -45.0, 0.0, 45.0, 90.0, 135.0, -135.0]  # range > 90°
    for w in yaws:
        prev, curr, dt, cam_yaw = _make_sample(w, 1, 0.0)
        cal.add_sample(prev, curr, dt, cam_yaw)
    cal.try_solve()
    assert cal.state == "calibrated"
    sign, offset = cal.result
    assert sign == 1
    assert abs(offset) < 2.0


def test_calibrate_sign_minus_offset_30():
    cal = YawCalibrator()
    yaws = [-90.0, -45.0, 0.0, 45.0, 90.0, 135.0, -135.0]
    for w in yaws:
        prev, curr, dt, cam_yaw = _make_sample(w, -1, -30.0)
        cal.add_sample(prev, curr, dt, cam_yaw)
    cal.try_solve()
    assert cal.state == "calibrated"
    sign, offset = cal.result
    assert sign == -1
    assert abs(offset - (-30.0)) < 2.0


def test_calibrate_sign_plus_offset_135():
    cal = YawCalibrator()
    yaws = [-90.0, -45.0, 0.0, 45.0, 90.0, 135.0, -135.0]
    for w in yaws:
        prev, curr, dt, cam_yaw = _make_sample(w, 1, 135.0)
        cal.add_sample(prev, curr, dt, cam_yaw)
    cal.try_solve()
    assert cal.state == "calibrated"
    sign, offset = cal.result
    assert sign == 1
    # offset peut wrap autour ±180 ; on compare en angle signé
    diff = ((offset - 135.0 + 180.0) % 360.0) - 180.0
    assert abs(diff) < 2.0


def test_reset():
    cal = YawCalibrator()
    yaws = [-90.0, -45.0, 0.0, 45.0, 90.0, 135.0, -135.0]
    for w in yaws:
        prev, curr, dt, cam_yaw = _make_sample(w, 1, 0.0)
        cal.add_sample(prev, curr, dt, cam_yaw)
    cal.try_solve()
    assert cal.state == "calibrated"
    cal.reset()
    assert cal.state == "uncalibrated"
    assert cal.result is None


def test_does_not_collect_after_calibrated():
    cal = YawCalibrator()
    yaws = [-90.0, -45.0, 0.0, 45.0, 90.0, 135.0, -135.0]
    for w in yaws:
        prev, curr, dt, cam_yaw = _make_sample(w, 1, 0.0)
        cal.add_sample(prev, curr, dt, cam_yaw)
    cal.try_solve()
    assert cal.state == "calibrated"
    n_before = len(cal._samples)
    # Nouveau sample : ne devrait pas être ajouté
    prev, curr, dt, cam_yaw = _make_sample(60.0, 1, 0.0)
    cal.add_sample(prev, curr, dt, cam_yaw)
    assert len(cal._samples) == n_before
