"""Compare report HTML export: self-contained summary tables and charts."""
import os
import sys

from perfcattle.web import app
from tests.test_compare_export import _make_store, _write_report

REPORT_API = sys.modules["perfcattle.view.report_api"]


def _read(path):
    with open(path, encoding="utf-8") as fp:
        return fp.read()


def test_compare_html_embeds_summary_and_charts(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    report_dir = tmp_path / "report"
    _write_report(report_dir, "run_a")
    _write_report(report_dir, "run_b")

    path = store.make_compare_html("Android", ["run_a", "run_b"], lan="cn")

    assert path.endswith(".html")
    assert os.path.isfile(path)
    html = _read(path)

    # Metadata and scene cards.
    assert "run_a" in html
    assert "run_b" in html
    assert "指标汇总" in html
    assert "12.5%" in html

    # Unity/GPU sections come from the metric catalog, including the derived
    # native memory and the Mono/GC counter.
    assert "Unity 帧耗时" in html
    assert "Unity 批处理" in html
    assert "Mono / GC 已用内存 (MB)" in html
    assert "Native 内存 (MB)" in html
    assert "GPU 扩展" in html

    # Chart data is embedded (no call back to /apm/log/compare_multi) and every
    # chart family the compare page shows is present.
    assert "const compareChartGroups =" in html
    assert "/apm/log/compare_multi" not in html
    for chart_id in (
        "chart-cpu-app",
        "chart-fps",
        "chart-unity-cpu-frame",
        "chart-unity-cpu-total-ms",
        "chart-unity-gc-alloc-kb",
        "chart-gpu-clock",
    ):
        assert chart_id in html

    # Missing collectors do not create misleading rows or cards.
    assert "电池温度" not in html
    assert "温度传感器" not in html


def test_compare_html_supports_english_labels(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    report_dir = tmp_path / "report"
    _write_report(report_dir, "run_a")
    _write_report(report_dir, "run_b")

    path = store.make_compare_html("Android", ["run_a", "run_b"], lan="en")
    html = _read(path)

    assert "Metrics Summary" in html
    assert "Unity Memory / GC" in html
    assert "Mono / GC Memory (MB)" in html
    assert "GPU Details" in html


def test_compare_html_uses_unique_paths(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    report_dir = tmp_path / "report"
    _write_report(report_dir, "run_a")
    _write_report(report_dir, "run_b")

    first = store.make_compare_html("Android", ["run_a", "run_b"])
    second = store.make_compare_html("Android", ["run_a", "run_b"])

    assert first != second
    assert os.path.isfile(first)
    assert os.path.isfile(second)


def test_compare_html_endpoint(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    report_dir = tmp_path / "report"
    _write_report(report_dir, "run_a")
    _write_report(report_dir, "run_b")
    monkeypatch.setattr(REPORT_API, "f", store)

    client = app.test_client()
    payload = client.get(
        "/apm/export/html/compare?scenes=run_a&scenes=run_b&platform=Android&lan=cn"
    ).get_json()

    assert payload["status"] == 1
    assert payload["path"].endswith(".html")
    assert os.path.isfile(payload["path"])

    single = client.get(
        "/apm/export/html/compare?scenes=run_a&platform=Android"
    ).get_json()
    assert single["status"] == 0
    assert "2-6" in single["msg"]
