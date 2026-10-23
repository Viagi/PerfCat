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
