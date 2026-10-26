# -*- coding: utf-8 -*-
"""GPU 性能采集器"""
import datetime
import os

from logzero import logger

from perfcattle.core.platform import Platform
from perfcattle.core.adb_client import adb
from perfcattle.core.metrics import MetricSample, MetricStatus
from perfcattle.collector.base import (
    ReusableCollector,
    CachedSample,
    CollectorUnavailableError,
    adb_shell,
)


class GPU(ReusableCollector):
    ADB_TIMEOUT = 5
    SAMPLER_INTERVAL_SECONDS = 1.0
    SAMPLER_STALE_TTL_SECONDS = 5.0
    _gpu_cache = {}

    def __init__(self, pkgName, deviceId, platform=Platform.Android):
        self.pkgName = pkgName
        self.deviceId = deviceId
        self.platform = platform
        self._last_gpu = self._gpu_cache.get(self._cache_key())
        self._previous_busy = None
        self._previous_total = None

    @classmethod
    def _make_cache_key(cls, platform, deviceId, pkgName):
        return platform, deviceId, pkgName

    @classmethod
    def _object_cache_key(cls, pkgName, deviceId, platform=Platform.Android):
        return cls._make_cache_key(platform, deviceId, pkgName)

    @classmethod
    def clear(cls):
        super().clear()
        cls._gpu_cache.clear()

    def _cache_key(self):
        return self._make_cache_key(self.platform, self.deviceId, self.pkgName)

    def _fallback_gpu_value(self):
        if self._last_gpu is not None:
            return CachedSample(self._last_gpu)
        cached = self._gpu_cache.get(self._cache_key())
        if cached is not None:
            self._last_gpu = cached
            return CachedSample(cached)
        raise CollectorUnavailableError("no valid GPU sample")

    def _sample_android_gpu_rate(self, allow_baseline=True):
        result = adb_shell(adb, cmd="cat /sys/class/kgsl/kgsl-3d0/gpubusy", deviceId=self.deviceId, timeout=self.ADB_TIMEOUT)
        parts = result.strip().split()
        if len(parts) < 2:
            raise CollectorUnavailableError("no valid GPU sample: invalid GPU counter")
        try:
            busy, total = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise CollectorUnavailableError("no valid GPU sample: invalid GPU counter") from exc
        if busy < 0 or total <= 0 or busy > total:
            raise CollectorUnavailableError("no valid GPU sample: invalid GPU counter")
        if self._previous_busy is None or self._previous_total is None:
            self._previous_busy, self._previous_total = busy, total
            if allow_baseline:
                return round(busy / total * 100, 2)
            raise CollectorUnavailableError("GPU counter baseline unavailable")
        if busy < self._previous_busy or total < self._previous_total:
            self._previous_busy, self._previous_total = busy, total
            raise CollectorUnavailableError("GPU counter reset or wrap")
        busy_delta = busy - self._previous_busy
        total_delta = total - self._previous_total
        self._previous_busy, self._previous_total = busy, total
        if total_delta <= 0 or busy_delta < 0 or busy_delta > total_delta:
            raise CollectorUnavailableError("no valid GPU sample: invalid GPU counter delta")
        gpu = round(busy_delta / total_delta * 100, 2)
        self._last_gpu = gpu
        self._gpu_cache[self._cache_key()] = gpu
        return gpu

    def getAndroidGpuRate(self, unified=None):
        try:
            result = self._sampled_value("gpu", lambda: self._sample_android_gpu_rate(True), interval=self.SAMPLER_INTERVAL_SECONDS, stale_ttl=self.SAMPLER_STALE_TTL_SECONDS, background=True)
            if isinstance(result, MetricSample):
                return result
            return result
        except Exception as e:
            cached = self._cached_sampled_value("gpu", max_age=self.SAMPLER_STALE_TTL_SECONDS)
            if cached is not None:
                logger.warning("[GPU] %s, using cached value", e)
                return cached
            logger.warning("[GPU] %s, no cached value", e)
            raise

    def getiOSGpuRate(self):
        from perfcattle.collector.ios_perf import IOSPerformance
        return IOSPerformance(self.pkgName, self.deviceId).getPerformance(IOSPerformance.gpu)

    def getGPU(self, noLog=False, unified=False):
        _f = self._get_report_store()
        gpu = self.getAndroidGpuRate() if self.platform == Platform.Android else self.getiOSGpuRate()
        if noLog is False:
            apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
            log_value = gpu.value if isinstance(gpu, MetricSample) else gpu
            if not isinstance(gpu, MetricSample) or gpu.status in (MetricStatus.OK, MetricStatus.CACHED):
                _f.add_log(os.path.join(_f.report_dir, "gpu.log"), apm_time, log_value)
        if unified:
            return gpu.to_dict() if isinstance(gpu, MetricSample) else MetricSample.ok("gpu", gpu, unit="percent", source="kgsl_gpubusy").to_dict()
        return gpu
