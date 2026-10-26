# -*- coding: utf-8 -*-
"""网络流量性能采集器"""
import datetime
import os
import re
import time
from pathlib import PurePosixPath

from logzero import logger

from perfcattle.core.platform import Platform
from perfcattle.core.adb_client import adb
from perfcattle.collector.base import ReusableCollector, adb_shell
from perfcattle.core.metrics import MetricSample, MetricStatus


class Network(ReusableCollector):
    ADB_TIMEOUT = 5
    FIRST_SAMPLE_INTERVAL_SECONDS = 0.2
    MIN_SAMPLE_INTERVAL_SECONDS = 0.2
    SAMPLER_INTERVAL_SECONDS = 1.0
    SAMPLER_STALE_TTL_SECONDS = 5.0
    _network_data_cache = {}
    _network_totals_cache = {}

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
        self._last_network_data = self._network_data_cache.get(self._cache_key(wifi=True))
        self._last_network_totals = {}
        self._last_network_sample_time = {}
        self._last_logged_sample_time = None
        self._network_force_next_sample = set()
        self._uid = None
        self._uid_stat_supported = None
        self.last_sample_time = None
        self.last_source = None
        self.last_scope = None
        self.fallback_reason = None
        self.last_sample = None
        if self.pid is None and self.platform == Platform.Android:
            self._refresh_pid()

    @classmethod
    def _make_cache_key(
        cls,
        platform,
        deviceId,
        pkgName,
        pid=None,
        processName=None,
        wifi=True,
    ):
        return platform, deviceId, pkgName, processName or pid or pkgName, wifi

    @classmethod
    def _object_cache_key(
        cls,
        pkgName,
        deviceId,
        platform=Platform.Android,
        pid=None,
        processName=None,
    ):
        return cls._make_cache_key(platform, deviceId, pkgName, pid, processName, None)

    @classmethod
    def clear(cls):
        super().clear()
        cls._network_data_cache.clear()
        cls._network_totals_cache.clear()

    def _pid_lookup_name(self):
        return self.processName or self.pkgName

    def _is_package_process(self):
        return not self.processName or self.processName == self.pkgName

    def _cache_key(self, wifi=True):
        return self._make_cache_key(
            self.platform,
            self.deviceId,
            self.pkgName,
            self.pid,
            self.processName,
            wifi,
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
        new_pid = process_list[0].split(":")[0]
        if self.pid and self.pid != new_pid:
            self._uid = None
            self._uid_stat_supported = None
            self._last_network_totals.clear()
            self._last_network_sample_time.clear()
        self.pid = new_pid
        return True

    def _network_scope(self):
        if self._uid_stat_supported:
            return "uid_total"
        return "package" if self._is_package_process() else "package_shared"

    def _network_fallback_reason(self):
        if self._is_package_process():
            return None
        return "using package network totals shared by all app processes"

    def _read_uid(self):
        if self._uid is not None:
            return self._uid
        output = adb_shell(adb,
            cmd=f"cat /proc/{self.pid}/status",
            deviceId=self.deviceId,
            timeout=self.ADB_TIMEOUT,
        )
        for line in (output or "").splitlines():
            if line.startswith("Uid:"):
                parts = line.split()
                if len(parts) > 1 and parts[1].isdigit():
                    self._uid = int(parts[1])
                    return self._uid
        return None

    def _read_uid_stat_counter(self, uid, filename):
        path = PurePosixPath("/proc/uid_stat") / str(uid) / filename
        return adb_shell(adb,
            cmd=f"cat {path}",
            deviceId=self.deviceId,
            timeout=self.ADB_TIMEOUT,
        )

    @staticmethod
    def _parse_counter(output):
        try:
            return int(str(output).strip())
        except (TypeError, ValueError):
            return None

    def _get_android_net_totals(self, wifi=True):
        if self._uid_stat_supported is False:
            return self._get_netstats_totals(wifi=wifi)
        uid = self._read_uid()
        if uid is None:
            if self._refresh_pid():
                self._uid = None
                uid = self._read_uid()
            if uid is None:
                return None
        send = recv = None
        for attempt in range(2):
            send = self._parse_counter(self._read_uid_stat_counter(uid, "tcp_snd"))
            recv = self._parse_counter(self._read_uid_stat_counter(uid, "tcp_rcv"))
            if send is not None and recv is not None:
                break
            if attempt == 0 and self._refresh_pid():
                self._uid = None
                uid = self._read_uid()
                continue
        if send is None or recv is None:
            self._uid_stat_supported = False
            self.fallback_reason = "uid network counters unavailable"
            logger.debug(
                "uid network counters unavailable for %s pid %s uid %s",
                self.pkgName,
                self.pid,
                uid,
            )
            return self._get_netstats_totals(wifi=wifi)
        stats = (round(float(send) / 1024, 2), round(float(recv) / 1024, 2))
        self._uid_stat_supported = True
        self._last_network_totals[wifi] = stats
        self._last_network_sample_time[wifi] = time.time()
        self._network_totals_cache[self._cache_key(wifi)] = stats
        self.last_source = "uid_stat"
        self.last_scope = self._network_scope()
        self.fallback_reason = self._network_fallback_reason()
        return stats

    def _read_netstats_detail(self):
        adb_shell(adb,
            cmd="dumpsys netstats poll",
            deviceId=self.deviceId,
            timeout=self.ADB_TIMEOUT,
        )
        return adb_shell(adb,
            cmd="dumpsys netstats detail",
            deviceId=self.deviceId,
            timeout=15,
        )

    @staticmethod
    def _network_type_matches(line, wifi):
        # Older Android releases print transport names instead of numeric IDs.
        network_type = r"(?:1|WIFI)" if wifi is True else r"(?:0|MOBILE)"
        return re.search(r"\btype={}(?=[,}}\s]|$)".format(network_type), line) is not None

    def _parse_netstats_totals(self, output, uid, wifi=True):
        total_recv = 0
        total_send = 0
        in_target = False
        for line in (output or "").splitlines():
            if line.startswith("  ident="):
                in_target = (
                    f"uid={uid}" in line
                    and "tag=0x0" in line
                    and self._network_type_matches(line, wifi)
                )
                continue
            if not in_target:
                continue
            match = re.search(r"\brb=(\d+).*?\btb=(\d+)", line)
            if not match:
                continue
            total_recv += int(match.group(1))
            total_send += int(match.group(2))
        if total_send == 0 and total_recv == 0:
            return None
        return round(float(total_send) / 1024, 2), round(float(total_recv) / 1024, 2)

    def _get_netstats_totals(self, wifi=True):
        uid = self._read_uid()
        if uid is None:
            return None
        stats = self._parse_netstats_totals(
            self._read_netstats_detail(), uid, wifi=wifi
        )
        if stats is None:
            self.fallback_reason = "uid network counters unavailable"
            return None
        self._last_network_totals[wifi] = stats
        self._last_network_sample_time[wifi] = time.time()
        self._network_totals_cache[self._cache_key(wifi)] = stats
        self.last_source = "netstats"
        self.last_scope = self._network_scope()
        self.fallback_reason = self._network_fallback_reason()
        return stats

    def _process_exists(self, devices):
        return self._refresh_pid(devices)

    def _sample_android_network(self, wifi=True):
        previous_stats = self._last_network_totals.get(wifi)
        if previous_stats is None:
            previous_stats = self._network_totals_cache.get(self._cache_key(wifi))
        previous_time = self._last_network_sample_time.get(wifi)
        cached_data = self._network_data_cache.get(self._cache_key(wifi))
        if (
            previous_stats is not None
            and cached_data is not None
            and previous_time is not None
            and wifi not in self._network_force_next_sample
            and time.time() - previous_time < self.MIN_SAMPLE_INTERVAL_SECONDS
        ):
            self._last_network_data = cached_data
            return cached_data
        current_stats = self._get_android_net_totals(wifi=wifi)
        if current_stats is None:
            self.fallback_reason = (
                self.fallback_reason or "uid network counters unavailable"
            )
            return None, None
        self._network_force_next_sample.discard(wifi)
        if previous_stats is None:
            if cached_data is not None:
                sendNum, recNum = cached_data
            else:
                time.sleep(self.FIRST_SAMPLE_INTERVAL_SECONDS)
                next_stats = self._get_android_net_totals(wifi=wifi)
                if next_stats is None:
                    self.fallback_reason = (
                        self.fallback_reason or "uid network counters unavailable"
                    )
                    return None, None
                sendNum = round(float(next_stats[0] - current_stats[0]), 2)
                recNum = round(float(next_stats[1] - current_stats[1]), 2)
        else:
            sendNum = round(float(current_stats[0] - previous_stats[0]), 2)
            recNum = round(float(current_stats[1] - previous_stats[1]), 2)
        if sendNum is None or recNum is None:
            return None, None
        if sendNum < 0 or recNum < 0:
            raise ValueError("network counter reset")
        self._last_network_data = (sendNum, recNum)
        self._network_data_cache[self._cache_key(wifi)] = self._last_network_data
        return self._last_network_data

    def getAndroidNet(self, wifi=True):
        """获取 Android 包 UID 发送/接收增量，单位：KB。"""
        _d = self._get_devices()
        sampler_name = "network:{}".format(wifi)
        try:
            sendNum, recNum = self._sampled_value(
                sampler_name,
                lambda: self._sample_android_network(wifi=wifi),
                interval=self.SAMPLER_INTERVAL_SECONDS,
                stale_ttl=self.SAMPLER_STALE_TTL_SECONDS,
                background=True,
            )
            sampler = getattr(self, "_samplers", {}).get(sampler_name)
            if sampler is not None:
                self.last_sample_time = sampler.last_sample_time
        except Exception as e:
            cached = self._cached_sampled_value(
                sampler_name, max_age=self.SAMPLER_STALE_TTL_SECONDS
            )
            sampler = getattr(self, "_samplers", {}).get(sampler_name)
            used_cache = cached is not None
            if cached is not None:
                sendNum, recNum = cached
                if sampler is not None:
                    self.last_sample_time = sampler.last_sample_time
            else:
                sendNum, recNum = None, None
            if not self._process_exists(_d):
                logger.error("[Network] {} : No process found".format(self.pkgName))
            elif used_cache:
                logger.warning("[Network] %s, using cached value", e)
            else:
                logger.exception(e)
        return sendNum, recNum

    def setAndroidNet(self, wifi=True):
        """设置 Android 网络基线数据"""
        _d = self._get_devices()
        try:
            stats = self._get_android_net_totals(wifi=wifi)
            if stats is None:
                return None, None
            sendNum, recNum = stats
            self.last_sample_time = time.time()
            self._network_force_next_sample.add(wifi)
        except Exception as e:
            cached_totals = self._network_totals_cache.get(self._cache_key(wifi))
            used_cache = False
            if wifi in self._last_network_totals:
                sendNum, recNum = self._last_network_totals[wifi]
                used_cache = True
            elif cached_totals is not None:
                sendNum, recNum = cached_totals
                used_cache = True
            else:
                sendNum, recNum = None, None
            if not self._process_exists(_d):
                logger.error("[Network] {} : No process found".format(self.pkgName))
            elif used_cache:
                logger.warning("[Network] %s, using cached totals", e)
            else:
                logger.exception(e)
        return sendNum, recNum

    def getiOSNet(self):
        """获取 iOS 上行和下行流量数据"""
        from perfcattle.collector.ios_perf import IOSPerformance
        apm = IOSPerformance(self.pkgName, self.deviceId)
        apm_data = apm.getPerformance(apm.network)
        self.last_sample_time = apm.last_sample_time
        sendNum = round(float(apm_data[1]), 2)
        recNum = round(float(apm_data[0]), 2)
        return sendNum, recNum

    def getNetWorkData(self, wifi=True, noLog=False):
        """获取上行和下行流量数据，单位：KB"""
        _f = self._get_report_store()
        sendNum, recNum = (
            self.getAndroidNet(wifi)
            if self.platform == Platform.Android
            else self.getiOSNet()
        )
        status = MetricStatus.OK if sendNum is not None and recNum is not None else MetricStatus.MISSING
        self.last_sample = {
            "send": sendNum, "recv": recNum, "unit": "KB", "status": status.value,
            "source": self.last_source, "scope": self.last_scope,
            "sample_epoch_ms": int((self.last_sample_time or time.time()) * 1000),
            "error": self.fallback_reason if status is not MetricStatus.OK else None,
        }
        if noLog is False and status is MetricStatus.OK:
            if self.last_sample_time == self._last_logged_sample_time:
                return sendNum, recNum
            sample_time = self.last_sample_time or time.time()
            apm_time = datetime.datetime.fromtimestamp(sample_time).strftime("%H:%M:%S.%f")
            _f.add_log(os.path.join(_f.report_dir, "upflow.log"), apm_time, sendNum)
            _f.add_log(os.path.join(_f.report_dir, "downflow.log"), apm_time, recNum)
            self._last_logged_sample_time = self.last_sample_time
        return sendNum, recNum
