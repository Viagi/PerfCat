# -*- coding: utf-8 -*-
"""Unity Profiler / MemoryProfiler 采集接口。"""
import io
import json
import os
import re
import zipfile

from flask import Blueprint, request, send_file
from logzero import logger

from perfcattle.core.platform import Platform
from perfcattle.device.manager import Devices
from perfcattle.report.manager import ReportStore
from perfcattle.service.unity_capture import unity_capture
from perfcattle.unity.hotspots import HotspotError, get_hotspot_reader
from perfcattle.unity.profiles import (
    default_areas,
    supported_areas,
    supported_versions,
)

unity_api = Blueprint("unity_api", __name__)
d = Devices()
f = ReportStore()


def _arg(name, default=""):
    return request.values.get(name, default)


def _bool_arg(name, default=False):
    value = _arg(name, None)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _optional_bool_arg(name):
    if name not in request.values:
        return None
    return _bool_arg(name)


def _areas_arg():
    """Read ``areas`` as a JSON array, comma list, or repeated form field.

    Missing parameter -> ``None`` (keep the player default). An explicit empty
    value -> ``[]`` (disable all categories).
    """
    if "areas" not in request.values and "areas[]" not in request.values:
        return None
    raw = request.values.getlist("areas") + request.values.getlist("areas[]")
    if len(raw) == 1:
        text = raw[0].strip()
        if text.startswith("["):
            try:
                raw = json.loads(text)
            except ValueError:
                raise ValueError("areas must be a JSON array or comma list")
        else:
            raw = text.split(",")
    result = []
    for item in raw:
        item = str(item).strip()
        if item and item not in result:
            result.append(item)
    return result


@unity_api.route("/apm/unity/versions", methods=["post", "get"])
def unityVersions():
    """list Unity versions the protocol layer can attach to"""
    return {
        "status": 1,
        "versions": supported_versions(),
        "areas": supported_areas(),
        "default_areas": default_areas(),
        "default_deep_profiling": False,
    }


@unity_api.route("/apm/unity/start", methods=["post", "get"])
def unityStart():
    """attach to a Unity player and start capturing profiler data"""
    platform = _arg("platform")
    device = _arg("device")
    pkgname = _arg("pkgname")
    process = _arg("process")
    try:
        if platform != Platform.Android:
            result = {
                "status": 1,
                "available": False,
                "msg": "unity capture supports android only",
            }
        else:
            areas = _areas_arg()
            deep_profiling = _bool_arg("deep_profiling", False)
            deviceId = d.getIdbyDevice(device, platform)
            pid = process.split(":")[0] if process and ":" in process else (process or None)
            result = unity_capture.start(
                deviceId,
                pkgname,
                pid=pid,
                deep_profiling=deep_profiling,
                areas=areas,
                record=_bool_arg("record", True),
            )
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "available": False, "msg": str(e)}
    return result


@unity_api.route("/apm/unity/arm", methods=["post", "get"])
def unityArm():
    """arm background auto-connect while the Unity switch is on"""
    params, error = _unity_target_params()
    if error:
        return error
    try:
        return unity_capture.arm(**params)
    except Exception as e:
        logger.exception(e)
        return {
            "status": 0,
            "available": False,
            "arming": False,
            "msg": str(e),
        }


@unity_api.route("/apm/unity/disarm", methods=["post", "get"])
def unityDisarm():
    """stop background auto-connect (optionally close the capture)"""
    try:
        return unity_capture.disarm(
            stop=_bool_arg("stop", False),
            discard=_bool_arg("discard", False),
        )
    except Exception as e:
        logger.exception(e)
        return {"status": 0, "available": False, "msg": str(e)}


@unity_api.route("/apm/unity/wait", methods=["post", "get"])
def unityWait():
    """wait until Unity is connected, or return the last failure reason"""
    params, error = _unity_target_params()
    if error:
        return error
    timeout = _arg("timeout") or "15"
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 15.0
    try:
        unity_capture.arm(**params)
        return unity_capture.wait_connected(timeout)
    except Exception as e:
        logger.exception(e)
        return {
            "status": 0,
            "available": False,
            "msg": str(e),
        }


def _unity_target_params():
    """Parse the target arguments shared by arm/wait."""
    platform = _arg("platform")
    if platform != Platform.Android:
        return None, {
            "status": 1,
            "available": False,
            "arming": False,
            "msg": "unity capture supports android only",
        }
    device = _arg("device")
    pkgname = _arg("pkgname")
    if not device or not pkgname:
        return None, {
            "status": 0,
            "available": False,
            "arming": False,
            "msg": "select a device and app first",
        }
    process = _arg("process")
    pid = process.split(":")[0] if process and ":" in process else (process or None)
    raw_device_id = re.sub(r"\(.*?\)|\{.*?\}|\[.*?\]", "", device).strip()
    try:
        deviceId = d.getIdbyDevice(device, platform)
    except Exception as e:
        # The device may be disconnected right now. Keep the serial so the
        # background retry can attach as soon as it reconnects.
        if not raw_device_id:
            logger.exception(e)
            return None, {
                "status": 0,
                "available": False,
                "arming": False,
                "msg": str(e) or "device lookup failed",
            }
        logger.debug(
            "unity auto-connect waiting for device %s: %s", raw_device_id, e
        )
        deviceId = raw_device_id
    try:
        areas = _areas_arg()
    except ValueError as e:
        return None, {
            "status": 0,
            "available": False,
            "arming": False,
            "msg": str(e),
        }
    return {
        "deviceId": deviceId,
        "pkgName": pkgname,
        "pid": pid,
        "deep_profiling": _bool_arg("deep_profiling", False),
        "areas": areas,
        "record": _bool_arg("record", False),
    }, None


