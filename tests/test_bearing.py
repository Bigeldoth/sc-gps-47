"""Tests for normalize_angle_signed and ema_angle.

`calculate_velocity_bearing` is covered by tests/test_velocity_tracker.py
under the current (velocity, current_pos, target) signature.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from navigation import (
    normalize_angle_signed,
    ema_angle,
)


# ---- normalize_angle_signed ----

def test_normalize_zero():
    assert normalize_angle_signed(0.0) == 0.0


def test_normalize_180():
    # Convention: allow +180 OR -180 depending on implementation; accept +180.
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
    # Without prev, return new
    assert ema_angle(None, 42.0, 0.5) == 42.0


def test_ema_angle_simple():
    # 0 -> 100, alpha 0.5 → 50
    assert abs(ema_angle(0.0, 100.0, 0.5) - 50.0) < 1e-6


def test_ema_angle_wrap_positive():
    # prev=170, new=-170 (short path is +20°), alpha 0.5 →
    # result ~180° (or -180°), not 0°
    result = ema_angle(170.0, -170.0, 0.5)
    # Signed diff is -340 normalized to +20, so prev + 0.5 * 20 = 180
    assert abs(abs(result) - 180.0) < 1e-6


def test_ema_angle_wrap_small_alpha():
    # prev=170, new=-170, alpha 0.1 → result ~172° (not 136° from naive EMA on raw values)
    result = ema_angle(170.0, -170.0, 0.1)
    assert abs(result - 172.0) < 0.5


