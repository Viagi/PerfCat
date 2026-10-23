# -*- coding: utf-8 -*-
"""PK 对比模式采集器"""
import datetime
import os
import time

from perfcattle.core.platform import Platform
from perfcattle.collector.base import create_devices, create_report_store
from perfcattle.collector.cpu import CPU
from perfcattle.collector.file_io import FileIO
from perfcattle.collector.fps import FPS
from perfcattle.collector.memory import Memory
from perfcattle.collector.network import Network


class PKCollectorBase:
    def _get_devices(self):
        return create_devices()

    def _get_report_store(self):
        return create_report_store()

    def _targets(self):
        if len(self.pkgNameList) == 1:
            return (
                (self.pkgNameList[0], self.deviceId1),
                (self.pkgNameList[0], self.deviceId2),
            )
        return (
            (self.pkgNameList[0], self.deviceId1),
            (self.pkgNameList[1], self.deviceId2),
        )


class CPU_PK(PKCollectorBase):
    """CPU 对比采集器"""

    def __init__(self, pkgNameList: list, deviceId1, deviceId2):
        self.pkgNameList = pkgNameList
        self.deviceId1 = deviceId1
        self.deviceId2 = deviceId2

    def _get_cpu_rate(self, pkgName, deviceId):
        cpu = CPU.getObject(
            pkgName=pkgName,
            deviceId=deviceId,
            platform=Platform.Android,
        )
        app_cpu, _ = cpu.getCpuRate(noLog=True)
        return app_cpu

    def getAndroidCpuRate(self):
        """获取 Android 进程 CPU 使用率"""
        _f = self._get_report_store()
        targets = self._targets()
        appCpuRate1 = self._get_cpu_rate(*targets[0])
        appCpuRate2 = self._get_cpu_rate(*targets[1])
        apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
        _f.add_log(os.path.join(_f.report_dir, "cpu_app1.log"), apm_time, appCpuRate1)
        _f.add_log(os.path.join(_f.report_dir, "cpu_app2.log"), apm_time, appCpuRate2)
        return appCpuRate1, appCpuRate2


class MEM_PK(PKCollectorBase):
    """内存对比采集器"""

    def __init__(self, pkgNameList: list, deviceId1, deviceId2):
        self.pkgNameList = pkgNameList
        self.deviceId1 = deviceId1
        self.deviceId2 = deviceId2

    def getAndroidMemory(self, pkgName, deviceId):
        """获取 Android 内存，单位：MB"""
        memory = Memory.getObject(
            pkgName=pkgName,
            deviceId=deviceId,
            platform=Platform.Android,
        )
        totalPass, _ = memory.getProcessMemory(noLog=True)
        return totalPass

    def getProcessMemory(self):
        """获取应用内存"""
        _f = self._get_report_store()
        targets = self._targets()
        totalPass1 = self.getAndroidMemory(*targets[0])
        totalPass2 = self.getAndroidMemory(*targets[1])
        apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
        _f.add_log(os.path.join(_f.report_dir, "mem1.log"), apm_time, totalPass1)
        _f.add_log(os.path.join(_f.report_dir, "mem2.log"), apm_time, totalPass2)

        return totalPass1, totalPass2


class Flow_PK(PKCollectorBase):
    """网络流量对比采集器"""

    def __init__(self, pkgNameList: list, deviceId1, deviceId2):
        self.pkgNameList = pkgNameList
        self.deviceId1 = deviceId1
        self.deviceId2 = deviceId2

    def getAndroidNet(self, pkgName, deviceId):
        """获取 Android 上行和下行数据，单位：KB"""
        network = Network.getObject(
            pkgName=pkgName,
            deviceId=deviceId,
            platform=Platform.Android,
        )
        sendNum, recNum = network.getNetWorkData(noLog=True)
        if sendNum is None or recNum is None:
            return None
        network = round(float(sendNum + recNum), 2)
        return network

    def getNetWorkData(self):
        """获取上行和下行流量数据，单位：KB"""
        _f = self._get_report_store()
        targets = self._targets()
        network1 = self.getAndroidNet(*targets[0])
        network2 = self.getAndroidNet(*targets[1])
        apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
        if network1 is not None:
            _f.add_log(os.path.join(_f.report_dir, "network1.log"), apm_time, network1)
        if network2 is not None:
            _f.add_log(os.path.join(_f.report_dir, "network2.log"), apm_time, network2)
        return network1, network2


