"""Report package export: Unity raw/snapshot + offline JSON in one ZIP."""
import json
import os
import time
import zipfile

from perfcattle.report import package_export as package_export_module
from perfcattle.report.package_export import ReportPackageExporter
from perfcattle.unity.export import build_profiler_json
from tests.test_unity_hotspots import _stream


class _LogIO(object):
    def __init__(self, report_dir):
        self.report_dir = str(report_dir)


def test_build_profiler_json(tmp_path):
    raw_path = _stream(tmp_path)
    payload = build_profiler_json(
        raw_path,
        meta={"unity_version": "2022.3", "profiler_log": "C:/secret/profiler.raw"},
        max_frames=0,
    )

    assert payload["format"] == "perfcattle-unity-profiler-json"
    assert payload["options"] == {
        "max_frames": 0,
        "top_markers": 30,
        "slow_frames": 10,
    }
    assert payload["stream"]["frame_count"] == 3
    assert payload["stream"]["marker_count"] == 3
    assert payload["summary"]["exported_frame_count"] == 3
    assert payload["summary"]["sampled"] is False
    # Absolute staging paths must not leak into the export.
    assert "profiler_log" not in payload["capture"]

    frames = {item["index"]: item for item in payload["frames"]}
    frame = frames[2]
    assert frame["main_thread_total_us"] == 40000.0
    assert frame["gc_bytes"] == 2048
    names = {item["name"] for item in frame["top_markers"]}
    assert {"Main Root", "Main Child"} <= names

    markers = {item["name"]: item for item in payload["summary"]["markers"]}
    assert markers["Main Root"]["calls"] == 1
    assert markers["Main Child"]["gc_bytes"] == 2048
    assert markers["Main Root"]["total_us"] == 30000.0

    assert payload["slow_frames"]
    slow = {item["index"]: item for item in payload["slow_frames"]}
    assert slow[2]["hierarchy"][0]["name"] == "Main Root"


def test_build_profiler_json_samples_when_frame_limit_is_small(tmp_path):
    payload = build_profiler_json(_stream(tmp_path), max_frames=2)
    assert payload["summary"]["sampled"] is True
    assert payload["summary"]["exported_frame_count"] == 2
    assert [item["index"] for item in payload["frames"]] == [1, 3]


def test_export_package_includes_unity_files(tmp_path):
    report_dir = tmp_path / "report"
    scene = "apm_test"
    scene_dir = report_dir / scene
    unity_dir = scene_dir / "unity"
    unity_dir.mkdir(parents=True)

    (scene_dir / "result.json").write_text(
        json.dumps({"app": "com.example", "platform": "Android"}), encoding="utf-8"
    )
    (scene_dir / "cpu_app.log").write_text("10:00:00=12.5\n", encoding="utf-8")
    (scene_dir / "fps.log").write_text("10:00:00=60\n", encoding="utf-8")

    raw_source = _stream(tmp_path)
    with open(raw_source, "rb") as src, open(unity_dir / "profiler.raw", "wb") as dst:
        dst.write(src.read())
    (unity_dir / "memory_20261023_120000.snapshot").write_bytes(b"\x01\xed\xbc\xfa")
    (unity_dir / "meta.json").write_text(
        json.dumps({"unity_version": "2022.3", "mode": "protocol"}), encoding="utf-8"
    )

    result = ReportPackageExporter(_LogIO(report_dir)).export("Android", scene)

    assert result["status"] == 1
    assert result["package"] is True
    zip_path = result["path"]
    assert zip_path.endswith("{}_export.zip".format(scene))

    with zipfile.ZipFile(zip_path) as bundle:
        names = set(bundle.namelist())
        assert "apm_test.xls" in names
        assert "result.json" in names
        assert "unity/profiler.raw" in names
        assert "unity/profiler.json" in names
        assert "unity/memory_20261023_120000.snapshot" in names
        assert "unity/meta.json" in names
        assert "EXPORT_README.txt" in names

        payload = json.loads(bundle.read("unity/profiler.json").decode("utf-8"))
        assert payload["format"] == "perfcattle-unity-profiler-json"
        assert payload["summary"]["frame_count"] == 3

        readme = bundle.read("EXPORT_README.txt").decode("utf-8")
        assert "文件清单" in readme
        for name in (
            "apm_test.xls",
            "result.json",
            "unity/profiler.raw",
            "unity/profiler.json",
            "unity/memory_20261023_120000.snapshot",
            "unity/meta.json",
        ):
            assert name in readme
        assert "Unity Profiler 原始流" in readme
        assert "Memory Profiler 快照" in readme
        assert "Unity 采集元信息" in readme

    # The generated JSON is also available next to the raw capture.
    assert (unity_dir / "profiler.json").is_file()


