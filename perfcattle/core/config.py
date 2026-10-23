# -*- encoding: utf-8 -*-
"""采集服务配置管理。"""
import json
import os
import tempfile
import threading
from logzero import logger


class ServiceConfig:
    """性能采集服务的运行状态管理"""
    CONFIG_PATH = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "config.json"
    )
    _config_lock = threading.RLock()

    @classmethod
    def get_status(cls):
        with cls._config_lock:
            with open(file=cls.CONFIG_PATH, mode="r", encoding="utf-8") as file:
                return json.load(file).get("run_switch")

    @classmethod
    def start(cls):
        cls._write_status("on")

    @classmethod
    def stop(cls):
        cls._write_status("off")
        logger.info("stop perfcattle success")
        return True

    @classmethod
    def _write_status(cls, status):
        payload = {"run_switch": status}
        with cls._config_lock:
            directory = os.path.dirname(cls.CONFIG_PATH)
            fd, temp_path = tempfile.mkstemp(prefix="config.", suffix=".tmp", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(payload, file)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temp_path, cls.CONFIG_PATH)
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
