# -*- coding: utf-8 -*-
"""磁盘性能采集器"""
import datetime
import os

import tidevice
from logzero import logger

from perfcattle.core.platform import Platform
from perfcattle.core.adb_client import adb
from perfcattle.collector.base import ReusableCollector, adb_shell


class Disk(ReusableCollector):
    ADB_TIMEOUT = 8
    _disk_cache = {}

    def __init__(self, deviceId, platform=Platform.Android):
        self.deviceId = deviceId
        self.platform = platform
        self._last_disk = self._disk_cache.get(self._cache_key())

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
        """记录初始磁盘状态"""
        _f = self._get_report_store()
        disk_info = adb_shell(adb, cmd="df", deviceId=self.deviceId, timeout=self.ADB_TIMEOUT)
        with open(
            os.path.join(_f.report_dir, "initail_disk.log"), "a+", encoding="utf-8"
        ) as file:
            file.write(disk_info)

    def setCurrentDisk(self):
        """记录当前磁盘状态"""
        _f = self._get_report_store()
        disk_info = adb_shell(adb, cmd="df", deviceId=self.deviceId, timeout=self.ADB_TIMEOUT)
        with open(
            os.path.join(_f.report_dir, "current_disk.log"), "a+", encoding="utf-8"
        ) as file:
            file.write(disk_info)

    def getAndroidDisk(self):
        """获取 Android 内部存储信息（只统计 /data，避免多挂载点重复计数）"""
        disk_info = adb_shell(adb,
            cmd="df -k /data", deviceId=self.deviceId, timeout=self.ADB_TIMEOUT
        )
        if not disk_info.strip():
            disk_info = adb_shell(adb,
                cmd="df -k", deviceId=self.deviceId, timeout=self.ADB_TIMEOUT
            )
        disk_lines = disk_info.splitlines()
        size_list = list()
        used_list = list()
        free_list = list()
        for line in disk_lines:
            disk_value_list = line.split()
            if (
                len(disk_value_list) < 4
                or not disk_value_list[1].isdigit()
                or not disk_value_list[2].isdigit()
                or not disk_value_list[3].isdigit()
            ):
                continue
            size_list.append(int(disk_value_list[1]))
            used_list.append(int(disk_value_list[2]))
            free_list.append(int(disk_value_list[3]))
        if not size_list:
            if self._last_disk is not None:
                logger.warning("[Disk] invalid df output, using cached value")
                return dict(self._last_disk)
            raise ValueError("invalid df output")
        sum_size = sum(size_list)
        sum_used = sum(used_list)
        sum_free = sum(free_list)
        disk_dict = {"disk_size": sum_size, "used": sum_used, "free": sum_free}
        self._last_disk = dict(disk_dict)
        self._disk_cache[self._cache_key()] = dict(disk_dict)
        return disk_dict

    def getiOSDisk(self):
        """获取 iOS 磁盘信息"""
        ios_device = tidevice.Device(udid=self.deviceId)
        disk_dict = ios_device.storage_info()
        return disk_dict

    def getDisk(self, noLog=False):
        """获取磁盘信息"""
        _f = self._get_report_store()
        disk = (
            self.getAndroidDisk()
            if self.platform == Platform.Android
            else self.getiOSDisk()
        )
        if noLog is False:
            apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
            _f.add_log(
                os.path.join(_f.report_dir, "disk_used.log"), apm_time, disk.get("used")
            )
            _f.add_log(
                os.path.join(_f.report_dir, "disk_free.log"), apm_time, disk.get("free")
            )
        return disk
