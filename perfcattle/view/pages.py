import json
import os
from flask import Blueprint
from flask import render_template
from flask import request
from logzero import logger
from perfcattle.device.manager import Devices
from perfcattle.report.manager import ReportStore
from perfcattle.tools.http_utils import Method

page = Blueprint("page", __name__)
d = Devices()
m = Method()
f = ReportStore()


@page.app_errorhandler(404)
def page_404(e):
    settings = m._settings(request)
    return render_template("404.html", **locals()), 404


@page.app_errorhandler(500)
def page_500(e):
    settings = m._settings(request)
    return render_template("500.html", **locals()), 500


@page.route("/")
def index():
    platform = request.args.get("platform")
    lan = request.args.get("lan")
    settings = m._settings(request)
    return render_template("index.html", **locals())


@page.route("/pk")
def pk():
    lan = request.args.get("lan")
    model = request.args.get("model")
    settings = m._settings(request)
    return render_template("pk.html", **locals())


@page.route("/report")
def report():
    lan = request.args.get("lan")
    settings = m._settings(request)
    report_dir = os.path.join(os.getcwd(), "report")
    if not os.path.exists(report_dir):
        os.mkdir(report_dir)
    dirs = os.listdir(report_dir)
    dir_list = reversed(
        sorted(dirs, key=lambda x: os.path.getmtime(os.path.join(report_dir, x)))
    )
    apm_data = []
    for dir in dir_list:
        if dir.split(".")[-1] not in ["log", "json", "mkv"]:
            try:
                with open(
                    os.path.join(report_dir, dir, "result.json"),
                    encoding="utf-8",
                ) as fpath:
                    json_data = json.loads(fpath.read())
                dict_data = {
                    "scene": dir,
                    "app": json_data["app"],
                    "platform": json_data["platform"],
                    "model": json_data["model"],
                    "devices": json_data["devices"],
                    "ctime": json_data["ctime"],
                    "video": json_data.get("video", 0),
                }
                apm_data.append(dict_data)
            except FileNotFoundError:
                # Skip directories without result.json (corrupted/incomplete reports)
                continue
            except Exception as e:
                logger.exception(e)
                continue
    apm_data_len = len(apm_data)
    return render_template("report.html", **locals())


@page.route("/analysis", methods=["post", "get"])
def analysis():
    lan = request.args.get("lan")
    scene = request.args.get("scene")
    app = request.args.get("app")
    platform = request.args.get("platform")
    settings = m._settings(request)
    report_dir = os.path.join(os.getcwd(), "report")
    dirs = os.listdir(report_dir)
    filter_dir = f.list_other_scenes(
        scene, app=app, platform=platform, model="normal"
    )
    apm_data = {}
    time_range = {"min": 0.0, "max": 0.0}
    # Initialize disk variables with defaults to prevent template errors
    initial_disk = []
    current_disk = []
    sum_init_disk = {"sum_size": 0, "sum_used": 0, "sum_free": 0}
    sum_current_disk = {"sum_size": 0, "sum_used": 0, "sum_free": 0}
    if scene in dirs:
        try:
            time_range = f.scene_time_range(scene)
            if platform == "Android":
                apm_data = f._setAndroidPerfs(scene)
                disk = f.analysisDisk(scene)
                initial_disk = disk[0]
                current_disk = disk[1]
                sum_init_disk = disk[2]
                sum_current_disk = disk[3]
            else:
                apm_data = f._setiOSPerfs(scene)
        except ZeroDivisionError:
            pass
        except Exception as e:
            logger.exception(e)
    return render_template("analysis.html", **locals())


@page.route("/pk_analysis", methods=["post", "get"])
def analysis_pk():
    lan = request.args.get("lan")
    scene = request.args.get("scene")
    app = request.args.get("app")
    model = request.args.get("model")
    settings = m._settings(request)
    report_dir = os.path.join(os.getcwd(), "report")
    dirs = os.listdir(report_dir)
    apm_data = {}
    if scene in dirs:
        try:
            apm_data = f._setpkPerfs(scene)
        except Exception as e:
            logger.exception(e)
    return render_template("analysis_pk.html", **locals())


COMPARE_SCENE_LIMIT = 6


def _compare_scene_names():
    """Read requested compare scenes from the URL (new and legacy formats)."""
    raw = []
    for source in (request.args, request.form):
        raw.extend(source.getlist("scenes"))
        raw.extend(source.getlist("scenes[]"))
    if not raw:
        raw = [request.args.get("scene1"), request.args.get("scene2")]

    scenes = []
    for value in raw:
        if value is None:
            continue
        for item in str(value).split(","):
            item = item.strip()
            if item and item not in scenes:
                scenes.append(item)
    return scenes[:COMPARE_SCENE_LIMIT]


def _scene_result(scene):
    path = os.path.join(os.getcwd(), "report", scene, "result.json")
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _valid_compare_scenes(requested):
    """Keep existing normal reports of the same app and platform."""
    report_dir = os.path.abspath(os.path.join(os.getcwd(), "report"))
    entries = []
    for scene in requested:
        target = os.path.abspath(os.path.join(report_dir, scene))
        if os.path.dirname(target) != report_dir or not os.path.isdir(target):
            continue
        meta = _scene_result(scene)
        if meta is None or meta.get("model") != "normal":
            continue
        entries.append((scene, meta))
    if not entries:
        return [], None, None
    app = entries[0][1].get("app")
    platform = entries[0][1].get("platform")
    matches = [
        scene
        for scene, meta in entries
        if meta.get("app") == app and meta.get("platform") == platform
    ]
    return matches[:COMPARE_SCENE_LIMIT], app, platform


@page.route("/compare_analysis", methods=["post", "get"])
def analysis_compare():
    platform = request.args.get("platform")
    lan = request.args.get("lan")
    app = request.args.get("app")
    settings = m._settings(request)
    scenes, meta_app, meta_platform = _valid_compare_scenes(_compare_scene_names())
    if meta_app is not None:
        app = meta_app
    if meta_platform is not None:
        platform = meta_platform

    apm_data_list = []
    unity_data_list = []
    valid_scenes = []
    for scene in scenes:
        try:
            if platform == "Android":
                data = f._setAndroidPerfs(scene)
            else:
                data = f._setiOSPerfs(scene)
        except ZeroDivisionError:
            continue
        except Exception as e:
            logger.exception(e)
            continue
        valid_scenes.append(scene)
        apm_data_list.append(data)

    scenes = valid_scenes
    for scene in scenes:
        try:
            unity_data_list.append(f.getUnityStatsLog(platform, scene))
        except Exception as e:
            logger.exception(e)
            unity_data_list.append({})

    show_unity = any(item.get("charts") for item in unity_data_list)
    cpu_cores = 0
    if platform == "Android":
        for scene in scenes:
            try:
                cpu_cores = max(
                    cpu_cores, int(f.read_result(scene).get("cores", 0) or 0)
                )
            except Exception:
                pass

    # Keep the old variables for links/backward-compatible templates.
    scene1 = scenes[0] if scenes else None
    scene2 = scenes[1] if len(scenes) > 1 else None
    return render_template("analysis_compare.html", **locals())
