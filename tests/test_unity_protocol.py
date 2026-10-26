"""Protocol capture: profiler.raw is opened at the recording start."""
import pytest
from unittest.mock import patch

from perfcattle.unity.profiles import FEATURE_PROFILER_STREAM
from perfcattle.unity.protocol import UnityProtocolSession


class FakeProfile:
    label = "Unity 2022.3"

    @staticmethod
    def supports(feature):
        return feature == FEATURE_PROFILER_STREAM

    @staticmethod
    def message(area, name):
        return "0" * 32


def _session():
    session = UnityProtocolSession("device", "com.example.game")
    session._running = True
    session.deep_profiling = True
    session.areas = ["CPU", "GPU"]
    session.profile = FakeProfile()
    return session


def test_begin_record_restarts_capture_into_a_raw_file():
    session = _session()
    with patch.object(session, "stop_profiler") as stop, patch.object(
        session, "start_profiler"
    ) as start:
        session.begin_record("/tmp/profiler.raw")

    stop.assert_called_once_with()
    start.assert_called_once_with(
        "/tmp/profiler.raw", deep_profiling=True, areas=["CPU", "GPU"]
    )
    # A fresh parser is needed because the restarted stream has its own header.
    assert session.stats_parser.snapshot()["available"] is False


def test_begin_record_is_a_noop_when_already_recording():
    session = _session()
    session._raw_file = object()
    with patch.object(session, "stop_profiler") as stop, patch.object(
        session, "start_profiler"
    ) as start:
        session.begin_record("/tmp/profiler.raw")

    stop.assert_not_called()
    start.assert_not_called()


def test_begin_record_works_without_manually_assigned_areas():
    """Regression: the session must own ``areas``.

    ``begin_record`` reads ``self.areas``; it used to be defined only on the
    backend, so a real attach-then-record raised AttributeError after the
    stream had already been stopped, blanking the live panel forever.
    """
    session = UnityProtocolSession("device", "com.example.game")
    session._running = True
    session.profile = FakeProfile()

    with patch.object(session, "stop_profiler") as stop, patch.object(
        session, "start_profiler"
    ) as start:
        session.begin_record("/tmp/profiler.raw")

    stop.assert_called_once_with()
    start.assert_called_once_with(
        "/tmp/profiler.raw", deep_profiling=False, areas=None
    )


def test_set_areas_is_stored_on_the_session():
    profile = FakeProfile()
    profile.normalize_areas = staticmethod(lambda areas: list(areas))
    profile.categories_for_areas = staticmethod(lambda areas: ["Scripts"])
    profile.supports = staticmethod(lambda feature: True)

    session = UnityProtocolSession("device", "com.example.game")
    session.profile = profile
    with patch.object(session, "_send"):
        session.set_areas(["CPU", "GPU"])

    assert session.areas == ["CPU", "GPU"]


def test_attach_streams_without_opening_a_raw_file(tmp_path):
    session = _session()
    raw_path = tmp_path / "profiler.raw"
    with patch.object(session, "set_deep_profiling"), patch.object(
        session, "set_areas"
    ), patch.object(session, "_send"), patch(
        "perfcattle.unity.protocol.threading.Thread"
    ) as thread:
        session.start_profiler(None, deep_profiling=False, areas=None)

    assert session.raw_path is None
    assert session._raw_file is None
    assert session.raw_bytes == 0
    assert not raw_path.exists()
    # The reader starts even without a file so the panel gets counters.
    thread.return_value.start.assert_called_once_with()


def test_stats_snapshot_keeps_last_values_while_restarting():
    """Rebuilding the stream must not blank the live panel."""
    session = _session()
    session._last_snapshot = {
        "available": True,
        "values": {"cpu_frame_ms": 5.0},
        "frame": 10,
    }

    snapshot = session.stats_snapshot()

    assert snapshot["available"] is True
    assert snapshot["values"] == {"cpu_frame_ms": 5.0}
    assert snapshot["stale"] is True
    # The fresh parser still owns the real (empty) state.
    assert session.stats_parser.snapshot()["available"] is False


def test_wait_stream_idle_returns_once_data_stops():
    import time as _time

    session = _session()
    session._stream_bytes = 100
    started = _time.monotonic()
    assert session._wait_stream_idle(idle_seconds=0.05, timeout=1.0) is True
    assert _time.monotonic() - started < 0.6


def test_start_profiler_resets_integrity_state(tmp_path):
    """A new stream must not inherit a previous truncation flag."""
    session = _session()
    session._truncated = True
    session._error = "frame payload truncated"
    session._last_snapshot = {"available": True, "values": {}, "frame": 1}

    with patch.object(session, "set_deep_profiling"), patch.object(
        session, "set_areas"
    ), patch.object(session, "_send"), patch(
        "perfcattle.unity.protocol.threading.Thread"
    ):
        session.start_profiler(str(tmp_path / "profiler.raw"))

    assert session.raw_complete is True
    assert session.error is None
    assert session._last_snapshot is None


def test_start_profiler_refuses_a_second_reader(tmp_path):
    from perfcattle.unity.protocol import UnityProtocolError

    session = _session()
    alive = type("T", (), {"is_alive": lambda self: True})()
    session._reader = alive

    with pytest.raises(UnityProtocolError, match="reader is still running"):
        session.start_profiler(str(tmp_path / "profiler.raw"))
