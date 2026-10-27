"""Compare report export: summary + every report's original/Unity files."""
import json
import os
import shutil
import sys
import zipfile

from perfcattle.report import package_export as package_export_module
from perfcattle.report.manager import ReportStore
from perfcattle.web import app
from tests.test_unity_hotspots import _stream

REPORT_API = sys.modules["perfcattle.view.report_api"]


def _write_report(
    report_dir,
    scene,
    raw_path=None,
    app_name="com.example",
    platform="Android",
    model="normal",
    unity_stats=True,
):
    scene_dir = report_dir / scene
    scene_dir.mkdir(parents=True)
    (scene_dir / "result.json").write_text(
        json.dumps(
            {
                "app": app_name,
                "platform": platform,
                "model": model,
                "devices": "device",
                "ctime": "2026-01-01-00-00-00",
                "cores": 8,
            }
        ),
        encoding="utf-8",
    )
    for name, value in (
        ("cpu_app.log", 12.5),
        ("cpu_sys.log", 40),
        ("mem_total.log", 512),
        ("fps.log", 60),
    ):
        (scene_dir / name).write_text(
            "10:00:00={}\n10:00:01={}\n".format(value, value), encoding="utf-8"
        )
    if unity_stats:
        (scene_dir / "unity_stats.log").write_text(
            '10:00:00={"cpu_frame_ms": 5.2, "render_frame_ms": 2.5, '
            '"cpu_total_ms": 8.4, "gpu_frame_ms": 3.1, '
            '"draw_calls": 120, "setpass_calls": 40, "batches": 130, '
            '"static_batches": 90, "dynamic_batches": 20, '
            '"instanced_batches": 20, "static_batched_draws": 90, '
            '"dynamic_batched_draws": 20, "instanced_batched_draws": 10, '
            '"shadow_casters": 30, '
            '"triangles": 50000, "vertices": 70000, '
            '"gc_alloc_kb": 12.5, "used_memory_mb": 900.0, '
            '"gc_used_memory_mb": 120.0, '
            '"render_textures": 55, "render_textures_mb": 48.1, '
            '"render_textures_changes": 12, "used_buffers": 1356, '
            '"used_buffers_mb": 50.9, "vertex_upload": 16, '
            '"vertex_upload_kb": 140.7, "index_upload": 16, '
            '"index_upload_kb": 9.6}\n',
            encoding="utf-8",
        )
        (scene_dir / "gpu_stats.log").write_text(
            '10:00:00={"gpu_temp": 55.0, "gpu_clock": 300, "gpu_bus": 250, '
            '"gpu_mem_app": 600.0, "gpu_mem_total": 800.0, '
            '"gpu_bw_read": 240.0, "gpu_bw_write": 60.0}\n',
            encoding="utf-8",
        )
    if raw_path:
        unity_dir = scene_dir / "unity"
        unity_dir.mkdir()
        shutil.copyfile(raw_path, unity_dir / "profiler.raw")
        (unity_dir / "meta.json").write_text(
            json.dumps({"unity_version": "2022.3", "mode": "protocol"}),
            encoding="utf-8",
        )
    return scene_dir


def _make_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "report").mkdir(exist_ok=True)
    return ReportStore()


