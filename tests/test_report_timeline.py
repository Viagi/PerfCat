"""Shared report timeline: Unity and system charts must use one origin."""
import json
import sys
from unittest.mock import patch

from flask import Flask

from perfcattle.report.log_io import LogIO
import perfcattle.view.apm_api  # noqa: F401  (module is shadowed in the package)

apm_api_module = sys.modules["perfcattle.view.apm_api"]


def make_log(tmp_path):
    report = tmp_path / "report"
    report.mkdir()
    log = LogIO(fileroot=str(tmp_path))
    log.report_dir = str(report)
    return log, report


def test_session_origin_aligns_unity_and_system_charts(tmp_path):
    log, report = make_log(tmp_path)
    scene = report / "scene"
    scene.mkdir()
    (scene / "apm_session.json").write_text(
        json.dumps({"clock": "10:00:00.500000"}), encoding="utf-8"
    )
    (scene / "cpu_app.log").write_text(
        "10:00:01.000000=90\n10:00:02.000000=95\n", encoding="utf-8"
    )
    (scene / "unity_stats.log").write_text(
        '10:00:01.500000={"cpu_frame_ms":16.7,"draw_calls":320,"frame":100}\n'
        '10:00:02.500000={"cpu_frame_ms":17.1,"draw_calls":330,"frame":101}\n',
        encoding="utf-8",
    )

    cpu = log.getCpuLog("Android", "scene")["cpuAppData"]
    unity = log.getUnityStatsLog("Android", "scene")["charts"]

    # Both charts use the recording start as x=0, not each file's first sample.
    assert cpu == [{"x": 0.5, "y": 90}, {"x": 1.5, "y": 95}]
    assert unity["draw_calls"] == [{"x": 1.0, "y": 320}, {"x": 2.0, "y": 330}]
    assert log.scene_time_range("scene") == {"min": 0.0, "max": 2.0}


def test_unity_stats_fall_back_to_earliest_log_without_session_file(tmp_path):
    log, report = make_log(tmp_path)
    scene = report / "scene"
    scene.mkdir()
    (scene / "cpu_app.log").write_text("10:00:03=90\n", encoding="utf-8")
    (scene / "unity_stats.log").write_text(
        '10:00:04={"draw_calls":320,"frame":100}\n', encoding="utf-8"
    )

    unity = log.getUnityStatsLog("Android", "scene")["charts"]

    # The system log starts 1s before Unity; Unity's first frame is x=1.
    assert unity["draw_calls"] == [{"x": 1.0, "y": 320}]
    assert log.scene_time_range("scene")["max"] == 1.0


def test_session_origin_refreshes_when_scene_is_re_recorded(tmp_path):
    log, report = make_log(tmp_path)
    scene = report / "scene"
    scene.mkdir()
    (scene / "apm_session.json").write_text(
        json.dumps({"clock": "10:00:00.000000"}), encoding="utf-8"
    )
    (scene / "cpu_app.log").write_text(
        "10:00:01.000000=90\n10:00:02.000000=95\n", encoding="utf-8"
    )
    assert log.scene_origin("scene") == 36000.0
    assert log.scene_time_range("scene") == {"min": 0.0, "max": 2.0}

    # A new recording reuses the same scene name and replaces the logs.
    (scene / "apm_session.json").write_text(
        json.dumps({"clock": "12:00:00.000000"}), encoding="utf-8"
    )
    (scene / "cpu_app.log").write_text(
        "12:00:01.000000=80\n12:00:03.000000=85\n", encoding="utf-8"
    )

    # The cached origin from the previous recording must not leak through.
    assert log.scene_origin("scene") == 43200.0
    assert log.getCpuLog("Android", "scene")["cpuAppData"] == [
        {"x": 1.0, "y": 80},
        {"x": 3.0, "y": 85},
    ]
    assert log.scene_time_range("scene") == {"min": 0.0, "max": 3.0}


def test_scene_time_range_refreshes_while_logs_grow(tmp_path):
    log, report = make_log(tmp_path)
    scene = report / "scene"
    scene.mkdir()
    (scene / "apm_session.json").write_text(
        json.dumps({"clock": "10:00:00.000000"}), encoding="utf-8"
    )
    log_path = scene / "cpu_app.log"
    log_path.write_text("10:00:01.000000=90\n", encoding="utf-8")
    assert log.scene_time_range("scene")["max"] == 1.0

    with open(log_path, "a", encoding="utf-8") as file:
        file.write("10:00:05.000000=95\n")

    # Appended samples extend the shared x axis instead of freezing at 1s.
    assert log.scene_time_range("scene")["max"] == 5.0


def test_begin_session_writes_shared_origin(tmp_path):
    log, report = make_log(tmp_path)

    assert log.begin_session("10:00:00.250000") is True
    payload = json.loads((report / "apm_session.json").read_text(encoding="utf-8"))
    assert payload["clock"] == "10:00:00.250000"
    assert log.scene_origin(None) is None


def test_cross_midnight_samples_stay_on_one_timeline(tmp_path):
    log, report = make_log(tmp_path)
    scene = report / "scene"
    scene.mkdir()
    (scene / "apm_session.json").write_text(
        json.dumps({"clock": "23:59:59.000000"}), encoding="utf-8"
    )
    (scene / "cpu_app.log").write_text(
        "23:59:59.500000=90\n00:00:01.000000=95\n", encoding="utf-8"
    )

    cpu = log.getCpuLog("Android", "scene")["cpuAppData"]

    assert cpu == [{"x": 0.5, "y": 90}, {"x": 2.0, "y": 95}]


def test_initialize_records_shared_time_origin():
    app = Flask(__name__)
    app.register_blueprint(apm_api_module.apm_api)
    client = app.test_client()

    with patch.object(apm_api_module, "f") as report, patch.object(
        apm_api_module, "_clear_collector_sessions"
    ), patch.object(
        apm_api_module.IOSPerformance, "stop_requests"
    ), patch.object(
        apm_api_module.IOSPerformance, "resume_requests"
    ):
        response = client.get("/apm/initialize")

    assert response.json["status"] == 1
    report.begin_session.assert_called_once_with()

