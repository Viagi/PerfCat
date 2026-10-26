import datetime
import json
import os
import requests
from flask import Blueprint, request, g
from logzero import logger
from perfcattle import __version__
from perfcattle.core.platform import Platform
from perfcattle.core.metrics import plottable_value
from perfcattle.core.selection_cache import ApmSelectionCache
from perfcattle.core.settings_cache import ApmSettingsCache
from perfcattle.device.manager import Devices
from perfcattle.report.manager import ReportStore
from perfcattle.tools.http_utils import Method
from perfcattle.collector.cpu import CPU
from perfcattle.collector.memory import Memory
from perfcattle.collector.network import Network
from perfcattle.collector.fps import FPS
from perfcattle.collector.battery import Battery
from perfcattle.collector.gpu import GPU
from perfcattle.collector.gpu_stats import create_gpu_stats, clear_gpu_stats
from perfcattle.collector.energy import Energy
from perfcattle.collector.disk import Disk
from perfcattle.collector.file_io import FileIO
from perfcattle.collector.thermal import ThermalSensor
from perfcattle.collector.base import Target
from perfcattle.collector.ios_perf import IOSPerformance, IOSRecoveryExhausted
from perfcattle.collector.pk import CPU_PK, MEM_PK, Flow_PK, FileIO_PK, FPS_PK

apm_api = Blueprint("apm_api", __name__)
_MEMORY_DETAIL_KEYS = (
    "java_heap", "native_heap", "code_pss", "stack_pss",
    "graphics_pss", "private_pss", "system_pss",
)

d = Devices()
f = ReportStore()
method = Method()


def _ios_sampling_error(exc):
    return {"status": 0, "msg": str(exc), "retryable": not isinstance(exc, IOSRecoveryExhausted)}


@apm_api.before_request
def _begin_ios_sample():
    if request.values.get("platform") == Platform.iOS and request.path in (
        "/apm/cpu", "/apm/mem", "/apm/fps", "/apm/gpu", "/apm/network",
        "/apm/battery", "/apm/disk", "/apm/energy",
    ):
        try:
            IOSPerformance.begin_request(
                request.values.get("process", ""), request.values.get("process_start", ""),
                request.values.get("network_process", ""), request.values.get("network_process_start", ""),
            )
            g.ios_sample_active = True
        except RuntimeError as exc:
            return {"status": 0, "msg": str(exc), "retryable": False}


@apm_api.teardown_request
def _end_ios_sample(error):
    if g.pop("ios_sample_active", False):
        IOSPerformance.end_request()


def _clear_collector_sessions():
    CPU.clear()
    Memory.clear()
    Network.clear()
    FPS.clear()
    Battery.clear()
    GPU.clear()
    Disk.clear()
    FileIO.clear()
    ThermalSensor.clear()
    clear_gpu_stats()


def _parse_process(process):
    if process and ":" in process:
        return process.split(":", 1)
    if process:
        return process, None
    return None, None


def _request_optional(name):
    if name in request.form:
        return request.form[name]
    if name in request.args:
        return request.args[name]
    return None


def _request_bool(name, default=False):
    value = _request_optional(name)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _pk_context(model, pkgname, device):
    if model not in ("2-devices", "2-app"):
        return None

    pkg_names = [pkgname] if model == "2-devices" else pkgname.split(",")
    devices = device.split(",")
    device_id1 = d.getIdbyDevice(devices[0], Platform.Android)
    device_id2 = d.getIdbyDevice(devices[1], Platform.Android)
    return pkg_names, device_id1, device_id2


def _android_process(platform, process):
    if process and platform == Platform.Android:
        return _parse_process(process)
    return None, None


def _target_collector_kwargs(pkgname, device_id, platform, process):
    pid, process_name = _android_process(platform, process)
    return {
        "pkgName": pkgname,
        "deviceId": device_id,
        "platform": platform,
        "pid": pid,
        "processName": process_name,
    }


@apm_api.route("/apm/settings", methods=["post", "get"])
def setSettings():
    """Persist APM settings to local file."""
    values = request.form if request.method == "POST" else request.args
    if not values:
        return {"status": 1, "settings": ApmSettingsCache.load()}
    return {"status": 1, "settings": ApmSettingsCache.save(values)}


