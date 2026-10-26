# -*- coding: utf-8 -*-
"""Unity 采集管理器。

职责边界：

- 选择采集后端（helper 优先，协议兜底）
- 管理 ``report/unity_<stamp>`` 暂存目录、文件清单和 meta.json
- 生成报告前把暂存目录改名为 ``unity_capture``，交给报告归档

具体传输与协议解析在 ``perfcattle.unity.backends`` / ``perfcattle.unity.protocol``，
版本差异在 ``perfcattle.unity.profiles``。
"""
import datetime
import json
import os
import shutil
import threading
import time

from logzero import logger

from perfcattle.unity.backends import (
    DEVICE_DIR_TEMPLATE,
    HelperBackend,
    ProtocolBackend,
)
from perfcattle.unity.profiles import default_areas as _default_areas
from perfcattle.unity.profiles import supported_areas as _supported_areas
from perfcattle.unity.profiles import supported_versions as _supported_versions


def _report_dir():
    path = os.path.join(os.getcwd(), "report")
    os.makedirs(path, exist_ok=True)
    return path


class UnityCapture(object):
    """Unity Profiler / MemoryProfiler 采集会话管理器。"""

    # 自动重连的轮询间隔：首次失败后 2 秒，逐步退避到 5 秒。
    # 只在勾选 Unity Profiler 且尚未连接时运行，连接成功后立即停止。
    AUTO_CONNECT_INTERVAL = 2.0
    AUTO_CONNECT_MAX_INTERVAL = 5.0

    def __init__(self):
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
        self._stats_thread = None
        self._stats_stop = threading.Event()
        self._stats_lock = threading.Lock()
        self._stats_backend = None
        self._stats_available = False
        self._stats_mode = None
        self._stats_version = None
        # 连接（arm）只提供实时面板数据；只有点 Start 后才把 Unity 数据
        # 写入 report/unity_stats.log 和 profiler.raw。
        self.recording = False
        self._connected = threading.Event()
        self._auto_generation = 0
        self._auto_stop_event = None
        self._auto_target = None
        self._auto_options = {}
        self._auto_thread = None
        self._session_owner = None
        self._raw_complete = None
        self._stream_error = None

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

    def _adopt(self, backend, info, owner=None):
        self.backend = backend
        self.mode = backend.mode
        self._session_owner = owner
        self.unity_version = info.get("unity_version")
        self.product_name = info.get("product_name")
        self.remote_port = info.get("port")
        self.deep_profiling = bool(info.get("deep_profiling", False))
        self.areas = info.get("areas")
        self.enabled_categories = info.get("enabled_categories")
        self.ignored_options = info.get("ignored_options") or []
        self.available = True
        self._connected.set()
        with self._stats_lock:
            self._stats_backend = backend
            self._stats_available = True
            self._stats_mode = self.mode
            self._stats_version = self.unity_version
        return self.status()

    def _clear_stats_refs(self):
        with self._stats_lock:
            self._stats_backend = None
            self._stats_available = False
            self._stats_mode = None
            self._stats_version = None

    # ----------------------------------------------------- stats logger

    def _start_stats_logger(self):
        self._stop_stats_logger()
        self._stats_stop.clear()
        thread = threading.Thread(
            target=self._stats_logger_loop, name="unity-stats-logger"
        )
        thread.daemon = True
        self._stats_thread = thread
        thread.start()

    def _stop_stats_logger(self):
        self._stats_stop.set()
        thread = self._stats_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._stats_thread = None

    def begin_recording(self):
        """开始把 Unity 数据写入本次报告。

        连接阶段（arm）只轮询实时指标给面板看；点 Start 后调用这里才
        清空 ``unity_stats.log``、让后端把原始流落到 ``profiler.raw``，
        并启动每秒写日志的线程。
        """
        with self.lock:
            if not self.available or self.backend is None:
                return {
                    "status": 0,
                    "available": False,
                    "msg": self.last_error or "unity capture is not running",
                }
            first = not self.recording
            self.recording = True
            if first:
                self.started_at = time.time()
            backend = self.backend
        if not first:
            # A duplicate Start must not rotate the helper log again.
            if not (self._stats_thread and self._stats_thread.is_alive()):
                self._start_stats_logger()
            result = self.status()
            result["status"] = 1
            return result
        # Backend 可能在设备上轮转文件（helper），不要占着捕获锁，
        # 否则实时面板会被阻塞。
        try:
            begin_record = getattr(backend, "begin_record", None)
            if begin_record is not None:
                begin_record()
        except Exception as e:
            logger.warning("[Unity] cannot begin recording: {}".format(e))
        self._stop_stats_logger()
        self._reset_stats_log()
        self._start_stats_logger()
        result = self.status()
        result["status"] = 1
        return result

    def end_recording(self):
        """停止写入 Unity 日志；连接本身保持不变。"""
        self.recording = False
        self._stop_stats_logger()

    @staticmethod
    def _reset_stats_log():
        path = os.path.join(_report_dir(), "unity_stats.log")
        try:
            with open(path, "w", encoding="utf-8"):
                pass
        except OSError as e:
            logger.debug("[Unity] cannot reset stats log: {}".format(e))

    def _stats_logger_loop(self):
        path = os.path.join(_report_dir(), "unity_stats.log")
        while not self._stats_stop.is_set():
            try:
                data = self.stats()
                if data.get("available"):
                    stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")
                    payload = dict(data.get("values") or {})
                    payload["frame"] = data.get("frame")
                    with open(path, "a", encoding="utf-8") as fp:
                        fp.write(
                            "{}={}\n".format(
                                stamp,
                                json.dumps(
                                    payload,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            )
                        )
            except Exception as e:
                logger.debug("[Unity] stats log failed: {}".format(e))
            self._stats_stop.wait(1.0)

    # ------------------------------------------------------------- api

    def start(self, deviceId, pkgName, pid=None, deep_profiling=False, areas=None,
              quiet=False, owner=None, record=False):
        """连接 Unity Player，优先 helper，失败后回退到协议模式。

        ``areas`` 为 None 时保留 Player 默认 Category；传入列表时按 Unity
        Profiler Modules 的语义整体替换。helper 模式不支持这两个选项，
        会在 ``ignored_options`` 中返回。``quiet`` 用于后台轮询，避免每次
        重试都写 warning 日志。

        ``record=False`` 时只连接并让面板能读实时指标（勾选开关后的
        arm 行为）；点 Start 后才用 ``record=True`` 开始写报告文件。
        """
        with self.lock:
            available = self.available
        if available:
            if record:
                return self.begin_recording()
            # A duplicate start (page reload / double click) must not kill
            # the stats logger of the live session.
            with self.lock:
                if self.recording and not (
                    self._stats_thread and self._stats_thread.is_alive()
                ):
                    self._start_stats_logger()
                return self.status()
        self._stop_stats_logger()
        with self.lock:
            if self.available:
                # A concurrent start won the race; it already owns the session.
                if record:
                    result = None
                else:
                    if self.recording and not (
                        self._stats_thread and self._stats_thread.is_alive()
                    ):
                        self._start_stats_logger()
                    return self.status()
            else:
                self._connected.clear()
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
                self.recording = False
                self.staging_dir = self._new_staging_dir()
                helper = HelperBackend(
                    deviceId, pkgName, pid, self.device_dir, self.staging_dir
                )
                helper_error = None
                try:
                    info = helper.start(
                        deep_profiling=deep_profiling, areas=areas
                    )
                    self.started_at = time.time()
                    result = self._adopt(helper, info, owner=owner)
                except Exception as e:
                    helper_error = str(e)
                    try:
                        helper.close()
                    except Exception:
                        pass
                    if not quiet:
                        logger.info(
                            "[Unity] helper unavailable ({}), trying PlayerConnection protocol".format(e)
                        )
                    protocol = ProtocolBackend(
                        deviceId, pkgName, pid, self.staging_dir
                    )
                    try:
                        info = protocol.start(
                            deep_profiling=deep_profiling, areas=areas
                        )
                        self.started_at = time.time()
                        result = self._adopt(protocol, info, owner=owner)
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
                        self._session_owner = None
                        self._connected.clear()
                        self._clear_stats_refs()
                        self.deep_profiling = False
                        self.areas = None
                        self.enabled_categories = None
                        self.recording = False
                        # 轮询重连时不能每 2 秒留下一个空的暂存目录。
                        if self.staging_dir and os.path.isdir(self.staging_dir):
                            shutil.rmtree(self.staging_dir, ignore_errors=True)
                        self.staging_dir = None
                        if quiet:
                            logger.debug(
                                "[Unity] capture unavailable: {}".format(
                                    self.last_error
                                )
                            )
                        else:
                            logger.warning(
                                "[Unity] capture unavailable: {}".format(
                                    self.last_error
                                )
                            )
                        self._stop_stats_logger()
                        return {
                            "status": 1,
                            "available": False,
                            "msg": self.last_error,
                        }
        if record:
            return self.begin_recording()
        return result or self.status()

    def stats(self):
        """Return live Unity counters for the game panel.

        Only the helper backend can expose counters; protocol mode streams raw
        profiler data and reports ``reason=protocol_mode``.

        The backend call runs outside ``self.lock`` because a MemoryProfiler
        snapshot holds that lock for its whole timeout and must not block the
        live panel.
        """
        with self._stats_lock:
            available = self._stats_available
            backend = self._stats_backend
            mode = self._stats_mode
            unity_version = self._stats_version
            last_error = self.last_error
        if not available or backend is None:
            return {
                "status": 0,
                "available": False,
                "reason": "not_running",
                "msg": last_error or "unity capture is not running",
            }
        try:
            data = dict(backend.stats())
        except Exception as e:
            logger.warning("[Unity] stats failed: {}".format(e))
            return {
                "status": 0,
                "available": False,
                "reason": "stats_failed",
                "msg": str(e),
            }
        data["status"] = 1
        data["mode"] = mode
        data["unity_version"] = unity_version
        return data

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

    def stop(self, discard=False, cancel_auto=True):
        """停止采集、完成文件落盘并写 meta。

        ``discard=True`` 用于关闭尚未开始正式录制的自动连接，避免在
        ``report/`` 下留下没有归属的暂存目录。``cancel_auto=False`` 供
        自动重连线程清理自己创建的会话时使用，避免误停新的目标。
        """
        if cancel_auto:
            self._cancel_auto()
        self.recording = False
        self._stop_stats_logger()
        with self.lock:
            result = {"status": 1, "files": []}
            if self.device is None:
                return {"status": 1, "msg": "no unity session"}
            if self.backend is None and self.mode is None:
                # Duplicate stop (UI stop + report generation) must not
                # overwrite meta.json with an empty mode.
                return {"status": 1, "msg": "no unity session"}
            if not self.staging_dir:
                self.staging_dir = self._new_staging_dir()
            os.makedirs(self.staging_dir, exist_ok=True)
            backend = self.backend
            if backend is not None:
                try:
                    result = (
                        backend.stop(discard=True)
                        if discard
                        else backend.stop()
                    )
                    self.files = list(backend.files)
                    self._raw_complete = result.get("raw_complete")
                    self._stream_error = result.get("stream_error")
                except Exception as e:
                    logger.warning("[Unity] stop failed: {}".format(e))
                    result = {"status": 0, "msg": str(e)}
            try:
                self._write_meta()
            except Exception as e:
                logger.warning("[Unity] cannot write meta.json: {}".format(e))
            finally:
                self.available = False
                self._session_owner = None
                self._connected.clear()
                self.mode = None
                self.backend = None
                self._clear_stats_refs()
                self.remote_port = None
                self.deep_profiling = False
                self.areas = None
                self.enabled_categories = None
                self.ignored_options = []
            result["files"] = self.files
            if (
                discard
                and self.staging_dir
                and os.path.basename(self.staging_dir) != "unity_capture"
            ):
                shutil.rmtree(self.staging_dir, ignore_errors=True)
                self.staging_dir = None
            return result

    def status(self):
        backend_status = self.backend.status() if self.backend else {}
        return {
            "status": 1,
            "available": self.available,
            "recording": bool(self.recording and self.available),
            "arming": self._auto_target is not None and not self.available,
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
            "metrics_supported": backend_status.get("metrics_supported"),
            "metrics_reason": backend_status.get("metrics_reason"),
            "raw_complete": backend_status.get("raw_complete"),
            "stream_error": backend_status.get("stream_error"),
            "supported_versions": self.supported_versions(),
            "supported_areas": self.supported_areas(),
            "default_areas": self.default_areas(),
            "msg": self.last_error or "",
        }

    # ------------------------------------------------ auto connect

    def _target_key(self, deviceId, pkgName, pid):
        return (deviceId, pkgName, pid)

    def _cancel_auto(self):
        """停止后台轮询；不触碰正在进行的 Unity 会话。"""
        self._auto_generation += 1
        event = self._auto_stop_event
        self._auto_stop_event = None
        self._auto_target = None
        self._auto_options = {}
        if event is not None:
            event.set()

    def arm(self, deviceId, pkgName, pid=None, deep_profiling=False, areas=None,
            record=False):
        """在勾选 Unity Profiler 后立即尝试连接，并持续轮询。

        轮询只在尚未连接时运行；连接成功或调用 ``disarm`` 后自动退出。
        失败不会留下暂存目录，也不会启动 Profiler 流之外的重活。

        ``record=False`` 是默认行为：连上只给实时面板提供数据；在录制
        过程中打开开关时传 ``record=True``，连接成功即开始写入报告。
        """
        if not deviceId or not pkgName:
            return {
                "status": 0,
                "available": False,
                "arming": False,
                "msg": "select a device and app first",
            }
        target = self._target_key(deviceId, pkgName, pid)
        options = {
            "deep_profiling": bool(deep_profiling),
            "areas": list(areas) if areas is not None else None,
            "record": bool(record),
        }
        with self.lock:
            same_target = (
                self.available
                and self.device == deviceId
                and self.pkg == pkgName
                and (pid is None or self.pid is None or self.pid == pid)
            )
        if same_target:
            if record:
                return self.begin_recording()
            with self.lock:
                if self.recording and not (
                    self._stats_thread and self._stats_thread.is_alive()
                ):
                    self._start_stats_logger()
                result = self.status()
                result["arming"] = False
                return result
        with self.lock:
            if self.available:
                # A capture for another app is live; the desired target wins.
                # It was opened by auto-connect, not by a report, so drop it.
                self.stop(discard=True)
            if (
                self._auto_target == target
                and self._auto_options == options
                and self._auto_thread is not None
                and self._auto_thread.is_alive()
            ):
                result = self.status()
                result["arming"] = True
                return result
            self._cancel_auto()
            self._auto_generation += 1
            generation = self._auto_generation
            stop_event = threading.Event()
            self._auto_stop_event = stop_event
            self._auto_target = target
            self._auto_options = options
            thread = threading.Thread(
                target=self._auto_connect_loop,
                args=(stop_event, generation),
                name="unity-auto-connect",
            )
            thread.daemon = True
            self._auto_thread = thread
            thread.start()
            result = self.status()
            result["arming"] = True
            return result

    def _stop_owner_session(self, generation):
        """Disarm race cleanup: stop only the session this thread created."""
        with self.lock:
            if not self.available or self._session_owner != generation:
                return
            self.stop(discard=True, cancel_auto=False)

    def _auto_connect_loop(self, stop_event, generation):
        delay = self.AUTO_CONNECT_INTERVAL
        while not stop_event.is_set() and generation == self._auto_generation:
            with self.lock:
                # The target may have been replaced while this thread waited
                # for the lock; never attach on behalf of a stale generation.
                if stop_event.is_set() or generation != self._auto_generation:
                    return
                if self.available or self._auto_target is None:
                    return
                target = self._auto_target
                options = dict(self._auto_options)
            result = self.start(
                target[0],
                target[1],
                pid=target[2],
                deep_profiling=options.get("deep_profiling", False),
                areas=options.get("areas"),
                quiet=True,
                owner=generation,
                record=options.get("record", False),
            )
            if stop_event.is_set() or generation != self._auto_generation:
                if result.get("available"):
                    # disarm raced with a successful attach: this capture was
                    # never part of a recording, so do not archive it.
                    self._stop_owner_session(generation)
                return
            if result.get("available"):
                return
            logger.debug(
                "[Unity] auto connect retry: {}".format(
                    result.get("msg") or "unavailable"
                )
            )
            if stop_event.wait(delay):
                return
            delay = min(self.AUTO_CONNECT_MAX_INTERVAL, delay + 1.0)

    def disarm(self, stop=False, discard=False):
        """取消自动轮询；``stop=True`` 时同时关闭当前 Unity 会话。"""
        self._cancel_auto()
        self._auto_thread = None
        with self.lock:
            if stop and self.available:
                return self.stop(discard=discard)
        return self.status()

    def wait_connected(self, timeout=15.0):
        """等待 Unity 连接；超时返回最近一次失败原因。"""
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = 15.0
        timeout = max(0.0, min(timeout, 60.0))
        deadline = time.time() + timeout
        while True:
            if self.available:
                return self.status()
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if self._connected.wait(min(0.25, remaining)):
                return self.status()
        result = self.status()
        result["available"] = False
        result["status"] = 1
        if not result.get("msg"):
            result["msg"] = "unity is not connected"
        return result

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
            "raw_complete": self._raw_complete,
            "stream_error": self._stream_error,
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
