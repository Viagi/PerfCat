# -*- coding: utf-8 -*-
"""FPS 性能采集器"""
import datetime
import os
import queue
import re
import shlex
import threading
import time
import traceback
import time

from logzero import logger

from perfcattle.collector.base import (
    adb_shell,
    ReusableCollector,
    CachedSample,
    CollectorUnavailableError,
    create_devices,
    create_report_store,
)
from perfcattle.core.platform import Platform
from perfcattle.core.adb_client import adb
from perfcattle.core.metrics import MetricSample, MetricStatus


class SurfaceStatsCollector(object):
    """SurfaceFlinger 帧数据采集器"""

    ADB_TIMEOUT = 5
    FIRST_COLLECTION_DELAY_SECONDS = 0.25

    def __init__(
        self,
        device,
        frequency,
        package_name,
        fps_queue,
        jank_threshold,
        surfaceview,
        fps_monitor,
        use_global_latency_fallback=False,
    ):
        self.device = device
        self.frequency = frequency
        self.package_name = package_name
        self.jank_threshold = jank_threshold / 1000.0
        self.use_global_latency_fallback = use_global_latency_fallback
        self.surface_before = 0
        self.last_timestamp = 0
        self.data_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.focus_window = None
        self.surfaceview = surfaceview
        self.fps_queue = fps_queue
        # 引用所属的 FPSMonitor 实例，以便写入实例属性而非全局变量
        self.fps_monitor = fps_monitor
        self.collector_thread = None
        self.calculator_thread = None
        self._surface_diagnostic_logged = False
        self._surface_candidate_cache = None

    def _get_devices(self):
        return create_devices()

    def start(self, start_time):
        if not self.use_global_latency_fallback:
            try:
                self.focus_window = self.get_focus_activity()
                if self.surfaceview is True:
                    surface_names = self._surface_candidates()
                    if surface_names:
                        self.focus_window = surface_names[0]
                    self._clear_surfaceflinger_latency_data(
                        self._surface_clear_targets(surface_names)
                    )
                else:
                    self._clear_surfaceflinger_latency_data()
            except Exception:
                logger.warning(
                    "Unable to dynamically obtain the current activity name, using page_ Flip statistics full screen frame rate"
                )
                self.use_global_latency_fallback = True
                self.surface_before = self._get_global_surface_stats()
        else:
            logger.debug("dumpsys SurfaceFlinger --latency-clear is none")
            self.use_global_latency_fallback = True
            self.surface_before = self._get_global_surface_stats()
        self.collector_thread = threading.Thread(target=self._collector_thread)
        self.collector_thread.daemon = True
        self.collector_thread.start()
        self.calculator_thread = threading.Thread(
            target=self._calculator_thread, args=(start_time,)
        )
        self.calculator_thread.daemon = True
        self.calculator_thread.start()

    def stop(self):
        if self.collector_thread:
            self.stop_event.set()
            self.collector_thread.join(timeout=max(self.frequency * 2, self.ADB_TIMEOUT))
            self.collector_thread = None
        if self.calculator_thread:
            self.calculator_thread.join(timeout=max(self.frequency * 2, 2))
            self.calculator_thread = None
        self._mark_fps_queue_done()

    def _mark_fps_queue_done(self):
        if not self.fps_queue:
            return
        try:
            self.fps_queue.task_done()
        except ValueError:
            logger.debug("fps queue task_done called without unfinished tasks")

    @staticmethod
    def _normalize_surface_name(line):
        """把 Android 12+ 的 RequestedLayerState 包装还原成 --latency 可用的层名。

        新系统返回：
            RequestedLayerState{833cb9a SurfaceView[...](BLAST)#1041458 parentId=... z=...}
        而 --latency 只认花括号里的
            "833cb9a SurfaceView[...](BLAST)#1041458"
        """
        line = (line or "").strip()
        if line.startswith("RequestedLayerState{") and line.endswith("}"):
            inner = line[len("RequestedLayerState{"):-1]
            for marker in (" parentId=", " relativeParentId=", " z="):
                index = inner.find(marker)
                if index > 0:
                    inner = inner[:index]
            return inner.strip()
        return line

    def _get_surfaceflinger_surfaces(self):
        result = adb_shell(adb,
            cmd="dumpsys SurfaceFlinger --list",
            deviceId=self.device,
            timeout=self.ADB_TIMEOUT,
        )
        surfaces = []
        seen = set()
        for line in result.splitlines():
            name = self._normalize_surface_name(line)
            if name and name not in seen:
                surfaces.append(name)
                seen.add(name)
        return surfaces

    def _surface_tokens(self):
        tokens = [self.package_name]
        if self.focus_window:
            tokens.append(self.focus_window)
            if "/" in self.focus_window:
                tokens.append(self.focus_window.split("/", 1)[1])
                tokens.append(self.focus_window.rsplit(".", 1)[-1])
        return [token for token in tokens if token]

    def _is_renderable_surface(self, surface):
        blocked_markers = (
            "ActivityRecordInputSink",
            "InputSink",
            "ImeInputTarget",
            "StatusBar",
            "NavigationBar",
            "Wallpaper",
            "Dim Layer",
            "Splash Screen",
        )
        return not any(marker in surface for marker in blocked_markers)

    def _surface_rank(self, surface, tokens):
        is_surface_view = "SurfaceView" in surface
        has_render_marker = any(
            marker in surface for marker in ("BLAST", "Cocos", "GLSurfaceView")
        )
        has_package = self.package_name and self.package_name in surface
        has_focus = self.focus_window and self.focus_window in surface
        has_focus_token = any(token in surface for token in tokens[1:])

        if is_surface_view and has_package and has_render_marker:
            return 0
        if is_surface_view and has_package:
            return 1
        if is_surface_view and has_focus_token and has_render_marker:
            return 2
        if is_surface_view and has_focus_token:
            return 3
        if has_focus or has_package:
            return 4
        if has_focus_token:
            return 5
        if is_surface_view and has_render_marker:
            return 6
        if is_surface_view:
            return 7
        return None

    def _surface_candidates(self, force_refresh=False):
        if self._surface_candidate_cache is not None and not force_refresh:
            return list(self._surface_candidate_cache)
        tokens = self._surface_tokens()
        candidates = []
        for surface in self._get_surfaceflinger_surfaces():
            if not self._is_renderable_surface(surface):
                continue
            rank = self._surface_rank(surface, tokens)

            if rank is not None:
                candidates.append((rank, surface))

        if self.focus_window:
            candidates.append((8, self.focus_window))
        if self.package_name:
            candidates.append((9, self.package_name))

        result = []
        seen = set()
        for _, surface in sorted(candidates, key=lambda item: item[0]):
            if surface not in seen:
                result.append(surface)
                seen.add(surface)
        self._surface_candidate_cache = result
        return result

    def _surface_latency_lines(self, surface_name=None):
        if surface_name:
            cmd = "dumpsys SurfaceFlinger --latency {}".format(
                shlex.quote(surface_name)
            )
        else:
            cmd = "dumpsys SurfaceFlinger --latency"
        results = adb_shell(adb, cmd=cmd, deviceId=self.device, timeout=self.ADB_TIMEOUT)
        return results.replace("\r\n", "\n").splitlines()

    def _valid_latency_lines(self, lines):
        if not lines:
            return False
        try:
            return int(lines[0].strip()) > 0
        except (TypeError, ValueError):
            return False

    def _latency_has_frame_data(self, lines):
        pending_fence_timestamp = str((1 << 63) - 1)
        for line in lines[1:]:
            fields = line.split()
            if len(fields) != 3 or not all(field.isdigit() for field in fields):
                continue
            if fields[1] != pending_fence_timestamp and int(fields[1]) > 0:
                return True
        return False

    def _log_surface_diagnostic_once(self, message, candidates):
        if self._surface_diagnostic_logged:
            return
        logger.info(
            "%s, package=%s, focus=%s, candidates=%s",
            message,
            self.package_name,
            self.focus_window,
            candidates,
        )
        self._surface_diagnostic_logged = True

    def _surface_clear_targets(self, surface_names):
        if not surface_names:
            return [None]
        targets = [surface_names[0]]
        if None not in targets:
            targets.append(None)
        return targets

    def _select_surface_latency(self):
        candidates = self._surface_candidates()
        first_valid_surface = None
        first_valid_lines = None
        for surface_name in candidates:
            lines = self._surface_latency_lines(surface_name)
            if self._valid_latency_lines(lines):
                if self._latency_has_frame_data(lines):
                    self.focus_window = surface_name
                    self._surface_diagnostic_logged = False
                    return lines
                if first_valid_lines is None:
                    first_valid_surface = surface_name
                    first_valid_lines = lines
        fallback_lines = self._surface_latency_lines()
        if self._valid_latency_lines(fallback_lines) and self._latency_has_frame_data(
            fallback_lines
        ):
            self._log_surface_diagnostic_once(
                "Using global SurfaceFlinger latency fallback", candidates
            )
            return fallback_lines
        if first_valid_lines is not None:
            self.focus_window = first_valid_surface
            self._log_surface_diagnostic_once(
                "SurfaceFlinger latency surface has no frame data", candidates
            )
            return first_valid_lines
        self._log_surface_diagnostic_once(
            "No valid SurfaceFlinger latency surface found", candidates
        )
        return []

    def get_surfaceview(self):
        candidates = self._surface_candidates()
        return candidates[0] if candidates else ""

    def get_surfaceview_activity(self):
        return self.get_surfaceview()

    def _parse_focus_activity(self, dumpsys_result):
        for line in dumpsys_result.splitlines():
            if not any(
                marker in line
                for marker in (
                    "mCurrentFocus",
                    "mFocusedWindow",
                    "mFocusedApp",
                    "topResumedActivity",
                )
            ):
                continue
            match = re.search(r"([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)", line)
            if match:
                return match.group(1).rstrip("}")
        return ""

    def get_focus_activity(self):
        focus_patterns = "mCurrentFocus|mFocusedWindow|mFocusedApp|topResumedActivity"
        for cmd in (
            "dumpsys window | grep -E '{}'".format(focus_patterns),
            "dumpsys activity activities | grep -E '{}'".format(focus_patterns),
            "dumpsys window windows",
            "dumpsys activity activities",
        ):
            dumpsys_result = adb_shell(adb,
                cmd=cmd, deviceId=self.device, timeout=self.ADB_TIMEOUT
            )
            focus_activity = self._parse_focus_activity(dumpsys_result)
            if focus_activity:
                return focus_activity
        return ""

    def get_foreground_process(self):
        focus_activity = self.get_focus_activity()
        if focus_activity:
            return focus_activity.split("/")[0]
        else:
            return ""

    def _calculate_results(self, refresh_period, timestamps):
        frame_count = len(timestamps)
        if frame_count == 0:
            fps = 0
            jank = 0
        elif frame_count == 1:
            fps = 1
            jank = 0
        else:
            seconds = timestamps[-1][1] - timestamps[0][1]
            if seconds > 0:
                fps = int(round((frame_count - 1) / seconds))
                jank = self._calculate_janky(timestamps)
            else:
                fps = 1
                jank = 0
        return fps, jank

    def _calculate_results_new(self, refresh_period, timestamps):
        frame_count = len(timestamps)
        if frame_count == 0:
            fps = 0
            jank = 0
        elif frame_count == 1:
            fps = 1
            jank = 0
        elif frame_count == 2 or frame_count == 3 or frame_count == 4:
            seconds = timestamps[-1][1] - timestamps[0][1]
            if seconds > 0:
                fps = int(round((frame_count - 1) / seconds))
                jank = self._calculate_janky(timestamps)
            else:
                fps = 1
                jank = 0
        else:
            seconds = timestamps[-1][1] - timestamps[0][1]
            if seconds > 0:
                fps = int(round((frame_count - 1) / seconds))
                jank = self._calculate_jankey_new(timestamps)
            else:
                fps = 1
                jank = 0
        return fps, jank

    def _calculate_jankey_new(self, timestamps):
        twofilmstamp = 83.3 / 1000.0
        tempstamp = 0
        jank = 0
        for index, timestamp in enumerate(timestamps):
            if (index == 0) or (index == 1) or (index == 2) or (index == 3):
                if tempstamp == 0:
                    tempstamp = timestamp[1]
                    continue
                costtime = timestamp[1] - tempstamp
                if costtime > self.jank_threshold:
                    jank = jank + 1
                tempstamp = timestamp[1]
            elif index > 3:
                currentstamp = timestamps[index][1]
                lastonestamp = timestamps[index - 1][1]
                lasttwostamp = timestamps[index - 2][1]
                lastthreestamp = timestamps[index - 3][1]
                lastfourstamp = timestamps[index - 4][1]
                tempframetime = (
                    (
                        (lastthreestamp - lastfourstamp)
                        + (lasttwostamp - lastthreestamp)
                        + (lastonestamp - lasttwostamp)
                    )
                    / 3
                    * 2
                )
                currentframetime = currentstamp - lastonestamp
                if (currentframetime > tempframetime) and (
                    currentframetime > twofilmstamp
                ):
                    jank = jank + 1
        return jank

    def _calculate_janky(self, timestamps):
        tempstamp = 0
        jank = 0
        for timestamp in timestamps:
            if tempstamp == 0:
                tempstamp = timestamp[1]
                continue
            costtime = timestamp[1] - tempstamp
            if costtime > self.jank_threshold:
                jank = jank + 1
            tempstamp = timestamp[1]
        return jank

    def _calculator_thread(self, start_time):
        while True:
            try:
                data = self.data_queue.get()
                if isinstance(data, str) and data == "Stop":
                    break
                before = time.time()
                if self.use_global_latency_fallback:
                    td = data["timestamp"] - self.surface_before["timestamp"]
                    seconds = td.seconds + td.microseconds / 1e6
                    frame_count = (
                        data["page_flip_count"] - self.surface_before["page_flip_count"]
                    )
                    fps = int(round(frame_count / seconds))
                    if fps > 60:
                        fps = 60
                    self.surface_before = data
                    sample_time = data["timestamp"].timestamp()
                    self.fps_monitor.update_sample(
                        fps, self.fps_monitor.collect_jank, sample_time
                    )
                else:
                    refresh_period = data[0]
                    timestamps = data[1]
                    collect_time = data[2]
                    fps, jank = self._calculate_results_new(refresh_period, timestamps)
                    self.fps_monitor.update_sample(fps, jank, collect_time)
                time_consume = time.time() - before
                delta_inter = self.frequency - time_consume
                if delta_inter > 0:
                    time.sleep(delta_inter)
            except:
                logger.error(
                    "an exception hanpend in fps _calculator_thread ,reason unkown!"
                )
                s = traceback.format_exc()
                logger.debug(s)
                self._mark_fps_queue_done()

    def _collector_thread(self):
        is_first = True
        first_collection_delay = min(
            self.frequency, self.FIRST_COLLECTION_DELAY_SECONDS
        )
        if first_collection_delay > 0 and self.stop_event.wait(first_collection_delay):
            self.data_queue.put("Stop")
            return
        while not self.stop_event.is_set():
            try:
                before = time.time()
                if self.use_global_latency_fallback:
                    surface_state = self._get_global_surface_stats()
                    if surface_state:
                        self.data_queue.put(surface_state)
                    self.stop_event.wait(self.frequency)
                else:
                    timestamps = []
                    refresh_period, new_timestamps = (
                        self._get_surfaceflinger_frame_data()
                    )
                    if refresh_period is None or new_timestamps is None:
                        cur_focus_window = self.get_focus_activity()
                        if self.focus_window != cur_focus_window:
                            self._surface_diagnostic_logged = False
                            self._surface_candidate_cache = None
                        self.focus_window = cur_focus_window
                        logger.warning("refresh_period is None or timestamps is None")
                        self.stop_event.wait(self.frequency)
                        continue
                    timestamps += [
                        timestamp
                        for timestamp in new_timestamps
                        if timestamp[1] > self.last_timestamp
                    ]
                    if len(timestamps):
                        first_timestamp = [[0, self.last_timestamp, 0]]
                        if not is_first:
                            timestamps = first_timestamp + timestamps
                        self.last_timestamp = timestamps[-1][1]
                        is_first = False
                    else:
                        is_first = True
                        cur_focus_window = self.get_focus_activity()
                        if self.focus_window != cur_focus_window:
                            self.focus_window = cur_focus_window
                            self._surface_diagnostic_logged = False
                            self._surface_candidate_cache = None
                            continue
                    self.data_queue.put((refresh_period, timestamps, time.time()))
                    time_consume = time.time() - before
                    delta_inter = self.frequency - time_consume
                    if delta_inter > 0:
                        self.stop_event.wait(delta_inter)
            except:
                logger.error(
                    "an exception hanpend in fps _collector_thread , reason unkown!"
                )
                s = traceback.format_exc()
                logger.debug(s)
                self._mark_fps_queue_done()
        self.data_queue.put("Stop")

    def _clear_surfaceflinger_latency_data(self, surface_names=None):
        """清除 SurfaceFlinger 延迟数据。

        Returns:
            如果设备支持 SurfaceFlinger 延迟则返回 True，否则返回 False。
        """
        if surface_names is None:
            surface_names = [self.focus_window] if self.focus_window else [None]
        elif isinstance(surface_names, str):
            surface_names = [surface_names]
        elif not surface_names:
            surface_names = [None]

        success = True
        for surface_name in surface_names:
            if not surface_name:
                cmd = "dumpsys SurfaceFlinger --latency-clear"
            else:
                cmd = "dumpsys SurfaceFlinger --latency-clear {}".format(
                    shlex.quote(surface_name)
                )
            results = adb_shell(adb,
                cmd=cmd, deviceId=self.device, timeout=self.ADB_TIMEOUT
            )
            success = success and len(results) == 0
        return success

    def get_sdk_version(self):
        sdk_version = int(
            adb_shell(adb,
                cmd="getprop ro.build.version.sdk",
                deviceId=self.device,
                timeout=self.ADB_TIMEOUT,
            )
        )
        return sdk_version

    def _get_surfaceflinger_frame_data(self):
        """返回已收集的 SurfaceFlinger 帧时间数据。
        return:(16.6,[[t1,t2,t3],[t4,t5,t6]])
        Returns:
            包含以下内容的元组:
            - 显示器的标称刷新周期（秒）。
            - 帧呈现时间戳列表（秒）。
            如果没有采集到数据，返回值可能为 (None, None)。
        """
        refresh_period = None
        timestamps = []
        nanoseconds_per_second = 1e9
        pending_fence_timestamp = (1 << 63) - 1
        if self.surfaceview is not True:
            results = self._surface_latency_lines(self.focus_window)
            if not self._valid_latency_lines(results):
                return (None, None)
            refresh_period = int(results[0].strip()) / nanoseconds_per_second
            results = adb_shell(adb,
                cmd="dumpsys gfxinfo %s framestats" % self.package_name,
                deviceId=self.device,
                timeout=self.ADB_TIMEOUT,
            )
            results = results.replace("\r\n", "\n").splitlines()
            if len(results) == 0:
                return (None, None)
            isHaveFoundWindow = False
            PROFILEDATA_line = 0
            activity = self.focus_window
            if "#" in self.focus_window:
                activity = activity.split("#")[0]
            for line in results:
                if not isHaveFoundWindow:
                    if "Window" in line and activity in line:
                        isHaveFoundWindow = True
                if not isHaveFoundWindow:
                    continue
                if "PROFILEDATA" in line:
                    PROFILEDATA_line += 1
                fields = []
                fields = line.split(",")
                if fields and "0" == fields[0]:
                    timestamp = [int(fields[1]), int(fields[2]), int(fields[13])]
                    if timestamp[1] == pending_fence_timestamp:
                        continue
                    timestamp = [
                        _timestamp / nanoseconds_per_second for _timestamp in timestamp
                    ]
                    timestamps.append(timestamp)
                if 2 == PROFILEDATA_line:
                    break
        else:
            results = self._select_surface_latency()
            if not results:
                return (None, None)
            if not results[0].strip().isdigit():
                return (None, None)
            try:
                refresh_period = int(results[0].strip()) / nanoseconds_per_second
            except Exception as e:
                logger.exception(e)
                return (None, None)

            for line in results[2:]:
                fields = line.split()
                if len(fields) != 3:
                    continue
                timestamp = [int(fields[0]), int(fields[1]), int(fields[2])]
                if timestamp[1] == pending_fence_timestamp:
                    continue
                timestamp = [
                    _timestamp / nanoseconds_per_second for _timestamp in timestamp
                ]
                timestamps.append(timestamp)
        return (refresh_period, timestamps)

    def _get_global_surface_stats(self):
        """返回全局 SurfaceFlinger page flip 计数和时间戳。

        当目标 Surface latency 数据不可用时，用全局 page flip 计数作为 FPS fallback。

        Returns:
            包含 {page_flip_count（出错时为0）, timestamp} 的字典。
        """
        cur_surface = None
        timestamp = datetime.datetime.now()
        ret = adb_shell(adb,
            cmd="service call SurfaceFlinger 1013",
            deviceId=self.device,
            timeout=self.ADB_TIMEOUT,
        )
        if not ret:
            return None
        match = re.search(r"^Result: Parcel\((\w+)", ret)
        if match:
            cur_surface = int(match.group(1), 16)
            return {"page_flip_count": cur_surface, "timestamp": timestamp}
        return None