@apm_api.route("/apm/selection", methods=["post", "get"])
def apmSelection():
    """Persist APM target selection to local file."""
    try:
        if request.method == "GET":
            return {"status": 1, "cache": ApmSelectionCache.load_all()}

        platform = request.form.get("platform") or request.args.get("platform")
        device = request.form.get("device") or request.args.get("device")
        package_name = _request_optional("packageName")
        process_name = _request_optional("processName")
        if not platform or not device:
            return {"status": 1, "cache": ApmSelectionCache.load_all()}
        if package_name is not None and not str(package_name).strip():
            return {"status": 1, "cache": ApmSelectionCache.load_all()}
        cache = ApmSelectionCache.save_selection(
            platform=platform,
            device=device,
            package_name=package_name,
            process_name=process_name,
            **({"ios_targets": json.loads(request.values["iosTargets"])}
               if platform == Platform.iOS and "iosTargets" in request.values else {}),
        )
        return {"status": 1, "cache": cache}
    except Exception as e:
        logger.exception(e)
        return {"status": 0, "msg": str(e)}


@apm_api.route("/perfcattle/version", methods=["post", "get"])
def version():
    try:
        pypi = json.loads(
            requests.get(url="https://pypi.org/pypi/perfcattle/json", timeout=3).text
        )
        latest = (pypi.get("info") or {}).get("version")
        if not latest:
            # Package absent from PyPI or an unexpected payload: not an error.
            return {"status": 0, "msg": "version unavailable"}
        result = {
            "status": 1,
            "lastest_version": latest,
            "current_version": __version__,
        }
    except Exception as e:
        logger.debug(e)
        result = {"status": 0, "msg": str(e)}
    return result


@apm_api.route("/apm/initialize", methods=["post", "get"])
def initialize():
    """initialize apm env"""
    try:
        IOSPerformance.stop_requests()
        _clear_collector_sessions()
        f.clear_file()
        f.begin_session()
        IOSPerformance.resume_requests()
        result = {"status": 1, "msg": "initialize env success"}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e)}
    return result


@apm_api.route("/apm/cpu", methods=["post", "get"])
def getCpuRate():
    """get process cpu rate"""
    model = method._request(request, "model")
    platform = method._request(request, "platform")
    pkgname = method._request(request, "pkgname")
    device = method._request(request, "device")
    try:
        pk_context = _pk_context(model, pkgname, device)
        if pk_context:
            pkgNameList, deviceId1, deviceId2 = pk_context
            cpu = CPU_PK(
                pkgNameList=pkgNameList, deviceId1=deviceId1, deviceId2=deviceId2
            )
            first, second = cpu.getAndroidCpuRate()
            result = {"status": 1, "first": first, "second": second}
        else:
            process = method._request(request, "process")
            deviceId = d.getIdbyDevice(device, platform)
            cpu = CPU.getObject(
                **_target_collector_kwargs(pkgname, deviceId, platform, process)
            )
            appCpuRate, systemCpuRate = cpu.getCpuRate()
            result = {
                "status": 1,
                "appCpuRate": appCpuRate,
                "systemCpuRate": systemCpuRate,
            }
    except Exception as e:
        logger.error("get cpu failed")
        logger.exception(e)
        if platform == Platform.iOS:
            return _ios_sampling_error(e)
        result = {
            "status": 1,
            "appCpuRate": 0,
            "systemCpuRate": 0,
            "first": 0,
            "second": 0,
        }
    return result


@apm_api.route("/apm/corecpu", methods=["post", "get"])
def getCoreCpuRate():
    """get process cpu core rate"""
    platform = method._request(request, "platform")
    pkgname = method._request(request, "pkgname")
    device = method._request(request, "device")
    cores = method._request(request, "cores")
    process = method._request(request, "process")
    try:
        cores = int(cores)
        deviceId = d.getIdbyDevice(device, platform)
        corecpu = CPU.getObject(
            **_target_collector_kwargs(pkgname, deviceId, platform, process)
        )
        coreCpuRate = corecpu.getCoreCpuRate(cores)
        result = {"status": 1, "coreCpuRate": coreCpuRate}
    except Exception as e:
        logger.error("get core cpu failed")
        logger.exception(e)
        coreCpuRate = list()
        while cores > 0:
            coreCpuRate.append(0)
            cores = cores - 1
        result = {"status": 1, "coreCpuRate": coreCpuRate}
    return result


