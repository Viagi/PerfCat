# -*- encoding: utf-8 -*-
"""报告日志读写模块

负责性能日志文件的读取、写入、解析等操作。
"""
import datetime
import json
import os
import time
from logzero import logger
from perfcattle.core.metrics import MetricQuality, finite_number
from perfcattle.core.platform import Platform


class LogIO:
    """日志文件读写操作"""

    # 采集开始时写入的会话标记，报告里所有图表共用它的时钟作为 x=0。
    SESSION_FILE = "apm_session.json"
    MEMORY_DETAIL_LOGS = (
        ("java_heap", "mem_java_heap.log"),
        ("native_heap", "mem_native_heap.log"),
        ("code_pss", "mem_code_pss.log"),
        ("stack_pss", "mem_stack_pss.log"),
        ("graphics_pss", "mem_graphics_pss.log"),
        ("private_pss", "mem_private_pss.log"),
        ("system_pss", "mem_system_pss.log"),
    )
    THERMAL_LOGS = (
        ("CPU", "thermal_cpu.log"),
        ("GPU", "thermal_gpu.log"),
        ("Memory", "thermal_memory.log"),
        ("Battery", "thermal_battery.log"),
    )
    THERMAL_SNAPSHOT_FILES = ("init_thermal_temp.json", "current_thermal_temp.json")
    # Unity 几何计数在报告/图表中统一换算为 k（千），保留 1 位小数。
    GEOMETRY_KEYS = ("triangles", "vertices")
    GEOMETRY_SCALE = 0.001

    def __init__(self, fileroot="."):
        self.fileroot = fileroot
        self.report_dir = self._get_report_dir()
        self._bounds_cache = {}

    def _get_report_dir(self):
        """获取报告目录路径"""
        report_dir = os.path.join(os.getcwd(), "report")
        if not os.path.exists(report_dir):
            os.mkdir(report_dir)
        return report_dir

    def open_file(self, path, mode):
        """以生成器方式逐行读取文件"""
        with open(path, mode) as f:
            for line in f:
                yield line

    def add_log(self, path, log_time, value):
        """添加一条日志记录到指定文件

        Args:
            path: 日志文件路径
            log_time: 时间戳字符串
            value: 记录值（非负数才写入）
        """
        if value >= 0:
            with open(path, "a+", encoding="utf-8") as file:
                file.write(f"{log_time}={str(value)}" + "\n")

    def add_json_log(self, path, log_time, value):
        """追加一条多字段指标（JSON 行），用于 GPU 扩展指标等。"""
        if not value:
            return
        with open(path, "a+", encoding="utf-8") as file:
            file.write(f"{log_time}={json.dumps(value)}" + "\n")

    # ------------------------------------------------------ time origin

    def begin_session(self, clock=None):
        """记录本次采集的起点，供报告里所有图表统一时间轴。

        ``clock`` 使用与各采集日志相同的 ``HH:MM:SS.ffffff`` 格式。
        """
        now = datetime.datetime.now()
        payload = {
            "clock": clock or now.strftime("%H:%M:%S.%f"),
            "epoch": time.time(),
            "date": now.strftime("%Y-%m-%d"),
        }
        path = os.path.join(self.report_dir, self.SESSION_FILE)
        try:
            with open(path, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False)
        except OSError as exc:
            logger.warning("write session marker failed: %s", exc)
            return False
        self._bounds_cache.clear()
        return True

    @staticmethod
    def _relative_seconds(seconds, origin):
        """把当日秒数换算成相对 origin 的秒数，并处理跨零点。"""
        delta = seconds - origin
        if delta < -43200:
            delta += 86400
        elif delta > 43200:
            delta -= 86400
        return delta

    def _session_epoch(self, scene):
        if not scene:
            return None
        path = os.path.join(self.report_dir, scene, self.SESSION_FILE)
        try:
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            if data.get("epoch") is not None:
                epoch = float(data["epoch"])
                return epoch if finite_number(epoch) else None
            if data.get("date") and data.get("clock"):
                date = datetime.datetime.strptime(data["date"], "%Y-%m-%d").date()
                clock = self._clock_seconds(data["clock"])
                return datetime.datetime.combine(
                    date, datetime.time(),
                ).timestamp() + clock
        except (TypeError, ValueError, OverflowError, OSError):
            return None
        return None

    def _log_epoch(self, scene, current_seconds, origin):
        session_epoch = self._session_epoch(scene)
        if session_epoch is not None and origin is not None:
            delta = self._relative_seconds(current_seconds, origin)
            return int(round((session_epoch + delta) * 1000))
        return int(round(current_seconds * 1000))

    def _session_origin(self, scene):
        if not scene:
            return None
        path = os.path.join(self.report_dir, scene, self.SESSION_FILE)
        try:
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not data.get("clock"):
            return None
        try:
            return self._clock_seconds(data["clock"])
        except (TypeError, ValueError):
            return None

    def _log_timestamps(self, scene):
        """按写入顺序遍历场景内所有 .log 文件的时间戳。"""
        if not scene:
            return
        folder = os.path.join(self.report_dir, scene)
        if not os.path.isdir(folder):
            return
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".log"):
                continue
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            try:
                for line in self.open_file(path, "r"):
                    text, sep, _ = line.partition("=")
                    if not sep:
                        continue
                    try:
                        yield self._clock_seconds(text)
                    except (TypeError, ValueError):
                        continue
            except OSError:
                continue

    def scene_bounds(self, scene):
        """返回 (公共时间原点, 所有日志相对原点的最大秒数)。

        优先使用 ``apm_session.json`` 记录的采集起点；旧报告没有该文件时
        退回场景内最早的一条日志时间。所有图表共用同一个原点，Unity 与
        系统指标才能按真实时间对齐。
        """
        if scene in self._bounds_cache:
            return self._bounds_cache[scene]
        origin = self._session_origin(scene)
        timestamps = list(self._log_timestamps(scene))
        if origin is None:
            if not timestamps:
                return (None, None)
            origin = min(timestamps)
        max_relative = (
            max(self._relative_seconds(value, origin) for value in timestamps)
            if timestamps
            else 0.0
        )
        bounds = (origin, max(0.0, max_relative))
        self._bounds_cache[scene] = bounds
        return bounds

    def scene_origin(self, scene):
        """场景的公共时间原点；没有日志时返回 None。"""
        return self.scene_bounds(scene)[0]

    def scene_time_range(self, scene):
        """所有图表共用的 x 轴范围（秒），保证并排图表视觉上对齐。"""
        origin, max_relative = self.scene_bounds(scene)
        if origin is None or max_relative is None:
            return {"min": 0.0, "max": 0.0}
        return {"min": 0.0, "max": round(max_relative, 1)}

    @staticmethod
    def _clock_seconds(text):
        """把 HH:MM:SS[.ffffff] 转为秒数。"""
        text = (text or "").strip()
        if "." in text:
            hms, micro = text.split(".", 1)
            microsec = int(micro[:6].ljust(6, "0"))
        else:
            hms, microsec = text, 0
        hour, minute, second = hms.split(":")
        return int(hour) * 3600 + int(minute) * 60 + int(second) + microsec / 1000000.0

    def _json_points(self, scene, filename):
        """读取 JSON 行日志，返回 [(绝对秒数, dict), ...]。"""
        points = []
        filepath = os.path.join(self.report_dir, scene, filename)
        if not os.path.exists(filepath):
            return points
        for line in self.open_file(filepath, "r"):
            time_str, sep, payload = line.partition("=")
            if not sep:
                continue
            try:
                data = json.loads(payload.strip())
                seconds = self._clock_seconds(time_str)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict):
                points.append((seconds, data))
        return points

    @staticmethod
    def _structured_value(value):
        """Unwrap ``{"value": .., "status": ..}`` metric samples.

        Newer collectors (GPU stats, disk snapshots) write structured samples
        instead of bare numbers. Only ok/cached samples carry a plottable
        value; anything else is a gap in the series, never a fabricated zero.
        """
        if not isinstance(value, dict):
            return value
        status = value.get("status")
        if status is not None and status not in ("ok", "cached"):
            return None
        return value.get("value")

    def read_json_lines(self, scene, filename):
        """读取 JSON 行日志，返回 (charts, values)。

        charts 的 x 与普通日志一致，使用场景的公共时间原点；额外的 t 字段
        保存绝对秒数，仅用于与其他日志对齐（前端会忽略）。
        """
        points = self._json_points(scene, filename)
        charts = {}
        values = {}
        if not points:
            return charts, values
        origin = self.scene_origin(scene)
        if origin is None:
            origin = points[0][0]
        for seconds, data in points:
            x = round(max(0.0, self._relative_seconds(seconds, origin)), 1)
            for key, raw_value in data.items():
                value = self._structured_value(raw_value)
                if value is None or isinstance(value, (dict, list, bool)):
                    continue
                charts.setdefault(key, []).append(
                    {"x": x, "y": value, "t": seconds}
                )
                values.setdefault(key, []).append(value)
        return charts, values

    def _absolute_points(self, scene, filename):
        """读取普通日志，返回 [(绝对秒数, 数值), ...]。"""
        points, _ = self._absolute_points_with_quality(scene, filename)
        return points

    def _absolute_points_with_quality(self, scene, filename):
        """读取普通日志并返回点集与结构化质量统计。

        Returned point timestamps remain clock seconds for legacy correlation;
        MetricQuality epoch fields use the scene session epoch when available.
        """
        points = []
        quality = MetricQuality()
        filepath = os.path.join(self.report_dir, scene, filename)
        if not os.path.exists(filepath):
            return points, quality
        session_origin = self.scene_origin(scene)
        for line in self.open_file(filepath, "r"):
            quality.total += 1
            parts = line.split("=", 1)
            if len(parts) < 2:
                quality.invalid += 1
                continue
            try:
                seconds = self._clock_seconds(parts[0])
                value = float(parts[1].strip())
                if not finite_number(value):
                    raise ValueError("non-finite value")
                if value.is_integer() and not any(c in parts[1] for c in ".eE"):
                    value = int(value)
            except (ValueError, TypeError, IndexError, OverflowError):
                quality.invalid += 1
                continue
            points.append((seconds, value))
            quality.valid += 1
            epoch_ms = self._log_epoch(scene, seconds, session_origin)
            quality.first_epoch_ms = epoch_ms if quality.first_epoch_ms is None else min(quality.first_epoch_ms, epoch_ms)
            quality.last_epoch_ms = epoch_ms if quality.last_epoch_ms is None else max(quality.last_epoch_ms, epoch_ms)
        return points, quality

    @staticmethod
    def _clock_distance(first, second):
        """两个当日秒数的最短距离，跨零点时也正确。"""
        return abs((first - second + 43200) % 86400 - 43200)

    @classmethod
    def _nearest_absolute(cls, points, target, max_distance=None):
        if not points:
            return None
        best = min(points, key=lambda item: cls._clock_distance(item[0], target))
        if max_distance is not None and cls._clock_distance(best[0], target) > max_distance:
            return None
        return best[1]

    def getGpuStatsLog(self, platform, scene):
        """GPU 扩展指标；带宽按同一时刻的 FPS 换算成 MB/帧。"""
        charts, values = self.read_json_lines(scene, "gpu_stats.log")
        avg = {
            key: round(sum(items) / len(items), 2)
            for key, items in values.items()
            if items
        }
        fps_points = self._absolute_points(scene, "fps.log")
        if fps_points:
            for source, target in (
                ("gpu_bw_read", "gpu_bw_read_frame"),
                ("gpu_bw_write", "gpu_bw_write_frame"),
            ):
                if source not in charts:
                    continue
                per_frame = []
                for point in charts[source]:
                    fps = self._nearest_absolute(fps_points, point["t"], max_distance=2.0)
                    if fps and fps > 0:
                        per_frame.append(
                            {"x": point["x"], "y": round(point["y"] / fps, 3)}
                        )
                    else:
                        per_frame.append({"x": point["x"], "y": None})
                charts[target] = per_frame
                numbers = [p["y"] for p in per_frame if p["y"] is not None]
                if numbers:
                    avg[target] = round(sum(numbers) / len(numbers), 3)
        for key in list(charts):
            charts[key] = [{"x": p["x"], "y": p["y"]} for p in charts[key]]
        return {"status": 1, "avg": avg, "charts": charts}

    def getUnityStatsLog(self, platform, scene):
        """Unity 侧实时计数器（帧耗时 / Draw Calls / 三角面 / 顶点数等）。"""
        charts, values = self.read_json_lines(scene, "unity_stats.log")
        # frame 只用于“指标曲线 -> 热点帧号”的映射，不参与平均值
        values.pop("frame", None)
        avg = {
            key: round(sum(items) / len(items), 2)
            for key, items in values.items()
            if items
        }
        for key in self.GEOMETRY_KEYS:
            if key in avg:
                avg[key] = round(avg[key] * self.GEOMETRY_SCALE, 1)
        for key in list(charts):
            if key in self.GEOMETRY_KEYS:
                charts[key] = [
                    {
                        "x": p["x"],
                        "y": round(p["y"] * self.GEOMETRY_SCALE, 1),
                    }
                    for p in charts[key]
                ]
            else:
                charts[key] = [{"x": p["x"], "y": p["y"]} for p in charts[key]]
        return {"status": 1, "avg": avg, "charts": charts}

    def getUnityStatsLogCompare(self, platform, scene1, scene2):
        """Unity 侧实时计数器的双场景对比数据。"""
        return self._status(
            scene1=self.getUnityStatsLog(platform, scene1),
            scene2=self.getUnityStatsLog(platform, scene2),
        )

    def getUnityCorrelationLog(self, platform, scene):
        """把系统 CPU 与 Unity 计数器放到同一个时间轴上。

        统一使用场景的公共时间原点（采集开始时刻），与其它图表完全一致，
        Unity 第一帧不会再和系统时间轴错开。
        """
        cpu_app = self._absolute_points(scene, "cpu_app.log")
        cpu_sys = self._absolute_points(scene, "cpu_sys.log")
        unity_points = self._json_points(scene, "unity_stats.log")
        origin = self.scene_origin(scene)
        if origin is None:
            origins = []
            for points in (cpu_app, cpu_sys):
                if points:
                    origins.append(points[0][0])
            if unity_points:
                origins.append(unity_points[0][0])
            if not origins:
                return {"status": 1, "charts": {}}
            origin = min(origins)

        def convert(points, scale=1.0):
            return [
                {
                    "x": round(
                        max(0.0, self._relative_seconds(seconds, origin)), 1
                    ),
                    "y": value if scale == 1.0 else round(value * scale, 1),
                }
                for seconds, value in points
            ]

        charts = {
            "cpu_app": convert(cpu_app),
            "cpu_sys": convert(cpu_sys),
        }
        for key in (
            "cpu_frame_ms",
            "gpu_frame_ms",
            "draw_calls",
            "triangles",
            "vertices",
            "batches",
            "setpass_calls",
        ):
            points = [
                (seconds, data.get(key))
                for seconds, data in unity_points
                if data.get(key) is not None
            ]
            scale = self.GEOMETRY_SCALE if key in self.GEOMETRY_KEYS else 1.0
            charts[key] = convert(points, scale)
        return {"status": 1, "charts": charts}

    def create_file(self, filename, content=""):
        """在报告目录下创建文件

        Args:
            filename: 文件名
            content: 写入内容
        """
        if not os.path.exists(self.report_dir):
            os.mkdir(self.report_dir)
        with open(
            os.path.join(self.report_dir, filename), "a+", encoding="utf-8"
        ) as file:
            file.write(content)

    def record_net(self, type, send, recv):
        """记录网络数据（前置/后置快照）

        Args:
            type: "pre" 或 "end"
            send: 发送字节数
            recv: 接收字节数
        """
        net_dict = dict()
        match type:
            case "pre":
                net_dict["send"] = send
                net_dict["recv"] = recv
                content = json.dumps(net_dict)
                self.create_file(filename="pre_net.json", content=content)
            case "end":
                net_dict["send"] = send
                net_dict["recv"] = recv
                content = json.dumps(net_dict)
                self.create_file(filename="end_net.json", content=content)
            case _:
                logger.error("record network data failed")

    def readLogWithQuality(self, scene, filename, origin=None):
        """读取日志点集、数值和 MetricQuality。"""
        return self._read_log_with_quality(scene, filename, origin=origin)

    def _read_log_with_quality(self, scene, filename, origin=None):
        log_data_list = []
        target_data_list = []
        quality = MetricQuality()
        if origin is None:
            origin = self.scene_origin(scene)

        filepath = os.path.join(self.report_dir, scene, filename)
        if not os.path.exists(filepath):
            quality.missing = 1
            return log_data_list, target_data_list, quality

        for line in self.open_file(filepath, "r"):
            quality.total += 1
            parts = line.split("=", 1)
            if len(parts) < 2:
                quality.invalid += 1
                continue

            time_str = parts[0].strip()
            value_str = parts[1].strip()
            try:
                current_seconds = self._clock_seconds(time_str)
                value = float(value_str)
                if not finite_number(value):
                    raise ValueError("non-finite value")
                if value.is_integer() and not any(c in value_str for c in ".eE"):
                    value = int(value)
            except (ValueError, TypeError, IndexError, OverflowError):
                quality.invalid += 1
                continue

            if origin is None:
                origin = current_seconds
            relative_time = round(
                max(0.0, self._relative_seconds(current_seconds, origin)), 1
            )
            epoch_ms = self._log_epoch(scene, current_seconds, origin)
            quality.first_epoch_ms = epoch_ms if quality.first_epoch_ms is None else min(quality.first_epoch_ms, epoch_ms)
            quality.last_epoch_ms = epoch_ms if quality.last_epoch_ms is None else max(quality.last_epoch_ms, epoch_ms)
            quality.valid += 1
            log_data_list.append({"x": relative_time, "y": value})
            target_data_list.append(value)

        return log_data_list, target_data_list, quality

    def readLog(self, scene, filename, origin=None):
        """读取 APM 日志文件数据，x 轴为相对采集起点的秒数。

        不传 ``origin`` 时使用场景的公共时间原点，因此所有图表（包括
        Unity 指标）共享同一条时间轴。

        Args:
            scene: 场景目录名
            filename: 日志文件名
            origin: 可选的公共时间原点（当日秒数）

        Returns:
            tuple: (log_data_list, target_data_list)
                - log_data_list: [{"x": 相对时间, "y": 值}, ...]
                - target_data_list: [值1, 值2, ...]
        """
        log_data_list, target_data_list, _ = self._read_log_with_quality(
            scene, filename, origin=origin
        )
        return log_data_list, target_data_list

    def _points(self, scene, filename):
        """读取日志点位数据"""
        return self.readLog(scene=scene, filename=filename)[0]

    def _status(self, **payload):
        """统一构造接口返回值"""
        return {"status": 1, **payload}

    def _compare_points(self, scene1, scene2, filename):
        """读取两个场景的同一日志点位"""
        return self._points(scene1, filename), self._points(scene2, filename)

    def _point_map(self, scene, specs):
        """按字段名和日志文件映射读取点位数据"""
        return {key: self._points(scene, filename) for key, filename in specs}

    @staticmethod
    def _thermal_celsius(raw_temp):
        try:
            value = float(str(raw_temp).strip())
        except (TypeError, ValueError):
            return None
        return round(value / 1000 if abs(value) > 1000 else value, 2)

    def _thermal_snapshot_points(self, scene):
        """兼容旧报告的采集前/采集后温度快照。"""
        thermal = {key: [] for key, _ in self.THERMAL_LOGS}
        for index, filename in enumerate(self.THERMAL_SNAPSHOT_FILES):
            filepath = os.path.join(self.report_dir, scene, filename)
            if not os.path.exists(filepath):
                continue
            try:
                with open(filepath, "r", encoding="utf-8") as file:
                    sensors = json.loads(file.read() or "[]")
            except Exception as exc:
                logger.warning("read thermal snapshot failed: %s: %s", filepath, exc)
                continue
            for sensor in sensors or []:
                group = sensor.get("name") or sensor.get("type")
                if group not in thermal:
                    continue
                value = self._thermal_celsius(sensor.get("temp"))
                if value is not None:
                    thermal[group].append({"x": index, "y": value})
        return thermal

    def _thermal_points(self, scene):
        thermal = self._point_map(scene, self.THERMAL_LOGS)
        if any(points for points in thermal.values()):
            return thermal
        return self._thermal_snapshot_points(scene)

    def getLogCompare(self, scene1, scene2, filename):
        """获取任意单日志文件的双场景对比数据"""
        scene1_data, scene2_data = self._compare_points(scene1, scene2, filename)
        return self._status(scene1=scene1_data, scene2=scene2_data)

    def getCpuLog(self, platform, scene):
        """获取 CPU 日志数据"""
        return self._status(
            cpuAppData=self._points(scene, "cpu_app.log"),
            cpuSysData=self._points(scene, "cpu_sys.log"),
        )

    def getCpuLogCompare(self, platform, scene1, scene2):
        """获取 CPU 对比日志数据"""
        cpu_app_scene1, cpu_app_scene2 = self._compare_points(
            scene1, scene2, "cpu_app.log"
        )
        cpu_sys_scene1, cpu_sys_scene2 = self._compare_points(
            scene1, scene2, "cpu_sys.log"
        )
        return self._status(
            scene1=cpu_app_scene1,
            scene2=cpu_app_scene2,
            cpuAppScene1=cpu_app_scene1,
            cpuAppScene2=cpu_app_scene2,
            cpuSysScene1=cpu_sys_scene1,
            cpuSysScene2=cpu_sys_scene2,
        )

    def getGpuLog(self, platform, scene):
        """获取 GPU 日志数据"""
        return self._status(gpu=self._points(scene, "gpu.log"))

    def getGpuLogCompare(self, platform, scene1, scene2):
        """获取 GPU 对比日志数据"""
        scene1_data, scene2_data = self._compare_points(scene1, scene2, "gpu.log")
        return self._status(scene1=scene1_data, scene2=scene2_data)

    def getMemLog(self, platform, scene):
        """获取内存日志数据"""
        mem_total_data = self._points(scene, "mem_total.log")
        if platform == Platform.Android:
            return self._status(
                memTotalData=mem_total_data,
                memSwapData=self._points(scene, "mem_swap.log"),
            )
        return self._status(memTotalData=mem_total_data)

    def getMemLogCompare(self, platform, scene1, scene2):
        """获取内存对比日志数据"""
        mem_total_scene1, mem_total_scene2 = self._compare_points(
            scene1, scene2, "mem_total.log"
        )
        result = self._status(
            scene1=mem_total_scene1,
            scene2=mem_total_scene2,
            memTotalScene1=mem_total_scene1,
            memTotalScene2=mem_total_scene2,
        )
        if platform == Platform.Android:
            result["memSwapScene1"], result["memSwapScene2"] = self._compare_points(
                scene1, scene2, "mem_swap.log"
            )
        return result

    def getMemDetailLog(self, platform, scene):
        """获取内存详情日志数据"""
        return self._status(
            memory_detail=self._point_map(scene, self.MEMORY_DETAIL_LOGS)
        )

    def getMemDetailLogCompare(self, platform, scene1, scene2):
        """获取内存详情对比日志数据"""
        scene1_detail = self.getMemDetailLog(platform, scene1)["memory_detail"]
        scene2_detail = self.getMemDetailLog(platform, scene2)["memory_detail"]
        return self._status(scene1=scene1_detail, scene2=scene2_detail)

    def getThermalLog(self, platform, scene):
        """获取温度传感器日志数据"""
        return self._status(thermal=self._thermal_points(scene))

    def getThermalLogCompare(self, platform, scene1, scene2):
        """获取温度传感器对比日志数据"""
        return self._status(
            scene1=self._thermal_points(scene1),
            scene2=self._thermal_points(scene2),
        )

    def getCpuCoreLog(self, platform, scene):
        """获取 CPU 核心日志数据"""
        cpu_core = dict()
        cores = self.read_result(scene=scene).get("cores", 0)
        if int(cores) > 0:
            for i in range(int(cores)):
                cpu_core[f"cpu{i}"] = self._points(scene, f"cpu{i}.log")
        return self._status(cores=cores, cpu_core=cpu_core)

    def getCpuCoreLogCompare(self, platform, scene1, scene2):
        """获取 CPU 核心对比日志数据"""
        scene1_core = self.getCpuCoreLog(platform, scene1)
        scene2_core = self.getCpuCoreLog(platform, scene2)
        scene1_cores = int(scene1_core.get("cores", 0) or 0)
        scene2_cores = int(scene2_core.get("cores", 0) or 0)
        return self._status(
            scene1=scene1_core["cpu_core"],
            scene2=scene2_core["cpu_core"],
            scene1Cores=scene1_cores,
            scene2Cores=scene2_cores,
            cores=max(scene1_cores, scene2_cores),
        )

    def getBatteryLog(self, platform, scene):
        """获取电池日志数据"""
        if platform == Platform.Android:
            return self._status(
                batteryLevel=self._points(scene, "battery_level.log"),
                batteryTem=self._points(scene, "battery_tem.log"),
            )
        return self._status(
            batteryTem=self._points(scene, "battery_tem.log"),
            batteryCurrent=self._points(scene, "battery_current.log"),
            batteryVoltage=self._points(scene, "battery_voltage.log"),
            batteryPower=self._points(scene, "battery_power.log"),
        )

    def getBatteryLogCompare(self, platform, scene1, scene2):
        """获取电池对比日志数据"""
        if platform == Platform.Android:
            battery_level_scene1, battery_level_scene2 = self._compare_points(
                scene1, scene2, "battery_level.log"
            )
            battery_temp_scene1, battery_temp_scene2 = self._compare_points(
                scene1, scene2, "battery_tem.log"
            )
            return self._status(
                scene1=battery_level_scene1,
                scene2=battery_level_scene2,
                batteryLevelScene1=battery_level_scene1,
                batteryLevelScene2=battery_level_scene2,
                batteryTemScene1=battery_temp_scene1,
                batteryTemScene2=battery_temp_scene2,
            )

        battery_power_scene1, battery_power_scene2 = self._compare_points(
            scene1, scene2, "battery_power.log"
        )
        battery_tem_scene1, battery_tem_scene2 = self._compare_points(
            scene1, scene2, "battery_tem.log"
        )
        battery_current_scene1, battery_current_scene2 = self._compare_points(
            scene1, scene2, "battery_current.log"
        )
        battery_voltage_scene1, battery_voltage_scene2 = self._compare_points(
            scene1, scene2, "battery_voltage.log"
        )
        return self._status(
            scene1=battery_power_scene1,
            scene2=battery_power_scene2,
            batteryTemScene1=battery_tem_scene1,
            batteryTemScene2=battery_tem_scene2,
            batteryCurrentScene1=battery_current_scene1,
            batteryCurrentScene2=battery_current_scene2,
            batteryVoltageScene1=battery_voltage_scene1,
            batteryVoltageScene2=battery_voltage_scene2,
            batteryPowerScene1=battery_power_scene1,
            batteryPowerScene2=battery_power_scene2,
        )

    def getFlowLog(self, platform, scene):
        """获取流量日志数据"""
        return self._status(
            upFlow=self._points(scene, "upflow.log"),
            downFlow=self._points(scene, "downflow.log"),
        )

    def getFileIOLog(self, platform, scene):
        """获取文件读写日志数据"""
        return self._status(
            read=self._points(scene, "file_read.log"),
            write=self._points(scene, "file_write.log"),
        )

    def getFileIOLogCompare(self, platform, scene1, scene2):
        """获取文件读写对比日志数据"""
        read_scene1, read_scene2 = self._compare_points(
            scene1, scene2, "file_read.log"
        )
        write_scene1, write_scene2 = self._compare_points(
            scene1, scene2, "file_write.log"
        )
        return self._status(
            scene1=read_scene1,
            scene2=read_scene2,
            readScene1=read_scene1,
            readScene2=read_scene2,
            writeScene1=write_scene1,
            writeScene2=write_scene2,
        )

    def getFlowSendLogCompare(self, platform, scene1, scene2):
        """获取上行流量对比日志数据"""
        scene1_data, scene2_data = self._compare_points(scene1, scene2, "upflow.log")
        return self._status(scene1=scene1_data, scene2=scene2_data)

    def getFlowRecvLogCompare(self, platform, scene1, scene2):
        """获取下行流量对比日志数据"""
        scene1_data, scene2_data = self._compare_points(
            scene1, scene2, "downflow.log"
        )
        return self._status(scene1=scene1_data, scene2=scene2_data)

    def getFpsLog(self, platform, scene):
        """获取 FPS 日志数据"""
        fps = self._points(scene, "fps.log")
        if platform == Platform.Android:
            return self._status(fps=fps, jank=self._points(scene, "jank.log"))
        return self._status(fps=fps)

    def getFpsLogCompare(self, platform, scene1, scene2):
        """获取 FPS 对比日志数据"""
        fps_scene1, fps_scene2 = self._compare_points(scene1, scene2, "fps.log")
        result = self._status(
            scene1=fps_scene1,
            scene2=fps_scene2,
            fpsScene1=fps_scene1,
            fpsScene2=fps_scene2,
        )
        if platform == Platform.Android:
            result["jankScene1"], result["jankScene2"] = self._compare_points(
                scene1, scene2, "jank.log"
            )
        return result

    def getDiskLog(self, platform, scene):
        """获取磁盘日志数据"""
        return self._status(
            used=self._points(scene, "disk_used.log"),
            free=self._points(scene, "disk_free.log"),
        )

    def read_result(self, scene):
        """读取场景的 result.json 配置文件"""
        path = os.path.join(self.report_dir, scene, "result.json")
        result_json = open(file=path, mode="r").read()
        result_dict = json.loads(result_json)
        return result_dict
