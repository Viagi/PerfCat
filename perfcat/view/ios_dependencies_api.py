"""Local, same-origin endpoints for optional iOS runtime setup."""
import secrets
import socket
from urllib.parse import urlsplit

from flask import Blueprint, request

from perfcat.service.ios_dependencies import IOSDependencies, ios_dependencies


ios_dependencies_api = Blueprint("ios_dependencies_api", __name__)
_token = secrets.token_urlsafe(32)


@ios_dependencies_api.before_request
def local_access():
    addresses = {"127.0.0.1", "::1"}
    try:
        addresses.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    host = urlsplit(request.host_url).hostname
    if request.remote_addr not in addresses or host not in addresses | {"localhost", socket.gethostname().lower()}:
        return {"status": 0, "msg": "Open PerfCat on this computer to manage dependencies."}, 403
    if request.method == "POST":
        if request.headers.get("Origin", "").rstrip("/") != request.host_url.rstrip("/"):
            return {"status": 0, "msg": "Same-origin request required."}, 403
        if not secrets.compare_digest(request.headers.get("X-PerfCat-Install-Token", ""), _token):
            return {"status": 0, "msg": "Installation confirmation expired; check dependencies again."}, 403


@ios_dependencies_api.route("/ios/dependencies/check")
def check():
    try:
        version = IOSDependencies.device_version(request.args.get("device"))
        profile = "modern" if int(version.split(".")[0]) >= 17 else "legacy"
        result = ios_dependencies[profile].check()
        return {"status": 1, **result, "ios_version": version, "token": _token}
    except Exception as exc:
        return {"status": 0, "msg": str(exc)}, 400


@ios_dependencies_api.route("/ios/dependencies/status")
def status():
    manager = ios_dependencies.get(request.args.get("profile"))
    if manager is None:
        return {"status": 0, "msg": "Unknown dependency profile."}, 400
    return {"status": 1, **manager.snapshot(), "log": manager.log_tail()}


@ios_dependencies_api.route("/ios/dependencies/install", methods=["POST"])
def install():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {"confirmed", "profile"} or body.get("confirmed") is not True:
        return {"status": 0, "msg": "Explicit confirmation required."}, 400
    manager = ios_dependencies.get(body.get("profile")) if isinstance(body.get("profile"), str) else None
    if manager is None:
        return {"status": 0, "msg": "Unknown dependency profile."}, 400
    try:
        return {"status": 1, **manager.start()}
    except Exception as exc:
        return {"status": 0, "msg": str(exc)}, 400
