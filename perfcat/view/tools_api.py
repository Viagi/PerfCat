import os
import time
from flask import Blueprint, request
from logzero import logger
from perfcat.core.platform import Platform
from perfcat.device.manager import Devices
from perfcat.report.manager import ReportStore
from perfcat.tools.http_utils import Method
from perfcat.tools.install import Install
from perfcat.tools.recording import Scrcpy

tools_api = Blueprint("tools_api", __name__)
d = Devices()
f = ReportStore()
method = Method()


@tools_api.route("/apm/install/file", methods=["post", "get"])
def installFile():
    """install apk/ipa from file"""
    platform = method._request(request, "platform")
    file = request.files["file"]
    currentPath = os.path.dirname(os.path.realpath(__file__))
    install = Install()
    unixtime = int(time.time())
    if platform == Platform.Android:
        file_path = os.path.join(currentPath, "{}.apk".format(unixtime))
        if install.uploadFile(file_path, file):
            install_status = install.installAPK(file_path)
        else:
            result = {"status": 0, "msg": "install file failed"}
            return result
    else:
        file_path = os.path.join(currentPath, "{}.ipa".format(unixtime))
        if install.uploadFile(file_path, file):
            install_status = install.installIPA(file_path)
        else:
            result = {"status": 0, "msg": "install file failed"}
            return result
    if install_status[0]:
        result = {"status": 1, "msg": "install sucess"}
    else:
        result = {"status": 0, "msg": install_status[1]}
    return result


@tools_api.route("/apm/install/link", methods=["post", "get"])
def installLink():
    """install apk/ipa from link"""
    platform = method._request(request, "platform")
    link = method._request(request, "link")
    currentPath = os.path.dirname(os.path.realpath(__file__))
    install = Install()
    unixtime = int(time.time())
    if platform == Platform.Android:
        d_status = install.downloadLink(
            filelink=link, path=currentPath, name="{}.apk".format(unixtime)
        )
        if d_status:
            install_status = install.installAPK(
                os.path.join(currentPath, "{}.apk".format(unixtime))
            )
        else:
            result = {"status": 0, "msg": "download link failed"}
            return result
    else:
        d_status = install.downloadLink(
            filelink=link, path=currentPath, name="{}.ipa".format(unixtime)
        )
        if d_status:
            install_status = install.installIPA(
                os.path.join(currentPath, "{}.ipa".format(unixtime))
            )
        else:
            result = {"status": 0, "msg": "download link failed"}
            return result
    if install_status[0]:
        result = {"status": 1, "msg": "install sucess"}
    else:
        result = {"status": 0, "msg": install_status[1]}
    return result


@tools_api.route("/apm/record/start", methods=["post", "get"])
def start_record():
    device = method._request(request, "device")
    platform = method._request(request, "platform")
    try:
        deviceId = d.getIdbyDevice(device, platform)
        final = Scrcpy.start_record(deviceId, request.values)
        if final == 0:
            result = {"status": 1, "msg": "success"}
        else:
            result = {"status": 0, "msg": "record screen failed"}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": "record screen failed"}
    return result


@tools_api.route("/apm/record/cast", methods=["post", "get"])
def cast_screen():
    device = method._request(request, "device")
    platform = method._request(request, "platform")
    try:
        deviceId = d.getIdbyDevice(device, platform)
        final = Scrcpy.cast_screen(deviceId)
        if final == 0:
            result = {"status": 1, "msg": "success"}
        else:
            result = {"status": 0, "msg": "cast screen failed"}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": "cast screen failed"}
    return result


@tools_api.route("/apm/record/play", methods=["post", "get"])
def play_record():
    scene = method._request(request, "scene")
    video = os.path.join(f.get_repordir(), scene, "record.mp4")
    try:
        Scrcpy.play_video(video)
        result = {"status": 1, "msg": "success"}
    except Exception as e:
        logger.exception(e)
        result = {"status": 0, "msg": "play video failed"}
    return result
