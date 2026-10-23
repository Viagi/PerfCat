import os
import shutil
from flask import Blueprint, request, send_file, jsonify
from logzero import logger
from perfcattle.core.platform import Platform
from perfcattle.device.manager import Devices
from perfcattle.report.manager import ReportStore
from perfcattle.tools.http_utils import Method
from perfcattle.collector.network import Network
from perfcattle.collector.battery import Battery
from perfcattle.collector.disk import Disk
from perfcattle.collector.file_io import FileIO
from perfcattle.collector.thermal import ThermalSensor
from perfcattle.collector.fps import FPS
from perfcattle.collector.cpu import CPU
from perfcattle.collector.memory import Memory
from perfcattle.collector.gpu import GPU
from perfcattle.collector.base import Target
from perfcattle.collector.ios_perf import IOSPerformance
from perfcattle.service.unity_capture import unity_capture
from perfcattle.tools.recording import Scrcpy

report_api = Blueprint("report_api", __name__)
d = Devices()
f = ReportStore()
method = Method()

COMPARE_LOG_FILES = {
    "cpu_system": "cpu_sys.log",
    "mem_swap": "mem_swap.log",
    "battery_temp": "battery_tem.log",
    "battery_current": "battery_current.log",
    "battery_voltage": "battery_voltage.log",
    "battery_power": "battery_power.log",
    "jank": "jank.log",
    "disk_used": "disk_used.log",
    "disk_free": "disk_free.log",
    "file_read": "file_read.log",
    "file_write": "file_write.log",
}


def _clear_collector_sessions():
    CPU.clear()
    Memory.clear()
    Network.clear()
    FPS.clear()
    Battery.clear()
    Disk.clear()
    FileIO.clear()
    ThermalSensor.clear()
    GPU.clear()


def _parse_process(process):
    if process and ":" in process:
        return process.split(":", 1)
    if process:
        return process, None
    return None, None


def _log_data_handlers(platform, scene):
    return {
        "cpu": lambda: f.getCpuLog(platform, scene),
        "mem": lambda: f.getMemLog(platform, scene),
        "mem_detail": lambda: f.getMemDetailLog(platform, scene),
        "thermal": lambda: f.getThermalLog(platform, scene),
        "battery": lambda: f.getBatteryLog(platform, scene),
        "flow": lambda: f.getFlowLog(platform, scene),
        "file_io": lambda: f.getFileIOLog(platform, scene),
        "fps": lambda: f.getFpsLog(platform, scene),
        "gpu": lambda: f.getGpuLog(platform, scene),
        "gpu_stats": lambda: f.getGpuStatsLog(platform, scene),
        "unity_stats": lambda: f.getUnityStatsLog(platform, scene),
        "unity_correlation": lambda: f.getUnityCorrelationLog(platform, scene),
        "disk": lambda: f.getDiskLog(platform, scene),
        "cpu_core": lambda: f.getCpuCoreLog(platform, scene),
    }


def _log_compare_handlers(platform, scene1, scene2):
    return {
        Target.CPU: lambda: f.getCpuLogCompare(platform, scene1, scene2),
        Target.Memory: lambda: f.getMemLogCompare(platform, scene1, scene2),
        "mem_detail": lambda: f.getMemDetailLogCompare(platform, scene1, scene2),
        "thermal": lambda: f.getThermalLogCompare(platform, scene1, scene2),
        Target.Battery: lambda: f.getBatteryLogCompare(platform, scene1, scene2),
        Target.FPS: lambda: f.getFpsLogCompare(platform, scene1, scene2),
        Target.GPU: lambda: f.getGpuLogCompare(platform, scene1, scene2),
        "cpu_core": lambda: f.getCpuCoreLogCompare(platform, scene1, scene2),
        "net_send": lambda: f.getFlowSendLogCompare(platform, scene1, scene2),
        "net_recv": lambda: f.getFlowRecvLogCompare(platform, scene1, scene2),
        Target.FileIO: lambda: f.getFileIOLogCompare(platform, scene1, scene2),
    }


