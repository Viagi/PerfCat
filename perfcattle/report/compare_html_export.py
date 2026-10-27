# -*- coding: utf-8 -*-
"""对比报告 HTML 导出。

把对比页的摘要表和曲线导出成一份自包含的 HTML 文件：曲线数据内嵌在页面里，
用浏览器直接打开即可离线查看，不需要 PerfCattle 服务或报告目录。

数据来源与对比导出包一致：摘要来自 aggregator，Unity/GPU 计数器来自
``unity_stats.log`` / ``gpu_stats.log``，指标清单见 ``metric_catalog``。
"""
import datetime
import os
import re

from jinja2 import Environment, FileSystemLoader
from logzero import logger

from perfcattle.core.platform import Platform
from perfcattle.report.metric_catalog import EXTRA_SECTIONS
from perfcattle.report.metric_catalog import GPU_METRICS
from perfcattle.report.metric_catalog import UNITY_METRICS
from perfcattle.report.package_export import _has_value

TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
    "assets",
    "report_template",
)

# group -> (中文标题, 英文标题)
SECTION_TITLES = {group: (cn, en) for group, cn, en in EXTRA_SECTIONS}

# 摘要表：与对比页一致，值为 aggregator 已经格式化好的文本（如 "12.5%"）。
SUMMARY_SECTIONS = (
    (
        "CPU",
        "CPU",
        (
            ("cpuAppRate", "应用", "APP"),
            ("cpuSystemRate", "整机", "Total"),
        ),
    ),
    (
        "内存",
        "Memory",
        (
            ("totalPassAvg", "总内存", "Total"),
            ("totalPassPeak", "峰值", "Peak"),
            ("swapPassAvg", "交换内存", "Swap"),
        ),
    ),
    (
        "网络",
        "Network",
        (
            ("flow_recv", "接收", "Recv"),
            ("flow_send", "发送", "Send"),
        ),
    ),
    (
        "File IO",
        "File IO",
        (
            ("file_read", "读取", "Read"),
            ("file_write", "写入", "Write"),
        ),
    ),
    (
        "FPS",
        "FPS",
        (
            ("fps", "FPS", "FPS"),
            ("frameStability", "流畅度", "Smooth"),
            ("jank", "卡顿/分钟", "jank/min"),
        ),
    ),
    (
        "电量",
        "Battery",
        (
            ("batteryLevel", "电量", "Level"),
            ("batteryTeml", "温度", "Temperature"),
            ("batteryCurrent", "电流", "Current"),
            ("batteryVoltage", "电压", "Voltage"),
            ("batteryPower", "功耗", "Power"),
        ),
    ),
    (
        "GPU",
        "GPU",
        (("gpu", "占用率", "Usage"),),
    ),
    (
        "温度传感器",
        "Temperature",
        (
            ("thermalCpuAvg", "CPU 均温", "CPU Temp"),
            ("thermalGpuAvg", "GPU 均温", "GPU Temp"),
        ),
    ),
)

