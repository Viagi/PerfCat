"""Quality and semantics regressions for system collectors."""
from unittest.mock import MagicMock

import pytest

from perfcattle.collector.cpu import CPU
from perfcattle.collector.disk import Disk
from perfcattle.collector.gpu_stats import GpuStats
from perfcattle.collector.network import Network
from perfcattle.collector.memory import Memory
from perfcattle.collector.file_io import FileIO
from perfcattle.collector.fps import FPS
from perfcattle.collector.thermal import ThermalSensor
from perfcattle.collector.gpu import GPU
from perfcattle.core.metrics import MetricSample, MetricStatus
from perfcattle.core.platform import Platform


def test_proc_stat_parser_handles_spaces_and_right_parenthesis():
    stat = "123 (worker name ) with ) S 0 0 0 0 0 0 0 0 0 0 11 7 0 0"
    assert CPU._parse_process_stat(stat) == 18


def test_cpu_rate_rejects_negative_and_over_total_deltas():
    cpu = CPU("app", "device", pid="123")
    first = {"total": 100, "idle": 50, "process": 10}
    assert cpu._calculate_cpu_rate(first, {"total": 110, "idle": 55, "process": 5}) is None
    assert cpu._calculate_cpu_rate(first, {"total": 110, "idle": 55, "process": 25}) is None


def test_gpu_cumulative_counter_uses_delta_and_marks_reset_invalid():
    stats = GpuStats("device")
    stats.previous_busy = 100
    stats.previous_total = 200
    assert stats._parse_counter_sample("150 300")["value"] == 50.0
    invalid = stats._parse_counter_sample("90 250")
    assert invalid["status"] == MetricStatus.INVALID.value


def test_network_uid_totals_are_not_claimed_as_transport():
    network = Network("app", "device", pid="123")
    network._uid_stat_supported = True
    network._read_uid = lambda: 10001
    network._read_uid_stat_counter = lambda uid, filename: "1024"
    assert network._get_android_net_totals(wifi=True) == (1.0, 1.0)
    assert network.last_scope == "uid_total"


def test_disk_df_fallback_filters_overlay_tmpfs_and_keeps_mount_metadata(monkeypatch):
    disk = Disk("device")
    outputs = iter([
        "Filesystem 1024-blocks Used Available Use% Mounted on\n",
        "overlay 100 20 80 20% /\n"
        "tmpfs 200 30 170 15% /dev\n"
        "/dev/block/data 1000 400 600 40% /data\n",
    ])
    monkeypatch.setattr("perfcattle.collector.disk.adb_read_optional", lambda *a, **k: next(outputs))
    result = disk.getAndroidDisk()
    assert result["disk_size"] == 1000
    assert result["mount_points"] == ["/data"]
    assert result["snapshot_only"] is True


def test_permission_failure_does_not_become_valid_zero(monkeypatch):
    Disk.clear()
    disk = Disk("device-denied")
    # df is a probe: a denied/unreadable df yields no rows, which must surface
    # as an unavailable disk sample rather than a fabricated zero.
    monkeypatch.setattr(
        "perfcattle.collector.disk.adb",
        MagicMock(shell=MagicMock(side_effect=PermissionError("denied"))),
    )
    with pytest.raises(ValueError, match="invalid df output"):
        disk.getAndroidDisk()


def test_public_error_paths_expose_status_without_fake_zero(monkeypatch):
    network = Network("app", "device", pid="123")
    network.getAndroidNet = lambda wifi=True: (None, None)
    assert network.getNetWorkData(noLog=True) == (None, None)
    assert network.last_sample["status"] == "missing"

    file_io = FileIO("app", "device", pid="123")
    file_io.getAndroidFileIO = lambda: (None, None)
    assert file_io.getFileIOData(noLog=True) == (None, None)
    assert file_io.last_sample["status"] == "missing"

    thermal = ThermalSensor("device")
    thermal._sampled_value = lambda *args, **kwargs: []
    assert thermal.getThermalTemp() == []
    assert thermal.last_sample["status"] == "missing"


def test_cpu_public_invalid_delta_is_not_unpackable(monkeypatch):
    cpu = CPU("app", "device", pid="123")
    monkeypatch.setattr(cpu, "_read_cpu_rate_sample", lambda: {"time": 1, "process": 2, "total": 1, "idle": 1})
    cpu._last_cpu_sample = {"time": 0, "process": 1, "total": 1, "idle": 1}
    with pytest.raises(RuntimeError, match="invalid CPU delta"):
        cpu._sample_android_cpu_rate()


def test_public_unified_boundaries_preserve_zero_and_status(monkeypatch):
    cpu = CPU("app", "device", pid="123")
    monkeypatch.setattr(cpu, "getAndroidCpuRate", lambda noLog=False: (0.0, 0.0))
    cpu_sample = cpu.getCpuRate(unified=True)
    assert cpu_sample["app"]["value"] == 0.0
    assert cpu_sample["app"]["status"] == "ok"

    memory = Memory("app", "device", pid="123")
    monkeypatch.setattr(memory, "getAndroidMemory", lambda: (0.0, 0.0))
    memory_sample = memory.getProcessMemory(noLog=True, unified=True)
    assert memory_sample["total"]["value"] == 0.0
    assert memory_sample["total"]["status"] == "ok"


def test_gpu_stats_sample_is_structured_for_every_metric(monkeypatch):
    stats = GpuStats("device")
    stats._sources = {"gpu_util": ("kgsl_gpubusy", "/x", "window_ratio")}
    monkeypatch.setattr(stats, "probe", lambda: None)
    values = iter(["100 200", "100 300"])
    monkeypatch.setattr("perfcattle.collector.gpu_stats._read", lambda *args: next(values))
    monkeypatch.setattr(stats, "_gpu_memory", lambda: {"gpu_mem_total": 0.0})
    monkeypatch.setattr(stats._bandwidth, "sample", lambda: {"gpu_read_mb": 0.0})
    stats.sample()
    result = stats.sample()
    assert result["gpu_util"]["value"] == 33.33
    assert result["gpu_mem_total"]["value"] == 0.0
    assert result["gpu_read_mb"]["value"] == 0.0


def test_ios_fps_jank_is_unavailable_not_zero(monkeypatch):
    fps = FPS("app", "device", Platform.iOS, "surface")
    monkeypatch.setattr(fps, "getiOSFps", lambda noLog=False: (60, None))
    assert fps.getFPS(noLog=True) == (60, None)