class FileIO_PK(PKCollectorBase):
    """文件 IO 对比采集器，分别返回两个目标的读写增量，单位 KB。"""

    def __init__(self, pkgNameList: list, deviceId1, deviceId2):
        self.pkgNameList = pkgNameList
        self.deviceId1 = deviceId1
        self.deviceId2 = deviceId2
        self.last_sample_times = []

    def _get_file_io(self, pkgName, deviceId):
        pid = None
        process_list = self._get_devices().getPid(pkgName=pkgName, deviceId=deviceId)
        if process_list:
            pid = process_list[0].split(":", 1)[0]
        return FileIO.getObject(
            pkgName=pkgName,
            deviceId=deviceId,
            platform=Platform.Android,
            pid=pid,
        )

    def setAndroidFileIO(self):
        """设置两个目标的文件 IO 基线。"""
        for pkgName, deviceId in self._targets():
            self._get_file_io(pkgName, deviceId).setAndroidFileIO()

    def getFileIOData(self):
        """获取两个目标的文件读写增量，单位 KB。"""
        _f = self._get_report_store()
        samples = []
        sample_times = []
        for pkgName, deviceId in self._targets():
            file_io = self._get_file_io(pkgName, deviceId)
            read_kb, write_kb = file_io.getFileIOData(noLog=True)
            samples.append((read_kb, write_kb))
            sample_times.append(getattr(file_io, "last_sample_time", None))

        self.last_sample_times = sample_times
        first_time = FileIO.format_log_time(sample_times[0])
        second_time = FileIO.format_log_time(sample_times[1])
        _f.add_log(
            os.path.join(_f.report_dir, "file_read1.log"), first_time, samples[0][0]
        )
        _f.add_log(
            os.path.join(_f.report_dir, "file_write1.log"), first_time, samples[0][1]
        )
        _f.add_log(
            os.path.join(_f.report_dir, "file_read2.log"), second_time, samples[1][0]
        )
        _f.add_log(
            os.path.join(_f.report_dir, "file_write2.log"), second_time, samples[1][1]
        )
        return samples


class FPS_PK(PKCollectorBase):
    """FPS 对比采集器"""

    def __init__(self, pkgNameList: list, deviceId1, deviceId2, surfaceview=True):
        self.pkgNameList = pkgNameList
        self.deviceId1 = deviceId1
        self.deviceId2 = deviceId2
        self.surfaceview = surfaceview

    def _get_fps_collector(self, pkgName, deviceId):
        return FPS.getObject(
            pkgName=pkgName,
            deviceId=deviceId,
            platform=Platform.Android,
            surfaceview=self.surfaceview,
        )

    def getAndroidFps(self, pkgName, deviceId):
        """获取 Android FPS，单位：HZ"""
        fps, _ = self._get_fps_collector(pkgName, deviceId).getFPS(noLog=True)
        return fps

    def getFPS(self):
        """获取 FPS"""
        _f = self._get_report_store()
        targets = self._targets()
        collectors = [self._get_fps_collector(*target) for target in targets]
        started = []
        for collector in collectors:
            _, started_now = collector._start_or_get_monitor()
            started.append(started_now and collector._last_fps is None)
        if any(started):
            time.sleep(1.2)
        fps1 = collectors[0].getFPS(noLog=True)[0]
        fps2 = collectors[1].getFPS(noLog=True)[0]
        apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
        _f.add_log(os.path.join(_f.report_dir, "fps1.log"), apm_time, fps1)
        _f.add_log(os.path.join(_f.report_dir, "fps2.log"), apm_time, fps2)
        return fps1, fps2