def test_export_without_unity_keeps_excel_path(tmp_path):
    report_dir = tmp_path / "report"
    scene = "apm_plain"
    scene_dir = report_dir / scene
    scene_dir.mkdir(parents=True)
    (scene_dir / "result.json").write_text("{}", encoding="utf-8")
    (scene_dir / "cpu_app.log").write_text("10:00:00=12.5\n", encoding="utf-8")

    result = ReportPackageExporter(_LogIO(report_dir)).export("Android", scene)

    assert result["package"] is False
    assert result["path"].endswith("apm_plain.xls")
    assert not (scene_dir / "{}_export.zip".format(scene)).exists()


def _scene_with_unity(tmp_path, scene="apm_test"):
    report_dir = tmp_path / "report"
    scene_dir = report_dir / scene
    unity_dir = scene_dir / "unity"
    unity_dir.mkdir(parents=True)

    (scene_dir / "result.json").write_text(
        json.dumps({"app": "com.example", "platform": "Android"}), encoding="utf-8"
    )
    (scene_dir / "cpu_app.log").write_text("10:00:00=12.5\n", encoding="utf-8")
    (scene_dir / "fps.log").write_text("10:00:00=60\n", encoding="utf-8")
    with open(_stream(tmp_path), "rb") as src, open(
        unity_dir / "profiler.raw", "wb"
    ) as dst:
        dst.write(src.read())
    (unity_dir / "meta.json").write_text(
        json.dumps({"unity_version": "2022.3", "mode": "protocol"}), encoding="utf-8"
    )
    return report_dir, scene, unity_dir


def test_collect_reuses_profiler_json_when_capture_is_unchanged(
    tmp_path, monkeypatch
):
    report_dir, scene, unity_dir = _scene_with_unity(tmp_path)
    exporter = ReportPackageExporter(_LogIO(report_dir))

    first = exporter.collect("Android", scene)
    assert first["json_files"] == ["unity/profiler.json"]
    payload = json.loads((unity_dir / "profiler.json").read_text(encoding="utf-8"))
    assert payload["options"]["max_frames"] == 1000

    rebuilt = []

    def fail_rebuild(*args, **kwargs):
        rebuilt.append(args)
        raise AssertionError("cached profiler JSON must not be rebuilt")

    monkeypatch.setattr(package_export_module, "write_profiler_json", fail_rebuild)

    second = exporter.collect("Android", scene)
    assert rebuilt == []
    assert second["json_files"] == ["unity/profiler.json"]


def test_collect_rebuilds_profiler_json_when_options_or_inputs_change(
    tmp_path, monkeypatch
):
    report_dir, scene, unity_dir = _scene_with_unity(tmp_path)
    exporter = ReportPackageExporter(_LogIO(report_dir))
    exporter.collect("Android", scene)

    calls = []
    real = package_export_module.write_profiler_json

    def spy(raw_path, out_path, **kwargs):
        calls.append(kwargs)
        return real(raw_path, out_path, **kwargs)

    monkeypatch.setattr(package_export_module, "write_profiler_json", spy)

    # Different sampling options invalidate the cache.
    exporter.collect("Android", scene, max_frames=2)
    assert len(calls) == 1
    assert calls[0]["max_frames"] == 2

    exporter.collect("Android", scene, max_frames=2)
    assert len(calls) == 1

    # Updated capture metadata invalidates the cache.
    time.sleep(0.01)
    os.utime(unity_dir / "meta.json", None)
    exporter.collect("Android", scene, max_frames=2)
    assert len(calls) == 2

    # A newer raw capture invalidates the cache.
    time.sleep(0.01)
    os.utime(unity_dir / "profiler.raw", None)
    exporter.collect("Android", scene, max_frames=2)
    assert len(calls) == 3


def test_collect_rebuilds_legacy_profiler_json_without_options(
    tmp_path, monkeypatch
):
    report_dir, scene, unity_dir = _scene_with_unity(tmp_path)
    exporter = ReportPackageExporter(_LogIO(report_dir))
    exporter.collect("Android", scene)

    json_path = unity_dir / "profiler.json"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    payload.pop("options")
    json_path.write_text(json.dumps(payload), encoding="utf-8")

    calls = []
    real = package_export_module.write_profiler_json

    def spy(raw_path, out_path, **kwargs):
        calls.append(kwargs)
        return real(raw_path, out_path, **kwargs)

    monkeypatch.setattr(package_export_module, "write_profiler_json", spy)

    exporter.collect("Android", scene)
    assert len(calls) == 1
    rebuilt = json.loads(json_path.read_text(encoding="utf-8"))
    assert rebuilt["options"]["max_frames"] == 1000