@apm_api.route("/apm/mem", methods=["post", "get"])
def getMemory():
    """get memery data"""
    model = method._request(request, "model")
    platform = method._request(request, "platform")
    pkgname = method._request(request, "pkgname")
    device = method._request(request, "device")
    try:
        pk_context = _pk_context(model, pkgname, device)
        if pk_context:
            pkgNameList, deviceId1, deviceId2 = pk_context
            mem = MEM_PK(
                pkgNameList=pkgNameList, deviceId1=deviceId1, deviceId2=deviceId2
            )
            first, second = mem.getProcessMemory()
            result = {"status": 1, "first": first, "second": second}
        else:
            process = method._request(request, "process")
            deviceId = d.getIdbyDevice(device, platform)
            mem = Memory.getObject(
                **_target_collector_kwargs(pkgname, deviceId, platform, process)
            )
            totalPass, swapPass = mem.getProcessMemory()
            result = {"status": 1, "totalPass": totalPass, "swapPass": swapPass}
    except Exception as e:
        logger.error("get memory data failed")
        logger.exception(e)
        if platform == Platform.iOS:
            return _ios_sampling_error(e)
        result = {
            "status": 1,
            "totalPass": 0,
            "nativePass": 0,
            "dalvikPass": 0,
            "first": 0,
            "second": 0,
        }
    return result


@apm_api.route("/apm/mem/detail", methods=["post", "get"])
def getMemoryDetail():
    """get memery detail data"""
    platform = method._request(request, "platform")
    pkgname = method._request(request, "pkgname")
    device = method._request(request, "device")
    process = method._request(request, "process")
    memory_detail = {}
    try:
        deviceId = d.getIdbyDevice(device, platform)
        pid, processName = _parse_process(process)
        memory = Memory.getObject(
            pkgName=pkgname,
            deviceId=deviceId,
            platform=platform,
            pid=pid,
            processName=processName,
        )
        memory_detail = memory.getAndroidMemoryDetail()
        result = {
            "status": 1,
            "available": memory_detail is not None,
            "memory_detail": memory_detail or {
                key: None for key in _MEMORY_DETAIL_KEYS
            },
        }
    except Exception as e:
        logger.error("get memory detail data failed")
        logger.exception(e)
        result = {
            "status": 1,
            "available": False,
            "memory_detail": {key: None for key in _MEMORY_DETAIL_KEYS},
            "msg": str(e),
        }
    return result


@apm_api.route("/apm/set/network", methods=["post", "get"])
def setNetWorkData():
    """set network data"""
    platform = method._request(request, "platform")
    pkgname = method._request(request, "pkgname")
    device = method._request(request, "device")
    wifi_switch = method._request(request, "wifi_switch")
    type = method._request(request, "type")
    process = method._request(request, "process")
    try:
        wifi = False if wifi_switch == "false" else True
        deviceId = d.getIdbyDevice(device, platform)
        network = Network.getObject(
            **_target_collector_kwargs(pkgname, deviceId, platform, process)
        )
        data = network.setAndroidNet(wifi=wifi)
        if data[0] is not None and data[1] is not None:
            f.record_net(type, data[0], data[1])
        result = {"status": 1, "msg": "set network data success"}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": "set network data failed"}
    return result


@apm_api.route("/apm/network", methods=["post", "get"])
def getNetWorkData():
    """get network data"""
    model = method._request(request, "model")
    platform = method._request(request, "platform")
    pkgname = method._request(request, "pkgname")
    device = method._request(request, "device")
    wifi_switch = method._request(request, "wifi_switch")
    try:
        wifi = False if wifi_switch == "false" else True
        pk_context = _pk_context(model, pkgname, device)
        if pk_context:
            pkgNameList, deviceId1, deviceId2 = pk_context
            network = Flow_PK(
                pkgNameList=pkgNameList, deviceId1=deviceId1, deviceId2=deviceId2
            )
            first, second = network.getNetWorkData()
            result = {"status": 1, "first": first, "second": second}
        else:
            process = method._request(request, "process")
            deviceId = d.getIdbyDevice(device, platform)
            network = Network.getObject(
                **_target_collector_kwargs(pkgname, deviceId, platform, process)
            )
            data = network.getNetWorkData(wifi=wifi, noLog=False)
            result = {
                "status": 1,
                "upflow": data[0],
                "downflow": data[1],
                "source": getattr(network, "last_source", None),
                "scope": getattr(network, "last_scope", None),
                "sample_time": getattr(network, "last_sample_time", None),
                "fallback_reason": getattr(network, "fallback_reason", None),
            }
    except Exception as e:
        logger.error("get network data failed")
        logger.exception(e)
        if platform == Platform.iOS:
            return _ios_sampling_error(e)
        result = {
            "status": 1,
            "upflow": None,
            "downflow": None,
            "first": None,
            "second": None,
            "source": None,
            "scope": None,
            "sample_time": None,
            "fallback_reason": str(e),
        }
    return result


