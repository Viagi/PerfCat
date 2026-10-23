# -*- encoding: utf-8 -*-
"""报告管理模块

负责报告目录管理、报告生成、缓存清理等操作。
"""
import json
import os
import shutil
import time
from logzero import logger
from perfcattle.core.platform import Platform
from perfcattle.report.log_io import LogIO
from perfcattle.report.aggregator import Aggregator
from perfcattle.report.excel_export import ExcelExporter
from perfcattle.report.html_export import HtmlExporter
from perfcattle.report.package_export import (
    ComparePackageExporter,
    ReportPackageExporter,
)

REPORT_ARTIFACT_EXTENSIONS = {"log", "json", "mp4"}
REPORT_ARTIFACT_MOVE_RETRIES = 5


class ReportManager:
    """报告管理器 - 负责报告目录管理和报告生成流程"""

    def __init__(self, fileroot="."):
        self.fileroot = fileroot
        self.report_dir = self.get_repordir()

    def get_repordir(self):
        """获取报告目录路径，不存在时自动创建"""
        report_dir = os.path.join(os.getcwd(), "report")
        if not os.path.exists(report_dir):
            os.mkdir(report_dir)
        return report_dir

    def _is_report_artifact(self, path):
        filename = os.path.basename(path)
        extension = filename.rsplit(".", 1)[-1]
        return os.path.isfile(path) and extension in REPORT_ARTIFACT_EXTENSIONS

    def _move_report_artifact(self, path, target_dir):
        filename = os.path.basename(path)
        for attempt in range(REPORT_ARTIFACT_MOVE_RETRIES):
            try:
                shutil.move(path, target_dir)
                return None
            except PermissionError:
                if attempt < REPORT_ARTIFACT_MOVE_RETRIES - 1:
                    logger.warning(
                        "File %s is locked, retrying in 1 second... (attempt %s/%s)",
                        filename,
                        attempt + 1,
                        REPORT_ARTIFACT_MOVE_RETRIES,
                    )
                    time.sleep(1)
                    continue

                logger.error(
                    "Failed to move %s after %s attempts, skipping",
                    filename,
                    REPORT_ARTIFACT_MOVE_RETRIES,
                )
                return filename

    def _move_report_artifacts(self, target_dir):
        failed_files = []
        for filename in os.listdir(self.report_dir):
            path = os.path.join(self.report_dir, filename)
            if filename == "unity_capture" and os.path.isdir(path):
                unity_target = os.path.join(target_dir, "unity")
                if os.path.exists(unity_target):
                    shutil.rmtree(unity_target, ignore_errors=True)
                try:
                    shutil.move(path, unity_target)
                except (PermissionError, OSError) as e:
                    logger.warning("Failed to move unity_capture: %s", e)
                    failed_files.append(filename)
                continue
            if not self._is_report_artifact(path):
                continue

            failed_file = self._move_report_artifact(path, target_dir)
            if failed_file:
                failed_files.append(failed_file)
        return failed_files

    def make_report(
        self, app, devices, video, platform=Platform.Android, model="normal", cores=0,
        process="", process_start="", network_process="", network_process_start=""
    ):
        """创建性能报告

        将当前报告目录下的日志和视频文件移动到以时间戳命名的子目录中，
        并创建 result.json 元数据文件。

        Args:
            app: 应用包名
            devices: 设备标识
            video: 视频标记
            platform: 平台类型
            model: 模式（normal/pk）
            cores: CPU 核心数

        Returns:
            str: 报告目录名（如 "apm_2024-01-01-12-00-00"）
        """
        current_time = time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime())
        report_new_dir = os.path.join(self.report_dir, f"apm_{current_time}")
        if not os.path.exists(report_new_dir):
            os.mkdir(report_new_dir)

        # 移动采集产物（mp4 可能被 scrcpy 锁定）
        failed_files = self._move_report_artifacts(report_new_dir)

        # 在目标目录中直接创建 result.json（确保始终存在）
        result_dict = {
            "app": app,
            "icon": "",
            "platform": platform,
            "model": model,
            "devices": devices,
            "ctime": current_time,
            "video": video
            if not failed_files
            else "",  # 如果 mp4 移动失败则清空视频标记
            "cores": cores,
        }
        if platform == Platform.iOS and process:
            result_dict.update(process=process, process_start=process_start)
        if platform == Platform.iOS and network_process:
            result_dict.update(network_process=network_process, network_process_start=network_process_start)
        result_path = os.path.join(report_new_dir, "result.json")
        with open(result_path, "w", encoding="utf-8") as fp:
            fp.write(json.dumps(result_dict))

        if failed_files:
            logger.warning(
                f"Report generated with some files failed to move: {failed_files}"
            )
        return f"apm_{current_time}"

    def clear_file(self):
        """清理报告目录中的缓存文件（log、json、mp4）"""
        from perfcattle.tools.recording import Scrcpy

        Scrcpy.stop_record()
        if os.path.exists(self.report_dir):
            for filename in os.listdir(self.report_dir):
                path = os.path.join(self.report_dir, filename)
                if not self._is_report_artifact(path):
                    continue
                for attempt in range(4):
                    try:
                        os.remove(path)
                        break
                    except PermissionError:
                        # Unity stats logger may hold the file for a few ms.
                        if attempt == 3:
                            logger.warning("skip locked report file: %s", filename)
                        else:
                            time.sleep(0.05)
        stale_unity = os.path.join(self.report_dir, "unity_capture")
        if os.path.isdir(stale_unity):
            shutil.rmtree(stale_unity, ignore_errors=True)

    def list_other_scenes(self, scene, app=None, platform=None, model=None):
        """过滤并按时间排序场景目录列表（排除指定场景）

        仅返回包含 result.json 的完整报告目录，避免把导出文件、
        采集中间产物等无关条目放进对比下拉框。传入 app / platform /
        model 时只保留对应报告，例如对比页只列同一 App、同一平台的
        normal 报告。

        Args:
            scene: 要排除的场景名
            app: 只保留该 App 的报告（None 表示不过滤）
            platform: 只保留该平台的报告（None 表示不过滤）
            model: 只保留该模式的报告，例如 "normal"（None 表示不过滤）

        Returns:
            list: 按修改时间倒序排列的场景目录列表
        """
        dirs = []
        for name in os.listdir(self.report_dir):
            path = os.path.join(self.report_dir, name)
            if not os.path.isdir(path):
                continue
            result_path = os.path.join(path, "result.json")
            if not os.path.isfile(result_path):
                continue
            if app is not None or platform is not None or model is not None:
                try:
                    with open(result_path, encoding="utf-8") as fp:
                        meta = json.load(fp)
                except (OSError, ValueError):
                    continue
                if not isinstance(meta, dict):
                    continue
                if app is not None and meta.get("app") != app:
                    continue
                if platform is not None and meta.get("platform") != platform:
                    continue
                if model is not None and meta.get("model") != model:
                    continue
            dirs.append(name)
        dir_list = list(
            reversed(
                sorted(
                    dirs,
                    key=lambda x: os.path.getmtime(os.path.join(self.report_dir, x)),
                )
            )
        )
        if scene in dir_list:
            dir_list.remove(scene)
        return dir_list

    def read_result(self, scene):
        """读取场景的 result.json 配置文件

        Args:
            scene: 场景目录名

        Returns:
            dict: result.json 中的数据
        """
        path = os.path.join(self.report_dir, scene, "result.json")
        result_json = open(file=path, mode="r").read()
        result_dict = json.loads(result_json)
        return result_dict

    def format_size(self, size, a_kilobyte_is_1024_bytes=True):
        """将文件大小转换为可读格式

        Args:
            size: 文件大小（字节）
            a_kilobyte_is_1024_bytes: 是否使用 1024 作为进制

        Returns:
            str: 人类可读的文件大小字符串
        """
        suffixes = {
            1000: ["KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB"],
            1024: ["KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB", "YiB"],
        }

        if size < 0:
            raise ValueError("number must be non-negative")

        multiple = 1024 if a_kilobyte_is_1024_bytes else 1000

        for suffix in suffixes[multiple]:
            size /= multiple
            if size < multiple:
                return "{0:.2f} {1}".format(size, suffix)

    def instance_type(self, data):
        """判断数据类型

        Args:
            data: 要判断的数据

        Returns:
            str: "float" 或 "int"
        """
        if isinstance(data, float):
            return "float"
        elif isinstance(data, int):
            return "int"
        else:
            return "int"


