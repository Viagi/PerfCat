"""Multi-scene compare: scene validation, API payload and page rendering."""
import json
import sys

from perfcattle.report.manager import ReportStore
from perfcattle.web import app

REPORT_API = sys.modules["perfcattle.view.report_api"]
PAGES = sys.modules["perfcattle.view.pages"]


def _write_report(report_dir, scene, app_name="com.example", platform="Android", model="normal"):
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
            }
        ),
        encoding="utf-8",
    )
    for name in ("cpu_app.log", "cpu_sys.log", "fps.log", "mem_total.log"):
        (scene_dir / name).write_text("10:00:00=10\n10:00:01=20\n", encoding="utf-8")
    return scene_dir


def _store(report_dir):
    store = ReportStore()
    store.report_dir = str(report_dir)
    store._log_io.report_dir = str(report_dir)
    return store


def test_compare_multi_returns_every_scene(tmp_path, monkeypatch):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    scenes = ["scene_a", "scene_b", "scene_c"]
    for scene in scenes:
        _write_report(report_dir, scene)

    monkeypatch.setattr(REPORT_API, "f", _store(report_dir))
    client = app.test_client()
    response = client.get(
        "/apm/log/compare_multi?scenes=scene_a&scenes=scene_b&scenes=scene_c"
        "&target=cpu&platform=Android"
    )
    payload = response.get_json()

    assert payload["status"] == 1
    assert [item["scene"] for item in payload["scenes"]] == scenes
    assert payload["scenes"][0]["data"]["cpuAppData"]


def test_compare_multi_rejects_more_than_six_scenes(tmp_path, monkeypatch):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    scenes = ["scene_{}".format(index) for index in range(7)]
    for scene in scenes:
        _write_report(report_dir, scene)

    monkeypatch.setattr(REPORT_API, "f", _store(report_dir))
    client = app.test_client()
    query = "&".join("scenes={}".format(scene) for scene in scenes)
    payload = client.get(
        "/apm/log/compare_multi?{}&target=cpu&platform=Android".format(query)
    ).get_json()

    assert payload["status"] == 0
    assert "2-6" in payload["msg"]


def test_compare_page_renders_multiple_scenes(tmp_path, monkeypatch):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    scenes = ["run_a", "run_b", "run_c"]
    for scene in scenes:
        _write_report(report_dir, scene)
    _write_report(report_dir, "other_app", app_name="com.other")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(PAGES, "f", _store(report_dir))

    client = app.test_client()
    response = client.get(
        "/compare_analysis?platform=Android&lan=cn&app=com.example"
        "&scenes=run_a&scenes=run_b&scenes=run_c&scenes=other_app"
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "run_a" in body and "run_b" in body and "run_c" in body
    # Scenes from another app are filtered out server-side.
    assert "other_app" not in body
    # The comparison export entry point is available for multi-scene pages.
    assert "exportCompareReport" in body
    assert "导出对比报告" in body


def _write_unity_logs(scene_dir):
    (scene_dir / "unity_stats.log").write_text(
        '10:00:00={"cpu_frame_ms":5.0,"render_frame_ms":2.0,'
        '"cpu_total_ms":7.0,"gpu_frame_ms":3.0,"draw_calls":100,'
        '"setpass_calls":40,"batches":120,"static_batches":80,'
        '"dynamic_batches":20,"instanced_batches":20,'
        '"static_batched_draws":80,"dynamic_batched_draws":20,'
        '"instanced_batched_draws":10,"shadow_casters":25,'
        '"triangles":50000,"vertices":70000,"gc_alloc_kb":10.0,'
        '"used_memory_mb":900.0,"gc_used_memory_mb":120.0,'
        '"render_textures":50,"render_textures_mb":40.0,'
        '"render_textures_changes":5,"used_buffers":100,'
        '"used_buffers_mb":10.0,"vertex_upload":5,'
        '"vertex_upload_kb":100.0,"index_upload":5,'
        '"index_upload_kb":10.0}\n',
        encoding="utf-8",
    )
    (scene_dir / "gpu_stats.log").write_text(
        '10:00:00={"gpu_temp":55.0,"gpu_clock":300,"gpu_bus":250,'
        '"gpu_mem_app":600.0,"gpu_mem_total":800.0,'
        '"gpu_bw_read":240.0,"gpu_bw_write":60.0}\n',
        encoding="utf-8",
    )


def test_compare_page_shows_every_recorded_unity_and_gpu_metric(tmp_path, monkeypatch):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    for scene in ("run_a", "run_b"):
        _write_unity_logs(_write_report(report_dir, scene))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(PAGES, "f", _store(report_dir))

    body = app.test_client().get(
        "/compare_analysis?platform=Android&lan=cn&scenes=run_a&scenes=run_b"
    ).get_data(as_text=True)

    # 已写进 unity_stats.log / gpu_stats.log 的计数器都应在对比页出现，
    # 而不是只出现在单报告页或原始日志里。
    for label in (
        "渲染线程",
        "CPU 总耗时",
        "SetPass Calls",
        "Batches",
        "静态合批",
        "动态合批",
        "实例化合批",
        "静态合批 DC",
        "阴影投射",
        "GC Alloc/帧",
        "Unity 已用内存",
        "Mono / GC 已用内存",
        "Native 内存",
        "GPU 温度",
        "GPU 频率",
        "显存(应用)",
        "带宽读/帧",
        "带宽写/帧",
    ):
        assert label in body

    for chart_id in (
        "chart-unity-setpass-calls",
        "chart-unity-batches",
        "chart-unity-render-frame-ms",
        "chart-unity-gc-alloc-kb",
        "chart-unity-used-memory-mb",
        "chart-unity-native-memory-mb",
        "chart-gpu-gpu-temp",
        "chart-gpu-gpu-bw-read-frame",
    ):
        assert chart_id in body


def test_compare_page_skips_unity_metrics_that_were_not_recorded(tmp_path, monkeypatch):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    for scene in ("run_a", "run_b"):
        _write_report(report_dir, scene)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(PAGES, "f", _store(report_dir))

    body = app.test_client().get(
        "/compare_analysis?platform=Android&lan=cn&scenes=run_a&scenes=run_b"
    ).get_data(as_text=True)

    assert "SetPass Calls" not in body
    assert "chart-unity-setpass-calls" not in body
    assert "GPU 温度" not in body
