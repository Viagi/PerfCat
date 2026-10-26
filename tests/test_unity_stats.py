"""Unity 游戏面板指标的解析与后端降级行为。"""
import json

from perfcattle.unity.backends import HelperBackend, ProtocolBackend


def make_helper():
    return HelperBackend("device", "com.example.game", "123", "/tmp", "/tmp")


def test_parse_stats_reply_returns_values():
    payload = {
        "available": True,
        "debug_build": True,
        "frame": 42,
        "values": {"cpu_frame_ms": 8.42, "draw_calls": 326},
        "missing": ["gpu_frame_ms"],
    }
    data = HelperBackend._parse_stats_reply("ok " + json.dumps(payload))
    assert data["available"] is True
    assert data["values"]["cpu_frame_ms"] == 8.42
    assert data["values"]["draw_calls"] == 326


def test_parse_stats_reply_detects_outdated_helper():
    data = HelperBackend._parse_stats_reply("err unknown_command")
    assert data == {"available": False, "reason": "helper_outdated"}


def test_parse_stats_reply_rejects_invalid_json():
    data = HelperBackend._parse_stats_reply("ok not-json")
    assert data == {"available": False, "reason": "invalid_stats_reply"}


def test_protocol_backend_parses_raw_stream():
    from tests.test_unity_raw_reader import build_stream

    backend = ProtocolBackend("device", "com.example.game", "123", "/tmp")
    backend.available = True
    backend.session.stats_parser.feed(build_stream())
    data = backend.stats()
    assert data["available"] is True
    assert data["values"]["draw_calls"] == 300
    assert data["values"]["triangles"] == 1000
    assert data["values"]["vertices"] == 1200


def test_protocol_backend_before_start_reports_not_running():
    backend = ProtocolBackend("device", "com.example.game", "123", "/tmp")
    data = backend.stats()
    assert data["available"] is False
    assert data["reason"] == "not_running"


def test_helper_stats_returns_not_running_before_start():
    data = make_helper().stats()
    assert data["available"] is False
    assert data["reason"] == "not_running"


def test_helper_stats_reads_command_reply(monkeypatch):
    backend = make_helper()
    backend.available = True
    backend.metrics_supported = True
    monkeypatch.setattr(backend, "_ensure_channel", lambda: None)
    payload = {"available": True, "values": {"cpu_frame_ms": 9.5}, "missing": []}
    monkeypatch.setattr(
        backend, "_command", lambda command, timeout=5: "ok " + json.dumps(payload)
    )
    data = backend.stats()
    assert data["available"] is True
    assert data["values"]["cpu_frame_ms"] == 9.5
    assert backend.last_stats == data


def test_stats_logger_does_not_write_stale_snapshots(tmp_path, monkeypatch):
    """A stale snapshot (stream restart) must not be logged as a new sample."""
    import json
    from perfcattle.unity import manager as unity_manager

    monkeypatch.setattr(
        unity_manager, "_report_dir", lambda: str(tmp_path), raising=False
    )
    capture = unity_manager.UnityCapture()
    capture._stats_available = True
    capture._stats_backend = object()
    capture._stats_mode = "protocol"
    capture._stats_version = "2022.3"

    calls = {"n": 0}

    def fake_stats():
        calls["n"] += 1
        return {"available": True, "stale": True, "values": {"draw_calls": 1},
                "frame": 1}

    monkeypatch.setattr(capture, "stats", fake_stats)
    monkeypatch.setattr(capture, "_stats_stop", _StopAfterFirstIteration())

    capture._stats_logger_loop()

    log = tmp_path / "unity_stats.log"
    assert not log.exists() or log.read_text(encoding="utf-8").strip() == ""


class _StopAfterFirstIteration:
    """Behaves like an Event that turns set after the first loop pass."""

    def __init__(self):
        self._checks = 0

    def is_set(self):
        self._checks += 1
        return self._checks > 1

    def wait(self, timeout):
        return True
