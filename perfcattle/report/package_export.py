# -*- coding: utf-8 -*-
"""测试报告打包导出。

单报告导出（``/apm/export/report``）把性能数据、报告元信息和 Unity 采集
文件打进一个 ZIP，并附带一份按包内实际文件生成的说明文档：

    <scene>_export.zip
        EXPORT_README.txt           说明文档（文件清单、打开方式、JSON 结构）
        <scene>.xls                 性能数据（保持原有格式）
        result.json                 报告元信息
        unity/profiler.raw          Unity Profiler 原始二进制流
        unity/profiler.json         Unity Profiler 离线 JSON（不依赖 Unity）
        unity/memory_*.snapshot     Memory Profiler 快照（二进制）
        unity/meta.json             Unity 采集元信息

对比导出（``/apm/export/compare``）在此基础上生成一份对比汇总，并把每份
报告的导出内容放在 ``scenes/<scene>/`` 下，便于离线查看和二次分析：

    compare_<app>_<time>.zip
        README.txt                  说明文档（逐项列出包内所有文件）
        compare.xls                 对比汇总（指标为行，报告为列）
        compare.json                对比汇总的机器可读版本
        scenes/<scene>/...          每份报告的单报告导出内容

没有 Unity 产物的单报告仍然只返回 Excel，保持旧行为。
"""
import datetime
import json
import os
import re
import shutil
import tempfile
import zipfile

import xlwt
from logzero import logger

from perfcattle.core.platform import Platform
from perfcattle.report.excel_export import ExcelExporter
from perfcattle.report.metric_catalog import GPU_METRICS as GPU_CATALOG
from perfcattle.report.metric_catalog import UNITY_METRICS as UNITY_CATALOG
from perfcattle.unity.export import (
    DEFAULT_MAX_FRAMES,
    DEFAULT_SLOW_FRAMES,
    DEFAULT_TOP_MARKERS,
    cached_profiler_json,
    write_profiler_json,
)
from perfcattle.unity.frame_counters import CACHE_FILE as UNITY_FRAME_CACHE

BINARY_EXTENSIONS = {".raw", ".snapshot", ".mp4", ".data"}

COMPARE_FORMAT = "perfcattle-compare-report"
COMPARE_FORMAT_VERSION = 1
COMPARE_MIN_SCENES = 2
COMPARE_SCENE_LIMIT = 6

README = """PerfCattle 测试报告导出包
==========================

场景：{scene}
生成时间：{generated_at}

本包由 PerfCattle 测试报告页的“导出”生成，包含性能数据、报告元信息和
Unity 采集文件。下面按包内实际文件逐一说明。

文件清单
--------
{files}

如何打开
--------
- .xls：用 Excel / WPS 打开，每列是一项性能指标。
- unity/profiler.raw：用与采集时相同或更新的 Unity 版本打开 Unity Profiler，
  再导入该文件。
- unity/memory_*.snapshot：用 Unity Memory Profiler 打开。
- .json：用任意文本编辑器或脚本读取，不需要安装 Unity。

JSON 说明
---------
- format = {format}
- frames[] 为每帧摘要：duration_us / main_us / gc_bytes / top_markers
- summary.markers 为整个导出区间按 marker 聚合的 calls / total / self / GC
- slow_frames[] 保留了最慢若干帧的完整 Merged Hierarchy
- 帧数超过 {max_frames} 时会等间隔抽样，并在 summary.sampled 中标记

注意：JSON 由 PerfCattle 自行解析 PD3U 原始流生成，不包含 Unity 的
Counters、Callstack 和对象元数据；需要这些信息时请使用原始 profiler.raw。
{warnings}"""

