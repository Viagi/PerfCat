# -*- coding: utf-8 -*-
"""GPU 扩展指标采集：使用率、频率、总线、温度、显存、读写带宽。

每个数据源每会话探测一次；缺失或被 SELinux 限制的节点会被跳过，
因此在只开放部分指标的机型上仍能采集其余数据。
"""
import re
import time

from logzero import logger

from perfcattle.core.adb_client import adb
from perfcattle.collector.base import adb_read_optional, adb_shell
from perfcattle.core.platform import Platform
from perfcattle.core.metrics import MetricSample, MetricStatus
from perfcattle.collector.gpu_counters import GpuBandwidth

BYTES_PER_MB = 1024 * 1024

_UTIL_SOURCES = [
    ("kgsl_gpubusy", "/sys/class/kgsl/kgsl-3d0/gpubusy", "cumulative_delta"),
    ("kgsl_percentage", "/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage", "window_ratio"),
    ("kernel_gpu_busy", "/sys/kernel/gpu/gpu_busy", "window_ratio"),
    ("mali_utilization", "/sys/class/misc/mali0/device/utilization", "window_ratio"),
]

_CLOCK_SOURCES = [
    ("kgsl_gpuclk", "/sys/class/kgsl/kgsl-3d0/gpuclk"),
    ("kgsl_devfreq", "/sys/class/kgsl/kgsl-3d0/devfreq/cur_freq"),
    ("kernel_gpu_clock", "/sys/kernel/gpu/gpu_clock"),
    ("gpufreq", "/sys/class/devfreq/gpufreq/cur_freq"),
]

# GPU <-> DDR bus vote level, not measured throughput
_BUS_SOURCES = [
    ("gpubw", "/sys/class/devfreq/soc:qcom,gpubw/cur_freq"),
    ("kgsl_busmon", "/sys/class/devfreq/soc:qcom,kgsl-busmon/cur_freq"),
    ("kernel_gpu_bus", "/sys/kernel/gpu/gpu_bus"),
]

_TEMP_SOURCES = [
    ("gpu_tmu", "/sys/kernel/gpu/gpu_tmu"),
]


def _read(deviceId, path):
    """cat 一个设备节点；不可读时返回空字符串。"""
    return adb_read_optional(adb, cmd=f"cat {path}", deviceId=deviceId, timeout=5)


def _to_number(text):
    match = re.search(r"-?\d+(?:\.\d+)?", text or "")
    return float(match.group()) if match else None


def _parse_percentage(text):
    value = _to_number(text)
    if value is None:
        return None
    if "%" not in text and value <= 1 and "." in text:
        value *= 100
    return round(value, 2)


def _parse_gpubusy(text):
    """kgsl gpubusy: "<used> <total>" cycles."""
    parts = (text or "").split()
    if len(parts) < 2:
        return None
    try:
        used, total = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if total <= 0:
        return None
    return round(used / total * 100, 2)


def _parse_hz_to_mhz(text):
    value = _to_number(text)
    if value is None:
        return None
    if value >= 1000000:
        value /= 1000000
    elif value >= 10000:
        value /= 1000
    return round(value, 2)


def _parse_temperature(text):
    value = _to_number(text)
    if value is None:
        return None
    if value > 1000:
        value /= 1000
    elif value > 200:
        value /= 10
    return round(value, 2)


