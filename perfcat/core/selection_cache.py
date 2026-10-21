# -*- encoding: utf-8 -*-
"""Local cache for APM target selections."""
from __future__ import annotations

import json
import os
import tempfile

from logzero import logger


class ApmSelectionCache:
    """Persist selected device, package and process names across PerfCat restarts."""

    CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".perfcat")
    CACHE_PATH = os.path.join(CONFIG_DIR, "apm_selection.json")

    @staticmethod
    def _ios_target_names(targets):
        result = {}
        for key in ("process", "network_process"):
            value = targets.get(key, "")
            if not isinstance(value, str) or len(value) > 512:
                raise ValueError("Invalid iOS process preferences")
            prefix, separator, name = value.partition(":")
            result[key] = name if separator and prefix.isascii() and prefix.isdecimal() else value
        return result

    @classmethod
    def load_all(cls):
        try:
            with open(cls.CACHE_PATH, "r", encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning("load apm selection cache failed: %s", exc)
            return {}

        if not isinstance(data, dict):
            return {}
        # Migrate old PID/start-time preferences in memory; the next save persists names only.
        ios = data.get("iOS", {})
        devices = ios.get("devices", {}) if isinstance(ios, dict) else {}
        if isinstance(devices, dict):
            for selection in devices.values():
                if isinstance(selection, dict) and isinstance(selection.get("iosTargets"), dict):
                    try:
                        selection["iosTargets"] = cls._ios_target_names(selection["iosTargets"])
                    except ValueError:
                        selection.pop("iosTargets", None)
        return data

    @classmethod
    def save_all(cls, cache):
        cache_dir = os.path.dirname(os.path.abspath(cls.CACHE_PATH))
        os.makedirs(cache_dir, exist_ok=True)
        cache = cache if isinstance(cache, dict) else {}
        fd, temp_path = tempfile.mkstemp(
            prefix=".apm_selection.", suffix=".json", dir=cache_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(cache, file, ensure_ascii=False, indent=2)
            os.replace(temp_path, cls.CACHE_PATH)
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise

    @classmethod
    def save_selection(
        cls, platform, device, package_name=None, process_name=None, ios_targets=None
    ):
        if not platform or not device:
            raise ValueError("platform and device are required")

        cache = cls.load_all()
        platform_key = str(platform)
        device_key = str(device)
        package_provided = package_name is not None
        process_provided = process_name is not None
        package_value = str(package_name).strip() if package_provided else None
        process_value = str(process_name).strip() if process_provided else None

        if package_provided and not package_value:
            return cache
        if platform_key == "iOS" and ios_targets is not None:
            if not isinstance(ios_targets, dict):
                raise ValueError("Invalid iOS process preferences")
            ios_targets = cls._ios_target_names(ios_targets)

        platform_cache = cache.setdefault(platform_key, {})
        devices = platform_cache.setdefault("devices", {})
        selection = devices.setdefault(device_key, {})
        previous_package = selection.get("packageName") or ""

        if package_provided:
            selection["packageName"] = package_value
        if platform_key == "iOS":
            if package_provided and previous_package != package_value:
                selection.pop("iosTargets", None)
            if ios_targets is not None:
                selection["iosTargets"] = ios_targets
        if process_provided:
            if process_value:
                selection["processName"] = process_value
            elif package_provided and previous_package != package_value:
                selection["processName"] = ""

        platform_cache["lastDevice"] = device_key
        devices[device_key] = selection
        cache[platform_key] = platform_cache
        cls.save_all(cache)
        return cache