COMPARE_README = """PerfCattle 对比报告导出包
==========================

App：{app}
平台：{platform}
对比报告：{scenes}
生成时间：{generated_at}

本包由 PerfCattle 对比分析页的“导出对比报告”生成。下面按包内实际文件
逐一说明，每个 scenes/<scene>/ 目录对应一份报告的完整内容。

文件清单
--------
{files}

如何查看
--------
- compare.xls：先打开这个文件查看并排对比结果；Scenes 工作表列出每份
  报告的基本信息。
- compare.json：需要脚本处理时读取这个文件。
- scenes/<scene>/<scene>.xls：该报告的完整性能数据。
- scenes/<scene>/unity/profiler.raw：Unity Profiler 原始流，用 Unity Profiler 打开。
- scenes/<scene>/unity/memory_*.snapshot：用 Unity Memory Profiler 打开。

说明
----
- compare.json 的 metrics[] 与 compare.xls 的指标行一一对应，值为 null
  表示该报告没有采集到这项指标。
- 每份报告的 Unity JSON 由 PerfCattle 解析 PD3U 原始流生成，不含 Unity
  的 Counters、Callstack 和对象元数据；需要这些信息时使用对应的 .raw。
- 帧数超过 {max_frames} 时会等间隔抽样，详见各 JSON 的 summary.sampled。
{warnings}"""


def _human_size(size):
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return "{:.0f} {}".format(value, unit)
            return "{:.1f} {}".format(value, unit)
        value /= 1024


def _describe_member(relative):
    """按包内相对路径返回 (类型, 用途说明)，用于生成文件清单。"""
    name = relative.rsplit("/", 1)[-1]
    lower = name.lower()
    if relative == "compare.xls":
        return "对比汇总（Excel）", "指标为行、报告为列；Scenes 工作表列出每份报告的信息"
    if relative == "compare.json":
        return "对比汇总（JSON）", "compare.xls 的机器可读版本，含各报告元信息和指标值"
    if lower in ("readme.txt", "export_readme.txt"):
        return "本说明文档", ""
    if lower.endswith((".xls", ".xlsx", ".csv")):
        return "性能数据（Excel）", "该场景采集到的各项性能指标"
    if name == "result.json":
        return "报告元信息", "App、平台、设备、采集时间、采集模式等"
    if name == "meta.json":
        return "Unity 采集元信息", "Unity 版本、采集模式、Profiler Modules 等"
    if lower.endswith(".raw"):
        return "Unity Profiler 原始流", "可用 Unity Profiler 打开，含 Counters / Callstack / 对象元数据"
    if lower.endswith(".snapshot"):
        return "Memory Profiler 快照", "可用 Unity Memory Profiler 打开"
    if lower.endswith(".json"):
        return "Unity Profiler 离线 JSON", "无需 Unity 即可分析帧耗时、线程耗时、热点 marker、GC"
    if lower.endswith(".log"):
        return "原始采集日志", "采集过程中写入的原始数据"
    return "其他文件", ""


def _manifest_text(entries):
    """把 (包内路径, 字节数) 列表渲染成对齐的说明清单。"""
    width = max([len(path) for path, _ in entries] + [20])
    lines = []
    for path, size in entries:
        kind, note = _describe_member(path)
        line = "  {}  {:>9}  {}".format(path.ljust(width), _human_size(size), kind)
        if note:
            line += "：{}".format(note)
        lines.append(line)
    return "\n".join(lines)


def _warnings_block(warnings):
    if not warnings:
        return ""
    lines = ["", "生成提示", "--------"]
    lines.extend("- {}".format(item) for item in warnings)
    return "\n".join(lines) + "\n"

