# -*- coding: utf-8 -*-
"""温度传感器采集器"""
import datetime
import json
import math
import os
import re
import time

from logzero import logger

from perfcattle.core.platform import Platform
from perfcattle.core.adb_client import adb
from perfcattle.collector.base import ReusableCollector, adb_shell
from perfcattle.core.metrics import MetricStatus


class ThermalSensor(ReusableCollector):
    ADB_TIMEOUT = 10
    MIN_TEMP_CELSIUS = 5
    MAX_TEMP_CELSIUS = 150
    SAMPLER_INTERVAL_SECONDS = 1.0
    SAMPLER_STALE_TTL_SECONDS = 10.0
    _thermal_cache = {}
    # Legacy msm-tsens.c TSENS_TYPE3 exposes decidegrees, not millidegrees.
    # https://github.com/LineageOS/android_kernel_xiaomi_sdm660/blob/lineage-18.1/drivers/thermal/msm-tsens.c
    DECIDEGREE_TSENS = {
        "qcom,msm8996-tsens", "qcom,msmtitanium-tsens",
        "qcom,msm8998-tsens", "qcom,msmhamster-tsens",
        "qcom,sdm660-tsens", "qcom,sdm630-tsens",
    }
    GAME_TEMPERATURE_GROUPS = (
        (
            "CPU",
            (
                re.compile(r"^cpuss-\d+$"),
                re.compile(r"^cpu[-_].*"),
                re.compile(r"^cpu.*therm.*"),
            ),
        ),
        (
            "GPU",
            (
                re.compile(r"^gpuss-\d+$"),
                re.compile(r"^gpu[-_].*"),
                re.compile(r"^gpu.*therm.*"),
            ),
        ),
        (
            "Memory",
            (
                re.compile(r"^ddr$"),
                re.compile(r".*ddr.*"),
                re.compile(r".*memory.*"),
            ),
        ),
        (
            "Battery",
            (
                re.compile(r"^battery$"),
                re.compile(r".*battery.*therm.*"),
                re.compile(r".*skin.*"),
            ),
        ),
    )
    THERMAL_LOG_FILES = {
        "CPU": "thermal_cpu.log",
        "GPU": "thermal_gpu.log",
        "Memory": "thermal_memory.log",
        "Battery": "thermal_battery.log",
    }

    def __init__(self, deviceId, platform=Platform.Android):
        self.deviceId = deviceId
        self.platform = platform
        self._last_thermal_temps = self._thermal_cache.get(self._cache_key())
        self._sensor_aliases = None
        self._tsens_decidegrees = False
        self.last_sample = None

    @classmethod
    def _make_cache_key(cls, platform, deviceId):
        return platform, deviceId

    @classmethod
    def _object_cache_key(cls, deviceId, platform=Platform.Android):
        return cls._make_cache_key(platform, deviceId)

    @classmethod
    def clear(cls):
        super().clear()
        cls._thermal_cache.clear()

    def _cache_key(self):
        return self._make_cache_key(self.platform, self.deviceId)

    def _read_thermal_zones(self):
        # read is a shell builtin: avoids two `cat` forks per thermal zone,
        # which makes Samsung devices with ~50 zones time out at 5s.
        cmd = (
            "printf '__RECORDS__\\n'; "
            'for z in /sys/class/thermal/thermal_zone*; do '
            'read -r t < "$z/type" 2>/dev/null || continue; '
            'read -r v < "$z/temp" 2>/dev/null || continue; '
            'printf "%s|%s|%s\\n" "$z" "$t" "$v"; done'
        )
        return adb_shell(adb, cmd=cmd, deviceId=self.deviceId, timeout=self.ADB_TIMEOUT)

    def _load_sensor_metadata(self):
        if self._sensor_aliases is not None:
            return
        self._sensor_aliases = {}
        cmd = (
            'cat /vendor/etc/thermal-engine.conf /system/etc/thermal-engine.conf '
            '2>/dev/null; printf "\\n__COMPATIBLE__\\n"; '
            'for p in /sys/firmware/devicetree/base/tsens*/compatible '
            '/sys/firmware/devicetree/base/soc/tsens*/compatible; do '
            'cat "$p" 2>/dev/null; printf "\\n"; done; '
            'printf "__PLATFORM__\\n"; getprop ro.board.platform; '
            'printf "__DRIVERS__\\n"; '
            'readlink /sys/bus/platform/devices/*tsens*/driver 2>/dev/null'
        )
        output = adb_shell(adb, cmd=cmd, deviceId=self.deviceId, timeout=self.ADB_TIMEOUT)
        config, marker, compatible = (output or "").partition("__COMPATIBLE__")
        compatible, _, hardware = compatible.partition("__PLATFORM__")
        board, _, drivers = hardware.partition("__DRIVERS__")
        aliases = {}
        for line in config.splitlines():
            match = re.fullmatch(r"\s*#\s*(tsens_tz_sensor\d+)\s*:\s*(.*?)\s*", line)
            if not match:
                continue
            sensor, alias = match.groups()
            alias = alias.lower()
            group = None
            if re.fullmatch(r"cpu\d*(?:[-, /]+cpu\d+)*", alias):
                group = "CPU"
            elif re.fullmatch(r"gpu\d*", alias):
                group = "GPU"
            # Conflicting or unrecognized declarations must not guess a group.
            aliases.setdefault(sensor, set()).add(group)
        self._sensor_aliases = {
            sensor: next(iter(groups)) for sensor, groups in aliases.items()
            if len(groups) == 1 and None not in groups
        }
        controllers = [line.strip().split("\x00")[0] for line in compatible.splitlines()
                       if line.strip("\x00 \r\n")]
        self._tsens_decidegrees = bool(
            marker and len(controllers) == 1 and controllers[0] in self.DECIDEGREE_TSENS
        )
        driver_names = [line.strip().rsplit("/", 1)[-1]
                        for line in drivers.splitlines() if line.strip()]
        if not controllers and driver_names == ["msm-tsens"]:
            self._tsens_decidegrees = board.strip() in {
                "sdm660", "sdm630", "msm8996", "msm8998",
            }
        logger.debug("Thermal metadata device=%s aliases=%s decidegrees=%s",
                     self.deviceId, self._sensor_aliases, self._tsens_decidegrees)

    @classmethod
    def _game_temperature_group(cls, sensor_type):
        normalized_type = str(sensor_type or "").strip().lower()
        if not normalized_type:
            return None
        for group, patterns in cls.GAME_TEMPERATURE_GROUPS:
            if any(pattern.match(normalized_type) for pattern in patterns):
                return group
        return None

    def _parse_thermal_zones(self, output):
        if (output or "").startswith("__RECORDS__"):
            zones = []
            for line in output.splitlines()[1:]:
                fields = line.strip().split("|")
                if len(fields) != 3:
                    continue
                path, sensor_type, temp = fields
                zone = path.rsplit("/", 1)[-1]
                if re.fullmatch(r"thermal_zone\d+", zone) and sensor_type and temp:
                    zones.append({"zone": zone, "type": sensor_type, "temp": temp})
            return zones

        # Keep legacy snapshots readable, but reject incomplete parallel lists.
        sections = {"zones": [], "types": [], "temps": []}
        current = None
        for raw_line in (output or "").splitlines():
            line = raw_line.strip()
            if line == "__ZONES__":
                current = "zones"
                continue
            if line == "__TYPES__":
                current = "types"
                continue
            if line == "__TEMPS__":
                current = "temps"
                continue
            if current and line:
                sections[current].append(line)

        size = len(sections["zones"])
        if len(sections["types"]) != size or len(sections["temps"]) != size:
            return []
        zones = []
        for index in range(size):
            zone_path = sections["zones"][index]
            zone = zone_path.rsplit("/", 1)[-1]
            if not re.match(r"^thermal_zone\d+$", zone):
                continue
            zones.append(
                {
                    "zone": zone,
                    "type": sections["types"][index].strip(),
                    "temp": sections["temps"][index].strip(),
                }
            )
        return zones

    @classmethod
    def _temp_celsius(cls, raw_temp):
        try:
            value = float(str(raw_temp).strip())
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        celsius = value / 1000 if abs(value) > 1000 else value
        if celsius < cls.MIN_TEMP_CELSIUS or celsius > cls.MAX_TEMP_CELSIUS:
            return None
        return celsius

    def _thermal_temps(self):
        sensors_by_group = {}
        thermal_zones = self._parse_thermal_zones(self._read_thermal_zones())
        if not thermal_zones:
            logger.warning("No thermal sensor found")
            return []

        for thermal_zone in thermal_zones:
            zone = thermal_zone["zone"]
            sensor_type = thermal_zone["type"]
            temp = thermal_zone["temp"]
            group = self._game_temperature_group(sensor_type)
            if group is None and re.fullmatch(r"tsens_tz_sensor\d+", sensor_type):
                self._load_sensor_metadata()
                group = self._sensor_aliases.get(sensor_type)
                # Only convert when both the driver ABI and sensor alias are known.
                if not group or not self._tsens_decidegrees:
                    continue
                try:
                    degrees = float(temp) / 10
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(degrees) or not (
                    self.MIN_TEMP_CELSIUS <= degrees <= self.MAX_TEMP_CELSIUS
                ):
                    continue
                temp = str(degrees * 1000)
            celsius = self._temp_celsius(temp)
            if not zone or not group or celsius is None:
                continue

            previous = sensors_by_group.get(group)
            if not previous or celsius > previous["celsius"]:
                sensors_by_group[group] = {
                    "name": group,
                    "type": group,
                    "zone": zone,
                    "source_type": sensor_type,
                    "temp": temp,
                    "celsius": celsius,
                }

        temp_list = []
        for group, _ in self.GAME_TEMPERATURE_GROUPS:
            sensor = sensors_by_group.get(group)
            if sensor:
                sensor.pop("celsius", None)
                temp_list.append(sensor)
        if temp_list:
            self._last_thermal_temps = temp_list
            self._thermal_cache[self._cache_key()] = list(temp_list)
        return temp_list

    def _log_thermal_temps(self, temp_list):
        _f = self._get_report_store()
        apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
        for sensor in temp_list:
            group = sensor.get("name") or sensor.get("type")
            filename = self.THERMAL_LOG_FILES.get(group)
            celsius = self._temp_celsius(sensor.get("temp"))
            if not filename or celsius is None:
                continue
            _f.add_log(
                os.path.join(_f.report_dir, filename),
                apm_time,
                round(celsius, 2),
            )

    def setInitalThermalTemp(self):
        """记录初始温度数据"""
        _f = self._get_report_store()
        temp_list = self._thermal_temps()
        if temp_list:
            content = json.dumps(temp_list)
            _f.create_file(filename="init_thermal_temp.json", content=content)

    def setCurrentThermalTemp(self):
        """记录当前温度数据"""
        _f = self._get_report_store()
        temp_list = self._thermal_temps()
        if temp_list:
            content = json.dumps(temp_list)
            _f.create_file(filename="current_thermal_temp.json", content=content)

    def getThermalType(self):
        """获取温度传感器类型列表"""
        cmd = "cat /sys/class/thermal/thermal_zone*/type"
        result = adb_shell(adb, cmd=cmd, deviceId=self.deviceId, timeout=self.ADB_TIMEOUT)
        typeList = result.splitlines()
        return typeList

    def getThermalTemp(self, noLog=True, unified=False):
        """获取温度数据"""
        try:
            temp_list = self._sampled_value(
                "thermal",
                self._thermal_temps,
                interval=self.SAMPLER_INTERVAL_SECONDS,
                stale_ttl=self.SAMPLER_STALE_TTL_SECONDS,
                background=self.platform == Platform.Android,
            )
        except Exception as e:
            temp_list = self._cached_sampled_value(
                "thermal", max_age=self.SAMPLER_STALE_TTL_SECONDS
            ) or []
            if temp_list:
                logger.warning("[Thermal] %s, using cached value", e)
            else:
                logger.exception(e)
        if noLog is False and temp_list:
            self._log_thermal_temps(temp_list)
        self.last_sample = {
            "sensors": temp_list if temp_list else None,
            "status": MetricStatus.CACHED.value if temp_list and self._cached_sampled_value("thermal", max_age=self.SAMPLER_STALE_TTL_SECONDS) is not None else (MetricStatus.OK.value if temp_list else MetricStatus.MISSING.value),
            "unit": "celsius", "sample_epoch_ms": int(time.time() * 1000),
        }
        if unified:
            return {"sensors": self.last_sample}
        return temp_list
