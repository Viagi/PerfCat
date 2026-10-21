# -*- encoding: utf-8 -*-
"""报告日志读写模块

负责性能日志文件的读取、写入、解析等操作。
"""
import json
import os
from logzero import logger
from perfcat.core.platform import Platform


class LogIO:
    """日志文件读写操作"""

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

    def __init__(self, fileroot="."):
        self.fileroot = fileroot
        self.report_dir = self._get_report_dir()

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

    def readLog(self, scene, filename):
        """读取 APM 日志文件数据，x 轴为相对时间（从起始时间开始的秒数）

        Args:
            scene: 场景目录名
            filename: 日志文件名

        Returns:
            tuple: (log_data_list, target_data_list)
                - log_data_list: [{"x": 相对时间, "y": 值}, ...]
                - target_data_list: [值1, 值2, ...]
        """
        log_data_list = []
        target_data_list = []
        start_time = None

        filepath = os.path.join(self.report_dir, scene, filename)
        if not os.path.exists(filepath):
            return log_data_list, target_data_list

        for line in self.open_file(filepath, "r"):
            parts = line.split("=")
            if len(parts) < 2:
                continue

            time_str = parts[0].strip()
            value_str = parts[1].strip()

            try:
                if "." in time_str:
                    time_parts = time_str.split(".")
                    hms = time_parts[0].split(":")
                    microsec = int(time_parts[1][:6].ljust(6, "0"))
                else:
                    hms = time_str.split(":")
                    microsec = 0

                current_seconds = (
                    int(hms[0]) * 3600
                    + int(hms[1]) * 60
                    + int(hms[2])
                    + microsec / 1000000.0
                )

                if start_time is None:
                    start_time = current_seconds

                relative_time = round(current_seconds - start_time, 1)
            except (ValueError, IndexError):
                relative_time = len(log_data_list)

            try:
                value = int(value_str)
            except ValueError:
                value = float(value_str)

            log_data_list.append({"x": relative_time, "y": value})
            target_data_list.append(value)

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