@apm_api.route("/apm/set/file_io", methods=["post", "get"])
def setFileIOData():
    """set file io baseline"""
    model = _request_optional("model") or "normal"
    platform = method._request(request, "platform")
    pkgname = method._request(request, "pkgname")
    device = method._request(request, "device")
    process = _request_optional("process") or ""
    try:
        pk_context = _pk_context(model, pkgname, device)
        if pk_context:
            pkgNameList, deviceId1, deviceId2 = pk_context
            file_io = FileIO_PK(pkgNameList, deviceId1, deviceId2)
            file_io.setAndroidFileIO()
        else:
            deviceId = d.getIdbyDevice(device, platform)
            file_io = FileIO.getObject(
                **_target_collector_kwargs(pkgname, deviceId, platform, process)
            )
            file_io.setAndroidFileIO()
        result = {
            "status": 1,
            "msg": "set file io data success",
            "source": getattr(file_io, "last_source", None),
            "scope": getattr(file_io, "last_scope", None),
        }
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": "set file io data failed"}
    return result


@apm_api.route("/apm/file_io", methods=["post", "get"])
def getFileIOData():
    """get process file read/write delta"""
    model = _request_optional("model") or "normal"
    platform = method._request(request, "platform")
    pkgname = method._request(request, "pkgname")
    device = method._request(request, "device")
    process = _request_optional("process") or ""
    try:
        pk_context = _pk_context(model, pkgname, device)
        if pk_context:
            pkgNameList, deviceId1, deviceId2 = pk_context
            file_io = FileIO_PK(pkgNameList, deviceId1, deviceId2)
            first, second = file_io.getFileIOData()
            result = {
                "status": 1,
                "first_read": first[0],
                "first_write": first[1],
                "second_read": second[0],
                "second_write": second[1],
                "first_sample_time": file_io.last_sample_times[0],
                "second_sample_time": file_io.last_sample_times[1],
            }
        else:
            deviceId = d.getIdbyDevice(device, platform)
            file_io = FileIO.getObject(
                **_target_collector_kwargs(pkgname, deviceId, platform, process)
            )
            data = file_io.getFileIOData(noLog=False)
            result = {
                "status": 1,
                "read": data[0],
                "write": data[1],
                "sample_time": file_io.last_sample_time,
                "source": getattr(file_io, "last_source", None),
                "scope": getattr(file_io, "last_scope", None),
                "fallback_reason": getattr(file_io, "fallback_reason", None),
            }
    except Exception as e:
        logger.error("get file io data failed")
        logger.exception(e)
        result = {
            "status": 1,
            "read": None,
            "write": None,
            "first_read": None,
            "first_write": None,
            "second_read": None,
            "second_write": None,
            "sample_time": None,
            "first_sample_time": None,
            "second_sample_time": None,
            "source": None,
            "scope": None,
            "fallback_reason": str(e),
        }
    return result


@apm_api.route("/apm/fps", methods=["post", "get"])
def getFps():
    """get fps data"""
    model = method._request(request, "model")
    platform = method._request(request, "platform")
    pkgname = method._request(request, "pkgname")
    device = method._request(request, "device")
    surv = method._request(request, "surv")
    try:
        surfaceview = False if surv == "false" else True
        pk_context = _pk_context(model, pkgname, device)
        if pk_context:
            pkgNameList, deviceId1, deviceId2 = pk_context
            fps = FPS_PK(
                pkgNameList=pkgNameList,
                deviceId1=deviceId1,
                deviceId2=deviceId2,
                surfaceview=surfaceview,
            )
            first, second = fps.getFPS()
            result = {"status": 1, "first": first, "second": second}
        else:
            deviceId = d.getIdbyDevice(device, platform)
            fps_monitor = FPS.getObject(
                pkgName=pkgname,
                deviceId=deviceId,
                surfaceview=surfaceview,
                platform=platform,
            )
            fps, jank = fps_monitor.getFPS()
            result = {"status": 1, "fps": fps, "jank": jank}
    except Exception as e:
        logger.error("get fps failed")
        logger.exception(e)
        if platform == Platform.iOS:
            return _ios_sampling_error(e)
        result = {"status": 1, "fps": 0, "jank": 0, "first": 0, "second": 0}
    return result


