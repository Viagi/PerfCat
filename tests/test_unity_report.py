"""Report-side Unity metric time alignment."""
import os
from unittest.mock import patch

from perfcattle.report.html_export import HtmlExporter
from perfcattle.report.log_io import LogIO
from perfcattle.report.manager import ReportManager


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
    assert data["charts"]["triangles"][0]["y"] == 230.0


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
    # 三角面按 k 展示，保留 1 位小数
    assert data["avg"]["triangles"] == 0.1
    assert [point["y"] for point in data["charts"]["triangles"]] == [0.1, 0.2]
    # frame is kept for chart-click -> frame mapping, but not averaged.
    assert [point["y"] for point in data["charts"]["frame"]] == [1, 2]
    assert "frame" not in data["avg"]


def test_unity_stats_log_rendering_resources(tmp_path):
    report = tmp_path / "report"
    scene_dir = report / "scene"
    scene_dir.mkdir(parents=True)
    (scene_dir / "unity_stats.log").write_text(
        '10:00:00={"render_textures":55,"render_textures_mb":48.1,'
        '"render_textures_changes":12,"used_buffers":1356,"used_buffers_mb":50.9,'
        '"vertex_upload":16,"vertex_upload_kb":140.7,'
        '"index_upload":16,"index_upload_kb":9.6,"frame":1}\n'
    )
    log = LogIO(fileroot=str(tmp_path))
    log.report_dir = str(report)
    data = log.getUnityStatsLog("Android", "scene")
    for key, value in (
        ("render_textures", 55.0),
        ("render_textures_mb", 48.1),
        ("render_textures_changes", 12.0),
        ("used_buffers", 1356.0),
        ("used_buffers_mb", 50.9),
        ("vertex_upload", 16.0),
        ("vertex_upload_kb", 140.7),
        ("index_upload", 16.0),
        ("index_upload_kb", 9.6),
    ):
        assert data["avg"][key] == value
        assert data["charts"][key][0]["y"] == value


def test_unity_stats_log_compare(tmp_path):
    report = tmp_path / "report"
    for scene, draw_calls in (("scene1", 300), ("scene2", 500)):
        scene_dir = report / scene
        scene_dir.mkdir(parents=True)
        (scene_dir / "unity_stats.log").write_text(
            f'10:00:00={{"draw_calls":{draw_calls},"triangles":100,"frame":1}}\n'
        )
    log = LogIO(fileroot=str(tmp_path))
    log.report_dir = str(report)
    data = log.getUnityStatsLogCompare("Android", "scene1", "scene2")
    assert data["status"] == 1
    assert data["scene1"]["avg"]["draw_calls"] == 300.0
    assert data["scene2"]["avg"]["draw_calls"] == 500.0
    assert data["scene1"]["charts"]["draw_calls"] == [{"x": 0.0, "y": 300}]


def test_unity_stats_derives_native_memory_curve(tmp_path):
    report = tmp_path / "report"
    scene_dir = report / "scene"
    scene_dir.mkdir(parents=True)
    (scene_dir / "unity_stats.log").write_text(
        '10:00:00={"used_memory_mb":1000.0,"gc_used_memory_mb":120.0,"frame":1}\n'
        '10:00:01={"used_memory_mb":1010.5,"gc_used_memory_mb":121.5,"frame":2}\n'
        '10:00:02={"gc_used_memory_mb":122.0,"frame":3}\n'
    )
    log = LogIO(fileroot=str(tmp_path))
    log.report_dir = str(report)
    data = log.getUnityStatsLog("Android", "scene")
    # native = Total Used Memory - GC Used Memory，只保留两个计数器都存在的样本
    assert [point["y"] for point in data["charts"]["native_memory_mb"]] == [880.0, 889.0]
    assert data["avg"]["native_memory_mb"] == 884.5
    assert data["avg"]["used_memory_mb"] == 1005.25
    assert data["avg"]["gc_used_memory_mb"] == 121.17


def test_unity_stats_without_gc_memory_has_no_native_curve(tmp_path):
    report = tmp_path / "report"
    scene_dir = report / "scene"
    scene_dir.mkdir(parents=True)
    (scene_dir / "unity_stats.log").write_text(
        '10:00:00={"used_memory_mb":1000.0,"frame":1}\n'
    )
    log = LogIO(fileroot=str(tmp_path))
    log.report_dir = str(report)
    data = log.getUnityStatsLog("Android", "scene")
    assert "native_memory_mb" not in data["charts"]
    assert "native_memory_mb" not in data["avg"]


