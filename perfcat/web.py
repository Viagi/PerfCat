from __future__ import absolute_import
import multiprocessing
import time
import os
import webbrowser
import requests
import socket
import sys
import psutil
import logging
from logzero import logger
from threading import Thread
from flask import Flask
from pyfiglet import Figlet
from perfcat.view.device_api import device_api
from perfcat.view.apm_api import apm_api
from perfcat.view.report_api import report_api
from perfcat.view.tools_api import tools_api
from perfcat.view.ios_dependencies_api import ios_dependencies_api
from perfcat.view.pages import page
from perfcat import __version__
from perfcat.core.logging_config import STARTUP_LOG_ATTR, setup_runtime_logging


STARTUP_LOG_EXTRA = {STARTUP_LOG_ATTR: True}


class RequestFilter(logging.Filter):
    """Filter out static resource and high-frequency polling requests"""

    # Patterns to filter (static resources, polling endpoints)
    FILTER_PATTERNS = (
        "/static/",
        "/favicon.ico",
        "/apm/",
        "/device/",
        "/perfcat/version",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # Filter static resources and polling requests
        for pattern in self.FILTER_PATTERNS:
            if pattern in msg:
                return False
        # Filter 304 Not Modified responses (cached resources)
        if '" 304 -' in msg:
            return False
        return True


# Apply filter to Werkzeug logger
werkzeug_logger = logging.getLogger("werkzeug")
werkzeug_logger.addFilter(RequestFilter())

# Suppress multiprocessing SpawnPoolWorker traceback noise
# These appear on Windows when worker processes exit normally
multiprocessing.log_to_stderr(logging.CRITICAL)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.register_blueprint(device_api)
app.register_blueprint(apm_api)
app.register_blueprint(report_api)
app.register_blueprint(tools_api)
app.register_blueprint(ios_dependencies_api)
app.register_blueprint(page)


def ip() -> str:
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except:
        logger.info("hostname:{}".format(socket.gethostname()), extra=STARTUP_LOG_EXTRA)
        logger.warning(
            "config [127.0.0.1 hostname] in /etc/hosts file",
            extra=STARTUP_LOG_EXTRA,
        )
        ip = "127.0.0.1"
    return ip


def listen(port):
    net_connections = psutil.net_connections()
    conn = [c for c in net_connections if c.status == "LISTEN" and c.laddr.port == port]
    if conn:
        pid = conn[0].pid
        logger.warning(
            "Port {} is used by process {}".format(port, pid),
            extra=STARTUP_LOG_EXTRA,
        )
        logger.info(
            "you can start perfcat : python -m perfcat --port={}".format(port),
            extra=STARTUP_LOG_EXTRA,
        )
        return False
    return True


def status(host: str, port: int):
    try:
        r = requests.get("http://{}:{}".format(host, port), timeout=2.0)
        return r.status_code != 200
    except Exception:
        return True  # Keep waiting


def open_browser(url: str):
    # Browser helpers such as gio/xdg-open may write directly to inherited
    # stdout/stderr. Suppress that noise so the console only shows errors
    # emitted through logging.
    saved_fds = []
    seen_fds = set()
    try:
        with open(os.devnull, "w") as devnull:
            for stream in (sys.stdout, sys.stderr):
                try:
                    fd = stream.fileno()
                except Exception:
                    continue
                if fd in seen_fds:
                    continue
                seen_fds.add(fd)
                saved_fd = os.dup(fd)
                os.dup2(devnull.fileno(), fd)
                saved_fds.append((fd, saved_fd))

            webbrowser.open(url, new=2)
    except Exception as exc:
        logger.info("Failed to open browser: {}".format(exc), extra=STARTUP_LOG_EXTRA)
    finally:
        for fd, saved_fd in reversed(saved_fds):
            try:
                os.dup2(saved_fd, fd)
            finally:
                os.close(saved_fd)


def open_url(host: str, port: int):
    f = Figlet(font="slant", width=300)
    logger.info(
        "\n{}".format(f.renderText("PERFCAT {}".format(__version__))),
        extra=STARTUP_LOG_EXTRA,
    )
    # Wait for server ready
    while status(host, port):
        time.sleep(0.5)
    open_browser("http://{}:{}/?platform=Android&lan=en".format(host, port))
    logger.info(
        "Running on http://{}:{}/?platform=Android&lan=en (Press CTRL+C to quit)".format(
            host, port
        ),
        extra=STARTUP_LOG_EXTRA,
    )


def start(host: str, port: int, debug: bool = False):
    app.run(host=host, port=port, debug=debug)


def main(
    host=None,
    port=50003,
    debug: bool = False,
    log_file=None,
    log_stdout: bool = True,
):
    try:
        log_path = setup_runtime_logging(log_file=log_file, tee_stdout=log_stdout)
        logger.info(
            "PerfCat runtime log: {}".format(log_path),
            extra=STARTUP_LOG_EXTRA,
        )
        if host is None:
            host = ip()
        # Use thread for open_url to avoid multiprocessing worker traceback on exit
        t = Thread(target=open_url, args=(host, port), daemon=True)
        t.start()
        # Run Flask in main process
        start(host, port, debug)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        logger.exception(e)