def test_compare_export_bundles_every_report(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    report_dir = tmp_path / "report"
    raw_path = _stream(tmp_path)
    _write_report(report_dir, "run_a", raw_path=raw_path)
    _write_report(report_dir, "run_b", raw_path=raw_path)

    result = store.export_compare("Android", ["run_a", "run_b"])

    assert result["status"] == 1
    assert result["app"] == "com.example"
    assert result["platform"] == "Android"
    assert result["warnings"] == []
    zip_path = result["path"]
    assert zip_path.endswith(".zip")
    assert zipfile.is_zipfile(zip_path)

    with zipfile.ZipFile(zip_path) as bundle:
        names = set(bundle.namelist())
        assert "compare.xls" in names
        assert "compare.json" in names
        assert "README.txt" in names
        for scene in ("run_a", "run_b"):
            assert "scenes/{}/{}.xls".format(scene, scene) in names
            assert "scenes/{}/result.json".format(scene) in names
            assert "scenes/{}/unity/profiler.raw".format(scene) in names
            assert "scenes/{}/unity/profiler.json".format(scene) in names
            assert "scenes/{}/unity/meta.json".format(scene) in names

        payload = json.loads(bundle.read("compare.json").decode("utf-8"))
        assert payload["format"] == "perfcattle-compare-report"
        assert [item["scene"] for item in payload["scenes"]] == ["run_a", "run_b"]

        readme = bundle.read("README.txt").decode("utf-8")
        assert "文件清单" in readme
        assert "compare.xls" in readme
        assert "compare.json" in readme
        assert "对比汇总（Excel）" in readme
        for scene in ("run_a", "run_b"):
            assert "scenes/{}/{}.xls".format(scene, scene) in readme
            assert "scenes/{}/unity/profiler.raw".format(scene) in readme
            assert "scenes/{}/unity/profiler.json".format(scene) in readme

        metrics = {(row["section"], row["label"]): row for row in payload["metrics"]}
        cpu = metrics[("CPU", "APP CPU")]
        assert cpu["values"] == {"run_a": "12.5%", "run_b": "12.5%"}
        unity = metrics[("Unity", "Draw Calls")]
        assert unity["values"] == {"run_a": 120, "run_b": 120}
        render_textures = metrics[("Unity", "Render Textures (MB)")]
        assert render_textures["values"] == {"run_a": 48.1, "run_b": 48.1}
        assert metrics[("Unity", "Used Buffers (MB)")]["values"] == {
            "run_a": 50.9,
            "run_b": 50.9,
        }
        assert metrics[("Unity", "Vertex Upload (KB)")]["values"] == {
            "run_a": 140.7,
            "run_b": 140.7,
        }
        assert metrics[("Unity", "Index Upload (KB)")]["values"] == {
            "run_a": 9.6,
            "run_b": 9.6,
        }
        # Every counter written to unity_stats.log reaches the comparison,
        # including the ones the compare page did not surface before.
        assert metrics[("Unity", "Render Thread (ms)")]["values"] == {
            "run_a": 2.5,
            "run_b": 2.5,
        }
        assert metrics[("Unity", "CPU Total (ms)")]["values"] == {
            "run_a": 8.4,
            "run_b": 8.4,
        }
        assert metrics[("Unity", "SetPass Calls")]["values"] == {
            "run_a": 40,
            "run_b": 40,
        }
        assert metrics[("Unity", "Static Batched Draw Calls")]["values"] == {
            "run_a": 90,
            "run_b": 90,
        }
        assert metrics[("Unity", "Shadow Casters")]["values"] == {
            "run_a": 30,
            "run_b": 30,
        }
        assert metrics[("Unity", "GC Alloc (KB/frame)")]["values"] == {
            "run_a": 12.5,
            "run_b": 12.5,
        }
        assert metrics[("Unity", "Unity Memory (MB)")]["values"] == {
            "run_a": 900.0,
            "run_b": 900.0,
        }
        # Mono/IL2CPP 托管堆：Unity 的 GC Used Memory
        assert metrics[("Unity", "Mono / GC Memory (MB)")]["values"] == {
            "run_a": 120.0,
            "run_b": 120.0,
        }
        # native = Total Used Memory - GC Used Memory
        assert metrics[("Unity", "Native Memory (MB)")]["values"] == {
            "run_a": 780.0,
            "run_b": 780.0,
        }
        assert metrics[("GPU Stats", "Temperature (C)")]["values"] == {
            "run_a": 55.0,
            "run_b": 55.0,
        }
        assert metrics[("GPU Stats", "Clock (MHz)")]["values"] == {
            "run_a": 300,
            "run_b": 300,
        }
        # gpu_stats.log bandwidth is converted to MB/frame with the FPS log.
        assert metrics[("GPU Stats", "Read (MB/frame)")]["values"] == {
            "run_a": 4.0,
            "run_b": 4.0,
        }
        # Missing metrics are not exported as misleading zero rows.
        assert ("Battery", "Current") not in metrics

        unity_json = json.loads(
            bundle.read("scenes/run_a/unity/profiler.json").decode("utf-8")
        )
        assert unity_json["format"] == "perfcattle-unity-profiler-json"
        assert unity_json["summary"]["frame_count"] == 3


def test_compare_export_reuses_profiler_json_on_repeat(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    report_dir = tmp_path / "report"
    raw_path = _stream(tmp_path)
    _write_report(report_dir, "run_a", raw_path=raw_path)
    _write_report(report_dir, "run_b", raw_path=raw_path)

    first = store.export_compare("Android", ["run_a", "run_b"])
    assert first["status"] == 1

    calls = []
    monkeypatch.setattr(
        package_export_module,
        "write_profiler_json",
        lambda *args, **kwargs: calls.append(args),
    )

    second = store.export_compare("Android", ["run_a", "run_b"])
    assert second["status"] == 1
    assert calls == []

    with zipfile.ZipFile(second["path"]) as bundle:
        profiler = json.loads(
            bundle.read("scenes/run_a/unity/profiler.json").decode("utf-8")
        )
    assert profiler["summary"]["frame_count"] == 3


def test_compare_export_includes_scene_without_unity(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    report_dir = tmp_path / "report"
    _write_report(report_dir, "run_a", raw_path=_stream(tmp_path))
    _write_report(report_dir, "run_b", unity_stats=False)

    result = store.export_compare("Android", ["run_a", "run_b"])

    with zipfile.ZipFile(result["path"]) as bundle:
        names = set(bundle.namelist())
        assert "scenes/run_a/unity/profiler.json" in names
        assert "scenes/run_b/run_b.xls" in names
        assert "scenes/run_b/result.json" in names
        assert "scenes/run_b/unity/profiler.json" not in names


def test_compare_export_endpoint(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    report_dir = tmp_path / "report"
    raw_path = _stream(tmp_path)
    _write_report(report_dir, "run_a", raw_path=raw_path)
    _write_report(report_dir, "run_b", raw_path=raw_path)
    monkeypatch.setattr(REPORT_API, "f", store)

    client = app.test_client()
    payload = client.get(
        "/apm/export/compare?scenes=run_a&scenes=run_b&platform=Android"
    ).get_json()

    assert payload["status"] == 1
    assert payload["package"] is True
    assert zipfile.is_zipfile(payload["path"])

    # Platform is derived from result.json when the caller omits it, and the
    # optional Unity sampling parameters reach the profiler JSON.
    payload = client.get(
        "/apm/export/compare?scenes=run_a&scenes=run_b&unity_frames=2"
    ).get_json()
    assert payload["status"] == 1
    assert payload["platform"] == "Android"
    with zipfile.ZipFile(payload["path"]) as bundle:
        profiler = json.loads(
            bundle.read("scenes/run_a/unity/profiler.json").decode("utf-8")
        )
    assert profiler["summary"]["sampled"] is True
    assert profiler["summary"]["exported_frame_count"] == 2


def test_compare_export_rejects_wrong_scene_count(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    _write_report(tmp_path / "report", "run_a")
    monkeypatch.setattr(REPORT_API, "f", store)

    payload = app.test_client().get(
        "/apm/export/compare?scenes=run_a&platform=Android"
    ).get_json()

    assert payload["status"] == 0
    assert "2-6" in payload["msg"]


def test_compare_export_rejects_invalid_or_mismatched_scenes(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    report_dir = tmp_path / "report"
    _write_report(report_dir, "run_a")
    _write_report(report_dir, "run_b", app_name="com.other")
    monkeypatch.setattr(REPORT_API, "f", store)

    client = app.test_client()
    mismatch = client.get(
        "/apm/export/compare?scenes=run_a&scenes=run_b&platform=Android"
    ).get_json()
    assert mismatch["status"] == 0
    assert "same app and platform" in mismatch["msg"]

    traversal = client.get(
        "/apm/export/compare?scenes=..%2Fevil&scenes=run_a&platform=Android"
    ).get_json()
    assert traversal["status"] == 0
    assert "invalid scene" in traversal["msg"]


def test_excel_export_includes_unity_and_gpu_stats_sheets(tmp_path, monkeypatch):
    """The Excel workbook must carry Unity / GPU stats, not only the old log list."""
    import xlwt  # noqa: F401  (already a hard dependency of the exporter)

    store = _make_store(tmp_path, monkeypatch)
    report_dir = tmp_path / "report"
    raw_path = _stream(tmp_path)
    scene_dir = _write_report(report_dir, "run_a", raw_path=raw_path)
    (scene_dir / "gpu_stats.log").write_text(
        '10:00:00={"gpu_util":{"value":41.5,"status":"ok"},'
        '"gpu_temp":{"value":null,"status":"missing"}}\n',
        encoding="utf-8",
    )

    exporter = store._excel_exporter if hasattr(store, "_excel_exporter") else None
    if exporter is None:
        from perfcattle.report.excel_export import ExcelExporter
        exporter = ExcelExporter(log_io=store._log_io)
    out = exporter.export_excel(platform="Android", scene="run_a")
    assert os.path.getsize(out) > 0
    assert exporter.JSON_LOG_SHEETS == ("unity_stats", "gpu_stats")