@apm_api.route("/apm/battery", methods=["post", "get"])
def getBattery():
    """get Battery data"""
    platform = method._request(request, "platform")
    device = method._request(request, "device")
    try:
        deviceId = d.getIdbyDevice(device, platform)
        battery_monitor = Battery.getObject(deviceId=deviceId, platform=platform)
        final = battery_monitor.getBattery()
        if platform == Platform.Android:
            result = {"status": 1, "level": final[0], "temperature": final[1]}
        else:
            result = {
                "status": 1,
                "temperature": final[0],
                "current": final[1],
                "voltage": final[2],
                "power": final[3],
            }
    except Exception as e:
        logger.exception(e)
        result = {
            "status": 1,
            "level": 0,
            "temperature": 0,
            "current": 0,
            "voltage": 0,
            "power": 0,
        }
    return result


@apm_api.route("/apm/gpu", methods=["post", "get"])
def getGpu():
    """get gpu data"""
    pkgname = method._request(request, "pkgname")
    device = method._request(request, "device")
    platform = method._request(request, "platform")
    try:
        deviceId = d.getIdbyDevice(device, platform)
        gpu = GPU.getObject(pkgName=pkgname, deviceId=deviceId, platform=platform)
        value = gpu.getGPU()
        result = {"status": 1, "gpu": value}
    except Exception as e:
        logger.exception(e)
        if platform == Platform.iOS:
            return _ios_sampling_error(e)
        # Do not fabricate 0%: a null point leaves a gap in the curve while
        # still letting the page keep polling.
        result = {"status": 1, "gpu": None, "msg": str(e)}
    return result


@apm_api.route("/apm/gpu/stats", methods=["post", "get"])
def getGpuStats():
    """get extended gpu data: clock, bus, temperature, memory, bandwidth"""
    pkgname = method._request(request, "pkgname")
    device = method._request(request, "device")
    platform = method._request(request, "platform")
    try:
        if platform != Platform.Android:
            result = {"status": 1, "data": {}, "msg": "not support ios"}
        else:
            deviceId = d.getIdbyDevice(device, platform)
            gpu = create_gpu_stats(
                deviceId=deviceId, pkgName=pkgname, pid=None, platform=platform
            )
            data = gpu.sample()
            if data:
                apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
                f.add_json_log(
                    os.path.join(f.report_dir, "gpu_stats.log"), apm_time, data
                )
            # The chart plots numbers: unwrap the structured samples (the log
            # keeps them) and drop non-ok ones so a gap replaces a fake zero.
            live = {}
            for key, sample in data.items():
                value = plottable_value(sample)
                if value is not None:
                    live[key] = value
            result = {"status": 1, "data": live}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e), "data": {}}
    return result


@apm_api.route("/apm/energy", methods=["post", "get"])
def getEnergy():
    """get energy data"""
    pkgname = method._request(request, "pkgname")
    device = method._request(request, "device")
    platform = method._request(request, "platform")
    try:
        deviceId = d.getIdbyDevice(device, platform)
        enery = Energy(deviceId=deviceId, packageName=pkgname)
        value = enery.getEnergy()
        result = {"status": 1, "value": value}
    except (RuntimeError, TimeoutError) as exc:
        return _ios_sampling_error(exc)
    except Exception as e:
        logger.exception(e)
        value = {
            "energy.overhead": 0,
            "energy.version": 0,
            "energy.gpu.cost": 0,
            "energy.cpu.cost": 0,
            "energy.appstate.cost": 0,
            "energy.thermalstate.cost": 0,
            "energy.networking.cost": 0,
            "energy.cost": 0,
            "energy.display.cost": 0,
            "energy.location.cost": 0,
        }
        result = {"status": 1, "value": value}
    return result


@apm_api.route("/apm/disk", methods=["post", "get"])
def getDisk():
    """get disk data"""
    device = method._request(request, "device")
    platform = method._request(request, "platform")
    try:
        deviceId = d.getIdbyDevice(device, platform)
        disk = Disk.getObject(deviceId=deviceId, platform=platform)
        value = disk.getDisk()
        result = {
            "status": 1,
            "available": True,
            "used": value["used"],
            "free": value["free"],
        }
    except Exception as e:
        logger.exception(e)
        result = {"status": 1, "available": False, "used": None, "free": None,
                  "msg": str(e)}
    return result


@apm_api.route("/apm/set/disk", methods=["post", "get"])
def setDiskData():
    """set disk data"""
    platform = method._request(request, "platform")
    device = method._request(request, "device")
    try:
        deviceId = d.getIdbyDevice(device, platform)
        disk = Disk.getObject(deviceId=deviceId, platform=platform)
        disk.setInitialDisk()
        result = {"status": 1, "msg": "set disk data success"}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": "set disk data failed"}
    return result


