# -*- encoding: utf-8 -*-
"""性能监控服务

AppPerformanceMonitor 继承自 ServiceConfig，提供 Python API 层面的
性能数据采集与报告生成功能。
"""
import datetime
import os
import time
from concurrent.futures import ThreadPoolExecutor, wait
from logzero import logger
from perfcattle.core.config import ServiceConfig
from perfcattle.core.platform import Platform
from perfcattle.device.manager import Devices
from perfcattle.report.manager import ReportStore
from perfcattle.tools.recording import Scrcpy
from perfcattle.collector.cpu import CPU
from perfcattle.collector.memory import Memory
from perfcattle.collector.battery import Battery
from perfcattle.collector.network import Network
from perfcattle.collector.fps import FPS
from perfcattle.core.metrics import MetricSample, MetricStatus
from perfcattle.collector.gpu import GPU
from perfcattle.collector.gpu_stats import create_gpu_stats, clear_gpu_stats
from perfcattle.collector.thermal import ThermalSensor
from perfcattle.collector.disk import Disk
from perfcattle.collector.file_io import FileIO
from perfcattle.collector.ios_perf import IOSPerformance

d = Devices()
f = ReportStore()


class AppPerformanceMonitor(ServiceConfig):
    """性能监控服务（Python API）

    继承自 ServiceConfig，通过进程池并行采集各维度性能数据。
    """

    SAMPLE_INTERVAL_SECONDS = 1.0

    def __init__(
        self,
        pkgName=None,
        platform=Platform.Android,
        deviceId=None,
        surfaceview=True,
        noLog=True,
        pid=None,
        processName=None,
        record=False,
        collect_all=False,
        duration=0,
    ):
        self.pkgName = pkgName
        self.deviceId = deviceId
        self.platform = platform
        self.surfaceview = surfaceview
        self.noLog = noLog
        self.pid = pid
        self.processName = processName
        self.record = record
        self.collect_all = collect_all
        self.duration = duration
        self.end_time = time.time() + self.duration
        d.devicesCheck(
            platform=self.platform, deviceid=self.deviceId, pkgname=self.pkgName
        )
        self.start()

    def _sleep_until_next_sample(self, started):
        sleep_time = self.SAMPLE_INTERVAL_SECONDS - (time.time() - started)
        if sleep_time > 0:
            time.sleep(sleep_time)

    def _continue_sampling(self, started):
        if self.collect_all is False:
            return False
        if self.duration > 0 and time.time() > self.end_time:
            return False
        self._sleep_until_next_sample(started)
        return True

    @staticmethod
    def clear_collector_sessions():
        IOSPerformance.clear()
        CPU.clear()
        Memory.clear()
        Network.clear()
        FPS.clear()
        GPU.clear()
        Battery.clear()
        Disk.clear()
        FileIO.clear()
        ThermalSensor.clear()
        clear_gpu_stats()

    def collectCpu(self):
        """采集 CPU 使用率"""
        _cpu = CPU.getObject(
            self.pkgName,
            self.deviceId,
            self.platform,
            pid=self.pid,
            processName=self.processName,
        )
        result = {}
        while self.get_status() == "on":
            started = time.time()
            appCpuRate, systemCpuRate = _cpu.getCpuRate(noLog=self.noLog)
            result = {"appCpuRate": appCpuRate, "systemCpuRate": systemCpuRate}
            logger.info(f"cpu: {result}")
            if not self._continue_sampling(started):
                break
        return result

    def collectCoreCpu(self):
        """采集各 CPU 核心使用率"""
        _cpucore = CPU.getObject(
            self.pkgName,
            self.deviceId,
            self.platform,
            pid=self.pid,
            processName=self.processName,
        )
        cores = d.getCpuCores(self.deviceId)
        value = _cpucore.getCoreCpuRate(cores=cores, noLog=self.noLog)
        result = {"cpu{}".format(value.index(element)): element for element in value}
        logger.info(f"cpu core: {result}")
        return result

    def collectMemory(self):
        """采集内存使用量"""
        _memory = Memory.getObject(
            self.pkgName,
            self.deviceId,
            self.platform,
            pid=self.pid,
            processName=self.processName,
        )
        result = {}
        while self.get_status() == "on":
            started = time.time()
            total, swap = _memory.getProcessMemory(noLog=self.noLog)
            result = {"total": total, "swap": swap}
            logger.info(f"memory: {result}")
            if not self._continue_sampling(started):
                break
        return result

    def collectMemoryDetail(self):
        """采集 Android 内存详情"""
        _memory = Memory.getObject(
            self.pkgName,
            self.deviceId,
            self.platform,
            pid=self.pid,
            processName=self.processName,
        )
        result = {}
        while self.get_status() == "on":
            started = time.time()
            if self.platform == Platform.iOS:
                break
            result = _memory.getAndroidMemoryDetail(noLog=self.noLog)
            logger.info(f"memory detail: {result}")
            if not self._continue_sampling(started):
                break
        return result

    def collectBattery(self):
        """采集电池信息"""
        _battery = Battery.getObject(self.deviceId, self.platform)
        result = {}
        while self.get_status() == "on":
            started = time.time()
            final = _battery.getBattery(noLog=self.noLog)
            if self.platform == Platform.Android:
                result = {"level": final[0], "temperature": final[1]}
            else:
                result = {
                    "temperature": final[0],
                    "current": final[1],
                    "voltage": final[2],
                    "power": final[3],
                }
            logger.info(f"battery: {result}")
            if not self._continue_sampling(started):
                break
        return result

    def collectNetwork(self, wifi=True):
        """采集网络流量"""
        _network = Network.getObject(
            self.pkgName,
            self.deviceId,
            self.platform,
            pid=self.pid,
            processName=self.processName,
        )
        if self.noLog is False and self.platform == Platform.Android:
            data = _network.setAndroidNet(wifi=wifi)
            f.record_net("pre", data[0], data[1])
        result = {}
        while self.get_status() == "on":
            started = time.time()
            upFlow, downFlow = _network.getNetWorkData(wifi=wifi, noLog=self.noLog)
            result = {"send": upFlow, "recv": downFlow}
            logger.info(f"network: {result}")
            if not self._continue_sampling(started):
                break
        return result

    def collectFileIO(self):
        """采集进程文件读写增量"""
        if self.platform != Platform.Android:
            return {}
        _file_io = FileIO.getObject(
            self.pkgName,
            self.deviceId,
            self.platform,
            pid=self.pid,
            processName=self.processName,
        )
        result = {}
        try:
            if self.noLog is False:
                _file_io.setAndroidFileIO()
            while self.get_status() == "on":
                started = time.time()
                read, write = _file_io.getFileIOData(noLog=self.noLog)
                result = {"read": read, "write": write, "source": _file_io.last_source}
                logger.info(f"file io: {result}")
                if not self._continue_sampling(started):
                    break
        finally:
            _file_io.stop()
        return result

    def collectFps(self):
        """采集帧率数据"""
        _fps = FPS.getObject(
            self.pkgName, self.deviceId, self.platform, self.surfaceview
        )
        result = {}
        while self.get_status() == "on":
            started = time.time()
            sample = _fps.getFPS(noLog=self.noLog)
            if isinstance(sample, MetricSample):
                if sample.status in (MetricStatus.OK, MetricStatus.CACHED):
                    result = {"fps": sample.value, "jank": None, "status": sample.status.value,
                              "sample_epoch_ms": sample.sample_epoch_ms, "source": sample.source}
                else:
                    result = {"fps": None, "jank": None, "status": sample.status.value, "error": sample.error}
            else:
                fps, jank = sample
                result = {"fps": fps, "jank": jank}
            logger.info(f"fps: {result}")
            if not self._continue_sampling(started):
                break
        return result

    def collectGpu(self):
        """采集 GPU 使用率"""
        _gpu = GPU.getObject(self.pkgName, self.deviceId, self.platform)
        result = {}
        while self.get_status() == "on":
            started = time.time()
            sample = _gpu.getGPU(noLog=self.noLog)
            if isinstance(sample, MetricSample):
                result = sample.to_dict()
                result["jank"] = None
            else:
                result = {"gpu": sample}
            logger.info(f"gpu: {result}")
            if not self._continue_sampling(started):
                break
        return result

    def collectGpuStats(self):
        """采集 GPU 扩展指标（频率/总线/显存/读写带宽）"""
        if self.platform != Platform.Android:
            return {}
        _gpu = create_gpu_stats(
            deviceId=self.deviceId,
            pkgName=self.pkgName,
            pid=self.pid,
            platform=self.platform,
        )
        result = {}
        while self.get_status() == "on":
            started = time.time()
            data = _gpu.sample()
            if not data:
                result = {"status": MetricStatus.MISSING.value, "metrics": {}}
                break
            result = {"status": MetricStatus.OK.value, "metrics": data, "sample_epoch_ms": int(time.time() * 1000),
                      "snapshot_only": False}
            logger.info(f"gpu stats: {result}")
            if self.noLog is False:
                apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
                f.add_json_log(
                    os.path.join(f.report_dir, "gpu_stats.log"), apm_time, data
                )
            if not self._continue_sampling(started):
                break
        return result

    def collectThermal(self):
        """采集温度传感器数据"""
        _thermal = ThermalSensor.getObject(self.deviceId, self.platform)
        result = []
        while self.get_status() == "on":
            started = time.time()
            result = _thermal.getThermalTemp(noLog=self.noLog)
            logger.info(f"thermal: {result}")
            if not self._continue_sampling(started):
                break
        return result

    def collectDisk(self):
        """采集磁盘使用数据"""
        _disk = Disk.getObject(self.deviceId, self.platform)
        result = _disk.getDisk()
        result.setdefault("snapshot_only", True)
        result.setdefault("sample_epoch_ms", int(time.time() * 1000))
        result.setdefault("sample_monotonic_ns", time.monotonic_ns())
        result.setdefault("status", MetricStatus.OK.value)
        logger.info(f"disk: {result}")
        return result

    def _android_report_summary(self, scene, summary):
        fps_log = f.getFpsLog(Platform.Android, scene)
        gpu_stats = f.getGpuStatsLog(Platform.Android, scene)
        unity_stats = f.getUnityStatsLog(Platform.Android, scene)
        unity_correlation = f.getUnityCorrelationLog(Platform.Android, scene)
        return {
            "app": summary["app"],
            "platform": summary["platform"],
            "devices": summary["devices"],
            "ctime": summary["ctime"],
            "cpu_app": summary["cpuAppRate"],
            "cpu_sys": summary["cpuSystemRate"],
            "mem_total": summary["totalPassAvg"],
            "mem_peak": summary["totalPassPeak"],
            "mem_swap": summary["swapPassAvg"],
            "fps": summary["fps"],
            "frame_stability": summary["frameStability"],
            "jank": summary["jank"],
            "level": summary["batteryLevel"],
            "tem": summary["batteryTeml"],
            "net_send": summary["flow_send"],
            "net_recv": summary["flow_recv"],
            "file_read": summary["file_read"],
            "file_write": summary["file_write"],
            "gpu": summary["gpu"],
            "thermal_cpu_avg": summary.get("thermalCpuAvg", 0),
            "thermal_gpu_avg": summary.get("thermalGpuAvg", 0),
            "cpu_charts": f.getCpuLog(Platform.Android, scene),
            "mem_charts": f.getMemLog(Platform.Android, scene),
            "mem_detail_charts": f.getMemDetailLog(Platform.Android, scene),
            "net_charts": f.getFlowLog(Platform.Android, scene),
            "file_io_charts": f.getFileIOLog(Platform.Android, scene),
            "battery_charts": f.getBatteryLog(Platform.Android, scene),
            "fps_charts": fps_log["fps"],
            "jank_charts": fps_log["jank"],
            "gpu_charts": f.getGpuLog(Platform.Android, scene),
            "gpu_stats": gpu_stats["avg"],
            "gpu_stats_charts": gpu_stats["charts"],
            "unity_stats": unity_stats["avg"],
            "unity_stats_charts": unity_stats["charts"],
            "unity_correlation_charts": unity_correlation["charts"],
            "time_range": f.scene_time_range(scene),
            "summary_flags": summary.get("summary_flags", {}),
        }

    def _ios_report_summary(self, scene, summary):
        return {
            "app": summary["app"],
            "platform": summary["platform"],
            "devices": summary["devices"],
            "ctime": summary["ctime"],
            "cpu_app": summary["cpuAppRate"],
            "cpu_sys": summary["cpuSystemRate"],
            "mem_total": summary["totalPassAvg"],
            "mem_peak": summary["totalPassPeak"],
            "fps": summary["fps"],
            "frame_stability": summary["frameStability"],
            "current": summary["batteryCurrent"],
            "voltage": summary["batteryVoltage"],
            "power": summary["batteryPower"],
            "tem": summary["batteryTeml"],
            "gpu": summary["gpu"],
            "net_send": summary["flow_send"],
            "net_recv": summary["flow_recv"],
            "cpu_charts": f.getCpuLog(Platform.iOS, scene),
            "mem_charts": f.getMemLog(Platform.iOS, scene),
            "net_charts": f.getFlowLog(Platform.iOS, scene),
            "thermal_charts": f.getThermalLog(Platform.Android, scene),
            "battery_charts": f.getBatteryLog(Platform.iOS, scene),
            "fps_charts": f.getFpsLog(Platform.iOS, scene),
            "gpu_charts": f.getGpuLog(Platform.iOS, scene),
            "summary_flags": summary.get("summary_flags", {}),
        }

    def setPerfs(self, report_path=None):
        """生成性能报告

        根据平台类型汇总数据并生成 HTML 报告。

        Args:
            report_path: 自定义报告输出路径
        """
        match self.platform:
            case Platform.Android:
                from perfcattle.core.adb_client import adb
                adb.shell(cmd="dumpsys battery reset", deviceId=self.deviceId)
                _flow = Network.getObject(
                    self.pkgName,
                    self.deviceId,
                    self.platform,
                    pid=self.pid,
                    processName=self.processName,
                )
                data = _flow.setAndroidNet()
                f.record_net("end", data[0], data[1])
                try:
                    from perfcattle.service.unity_capture import unity_capture
                    unity_capture.stop()
                    unity_capture.finalize()
                except Exception as e:
                    logger.warning("unity capture finalize failed: %s", e)
                scene = f.make_report(
                    app=self.pkgName,
                    devices=self.deviceId,
                    video=0,
                    platform=self.platform,
                    model="normal",
                )
                summary = f._setAndroidPerfs(scene)
                f.make_android_html(
                    scene=scene,
                    summary=self._android_report_summary(scene, summary),
                    report_path=report_path,
                )
            case Platform.iOS:
                try:
                    from perfcattle.service.unity_capture import unity_capture
                    unity_capture.stop()
                    unity_capture.finalize()
                except Exception as e:
                    logger.warning("unity capture finalize failed: %s", e)
                scene = f.make_report(
                    app=self.pkgName,
                    devices=self.deviceId,
                    video=0,
                    platform=self.platform,
                    model="normal",
                )
                summary = f._setiOSPerfs(scene)
                f.make_ios_html(
                    scene=scene,
                    summary=self._ios_report_summary(scene, summary),
                    report_path=report_path,
                )
            case _:
                raise Exception("platfrom is invalid")

    def collectAll(self, report_path=None):
        """并行采集所有性能数据并生成报告

        Args:
            report_path: 自定义报告输出路径
        """
        executor = None
        recording_started = False

        def cleanup():
            nonlocal executor, recording_started
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
                executor = None
            if recording_started:
                try:
                    Scrcpy.stop_record()
                except Exception:
                    logger.exception("Stop screen recording failed")
                recording_started = False
            self.clear_collector_sessions()

        try:
            f.clear_file()
            f.begin_session()
            if self.record:
                record_options = None
                if getattr(self, "duration", 0) > 0:
                    record_options = {
                        "screen_off_timeout": max(int(self.duration) + 300, 3600)
                    }
                if record_options:
                    recording_started = Scrcpy.start_record(
                        self.deviceId, record_options
                    ) == 0
                else:
                    recording_started = Scrcpy.start_record(self.deviceId) == 0
                if not recording_started:
                    logger.error("Start screen recording failed, continue collecting data")

            collectors = [
                self.collectCpu,
                self.collectMemory,
                self.collectMemoryDetail,
                self.collectBattery,
                self.collectFps,
                self.collectNetwork,
                self.collectFileIO,
                self.collectGpu,
                self.collectGpuStats,
                self.collectThermal,
            ]
            # Disk is a capacity snapshot, not a continuous time-series metric.
            # Include it in Android collection while keeping its snapshot_only
            # metadata separate from the sampled collectors.
            if getattr(self, "platform", None) == Platform.Android:
                collectors.append(self.collectDisk)
            collectors = tuple(collectors)
            executor = ThreadPoolExecutor(
                max_workers=len(collectors),
                thread_name_prefix="perfcattle-collector",
            )
            futures = [executor.submit(collector) for collector in collectors]
            wait(futures)
            for future in futures:
                future.result()
            cleanup()
            self.setPerfs(report_path=report_path)
        except KeyboardInterrupt:
            self.stop()
            cleanup()
            self.setPerfs(report_path=report_path)
        except Exception as e:
            self.stop()
            cleanup()
            logger.exception(e)
        finally:
            cleanup()
            logger.info("End of testing")
