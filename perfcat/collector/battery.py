# -*- coding: utf-8 -*-
"""电池性能采集器"""
import datetime
import os
import re

import tidevice
from logzero import logger

from perfcat.core.platform import Platform
from perfcat.core.adb_client import adb
from perfcat.collector.base import ReusableCollector
from perfcat.tools.http_utils import Method


class Battery(ReusableCollector):
    ADB_TIMEOUT = 5
    SAMPLER_INTERVAL_SECONDS = 5.0
    SAMPLER_STALE_TTL_SECONDS = 20.0
    _battery_cache = {}

    def __init__(self, deviceId, platform=Platform.Android):
        self.deviceId = deviceId
        self.platform = platform
        self._last_battery = self._battery_cache.get(self._cache_key())
        self._android_battery_prepared = False

    @classmethod
    def _make_cache_key(cls, platform, deviceId):
        return platform, deviceId

    @classmethod
    def _object_cache_key(cls, deviceId, platform=Platform.Android):
        return cls._make_cache_key(platform, deviceId)

    @classmethod
    def clear(cls):
        super().clear()
        cls._battery_cache.clear()

    def _cache_key(self):
        return self._make_cache_key(self.platform, self.deviceId)

    def getBattery(self, noLog=False):
        if self.platform == Platform.Android:
            level, temperature = self.getAndroidBattery(noLog)
            return level, temperature
        else:
            temperature, current, voltage, power = self.getiOSBattery(noLog)
            return temperature, current, voltage, power

    def _sample_android_battery(self):
        if not self._android_battery_prepared:
            self.recoverBattery()
            self._android_battery_prepared = True
        cmd = "dumpsys battery"
        output = adb.shell(cmd=cmd, deviceId=self.deviceId, timeout=self.ADB_TIMEOUT)
        level = int(re.findall(r"level:\s?(\d+)", output)[0])
        temperature = int(re.findall(r"temperature:\s?(\d+)", output)[0]) / 10
        self._last_battery = (level, temperature)
        self._battery_cache[self._cache_key()] = self._last_battery
        return self._last_battery

    def getAndroidBattery(self, noLog=False):
        """获取 Android 电池信息，单位：%"""
        _f = self._get_report_store()
        has_sample = False
        try:
            level, temperature = self._sampled_value(
                "battery",
                self._sample_android_battery,
                interval=self.SAMPLER_INTERVAL_SECONDS,
                stale_ttl=self.SAMPLER_STALE_TTL_SECONDS,
                background=True,
            )
            has_sample = True
        except Exception as e:
            cached = self._cached_sampled_value(
                "battery", max_age=self.SAMPLER_STALE_TTL_SECONDS
            )
            used_cache = cached is not None
            if cached is not None:
                level, temperature = cached
                has_sample = True
            else:
                level, temperature = 0, 0
            if used_cache:
                logger.warning("[Battery] %s, using cached value", e)
            else:
                logger.exception(e)
        if noLog is False and has_sample:
            apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
            _f.add_log(
                os.path.join(_f.report_dir, "battery_level.log"), apm_time, level
            )
            _f.add_log(
                os.path.join(_f.report_dir, "battery_tem.log"), apm_time, temperature
            )
        return level, temperature

    def getiOSBattery(self, noLog=False):
        """获取 iOS 电池信息，单位：%"""
        _f = self._get_report_store()
        d = tidevice.Device(udid=self.deviceId)
        ioDict = d.get_io_power()
        tem = Method._setValue(ioDict["Diagnostics"]["IORegistry"]["Temperature"] / 100)
        current = Method._setValue(
            abs(ioDict["Diagnostics"]["IORegistry"]["InstantAmperage"])
        )
        voltage = Method._setValue(ioDict["Diagnostics"]["IORegistry"]["Voltage"])
        power = current * voltage / 1000
        if noLog is False:
            apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
            _f.add_log(
                os.path.join(_f.report_dir, "battery_tem.log"), apm_time, tem
            )  # unknown
            _f.add_log(
                os.path.join(_f.report_dir, "battery_current.log"), apm_time, current
            )  # mA
            _f.add_log(
                os.path.join(_f.report_dir, "battery_voltage.log"), apm_time, voltage
            )  # mV
            _f.add_log(os.path.join(_f.report_dir, "battery_power.log"), apm_time, power)
        return tem, current, voltage, power

    def recoverBattery(self):
        """重置手机充电状态"""
        cmd = "dumpsys battery reset"
        adb.shell(cmd=cmd, deviceId=self.deviceId, timeout=self.ADB_TIMEOUT)
