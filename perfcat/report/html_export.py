# -*- encoding: utf-8 -*-
"""HTML 报告导出模块。

负责生成 Android / iOS 的 HTML 性能报告。
"""
import os
import math
from jinja2 import Environment, FileSystemLoader
from perfcat.report.log_io import LogIO


class HtmlExporter:
    """HTML 报告生成器"""

    TEMPLATE_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
        "assets",
        "report_template",
    )

    def __init__(self, log_io: LogIO = None):
        """初始化导出器

        Args:
            log_io: LogIO 实例，用于获取报告目录路径。如果为 None 则创建默认实例。
        """
        self.log_io = log_io or LogIO()

    @property
    def report_dir(self):
        return self.log_io.report_dir

    @classmethod
    def _has_points(cls, value):
        if isinstance(value, dict):
            return any(cls._has_points(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return len(value) > 0
        return bool(value)

    @staticmethod
    def _format_score(value):
        value = round(value, 2)
        if value == int(value):
            return str(int(value))
        return f"{value:.2f}".rstrip("0").rstrip(".")

    @classmethod
    def _point_values(cls, points):
        values = []
        for point in points or []:
            if isinstance(point, dict):
                value = point.get("y")
            elif isinstance(point, (list, tuple)) and len(point) >= 2:
                value = point[1]
            else:
                continue
            if isinstance(value, (int, float)):
                values.append(value)
        return values

    @classmethod
    def _peak_point_text(cls, points, suffix):
        values = cls._point_values(points)
        if not values:
            return 0
        return f"{round(max(values), 2)}{suffix}"

    @classmethod
    def _frame_stability_index(cls, points):
        values = cls._point_values(points)
        if not values:
            return 0
        average = sum(values) / len(values)
        if average <= 0:
            return 0
        variance = sum((value - average) ** 2 for value in values) / len(values)
        return cls._format_score(max(0, 100 - (math.sqrt(variance) / average * 100)))

    @classmethod
    def _thermal_average_text(cls, thermal_charts, group):
        if not isinstance(thermal_charts, dict):
            return 0
        values = cls._point_values(thermal_charts.get(group))
        if not values:
            return 0
        return f"{cls._format_score(sum(values) / len(values))}°C"

    def _android_summary_flags(self, summary):
        cpu_charts = summary.get("cpu_charts", {})
        mem_charts = summary.get("mem_charts", {})
        net_charts = summary.get("net_charts", {})
        file_io_charts = summary.get("file_io_charts", {})
        thermal_charts = summary.get("thermal_charts", {})
        thermal_points = (
            thermal_charts.get("thermal", thermal_charts)
            if isinstance(thermal_charts, dict)
            else {}
        )
        if not isinstance(thermal_points, dict):
            thermal_points = {}
        battery_charts = summary.get("battery_charts", {})
        fps_charts = summary.get("fps_charts")
        fps_points = (
            fps_charts.get("fps") if isinstance(fps_charts, dict) else fps_charts
        )
        jank_charts = summary.get("jank_charts")
        jank_points = (
            jank_charts.get("jank") if isinstance(jank_charts, dict) else jank_charts
        )
        return {
            "cpu_app": self._has_points(cpu_charts.get("cpuAppData")),
            "cpu_system": self._has_points(cpu_charts.get("cpuSysData")),
            "mem_total": self._has_points(mem_charts.get("memTotalData")),
            "mem_peak": self._has_points(mem_charts.get("memTotalData")),
            "mem_swap": self._has_points(mem_charts.get("memSwapData")),
            "fps": self._has_points(fps_points),
            "frame_stability": self._has_points(fps_points),
            "jank": self._has_points(jank_points),
            "battery_level": self._has_points(battery_charts.get("batteryLevel")),
            "battery_temp": self._has_points(battery_charts.get("batteryTem")),
            "network_send": self._has_points(net_charts.get("upFlow")),
            "network_recv": self._has_points(net_charts.get("downFlow")),
            "file_io": self._has_points(file_io_charts.get("read"))
            or self._has_points(file_io_charts.get("write")),
            "gpu": self._has_points(summary.get("gpu_charts", {}).get("gpu")),
            "thermal": self._has_points(thermal_points),
            "thermal_cpu_avg": self._has_points(thermal_points.get("CPU")),
            "thermal_gpu_avg": self._has_points(thermal_points.get("GPU")),
        }

    def _ios_summary_flags(self, summary):
        cpu_charts = summary.get("cpu_charts", {})
        mem_charts = summary.get("mem_charts", {})
        net_charts = summary.get("net_charts", {})
        battery_charts = summary.get("battery_charts", {})
        fps_charts = summary.get("fps_charts")
        fps_points = (
            fps_charts.get("fps") if isinstance(fps_charts, dict) else fps_charts
        )
        return {
            "cpu_app": self._has_points(cpu_charts.get("cpuAppData")),
            "cpu_system": self._has_points(cpu_charts.get("cpuSysData")),
            "gpu": self._has_points(summary.get("gpu_charts", {}).get("gpu")),
            "mem_total": self._has_points(mem_charts.get("memTotalData")),
            "mem_peak": self._has_points(mem_charts.get("memTotalData")),
            "fps": self._has_points(fps_points),
            "frame_stability": self._has_points(fps_points),
            "battery_temp": self._has_points(battery_charts.get("batteryTem")),
            "battery_current": self._has_points(battery_charts.get("batteryCurrent")),
            "battery_voltage": self._has_points(battery_charts.get("batteryVoltage")),
            "battery_power": self._has_points(battery_charts.get("batteryPower")),
            "network_send": self._has_points(net_charts.get("upFlow")),
            "network_recv": self._has_points(net_charts.get("downFlow")),
        }

    def make_android_html(self, scene, summary: dict, report_path=None):
        """生成 Android HTML 报告

        Args:
            scene: 场景目录名
            summary: 包含所有性能摘要数据的字典
            report_path: 自定义输出路径，为 None 时输出到场景目录下

        Returns:
            str: 生成的 HTML 文件路径
        """
        file_loader = FileSystemLoader(self.TEMPLATE_DIR)
        env = Environment(loader=file_loader)
        template = env.get_template("android.html")
        if report_path:
            html_path = report_path
        else:
            html_path = os.path.join(self.report_dir, scene, "report.html")
        file_io_charts = summary.get("file_io_charts", {"read": [], "write": []})
        thermal_charts = summary.get("thermal_charts", {"thermal": {}})
        thermal_charts = thermal_charts.get("thermal", thermal_charts)
        if not isinstance(thermal_charts, dict):
            thermal_charts = {}
        summary_flags = summary.get("summary_flags") or self._android_summary_flags(
            summary
        )
        mem_charts = summary.get("mem_charts", {})
        fps_points = summary.get("fps_charts", [])
        mem_peak = summary.get("mem_peak") or self._peak_point_text(
            mem_charts.get("memTotalData"), "MB"
        )
        frame_stability = summary.get(
            "frame_stability"
        ) or self._frame_stability_index(fps_points)
        thermal_cpu_avg = summary.get("thermal_cpu_avg") or self._thermal_average_text(
            thermal_charts, "CPU"
        )
        thermal_gpu_avg = summary.get("thermal_gpu_avg") or self._thermal_average_text(
            thermal_charts, "GPU"
        )
        with open(html_path, "w+") as fout:
            html_content = template.render(
                devices=summary["devices"],
                app=summary["app"],
                platform=summary["platform"],
                ctime=summary["ctime"],
                cpu_app=summary["cpu_app"],
                cpu_sys=summary["cpu_sys"],
                mem_total=summary["mem_total"],
                mem_peak=mem_peak,
                mem_swap=summary["mem_swap"],
                fps=summary["fps"],
                frame_stability=frame_stability,
                jank=summary["jank"],
                level=summary["level"],
                tem=summary["tem"],
                net_send=summary["net_send"],
                net_recv=summary["net_recv"],
                file_read=summary.get("file_read", 0),
                file_write=summary.get("file_write", 0),
                thermal_cpu_avg=thermal_cpu_avg,
                thermal_gpu_avg=thermal_gpu_avg,
                cpu_charts=summary["cpu_charts"],
                mem_charts=summary["mem_charts"],
                net_charts=summary["net_charts"],
                file_io_charts=file_io_charts,
                thermal_charts=thermal_charts,
                battery_charts=summary["battery_charts"],
                fps_charts=summary["fps_charts"],
                jank_charts=summary["jank_charts"],
                mem_detail_charts=summary["mem_detail_charts"],
                gpu=summary["gpu"],
                gpu_charts=summary["gpu_charts"],
                gpu_stats=summary.get("gpu_stats", {}),
                gpu_stats_charts=summary.get("gpu_stats_charts", {}),
                summary_flags=summary_flags,
            )

            fout.write(html_content)
        return html_path

    def make_ios_html(self, scene, summary: dict, report_path=None):
        """生成 iOS HTML 报告

        Args:
            scene: 场景目录名
            summary: 包含所有性能摘要数据的字典
            report_path: 自定义输出路径，为 None 时输出到场景目录下

        Returns:
            str: 生成的 HTML 文件路径
        """
        file_loader = FileSystemLoader(self.TEMPLATE_DIR)
        env = Environment(loader=file_loader)
        template = env.get_template("ios.html")
        if report_path:
            html_path = report_path
        else:
            html_path = os.path.join(self.report_dir, scene, "report.html")
        summary_flags = summary.get("summary_flags") or self._ios_summary_flags(
            summary
        )
        mem_charts = summary.get("mem_charts", {})
        fps_charts = summary.get("fps_charts", {})
        fps_points = (
            fps_charts.get("fps") if isinstance(fps_charts, dict) else fps_charts
        )
        mem_peak = summary.get("mem_peak") or self._peak_point_text(
            mem_charts.get("memTotalData"), "MB"
        )
        frame_stability = summary.get(
            "frame_stability"
        ) or self._frame_stability_index(fps_points)
        with open(html_path, "w+") as fout:
            html_content = template.render(
                devices=summary["devices"],
                app=summary["app"],
                platform=summary["platform"],
                ctime=summary["ctime"],
                cpu_app=summary["cpu_app"],
                cpu_sys=summary["cpu_sys"],
                gpu=summary["gpu"],
                mem_total=summary["mem_total"],
                mem_peak=mem_peak,
                process=summary.get("process", ""),
                process_start=summary.get("process_start", ""),
                network_process=summary.get("network_process", ""),
                network_process_start=summary.get("network_process_start", ""),
                fps=summary["fps"],
                frame_stability=frame_stability,
                tem=summary["tem"],
                current=summary["current"],
                voltage=summary["voltage"],
                power=summary["power"],
                net_send=summary["net_send"],
                net_recv=summary["net_recv"],
                cpu_charts=summary["cpu_charts"],
                mem_charts=summary["mem_charts"],
                net_charts=summary["net_charts"],
                battery_charts=summary["battery_charts"],
                fps_charts=summary["fps_charts"],
                gpu_charts=summary["gpu_charts"],
                summary_flags=summary_flags,
            )
            fout.write(html_content)
        return html_path
