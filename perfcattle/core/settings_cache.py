# -*- encoding: utf-8 -*-
"""Local cache for APM page settings."""
from __future__ import annotations

import json
import os
import tempfile

from logzero import logger


class ApmSettingsCache:
    """Persist settings panel values across PerfCattle restarts."""

    CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".perfcattle")
    CACHE_PATH = os.path.join(CONFIG_DIR, "apm_settings.json")
    DEFAULTS = {
        "cpuWarning": 0,
        "memWarning": 0,
        "fpsWarning": 0,
        "netdataRecvWarning": 0,
        "netdataSendWarning": 0,
        "betteryWarning": 0,
        "gpuWarning": 0,
        "duration": 0,
        "perfcattle_host": "",
        "host_switch": "0",
        "recordMaxSize": "720",
        "recordMaxFps": "60",
        "recordVideoBitRate": "4M",
        "recordNoAudio": "1",
        "defaultPackageName": "",
        "defaultProcessName": "",
        "collectCpu": "1",
        "collectMemory": "1",
        "collectNetwork": "1",
        "collectNetworkWifi": "1",
        "collectFileIO": "1",
        "collectFps": "1",
        "collectFpsSurfaceView": "1",
        "collectRecordScreen": "0",
        "collectGpu": "1",
        "collectGpuStats": "0",
        "collectBattery": "0",
        "collectMemoryDetail": "0",
        "collectThermal": "0",
        "collectCpuCore": "0",
        "collectDisk": "0",
        "collectEnergy": "0",
    }

    @classmethod
    def load(cls):
        settings = dict(cls.DEFAULTS)
        try:
            with open(cls.CACHE_PATH, "r", encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            return settings
        except Exception as exc:
            logger.warning("load apm settings cache failed: %s", exc)
            return settings

        if isinstance(data, dict):
            for key in cls.DEFAULTS:
                value = data.get(key)
                if value not in (None, "NaN"):
                    settings[key] = value
        return settings

    @classmethod
    def save(cls, values):
        settings = cls.load()
        if values:
            for key in cls.DEFAULTS:
                try:
                    value = values.get(key)
                except AttributeError:
                    value = values[key] if key in values else None
                if value is not None:
                    settings[key] = value
        cls.save_all(settings)
        return settings

    @classmethod
    def save_all(cls, settings):
        cache_dir = os.path.dirname(os.path.abspath(cls.CACHE_PATH))
        os.makedirs(cache_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=".apm_settings.", suffix=".json", dir=cache_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(settings, file, ensure_ascii=False, indent=2)
            os.replace(temp_path, cls.CACHE_PATH)
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise
