# -*- encoding: utf-8 -*-
"""报告数据聚合模块

负责将各维度的性能日志数据汇总为摘要报告。
"""
import json
import math
import os
from perfcat.report.log_io import LogIO


class Aggregator:
    """性能数据聚合器，将日志数据汇总为结构化摘要"""

    def __init__(self, log_io: LogIO = None):
        """初始化聚合器

        Args:
            log_io: LogIO 实例，用于读取日志数据。如果为 None 则创建默认实例。
        """
        self.log_io = log_io or LogIO()

    @property
    def report_dir(self):
        return self.log_io.report_dir

    def _scene_file_path(self, scene, filename):
        return os.path.join(self.report_dir, scene, filename)

    @staticmethod
    def _format_rate(value):
        value = round(value, 2)
        if value == int(value):
            return str(int(value))
        return f"{value:.2f}".rstrip("0").rstrip(".")

    def _scene_metadata(self, scene):
        result = self.log_io.read_result(scene=scene)
        metadata = {
            "app": result.get("app"),
            "devices": result.get("devices"),
            "platform": result.get("platform"),
            "ctime": result.get("ctime"),
            "video": result.get("video", 0),
        }
        if result.get("platform") == "iOS":
            metadata.update(process=result.get("process", ""),
                            process_start=result.get("process_start", ""),
                            network_process=result.get("network_process", ""),
                            network_process_start=result.get("network_process_start", ""))
        return metadata

    def _values(self, scene, filename):
        return self.log_io.readLog(scene=scene, filename=filename)[1]

    @staticmethod
    def _average(values):
        if not values:
            return None
        return sum(values) / len(values)

    def _average_text(self, values, suffix):
        average = self._average(values)
        if average is None:
            return 0
        return f"{round(average, 2)}{suffix}"

    @staticmethod
    def _peak_text(values, suffix):
        if not values:
            return 0
        return f"{round(max(values), 2)}{suffix}"

    def _frame_stability_index(self, fps_values):
        if not fps_values:
            return 0

        average = sum(fps_values) / len(fps_values)
        if average <= 0:
            return 0

        variance = sum((value - average) ** 2 for value in fps_values) / len(
            fps_values
        )
        score = max(0, 100 - (math.sqrt(variance) / average * 100))
        return self._format_rate(score)

    def _mean_log_text(self, scene, filename, suffix, *, integer=False):
        values = self._values(scene, filename)
        if not values:
            return 0
        average = sum(values) / len(values)
        if integer:
            return f"{int(average)}{suffix}"
        return f"{round(average, 2)}{suffix}"

    @staticmethod
    def _sum_mb_text(values):
        return f"{round(float(sum(values) / 1024), 2)}MB"

    def _scene_file_exists(self, scene, filename):
        return os.path.exists(self._scene_file_path(scene, filename))

    def _read_json_file(self, scene, filename, default):
        if not self._scene_file_exists(scene, filename):
            return default
        with open(self._scene_file_path(scene, filename), "r", encoding="utf-8") as file:
            return json.loads(file.read() or "null") or default

    def _thermal_flag(self, scene):
        has_log = any(
            self._scene_file_exists(scene, filename)
            for _, filename in self.log_io.THERMAL_LOGS
        )
        has_snapshot = any(
            self._scene_file_exists(scene, filename)
            for filename in self.log_io.THERMAL_SNAPSHOT_FILES
        )
        return has_log or has_snapshot

    def _thermal_log_values(self, scene, group, index=None):
        filename = dict(self.log_io.THERMAL_LOGS).get(group)
        if not filename:
            return []
        if index is not None:
            name, ext = os.path.splitext(filename)
            filename = f"{name}{index}{ext}"
        return self._values(scene, filename)

    def _thermal_snapshot_values(self, scene, group):
        values = []
        for filename in self.log_io.THERMAL_SNAPSHOT_FILES:
            for sensor in self._read_json_file(scene, filename, []):
                if (sensor.get("name") or sensor.get("type")) != group:
                    continue
                value = self.log_io._thermal_celsius(sensor.get("temp"))
                if value is not None:
                    values.append(value)
        return values

    def _thermal_average_summary(self, scene, group, index=None):
        values = self._thermal_log_values(scene, group, index=index)
        if index is None and not values:
            values = self._thermal_snapshot_values(scene, group)
        average = self._average(values)
        if average is None:
            return 0, False
        return f"{self._format_rate(average)}°C", True

    def _disk_snapshot(self, scene, filename):
        if not self._scene_file_exists(scene, filename):
            return [], {}

        disk_list = []
        size_list = []
        used_list = []
        free_list = []
        seen_sizes = set()
        lines = self.log_io.open_file(self._scene_file_path(scene, filename), "r")
        for line in lines:
            if "Filesystem" in line or line.strip() == "":
                continue

            disk_value_list = line.split()
            if len(disk_value_list) < 6:
                continue
            disk_list.append(
                dict(
                    filesystem=disk_value_list[0],
                    blocks=disk_value_list[1],
                    used=disk_value_list[2],
                    available=disk_value_list[3],
                    use_percent=disk_value_list[4],
                    mounted=disk_value_list[5],
                )
            )
            # /data/user/0 和 /storage/emulated 指向同一块存储，避免重复求和
            size_key = (
                disk_value_list[1],
                disk_value_list[2],
                disk_value_list[3],
            )
            if size_key in seen_sizes:
                continue
            seen_sizes.add(size_key)
            size_list.append(int(disk_value_list[1]))
            used_list.append(int(disk_value_list[2]))
            free_list.append(int(disk_value_list[3]))

        return disk_list, {
            "sum_size": int(sum(size_list) / 1024 / 1024),
            "sum_used": int(sum(used_list) / 1024),
            "sum_free": int(sum(free_list) / 1024),
        }

    def _jank_per_minute(self, jank_data, jank_log_data):
        if not jank_data:
            return "0/min"

        duration_seconds = 0
        if len(jank_log_data) >= 2:
            duration_seconds = jank_log_data[-1]["x"] - jank_log_data[0]["x"]
        if duration_seconds <= 0:
            duration_seconds = len(jank_data)
        if duration_seconds <= 0:
            return "0/min"

        rate = sum(jank_data) * 60.0 / duration_seconds
        return f"{self._format_rate(rate)}/min"

    def _network_summary(self, scene):
        pre_net_path = self._scene_file_path(scene, "pre_net.json")
        end_net_path = self._scene_file_path(scene, "end_net.json")
        if os.path.exists(pre_net_path) and os.path.exists(end_net_path):
            try:
                with open(pre_net_path, encoding="utf-8") as f_pre:
                    json_pre = json.loads(f_pre.read())
                with open(end_net_path, encoding="utf-8") as f_end:
                    json_end = json.loads(f_end.read())
                send = json_end["send"] - json_pre["send"]
                recv = json_end["recv"] - json_pre["recv"]
                if send >= 0 and recv >= 0:
                    return send, recv, True
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass

        flow_send_data = self.log_io.readLog(scene=scene, filename="upflow.log")[1]
        flow_recv_data = self.log_io.readLog(scene=scene, filename="downflow.log")[1]
        return sum(flow_send_data), sum(flow_recv_data), bool(
            flow_send_data or flow_recv_data
        )

    def _network_summary_kb(self, scene):
        send, recv, _ = self._network_summary(scene)
        return send, recv

    def _setAndroidPerfs(self, scene):
        """汇总 Android 性能数据

        Args:
            scene: 场景目录名

        Returns:
            dict: 包含所有 Android 性能指标的字典
        """
        metadata = self._scene_metadata(scene)

        cpuAppData = self._values(scene, "cpu_app.log")
        cpuSystemData = self._values(scene, "cpu_sys.log")
        cpu_app_flag = len(cpuAppData) > 0
        cpu_system_flag = len(cpuSystemData) > 0
        cpuAppRate = (
            f"{round(sum(cpuAppData) / len(cpuAppData), 2)}%"
            if cpu_app_flag
            else 0
        )
        cpuSystemRate = (
            f"{round(sum(cpuSystemData) / len(cpuSystemData), 2)}%"
            if cpu_system_flag
            else 0
        )

        batteryLevelData = self._values(scene, "battery_level.log")
        batteryTemlData = self._values(scene, "battery_tem.log")
        battery_level_flag = len(batteryLevelData) > 0
        battery_temp_flag = len(batteryTemlData) > 0
        batteryLevel = f"{batteryLevelData[-1]}%" if battery_level_flag else 0
        batteryTeml = f"{batteryTemlData[-1]}°C" if battery_temp_flag else 0

        totalPassData = self._values(scene, "mem_total.log")
        swapPassData = self._values(scene, "mem_swap.log")

        totalPassAvg = self._average_text(totalPassData, "MB")
        totalPassPeak = self._peak_text(totalPassData, "MB")
        swapPassAvg = self._average_text(swapPassData, "MB")

        fpsData = self._values(scene, "fps.log")
        jankLogData, jankData = self.log_io.readLog(scene=scene, filename="jank.log")
        fps_flag = len(fpsData) > 0
        jank_flag = len(jankLogData) > 0
        if fps_flag:
            fpsAvg = f"{int(sum(fpsData) / len(fpsData))}HZ/s"
            frameStability = self._frame_stability_index(fpsData)
            jankAvg = self._jank_per_minute(jankData, jankLogData)
        else:
            fpsAvg, frameStability, jankAvg = 0, 0, 0

        send, recv, network_flag = self._network_summary(scene)
        flowSend = f"{round(float(send / 1024), 2)}MB"
        flowRecv = f"{round(float(recv / 1024), 2)}MB"

        fileReadData = self._values(scene, "file_read.log")
        fileWriteData = self._values(scene, "file_write.log")
        file_io_flag = len(fileReadData) > 0 or len(fileWriteData) > 0
        if file_io_flag:
            fileRead = f"{round(float(sum(fileReadData) / 1024), 2)}MB"
            fileWrite = f"{round(float(sum(fileWriteData) / 1024), 2)}MB"
        else:
            fileRead, fileWrite = 0, 0

        gpuData = self._values(scene, "gpu.log")
        gpu_flag = len(gpuData) > 0
        if gpu_flag:
            gpu = round(sum(gpuData) / len(gpuData), 2)
        else:
            gpu = 0

        mem_detail_flag = self._scene_file_exists(scene, "mem_java_heap.log")
        disk_flag = self._scene_file_exists(scene, "disk_free.log")
        thermal_flag = self._thermal_flag(scene)
        thermalCpuAvg, thermal_cpu_avg_flag = self._thermal_average_summary(
            scene, "CPU"
        )
        thermalGpuAvg, thermal_gpu_avg_flag = self._thermal_average_summary(
            scene, "GPU"
        )
        cpu_core_flag = self._scene_file_exists(scene, "cpu0.log")
        summary_flags = {
            "cpu_app": cpu_app_flag,
            "cpu_system": cpu_system_flag,
            "mem_total": len(totalPassData) > 0,
            "mem_peak": len(totalPassData) > 0,
            "mem_swap": len(swapPassData) > 0,
            "fps": fps_flag,
            "frame_stability": fps_flag,
            "jank": jank_flag,
            "battery_level": battery_level_flag,
            "battery_temp": battery_temp_flag,
            "network_send": network_flag,
            "network_recv": network_flag,
            "file_io": file_io_flag,
            "gpu": gpu_flag,
            "thermal": thermal_flag,
            "thermal_cpu_avg": thermal_cpu_avg_flag,
            "thermal_gpu_avg": thermal_gpu_avg_flag,
        }
        apm_dict = {
            **metadata,
            "cpuAppRate": cpuAppRate,
            "cpuSystemRate": cpuSystemRate,
            "totalPassAvg": totalPassAvg,
            "totalPassPeak": totalPassPeak,
            "swapPassAvg": swapPassAvg,
            "fps": fpsAvg,
            "frameStability": frameStability,
            "jank": jankAvg,
            "flow_send": flowSend,
            "flow_recv": flowRecv,
            "file_read": fileRead,
            "file_write": fileWrite,
            "batteryLevel": batteryLevel,
            "batteryTeml": batteryTeml,
            "mem_detail_flag": mem_detail_flag,
            "disk_flag": disk_flag,
            "file_io_flag": file_io_flag,
            "gpu": gpu,
            "thermal_flag": thermal_flag,
            "thermalCpuAvg": thermalCpuAvg,
            "thermalGpuAvg": thermalGpuAvg,
            "cpu_core_flag": cpu_core_flag,
            "summary_flags": summary_flags,
        }

        if thermal_flag:
            init_thermal_temp = self._read_json_file(
                scene, "init_thermal_temp.json", []
            )
            current_thermal_temp = self._read_json_file(
                scene, "current_thermal_temp.json", []
            )
            apm_dict["init_thermal_temp"] = init_thermal_temp
            apm_dict["current_thermal_temp"] = current_thermal_temp

        return apm_dict

    def _setiOSPerfs(self, scene):
        """汇总 iOS 性能数据

        Args:
            scene: 场景目录名

        Returns:
            dict: 包含所有 iOS 性能指标的字典
        """
        metadata = self._scene_metadata(scene)

        cpuAppData = self._values(scene, "cpu_app.log")
        cpuSystemData = self._values(scene, "cpu_sys.log")
        cpu_app_flag = len(cpuAppData) > 0
        cpu_system_flag = len(cpuSystemData) > 0
        cpuAppRate = (
            f"{round(sum(cpuAppData) / len(cpuAppData), 2)}%"
            if cpu_app_flag
            else 0
        )
        cpuSystemRate = (
            f"{round(sum(cpuSystemData) / len(cpuSystemData), 2)}%"
            if cpu_system_flag
            else 0
        )

        totalPassData = self._values(scene, "mem_total.log")
        if len(totalPassData) > 0:
            totalPassAvg = self._average_text(totalPassData, "MB")
            totalPassPeak = self._peak_text(totalPassData, "MB")
        else:
            totalPassAvg, totalPassPeak = 0, 0

        fpsData = self._values(scene, "fps.log")
        fps_flag = len(fpsData) > 0
        if fps_flag:
            fpsAvg = f"{int(sum(fpsData) / len(fpsData))}HZ/s"
            frameStability = self._frame_stability_index(fpsData)
        else:
            fpsAvg, frameStability = 0, 0

        flowSendData = self._values(scene, "upflow.log")
        flowRecvData = self._values(scene, "downflow.log")
        network_flag = bool(flowSendData or flowRecvData)
        if network_flag:
            flowSend = f"{round(float(sum(flowSendData) / 1024), 2)}MB"
            flowRecv = f"{round(float(sum(flowRecvData) / 1024), 2)}MB"
        else:
            flowSend, flowRecv = 0, 0
        fileRead, fileWrite = 0, 0

        batteryTemlData = self._values(scene, "battery_tem.log")
        batteryCurrentData = self._values(scene, "battery_current.log")
        batteryVoltageData = self._values(scene, "battery_voltage.log")
        batteryPowerData = self._values(scene, "battery_power.log")
        battery_temp_flag = len(batteryTemlData) > 0
        battery_current_flag = len(batteryCurrentData) > 0
        battery_voltage_flag = len(batteryVoltageData) > 0
        battery_power_flag = len(batteryPowerData) > 0
        batteryTeml = int(batteryTemlData[-1]) if battery_temp_flag else 0
        batteryCurrent = (
            int(sum(batteryCurrentData) / len(batteryCurrentData))
            if battery_current_flag
            else 0
        )
        batteryVoltage = (
            int(sum(batteryVoltageData) / len(batteryVoltageData))
            if battery_voltage_flag
            else 0
        )
        batteryPower = (
            int(sum(batteryPowerData) / len(batteryPowerData))
            if battery_power_flag
            else 0
        )

        gpuData = self._values(scene, "gpu.log")
        gpu_flag = len(gpuData) > 0
        if gpu_flag:
            gpu = round(sum(gpuData) / len(gpuData), 2)
        else:
            gpu = 0
        summary_flags = {
            "cpu_app": cpu_app_flag,
            "cpu_system": cpu_system_flag,
            "gpu": gpu_flag,
            "mem_total": len(totalPassData) > 0,
            "mem_peak": len(totalPassData) > 0,
            "fps": fps_flag,
            "frame_stability": fps_flag,
            "battery_temp": battery_temp_flag,
            "battery_current": battery_current_flag,
            "battery_voltage": battery_voltage_flag,
            "battery_power": battery_power_flag,
            "network_send": network_flag,
            "network_recv": network_flag,
        }
        return {
            **metadata,
            "cpuAppRate": cpuAppRate,
            "cpuSystemRate": cpuSystemRate,
            "totalPassAvg": totalPassAvg,
            "totalPassPeak": totalPassPeak,
            "nativePassAvg": 0,
            "dalvikPassAvg": 0,
            "fps": fpsAvg,
            "frameStability": frameStability,
            "jank": 0,
            "flow_send": flowSend,
            "flow_recv": flowRecv,
            "file_read": fileRead,
            "file_write": fileWrite,
            "batteryTeml": batteryTeml,
            "batteryCurrent": batteryCurrent,
            "batteryVoltage": batteryVoltage,
            "batteryPower": batteryPower,
            "gpu": gpu,
            "disk_flag": self._scene_file_exists(scene, "disk_free.log"),
            "file_io_flag": False,
            "summary_flags": summary_flags,
        }

    def _setpkPerfs(self, scene):
        """汇总 PK 对比模式的性能数据

        Args:
            scene: 场景目录名

        Returns:
            dict: 包含双应用对比性能指标的字典
        """
        networkData1 = self._values(scene, "network1.log")
        networkData2 = self._values(scene, "network2.log")
        cpuAppData1 = self._values(scene, "cpu_app1.log")
        cpuAppData2 = self._values(scene, "cpu_app2.log")
        memData1 = self._values(scene, "mem1.log")
        memData2 = self._values(scene, "mem2.log")
        fpsData1 = self._values(scene, "fps1.log")
        fpsData2 = self._values(scene, "fps2.log")
        fileReadData1 = self._values(scene, "file_read1.log")
        fileReadData2 = self._values(scene, "file_read2.log")
        fileWriteData1 = self._values(scene, "file_write1.log")
        fileWriteData2 = self._values(scene, "file_write2.log")
        thermal_flag = any(
            self._scene_file_exists(scene, f"thermal_{name.lower()}{index}.log")
            for name, _ in self.log_io.THERMAL_LOGS
            for index in (1, 2)
        )
        thermalCpuAvg1, thermal_cpu_avg_flag1 = self._thermal_average_summary(
            scene, "CPU", index=1
        )
        thermalCpuAvg2, thermal_cpu_avg_flag2 = self._thermal_average_summary(
            scene, "CPU", index=2
        )
        thermalGpuAvg1, thermal_gpu_avg_flag1 = self._thermal_average_summary(
            scene, "GPU", index=1
        )
        thermalGpuAvg2, thermal_gpu_avg_flag2 = self._thermal_average_summary(
            scene, "GPU", index=2
        )
        file_io_values = (
            fileReadData1,
            fileReadData2,
            fileWriteData1,
            fileWriteData2,
        )
        if any(file_io_values):
            fileRead1 = self._sum_mb_text(fileReadData1)
            fileRead2 = self._sum_mb_text(fileReadData2)
            fileWrite1 = self._sum_mb_text(fileWriteData1)
            fileWrite2 = self._sum_mb_text(fileWriteData2)
            file_io_flag = True
        else:
            fileRead1, fileRead2 = 0, 0
            fileWrite1, fileWrite2 = 0, 0
            file_io_flag = False

        return {
            "cpuAppRate1": self._average_text(cpuAppData1, "%"),
            "cpuAppRate2": self._average_text(cpuAppData2, "%"),
            "totalPassAvg1": self._average_text(memData1, "MB"),
            "totalPassAvg2": self._average_text(memData2, "MB"),
            "totalPassPeak1": self._peak_text(memData1, "MB"),
            "totalPassPeak2": self._peak_text(memData2, "MB"),
            "network1": self._sum_mb_text(networkData1),
            "network2": self._sum_mb_text(networkData2),
            "fileRead1": fileRead1,
            "fileRead2": fileRead2,
            "fileWrite1": fileWrite1,
            "fileWrite2": fileWrite2,
            "file_io_flag": file_io_flag,
            "thermal_flag": thermal_flag,
            "thermalCpuAvg1": thermalCpuAvg1,
            "thermalCpuAvg2": thermalCpuAvg2,
            "thermalGpuAvg1": thermalGpuAvg1,
            "thermalGpuAvg2": thermalGpuAvg2,
            "fpsAvg1": f"{int(sum(fpsData1) / len(fpsData1))}HZ/s"
            if fpsData1
            else 0,
            "fpsAvg2": f"{int(sum(fpsData2) / len(fpsData2))}HZ/s"
            if fpsData2
            else 0,
            "frameStability1": self._frame_stability_index(fpsData1),
            "frameStability2": self._frame_stability_index(fpsData2),
            "summary_flags": {
                "cpu_app1": len(cpuAppData1) > 0,
                "cpu_app2": len(cpuAppData2) > 0,
                "mem_total1": len(memData1) > 0,
                "mem_total2": len(memData2) > 0,
                "mem_peak1": len(memData1) > 0,
                "mem_peak2": len(memData2) > 0,
                "fps1": len(fpsData1) > 0,
                "fps2": len(fpsData2) > 0,
                "frame_stability1": len(fpsData1) > 0,
                "frame_stability2": len(fpsData2) > 0,
                "network1": len(networkData1) > 0,
                "network2": len(networkData2) > 0,
                "file_io1": bool(fileReadData1 or fileWriteData1),
                "file_io2": bool(fileReadData2 or fileWriteData2),
                "thermal": thermal_flag,
                "thermal_cpu_avg1": thermal_cpu_avg_flag1,
                "thermal_cpu_avg2": thermal_cpu_avg_flag2,
                "thermal_gpu_avg1": thermal_gpu_avg_flag1,
                "thermal_gpu_avg2": thermal_gpu_avg_flag2,
            },
        }

    def analysisDisk(self, scene):
        """分析磁盘使用数据

        Args:
            scene: 场景目录名

        Returns:
            tuple: (initail_disk_list, current_disk_list, sum_init_disk, sum_current_disk)
        """
        initail_disk_list, sum_init_disk = self._disk_snapshot(
            scene, "initail_disk.log"
        )
        current_disk_list, sum_current_disk = self._disk_snapshot(
            scene, "current_disk.log"
        )

        return initail_disk_list, current_disk_list, sum_init_disk, sum_current_disk