# 与对比页面显示的摘要指标保持一致。值为 None 的列不会出现在导出包中。
SUMMARY_METRICS = (
    (
        "CPU",
        (
            ("cpuAppRate", "APP CPU"),
            ("cpuSystemRate", "Total CPU"),
        ),
    ),
    (
        "Memory",
        (
            ("totalPassAvg", "Average"),
            ("totalPassPeak", "Peak"),
            ("swapPassAvg", "Swap"),
        ),
    ),
    (
        "Network",
        (
            ("flow_recv", "Recv"),
            ("flow_send", "Send"),
        ),
    ),
    (
        "File IO",
        (
            ("file_read", "Read"),
            ("file_write", "Write"),
        ),
    ),
    (
        "FPS",
        (
            ("fps", "FPS"),
            ("frameStability", "Smooth"),
            ("jank", "Jank/min"),
        ),
    ),
    (
        "Battery",
        (
            ("batteryLevel", "Level"),
            ("batteryTeml", "Temperature"),
            ("batteryCurrent", "Current"),
            ("batteryVoltage", "Voltage"),
            ("batteryPower", "Power"),
        ),
    ),
    (
        "GPU",
        (("gpu", "GPU"),),
    ),
    (
        "Thermal",
        (
            ("thermalCpuAvg", "CPU Average"),
            ("thermalGpuAvg", "GPU Average"),
        ),
    ),
)

# 指标清单与对比页共用 metric_catalog，采集端新增计数器时只登记一次。
UNITY_METRICS = tuple(
    (key, en)
    for key, cn, en, group, decimals, page_extra, chart, unit in UNITY_CATALOG
)

# gpu_stats.log 里的扩展计数（频率/温度/显存/带宽），与 gpu.log 的占用率互补。
GPU_STATS_METRICS = tuple(
    (key, en)
    for key, cn, en, group, decimals, page_extra, chart, unit in GPU_CATALOG
)


def _unity_options(max_frames=None, top_markers=None, slow_frames=None):
    return {
        "max_frames": DEFAULT_MAX_FRAMES if max_frames is None else max_frames,
        "top_markers": DEFAULT_TOP_MARKERS
        if top_markers is None
        else top_markers,
        "slow_frames": DEFAULT_SLOW_FRAMES if slow_frames is None else slow_frames,
    }


def _has_value(value):
    """Treat the aggregator's zero placeholders as "not collected"."""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        text = value.strip()
        return text not in ("", "0", "0%", "0MB", "0°C", "0FPS")
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