@unity_api.route("/apm/unity/options", methods=["post", "get"])
def unityOptions():
    """change Deep Profiling / Profiler Modules on the live connection"""
    try:
        result = unity_capture.apply_options(
            deep_profiling=_optional_bool_arg("deep_profiling"),
            areas=_areas_arg(),
        )
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e)}
    return result


@unity_api.route("/apm/unity/snapshot", methods=["post", "get"])
def unitySnapshot():
    """ask the Unity player for a MemoryProfiler snapshot and store it"""
    try:
        result = unity_capture.snapshot()
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e)}
    return result


@unity_api.route("/apm/unity/stop", methods=["post", "get"])
def unityStop():
    """stop the Unity capture and finish the streamed files"""
    try:
        result = unity_capture.stop()
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": str(e)}
    return result


@unity_api.route("/apm/unity/status", methods=["post", "get"])
def unityStatus():
    return unity_capture.status()


@unity_api.route("/apm/unity/stats", methods=["post", "get"])
def unityStats():
    """live Unity counters for the game panel (helper backend only)"""
    try:
        return unity_capture.stats()
    except Exception as e:
        logger.exception(e)
        return {
            "status": 0,
            "available": False,
            "reason": "stats_failed",
            "msg": str(e),
        }


def _unity_raw_path(scene):
    root = os.path.abspath(f.report_dir)
    if not scene:
        return None
    folder = os.path.abspath(os.path.join(root, scene, "unity"))
    path = os.path.abspath(os.path.join(folder, "profiler.raw"))
    if not path.startswith(os.path.abspath(root) + os.sep):
        return None
    if not os.path.isfile(path):
        return None
    return path


@unity_api.route("/apm/unity/frames", methods=["post", "get"])
def unityFrames():
    """list frames available in an archived Unity profiler.raw"""
    path = _unity_raw_path(_arg("scene"))
    if not path:
        return {"status": 0, "msg": "profiler.raw not found"}
    try:
        reader = get_hotspot_reader(path)
        return {"status": 1, "frames": reader.frames()}
    except HotspotError as e:
        return {"status": 0, "msg": str(e)}
    except Exception as e:
        logger.exception(e)
        return {"status": 0, "msg": str(e)}


@unity_api.route("/apm/unity/frame", methods=["post", "get"])
def unityFrame():
    """return Timeline / Hierarchy / Raw Hierarchy data for one frame"""
    path = _unity_raw_path(_arg("scene"))
    if not path:
        return {"status": 0, "msg": "profiler.raw not found"}
    try:
        frame = int(_arg("frame") or 0)
    except ValueError:
        return {"status": 0, "msg": "frame must be an integer"}
    try:
        reader = get_hotspot_reader(path)
        return reader.frame(frame, _arg("thread") or "main")
    except HotspotError as e:
        return {"status": 0, "msg": str(e)}
    except Exception as e:
        logger.exception(e)
        return {"status": 0, "msg": str(e)}


@unity_api.route("/apm/unity/files", methods=["post", "get"])
def unityFiles():
    """list raw files captured for a scene"""
    scene = _arg("scene")
    folder = os.path.join(f.report_dir, scene, "unity")
    files = []
    if scene and os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                files.append({"name": name, "size": os.path.getsize(path)})
    return {"status": 1, "files": files}


@unity_api.route("/apm/unity/download", methods=["post", "get"])
def unityDownload():
    """download one captured Unity file"""
    scene = _arg("scene")
    name = os.path.basename(_arg("file") or "")
    folder = os.path.abspath(os.path.join(f.report_dir, scene or "", "unity"))
    path = os.path.abspath(os.path.join(folder, name))
    if not name or not path.startswith(folder + os.sep) or not os.path.isfile(path):
        return {"status": 0, "msg": "file not found"}, 404
    return send_file(path, as_attachment=True, download_name=name)


@unity_api.route("/apm/unity/helper", methods=["post", "get"])
def unityHelper():
    """download the drop-in Unity helper scripts"""
    base = os.path.join(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "assets", "unity"
    )
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in ("PerfCattleCapture.cs", "PerfCattleCaptureRunner.cs", "README.md"):
            path = os.path.join(base, name)
            if os.path.exists(path):
                bundle.write(path, name)
        doc = os.path.join(repo_root, "docs", "UNITY_CAPTURE.md")
        if os.path.exists(doc):
            bundle.write(doc, "UNITY_CAPTURE.md")
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="perfcattle-unity-capture.zip",
        mimetype="application/zip",
    )
