"""Tests for the [Kalman] tuning knobs: ConfigManager getters/setters and the
VelocityTracker constructor parameters they feed.

Covers A8b: the constant-velocity Kalman/OCR tuning constants are now exposed
through config.ini ([Kalman]) and the Options dialog, instead of being hardcoded
module-level. We verify documented defaults, parse/round-trip, clamping of
garbage/out-of-range input, and that VelocityTracker honours the constructor
overrides.
"""
import configparser
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config_manager import ConfigManager
from velocity_tracker import VelocityTracker
import app_paths


# Documented defaults (mirror src/velocity_tracker.py class constants).
_DEFAULTS = {
    "max_speed_km_s": 2.5,
    "sigma_a": 1.0,
    "sigma_z": 0.02,
    "gate_nis": 30.0,
    "coast_s": 2.0,
}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A ConfigManager with an empty in-memory config (no [Kalman] section).

    Isolate paths before construction: initialization may migrate and save the
    configuration, even before an explicit setter or save is called.
    """
    monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
    monkeypatch.setattr(app_paths, "user_data_dir", lambda: tmp_path)
    cm = ConfigManager()
    cm.config = configparser.ConfigParser()
    return cm


# ---- ConfigManager: defaults when the section/keys are absent ----

def test_defaults_when_section_absent(cfg):
    assert cfg.get_kalman_max_speed_km_s() == _DEFAULTS["max_speed_km_s"]
    assert cfg.get_kalman_sigma_a() == _DEFAULTS["sigma_a"]
    assert cfg.get_kalman_sigma_z() == _DEFAULTS["sigma_z"]
    assert cfg.get_kalman_gate_nis() == _DEFAULTS["gate_nis"]
    assert cfg.get_kalman_coast_s() == _DEFAULTS["coast_s"]


# ---- ConfigManager: set -> get round-trip ----

def test_set_get_roundtrip(cfg):
    cfg.set_kalman_max_speed_km_s(3.0)
    cfg.set_kalman_sigma_a(2.0)
    cfg.set_kalman_sigma_z(0.05)
    cfg.set_kalman_gate_nis(50.0)
    cfg.set_kalman_coast_s(4.0)

    assert cfg.get_kalman_max_speed_km_s() == 3.0
    assert cfg.get_kalman_sigma_a() == 2.0
    assert cfg.get_kalman_sigma_z() == 0.05
    assert cfg.get_kalman_gate_nis() == 50.0
    assert cfg.get_kalman_coast_s() == 4.0


def test_parses_values_written_directly(cfg):
    cfg.config.add_section("Kalman")
    cfg.config.set("Kalman", "max_speed_km_s", "1.8")
    cfg.config.set("Kalman", "sigma_a", "0.5")
    cfg.config.set("Kalman", "sigma_z", "0.03")
    cfg.config.set("Kalman", "gate_nis", "12.5")
    cfg.config.set("Kalman", "coast_s", "1.5")

    assert cfg.get_kalman_max_speed_km_s() == 1.8
    assert cfg.get_kalman_sigma_a() == 0.5
    assert cfg.get_kalman_sigma_z() == 0.03
    assert cfg.get_kalman_gate_nis() == 12.5
    assert cfg.get_kalman_coast_s() == 1.5


# ---- ConfigManager: clamping of out-of-range and garbage input ----

def test_clamps_too_high(cfg):
    cfg.config.add_section("Kalman")
    cfg.config.set("Kalman", "max_speed_km_s", "9999")
    cfg.config.set("Kalman", "sigma_a", "9999")
    cfg.config.set("Kalman", "sigma_z", "9999")
    cfg.config.set("Kalman", "gate_nis", "1e12")
    cfg.config.set("Kalman", "coast_s", "9999")

    assert cfg.get_kalman_max_speed_km_s() == 50.0
    assert cfg.get_kalman_sigma_a() == 100.0
    assert cfg.get_kalman_sigma_z() == 10.0
    assert cfg.get_kalman_gate_nis() == 100000.0
    assert cfg.get_kalman_coast_s() == 30.0


def test_clamps_too_low(cfg):
    cfg.config.add_section("Kalman")
    cfg.config.set("Kalman", "max_speed_km_s", "-5")
    cfg.config.set("Kalman", "sigma_a", "0")
    cfg.config.set("Kalman", "sigma_z", "0")
    cfg.config.set("Kalman", "gate_nis", "-1")
    cfg.config.set("Kalman", "coast_s", "-3")

    assert cfg.get_kalman_max_speed_km_s() == 0.1
    assert cfg.get_kalman_sigma_a() == 0.001
    assert cfg.get_kalman_sigma_z() == 0.0001
    assert cfg.get_kalman_gate_nis() == 0.1
    assert cfg.get_kalman_coast_s() == 0.0


def test_garbage_falls_back_to_default(cfg):
    cfg.config.add_section("Kalman")
    for key in _DEFAULTS:
        cfg.config.set("Kalman", key, "not-a-number")

    assert cfg.get_kalman_max_speed_km_s() == _DEFAULTS["max_speed_km_s"]
    assert cfg.get_kalman_sigma_a() == _DEFAULTS["sigma_a"]
    assert cfg.get_kalman_sigma_z() == _DEFAULTS["sigma_z"]
    assert cfg.get_kalman_gate_nis() == _DEFAULTS["gate_nis"]
    assert cfg.get_kalman_coast_s() == _DEFAULTS["coast_s"]


def test_setters_also_clamp(cfg):
    cfg.set_kalman_max_speed_km_s(10000)
    cfg.set_kalman_coast_s(-10)
    assert cfg.get_kalman_max_speed_km_s() == 50.0
    assert cfg.get_kalman_coast_s() == 0.0


# ---- VelocityTracker: constructor accepts and uses the new params ----

def test_tracker_accepts_new_params():
    vt = VelocityTracker(
        sigma_a=2.0, sigma_z=0.05, gate_nis=50.0,
        coast_s=4.0, max_speed_km_s=3.0,
    )
    assert vt.MAX_SPEED_KM_S == 3.0
    assert vt.GATE_NIS == 50.0
    assert vt.COAST_S == 4.0


def test_defaults_match_class_constants():
    vt = VelocityTracker()
    assert vt.MAX_SPEED_KM_S == _DEFAULTS["max_speed_km_s"]
    assert vt.GATE_NIS == _DEFAULTS["gate_nis"]
    assert vt.COAST_S == _DEFAULTS["coast_s"]


def test_custom_max_speed_clamps_velocity():
    """A low MAX_SPEED_KM_S must clamp the estimated 3D speed below it."""
    cap = 0.5  # km/s
    vt = VelocityTracker(max_speed_km_s=cap)
    # Feed a fast, steady 1 km/s motion along x: without the cap the filter
    # would converge to ~1 km/s; with the cap it must stay <= 0.5 km/s.
    t = 0.0
    for i in range(10):
        vt.add_sample(float(i), 0.0, 0.0, t=t)
        t += 1.0
    assert vt.speed_km_s <= cap + 1e-9
    # And the default-capped tracker is allowed to exceed that same speed.
    vt_default = VelocityTracker()
    t = 0.0
    for i in range(10):
        vt_default.add_sample(float(i), 0.0, 0.0, t=t)
        t += 1.0
    assert vt_default.speed_km_s > cap


def test_custom_coast_s_changes_integrity_window():
    """COAST_S sets the boundary between COASTING and LOST integrity."""
    short = VelocityTracker(coast_s=0.5)
    short.add_sample(0.0, 0.0, 0.0, t=0.0)
    short.add_sample(1.0, 0.0, 0.0, t=1.0)
    # Coast 0.4 s past the last accepted read: still within the 0.5 s window.
    short.predict_only(1.4)
    assert short.integrity == "COASTING"
    # Coast past 0.5 s: now LOST.
    short.predict_only(1.6)
    assert short.integrity == "LOST"

    # A longer window keeps the same age COASTING.
    long = VelocityTracker(coast_s=2.0)
    long.add_sample(0.0, 0.0, 0.0, t=0.0)
    long.add_sample(1.0, 0.0, 0.0, t=1.0)
    long.predict_only(1.6)
    assert long.integrity == "COASTING"


def test_custom_gate_nis_rejects_more_aggressively():
    """A tight gate rejects a jump that the default gate accepts.

    Choose a jump whose 3-DOF NIS lands between the two gates so the only
    difference in outcome is the configured ``gate_nis`` threshold.
    """
    jump = 5.0  # km — large enough to score a moderate NIS

    # Default gate (30) accepts this jump.
    default = VelocityTracker()
    default.add_sample(0.0, 0.0, 0.0, t=0.0)
    accepted_default = default.add_sample(jump, 0.0, 0.0, t=1.0)
    assert accepted_default is True

    # A tight gate (1.0) rejects the same jump as an OCR misread.
    tight = VelocityTracker(gate_nis=1.0)
    tight.add_sample(0.0, 0.0, 0.0, t=0.0)
    accepted_tight = tight.add_sample(jump, 0.0, 0.0, t=1.0)
    assert accepted_tight is False