def _render_android_report(tmp_path, unity_stats, unity_stats_charts=None):
    scene_dir = tmp_path / "report" / "scene"
    scene_dir.mkdir(parents=True)
    report_path = scene_dir / "report.html"
    log_io = LogIO()
    log_io.report_dir = str(tmp_path / "report")
    exporter = HtmlExporter(log_io=log_io)
    summary = {
        "devices": "test_device",
        "app": "com.test.app",
        "platform": "Android",
        "ctime": "2024-01-01-12-00-00",
        "cpu_app": "10%",
        "cpu_sys": "20%",
        "mem_total": "100MB",
        "mem_peak": "120MB",
        "mem_swap": "0MB",
        "fps": "60HZ/s",
        "frame_stability": "99",
        "jank": "0/min",
        "level": "100%",
        "tem": "30°C",
        "net_send": "0MB",
        "net_recv": "0MB",
        "file_read": "0MB",
        "file_write": "0MB",
        "gpu": 0,
        "cpu_charts": {},
        "mem_charts": {},
        "net_charts": {},
        "battery_charts": {},
        "fps_charts": {},
        "jank_charts": {},
        "mem_detail_charts": {"memory_detail": {}},
        "gpu_charts": {},
        "gpu_stats": {},
        "gpu_stats_charts": {},
        "unity_stats": unity_stats,
        "unity_stats_charts": unity_stats_charts or {},
        "unity_correlation_charts": {},
    }
    exporter.make_android_html("scene", summary, report_path=str(report_path))
    return report_path.read_text(encoding="utf-8")


def test_android_report_top_cards_include_unity_stats(tmp_path):
    html = _render_android_report(
        tmp_path,
        {
            "cpu_frame_ms": 16.9,
            "gpu_frame_ms": 12.7,
            "cpu_total_ms": 25.4,
            "render_frame_ms": 11.1,
            "draw_calls": 310.0,
            "setpass_calls": 40.0,
            "batches": 120.0,
            "shadow_casters": 12.0,
            "triangles": 230.5,
            "vertices": 231.5,
            "gc_alloc_kb": 8.25,
            "used_memory_mb": 900.0,
            "gc_used_memory_mb": 120.0,
            "native_memory_mb": 780.0,
        },
        unity_stats_charts={"shadow_casters": [{"x": 0.0, "y": 12}]},
    )

    assert "Unity CPU Frame" in html
    assert "Avg: 16.9 ms" in html
    assert "Unity GPU Frame" in html
    assert "Avg: 12.7 ms" in html
    assert "Unity Total Frame" in html
    assert "Avg: 25.4 ms" in html
    assert "Unity Render Thread" in html
    assert "Avg: 11.1 ms" in html
    assert "Draw Calls" in html
    assert "Avg: 310" in html
    assert "SetPass Calls" in html
    assert "Avg: 40" in html
    assert "Batches" in html
    assert "Avg: 120" in html
    assert "GC Alloc/Frame" in html
    assert "Avg: 8.25 KB" in html
    assert "Unity Memory" in html
    assert "Avg: 900.0 MB" in html
    assert "Mono / GC Memory" in html
    assert "Avg: 120.0 MB" in html
    assert "Native Memory" in html
    assert "Avg: 780.0 MB" in html
    assert "Avg: 230.5 k" in html
    assert "Avg: 231.5 k" in html
    # 导出 HTML 的曲线也要包含新增计数器
    assert "Render Thread (ms)" in html
    assert "CPU Total (ms)" in html
    assert "GC Alloc (KB)" in html
    assert "Shadow Casters" in html


def test_android_report_top_cards_hide_unity_without_data(tmp_path):
    html = _render_android_report(tmp_path, {})

    assert "Unity CPU Frame" not in html
    assert "Unity GPU Frame" not in html
    assert "Unity Total Frame" not in html
    assert "Unity Render Thread" not in html
    assert "Mono / GC Memory" not in html
    assert "Unity Heap" not in html


