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
    assert invalid["status"] is MetricStatus.INVALID


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
    monkeypatch.setattr("perfcattle.collector.disk.adb_shell", lambda *a, **k: next(outputs))
    result = disk.getAndroidDisk()
    assert result["disk_size"] == 1000
    assert result["mount_points"] == ["/data"]
    assert result["snapshot_only"] is True


def test_permission_failure_does_not_become_valid_zero(monkeypatch):
    disk = Disk("device")
    monkeypatch.setattr("perfcattle.collector.disk.adb_shell", MagicMock(side_effect=PermissionError("denied")))
    with pytest.raises(PermissionError):
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
