# -*- coding: utf-8 -*-
"""CPU 性能采集器"""
import datetime
import os
import time

from logzero import logger

from perfcattle.core.platform import Platform
from perfcattle.core.adb_client import adb
from perfcattle.collector.base import ReusableCollector, adb_shell, CollectorUnavailableError
from perfcattle.core.metrics import MetricSample, MetricStatus


class CPU(ReusableCollector):
    ADB_TIMEOUT = 5
    FIRST_SAMPLE_INTERVAL_SECONDS = 0.2
    MIN_SAMPLE_INTERVAL_SECONDS = 0.2
    SAMPLER_INTERVAL_SECONDS = 1.0
    SAMPLER_STALE_TTL_SECONDS = 5.0
    _cpu_rate_cache = {}
    _core_cpu_rate_cache = {}

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
        self._last_cpu_rate = self._cpu_rate_cache.get(self._cache_key())
        self._last_core_cpu_rate = self._core_cpu_rate_cache.get(self._cache_key())
        self._last_cpu_sample = None
        self._last_core_cpu_sample = None
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
        cls._cpu_rate_cache.clear()
        cls._core_cpu_rate_cache.clear()

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

    def _read_cpu_stat_lines(self):
        result = adb_shell(adb,
            cmd="cat /proc/stat", deviceId=self.deviceId, timeout=self.ADB_TIMEOUT
        )
        return result.splitlines()

    def _parse_cpu_line(self, line):
        fields = line.split()
        if not fields or not fields[0].startswith("cpu"):
            return None
        values = []
        for value in fields[1:8]:
            try:
                values.append(float(value))
            except ValueError:
                return None
        if len(values) < 4:
            return None
        return {
            "name": fields[0],
            "total": sum(values),
            "idle": values[3],
        }

    def _cpu_stats(self):
        aggregate = None
        cores = []
        for line in self._read_cpu_stat_lines():
            stat = self._parse_cpu_line(line)
            if stat is None:
                continue
            if stat["name"] == "cpu":
                aggregate = stat
            elif stat["name"][3:].isdigit():
                cores.append(stat)
        if not cores and aggregate is not None:
            cores.append(aggregate)
        return aggregate, cores

    @staticmethod
    def _parse_process_stat(result):
        """Parse /proc/<pid>/stat without splitting the comm field.

        ``comm`` may contain whitespace and right parentheses.  The kernel
        encloses it between the first ``(`` and the final ``)``; fields after
        that marker therefore start at field 3 (state).
        """
        text = (result or "").strip()
        start = text.find("(")
        end = text.rfind(")")
        if start <= 0 or end <= start:
            raise ValueError("invalid process cpu stat output")
        fields = text[end + 1 :].strip().split()
        # fields[0] is state (field 3), utime/stime are fields 14/15.
        if len(fields) <= 12:
            raise ValueError("invalid process cpu stat output")
        try:
            return float(fields[11]) + float(fields[12])
        except (TypeError, ValueError):
            raise ValueError("invalid process cpu stat output")

    def getprocessCpuStat(self):
        """获取某一时刻进程的 CPU 使用量"""
        for attempt in range(2):
            cmd = "cat /proc/{}/stat".format(self.pid)
            result = adb_shell(adb,
                cmd=cmd, deviceId=self.deviceId, timeout=self.ADB_TIMEOUT
            )
            try:
                return self._parse_process_stat(result)
            except ValueError:
                if attempt == 0 and self._refresh_pid():
                    continue
                raise

    def getTotalCpuStat(self):
        """获取某一时刻总 CPU 使用量"""
        aggregate, cores = self._cpu_stats()
        if aggregate is not None:
            return float(aggregate["total"])
        return float(sum(core["total"] for core in cores))

    def getCpuCoreStat(self):
        """获取某一时刻各核心 CPU 使用量"""
        _, cores = self._cpu_stats()
        return [core["total"] for core in cores]

    def getCoreIdleCpuStat(self):
        """获取某一时刻各核心空闲 CPU 使用量"""
        _, cores = self._cpu_stats()
        return [core["idle"] for core in cores]

    def getIdleCpuStat(self):
        """获取某一时刻总空闲 CPU 使用量"""
        aggregate, cores = self._cpu_stats()
        if aggregate is not None:
            return aggregate["idle"]
        return sum(core["idle"] for core in cores)

    def _write_cpu_rate_log(self, file_obj, appCpuRate, sysCpuRate):
        apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
        file_obj.add_log(
            os.path.join(file_obj.report_dir, "cpu_app.log"), apm_time, appCpuRate
        )
        file_obj.add_log(
            os.path.join(file_obj.report_dir, "cpu_sys.log"), apm_time, sysCpuRate
        )

    def _process_exists(self, devices):
        return self._refresh_pid(devices)

    def _total_idle_from_stats(self):
        aggregate, cores = self._cpu_stats()
        if aggregate is not None:
            return aggregate["total"], aggregate["idle"]
        return (
            sum(core["total"] for core in cores),
            sum(core["idle"] for core in cores),
        )

    def _read_cpu_rate_sample(self):
        process_cpu_time = self.getprocessCpuStat()
        total_cpu_time, idle_cpu_time = self._total_idle_from_stats()
        return {
            "time": time.time(),
            "process": process_cpu_time,
            "total": total_cpu_time,
            "idle": idle_cpu_time,
        }

    def _calculate_cpu_rate(self, first, second):
        total_delta = second["total"] - first["total"]
        process_delta = second["process"] - first["process"]
        idle_delta = second["idle"] - first["idle"]
        busy_delta = total_delta - idle_delta
        if (
            total_delta <= 0
            or process_delta < 0
            or idle_delta < 0
            or busy_delta < 0
            or process_delta > total_delta
        ):
            return None
        app_cpu_rate = min(100.0, max(0.0, round(float(process_delta / total_delta * 100), 2)))
        sys_cpu_rate = min(100.0, max(0.0, round(float(busy_delta / total_delta * 100), 2)))
        return app_cpu_rate, sys_cpu_rate

    def _read_core_cpu_rate_sample(self):
        process_cpu_time = self.getprocessCpuStat()
        core_cpu_total_time_list = self.getCpuCoreStat()
        return {
            "time": time.time(),
            "process": process_cpu_time,
            "cores": core_cpu_total_time_list,
        }

    def _calculate_core_cpu_rate(self, first, second, cores):
        core_cpu_rate_list = []
        if cores <= 0 or len(first.get("cores", [])) != len(second.get("cores", [])):
            return None
        process_delta = second["process"] - first["process"]
        if process_delta < 0:
            return None
        for index in range(len(first["cores"])):
            cpu_delta = second["cores"][index] - first["cores"][index]
            if cpu_delta <= 0 or process_delta > cpu_delta * cores:
                return None
            core_cpu_rate = round(float(process_delta / cpu_delta / cores * 100), 2)
            if not 0 <= core_cpu_rate <= 100:
                return None
            core_cpu_rate_list.append(core_cpu_rate)
        return core_cpu_rate_list

    def _sample_android_core_cpu_rate(self, cores):
        previous_sample = self._last_core_cpu_sample
        if (
            previous_sample is not None
            and time.time() - previous_sample["time"]
            < self.MIN_SAMPLE_INTERVAL_SECONDS
            and self._last_core_cpu_rate is not None
        ):
            return list(self._last_core_cpu_rate)
        current_sample = self._read_core_cpu_rate_sample()
        if previous_sample is None:
            if self._last_core_cpu_rate is not None:
                coreCpuRateList = list(self._last_core_cpu_rate)
                self._last_core_cpu_sample = current_sample
            else:
                time.sleep(self.FIRST_SAMPLE_INTERVAL_SECONDS)
                next_sample = self._read_core_cpu_rate_sample()
                coreCpuRateList = self._calculate_core_cpu_rate(
                    current_sample, next_sample, cores
                )
                self._last_core_cpu_sample = next_sample
        else:
            coreCpuRateList = self._calculate_core_cpu_rate(
                previous_sample, current_sample, cores
            )
            self._last_core_cpu_sample = current_sample
        if coreCpuRateList is None:
            raise CollectorUnavailableError("invalid CPU core delta")
        self._last_core_cpu_rate = list(coreCpuRateList)
        self._core_cpu_rate_cache[self._cache_key()] = list(coreCpuRateList)
        return list(coreCpuRateList)

    def getCoreCpuRate(self, cores=0, noLog=False):
        """获取各核心 CPU 使用率"""
        _f = self._get_report_store()
        _d = self._get_devices()
        try:
            coreCpuRateList = self._sampled_value(
                "cpu_core:{}".format(cores),
                lambda: self._sample_android_core_cpu_rate(cores),
                interval=self.SAMPLER_INTERVAL_SECONDS,
                stale_ttl=self.SAMPLER_STALE_TTL_SECONDS,
                background=self.platform == Platform.Android,
            )
            coreCpuRateList = list(coreCpuRateList)
            if noLog is False:
                for i, coreCpuRate in enumerate(coreCpuRateList):
                    apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
                    _f.add_log(
                        os.path.join(_f.report_dir, "cpu{}.log".format(i)),
                        apm_time,
                        coreCpuRate,
                    )
        except Exception as e:
            cached = self._cached_sampled_value(
                "cpu_core:{}".format(cores), max_age=self.SAMPLER_STALE_TTL_SECONDS
            )
            used_cache = cached is not None
            coreCpuRateList = list(cached) if used_cache else []
            if not self._process_exists(_d):
                logger.error("[CPU Core] {} : No process found".format(self.pkgName))
            elif used_cache:
                logger.warning("[CPU Core] %s, using cached value", e)
            else:
                logger.exception(e)
        return coreCpuRateList

    def _sample_android_cpu_rate(self):
        previous_sample = self._last_cpu_sample
        if (
            previous_sample is not None
            and time.time() - previous_sample["time"]
            < self.MIN_SAMPLE_INTERVAL_SECONDS
            and self._last_cpu_rate is not None
        ):
            return self._last_cpu_rate
        current_sample = self._read_cpu_rate_sample()
        if previous_sample is None:
            if self._last_cpu_rate is not None:
                appCpuRate, sysCpuRate = self._last_cpu_rate
                self._last_cpu_sample = current_sample
            else:
                time.sleep(self.FIRST_SAMPLE_INTERVAL_SECONDS)
                next_sample = self._read_cpu_rate_sample()
                rates = self._calculate_cpu_rate(current_sample, next_sample)
                if rates is None:
                    raise CollectorUnavailableError("invalid CPU delta")
                appCpuRate, sysCpuRate = rates
                self._last_cpu_sample = next_sample
        else:
            rates = self._calculate_cpu_rate(previous_sample, current_sample)
            if rates is None:
                raise CollectorUnavailableError("invalid CPU delta")
            appCpuRate, sysCpuRate = rates
            self._last_cpu_sample = current_sample
        self._last_cpu_rate = (appCpuRate, sysCpuRate)
        self._cpu_rate_cache[self._cache_key()] = self._last_cpu_rate
        return self._last_cpu_rate

    def getAndroidCpuRate(self, noLog=False):
        """获取 Android 进程 CPU 使用率"""
        _f = self._get_report_store()
        _d = self._get_devices()
        try:
            appCpuRate, sysCpuRate = self._sampled_value(
                "cpu",
                self._sample_android_cpu_rate,
                interval=self.SAMPLER_INTERVAL_SECONDS,
                stale_ttl=self.SAMPLER_STALE_TTL_SECONDS,
                background=True,
            )
            if noLog is False:
                self._write_cpu_rate_log(_f, appCpuRate, sysCpuRate)
        except Exception as e:
            cached = self._cached_sampled_value(
                "cpu", max_age=self.SAMPLER_STALE_TTL_SECONDS
            )
            used_cache = cached is not None
            if cached is not None:
                appCpuRate, sysCpuRate = cached
            else:
                raise
            if not self._process_exists(_d):
                logger.error("[CPU] {} : No process found".format(self.pkgName))
            elif used_cache:
                logger.warning("[CPU] %s, using cached value", e)
            else:
                logger.exception(e)
            if noLog is False and used_cache:
                self._write_cpu_rate_log(_f, appCpuRate, sysCpuRate)
        return appCpuRate, sysCpuRate

    def getiOSCpuRate(self, noLog=False):
        """获取 iOS 进程 CPU 使用率，单位：%"""
        _f = self._get_report_store()
        from perfcattle.collector.ios_perf import IOSPerformance
        apm = IOSPerformance(self.pkgName, self.deviceId)
        app_cpu, sys_cpu = apm.getPerformance(apm.cpu)
        appCpuRate = round(float(app_cpu), 2)
        sysCpuRate = round(float(sys_cpu), 2)
        if noLog is False:
            apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
            _f.add_log(os.path.join(_f.report_dir, "cpu_app.log"), apm_time, appCpuRate)
            _f.add_log(os.path.join(_f.report_dir, "cpu_sys.log"), apm_time, sysCpuRate)
        return appCpuRate, sysCpuRate

    def getCpuRate(self, noLog=False):
        """获取进程 CPU 使用率，单位：%"""
        appCpuRate, systemCpuRate = (
            self.getAndroidCpuRate(noLog)
            if self.platform == Platform.Android
            else self.getiOSCpuRate(noLog)
        )
        return appCpuRate, systemCpuRate