# 系统曲线：id / target / path / 中文标题 / 英文标题 / ApexCharts 选项
BASE_CHARTS = (
    ("chart-cpu-app", "cpu", "cpuAppData", "CPU 使用率 (%)：应用", "CPU (%) : APP Usage", {"unit": "%"}),
    ("chart-cpu-system", "cpu", "cpuSysData", "CPU 使用率 (%)：整机", "CPU (%) : System Usage", {"unit": "%"}),
    ("chart-mem-total", "mem", "memTotalData", "内存 (MB)：总内存", "Memory (MB) : Total Usage", {"unit": "MB"}),
    ("chart-mem-swap", "mem", "memSwapData", "内存 (MB)：交换内存", "Memory (MB) : Swap Usage", {"unit": "MB"}),
    ("chart-fps", "fps", "fps", "FPS (HZ)", "FPS (HZ)", {}),
    ("chart-jank", "fps", "jank", "卡顿次数", "Jank (Times)", {"integer": True}),
    ("chart-battery-level", "battery", "batteryLevel", "电量", "Battery : Level", {"unit": "%"}),
    ("chart-battery-temp", "battery", "batteryTem", "电池温度", "Battery : Temperature", {"unit": "°C"}),
    ("chart-battery-current", "battery", "batteryCurrent", "电流", "Battery : Current", {}),
    ("chart-battery-voltage", "battery", "batteryVoltage", "电压", "Battery : Voltage", {}),
    ("chart-battery-power", "battery", "batteryPower", "功耗", "Battery : Power", {}),
    ("chart-networkdata-recv", "flow", "downFlow", "网络流量 (KB)：接收", "Network Data (KB) : Recv", {"unit": "KB"}),
    ("chart-networkdata-send", "flow", "upFlow", "网络流量 (KB)：发送", "Network Data (KB) : Send", {"unit": "KB"}),
    ("chart-file-io-read", "file_io", "read", "文件 IO (KB)：读取", "File IO (KB) : Read", {"unit": "KB"}),
    ("chart-file-io-write", "file_io", "write", "文件 IO (KB)：写入", "File IO (KB) : Write", {"unit": "KB"}),
    ("chart-file-io-total-read", "file_io", "read", "文件 IO 累计 (KB)：读取", "File IO (KB) : Total Read", {"unit": "KB", "cumulative": True}),
    ("chart-file-io-total-write", "file_io", "write", "文件 IO 累计 (KB)：写入", "File IO (KB) : Total Write", {"unit": "KB", "cumulative": True}),
    ("chart-gpu", "gpu", "gpu", "GPU (%)", "GPU (%)", {"unit": "%"}),
    ("chart-thermal-cpu", "thermal", "thermal.CPU", "温度传感器 (°C)：CPU", "Thermal Sensor (°C) : CPU", {"unit": "°C"}),
    ("chart-thermal-gpu", "thermal", "thermal.GPU", "温度传感器 (°C)：GPU", "Thermal Sensor (°C) : GPU", {"unit": "°C"}),
    ("chart-thermal-memory", "thermal", "thermal.Memory", "温度传感器 (°C)：内存", "Thermal Sensor (°C) : Memory", {"unit": "°C"}),
    ("chart-thermal-battery", "thermal", "thermal.Battery", "温度传感器 (°C)：电池", "Thermal Sensor (°C) : Battery", {"unit": "°C"}),
    ("chart-disk-used", "disk", "used", "磁盘 (KB)：已用", "Disk (KB) : Used", {"unit": "KB"}),
    ("chart-disk-free", "disk", "free", "磁盘 (KB)：可用", "Disk (KB) : Free", {"unit": "KB"}),
)

# 内存详情曲线（Android）：path 前缀 memory_detail.
MEMORY_DETAIL_CHARTS = (
    ("java_heap", "Java Heap"),
    ("native_heap", "Native Heap"),
    ("code_pss", "Code PSS"),
    ("stack_pss", "Stack PSS"),
    ("graphics_pss", "Graphics PSS"),
    ("private_pss", "Private PSS"),
    ("system_pss", "System PSS"),
)

# Unity 基础曲线：group 决定落在哪个 Unity 分组里。
UNITY_BASE_CHARTS = (
    ("frame", "chart-unity-cpu-frame", "charts.cpu_frame_ms", "Unity 帧耗时 (ms)：CPU", "Unity Frame (ms) : CPU", {"unit": "ms"}),
    ("frame", "chart-unity-gpu-frame", "charts.gpu_frame_ms", "Unity 帧耗时 (ms)：GPU", "Unity Frame (ms) : GPU", {"unit": "ms"}),
    ("batching", "chart-unity-drawcalls", "charts.draw_calls", "Unity Draw Calls", "Unity Draw Calls", {"integer": True}),
    ("geometry", "chart-unity-triangles", "charts.triangles", "Unity 几何：三角面 (k)", "Unity Geometry : Triangles (k)", {"unit": "k", "decimals": 1}),
    ("geometry", "chart-unity-vertices", "charts.vertices", "Unity 几何：顶点 (k)", "Unity Geometry : Vertices (k)", {"unit": "k", "decimals": 1}),
    ("resources", "chart-unity-render-textures", "charts.render_textures_mb", "Unity Render Textures (MB)", "Unity Render Textures (MB)", {"unit": "MB", "decimals": 1}),
    ("resources", "chart-unity-used-buffers", "charts.used_buffers_mb", "Unity Used Buffers (MB)", "Unity Used Buffers (MB)", {"unit": "MB", "decimals": 1}),
    ("resources", "chart-unity-vertex-upload", "charts.vertex_upload_kb", "Unity VB 上传 (KB/帧)", "Unity VB Upload (KB/frame)", {"unit": "KB", "decimals": 1}),
    ("resources", "chart-unity-index-upload", "charts.index_upload_kb", "Unity IB 上传 (KB/帧)", "Unity IB Upload (KB/frame)", {"unit": "KB", "decimals": 1}),
)