class GpuStats(object):
    """单个 Android 设备的最佳努力 GPU 指标采集。"""

    MEMORY_INTERVAL = 5.0

    def __init__(self, deviceId, pkgName=None, pid=None):
        self.deviceId = deviceId
        self.pkgName = pkgName
        self.pid = pid
        self._sources = {}
        self._probed = False
        self._mem_cache = {}
        self._mem_ts = 0.0
        self._bandwidth = GpuBandwidth(deviceId)
        self.previous_busy = None
        self.previous_total = None
        # None until a second reading reveals whether the node is cumulative.
        self.counter_mode = None
        self.counter_semantics = "unknown"
        self.counter_source = None

    def _counter_result(self, value, status, error):
        """Return a serialized, schema-consistent counter sample."""
        return MetricSample(
            metric="gpu_util",
            value=value,
            unit="percent",
            status=status,
            source=self.counter_source,
            aggregation=self.counter_semantics,
            sample_epoch_ms=int(time.time() * 1000),
            sample_monotonic_ns=time.monotonic_ns(),
            error=error,
        ).to_dict()

    def _parse_counter_sample(self, text):
        parts = (text or "").split()
        try:
            busy, total = int(parts[0]), int(parts[1])
        except (IndexError, TypeError, ValueError):
            return self._counter_result(None, MetricStatus.INVALID, "invalid counter")
        if busy == 0 and total == 0:
            # Idle GPU (no work submitted yet): a genuine 0% sample.
            return self._counter_result(0, MetricStatus.OK, None)
        if busy < 0 or total <= 0 or busy > total:
            return self._counter_result(None, MetricStatus.INVALID, "invalid counter")

        # Same adaptive semantics as the main GPU collector: a kernel may
        # report cumulative cycles or the busy/total of the current window.
        ratio = round(busy / total * 100, 2)
        previous = (self.previous_busy, self.previous_total)
        self.previous_busy, self.previous_total = busy, total

        if self.counter_mode == "ratio":
            return self._counter_result(ratio, MetricStatus.OK, None)
        if previous[0] is None or previous[1] is None:
            return self._counter_result(ratio, MetricStatus.OK, None)
        if busy == previous[0] and total == previous[1]:
            # Idle: the counter has not advanced; keep the current semantics.
            return self._counter_result(ratio, MetricStatus.OK, None)
        if busy < previous[0] or total < previous[1]:
            self.counter_mode = "ratio"
            self.counter_semantics = "window_ratio"
            return self._counter_result(ratio, MetricStatus.OK, None)
        busy_delta = busy - previous[0]
        total_delta = total - previous[1]
        if busy_delta > total_delta:
            self.counter_mode = "ratio"
            self.counter_semantics = "window_ratio"
            return self._counter_result(ratio, MetricStatus.OK, None)
        self.counter_mode = "delta"
        self.counter_semantics = "cumulative_delta"
        return self._counter_result(round(busy_delta / total_delta * 100, 2), MetricStatus.OK, None)

    def _probe(self, sources, parser):
        for entry in sources:
            name, path, *semantics = entry
            text = _read(self.deviceId, path)
            if not text.strip():
                continue
            value = parser(text)
            if value is not None:
                return name, path, (semantics[0] if semantics else "window_ratio")
        return None

    def _probe_temperature(self):
        for name, path in _TEMP_SOURCES:
            text = _read(self.deviceId, path)
            if text.strip() and _parse_temperature(text) is not None:
                return name, path

        listing = adb_read_optional(adb,
            cmd=(
                'for f in /sys/class/thermal/thermal_zone*/type; do '
                'echo "$f $(cat $f 2>/dev/null)"; done'
            ),
            deviceId=self.deviceId,
            timeout=8,
        )
        for line in (listing or "").splitlines():
            path, _, kind = line.partition(" ")
            if re.search(r"gpu|gfx|graphics|kgsl", kind, re.IGNORECASE):
                temp_path = path.replace("/type", "/temp")
                if _read(self.deviceId, temp_path).strip():
                    return "thermal_zone", temp_path
        return None

    def probe(self):
        if self._probed:
            return self._sources
        util = self._probe(
            _UTIL_SOURCES,
            lambda t: _parse_gpubusy(t)
            if _parse_gpubusy(t) is not None
            else _parse_percentage(t),
        )
        if util:
            self._sources["gpu_util"] = util
            self.counter_source = util[0]
            self.counter_semantics = util[2]
        clock = self._probe(_CLOCK_SOURCES, _parse_hz_to_mhz)
        if clock:
            self._sources["gpu_clock"] = clock
        bus = self._probe(_BUS_SOURCES, _parse_hz_to_mhz)
        if bus:
            self._sources["gpu_bus"] = bus
        temp = self._probe_temperature()
        if temp:
            self._sources["gpu_temp"] = temp
        self._probed = True
        logger.info(f"gpu stats sources: {self.capabilities()}")
        return self._sources

    def capabilities(self):
        return {
            key: {"source": entry[0], "path": entry[1], "semantics": entry[2] if len(entry) > 2 else "window_ratio"}
            for key, entry in self._sources.items()
        }

    def _resolve_pid(self):
        if self.pid:
            return self.pid
        if not self.pkgName:
            return None
        try:
            from perfcattle.device.manager import Devices

            entries = Devices().getPid(deviceId=self.deviceId, pkgName=self.pkgName)
            if entries:
                self.pid = entries[0].split(":")[0]
        except Exception as e:
            logger.debug(f"gpu stats: cannot resolve pid: {e}")
        return self.pid

    def _gpu_memory(self):
        now = time.time()
        if self._mem_cache and now - self._mem_ts < self.MEMORY_INTERVAL:
            return self._mem_cache

        dump = adb_read_optional(adb, cmd="dumpsys gpu", deviceId=self.deviceId, timeout=8) or ""
        result = {}
        total = re.search(r"Global total:\s*(\d+)", dump)
        if total:
            result["gpu_mem_total"] = round(int(total.group(1)) / BYTES_PER_MB, 2)
        pid = self._resolve_pid()
        if pid:
            app = re.search(rf"Proc {pid} total:\s*(\d+)", dump)
            if app:
                result["gpu_mem_app"] = round(int(app.group(1)) / BYTES_PER_MB, 2)

        if result:
            self._mem_cache = result
            self._mem_ts = now
        return result

    def sample(self):
        """返回设备当前能提供的指标。"""
        self.probe()
        data = {}
        readers = {
            "gpu_util": _parse_gpubusy,
            "gpu_clock": _parse_hz_to_mhz,
            "gpu_bus": _parse_hz_to_mhz,
            "gpu_temp": _parse_temperature,
        }
        for key, entry in self._sources.items():
            name, path, *rest = entry
            semantics = rest[0] if rest else "window_ratio"
            text = _read(self.deviceId, path)
            if key == "gpu_util" and semantics == "cumulative_delta":
                data[key] = self._parse_counter_sample(text)
                continue
            parser = readers[key]
            value = parser(text)
            if value is None and key == "gpu_util":
                value = _parse_percentage(text)
            if value is not None:
                unit = "percent" if key == "gpu_util" else ("celsius" if key == "gpu_temp" else "MHz")
                data[key] = MetricSample.ok(key, value, unit=unit, source=name, aggregation=semantics).to_dict()
        try:
            data.update({key: MetricSample.ok(key, value, unit="MB", source="dumpsys gpu").to_dict() for key, value in self._gpu_memory().items()})
        except Exception as e:
            logger.debug(f"gpu stats: memory sampling failed: {e}")
        try:
            data.update({key: MetricSample.ok(key, value, unit="MB/s", source="gpu_perfcounter").to_dict() for key, value in self._bandwidth.sample().items()})
        except Exception as e:
            logger.debug(f"gpu stats: bandwidth sampling failed: {e}")
        return data


class GpuStatsUnsupported(object):
    """非 Android 平台占位。"""

    def __init__(self, *args, **kwargs):
        pass

    def capabilities(self):
        return {}

    def sample(self):
        return {}


def create_gpu_stats(deviceId, pkgName=None, pid=None, platform=Platform.Android):
    if platform != Platform.Android:
        return GpuStatsUnsupported()
    key = (deviceId, pkgName)
    instance = _INSTANCES.get(key)
    if instance is None or (pid and instance.pid != pid):
        instance = GpuStats(deviceId=deviceId, pkgName=pkgName, pid=pid)
        _INSTANCES[key] = instance
    return instance


def clear_gpu_stats():
    """Drop cached probes/samplers between sessions."""
    _INSTANCES.clear()


_INSTANCES = {}
