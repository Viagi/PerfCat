from flask import Blueprint, request
from logzero import logger
from perfcattle.core.platform import Platform
from perfcattle.device.manager import Devices
from perfcattle.tools.http_utils import Method

device_api = Blueprint("device_api", __name__)
d = Devices()
method = Method()


@device_api.route("/device/info", methods=["post", "get"])
def deviceinfo():
    """get devices info"""
    platform = method._request(request, "platform")
    try:
        match platform:
            case Platform.Android:
                deviceids = d.getDeviceIds()
                devices = d.getDevices()
                if len(deviceids) > 0:
                    pkgnames = d.getPkgname(deviceids[0])
                    device_detail = d.getDeviceDetail(
                        deviceId=deviceids[0], platform=platform
                    )
                    result = {
                        "status": 1,
                        "deviceids": deviceids,
                        "devices": devices,
                        "pkgnames": pkgnames,
                        "device_detail": device_detail,
                    }
                else:
                    result = {"status": 0, "msg": "no devices"}
            case Platform.iOS:
                deviceinfos = d.getDeviceInfoByiOS()
                if len(deviceinfos) > 0:
                    pkgnames = d.getPkgnameByiOS(deviceinfos[0])
                    device_detail = d.getDeviceDetail(
                        deviceId=deviceinfos[0], platform=platform
                    )
                    result = {
                        "status": 1,
                        "deviceids": deviceinfos,
                        "devices": deviceinfos,
                        "pkgnames": pkgnames,
                        "device_detail": device_detail,
                    }
                else:
                    result = {"status": 0, "msg": "no devices"}
            case _:
                result = {"status": 0, "msg": f"no this platform = {platform}"}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": "devices connect error!"}
    return result


@device_api.route("/device/cpucore", methods=["post", "get"])
def cpucore():
    try:
        deviceId = d.getDeviceIds()[0]
        num = d.getCpuCores(deviceId)
        result = {"status": 1, "num": num}
    except Exception as e:
        result = {"status": 1, "num": 0}
    return result


@device_api.route("/device/package", methods=["post", "get"])
def packageNames():
    """get devices packageNames"""
    platform = method._request(request, "platform")
    device = method._request(request, "device")
    match platform:
        case Platform.Android:
            deviceId = d.getIdbyDevice(device, platform)
            pkgnames = d.getPkgname(deviceId)
        case Platform.iOS:
            pkgnames = d.getPkgnameByiOS(device)
        case _:
            result = {"status": 0, "msg": "platform is undefined"}
            return result
    result = (
        {"status": 1, "pkgnames": pkgnames}
        if len(pkgnames) > 0
        else {"status": 0, "msg": "no pkgnames"}
    )
    return result


@device_api.route("/ios/processes")
def iosProcesses():
    try:
        package = request.args.get("pkgname", "")
        if not package or len(package) > 255:
            raise ValueError("Select an iOS app first")
        return {"status": 1, "processes": d.getProcessesByiOS(request.args.get("device"), package)}
    except Exception as exc:
        logger.warning("iOS process listing failed: %s", exc)
        return {"status": 0, "msg": str(exc)}


@device_api.route("/package/pids", methods=["post", "get"])
def getPackagePids():
    platform = method._request(request, "platform")
    device = method._request(request, "device")
    pkgname = method._request(request, "pkgname")
    fast = str(request.values.get("fast", "0")).strip().lower() in (
        "1", "true", "yes", "on"
    )
    try:
        deviceId = d.getIdbyDevice(device, platform)
        pids = d.getPid(deviceId, pkgname, fast=fast)
        if len(pids) > 0:
            result = {"status": 1, "pids": pids}
        else:
            result = {
                "status": 0,
                "msg": "No process found, please start the app first.",
            }
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": "No process found, please start the app first."}
    return result


@device_api.route("/package/launch", methods=["post", "get"])
def launchPackage():
    """launch the selected app by package name / bundle id"""
    platform = request.values.get("platform")
    device = request.values.get("device")
    pkgname = (request.values.get("pkgname") or "").strip()
    if platform not in (Platform.Android, Platform.iOS):
        return {"status": 0, "msg": "platform is undefined"}
    if not device or not pkgname:
        return {"status": 0, "msg": "Select a device and app first"}
    try:
        deviceId = d.getIdbyDevice(device, platform)
        d.launchApp(deviceId, pkgname, platform)
        result = {"status": 1, "msg": "App launched"}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e) or "App launch failed"}
    return result


@device_api.route("/package/activity", methods=["post", "get"])
def getPackageActivity():
    platform = method._request(request, "platform")
    device = method._request(request, "device")
    try:
        deviceId = d.getIdbyDevice(device, platform)
        activity = d.getCurrentActivity(deviceId)
        result = {"status": 1, "activity": activity}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": "no activity found"}
    return result


@device_api.route("/package/start/time/android", methods=["post", "get"])
def getStartupTimeByAndroid():
    platform = method._request(request, "platform")
    device = method._request(request, "device")
    activity = method._request(request, "activity")
    try:
        deviceId = d.getIdbyDevice(device, platform)
        time = d.getStartupTimeByAndroid(activity, deviceId)
        result = {"status": 1, "time": time}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e)}
    return result


@device_api.route("/package/start/time/ios", methods=["post", "get"])
def getStartupTimeByiOS():
    pkgname = method._request(request, "pkgname")
    try:
        time = d.getStartupTimeByiOS(pkgname)
        result = {"status": 1, "time": time}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": "no result found"}
    return result