def _report_metadata(scene):
    result = f.read_result(scene)
    metadata = {
        "app": result.get("app"),
        "platform": result.get("platform"),
        "devices": result.get("devices"),
        "ctime": result.get("ctime"),
    }
    if result.get("platform") == Platform.iOS:
        metadata.update(process=result.get("process", ""),
                        process_start=result.get("process_start", ""),
                        network_process=result.get("network_process", ""),
                        network_process_start=result.get("network_process_start", ""))
    return metadata


def _request_scenes():
    scenes = []
    for source in (request.form, request.args):
        scenes.extend(source.getlist("scenes"))
        scenes.extend(source.getlist("scenes[]"))
        scene = source.get("scene")
        if scene:
            scenes.append(scene)

    result = []
    seen = set()
    for scene in scenes:
        scene = str(scene).strip()
        if not scene or scene in seen:
            continue
        seen.add(scene)
        result.append(scene)
    return result


def _remove_report_scenes(scenes):
    report_dir = os.path.abspath(f.report_dir)
    removed = []
    failed = []

    for scene in scenes:
        target = os.path.abspath(os.path.join(report_dir, scene))
        try:
            if (
                target == report_dir
                or os.path.dirname(target) != report_dir
                or os.path.commonpath([report_dir, target]) != report_dir
            ):
                raise ValueError("invalid report scene")
            if os.path.isdir(target):
                shutil.rmtree(target)
            elif os.path.exists(target):
                raise ValueError("report path is not a directory")
            removed.append(scene)
        except Exception as exc:
            logger.warning("remove report failed: %s: %s", scene, exc)
            failed.append({"scene": scene, "msg": str(exc)})

    return removed, failed


@report_api.route("/apm/create/report", methods=["post", "get"])
def makeReport():
    """Create test report records"""
    platform = method._request(request, "platform")
    app = method._request(request, "app")
    model = method._request(request, "model")
    devices = method._request(request, "devices")
    wifi_switch = method._request(request, "wifi_switch")
    record_switch = method._request(request, "record_switch")
    thermal_switch = method._request(request, "thermal_switch")
    process = method._request(request, "process")
    cores = method._request(request, "cores")
    try:
        video = 0
        if platform == Platform.iOS:
            IOSPerformance.stop_requests()
        if platform == Platform.Android and model == "normal":
            deviceId = d.getIdbyDevice(devices, platform)
            battery_monitor = Battery.getObject(deviceId=deviceId)
            battery_monitor.recoverBattery()
            wifi = False if wifi_switch == "false" else True
            if process and platform == Platform.Android:
                pid, processName = _parse_process(process)
            else:
                pid, processName = None, None

            # set current natwork
            network = Network.getObject(
                pkgName=app,
                deviceId=deviceId,
                platform=platform,
                pid=pid,
                processName=processName,
            )
            data = network.setAndroidNet(wifi=wifi)
            if data[0] is not None and data[1] is not None:
                f.record_net("end", data[0], data[1])

            # set current disk
            disk = Disk.getObject(deviceId=deviceId)
            disk.setCurrentDisk()

            file_io = FileIO.getObject(
                pkgName=app,
                deviceId=deviceId,
                platform=platform,
                pid=pid,
                processName=processName,
            )
            file_io.stop()

            # set current thermal
            thermal_checked = False if thermal_switch == "false" else True
            if thermal_checked:
                thermal = ThermalSensor.getObject(deviceId=deviceId)
                thermal.setCurrentThermalTemp()

            record = False if record_switch == "false" else True
            if record:
                video = 1 if Scrcpy.stop_record() else 0
            _clear_collector_sessions()
        try:
            unity_capture.stop()
            unity_capture.finalize()
        except Exception as e:
            logger.warning("unity capture finalize failed: %s", e)
        f.make_report(
            app=app,
            devices=devices,
            video=video,
            platform=platform,
            model=model,
            cores=cores,
            **({"process": process or "",
                "process_start": method._request(request, "process_start") or "",
                "network_process": request.values.get("network_process", ""),
                "network_process_start": request.values.get("network_process_start", "")}
               if platform == Platform.iOS else {}),
        )
        result = {"status": 1}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e)}
    return result


