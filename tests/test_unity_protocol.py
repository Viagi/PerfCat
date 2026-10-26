"""Protocol capture: profiler.raw is opened at the recording start."""
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
