"""Tests for the navigation telemetry recorder."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from telemetry import TelemetryRecorder, create_session_recorder


def _read_lines(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_records_jsonl_round_trip(tmp_path):
    path = tmp_path / "t.jsonl"
    rec = TelemetryRecorder(path)
    assert rec.enabled
    rec.record(t_capture=1.0, status="ok", x=10.0, y=-5.0, z=2.0)
    rec.record(t_capture=1.2, status="no_read", reason="no_coords")
    rec.close()

    rows = _read_lines(path)
    assert len(rows) == 2
    assert rows[0]["status"] == "ok"
    assert rows[0]["x"] == 10.0
    assert rows[1]["reason"] == "no_coords"
    # t_wall is auto-added when not provided.
    assert "t_wall" in rows[0]


def test_record_keeps_explicit_t_wall(tmp_path):
    rec = TelemetryRecorder(tmp_path / "t.jsonl")
    rec.record(status="ok", t_wall=123.0)
    rec.close()
    assert _read_lines(tmp_path / "t.jsonl")[0]["t_wall"] == 123.0


def test_close_is_idempotent_and_blocks_further_writes(tmp_path):
    path = tmp_path / "t.jsonl"
    rec = TelemetryRecorder(path)
    rec.record(status="ok")
    rec.close()
    rec.close()  # must not raise
    assert not rec.enabled
    rec.record(status="ok")  # no-op after close
    assert len(_read_lines(path)) == 1


def test_unopenable_path_behaves_like_disabled(tmp_path):
    # Parent directory does not exist → open() fails → recorder disabled.
    rec = TelemetryRecorder(tmp_path / "missing_dir" / "t.jsonl")
    assert not rec.enabled
    rec.record(status="ok")  # must not raise
    rec.close()


def test_factory_disabled_returns_none_and_writes_nothing(tmp_path):
    logs = tmp_path / "logs"
    assert create_session_recorder(False, logs) is None
    assert not logs.exists()


def test_factory_enabled_creates_session_file(tmp_path):
    logs = tmp_path / "logs"
    rec = create_session_recorder(True, logs)
    assert rec is not None and rec.enabled
    rec.record(status="ok")
    rec.close()

    files = list(logs.glob("telemetry-*.jsonl"))
    assert len(files) == 1
    assert len(_read_lines(files[0])) == 1
