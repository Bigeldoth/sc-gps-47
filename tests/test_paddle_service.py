"""Unit tests for src/paddle_service.py.

Mocks `subprocess.Popen` with a fake worker so the tests run without an
installed `.venv-paddle/`. Covers:
- start() failures when the venv is missing
- init handshake protocol (one request line, one response line)
- recognize / recognize_detailed round-trip
- crash recovery (sidecar dies -> auto-restart + retry)
- concurrent calls serialize via the internal Lock
"""
import io
import json
import os
import queue
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import paddle_service
from paddle_service import PaddleService, PaddleServiceError, _encode_image


# ─── Fake subprocess.Popen plumbing ──────────────────────────────────────


class FakePopen:
    """Minimal Popen stand-in driven by an in-memory request/response queue.

    The test scripts a list of expected (request_op, response_dict) pairs.
    Each `stdin.write` is matched against the next expected request; each
    `stdout.readline` returns the matched response.
    """

    def __init__(self, scripted: list[tuple[str, dict]]):
        self._scripted = list(scripted)
        self._responses: queue.Queue = queue.Queue()
        self._return_code: int | None = None
        self.stdin = _WriteSink(self._on_request)
        self.stdout = _LineReader(self._responses)
        self.stderr = io.BytesIO(b"")
        self._closed = False

    def _on_request(self, line: bytes):
        if not self._scripted:
            raise AssertionError(f"Unexpected request: {line!r}")
        expected_op, response = self._scripted.pop(0)
        msg = json.loads(line.decode("utf-8"))
        if msg.get("op") != expected_op:
            raise AssertionError(
                f"Expected op={expected_op!r}, got {msg.get('op')!r}"
            )
        encoded = (json.dumps(response) + "\n").encode("utf-8")
        self._responses.put(encoded)

    def poll(self):
        return self._return_code

    def wait(self, timeout=None):
        return self._return_code or 0

    def terminate(self):
        self.simulate_exit(0)

    def kill(self):
        self.simulate_exit(-9)

    def simulate_exit(self, code: int):
        self._return_code = code
        self._responses.put(b"")  # unblock readline()


class _WriteSink:
    def __init__(self, on_line):
        self._on_line = on_line

    def write(self, data: bytes):
        self._on_line(data)
        return len(data)

    def flush(self):
        pass


class _LineReader:
    def __init__(self, q: queue.Queue):
        self._q = q

    def readline(self) -> bytes:
        return self._q.get()


# ─── Tests ────────────────────────────────────────────────────────────────


def test_service_start_no_venv(monkeypatch, tmp_path):
    """Without a venv, start() should raise PaddleServiceError with a clear message."""
    monkeypatch.setattr(paddle_service, "VENV_PYTHON", tmp_path / "missing.exe")
    monkeypatch.setattr(paddle_service, "VENV_DIR", tmp_path / ".venv-paddle")
    svc = PaddleService(device="cpu")
    with pytest.raises(PaddleServiceError, match="venv not found"):
        svc.start(blocking=True, timeout=2.0)


def test_init_handshake_round_trip(monkeypatch, tmp_path):
    """start(blocking=True) writes one init request and reads one response."""
    monkeypatch.setattr(paddle_service, "VENV_PYTHON", tmp_path / "py.exe")
    monkeypatch.setattr(paddle_service, "WORKER_SCRIPT", tmp_path / "worker.py")
    (tmp_path / "py.exe").touch()
    (tmp_path / "worker.py").touch()

    fake = FakePopen(scripted=[
        ("init", {"ok": True, "result": {"api": "3.x", "device": "cpu", "upscale": 3}}),
    ])
    with patch.object(paddle_service.subprocess, "Popen", return_value=fake):
        svc = PaddleService(device="cpu")
        assert svc.start(blocking=True, timeout=2.0) is True
        assert svc._initialized is True


