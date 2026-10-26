# -*- encoding: utf-8 -*-
"""Regression tests for collection/recording stability."""
import importlib
import threading
import time
from pathlib import Path

from flask import Flask
from perfcattle.collector.base import CollectorSampler
from perfcattle.core.metrics import MetricStatus
from perfcattle.report import manager as report_manager_module
from perfcattle.report.manager import ReportManager
from perfcattle.service import monitor as monitor_module
from perfcattle.service.monitor import AppPerformanceMonitor
from perfcattle.tools.recording import Scrcpy

report_api_module = importlib.import_module("perfcattle.view.report_api")


def test_unified_sampler_returns_metric_samples_for_success_and_cache():
    calls = []

    def sample():
        calls.append(len(calls) + 1)
        return calls[-1]

    sampler = CollectorSampler(
        name="cpu", sample_func=sample, interval=0.2, stale_ttl=1.0,
        background=False, unified=True, unit="percent", source="test",
    )
    first = sampler.sample_once()
    second = sampler.sample()
    assert first.status is MetricStatus.OK
    assert first.value == 1
    assert second.status is MetricStatus.CACHED
    assert second.value == 1
    assert second.stale_age_ms is not None
    assert calls == [1]


def test_unified_sampler_returns_failure_and_expired_cache():
    calls = []

    def sample():
        calls.append(1)
        if len(calls) == 1:
            return 7
        raise RuntimeError("offline")

    sampler = CollectorSampler(
        name="cpu", sample_func=sample, interval=0.2, stale_ttl=0.01,
        background=False, unified=True,
    )
    assert sampler.sample_once().status is MetricStatus.OK
    time.sleep(0.22)
    failed = sampler.sample()
    assert failed.status is MetricStatus.DISCONNECTED
    assert failed.value is None
    assert failed.error == "offline"


def test_background_sampler_staggers_and_serves_cached_value():
    calls = []

    def sample():
        calls.append(threading.current_thread().name)
        return len(calls)

    sampler = CollectorSampler(
        name="cpu",
        sample_func=sample,
        interval=0.2,
        stale_ttl=1.0,
        background=True,
        phase_offset=0.05,
    )

    try:
        started = time.perf_counter()
        assert sampler.get_or_sample() == 1
        assert time.perf_counter() - started >= 0.02
        assert calls[0].startswith("perfcattle-sampler-cpu")
        assert sampler.get_or_sample() == 1
        assert calls == ["perfcattle-sampler-cpu"]
    finally:
        sampler.stop()


def test_collect_all_keeps_recording_in_parent_process(monkeypatch):
    events = []

    class FakeFile:
        def clear_file(self):
            events.append(("clear_file",))

        def begin_session(self):
            events.append(("begin_session",))

    class FakeScrcpy:
        @staticmethod
        def start_record(device):
            events.append(("start_record", device))
            return 0

        @staticmethod
        def stop_record():
            events.append(("stop_record",))

    class FakeFuture:
        def result(self):
            events.append(("result",))

    class FakeExecutor:
        instances = []

        def __init__(self, max_workers, thread_name_prefix=None):
            self.max_workers = max_workers
            self.thread_name_prefix = thread_name_prefix
            self.tasks = []
            FakeExecutor.instances.append(self)
            events.append(("executor", max_workers, thread_name_prefix))

        def submit(self, func):
            self.tasks.append(func)
            events.append(("task", func.__name__))
            return FakeFuture()

        def shutdown(self, wait=True, cancel_futures=False):
            events.append(("shutdown", wait, cancel_futures))

    def fake_wait(futures):
        events.append(("wait", len(futures)))

    monitor = AppPerformanceMonitor.__new__(AppPerformanceMonitor)
    monitor.record = True
    monitor.deviceId = "device-1"
    monitor.collectCpu = lambda: None
    monitor.collectMemory = lambda: None
    monitor.collectMemoryDetail = lambda: None
    monitor.collectBattery = lambda: None
    monitor.collectFps = lambda: None
    monitor.collectNetwork = lambda: None
    monitor.collectFileIO = lambda: None
    monitor.collectGpu = lambda: None
    monitor.collectThermal = lambda: None
    monitor.setPerfs = lambda report_path=None: events.append(
        ("set_perfs", report_path)
    )

    monkeypatch.setattr(monitor_module, "f", FakeFile())
    monkeypatch.setattr(monitor_module, "Scrcpy", FakeScrcpy)
    monkeypatch.setattr(monitor_module, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(monitor_module, "wait", fake_wait)

    monitor.collectAll(report_path="report.html")

    assert FakeExecutor.instances[0].max_workers == 9
    assert len(FakeExecutor.instances[0].tasks) == 9
    assert "start_record" not in [
        func.__name__ for func in FakeExecutor.instances[0].tasks
    ]
    assert ("wait", 9) in events
    # The recording start must be recorded before collectors begin writing logs.
    assert events.index(("clear_file",)) < events.index(("begin_session",))
    assert events.index(("stop_record",)) < events.index(
        ("set_perfs", "report.html")
    )


def test_report_cleanup_stops_recording_before_removing_files(monkeypatch, tmp_path):
    events = []
    report_manager = ReportManager()
    report_manager.report_dir = str(tmp_path)
    (tmp_path / "record.mp4").write_text("locked while recording", encoding="utf-8")

    monkeypatch.setattr(
        Scrcpy, "stop_record", staticmethod(lambda: events.append(("stop_record",)))
    )
    monkeypatch.setattr(
        report_manager_module.os,
        "remove",
        lambda path: events.append(("remove", Path(path).name)),
    )

    report_manager.clear_file()

    assert events == [("stop_record",), ("remove", "record.mp4")]


def test_report_batch_delete_removes_all_selected_reports(monkeypatch, tmp_path):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    for scene in ("scene-a", "scene-b", "scene-c"):
        scene_dir = report_dir / scene
        scene_dir.mkdir()
        (scene_dir / "result.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(report_api_module.f, "report_dir", str(report_dir))
    app = Flask(__name__)
    app.register_blueprint(report_api_module.report_api)

    response = app.test_client().post(
        "/apm/remove/report",
        data={"scenes": ["scene-a", "scene-b"]},
    )

    assert response.json["status"] == 1
    assert response.json["removed"] == ["scene-a", "scene-b"]
    assert not (report_dir / "scene-a").exists()
    assert not (report_dir / "scene-b").exists()
    assert (report_dir / "scene-c").exists()

    response = app.test_client().post(
        "/apm/remove/report",
        data={"scenes": ["."]},
    )

    assert response.json["status"] == 0
    assert report_dir.exists()
