"""Report-side Unity metric time alignment."""
import os

from perfcattle.report.log_io import LogIO


def test_unity_correlation_uses_shared_origin(tmp_path):
    report = tmp_path / "report"
    scene_dir = report / "scene"
    scene_dir.mkdir(parents=True)
    (scene_dir / "cpu_app.log").write_text(
        "10:00:03=90\n10:00:04=95\n10:00:05=80\n"
    )
    (scene_dir / "unity_stats.log").write_text(
        '10:00:04={"draw_calls":320,"triangles":230000,"vertices":231000,'
        '"cpu_frame_ms":16.7,"frame":100}\n'
        '10:00:05={"draw_calls":330,"triangles":231000,"vertices":232000,'
        '"cpu_frame_ms":17.1,"frame":101}\n'
    )
    log = LogIO(fileroot=str(tmp_path))
    log.report_dir = str(report)
    data = log.getUnityCorrelationLog("Android", "scene")
    assert data["charts"]["cpu_app"] == [
        {"x": 0.0, "y": 90},
        {"x": 1.0, "y": 95},
        {"x": 2.0, "y": 80},
    ]
    # Unity 日志从 10:00:04 开始，公共原点是 10:00:03
    assert data["charts"]["draw_calls"] == [
        {"x": 1.0, "y": 320},
        {"x": 2.0, "y": 330},
    ]
    assert data["charts"]["triangles"][0]["y"] == 230000


def test_unity_stats_log_average(tmp_path):
    report = tmp_path / "report"
    scene_dir = report / "scene"
    scene_dir.mkdir(parents=True)
    (scene_dir / "unity_stats.log").write_text(
        '10:00:00={"draw_calls":300,"triangles":100,"frame":1}\n'
        '10:00:01={"draw_calls":320,"triangles":200,"frame":2}\n'
    )
    log = LogIO(fileroot=str(tmp_path))
    log.report_dir = str(report)
    data = log.getUnityStatsLog("Android", "scene")
    assert data["avg"]["draw_calls"] == 310.0
    assert data["avg"]["triangles"] == 150.0
    # frame is kept for chart-click -> frame mapping, but not averaged.
    assert [point["y"] for point in data["charts"]["frame"]] == [1, 2]
    assert "frame" not in data["avg"]