class ReportPackageExporter(object):
    """把一个场景目录打包成性能数据 + Unity 原始文件的 ZIP。"""

    def __init__(self, log_io=None, excel_exporter=None):
        self.log_io = log_io
        self.excel_exporter = excel_exporter or ExcelExporter(log_io)

    @property
    def report_dir(self):
        return self.excel_exporter.report_dir

    def _scene_dir(self, scene):
        root = os.path.abspath(self.report_dir)
        target = os.path.abspath(os.path.join(root, scene or ""))
        if not scene or os.path.dirname(target) != root:
            raise ValueError("invalid scene")
        if not os.path.isdir(target):
            raise ValueError("scene not found: {}".format(scene))
        return target

    @staticmethod
    def _files(folder, suffix):
        if not os.path.isdir(folder):
            return []
        return [
            os.path.join(folder, name)
            for name in sorted(os.listdir(folder))
            if name.lower().endswith(suffix) and os.path.isfile(os.path.join(folder, name))
        ]

    @staticmethod
    def _read_json(path):
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _dedupe(paths):
        seen = set()
        unique = []
        for path in paths:
            key = os.path.abspath(path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        return unique

    def _export_unity_json(
        self, raw_path, unity_dir, meta, options, warnings, meta_path=None
    ):
        name = os.path.basename(raw_path)
        stem = "profiler" if name == "profiler.raw" else os.path.splitext(name)[0]
        out_path = os.path.join(unity_dir, "{}.json".format(stem))
        if cached_profiler_json(
            raw_path, out_path, meta_path=meta_path, **options
        ) is not None:
            logger.info("[Unity] reuse profiler json: %s", out_path)
            return out_path
        try:
            write_profiler_json(
                raw_path,
                out_path,
                meta=meta,
                max_frames=options["max_frames"],
                top_markers=options["top_markers"],
                slow_frames=options["slow_frames"],
            )
            return out_path
        except Exception as e:
            logger.exception(e)
            warnings.append("{}: {}".format(name, e))
            return None

    def collect(
        self,
        platform,
        scene,
        max_frames=None,
        top_markers=None,
        slow_frames=None,
    ):
        """生成单报告的 Excel / Unity JSON，并返回待打包文件清单。

        返回值中的 ``members`` 都是绝对路径，``scene_dir`` 用于计算 ZIP 内的
        相对路径。对比导出直接复用它，保证两种导出包含同样的原始文件。
        """
        options = _unity_options(max_frames, top_markers, slow_frames)
        scene_dir = self._scene_dir(scene)
        xls_path = self.excel_exporter.export_excel(platform=platform, scene=scene)
        unity_dir = os.path.join(scene_dir, "unity")
        raw_files = self._files(unity_dir, ".raw")
        snapshots = self._files(unity_dir, ".snapshot")
        has_unity = bool(raw_files or snapshots)

        warnings = []
        json_files = []
        meta_path = os.path.join(unity_dir, "meta.json")
        if has_unity:
            meta = self._read_json(meta_path)
            for raw_path in raw_files:
                json_path = self._export_unity_json(
                    raw_path, unity_dir, meta, options, warnings, meta_path
                )
                if json_path:
                    json_files.append(json_path)

        members = [xls_path]
        result_json = os.path.join(scene_dir, "result.json")
        if os.path.isfile(result_json):
            members.append(result_json)
        # Include raw APM logs and the session metadata so the bundle can be
        # re-audited independently of the aggregate Excel/JSON summaries.
        for name in sorted(os.listdir(scene_dir)):
            if not os.path.isfile(os.path.join(scene_dir, name)):
                continue
            if name == "apm_session.json" or name.lower().endswith(".log"):
                members.append(os.path.join(scene_dir, name))
        members.extend(json_files)
        if os.path.isfile(meta_path):
            members.append(meta_path)
        # 逐帧计数器缓存只占几百 KB，带上后离线也能复现峰值/卡顿统计，
        # 不必再解析整条 raw 流。
        frame_cache = os.path.join(unity_dir, UNITY_FRAME_CACHE)
        if os.path.isfile(frame_cache):
            members.append(frame_cache)
        members.extend(raw_files)
        members.extend(snapshots)
        members = self._dedupe(members)

        return {
            "scene": scene,
            "scene_dir": scene_dir,
            "xls": xls_path,
            "has_unity": has_unity,
            "members": members,
            "unity_files": [
                os.path.relpath(path, scene_dir).replace(os.sep, "/")
                for path in raw_files + snapshots
            ],
            "json_files": [
                os.path.relpath(path, scene_dir).replace(os.sep, "/")
                for path in json_files
            ],
            "warnings": warnings,
        }

    def export(
        self,
        platform,
        scene,
        max_frames=None,
        top_markers=None,
        slow_frames=None,
    ):
        """生成导出包，返回 ``status/path/package`` 结构。"""
        collected = self.collect(
            platform,
            scene,
            max_frames=max_frames,
            top_markers=top_markers,
            slow_frames=slow_frames,
        )
        if not collected["has_unity"]:
            return {
                "status": 1,
                "msg": "success",
                "path": collected["xls"],
                "package": False,
            }

        options = _unity_options(max_frames, top_markers, slow_frames)
        scene_dir = collected["scene_dir"]
        zip_path = os.path.join(scene_dir, "{}_export.zip".format(scene))
        tmp_path = zip_path + ".tmp"
        generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entries = []
        for path in collected["members"]:
            if not os.path.isfile(path):
                continue
            arcname = os.path.relpath(path, scene_dir).replace(os.sep, "/")
            entries.append((arcname, os.path.getsize(path)))
        entries.append(("EXPORT_README.txt", None))
        entries.append(("manifest.json", None))

        manifest = {
            "format": "perfcattle-report-manifest",
            "format_version": 1,
            "scene": scene,
            "generated_at": generated_at,
            "files": [],
        }
        for path in collected["members"]:
            if not os.path.isfile(path):
                continue
            arcname = os.path.relpath(path, scene_dir).replace(os.sep, "/")
            file_entry = {"path": arcname, "size": os.path.getsize(path)}
            if arcname.lower().endswith(".log"):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fp:
                        file_entry["lines"] = sum(1 for _ in fp)
                except OSError:
                    pass
            manifest["files"].append(file_entry)

        readme = README.format(
            scene=scene,
            generated_at=generated_at,
            files=_manifest_text(entries),
            format="perfcattle-unity-profiler-json",
            max_frames=options["max_frames"] or "unlimited",
            warnings=_warnings_block(collected["warnings"]),
        )

        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            bundle.writestr("EXPORT_README.txt", readme)
            for path in collected["members"]:
                if not os.path.isfile(path):
                    continue
                arcname = os.path.relpath(path, scene_dir).replace(os.sep, "/")
                extension = os.path.splitext(path)[1].lower()
                compression = (
                    zipfile.ZIP_STORED
                    if extension in BINARY_EXTENSIONS
                    else zipfile.ZIP_DEFLATED
                )
                bundle.write(path, arcname, compress_type=compression)
        os.replace(tmp_path, zip_path)

        return {
            "status": 1,
            "msg": "success",
            "path": zip_path,
            "package": True,
            "xls": collected["xls"],
            "unity_files": collected["unity_files"],
            "json_files": collected["json_files"],
            "warnings": collected["warnings"],
        }


class ComparePackageExporter(object):
    """把多份报告的对比汇总和各自的原始导出内容打包成一个 ZIP。"""

    def __init__(self, log_io, aggregator, package_exporter):
        self.log_io = log_io
        self.aggregator = aggregator
        self.package_exporter = package_exporter

    @property
    def report_dir(self):
        return self.log_io.report_dir

    def _validate(self, scenes):
        cleaned = []
        for scene in scenes or []:
            scene = str(scene).strip()
            if scene and scene not in cleaned:
                cleaned.append(scene)
        if not COMPARE_MIN_SCENES <= len(cleaned) <= COMPARE_SCENE_LIMIT:
            raise ValueError("scenes must contain 2-6 reports")

        root = os.path.abspath(self.report_dir)
        metas = []
        for scene in cleaned:
            target = os.path.abspath(os.path.join(root, scene))
            if os.path.dirname(target) != root or not os.path.isdir(target):
                raise ValueError("invalid scene: {}".format(scene))
            meta = self._read_result(target, scene)
            if meta.get("model") != "normal":
                raise ValueError("not a normal report: {}".format(scene))
            metas.append(meta)

        app = metas[0].get("app")
        platform = metas[0].get("platform")
        for scene, meta in zip(cleaned, metas):
            if meta.get("app") != app or meta.get("platform") != platform:
                raise ValueError(
                    "all reports must share the same app and platform: {}".format(scene)
                )
        return cleaned, metas, app, platform

    @staticmethod
    def _read_result(target, scene):
        try:
            with open(os.path.join(target, "result.json"), encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, ValueError):
            raise ValueError("report metadata not found: {}".format(scene))
        if not isinstance(data, dict):
            raise ValueError("report metadata is invalid: {}".format(scene))
        return data

    def _scene_summary(self, scene, platform, warnings):
        try:
            if platform == Platform.Android:
                return self.aggregator._setAndroidPerfs(scene)
            return self.aggregator._setiOSPerfs(scene)
        except Exception as e:
            logger.exception(e)
            warnings.append("{} summary: {}".format(scene, e))
            return {}

    def _unity_summary(self, scene, platform, warnings):
        try:
            # 导出是显式操作，可以等逐帧缓存生成完，避免 Excel 用 1 秒采样
            # 的数据而分析页用逐帧数据。
            data = self.log_io.getUnityStatsLog(platform, scene, wait=30)
            return {
                "avg": data.get("avg", {}),
                "charts": data.get("charts", {}),
                "peaks": data.get("peaks", {}),
                "frame_count": data.get("frame_count"),
            }
        except Exception as e:
            logger.exception(e)
            warnings.append("{} unity stats: {}".format(scene, e))
            return {"avg": {}, "charts": {}}

    def _gpu_summary(self, scene, platform, warnings):
        try:
            data = self.log_io.getGpuStatsLog(platform, scene)
            return data.get("avg", {})
        except Exception as e:
            logger.exception(e)
            warnings.append("{} gpu stats: {}".format(scene, e))
            return {}

    @staticmethod
    def _metric_rows(scenes, summaries, unity_summaries, gpu_summaries):
        rows = []
        for section, metrics in SUMMARY_METRICS:
            for key, label in metrics:
                values = {}
                for scene in scenes:
                    value = summaries.get(scene, {}).get(key)
                    # The aggregator writes 0 for "not collected"; keep null
                    # in the bundle instead of a misleading zero.
                    values[scene] = value if _has_value(value) else None
                if any(value is not None for value in values.values()):
                    rows.append({"section": section, "label": label, "values": values})

        for key, label in UNITY_METRICS:
            values = {}
            for scene in scenes:
                average = unity_summaries.get(scene, {}).get("avg", {})
                values[scene] = average.get(key) if key in average else None
            if any(value is not None for value in values.values()):
                rows.append({"section": "Unity", "label": label, "values": values})

        for key, label in GPU_STATS_METRICS:
            values = {}
            for scene in scenes:
                average = gpu_summaries.get(scene, {})
                values[scene] = average.get(key) if key in average else None
            if any(value is not None for value in values.values()):
                rows.append({"section": "GPU Stats", "label": label, "values": values})
        return rows

    @staticmethod
    def _write_excel(path, scenes, metas, rows):
        wb = xlwt.Workbook(encoding="utf-8")
        ws = wb.add_sheet("Comparison")
        headers = ["Section", "Metric"] + list(scenes)
        for column, title in enumerate(headers):
            ws.write(0, column, title)
        for row_index, row in enumerate(rows, start=1):
            ws.write(row_index, 0, row["section"])
            ws.write(row_index, 1, row["label"])
            for column, scene in enumerate(scenes, start=2):
                value = row["values"].get(scene)
                ws.write(row_index, column, "" if value is None else value)

        info = wb.add_sheet("Scenes")
        info_headers = ["Scene", "App", "Platform", "Model", "Device", "Ctime"]
        for column, title in enumerate(info_headers):
            info.write(0, column, title)
        for row_index, (scene, meta) in enumerate(zip(scenes, metas), start=1):
            for column, value in enumerate(
                [
                    scene,
                    meta.get("app"),
                    meta.get("platform"),
                    meta.get("model"),
                    meta.get("devices"),
                    meta.get("ctime"),
                ]
            ):
                info.write(row_index, column, "" if value is None else value)
        wb.save(path)

    def _unique_zip_path(self, app):
        safe = re.sub(r"[^0-9A-Za-z._-]+", "_", str(app or "compare")).strip("._")
        safe = safe or "compare"
        stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        root = os.path.abspath(self.report_dir)
        path = os.path.join(root, "compare_{}_{}.zip".format(safe, stamp))
        counter = 1
        while os.path.exists(path):
            path = os.path.join(
                root, "compare_{}_{}_{}.zip".format(safe, stamp, counter)
            )
            counter += 1
        return path

    def export(
        self,
        platform,
        scenes,
        max_frames=None,
        top_markers=None,
        slow_frames=None,
    ):
        """生成对比导出包，返回 ``status/path`` 结构。"""
        scenes, metas, app, scene_platform = self._validate(scenes)
        platform = scene_platform or platform
        options = _unity_options(max_frames, top_markers, slow_frames)

        warnings = []
        summaries = {}
        unity_summaries = {}
        gpu_summaries = {}
        for scene in scenes:
            summaries[scene] = self._scene_summary(scene, platform, warnings)
            unity_summaries[scene] = self._unity_summary(scene, platform, warnings)
            gpu_summaries[scene] = self._gpu_summary(scene, platform, warnings)

        rows = self._metric_rows(scenes, summaries, unity_summaries, gpu_summaries)
        generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        temp_dir = tempfile.mkdtemp(prefix="perfcattle-compare-")
        try:
            compare_xls = os.path.join(temp_dir, "compare.xls")
            self._write_excel(compare_xls, scenes, metas, rows)

            compare_json = os.path.join(temp_dir, "compare.json")
            payload = {
                "format": COMPARE_FORMAT,
                "format_version": COMPARE_FORMAT_VERSION,
                "generated_at": generated_at,
                "app": app,
                "platform": platform,
                "scenes": [
                    {
                        "scene": scene,
                        "metadata": meta,
                        "summary": summaries.get(scene, {}),
                        "unity": unity_summaries.get(scene, {}),
                        "gpu_stats": gpu_summaries.get(scene, {}),
                    }
                    for scene, meta in zip(scenes, metas)
                ],
                "metrics": rows,
            }
            with open(compare_json, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
                fp.write("\n")

            scene_packages = []
            for scene in scenes:
                collected = self.package_exporter.collect(
                    platform,
                    scene,
                    max_frames=options["max_frames"],
                    top_markers=options["top_markers"],
                    slow_frames=options["slow_frames"],
                )
                warnings.extend(collected["warnings"])
                scene_packages.append((scene, collected))

            entries = [
                ("compare.xls", os.path.getsize(compare_xls)),
                ("compare.json", os.path.getsize(compare_json)),
            ]
            for scene, collected in scene_packages:
                scene_dir = collected["scene_dir"]
                for path in collected["members"]:
                    if not os.path.isfile(path):
                        continue
                    relative = os.path.relpath(path, scene_dir).replace(os.sep, "/")
                    entries.append(
                        ("scenes/{}/{}".format(scene, relative), os.path.getsize(path))
                    )
            entries.append(("README.txt", None))

            readme = COMPARE_README.format(
                app=app,
                platform=platform,
                scenes=", ".join(scenes),
                generated_at=generated_at,
                files=_manifest_text(entries),
                max_frames=options["max_frames"] or "unlimited",
                warnings=_warnings_block(warnings),
            )

            zip_path = self._unique_zip_path(app)
            tmp_path = zip_path + ".tmp"
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as bundle:
                bundle.write(compare_xls, "compare.xls")
                bundle.write(compare_json, "compare.json")
                bundle.writestr("README.txt", readme)

                for scene, collected in scene_packages:
                    scene_dir = collected["scene_dir"]
                    for path in collected["members"]:
                        if not os.path.isfile(path):
                            continue
                        relative = os.path.relpath(path, scene_dir).replace(
                            os.sep, "/"
                        )
                        arcname = "scenes/{}/{}".format(scene, relative)
                        extension = os.path.splitext(path)[1].lower()
                        compression = (
                            zipfile.ZIP_STORED
                            if extension in BINARY_EXTENSIONS
                            else zipfile.ZIP_DEFLATED
                        )
                        bundle.write(path, arcname, compress_type=compression)
            os.replace(tmp_path, zip_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        return {
            "status": 1,
            "msg": "success",
            "path": zip_path,
            "package": True,
            "scenes": scenes,
            "app": app,
            "platform": platform,
            "warnings": warnings,
        }


__all__ = [
    "COMPARE_FORMAT",
    "COMPARE_FORMAT_VERSION",
    "COMPARE_MIN_SCENES",
    "COMPARE_SCENE_LIMIT",
    "ComparePackageExporter",
    "ReportPackageExporter",
]