def test_android_report_memory_chart_includes_unity_curves(tmp_path):
    html = _render_android_report(
        tmp_path,
        {
            "used_memory_mb": 1000.5,
            "native_memory_mb": 880.5,
            "gc_used_memory_mb": 120.5,
        },
        unity_stats_charts={
            "used_memory_mb": [{"x": 0.0, "y": 1000.5}],
            "native_memory_mb": [{"x": 0.0, "y": 880.5}],
            "gc_used_memory_mb": [{"x": 0.0, "y": 120.5}],
        },
    )

    assert "Unity Heap" in html
    assert "Unity Native" in html
    assert "Unity Mono/GC" in html
    assert "Unity Heap: 1000.5 MB" in html
    assert "Native: 880.5 MB" in html
    assert "Mono: 120.5 MB" in html


def test_list_other_scenes_ignores_non_report_entries(tmp_path):
    report = tmp_path / "report"
    report.mkdir()
    for scene in ("scene1", "scene2"):
        scene_dir = report / scene
        scene_dir.mkdir()
        (scene_dir / "result.json").write_text("{}")
    # 非报告目录与导出产物不应出现在对比选项里
    (report / "loose_dir").mkdir()
    (report / "export.json").write_text("{}")
    (report / "export.mkv").write_text("")

    with patch("os.getcwd", return_value=str(tmp_path)):
        manager = ReportManager()
    assert manager.list_other_scenes("scene1") == ["scene2"]
    # 传入的排除场景不在列表里时也不应抛错
    assert manager.list_other_scenes("missing") == ["scene2", "scene1"]


def test_list_other_scenes_filters_app_platform_and_model(tmp_path):
    import json

    report = tmp_path / "report"
    report.mkdir()
    reports = {
        "normal_a": {"app": "com.a", "platform": "Android", "model": "normal"},
        "normal_a2": {"app": "com.a", "platform": "Android", "model": "normal"},
        "pk_a": {"app": "com.a", "platform": "Android", "model": "pk"},
        "ios_a": {"app": "com.a", "platform": "iOS", "model": "normal"},
        "normal_b": {"app": "com.b", "platform": "Android", "model": "normal"},
    }
    for scene, meta in reports.items():
        scene_dir = report / scene
        scene_dir.mkdir()
        (scene_dir / "result.json").write_text(json.dumps(meta), encoding="utf-8")

    with patch("os.getcwd", return_value=str(tmp_path)):
        manager = ReportManager()

    assert manager.list_other_scenes(
        "normal_a", app="com.a", platform="Android", model="normal"
    ) == ["normal_a2"]
    # 不传过滤条件时保持旧行为（所有完整报告）
    assert set(manager.list_other_scenes("normal_a")) == set(reports) - {"normal_a"}


def test_analysis_page_cards_cover_recorded_unity_metrics(tmp_path, monkeypatch):
    """单报告页顶部卡片也要显示已采集的 Unity 计数器。"""
    import json
    from perfcattle.report.manager import ReportStore
    from perfcattle.view import pages as PAGES
    from perfcattle.web import app

    report = tmp_path / "report"
    scene_dir = report / "run"
    scene_dir.mkdir(parents=True)
    (scene_dir / "result.json").write_text(
        json.dumps(
            {
                "app": "com.test",
                "platform": "Android",
                "model": "normal",
                "devices": "device",
                "ctime": "2026-01-01-00-00-00",
            }
        ),
        encoding="utf-8",
    )
    (scene_dir / "unity_stats.log").write_text(
        '10:00:00={"cpu_frame_ms":16.7,"gpu_frame_ms":12.5,'
        '"render_frame_ms":7.1,"cpu_total_ms":25.0,"draw_calls":300,'
        '"setpass_calls":40,"batches":120,"static_batches":80,'
        '"dynamic_batches":20,"instanced_batches":20,"shadow_casters":12,'
        '"triangles":230000,"vertices":231000,"gc_alloc_kb":10.0,'
        '"used_memory_mb":900.0,"gc_used_memory_mb":120.0,"frame":1}\n',
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    store = ReportStore()
    store.report_dir = str(report)
    store._log_io.report_dir = str(report)
    monkeypatch.setattr(PAGES, "f", store)

    body = app.test_client().get(
        "/analysis?platform=Android&lan=cn&scene=run&app=com.test"
    ).get_data(as_text=True)

    for label in (
        "Unity 总帧耗时",
        "Unity 渲染线程",
        "SetPass Calls",
        "Batches",
        "GC Alloc/帧",
        "Unity 已用内存",
        "Mono / GC 内存",
        "Native 内存",
    ):
        assert label in body
    # 曲线汇总和导出脚本里也要带上新增字段
    assert "render_frame_ms" in body
    assert "gc_used_memory_mb" in body
    assert "shadow_casters" in body
