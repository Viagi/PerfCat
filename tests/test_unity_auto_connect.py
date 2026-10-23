"""Unity auto-connect: arm, retry, wait and disarm."""
import os
import time
from unittest.mock import patch

from perfcattle.unity import manager as manager_module
from perfcattle.unity.manager import UnityCapture


class FakeBackend:
    mode = "helper"

    def __init__(self):
        self.files = []

    def status(self):
        return {"metrics_supported": True, "metrics_reason": None}

    def stats(self):
        return {
            "available": True,
            "values": {"cpu_frame_ms": 16.0, "draw_calls": 300},
            "frame": 1,
        }

    def stop(self):
        return {"status": 1, "files": []}

    def close(self):
        return None


class FailingBackend:
    def __init__(self, *args, **kwargs):
        pass

    def start(self, *args, **kwargs):
        raise RuntimeError("player not running")

    def close(self):
        return None


def test_arm_retries_until_start_succeeds():
    capture = UnityCapture()
    capture.AUTO_CONNECT_INTERVAL = 0.02
    capture.AUTO_CONNECT_MAX_INTERVAL = 0.02
    calls = {"count": 0}
    adopt = capture._adopt

    def fake_start(
        device, pkg, pid=None, deep_profiling=False, areas=None, quiet=False,
        owner=None, record=False
    ):
        calls["count"] += 1
        if calls["count"] < 3:
            capture.last_error = "player not running"
            return {"status": 1, "available": False, "msg": "player not running"}
        with capture.lock:
            capture.device = device
            capture.pkg = pkg
            capture.pid = pid
            return adopt(FakeBackend(), {"unity_version": "2022.3"}, owner=owner)

    try:
        with patch.object(capture, "start", side_effect=fake_start):
            capture.arm("device", "com.example.game", pid="1")
            result = capture.wait_connected(2.0)
        assert result["available"] is True
        assert calls["count"] >= 3
        assert capture._connected.is_set()
    finally:
        capture._cancel_auto()


def test_wait_connected_returns_last_error_after_timeout():
    capture = UnityCapture()
    capture.last_error = "PerfCattleCapture helper not found"
    try:
        result = capture.wait_connected(0.05)
        assert result["available"] is False
        assert "helper not found" in result["msg"]
    finally:
        capture._cancel_auto()


def test_disarm_stops_retry_loop():
    capture = UnityCapture()
    capture.AUTO_CONNECT_INTERVAL = 0.02
    capture.AUTO_CONNECT_MAX_INTERVAL = 0.02

    def failing_start(*args, **kwargs):
        capture.last_error = "not running"
        return {"status": 1, "available": False, "msg": "not running"}

    try:
        with patch.object(capture, "start", side_effect=failing_start) as start:
            capture.arm("device", "com.example.game")
            time.sleep(0.08)
            capture.disarm()
            count_after_disarm = start.call_count
            time.sleep(0.1)
        # At most one attempt may already be in flight when disarm happens.
        assert start.call_count <= count_after_disarm + 1
        assert capture._auto_target is None
        assert capture.status()["arming"] is False
    finally:
        capture._cancel_auto()


def test_failed_start_removes_staging_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(manager_module, "HelperBackend", FailingBackend)
    monkeypatch.setattr(manager_module, "ProtocolBackend", FailingBackend)

    capture = UnityCapture()
    result = capture.start("device", "com.example.game")

    assert result["available"] is False
    assert "player not running" in result["msg"]
    assert os.listdir(tmp_path / "report") == []


def test_discard_stop_removes_unarchived_capture(tmp_path):
    staging = tmp_path / "report" / "unity_test"
    staging.mkdir(parents=True)

    class DiscardBackend(FakeBackend):
        def stop(self, discard=False):
            assert discard is True
            return {"status": 1, "files": []}

    capture = UnityCapture()
    capture.device = "device"
    capture.pkg = "com.example.game"
    capture.pid = "1"
    capture.staging_dir = str(staging)
    capture._adopt(DiscardBackend(), {"unity_version": "2022.3"})

    capture.stop(discard=True)

    assert not staging.exists()
    assert capture.staging_dir is None

