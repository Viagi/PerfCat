"""Report package export must include raw APM logs and a manifest."""
import json
import zipfile

from perfcattle.report.package_export import ReportPackageExporter
from perfcattle.report.manager import ReportStore
from tests.test_compare_export import _write_report
from tests.test_unity_hotspots import _stream


def test_single_report_export_includes_raw_logs_and_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "report").mkdir()
    store = ReportStore()
    report_dir = tmp_path / "report"
    raw_path = _stream(tmp_path)
    _write_report(report_dir, "run_a", raw_path=raw_path)

    exporter = ReportPackageExporter(log_io=store)
    result = exporter.export("Android", "run_a")

    assert result["package"] is True
    with zipfile.ZipFile(result["path"]) as bundle:
        names = set(bundle.namelist())
        for name in (
            "cpu_app.log",
            "cpu_sys.log",
            "mem_total.log",
            "fps.log",
            "unity_stats.log",
            "manifest.json",
            "result.json",
        ):
            assert name in names, name

        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
        assert manifest["format"] == "perfcattle-report-manifest"
        paths = {entry["path"]: entry for entry in manifest["files"]}
        assert "cpu_app.log" in paths
        assert paths["cpu_app.log"]["lines"] == 2