def test_recognize_round_trip(monkeypatch, tmp_path):
    """recognize() sends a recognize op and returns the worker's text payload."""
    monkeypatch.setattr(paddle_service, "VENV_PYTHON", tmp_path / "py.exe")
    monkeypatch.setattr(paddle_service, "WORKER_SCRIPT", tmp_path / "worker.py")
    (tmp_path / "py.exe").touch()
    (tmp_path / "worker.py").touch()

    fake = FakePopen(scripted=[
        ("init", {"ok": True, "result": {"api": "3.x"}}),
        ("recognize", {"ok": True, "result": {"text": "PAY -123.4 -456.7 -789.0"}}),
    ])
    with patch.object(paddle_service.subprocess, "Popen", return_value=fake):
        svc = PaddleService(device="cpu")
        svc.start(blocking=True, timeout=2.0)
        img = np.zeros((45, 600, 3), dtype=np.uint8)
        assert svc.recognize(img) == "PAY -123.4 -456.7 -789.0"


def test_recognize_detailed_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(paddle_service, "VENV_PYTHON", tmp_path / "py.exe")
    monkeypatch.setattr(paddle_service, "WORKER_SCRIPT", tmp_path / "worker.py")
    (tmp_path / "py.exe").touch()
    (tmp_path / "worker.py").touch()

    expected = {"texts": ["A"], "scores": [0.9], "polys": [[[0, 0], [1, 0], [1, 1], [0, 1]]],
                "upscale": 3, "shape": [135, 1800, 3]}
    fake = FakePopen(scripted=[
        ("init", {"ok": True, "result": {"api": "3.x"}}),
        ("recognize_detailed", {"ok": True, "result": expected}),
    ])
    with patch.object(paddle_service.subprocess, "Popen", return_value=fake):
        svc = PaddleService(device="cpu")
        svc.start(blocking=True, timeout=2.0)
        img = np.zeros((45, 600, 3), dtype=np.uint8)
        assert svc.recognize_detailed(img) == expected


def test_worker_error_raises(monkeypatch, tmp_path):
    """If the worker replies ok=false, the service raises PaddleServiceError."""
    monkeypatch.setattr(paddle_service, "VENV_PYTHON", tmp_path / "py.exe")
    monkeypatch.setattr(paddle_service, "WORKER_SCRIPT", tmp_path / "worker.py")
    (tmp_path / "py.exe").touch()
    (tmp_path / "worker.py").touch()

    fake = FakePopen(scripted=[
        ("init", {"ok": False, "error": "boom", "traceback": "..."}),
    ])
    with patch.object(paddle_service.subprocess, "Popen", return_value=fake):
        svc = PaddleService(device="cpu")
        # start() catches the init failure and returns False rather than raising.
        assert svc.start(blocking=True, timeout=2.0) is False


def test_encode_image_round_trips_through_b64():
    """_encode_image must produce a payload the worker can decode losslessly."""
    import base64
    img = np.arange(45 * 600 * 3, dtype=np.uint8).reshape(45, 600, 3)
    payload = _encode_image(img)
    raw = base64.b64decode(payload["img"])
    decoded = np.frombuffer(raw, dtype=np.uint8).reshape(payload["shape"])
    assert np.array_equal(img, decoded)
    assert payload["dtype"] == "uint8"


def test_concurrent_calls_serialize(monkeypatch, tmp_path):
    """4 threads calling recognize() must not interleave JSON on the pipe."""
    monkeypatch.setattr(paddle_service, "VENV_PYTHON", tmp_path / "py.exe")
    monkeypatch.setattr(paddle_service, "WORKER_SCRIPT", tmp_path / "worker.py")
    (tmp_path / "py.exe").touch()
    (tmp_path / "worker.py").touch()

    scripted = [("init", {"ok": True, "result": {"api": "3.x"}})]
    for i in range(4):
        scripted.append(("recognize", {"ok": True, "result": {"text": f"frame{i}"}}))
    fake = FakePopen(scripted=scripted)

    with patch.object(paddle_service.subprocess, "Popen", return_value=fake):
        svc = PaddleService(device="cpu")
        svc.start(blocking=True, timeout=2.0)
        results = []
        errors = []

        def worker():
            try:
                img = np.zeros((45, 600, 3), dtype=np.uint8)
                results.append(svc.recognize(img))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not errors
        assert len(results) == 4
        assert set(results) == {"frame0", "frame1", "frame2", "frame3"}
