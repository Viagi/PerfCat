"""Focused collector regressions for adb-backed metrics."""
from unittest.mock import MagicMock

import pytest

from perfcattle.collector import cpu as cpu_module
from perfcattle.collector import fps as fps_module
from perfcattle.collector import gpu as gpu_module
from perfcattle.collector import memory as memory_module
from perfcattle.collector import network as network_module
from perfcattle.collector import thermal as thermal_module
from perfcattle.collector.cpu import CPU
from perfcattle.collector.file_io import FileIO
from perfcattle.collector.fps import FPS
from perfcattle.collector.gpu import GPU
from perfcattle.collector.memory import Memory
from perfcattle.collector.network import Network
from perfcattle.collector.thermal import ThermalSensor
from perfcattle.report.log_io import LogIO
from perfcattle.core.metrics import MetricSample, MetricStatus


@pytest.fixture(autouse=True)
def clear_collector_caches():
    FPS.clear()
    CPU.clear()
    Memory.clear()
    Network.clear()
    FileIO.clear()
    GPU.clear()
    ThermalSensor.clear()
    yield
    FPS.clear()
    CPU.clear()
    Memory.clear()
    Network.clear()
    FileIO.clear()
    GPU.clear()
    ThermalSensor.clear()


def process_stat(utime, stime):
    return f"123 (app) S 0 0 0 0 0 0 0 0 0 0 {utime} {stime} 0 0"


CPU_STAT_1 = "\n".join(
    [
        "cpu 100 0 50 850 0 0 0 0 0 0",
        "cpu0 50 0 25 425 0 0 0 0 0 0",
        "cpu1 50 0 25 425 0 0 0 0 0 0",
    ]
)
CPU_STAT_2 = "\n".join(
    [
        "cpu 140 0 80 900 0 0 0 0 0 0",
        "cpu0 70 0 40 450 0 0 0 0 0 0",
        "cpu1 70 0 40 450 0 0 0 0 0 0",
    ]
)


MEMINFO_OUTPUT = """
Applications Memory Usage (in Kilobytes):
Java Heap:      512
Native Heap:    256
Code:           128
Stack:           64
Graphics:        32
Private Other:   16
System:           8
TOTAL          2048
TOTAL SWAP PSS: 1024
"""


def test_android_cpu_parses_proc_stat_without_shell_pipe(monkeypatch):
    monkeypatch.setattr(cpu_module.time, "sleep", lambda _: None)
    mock_adb = MagicMock()
    stat_outputs = iter([CPU_STAT_1, CPU_STAT_2])
    process_outputs = iter([process_stat(10, 5), process_stat(20, 7)])
    commands = []

    def shell(cmd, deviceId, timeout=None):
        commands.append(cmd)
        if cmd == "cat /proc/123/stat":
            return next(process_outputs)
        if cmd == "cat /proc/stat":
            return next(stat_outputs)
        return ""

    mock_adb.shell.side_effect = shell
    monkeypatch.setattr(cpu_module, "adb", mock_adb)

    assert CPU("com.example", "device-1", pid="123").getAndroidCpuRate(
        noLog=True
    ) == (10, 58.33)
    assert all("|" not in command for command in commands)