@apm_api.route("/apm/set/thermal", methods=["post", "get"])
def setThermalData():
    """set thermal data"""
    platform = method._request(request, "platform")
    device = method._request(request, "device")
    try:
        deviceId = d.getIdbyDevice(device, platform)
        thermal = ThermalSensor.getObject(deviceId=deviceId, platform=platform)
        thermal.setInitalThermalTemp()
        result = {"status": 1, "msg": "set thermal data success"}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": "set thermal data failed"}
    return result


@apm_api.route("/apm/thermal", methods=["post", "get"])
def getThermal():
    """get thermal sensor data"""
    platform = method._request(request, "platform")
    device = method._request(request, "device")
    no_log = _request_bool("noLog", True)
    try:
        deviceId = d.getIdbyDevice(device, platform)
        thermal = ThermalSensor.getObject(deviceId=deviceId, platform=platform)
        sensors = thermal.getThermalTemp(noLog=no_log) or []
        result = {
            "status": 1,
            "available": bool(sensors),
            "sensors": sensors,
        }
    except Exception as e:
        logger.exception(e)
        result = {"status": 1, "available": False, "sensors": [], "msg": str(e)}
    return result


@apm_api.route("/apm/collect", methods=["post", "get"])
def apmCollect():
    """apm common api"""
    platform = method._request(request, "platform")
    deviceid = method._request(request, "deviceid")
    pkgname = method._request(request, "pkgname")
    target = method._request(request, "target")
    try:
        match target:
            case Target.CPU:
                cpu = CPU.getObject(
                    pkgName=pkgname, deviceId=deviceid, platform=platform
                )
                appCpuRate, systemCpuRate = cpu.getCpuRate(noLog=True)
                result = {
                    "status": 1,
                    "appCpuRate": appCpuRate,
                    "systemCpuRate": systemCpuRate,
                }
            case Target.Memory:
                mem = Memory.getObject(
                    pkgName=pkgname, deviceId=deviceid, platform=platform
                )
                totalPass, swapPass = mem.getProcessMemory(noLog=True)
                result = {"status": 1, "totalPass": totalPass, "swapPass": swapPass}
            case Target.MemoryDetail:
                if platform == Platform.Android:
                    mem = Memory.getObject(
                        pkgName=pkgname, deviceId=deviceid, platform=platform
                    )
                    data = mem.getAndroidMemoryDetail(noLog=True)
                    result = {"status": 1, "data": data}
                else:
                    result = {"status": 0, "msg": "not support ios"}
            case Target.Network:
                network = Network.getObject(
                    pkgName=pkgname, deviceId=deviceid, platform=platform
                )
                data = network.getNetWorkData(wifi=True, noLog=True)
                result = {"status": 1, "upflow": data[0], "downflow": data[1]}
            case Target.FileIO:
                file_io = FileIO.getObject(
                    pkgName=pkgname, deviceId=deviceid, platform=platform
                )
                data = file_io.getFileIOData(noLog=True)
                result = {
                    "status": 1,
                    "read": data[0],
                    "write": data[1],
                    "sample_time": file_io.last_sample_time,
                }
            case Target.FPS:
                fps_monitor = FPS.getObject(
                    pkgName=pkgname, deviceId=deviceid, platform=platform
                )
                fps, jank = fps_monitor.getFPS(noLog=True)
                result = {"status": 1, "fps": fps, "jank": jank}
            case Target.Battery:
                battery_monitor = Battery.getObject(deviceId=deviceid, platform=platform)
                final = battery_monitor.getBattery(noLog=True)
                if platform == "Android":
                    result = {"status": 1, "level": final[0], "temperature": final[1]}
                else:
                    result = {
                        "status": 1,
                        "temperature": final[0],
                        "current": final[1],
                        "voltage": final[2],
                        "power": final[3],
                    }
            case Target.GPU:
                gpu = GPU.getObject(
                    pkgName=pkgname, deviceId=deviceid, platform=platform
                )
                final = gpu.getGPU()
                result = {"status": 1, "gpu": final}
            case Target.GPU_STATS:
                stats = create_gpu_stats(
                    deviceId=deviceid, pkgName=pkgname, pid=None, platform=platform
                )
                result = {"status": 1, "data": stats.sample()}
            case _:
                result = {"status": 0, "msg": "no this target"}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e)}
    return result