UNITY_GROUP_ORDER = ("frame", "batching", "geometry", "memory", "resources")
GPU_GROUP = "gpu"


def _format_number(value, decimals):
    if decimals == 0:
        return str(int(round(value)))
    return ("{0:." + str(decimals) + "f}").format(value)


class CompareHtmlExporter(object):
    """把多份报告渲染成一份自包含的对比 HTML。"""

    def __init__(self, log_io, aggregator, compare_exporter):
        self.log_io = log_io
        self.aggregator = aggregator
        self.compare_exporter = compare_exporter

    @property
    def report_dir(self):
        return self.log_io.report_dir

    # === 数据收集 ===

    def _scene_charts(self, platform, scene, warnings):
        """读取各 target 的曲线数据；单个 target 失败不影响页面其他部分。"""
        loaders = {
            "cpu": lambda: self.log_io.getCpuLog(platform, scene),
            "cpu_core": lambda: self.log_io.getCpuCoreLog(platform, scene),
            "mem": lambda: self.log_io.getMemLog(platform, scene),
            "mem_detail": lambda: self.log_io.getMemDetailLog(platform, scene),
            "fps": lambda: self.log_io.getFpsLog(platform, scene),
            "battery": lambda: self.log_io.getBatteryLog(platform, scene),
            "flow": lambda: self.log_io.getFlowLog(platform, scene),
            "file_io": lambda: self.log_io.getFileIOLog(platform, scene),
            "gpu": lambda: self.log_io.getGpuLog(platform, scene),
            "thermal": lambda: self.log_io.getThermalLog(platform, scene),
            "disk": lambda: self.log_io.getDiskLog(platform, scene),
        }
        data = {}
        for target, loader in loaders.items():
            try:
                data[target] = loader()
            except Exception as e:
                logger.exception(e)
                warnings.append("{} {}: {}".format(scene, target, e))
                data[target] = {}
        return data

    @staticmethod
    def _pick(data, path):
        value = data
        for key in str(path or "").split("."):
            if not isinstance(value, dict):
                return []
            value = value.get(key)
            if value is None:
                return []
        return value if isinstance(value, list) else []

    @staticmethod
    def _normalize_points(points):
        result = []
        for index, point in enumerate(points or []):
            if isinstance(point, dict):
                x, y = point.get("x"), point.get("y")
            elif isinstance(point, (list, tuple)) and len(point) >= 2:
                x, y = point[0], point[1]
            else:
                x, y = index, point
            if x is None or y is None:
                continue
            try:
                x = float(x)
                y = float(y)
            except (TypeError, ValueError):
                continue
            result.append([round(x, 3), y])
        return result

    @staticmethod
    def _cumulative(points):
        total = 0.0
        result = []
        for x, y in points:
            total += y
            result.append([x, round(total, 2)])
        return result

    def _series(self, scenes, chart_data, target, path, cumulative=False):
        series = []
        for scene in scenes:
            points = self._pick(chart_data.get(scene, {}).get(target), path)
            points = self._normalize_points(points)
            if cumulative:
                points = self._cumulative(points)
            if points:
                series.append({"name": scene, "data": points})
        return series

    @staticmethod
    def _scene_meta(meta, scene):
        devices = meta.get("devices")
        if isinstance(devices, (list, tuple)):
            devices = ", ".join(str(item) for item in devices)
        return {
            "scene": scene,
            "app": meta.get("app"),
            "platform": meta.get("platform"),
            "device": devices or "-",
            "ctime": meta.get("ctime") or "-",
        }

    def _summary_sections(self, scenes, summaries, lang):
        sections = []
        index = 0 if lang == "cn" else 1
        for cn_title, en_title, metrics in SUMMARY_SECTIONS:
            rows = []
            for key, cn_label, en_label in metrics:
                cells = []
                present = False
                for scene in scenes:
                    value = summaries.get(scene, {}).get(key)
                    if _has_value(value):
                        present = True
                        cells.append(str(value))
                    else:
                        cells.append("-")
                if present:
                    rows.append({"label": (cn_label, en_label)[index], "cells": cells})
            if rows:
                sections.append({"title": (cn_title, en_title)[index], "metrics": rows})
        return sections

    @staticmethod
    def _catalog_sections(scenes, averages, catalog, lang):
        """按 metric_catalog 生成 Unity / GPU 摘要分组。"""
        sections = []
        groups = []
        for key, cn, en, group, decimals, _page_extra, _chart, unit in catalog:
            if group not in groups:
                groups.append(group)
        for group in groups:
            rows = []
            for key, cn, en, metric_group, decimals, _page_extra, _chart, unit in catalog:
                if metric_group != group:
                    continue
                cells = []
                present = False
                for scene in scenes:
                    average = averages.get(scene, {})
                    value = average.get(key)
                    if value is None:
                        cells.append("-")
                        continue
                    present = True
                    try:
                        cells.append(_format_number(float(value), decimals))
                    except (TypeError, ValueError):
                        cells.append(str(value))
                if not present:
                    continue
                label = cn if lang == "cn" else en
                if lang == "cn" and unit:
                    label = "{} ({})".format(label, unit)
                rows.append({"label": label, "cells": cells})
            if not rows:
                continue
            title = SECTION_TITLES.get(group)
            if title is None:
                title = (group, group)
            sections.append(
                {
                    "title": title[0] if lang == "cn" else title[1],
                    "metrics": rows,
                }
            )
        return sections

    # === 曲线 ===

    def _base_charts(self, scenes, chart_data, lang, cores):
        charts = []
        for chart_id, target, path, cn, en, opts in BASE_CHARTS:
            charts.append(
                {
                    "id": chart_id,
                    "target": target,
                    "path": path,
                    "title": cn if lang == "cn" else en,
                    "opts": opts,
                }
            )
            if chart_id == "chart-cpu-system":
                for core in range(cores):
                    charts.append(
                        {
                            "id": "chart-cpu-core-{}".format(core),
                            "target": "cpu_core",
                            "path": "cpu_core.cpu{}".format(core),
                            "title": (
                                "CPU 使用率 (%)：核心 {}".format(core)
                                if lang == "cn"
                                else "CPU (%) : Core {}".format(core)
                            ),
                            "opts": {"unit": "%"},
                        }
                    )
        for key, label in MEMORY_DETAIL_CHARTS:
            charts.append(
                {
                    "id": "chart-mem-detail-{}".format(key),
                    "target": "mem_detail",
                    "path": "memory_detail.{}".format(key),
                    "title": (
                        "内存详情 (MB)：{}".format(label)
                        if lang == "cn"
                        else "Memory (MB) : {}".format(label)
                    ),
                    "opts": {"unit": "MB"},
                }
            )

        result = []
        for spec in charts:
            series = self._series(scenes, chart_data, spec["target"], spec["path"])
            if series:
                result.append({**spec, "series": series})
        return result

    def _unity_groups(self, scenes, chart_data, lang):
        groups = {group: [] for group in UNITY_GROUP_ORDER}
        gpu_charts = []

        for group, chart_id, path, cn, en, opts in UNITY_BASE_CHARTS:
            series = self._series(scenes, chart_data, "unity_stats", path)
            if series:
                groups[group].append(
                    {
                        "id": chart_id,
                        "title": cn if lang == "cn" else en,
                        "opts": opts,
                        "series": series,
                    }
                )

        for key, cn, en, group, decimals, _page_extra, chart, unit in UNITY_METRICS:
            if not chart:
                continue
            series = self._series(
                scenes, chart_data, "unity_stats", "charts.{}".format(key)
            )
            if not series:
                continue
            label = cn if lang == "cn" else en
            if lang == "cn" and unit:
                label = "{} ({})".format(label, unit)
            groups.setdefault(group, []).append(
                {
                    "id": "chart-unity-{}".format(key.replace("_", "-")),
                    "title": label,
                    "opts": (
                        {"integer": True}
                        if decimals == 0
                        else {"decimals": decimals, "unit": unit}
                    ),
                    "series": series,
                }
            )

        for key, cn, en, _group, decimals, _page_extra, chart, unit in GPU_METRICS:
            if not chart:
                continue
            series = self._series(
                scenes, chart_data, "gpu_stats", "charts.{}".format(key)
            )
            if not series:
                continue
            label = cn if lang == "cn" else en
            if lang == "cn" and unit:
                label = "{} ({})".format(label, unit)
            gpu_charts.append(
                {
                    "id": "chart-{}".format(key.replace("_", "-")),
                    "title": label,
                    "opts": {"decimals": decimals, "unit": unit},
                    "series": series,
                }
            )

        result = []
        for group in UNITY_GROUP_ORDER:
            charts = groups.get(group) or []
            if not charts:
                continue
            title = SECTION_TITLES.get(group, (group, group))
            result.append(
                {"title": (title[0], title[1])[0 if lang == "cn" else 1], "charts": charts}
            )
        if gpu_charts:
            title = SECTION_TITLES.get(GPU_GROUP, ("GPU", "GPU"))
            result.append(
                {
                    "title": (title[0], title[1])[0 if lang == "cn" else 1],
                    "charts": gpu_charts,
                }
            )
        return result

    # === 导出 ===

    def _output_path(self, app):
        safe = re.sub(r"[^0-9A-Za-z._-]+", "_", str(app or "compare")).strip("._")
        safe = safe or "compare"
        stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        root = os.path.abspath(self.report_dir)
        path = os.path.join(root, "compare_{}_{}.html".format(safe, stamp))
        counter = 1
        while os.path.exists(path):
            path = os.path.join(
                root, "compare_{}_{}_{}.html".format(safe, stamp, counter)
            )
            counter += 1
        return path

    def export(self, platform, scenes, lan=None):
        """生成对比 HTML，返回文件路径。"""
        scenes, metas, app, scene_platform = self.compare_exporter._validate(scenes)
        platform = scene_platform or platform
        lang = "en" if str(lan or "cn").lower().startswith("en") else "cn"

        warnings = []
        summaries = {}
        unity_data = {}
        gpu_data = {}
        chart_data = {}
        cores = 0
        for scene, meta in zip(scenes, metas):
            summaries[scene] = self.compare_exporter._scene_summary(
                scene, platform, warnings
            )
            try:
                unity_data[scene] = self.log_io.getUnityStatsLog(platform, scene)
            except Exception as e:
                logger.exception(e)
                warnings.append("{} unity stats: {}".format(scene, e))
                unity_data[scene] = {"avg": {}, "charts": {}}
            try:
                gpu_data[scene] = self.log_io.getGpuStatsLog(platform, scene)
            except Exception as e:
                logger.exception(e)
                warnings.append("{} gpu stats: {}".format(scene, e))
                gpu_data[scene] = {"avg": {}, "charts": {}}
            chart_data[scene] = self._scene_charts(platform, scene, warnings)
            chart_data[scene]["unity_stats"] = unity_data[scene]
            chart_data[scene]["gpu_stats"] = gpu_data[scene]
            try:
                cores = max(cores, int(meta.get("cores", 0) or 0))
            except (TypeError, ValueError):
                pass
        if platform != Platform.Android:
            cores = 0

        unity_avg = {scene: unity_data[scene].get("avg") or {} for scene in scenes}
        gpu_avg = {scene: gpu_data[scene].get("avg") or {} for scene in scenes}
        summary_sections = self._summary_sections(scenes, summaries, lang)
        summary_sections.extend(
            self._catalog_sections(scenes, unity_avg, UNITY_METRICS, lang)
        )
        summary_sections.extend(
            self._catalog_sections(scenes, gpu_avg, GPU_METRICS, lang)
        )
        chart_groups = [
            {
                "title": "系统指标" if lang == "cn" else "System",
                "charts": self._base_charts(scenes, chart_data, lang, cores),
            }
        ]
        chart_groups.extend(self._unity_groups(scenes, chart_data, lang))
        chart_groups = [group for group in chart_groups if group["charts"]]

        context = {
            "lang": lang,
            "title": "对比分析" if lang == "cn" else "Data Comparison",
            "app": app,
            "platform": platform,
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "scenes": scenes,
            "scene_meta": [
                self._scene_meta(meta, scene) for scene, meta in zip(scenes, metas)
            ],
            "summary_sections": summary_sections,
            "chart_groups": chart_groups,
            "warnings": warnings,
        }
        env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)
        html = env.get_template("compare.html").render(**context)

        path = self._output_path(app)
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(html)
        logger.info("[Compare] html: %s", path)
        return path


__all__ = ["CompareHtmlExporter"]
