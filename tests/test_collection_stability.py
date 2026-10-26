# -*- encoding: utf-8 -*-
"""Regression tests for collection/recording stability."""
import importlib
import threading
import time
from pathlib import Path

from flask import Flask
from perfcattle.collector.base import CachedSample, CollectorSampler, ReusableCollector
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


def test_unified_sampler_does_not_refresh_ttl_for_cached_sample_fallback(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("perfcattle.collector.base.time.time", lambda: clock[0])
    monotonic = [10.0]
    monkeypatch.setattr("perfcattle.collector.base.time.monotonic", lambda: monotonic[0])
    calls = []

    def sample():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            return 7
        return CachedSample(7)

    sampler = CollectorSampler(
        name="gpu", sample_func=sample, interval=0.1, stale_ttl=1.0,
        background=False, unified=True,
    )
    assert sampler.sample_once().status is MetricStatus.OK
    original_wall = sampler.last_sample_time
    original_mono = sampler.last_sample_monotonic
    clock[0] = 100.5
    monotonic[0] = 10.5
    cached = sampler.sample_once()
    assert cached.status is MetricStatus.CACHED
    assert cached.value == 7
    assert sampler.last_sample_time == original_wall
    assert sampler.last_sample_monotonic == original_mono
    assert sampler.consecutive_failures == 0
    clock[0] = 101.1
    monotonic[0] = 11.1
    missing = sampler.sample()
    assert missing.status is MetricStatus.MISSING
    assert missing.value is None
    assert calls == [1, 2, 3]


def test_unified_sampler_preserves_real_zero_as_success():
    sampler = CollectorSampler(
        name="gpu", sample_func=lambda: 0, interval=0.1, stale_ttl=1.0,
        background=False, unified=True,
    )
    result = sampler.sample_once()
    assert result.status is MetricStatus.OK
    assert result.value == 0


def test_unified_sampler_busy_returns_cached_sample_with_stale_age():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def sample():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            return 7
        entered.set()
        release.wait(timeout=1)
        return 8

    sampler = CollectorSampler(
        name="cpu", sample_func=sample, interval=0.2, stale_ttl=1.0,
        background=False, unified=True,
    )
    assert sampler.sample_once().status is MetricStatus.OK
    worker = threading.Thread(target=sampler.sample_once)
    worker.start()
    assert entered.wait(timeout=1)
    busy = sampler.sample_once()
    release.set()
    worker.join(timeout=1)
    assert busy.status is MetricStatus.CACHED
    assert busy.value == 7
    assert busy.stale_age_ms is not None


def test_reusable_collector_preserves_none_for_environment_override(monkeypatch):
    monkeypatch.setenv("PERFCATTLE_UNIFIED_SAMPLER", "true")

    class Collector(ReusableCollector):
        pass

    collector = Collector()
    sampler = collector._get_sampler("cpu", lambda: 1, background=False)
    assert sampler.unified is True


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
    assert failed.status is MetricStatus.MISSING
    assert failed.value is None
    assert failed.error == "offline"
    assert failed.stale_age_ms is None


def test_get_latest_compatibly_estimates_missing_monotonic_timestamp():
    sampler = CollectorSampler(
        name="cpu", sample_func=lambda: 5, interval=0.1, stale_ttl=1.0,
        background=False, unified=True,
    )
    sampler.sample_once()
    sampler.last_sample_monotonic = None
    latest = sampler.get_latest(max_age=1.0)
    assert latest == 5
    assert sampler.last_sample_monotonic is None


def test_missing_monotonic_timestamp_is_not_fresh_after_wall_clock_jump(monkeypatch):
    sampler = CollectorSampler(
        name="cpu", sample_func=lambda: 5, interval=0.1, stale_ttl=1.0,
        background=False, unified=True,
    )
    sampler.sample_once()
    sampler.last_sample_monotonic = None
    monkeypatch.setattr("perfcattle.collector.base.time.time", lambda: sampler.last_sample_time - 10)
    assert sampler.get_latest(max_age=1.0) is None


def test_missing_monotonic_attribute_is_safe(monkeypatch):
    sampler = CollectorSampler(
        name="cpu", sample_func=lambda: 5, interval=0.1, stale_ttl=1.0,
        background=False, unified=True,
    )
    sampler.sample_once()
    del sampler.last_sample_monotonic
    assert sampler.get_latest(max_age=1.0) == 5
    monkeypatch.setattr("perfcattle.collector.base.time.time", lambda: sampler.last_sample_time - 10)
    assert sampler.get_latest(max_age=1.0) is None


def test_wait_for_latest_returns_cached_sample_without_rewrapping(monkeypatch):
    sampler = CollectorSampler(
        name="cpu", sample_func=lambda: 5, interval=0.1, stale_ttl=1.0,
        background=False, unified=True,
    )
    sampler.sample_once()
    monkeypatch.setattr(sampler, "start", lambda: None)
    result = sampler._wait_for_latest()
    assert result.status is MetricStatus.CACHED
    assert result.value == 5
    assert result.stale_age_ms is not None


def test_adb_error_kind_reaches_metric_sample_error():
    from perfcattle.core.adb_client import ADBCommandError
    from perfcattle.core.command_result import CommandResult

    def sample():
        raise ADBCommandError(CommandResult(ok=False, error_kind="timeout", stderr="adb timeout"))

    sampler = CollectorSampler(
        name="cpu", sample_func=sample, interval=0.1, stale_ttl=1.0,
        background=False, unified=True,
    )
    result = sampler.sample_once()
    assert result.status is MetricStatus.MISSING
    assert result.error == "timeout: adb timeout"


def test_unified_sampler_uses_missing_when_cache_ttl_expires():
    calls = []

    def sample():
        calls.append(1)
        if len(calls) == 1:
            return 3
        raise RuntimeError("offline")

    sampler = CollectorSampler(
        name="cpu", sample_func=sample, interval=0.01, stale_ttl=0.01,
        background=False, unified=True,
    )
    assert sampler.sample_once().status is MetricStatus.OK
    sampler.last_sample_monotonic -= 1.0
    expired = sampler.sample()
    assert expired.status is MetricStatus.MISSING
    assert expired.value is None


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
    monitor.collectGpuStats = lambda: None
    monitor.collectThermal = lambda: None
    monitor.setPerfs = lambda report_path=None: events.append(
        ("set_perfs", report_path)
    )

    monkeypatch.setattr(monitor_module, "f", FakeFile())
    monkeypatch.setattr(monitor_module, "Scrcpy", FakeScrcpy)
    monkeypatch.setattr(monitor_module, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(monitor_module, "wait", fake_wait)

    monitor.collectAll(report_path="report.html")

    assert FakeExecutor.instances[0].max_workers == 10
    assert len(FakeExecutor.instances[0].tasks) == 10
    assert "start_record" not in [
        func.__name__ for func in FakeExecutor.instances[0].tasks
    ]
    assert ("wait", 10) in events
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


def test_background_sampler_falls_back_to_sync_sample_before_first_cache():
    """A first poll must not surface "no cached value" while the background
    thread is still queued behind the shared ADB semaphore."""
    from perfcattle.collector.base import CollectorSampler

    sampler = CollectorSampler(
        "startup", lambda: 7.5, background=True, unified=False
    )
    # Simulate a background thread that has not produced a value yet.
    sampler.start = lambda: None
    sampler._wait_for_latest = lambda: (_ for _ in ()).throw(
        RuntimeError("startup sampler has no cached value")
    )

    assert sampler.get_or_sample() == 7.5
    sampler.stop()