@report_api.route("/apm/edit/report", methods=["post", "get"])
def editReport():
    """Edit test report records"""
    old_scene = method._request(request, "old_scene")
    new_scene = method._request(request, "new_scene")
    report_dir = os.path.join(os.getcwd(), "report")
    if old_scene == new_scene:
        result = {"status": 0, "msg": "scene not changed"}
    elif os.path.exists(os.path.join(report_dir, new_scene)):
        result = {"status": 0, "msg": "scene existed"}
    else:
        try:
            new_scene = new_scene.replace("/", "_").replace(" ", "").replace("&", "_")
            os.rename(
                os.path.join(report_dir, old_scene), os.path.join(report_dir, new_scene)
            )
            result = {"status": 1}
        except Exception as e:
            logger.exception(e)
            result = {"status": 0, "msg": str(e)}
    return result


@report_api.route("/apm/export/report", methods=["post", "get"])
def exportReport():
    platform = method._request(request, "platform")
    scene = method._request(request, "scene")
    try:
        path = f.export_excel(platform=platform, scene=scene)
        result = {"status": 1, "msg": "success", "path": path}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e)}
    return result


@report_api.route("/apm/export/html/android", methods=["post", "get"])
def exportAndroidHtml():
    scene = method._request(request, "scene")
    cpu_app = method._request(request, "cpu_app")
    cpu_sys = method._request(request, "cpu_sys")
    mem_total = method._request(request, "mem_total")
    mem_peak = method._request(request, "mem_peak")
    mem_swap = method._request(request, "mem_swap")
    fps = method._request(request, "fps")
    frame_stability = method._request(request, "frame_stability")
    jank = method._request(request, "jank")
    level = method._request(request, "level")
    temperature = method._request(request, "temperature")
    net_send = method._request(request, "net_send")
    net_recv = method._request(request, "net_recv")
    file_read = method._request(request, "file_read")
    file_write = method._request(request, "file_write")
    thermal_cpu_avg = method._request(request, "thermal_cpu_avg")
    thermal_gpu_avg = method._request(request, "thermal_gpu_avg")
    gpu = method._request(request, "gpu")
    try:
        fps_log = f.getFpsLog(Platform.Android, scene)
        gpu_stats = f.getGpuStatsLog(Platform.Android, scene)
        unity_stats = f.getUnityStatsLog(Platform.Android, scene)
        unity_correlation = f.getUnityCorrelationLog(Platform.Android, scene)
        summary_dict = {
            **_report_metadata(scene),
            "cpu_app": cpu_app,
            "cpu_sys": cpu_sys,
            "mem_total": mem_total,
            "mem_peak": mem_peak,
            "mem_swap": mem_swap,
            "fps": fps,
            "frame_stability": frame_stability,
            "jank": jank,
            "level": level,
            "tem": temperature,
            "net_send": net_send,
            "net_recv": net_recv,
            "file_read": file_read or 0,
            "file_write": file_write or 0,
            "thermal_cpu_avg": thermal_cpu_avg or 0,
            "thermal_gpu_avg": thermal_gpu_avg or 0,
            "gpu": gpu,
            "cpu_charts": f.getCpuLog(Platform.Android, scene),
            "mem_charts": f.getMemLog(Platform.Android, scene),
            "mem_detail_charts": f.getMemDetailLog(Platform.Android, scene),
            "thermal_charts": f.getThermalLog(Platform.Android, scene),
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
        }
        path = f.make_android_html(scene, summary_dict)
        result = {"status": 1, "msg": "success", "path": path}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e)}
    return result


