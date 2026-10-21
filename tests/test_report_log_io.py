"""Focused report log IO tests."""
from unittest.mock import patch

from perfcat.report.log_io import LogIO


def test_file_io_helpers_include_read_write_dimensions(tmp_report_dir):
    scene1 = tmp_report_dir / "scene1"
    scene2 = tmp_report_dir / "scene2"
    scene1.mkdir()
    scene2.mkdir()
    for scene, read, write in ((scene1, 10, 20), (scene2, 30, 40)):
        (scene / "file_read.log").write_text(f"10:00:00={read}\n")
        (scene / "file_write.log").write_text(f"10:00:00={write}\n")

    with patch("os.getcwd", return_value=str(tmp_report_dir.parent)):
        log_io = LogIO()
        single = log_io.getFileIOLog("Android", "scene1")
        compare = log_io.getFileIOLogCompare("Android", "scene1", "scene2")

    assert "total" not in single
    assert single["read"][0]["y"] == 10
    assert single["write"][0]["y"] == 20
    assert "totalScene1" not in compare
    assert compare["readScene1"][0]["y"] == 10
    assert compare["writeScene2"][0]["y"] == 40


def test_thermal_helpers_read_logs_and_snapshot_fallback(tmp_report_dir):
    scene1 = tmp_report_dir / "scene1"
    scene2 = tmp_report_dir / "scene2"
    scene1.mkdir()
    scene2.mkdir()
    (scene1 / "thermal_cpu.log").write_text("10:00:00=45\n")
    (scene1 / "thermal_gpu.log").write_text("10:00:00=39\n")
    (scene2 / "init_thermal_temp.json").write_text(
        '[{"name":"CPU","temp":"44000"}]'
    )
    (scene2 / "current_thermal_temp.json").write_text(
        '[{"name":"CPU","temp":"46000"}]'
    )

    with patch("os.getcwd", return_value=str(tmp_report_dir.parent)):
        log_io = LogIO()
        single = log_io.getThermalLog("Android", "scene1")
        compare = log_io.getThermalLogCompare("Android", "scene1", "scene2")

    assert single["thermal"]["CPU"][0]["y"] == 45
    assert single["thermal"]["GPU"][0]["y"] == 39
    assert compare["scene2"]["CPU"] == [{"x": 0, "y": 44.0}, {"x": 1, "y": 46.0}]
