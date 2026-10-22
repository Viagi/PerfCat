# -*- coding: utf-8 -*-
"""Unity 采集管理器。

职责边界：

- 选择采集后端（helper 优先，协议兜底）
- 管理 ``report/unity_<stamp>`` 暂存目录、文件清单和 meta.json
- 生成报告前把暂存目录改名为 ``unity_capture``，交给报告归档

具体传输与协议解析在 ``perfcat.unity.backends`` / ``perfcat.unity.protocol``，
版本差异在 ``perfcat.unity.profiles``。
"""
import os
import shutil
import time

from logzero import logger

from perfcat.unity.backends import (
    DEVICE_DIR_TEMPLATE,
    HelperBackend,
    ProtocolBackend,
)
from perfcat.unity.profiles import default_areas as _default_areas
from perfcat.unity.profiles import supported_areas as _supported_areas
from perfcat.unity.profiles import supported_versions as _supported_versions


def _report_dir():
    path = os.path.join(os.getcwd(), "report")
    os.makedirs(path, exist_ok=True)
    return path


class UnityCapture(object):
    """Unity Profiler / MemoryProfiler 采集会话管理器。"""

    def __init__(self):
        import threading

        self.lock = threading.RLock()
        self.device = None
        self.pkg = None
        self.pid = None
        self.device_dir = None
        self.staging_dir = None
        self.started_at = None
        self.unity_version = None
        self.product_name = None
        self.available = False
        self.last_error = None
        self.files = []
        self.mode = None
        self.backend = None
        self.remote_port = None
        self.deep_profiling = False
        self.areas = None
        self.enabled_categories = None
        self.ignored_options = []

    # --------------------------------------------------------- helpers

    def _new_staging_dir(self):
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        path = os.path.join(_report_dir(), "unity_{}".format(stamp))
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def supported_versions():
        return _supported_versions()

    @staticmethod
    def supported_areas():
        return _supported_areas()

    @staticmethod
    def default_areas():
        return _default_areas()

    def _adopt(self, backend, info):
        self.backend = backend
        self.mode = backend.mode
        self.unity_version = info.get("unity_version")
        self.product_name = info.get("product_name")
        self.remote_port = info.get("port")
        self.deep_profiling = bool(info.get("deep_profiling", False))
        self.areas = info.get("areas")
        self.enabled_categories = info.get("enabled_categories")
        self.ignored_options = info.get("ignored_options") or []
        self.available = True
        return self.status()

    # ------------------------------------------------------------- api

    def start(self, deviceId, pkgName, pid=None, deep_profiling=False, areas=None):
        """连接 Unity Player，优先 helper，失败后回退到协议模式。

        ``areas`` 为 None 时保留 Player 默认 Category；传入列表时按 Unity
        Profiler Modules 的语义整体替换。helper 模式不支持这两个选项，
        会在 ``ignored_options`` 中返回。
        """
        with self.lock:
            if self.available:
                return self.status()
            self.device = deviceId
            self.pkg = pkgName
            self.pid = pid
            self.device_dir = DEVICE_DIR_TEMPLATE.format(pkg=pkgName)
            self.last_error = None
            self.mode = None
            self.backend = None
            self.files = []
            self.deep_profiling = bool(deep_profiling)
            self.areas = areas
            self.enabled_categories = None
            self.ignored_options = []
            self.staging_dir = self._new_staging_dir()
            helper = HelperBackend(
                deviceId, pkgName, pid, self.device_dir, self.staging_dir
            )
            helper_error = None
            try:
                info = helper.start(deep_profiling=deep_profiling, areas=areas)
                self.started_at = time.time()
                return self._adopt(helper, info)
            except Exception as e:
                helper_error = str(e)
                try:
                    helper.close()
                except Exception:
                    pass
                logger.info(
                    "[Unity] helper unavailable ({}), trying PlayerConnection protocol".format(e)
                )
            protocol = ProtocolBackend(deviceId, pkgName, pid, self.staging_dir)
            try:
                info = protocol.start(deep_profiling=deep_profiling, areas=areas)
                self.started_at = time.time()
                return self._adopt(protocol, info)
            except Exception as e:
                try:
                    protocol.close()
                except Exception:
                    pass
                self.last_error = (
                    "{}; protocol: {}".format(helper_error, e)
                    if helper_error
                    else str(e)
                )
                self.available = False
                self.mode = None
                self.backend = None
                self.deep_profiling = False
                self.areas = None
                self.enabled_categories = None
                logger.warning("[Unity] capture unavailable: {}".format(self.last_error))
                return {"status": 1, "available": False, "msg": self.last_error}

    def snapshot(self, timeout=300):
        """抓取一次 MemoryProfiler 快照。"""
        with self.lock:
            if not self.available or self.backend is None:
                return {
                    "status": 0,
                    "msg": self.last_error or "unity capture is not running",
                }
            name = time.strftime("memory_%Y%m%d_%H%M%S", time.localtime())
            try:
                data = self.backend.snapshot(name)
            except Exception as e:
                logger.exception(e)
                return {"status": 0, "msg": str(e)}
            self.files.append({"name": data["name"], "size": data["size"]})
            logger.info(
                "[Unity] memory snapshot saved: {} ({} bytes)".format(
                    data["file"], data["size"]
                )
            )
            return {
                "status": 1,
                "file": data["file"],
                "name": data["name"],
                "size": data["size"],
            }

    def apply_options(self, deep_profiling=None, areas=None):
        """在采集过程中修改 Deep Profiling / Profiler Modules。

        与 Unity Profiler 窗口的行为一致：Category 选择立即生效；
        Deep Profiling 只对后续帧生效，且需要构建时开启 Deep Profiling Support。
        """
        with self.lock:
            if not self.available or self.backend is None:
                return {
                    "status": 0,
                    "msg": self.last_error or "unity capture is not running",
                }
            if deep_profiling is None and areas is None:
                return self.status()
            try:
                info = self.backend.set_options(
                    deep_profiling=deep_profiling, areas=areas
                )
            except Exception as e:
                logger.warning("[Unity] apply options failed: {}".format(e))
                return {"status": 0, "msg": str(e)}
            self.deep_profiling = info["deep_profiling"]
            self.areas = info["areas"]
            self.enabled_categories = info.get("enabled_categories")
            try:
                self._write_meta()
            except Exception as e:
                logger.warning("[Unity] cannot update meta.json: {}".format(e))
            result = self.status()
            result["status"] = 1
            return result

    def stop(self):
        """停止采集、完成文件落盘并写 meta。"""
        with self.lock:
            result = {"status": 1, "files": []}
            if self.device is None:
                return {"status": 1, "msg": "no unity session"}
            if not self.staging_dir:
                self.staging_dir = self._new_staging_dir()
            os.makedirs(self.staging_dir, exist_ok=True)
            backend = self.backend
            if backend is not None:
                try:
                    result = backend.stop()
                    self.files = list(backend.files)
                except Exception as e:
                    logger.warning("[Unity] stop failed: {}".format(e))
                    result = {"status": 0, "msg": str(e)}
            try:
                self._write_meta()
            except Exception as e:
                logger.warning("[Unity] cannot write meta.json: {}".format(e))
            finally:
                self.available = False
                self.mode = None
                self.backend = None
                self.remote_port = None
                self.deep_profiling = False
                self.areas = None
                self.enabled_categories = None
                self.ignored_options = []
            result["files"] = self.files
            return result

    def status(self):
        backend_status = self.backend.status() if self.backend else {}
        return {
            "status": 1,
            "available": self.available,
            "mode": self.mode,
            "unity_version": self.unity_version,
            "product_name": self.product_name,
            "device_dir": self.device_dir,
            "staging_dir": self.staging_dir,
            "files": self.files,
            "bytes": backend_status.get("bytes"),
            "deep_profiling": self.deep_profiling,
            "areas": self.areas,
            "enabled_categories": backend_status.get(
                "enabled_categories", self.enabled_categories
            ),
            "ignored_options": self.ignored_options,
            "supported_versions": self.supported_versions(),
            "supported_areas": self.supported_areas(),
            "default_areas": self.default_areas(),
            "msg": self.last_error or "",
        }

    def finalize(self):
        """把本次暂存目录改名为 make_report 识别的固定目录。"""
        with self.lock:
            if not self.staging_dir or not os.path.isdir(self.staging_dir):
                return None
            target = os.path.join(_report_dir(), "unity_capture")
            if os.path.exists(target):
                orphan = os.path.join(
                    _report_dir(),
                    "unity_orphan_{}".format(
                        time.strftime(
                            "%Y%m%d_%H%M%S",
                            time.localtime(os.path.getmtime(target)),
                        )
                    ),
                )
                try:
                    if os.path.exists(orphan):
                        shutil.rmtree(orphan, ignore_errors=True)
                    os.rename(target, orphan)
                except OSError:
                    shutil.rmtree(target, ignore_errors=True)
            try:
                os.rename(self.staging_dir, target)
            except OSError as e:
                logger.warning("[Unity] finalize failed: {}".format(e))
                return None
            self.staging_dir = target
            return target

    def _write_meta(self):
        if not self.staging_dir or not os.path.isdir(self.staging_dir):
            return
        backend = self.backend
        meta = {
            "app": self.pkg,
            "device": self.device,
            "pid": self.pid,
            "mode": self.mode or "unknown",
            "unity_version": self.unity_version,
            "product_name": self.product_name,
            "device_dir": self.device_dir,
            "helper_port": self.remote_port,
            "profiler_log": getattr(backend, "remote_log", None),
            "started_at": time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(self.started_at or time.time()),
            ),
            "stopped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "deep_profiling": self.deep_profiling,
            "areas": self.areas,
            "enabled_categories": self.enabled_categories,
            "ignored_options": self.ignored_options,
            "files": self.files,
            "note": (
                "raw captures only; open .snapshot with Memory Profiler, "
                "profiler.raw with a Unity profiler binary-log reader"
            ),
        }
        import json

        with open(
            os.path.join(self.staging_dir, "meta.json"), "w", encoding="utf-8"
        ) as fp:
            json.dump(meta, fp, ensure_ascii=False, indent=2)


unity_capture = UnityCapture()
