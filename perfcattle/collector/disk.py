# -*- coding: utf-8 -*-
"""磁盘性能采集器"""
import datetime
import os
import time

import tidevice
from logzero import logger

from perfcattle.core.platform import Platform
from perfcattle.core.adb_client import adb
from perfcattle.collector.base import ReusableCollector, adb_read_optional, adb_shell
from perfcattle.core.metrics import MetricSample, MetricStatus


class Disk(ReusableCollector):
    ADB_TIMEOUT = 8
    _disk_cache = {}
    _EXCLUDED_FS = {"overlay", "tmpfs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2"}

    def __init__(self, deviceId, platform=Platform.Android):
        self.deviceId = deviceId
        self.platform = platform
        self._last_disk = self._disk_cache.get(self._cache_key())
        self.last_sample_time = None

    @classmethod
    def _make_cache_key(cls, platform, deviceId):
        return platform, deviceId

    @classmethod
    def _object_cache_key(cls, deviceId, platform=Platform.Android):
        return cls._make_cache_key(platform, deviceId)

    @classmethod
    def clear(cls):
        super().clear()
        cls._disk_cache.clear()

    def _cache_key(self):
        return self._make_cache_key(self.platform, self.deviceId)

    def setInitialDisk(self):
        _f = self._get_report_store()
        disk_info = adb_read_optional(adb, cmd="df", deviceId=self.deviceId, timeout=self.ADB_TIMEOUT)
        with open(os.path.join(_f.report_dir, "initail_disk.log"), "a+", encoding="utf-8") as file:
            file.write(disk_info)

    def setCurrentDisk(self):
        _f = self._get_report_store()
        disk_info = adb_read_optional(adb, cmd="df", deviceId=self.deviceId, timeout=self.ADB_TIMEOUT)
        with open(os.path.join(_f.report_dir, "current_disk.log"), "a+", encoding="utf-8") as file:
            file.write(disk_info)

    @classmethod
    def _parse_df(cls, disk_info):
        rows = []
        seen_mounts = set()
        for line in (disk_info or "").splitlines():
            fields = line.split()
            if len(fields) < 6:
                continue
            filesystem, size, used, free, _, mount = fields[:6]
            if not all(value.isdigit() for value in (size, used, free)):
                continue
            if filesystem.lower() in cls._EXCLUDED_FS or mount in seen_mounts:
                continue
            seen_mounts.add(mount)
            rows.append((int(size), int(used), int(free), mount))
        return rows

    def getAndroidDisk(self):
        disk_info = adb_read_optional(adb, cmd="df -k /data", deviceId=self.deviceId, timeout=self.ADB_TIMEOUT)
        rows = self._parse_df(disk_info)
        if not rows:
            disk_info = adb_read_optional(adb, cmd="df -k", deviceId=self.deviceId, timeout=self.ADB_TIMEOUT)
            rows = self._parse_df(disk_info)
        if not rows:
            if self._last_disk is not None:
                logger.warning("[Disk] invalid df output, using cached value")
                return dict(self._last_disk)
            raise ValueError("invalid df output")
        disk_dict = {
            "disk_size": sum(row[0] for row in rows),
            "used": sum(row[1] for row in rows),
            "free": sum(row[2] for row in rows),
            "mount_points": [row[3] for row in rows],
            "snapshot_only": True,
            "sample_epoch_ms": int(time.time() * 1000),
            "sample_monotonic_ns": time.monotonic_ns(),
        }
        self.last_sample_time = disk_dict["sample_epoch_ms"] / 1000.0
        self._last_disk = dict(disk_dict)
        self._disk_cache[self._cache_key()] = dict(disk_dict)
        return disk_dict

    def getiOSDisk(self):
        return tidevice.Device(udid=self.deviceId).storage_info()

    def getDisk(self, noLog=False, unified=False):
        _f = self._get_report_store()
        disk = self.getAndroidDisk() if self.platform == Platform.Android else self.getiOSDisk()
        if noLog is False:
            apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
            _f.add_log(os.path.join(_f.report_dir, "disk_used.log"), apm_time, disk.get("used"))
            _f.add_log(os.path.join(_f.report_dir, "disk_free.log"), apm_time, disk.get("free"))
        if unified:
            status = disk.get("status", MetricStatus.OK.value)
            return {
                key: MetricSample(key, value, "KB", status, source="df", sample_epoch_ms=disk.get("sample_epoch_ms"), sample_monotonic_ns=disk.get("sample_monotonic_ns"), aggregation="snapshot").to_dict()
                for key, value in (("disk_size", disk.get("disk_size")), ("used", disk.get("used")), ("free", disk.get("free")))
            }
        return disk
