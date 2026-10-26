# -*- coding: utf-8 -*-
"""内存性能采集器"""
import datetime
import os
import re
import threading
import time

from logzero import logger

from perfcattle.core.platform import Platform
from perfcattle.core.adb_client import adb
from perfcattle.collector.base import ReusableCollector


class Memory(ReusableCollector):
    ADB_TIMEOUT = 8
    MEMINFO_CACHE_TTL_SECONDS = 0.3
    MEMORY_DETAIL_CACHE_TTL_SECONDS = 1.5
    SAMPLER_INTERVAL_SECONDS = 1.0
    SAMPLER_STALE_TTL_SECONDS = 5.0
    MEMORY_DETAIL_SAMPLER_INTERVAL_SECONDS = 2.0
    MEMORY_DETAIL_STALE_TTL_SECONDS = 8.0
    _process_memory_cache = {}
    _memory_detail_cache = {}

    def __init__(
        self,
        pkgName,
        deviceId,
        platform=Platform.Android,
        pid=None,
        processName=None,
    ):
        self.pkgName = pkgName
        self.deviceId = deviceId
        self.platform = platform
        self.pid = pid
        self.processName = processName
        self._last_process_memory = self._process_memory_cache.get(self._cache_key())
        self._last_memory_detail = self._memory_detail_cache.get(self._cache_key())
        self._last_meminfo_output = None
        self._last_meminfo_time = 0
        self._last_meminfo_pid = None
        self._meminfo_lock = threading.RLock()
        if self.pid is None and self.platform == Platform.Android:
            self._refresh_pid()

    @classmethod
    def _make_cache_key(cls, platform, deviceId, pkgName, pid=None, processName=None):
        return platform, deviceId, pkgName, processName or pid or pkgName

    @classmethod
    def _object_cache_key(
        cls,
        pkgName,
        deviceId,
        platform=Platform.Android,
        pid=None,
        processName=None,
    ):
        return cls._make_cache_key(platform, deviceId, pkgName, pid, processName)

    @classmethod
    def clear(cls):
        super().clear()
        cls._process_memory_cache.clear()
        cls._memory_detail_cache.clear()

    def _pid_lookup_name(self):
        return self.processName or self.pkgName

    def _cache_process_name(self):
        return self.processName or self.pid or self.pkgName

    def _cache_key(self):
        return self._make_cache_key(
            self.platform,
            self.deviceId,
            self.pkgName,
            self.pid,
            self.processName,
        )

    def _refresh_pid(self, devices=None):
        if self.platform != Platform.Android:
            return False
        devices = devices or self._get_devices()
        process_list = devices.getPid(
            pkgName=self._pid_lookup_name(), deviceId=self.deviceId
        )
        if not process_list:
            return False
        self.pid = process_list[0].split(":")[0]
        return True

    def _process_exists(self, devices):
        return self._refresh_pid(devices)

    def _write_memory_detail_log(self, file_obj, memory_dict):
        apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
        metrics = (
            ("mem_java_heap.log", "java_heap"),
            ("mem_native_heap.log", "native_heap"),
            ("mem_code_pss.log", "code_pss"),
            ("mem_stack_pss.log", "stack_pss"),
            ("mem_graphics_pss.log", "graphics_pss"),
            ("mem_private_pss.log", "private_pss"),
            ("mem_system_pss.log", "system_pss"),
        )
        for filename, key in metrics:
            file_obj.add_log(
                os.path.join(file_obj.report_dir, filename),
                apm_time,
                memory_dict.get(key),
            )

    def _invalidate_meminfo_cache(self):
        with self._meminfo_lock:
            self._last_meminfo_output = None
            self._last_meminfo_time = 0
            self._last_meminfo_pid = None

    def _read_android_meminfo(self, allow_cached=True, cache_ttl=None):
        with self._meminfo_lock:
            cache_ttl = (
                self.MEMINFO_CACHE_TTL_SECONDS if cache_ttl is None else cache_ttl
            )
            now = time.monotonic()
            if (
                allow_cached
                and self._last_meminfo_output is not None
                and self._last_meminfo_pid == self.pid
                and now - self._last_meminfo_time <= cache_ttl
            ):
                return self._last_meminfo_output

            output = adb.shell(
                cmd="dumpsys meminfo {}".format(self.pid),
                deviceId=self.deviceId,
                timeout=self.ADB_TIMEOUT,
            )
            if output:
                self._last_meminfo_output = output
                self._last_meminfo_time = time.monotonic()
                self._last_meminfo_pid = self.pid
            return output

    def _parse_android_memory(self, output):
        m_total = re.search(r"TOTAL\s*(\d+)", output)
        if not m_total:
            m_total = re.search(r"TOTAL PSS:\s*(\d+)", output)
        m_swap = re.search(r"TOTAL SWAP PSS:\s*(\d+)", output)
        if not m_swap:
            m_swap = re.search(r"TOTAL SWAP \(KB\):\s*(\d+)", output)
        if not m_total:
            return None
        totalPass = round(float(float(m_total.group(1))) / 1024, 2)
        swapPass = round(float(float(m_swap.group(1))) / 1024, 2) if m_swap else 0
        return totalPass, swapPass

    def _parse_android_memory_detail(self, output):
        """解析 dumpsys meminfo 的两代格式。

        旧格式：Java Heap: / Native Heap: / Code: / Stack: / Graphics: /
        Private Other: / System:
        新格式（Android 12+ 常见）：Dalvik Heap / Native Heap / Stack /
        Gfx dev / GL mtrack / EGL mtrack / Unknown，且没有冒号。
        """
        def row_pss(label):
            match = re.search(
                r"^\s*" + re.escape(label) + r"\s*:?[ \t]+(\d+)", output, re.MULTILINE
            )
            return int(match.group(1)) if match else None

        java = row_pss("Java Heap")
        if java is None:
            java = row_pss("Dalvik Heap")
        native = row_pss("Native Heap")
        code = row_pss("Code")
        if code is None:
            code = sum(
                row_pss(label) or 0
                for label in (
                    ".so mmap", ".jar mmap", ".apk mmap",
                    ".dex mmap", ".oat mmap", ".art mmap",
                )
            ) or None
        stack = row_pss("Stack")
        graphics = row_pss("Graphics")
        if graphics is None:
            graphics = sum(
                row_pss(label) or 0
                for label in ("Gfx dev", "GL mtrack", "EGL mtrack")
            ) or None
        private = row_pss("Private Other")
        if private is None:
            private = row_pss("Unknown")
        system = row_pss("System") or 0

        if java is None or native is None:
            return None
        return dict(
            java_heap=round(float(java) / 1024, 2),
            native_heap=round(float(native) / 1024, 2),
            code_pss=round(float(code or 0) / 1024, 2),
            stack_pss=round(float(stack or 0) / 1024, 2),
            graphics_pss=round(float(graphics or 0) / 1024, 2),
            private_pss=round(float(private or 0) / 1024, 2),
            system_pss=round(float(system) / 1024, 2),
        )

    def _read_parsed_android_meminfo(
        self, devices, parser, error_message, cache_ttl=None
    ):
        for attempt in range(2):
            output = self._read_android_meminfo(
                allow_cached=attempt == 0, cache_ttl=cache_ttl
            )
            output = output or ""
            parsed = parser(output)
            if parsed is not None:
                return parsed
            self._invalidate_meminfo_cache()
            if attempt == 0 and self._refresh_pid(devices):
                continue
            raise ValueError(error_message)

    def _sample_android_memory(self):
        _d = self._get_devices()
        totalPass, swapPass = self._read_parsed_android_meminfo(
            _d, self._parse_android_memory, "invalid memory output"
        )
        self._last_process_memory = (totalPass, swapPass)
        self._process_memory_cache[self._cache_key()] = self._last_process_memory
        return self._last_process_memory

    def getAndroidMemory(self):
        """获取 Android 内存，单位：MB"""
        _d = self._get_devices()
        try:
            totalPass, swapPass = self._sampled_value(
                "memory",
                self._sample_android_memory,
                interval=self.SAMPLER_INTERVAL_SECONDS,
                stale_ttl=self.SAMPLER_STALE_TTL_SECONDS,
                background=True,
            )
        except Exception as e:
            cached = self._cached_sampled_value(
                "memory", max_age=self.SAMPLER_STALE_TTL_SECONDS
            )
            used_cache = cached is not None
            if cached is not None:
                totalPass, swapPass = cached
            else:
                totalPass, swapPass = 0, 0
            if used_cache:
                logger.warning("[Memory] %s, using cached value", e)
            elif not self._process_exists(_d):
                logger.error("[Memory] {} : No process found".format(self.pkgName))
            else:
                logger.exception(e)
        return totalPass, swapPass

    def _sample_android_memory_detail(self):
        _d = self._get_devices()
        memory_dict = self._read_parsed_android_meminfo(
            _d,
            self._parse_android_memory_detail,
            "invalid memory detail output",
            cache_ttl=self.MEMORY_DETAIL_CACHE_TTL_SECONDS,
        )
        self._last_memory_detail = dict(memory_dict)
        self._memory_detail_cache[self._cache_key()] = dict(memory_dict)
        return dict(memory_dict)

    def getAndroidMemoryDetail(self, noLog=False):
        """获取 Android 详细内存信息，单位：MB"""
        _f = self._get_report_store()
        _d = self._get_devices()
        try:
            memory_dict = self._sampled_value(
                "memory_detail",
                self._sample_android_memory_detail,
                interval=self.MEMORY_DETAIL_SAMPLER_INTERVAL_SECONDS,
                stale_ttl=self.MEMORY_DETAIL_STALE_TTL_SECONDS,
                background=True,
            )
            memory_dict = dict(memory_dict)
            if noLog is False:
                self._write_memory_detail_log(_f, memory_dict)
        except Exception as e:
            cached = self._cached_sampled_value(
                "memory_detail", max_age=self.MEMORY_DETAIL_STALE_TTL_SECONDS
            )
            used_cache = cached is not None
            if cached is not None:
                memory_dict = dict(cached)
            else:
                memory_dict = dict(
                    java_heap=0,
                    native_heap=0,
                    code_pss=0,
                    stack_pss=0,
                    graphics_pss=0,
                    private_pss=0,
                    system_pss=0,
                )
            if used_cache:
                logger.warning("[Memory Detail] %s, using cached value", e)
            elif not self._process_exists(_d):
                logger.error(
                    "[Memory Detail] {} : No process found".format(self.pkgName)
                )
            else:
                logger.exception(e)
            if noLog is False and used_cache:
                self._write_memory_detail_log(_f, memory_dict)
        return memory_dict

    def getiOSMemory(self):
        """获取 iOS 内存"""
        from perfcattle.collector.ios_perf import IOSPerformance
        apm = IOSPerformance(self.pkgName, self.deviceId)
        totalPass = round(float(apm.getPerformance(apm.memory)), 2)
        swapPass = 0
        return totalPass, swapPass

    def getProcessMemory(self, noLog=False):
        """获取应用内存"""
        _f = self._get_report_store()
        totalPass, swapPass = (
            self.getAndroidMemory()
            if self.platform == Platform.Android
            else self.getiOSMemory()
        )
        if noLog is False:
            apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
            _f.add_log(os.path.join(_f.report_dir, "mem_total.log"), apm_time, totalPass)
            if self.platform == Platform.Android:
                _f.add_log(
                    os.path.join(_f.report_dir, "mem_swap.log"), apm_time, swapPass
                )
        return totalPass, swapPass