@report_api.route("/apm/export/html/ios", methods=["post", "get"])
def exportiOSHtml():
    scene = method._request(request, "scene")
    cpu_app = method._request(request, "cpu_app")
    cpu_sys = method._request(request, "cpu_sys")
    mem_total = method._request(request, "mem_total")
    mem_peak = method._request(request, "mem_peak")
    gpu = method._request(request, "gpu")
    fps = method._request(request, "fps")
    frame_stability = method._request(request, "frame_stability")
    temperature = method._request(request, "temperature")
    current = method._request(request, "current")
    voltage = method._request(request, "voltage")
    power = method._request(request, "power")
    net_send = method._request(request, "net_send")
    net_recv = method._request(request, "net_recv")
    try:
        summary_dict = {
            **_report_metadata(scene),
            "cpu_app": cpu_app,
            "cpu_sys": cpu_sys,
            "mem_total": mem_total,
            "mem_peak": mem_peak,
            "gpu": gpu,
            "fps": fps,
            "frame_stability": frame_stability,
            "tem": temperature,
            "current": current,
            "voltage": voltage,
            "power": power,
            "net_send": net_send,
            "net_recv": net_recv,
            "cpu_charts": f.getCpuLog(Platform.iOS, scene),
            "mem_charts": f.getMemLog(Platform.iOS, scene),
            "net_charts": f.getFlowLog(Platform.iOS, scene),
            "battery_charts": f.getBatteryLog(Platform.iOS, scene),
            "fps_charts": f.getFpsLog(Platform.iOS, scene),
            "gpu_charts": f.getGpuLog(Platform.iOS, scene),
        }
        path = f.make_ios_html(scene, summary_dict)
        result = {"status": 1, "msg": "success", "path": path}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e)}
    return result


@report_api.route("/apm/log", methods=["post", "get"])
def getLogData():
    """Get apm detailed data"""
    scene = method._request(request, "scene")
    target = method._request(request, "target")
    platform = method._request(request, "platform")
    try:
        handler = _log_data_handlers(platform, scene).get(target)
        result = handler() if handler else {"status": 0, "msg": "no target found"}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e)}
    return result


@report_api.route("/apm/log/compare", methods=["post", "get"])
def getLogCompareData():
    """Get apm detailed data"""
    scene1 = method._request(request, "scene1")
    scene2 = method._request(request, "scene2")
    target = method._request(request, "target")
    platform = method._request(request, "platform")
    try:
        if target in COMPARE_LOG_FILES:
            result = f.getLogCompare(scene1, scene2, COMPARE_LOG_FILES[target])
        else:
            handler = _log_compare_handlers(platform, scene1, scene2).get(target)
            result = handler() if handler else {"status": 0, "msg": "no target found"}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e)}
    return result


@report_api.route("/apm/log/pk", methods=["post", "get"])
def getpkLogData():
    """Get apm detailed data"""
    scene = method._request(request, "scene")
    target1 = method._request(request, "target1")
    target2 = method._request(request, "target2")
    try:
        first = f.readLog(scene=scene, filename=f"{target1}.log")[0]
        second = f.readLog(scene=scene, filename=f"{target2}.log")[0]
        result = {"status": 1, "first": first, "second": second}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e)}
    return result


@report_api.route("/apm/remove/report", methods=["post", "get"])
def removeReport():
    """Remove test report record"""
    try:
        scenes = _request_scenes()
        if not scenes:
            raise ValueError("scene is required")
        removed, failed = _remove_report_scenes(scenes)
        result = {
            "status": 0 if failed else 1,
            "removed": removed,
            "failed": failed,
        }
        if failed:
            result["msg"] = "; ".join(
                f"{item['scene']}: {item['msg']}" for item in failed
            )
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e)}
    return result


@report_api.route("/apm/video/<scene>", methods=["GET"])
def get_video(scene):
    """Stream video file for analysis page"""
    video_path = os.path.join(f.report_dir, scene, "record.mp4")
    if not os.path.exists(video_path):
        return jsonify({"error": "Video not found"}), 404
    return send_file(video_path, mimetype="video/mp4", conditional=True)