class ReportStore(ReportManager):
    """报告数据入口，组合日志读写、聚合和导出能力。"""

    def __init__(self, fileroot="."):
        super().__init__(fileroot)
        self._log_io = LogIO(fileroot)
        # 确保 log_io 使用相同的 report_dir
        self._log_io.report_dir = self.report_dir
        self._aggregator = Aggregator(self._log_io)
        self._excel_exporter = ExcelExporter(self._log_io)
        self._package_exporter = ReportPackageExporter(
            self._log_io, self._excel_exporter
        )
        self._compare_exporter = ComparePackageExporter(
            self._log_io, self._aggregator, self._package_exporter
        )
        self._html_exporter = HtmlExporter(self._log_io)

    # === LogIO 委托方法 ===

    def open_file(self, path, mode):
        """以生成器方式逐行读取文件"""
        return self._log_io.open_file(path, mode)

    def add_log(self, path, log_time, value):
        """添加一条日志记录"""
        return self._log_io.add_log(path, log_time, value)

    def add_json_log(self, path, log_time, value):
        """追加一条 JSON 行日志（GPU 扩展指标）"""
        return self._log_io.add_json_log(path, log_time, value)

    def begin_session(self, clock=None):
        """记录采集起点，供报告内所有图表统一时间轴"""
        return self._log_io.begin_session(clock=clock)

    def scene_time_range(self, scene):
        """报告内所有图表共用的 x 轴范围"""
        return self._log_io.scene_time_range(scene)

    def scene_origin(self, scene):
        """报告内所有图表共用的时间原点"""
        return self._log_io.scene_origin(scene)

    def create_file(self, filename, content=""):
        """在报告目录下创建文件"""
        return self._log_io.create_file(filename, content)

    def record_net(self, type, send, recv):
        """记录网络数据"""
        return self._log_io.record_net(type, send, recv)

    def readLog(self, scene, filename):
        """读取日志数据"""
        return self._log_io.readLog(scene, filename)

    def getLogCompare(self, scene1, scene2, filename):
        """获取任意单日志文件的双场景对比数据"""
        return self._log_io.getLogCompare(scene1, scene2, filename)

    def getCpuLog(self, platform, scene):
        """获取 CPU 日志数据"""
        return self._log_io.getCpuLog(platform, scene)

    def getCpuLogCompare(self, platform, scene1, scene2):
        """获取 CPU 对比日志数据"""
        return self._log_io.getCpuLogCompare(platform, scene1, scene2)

    def getGpuLog(self, platform, scene):
        """获取 GPU 日志数据"""
        return self._log_io.getGpuLog(platform, scene)

    def getGpuStatsLog(self, platform, scene):
        """获取 GPU 扩展指标（含 MB/帧带宽）"""
        return self._log_io.getGpuStatsLog(platform, scene)

    def getUnityStatsLog(self, platform, scene):
        """获取 Unity 侧实时计数器"""
        return self._log_io.getUnityStatsLog(platform, scene)

    def getUnityStatsLogCompare(self, platform, scene1, scene2):
        """获取 Unity 侧实时计数器的双场景对比数据"""
        return self._log_io.getUnityStatsLogCompare(platform, scene1, scene2)

    def getUnityCorrelationLog(self, platform, scene):
        """获取 CPU 与 Unity 计数器的统一时间轴数据"""
        return self._log_io.getUnityCorrelationLog(platform, scene)

    def getGpuLogCompare(self, platform, scene1, scene2):
        """获取 GPU 对比日志数据"""
        return self._log_io.getGpuLogCompare(platform, scene1, scene2)

    def getMemLog(self, platform, scene):
        """获取内存日志数据"""
        return self._log_io.getMemLog(platform, scene)

    def getMemLogCompare(self, platform, scene1, scene2):
        """获取内存对比日志数据"""
        return self._log_io.getMemLogCompare(platform, scene1, scene2)

    def getMemDetailLog(self, platform, scene):
        """获取内存详情日志数据"""
        return self._log_io.getMemDetailLog(platform, scene)

    def getMemDetailLogCompare(self, platform, scene1, scene2):
        """获取内存详情对比日志数据"""
        return self._log_io.getMemDetailLogCompare(platform, scene1, scene2)

    def getThermalLog(self, platform, scene):
        """获取温度传感器日志数据"""
        return self._log_io.getThermalLog(platform, scene)

    def getThermalLogCompare(self, platform, scene1, scene2):
        """获取温度传感器对比日志数据"""
        return self._log_io.getThermalLogCompare(platform, scene1, scene2)

    def getCpuCoreLog(self, platform, scene):
        """获取 CPU 核心日志数据"""
        return self._log_io.getCpuCoreLog(platform, scene)

    def getCpuCoreLogCompare(self, platform, scene1, scene2):
        """获取 CPU 核心对比日志数据"""
        return self._log_io.getCpuCoreLogCompare(platform, scene1, scene2)

    def getBatteryLog(self, platform, scene):
        """获取电池日志数据"""
        return self._log_io.getBatteryLog(platform, scene)

    def getBatteryLogCompare(self, platform, scene1, scene2):
        """获取电池对比日志数据"""
        return self._log_io.getBatteryLogCompare(platform, scene1, scene2)

    def getFlowLog(self, platform, scene):
        """获取流量日志数据"""
        return self._log_io.getFlowLog(platform, scene)

    def getFlowSendLogCompare(self, platform, scene1, scene2):
        """获取上行流量对比日志数据"""
        return self._log_io.getFlowSendLogCompare(platform, scene1, scene2)

    def getFlowRecvLogCompare(self, platform, scene1, scene2):
        """获取下行流量对比日志数据"""
        return self._log_io.getFlowRecvLogCompare(platform, scene1, scene2)

    def getFileIOLog(self, platform, scene):
        """获取文件读写日志数据"""
        return self._log_io.getFileIOLog(platform, scene)

    def getFileIOLogCompare(self, platform, scene1, scene2):
        """获取文件读写对比日志数据"""
        return self._log_io.getFileIOLogCompare(platform, scene1, scene2)

    def getFpsLog(self, platform, scene):
        """获取 FPS 日志数据"""
        return self._log_io.getFpsLog(platform, scene)

    def getFpsLogCompare(self, platform, scene1, scene2):
        """获取 FPS 对比日志数据"""
        return self._log_io.getFpsLogCompare(platform, scene1, scene2)

    def getDiskLog(self, platform, scene):
        """获取磁盘日志数据"""
        return self._log_io.getDiskLog(platform, scene)

    # === Aggregator 委托方法 ===

    def _setAndroidPerfs(self, scene):
        """汇总 Android 性能数据"""
        return self._aggregator._setAndroidPerfs(scene)

    def _setiOSPerfs(self, scene):
        """汇总 iOS 性能数据"""
        return self._aggregator._setiOSPerfs(scene)

    def _setpkPerfs(self, scene):
        """汇总 PK 对比数据"""
        return self._aggregator._setpkPerfs(scene)

    def analysisDisk(self, scene):
        """分析磁盘数据"""
        return self._aggregator.analysisDisk(scene)

    # === ExcelExporter 委托方法 ===

    def export_excel(self, platform, scene):
        """导出 Excel 报告"""
        return self._excel_exporter.export_excel(platform, scene)

    def export_report(self, platform, scene, max_frames=None, top_markers=None,
                      slow_frames=None):
        """导出报告包（Excel + Unity 原始文件 + Unity Profiler JSON）。"""
        return self._package_exporter.export(
            platform,
            scene,
            max_frames=max_frames,
            top_markers=top_markers,
            slow_frames=slow_frames,
        )

    def export_compare(self, platform, scenes, max_frames=None, top_markers=None,
                       slow_frames=None):
        """导出对比报告包（对比汇总 + 各报告原始文件 + Unity JSON/二进制）。"""
        return self._compare_exporter.export(
            platform,
            scenes,
            max_frames=max_frames,
            top_markers=top_markers,
            slow_frames=slow_frames,
        )

    # === HtmlExporter 委托方法 ===

    def make_android_html(self, scene, summary: dict, report_path=None):
        """生成 Android HTML 报告"""
        return self._html_exporter.make_android_html(scene, summary, report_path)

    def make_ios_html(self, scene, summary: dict, report_path=None):
        """生成 iOS HTML 报告"""
        return self._html_exporter.make_ios_html(scene, summary, report_path)