def test_android_cpu_reuses_last_value_after_transient_failure(monkeypatch):
    monkeypatch.setattr(cpu_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(CPU, "_process_exists", lambda self, devices: True)
    monkeypatch.setattr(CPU, "_refresh_pid", lambda self, devices=None: False)
    mock_adb = MagicMock()
    outputs = iter(
        [
            process_stat(10, 5),
            CPU_STAT_1,
            process_stat(20, 7),
            CPU_STAT_2,
            "",
        ]
    )
    mock_adb.shell.side_effect = lambda cmd, deviceId, timeout=None: next(outputs)
    monkeypatch.setattr(cpu_module, "adb", mock_adb)
    cpu = CPU("com.example", "device-1", pid="123")

    assert cpu.getAndroidCpuRate(noLog=True) == (10, 58.33)
    assert cpu.getAndroidCpuRate(noLog=True) == (10, 58.33)


def test_android_network_reuses_last_value_after_parse_failure(monkeypatch):
    monkeypatch.setattr(network_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(Network, "_process_exists", lambda self, devices: True)
    monkeypatch.setattr(Network, "_refresh_pid", lambda self, devices=None: False)
    mock_adb = MagicMock()
    send_outputs = iter(["2048", "4096", "", ""])
    recv_outputs = iter(["1024", "3072", "", ""])

    def shell(cmd, deviceId, timeout=None):
        if cmd == "cat /proc/123/status":
            return "Uid:\t10001\t10001\t10001\t10001\n"
        if cmd == "cat /proc/uid_stat/10001/tcp_snd":
            return next(send_outputs)
        if cmd == "cat /proc/uid_stat/10001/tcp_rcv":
            return next(recv_outputs)
        return ""

    mock_adb.shell.side_effect = shell
    monkeypatch.setattr(network_module, "adb", mock_adb)
    network = Network("com.example", "device-1", pid="123")

    assert network.getAndroidNet() == (2, 2)
    assert network.getAndroidNet() == (2, 2)


def test_android_thermal_filters_game_temperature_zones(monkeypatch):
    mock_adb = MagicMock()
    mock_adb.shell.return_value = (
        "__ZONES__\n"
        "/sys/class/thermal/thermal_zone0\n"
        "/sys/class/thermal/thermal_zone1\n"
        "/sys/class/thermal/thermal_zone8\n"
        "/sys/class/thermal/thermal_zone9\n"
        "/sys/class/thermal/thermal_zone10\n"
        "__TYPES__\n"
        "pm8550-bcl-lvl0\n"
        "cpuss-0\n"
        "ddr\n"
        "battery\n"
        "gpuss-1\n"
        "__TEMPS__\n"
        "0\n"
        "45000\n"
        "40100\n"
        "31300\n"
        "42000\n"
    )
    monkeypatch.setattr(thermal_module, "adb", mock_adb)

    assert ThermalSensor("device-1").getThermalTemp() == [
        {
            "name": "CPU",
            "type": "CPU",
            "zone": "thermal_zone1",
            "source_type": "cpuss-0",
            "temp": "45000",
        },
        {
            "name": "GPU",
            "type": "GPU",
            "zone": "thermal_zone10",
            "source_type": "gpuss-1",
            "temp": "42000",
        },
        {
            "name": "Memory",
            "type": "Memory",
            "zone": "thermal_zone8",
            "source_type": "ddr",
            "temp": "40100",
        },
        {
            "name": "Battery",
            "type": "Battery",
            "zone": "thermal_zone9",
            "source_type": "battery",
            "temp": "31300",
        },
    ]


def test_android_thermal_writes_grouped_temperature_logs(monkeypatch, tmp_path):
    mock_adb = MagicMock()
    mock_adb.shell.return_value = (
        "__ZONES__\n"
        "/sys/class/thermal/thermal_zone1\n"
        "/sys/class/thermal/thermal_zone2\n"
        "__TYPES__\n"
        "cpuss-0\n"
        "gpuss-0\n"
        "__TEMPS__\n"
        "45000\n"
        "39000\n"
    )
    monkeypatch.setattr(thermal_module, "adb", mock_adb)
    log_io = LogIO()
    log_io.report_dir = str(tmp_path)
    monkeypatch.setattr(ThermalSensor, "_get_report_store", lambda self: log_io)

    ThermalSensor("device-1").getThermalTemp(noLog=False)

    assert (tmp_path / "thermal_cpu.log").read_text(encoding="utf-8").endswith(
        "=45.0\n"
    )
    assert (tmp_path / "thermal_gpu.log").read_text(encoding="utf-8").endswith(
        "=39.0\n"
    )


def test_android_memory_detail_reuses_recent_meminfo_sample(monkeypatch):
    mock_adb = MagicMock()
    commands = []

    def shell(cmd, deviceId, timeout=None):
        commands.append(cmd)
        return MEMINFO_OUTPUT

    mock_adb.shell.side_effect = shell
    monkeypatch.setattr(memory_module, "adb", mock_adb)
    monkeypatch.setattr(memory_module.time, "monotonic", lambda: 100)
    memory = Memory("com.example", "device-1", pid="123")

    assert memory.getAndroidMemory() == (2, 1)
    assert memory.getAndroidMemoryDetail(noLog=True)["java_heap"] == 0.5
    assert commands == ["dumpsys meminfo 123"]


def test_android_fps_without_cache_raises_instead_of_fabricating_zero(monkeypatch):
    monkeypatch.setattr(fps_module.time, "sleep", lambda _: None)

    class FakeMonitor:
        frequency = 0

        def __init__(self):
            self.collect_fps = 0
            self.collect_jank = 0

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(fps_module, "FPSMonitor", lambda **kwargs: FakeMonitor())
    fps = FPS("com.example", "device-1")

    with pytest.raises(RuntimeError, match="no valid FPS sample"):
        fps.getAndroidFps(noLog=True)


def test_android_fps_reuses_last_value_when_sample_has_no_frames(monkeypatch):
    monkeypatch.setattr(fps_module.time, "sleep", lambda _: None)

    class FakeMonitor:
        frequency = 0

        def __init__(self, result):
            self.result = result
            self.collect_fps = 0
            self.collect_jank = 0

        def start(self):
            self.collect_fps, self.collect_jank = self.result

        def stop(self):
            pass

    monitors = iter([FakeMonitor((30, 0)), FakeMonitor((0, 0))])
    monkeypatch.setattr(fps_module, "FPSMonitor", lambda **kwargs: next(monitors))

    fps = FPS("com.example", "device-1")

    assert fps.getAndroidFps(noLog=True) == (30, 0)
    assert fps.getAndroidFps(noLog=True) == (30, 0)


def test_android_fps_reuses_running_monitor_across_instances(monkeypatch):
    monkeypatch.setattr(fps_module.time, "sleep", lambda _: None)

    class LiveThread:
        def is_alive(self):
            return True

    class LiveCollector:
        collector_thread = LiveThread()
        calculator_thread = LiveThread()

    class FakeMonitor:
        frequency = 0
        fpscollector = LiveCollector()
        starts = 0

        def __init__(self, **kwargs):
            self.collect_fps = 0
            self.collect_jank = 0

        def start(self):
            FakeMonitor.starts += 1
            self.collect_fps = 30
            self.collect_jank = 0

        def stop(self):
            pass

    monkeypatch.setattr(fps_module, "FPSMonitor", FakeMonitor)

    assert FPS("com.example", "device-1").getAndroidFps(noLog=True) == (30, 0)
    assert FPS("com.example", "device-1").getAndroidFps(noLog=True) == (30, 0)
    assert FakeMonitor.starts == 1


def test_android_gpu_cumulative_counter_uses_delta(monkeypatch):
    mock_adb = MagicMock()
    outputs = iter(["25 100", "50 200", ""])
    mock_adb.shell.side_effect = lambda cmd, deviceId, timeout=None: next(outputs)
    monkeypatch.setattr(gpu_module, "adb", mock_adb)
    gpu = GPU("com.example", "device-1")

    # A single reading is already a valid measurement (25%).
    assert gpu._sample_android_gpu_rate() == 25.0
    # A monotonic pair switches to window deltas: (50-25)/(200-100) = 25%.
    assert gpu._sample_android_gpu_rate() == 25.0
    assert gpu._counter_semantics == "cumulative_delta"
    # A subsequent invalid read leaves the last valid value cached.
    with pytest.raises(RuntimeError, match="invalid GPU counter"):
        gpu._sample_android_gpu_rate()
    assert gpu._last_gpu == 25.0


def test_android_gpu_windowed_counter_keeps_sampling(monkeypatch):
    """A windowed kgsl node drops/stalls between reads; that must not stop
    the GPU curve (regression: every sample was reported as a reset)."""
    mock_adb = MagicMock()
    outputs = iter(["760 1000", "800 1000", "500 1000", "760 1000"])
    mock_adb.shell.side_effect = lambda cmd, deviceId, timeout=None: next(outputs)
    monkeypatch.setattr(gpu_module, "adb", mock_adb)
    gpu = GPU("com.example", "device-windowed")

    assert gpu._sample_android_gpu_rate() == 76.0
    # The window total stalls -> the node is windowed, so the raw ratio wins
    # instead of being reported as an invalid/reset counter.
    assert gpu._sample_android_gpu_rate() == 80.0
    assert gpu._sample_android_gpu_rate() == 50.0
    assert gpu._sample_android_gpu_rate() == 76.0
    assert gpu._counter_semantics == "window_ratio"


def test_android_gpu_treats_invalid_output_as_unavailable(monkeypatch):
    mock_adb = MagicMock()
    mock_adb.shell.return_value = "not-a-number nope"
    monkeypatch.setattr(gpu_module, "adb", mock_adb)

    with pytest.raises(RuntimeError, match="no valid GPU sample"):
        GPU("com.example", "device-1").getAndroidGpuRate()


def test_android_gpu_treats_zero_denominator_as_unavailable(monkeypatch):
    mock_adb = MagicMock()
    mock_adb.shell.return_value = "0 0"
    monkeypatch.setattr(gpu_module, "adb", mock_adb)

    with pytest.raises(RuntimeError, match="no valid GPU sample"):
        GPU("com.example", "device-1").getAndroidGpuRate()


def test_android_gpu_accepts_zero_utilization_as_valid(monkeypatch):
    mock_adb = MagicMock()
    outputs = iter(["0 100", "0 200"])
    mock_adb.shell.side_effect = lambda cmd, deviceId, timeout=None: next(outputs)
    monkeypatch.setattr(gpu_module, "adb", mock_adb)
    gpu = GPU("com.example", "device-1")

    # busy stays 0 -> a real 0% utilization, never a failure or a fake value.
    assert gpu._sample_android_gpu_rate() == 0
    assert gpu._sample_android_gpu_rate() == 0


def test_android_cpu_refreshes_stale_pid_by_process_name(monkeypatch):
    monkeypatch.setattr(cpu_module.time, "sleep", lambda _: None)

    def refresh_pid(self, devices=None):
        assert self.processName == "com.example:game"
        self.pid = "456"
        return True

    monkeypatch.setattr(CPU, "_refresh_pid", refresh_pid)
    mock_adb = MagicMock()
    stat_outputs = iter([CPU_STAT_1, CPU_STAT_2])
    process_outputs = {
        "cat /proc/123/stat": "",
        "cat /proc/456/stat": iter([process_stat(10, 5), process_stat(20, 7)]),
    }

    def shell(cmd, deviceId, timeout=None):
        if cmd == "cat /proc/stat":
            return next(stat_outputs)
        if cmd == "cat /proc/123/stat":
            return process_outputs[cmd]
        if cmd == "cat /proc/456/stat":
            return next(process_outputs[cmd])
        return ""

    mock_adb.shell.side_effect = shell
    monkeypatch.setattr(cpu_module, "adb", mock_adb)
    cpu = CPU("com.example", "device-1", pid="123", processName="com.example:game")

    assert cpu.getAndroidCpuRate(noLog=True) == (10, 58.33)
    assert cpu.pid == "456"


def test_android_network_refreshes_stale_pid_by_process_name(monkeypatch):
    monkeypatch.setattr(network_module.time, "sleep", lambda _: None)

    def refresh_pid(self, devices=None):
        assert self.processName == "com.example:game"
        self.pid = "456"
        return True

    monkeypatch.setattr(Network, "_refresh_pid", refresh_pid)
    mock_adb = MagicMock()
    send_outputs = iter(["2048", "4096"])
    recv_outputs = iter(["1024", "3072"])

    def shell(cmd, deviceId, timeout=None):
        if cmd == "cat /proc/123/status":
            return ""
        if cmd == "cat /proc/456/status":
            return "Uid:\t10001\t10001\t10001\t10001\n"
        if cmd == "cat /proc/uid_stat/10001/tcp_snd":
            return next(send_outputs)
        if cmd == "cat /proc/uid_stat/10001/tcp_rcv":
            return next(recv_outputs)
        return ""

    mock_adb.shell.side_effect = shell
    monkeypatch.setattr(network_module, "adb", mock_adb)
    network = Network(
        "com.example", "device-1", pid="123", processName="com.example:game"
    )

    assert network.getAndroidNet() == (2, 2)
    assert network.pid == "456"


def test_android_file_io_parses_storaged_totals():
    output = """
Package fg_rchar fg_wchar fg_rbytes fg_wbytes bg_rchar bg_wchar bg_rbytes bg_wbytes
com.example 1024 2048 512 256 4096 8192 128 64
"""

    assert FileIO._parse_storaged_output(output, "com.example") == (5760, 10560)
    assert FileIO._parse_storaged_output(
        output, "com.example", metric="chars"
    ) == (5120, 10240)
    assert FileIO._parse_storaged_output(
        output, "com.example", metric="bytes"
    ) == (640, 320)
    assert FileIO._parse_storaged_output(
        output, "com.example", metric="foreground"
    ) == (1536, 2304)


@pytest.mark.parametrize(
    "status,value,error",
    [
        (MetricStatus.OK, 30, None),
        (MetricStatus.CACHED, 29, None),
        (MetricStatus.MISSING, None, "surface unavailable"),
    ],
)
def test_android_fps_unified_returns_metric_sample_for_all_statuses(
    monkeypatch, status, value, error
):
    fps = FPS("com.example", "device-1")
    sample = MetricSample(
        metric="fps", value=value, status=status, unit="fps", error=error
    )
    monkeypatch.setattr(fps, "_sampled_value", lambda *args, **kwargs: sample)

    result = fps.getAndroidFps(noLog=True)

    assert isinstance(result, MetricSample)
    assert result.status is status
    assert result.value == value
    assert result.error == error


def test_android_fps_unified_preserves_legal_zero(monkeypatch):
    fps = FPS("com.example", "device-1")
    sample = MetricSample(metric="fps", value=0, status=MetricStatus.OK)
    monkeypatch.setattr(fps, "_sampled_value", lambda *args, **kwargs: sample)

    result = fps.getAndroidFps(noLog=True)

    assert result.status is MetricStatus.OK
    assert result.value == 0


@pytest.mark.parametrize(
    "status,value,error",
    [
        (MetricStatus.OK, 25, None),
        (MetricStatus.CACHED, 24, None),
        (MetricStatus.MISSING, None, "gpu unavailable"),
    ],
)
def test_android_gpu_unified_returns_metric_sample_for_all_statuses(
    monkeypatch, status, value, error
):
    gpu = GPU("com.example", "device-1")
    sample = MetricSample(
        metric="gpu", value=value, status=status, unit="percent", error=error
    )
    monkeypatch.setattr(gpu, "_sampled_value", lambda *args, **kwargs: sample)

    result = gpu.getAndroidGpuRate()

    assert isinstance(result, MetricSample)
    assert result.status is status
    assert result.value == value
    assert result.error == error


def test_android_gpu_unified_preserves_legal_zero(monkeypatch):
    gpu = GPU("com.example", "device-1")
    sample = MetricSample(metric="gpu", value=0, status=MetricStatus.OK)
    monkeypatch.setattr(gpu, "_sampled_value", lambda *args, **kwargs: sample)

    result = gpu.getAndroidGpuRate()

    assert result.status is MetricStatus.OK
    assert result.value == 0


def test_android_file_io_storaged_returns_kb_delta(monkeypatch):
    events = iter(
        [
            [(100, "com.example 1000 2000 0 0 0 0 0 0", 1000, 2000)],
            [
                (100, "com.example 1000 2000 0 0 0 0 0 0", 1000, 2000),
                (101, "com.example 1024 2048 0 0 0 0 0 0", 1024, 2048),
            ],
        ]
    )
    monkeypatch.setattr(FileIO, "_read_storaged_events", lambda self: next(events))

    file_io = FileIO("com.example", "device-1", pid="123")

    assert file_io._sample_storaged_once(sample_time=100.0) == (0, 0)
    assert file_io._sample_storaged_once(sample_time=100.8) == (1.0, 2.0)
    assert file_io.last_source == "storaged"
    assert file_io.last_sample_time == 100.8