class Monitor(object):
    """监视器基类"""

    def __init__(self, **kwargs):
        self.config = kwargs
        self.matched_data = {}

    def start(self):
        logger.warning("请在%s类中实现start方法" % type(self))

    def clear(self):
        self.matched_data = {}

    def stop(self):
        logger.warning("请在%s类中实现stop方法" % type(self))

    def save(self):
        logger.warning("请在%s类中实现save方法" % type(self))


class TimeUtils(object):
    """时间工具类"""
    UnderLineFormatter = "%Y_%m_%d_%H_%M_%S"
    NormalFormatter = "%Y-%m-%d %H-%M-%S"
    ColonFormatter = "%Y-%m-%d %H:%M:%S"

    @staticmethod
    def getCurrentTimeUnderline():
        return time.strftime(TimeUtils.UnderLineFormatter, time.localtime(time.time()))


class FPSMonitor(Monitor):
    """FPS 监视器，collect_fps 和 collect_jank 作为实例属性而非全局变量"""

    def __init__(
        self,
        device_id,
        package_name=None,
        frequency=1.0,
        timeout=24 * 60 * 60,
        fps_queue=None,
        jank_threshold=166,
        use_global_latency_fallback=False,
        surfaceview=True,
        start_time=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.start_time = start_time
        self.use_global_latency_fallback = use_global_latency_fallback
        self.frequency = frequency  # 取样频率
        self.jank_threshold = jank_threshold
        self.device = device_id
        self.timeout = timeout
        self.surfaceview = surfaceview
        self.package = package_name
        # 关键改动：将全局变量改为实例属性
        self._sample_lock = threading.Lock()
        self.collect_fps = 0
        self.collect_jank = 0
        self.collect_sample_time = None
        self.collect_sequence = 0
        self.fpscollector = SurfaceStatsCollector(
            self.device,
            self.frequency,
            package_name,
            fps_queue,
            self.jank_threshold,
            self.surfaceview,
            self,  # 传入自身引用
            self.use_global_latency_fallback,
        )

    def start(self):
        self.fpscollector.start(self.start_time)

    def update_sample(self, fps, jank, sample_time=None):
        if fps <= 0:
            return
        with self._sample_lock:
            self.collect_fps = fps
            self.collect_jank = jank
            self.collect_sample_time = sample_time or time.time()
            self.collect_sequence += 1

    def get_sample(self):
        with self._sample_lock:
            return (
                self.collect_fps,
                self.collect_jank,
                self.collect_sample_time,
                self.collect_sequence,
            )

    def stop(self):
        self.fpscollector.stop()
        return self.collect_fps, self.collect_jank

    def save(self):
        pass

    def parse(self, file_path):
        pass

    def get_fps_collector(self):
        return self.fpscollector


class FPS(ReusableCollector):
    """FPS 采集器"""
    AndroidFPS = None
    _fps_cache = {}
    _object_cache = {}
    _monitor_sessions = {}
    _session_lock = threading.RLock()
    _SESSION_IDLE_SECONDS = 60
    _FIRST_SAMPLE_WAIT_SECONDS = 2.0

    @classmethod
    def getObject(cls, *args, **kwargs):
        obj = FPS(*args, **kwargs)
        if obj.platform == Platform.Android:
            key = obj._cache_key()
            with cls._session_lock:
                cached_obj = cls._object_cache.get(key)
                if cached_obj is None:
                    setattr(obj, "_managed_reusable_collector", True)
                    cls._object_cache[key] = obj
                    cls.AndroidFPS = obj
                    return obj
                setattr(cached_obj, "_managed_reusable_collector", True)
                return cached_obj
        return obj

    @classmethod
    def clear(cls):
        with cls._session_lock:
            sessions = list(cls._monitor_sessions.values())
            objects = list(cls._object_cache.values())
            cls._monitor_sessions.clear()
            cls._object_cache.clear()
            cls._fps_cache.clear()
            cls.AndroidFPS = None
        for obj in objects:
            ReusableCollector.stop(obj)
        for session in sessions:
            cls._stop_monitor(session["monitor"])

    @classmethod
    def _stop_monitor(cls, monitor):
        try:
            monitor.stop()
        except Exception as e:
            logger.warning("[FPS] stop monitor failed: %s", e)

    @classmethod
    def _monitor_running(cls, monitor):
        collector = getattr(monitor, "fpscollector", None)
        collector_thread = getattr(collector, "collector_thread", None)
        calculator_thread = getattr(collector, "calculator_thread", None)
        return bool(
            collector_thread
            and collector_thread.is_alive()
            and calculator_thread
            and calculator_thread.is_alive()
        )

    @classmethod
    def _cleanup_idle_sessions(cls, now):
        stale_sessions = []
        stale_objects = []
        with cls._session_lock:
            for key, session in list(cls._monitor_sessions.items()):
                if now - session["last_access"] > cls._SESSION_IDLE_SECONDS:
                    stale_sessions.append(cls._monitor_sessions.pop(key))
                    obj = cls._object_cache.pop(key, None)
                    if obj is not None:
                        stale_objects.append(obj)
                    cls._fps_cache.pop(key, None)
        for obj in stale_objects:
            ReusableCollector.stop(obj)
        for session in stale_sessions:
            cls._stop_monitor(session["monitor"])

    @classmethod
    def _get_session(cls, key):
        with cls._session_lock:
            return cls._monitor_sessions.get(key)

    @classmethod
    def _set_session(cls, key, monitor, now):
        with cls._session_lock:
            cls._monitor_sessions[key] = {
                "monitor": monitor,
                "last_access": now,
            }

    @classmethod
    def _touch_session(cls, key, now):
        with cls._session_lock:
            session = cls._monitor_sessions.get(key)
            if session is not None:
                session["last_access"] = now

    @classmethod
    def _remove_session(cls, key):
        with cls._session_lock:
            return cls._monitor_sessions.pop(key, None)

    @classmethod
    def stop_session(cls, platform, deviceId, pkgName, surfaceview=True):
        key = cls._make_cache_key(platform, deviceId, pkgName, surfaceview)
        session = cls._remove_session(key)
        with cls._session_lock:
            obj = cls._object_cache.pop(key, None)
            cls._fps_cache.pop(key, None)
        if obj is not None:
            ReusableCollector.stop(obj)
        if session is not None:
            cls._stop_monitor(session["monitor"])
        cls.AndroidFPS = None

    def __init__(self, pkgName, deviceId, platform=Platform.Android, surfaceview=True):
        self.pkgName = pkgName
        self.deviceId = deviceId
        self.platform = platform
        self.surfaceview = surfaceview
        self.apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
        self.monitors = None
        self._last_fps = self._fps_cache.get(self._cache_key())
        self.last_sample_time = None
        self._last_sample_sequence = None
        self._last_logged_sample_time = None
        self.last_sample = None

    def _get_devices(self):
        return create_devices()

    def _get_report_store(self):
        return create_report_store()

    @classmethod
    def _make_cache_key(cls, platform, deviceId, pkgName, surfaceview):
        return platform, deviceId, pkgName, surfaceview

    def _cache_key(self):
        return self._make_cache_key(
            self.platform, self.deviceId, self.pkgName, self.surfaceview
        )

    def _write_fps_log(self, file_obj, fps, jank):
        apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
        file_obj.add_log(os.path.join(file_obj.report_dir, "fps.log"), apm_time, fps)
        file_obj.add_log(os.path.join(file_obj.report_dir, "jank.log"), apm_time, jank)

    def _process_exists(self, devices):
        try:
            return len(devices.getPid(self.deviceId, self.pkgName)) > 0
        except Exception:
            return False

    def _new_monitor(self):
        return FPSMonitor(
            device_id=self.deviceId,
            package_name=self.pkgName,
            frequency=1,
            surfaceview=self.surfaceview,
            start_time=TimeUtils.getCurrentTimeUnderline(),
        )

    def _start_or_get_monitor(self):
        key = self._cache_key()
        now = time.time()
        self._cleanup_idle_sessions(now)
        session = self._get_session(key)
        if session is not None and self._monitor_running(session["monitor"]):
            self._touch_session(key, now)
            return session["monitor"], False

        if session is not None:
            self._remove_session(key)
            self._stop_monitor(session["monitor"])

        monitor = self._new_monitor()
        monitor.start()
        self._set_session(key, monitor, now)
        return monitor, True

    def _read_monitor_value(self, monitor, wait_for_first_sample=False):
        deadline = time.time() + self._FIRST_SAMPLE_WAIT_SECONDS
        while True:
            if hasattr(monitor, "get_sample"):
                fps, jank, sample_time, sequence = monitor.get_sample()
            else:
                fps = monitor.collect_fps
                jank = monitor.collect_jank
                sample_time = getattr(monitor, "collect_sample_time", None)
                sequence = getattr(monitor, "collect_sequence", None)
            if fps > 0:
                return fps, jank, sample_time, sequence
            if not wait_for_first_sample or time.time() >= deadline:
                return fps, jank, sample_time, sequence
            time.sleep(0.1)

    def _sample_android_fps(self):
        monitor, started = self._start_or_get_monitor()
        fps, jank, sample_time, sequence = self._read_monitor_value(
            monitor, wait_for_first_sample=started and self._last_fps is None
        )
        used_cache = False
        if fps <= 0 and self._last_fps is not None:
            fps, jank = self._last_fps
            used_cache = True
        elif fps > 0:
            self._last_fps = (fps, jank)
            self._fps_cache[self._cache_key()] = self._last_fps
            if sample_time:
                self.last_sample_time = sample_time
            if sequence is not None:
                self._last_sample_sequence = sequence
        if fps <= 0:
            raise CollectorUnavailableError("no valid FPS sample")
        result = {
            "fps": fps,
            "jank": jank,
            "sample_time": self.last_sample_time,
            "sequence": self._last_sample_sequence,
        }
        return CachedSample(result) if used_cache else result

    def getAndroidFps(self, noLog=False):
        """获取 Android FPS，单位：HZ"""
        _f = self._get_report_store()
        _d = self._get_devices()
        try:
            result = self._sampled_value(
                "fps",
                self._sample_android_fps,
                interval=1.0,
                stale_ttl=self.SAMPLER_STALE_TTL_SECONDS,
                background=True,
            )
            if isinstance(result, MetricSample):
                self.last_sample = result.to_dict()
                if result.status in (MetricStatus.OK, MetricStatus.CACHED):
                    fps = result.value
                    jank = None
                    self.last_sample_time = (

                        result.sample_epoch_ms / 1000.0
                        if result.sample_epoch_ms is not None
                        else self.last_sample_time
                    )
                    if (
                        noLog is False
                        and self.last_sample_time != self._last_logged_sample_time
                    ):
                        self._write_fps_log(_f, fps, jank)
                        self._last_logged_sample_time = self.last_sample_time
                return result
            fps = result["fps"]
            jank = result["jank"]
            self.last_sample_time = result.get("sample_time")
            self._last_sample_sequence = result.get("sequence")
            self.last_sample = {"fps": fps, "jank": jank, "status": MetricStatus.OK.value,
                                "unit": "fps", "sample_epoch_ms": int((self.last_sample_time or time.time()) * 1000)}
            if (
                noLog is False
                and (fps > 0 or self._last_fps is not None)
                and self.last_sample_time != self._last_logged_sample_time
            ):
                self._write_fps_log(_f, fps, jank)
                self._last_logged_sample_time = self.last_sample_time
        except Exception as e:
            used_cache = self._last_fps is not None
            if self._last_fps is not None:
                fps, jank = self._last_fps
            else:
                raise
            if not self._process_exists(_d):
                logger.error("[FPS] {} : No process found".format(self.pkgName))
            elif used_cache:
                logger.warning("[FPS] %s, using cached value", e)
            else:
                logger.exception(e)
            if (
                noLog is False
                and self._last_fps is not None
                and self.last_sample_time != self._last_logged_sample_time
            ):
                self._write_fps_log(_f, fps, jank)
                self._last_logged_sample_time = self.last_sample_time
        return fps, jank

    def stop(self):
        ReusableCollector.stop(self)
        session = self._remove_session(self._cache_key())
        if session is not None:
            self._stop_monitor(session["monitor"])

    def getiOSFps(self, noLog=False):
        """获取 iOS FPS"""
        _f = self._get_report_store()
        from perfcattle.collector.ios_perf import IOSPerformance
        apm = IOSPerformance(self.pkgName, self.deviceId)
        fps = int(apm.getPerformance(apm.fps))
        if noLog is False:
            apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
            _f.add_log(os.path.join(_f.report_dir, "fps.log"), apm_time, fps)
        return fps, 0

    def getFPS(self, noLog=False):
        """获取 fps 和 jank；统一模式返回 MetricSample。"""
        if self.platform == Platform.Android:
            result = self.getAndroidFps(noLog)
            if isinstance(result, MetricSample):
                return result
            fps, jank = result
        else:
            fps, jank = self.getiOSFps(noLog)
        return fps, jank
