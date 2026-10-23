# -*- coding: utf-8 -*-
"""GPU 性能采集器"""
import datetime
import os

from logzero import logger

from perfcattle.core.platform import Platform
from perfcattle.core.adb_client import adb
from perfcattle.collector.base import ReusableCollector


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
            return self._last_gpu
        cached = self._gpu_cache.get(self._cache_key())
        if cached is not None:
            self._last_gpu = cached
            return cached
        return 0

    def _sample_android_gpu_rate(self):
        cmd = "cat /sys/class/kgsl/kgsl-3d0/gpubusy"
        result = adb.shell(cmd=cmd, deviceId=self.deviceId, timeout=self.ADB_TIMEOUT)
        parts = result.strip().split()
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return self._fallback_gpu_value()
        try:
            numerator = int(parts[0])
            denominator = int(parts[1])
        except ValueError:
            return self._fallback_gpu_value()
        if denominator <= 0:
            return self._fallback_gpu_value()
        gpu = round(float(numerator / denominator) * 100, 2)
        self._last_gpu = gpu
        self._gpu_cache[self._cache_key()] = gpu
        return gpu

    def getAndroidGpuRate(self):
        """获取 Android GPU 使用率"""
        try:
            return self._sampled_value(
                "gpu",
                self._sample_android_gpu_rate,
                interval=self.SAMPLER_INTERVAL_SECONDS,
                stale_ttl=self.SAMPLER_STALE_TTL_SECONDS,
                background=True,
            )
        except Exception as e:
            cached = self._cached_sampled_value(
                "gpu", max_age=self.SAMPLER_STALE_TTL_SECONDS
            )
            if cached is not None:
                logger.warning("[GPU] %s, using cached value", e)
                return cached
            logger.warning("[GPU] %s, no cached value", e)
            return 0

    def getiOSGpuRate(self):
        """获取 iOS GPU 使用率"""
        from perfcattle.collector.ios_perf import IOSPerformance
        apm = IOSPerformance(self.pkgName, self.deviceId)
        gpu = apm.getPerformance(apm.gpu)
        return gpu

    def getGPU(self, noLog=False):
        """获取 GPU 使用率"""
        _f = self._get_report_store()
        gpu = (
            self.getAndroidGpuRate()
            if self.platform == Platform.Android
            else self.getiOSGpuRate()
        )
        if noLog is False:
            apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
            _f.add_log(os.path.join(_f.report_dir, "gpu.log"), apm_time, gpu)
        return gpu
