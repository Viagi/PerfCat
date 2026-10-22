# -*- coding: utf-8 -*-
"""Unity 采集后端。

两种后端实现同一组语义：

- ``HelperBackend``   游戏内集成 PerfCatCapture.cs，通过设备文件 + 本机端口命令
- ``ProtocolBackend`` 直接模拟 Unity Editor 的 PlayerConnection 协议

上层 ``UnityCapture`` 只负责选择后端、创建暂存目录、写 meta 和归档，
不关心具体是哪种传输方式。新增后端只需实现 ``start/snapshot/stop/close``。
"""
import os
import posixpath
import socket
import subprocess

from logzero import logger

from perfcat.core.adb_client import adb
from perfcat.unity.protocol import UnityProtocolSession

DEVICE_DIR_TEMPLATE = "/sdcard/Android/data/{pkg}/files/perfcat"
PROFILER_FILE = "profiler.raw"
PORT_FILE = "port.txt"
COMMAND_PORT = 39777


def _run(args, timeout=None):
    """Run an adb command, return (returncode, stdout)."""
    try:
        proc = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout
        )
        return proc.returncode, proc.stdout.decode("utf-8", errors="replace").strip()
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as e:
        return -1, str(e)


class HelperBackend(object):
    """采集后端：游戏内 PerfCatCapture 组件。"""

    mode = "helper"

    def __init__(self, device_id, pkg_name, pid, device_dir, staging_dir):
        self.device = device_id
        self.pkg = pkg_name
        self.pid = pid
        self.device_dir = device_dir
        self.staging_dir = staging_dir
        self.local_port = None
        self.remote_port = None
        self.remote_log = None
        self.unity_version = None
        self.product_name = None
        self.available = False
        self.files = []
        self.deep_profiling = False
        self.areas = None
        self.enabled_categories = None
        self.ignored_options = []

    # ------------------------------------------------------------- adb

    def _shell(self, cmd, timeout=8):
        return _run(
            [adb.adb_path, "-s", self.device, "shell", cmd], timeout=timeout
        )

    def _forward(self, remote_port):
        code, out = _run(
            [adb.adb_path, "-s", self.device, "forward", "tcp:0",
             "tcp:{}".format(remote_port)],
            timeout=10,
        )
        if code != 0 or not out.isdigit():
            raise RuntimeError("adb forward failed: {}".format(out))
        return int(out)

    def _unforward(self):
        if self.local_port:
            _run(
                [adb.adb_path, "-s", self.device, "forward", "--remove",
                 "tcp:{}".format(self.local_port)],
                timeout=10,
            )
            self.local_port = None

    def _device_file_size(self, path):
        code, out = self._shell('stat -c %s "{}"'.format(path))
        if code == 0 and out.isdigit():
            return int(out)
        return -1

    def _pull(self, remote, local):
        os.makedirs(os.path.dirname(local), exist_ok=True)
        code, out = _run([adb.adb_path, "-s", self.device, "pull", remote, local])
        if code != 0 or not os.path.exists(local):
            raise RuntimeError("adb pull failed: {}".format(out))
        return os.path.getsize(local)

    def _read_port(self):
        code, out = self._shell(
            'cat "{}"'.format(posixpath.join(self.device_dir, PORT_FILE))
        )
        if code != 0 or not out.strip().isdigit():
            return None
        return int(out.strip())

    # --------------------------------------------------------- channel

    def _connect(self):
        sock = socket.create_connection(("127.0.0.1", self.local_port), timeout=8)
        sock.settimeout(8)
        return sock

    def _readline(self, sock):
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf.decode("utf-8", errors="replace").strip()

    def _command(self, command, timeout=8):
        sock = self._connect()
        try:
            sock.settimeout(timeout)
            sock.sendall((command + "\n").encode("utf-8"))
            return self._readline(sock)
        finally:
            sock.close()

    def _open_channel(self):
        port = self._read_port()
        if not port:
            raise RuntimeError(
                "PerfCatCapture helper not found in {}".format(self.device_dir)
            )
        if self.remote_port != port or self.local_port is None:
            self._unforward()
            self.local_port = self._forward(port)
            self.remote_port = port

    def _ensure_channel(self):
        if self.local_port is None:
            self._open_channel()
            return
        try:
            reply = self._command("ping", timeout=5)
            if reply.startswith("ok"):
                return
        except OSError:
            pass
        self._open_channel()

    # ------------------------------------------------------------- api

    def start(self, deep_profiling=False, areas=None):
        self._open_channel()
        reply = self._command("ping", timeout=6)
        if not reply.startswith("ok"):
            raise RuntimeError("unexpected helper reply: {}".format(reply))
        parts = reply.split(" ")
        self.unity_version = parts[1] if len(parts) > 1 else "unknown"
        self.product_name = parts[2] if len(parts) > 2 else ""
        stamp = os.path.basename(self.staging_dir).replace("unity_", "")
        self.remote_log = None
        rotate = self._command("start unity_{}".format(stamp), timeout=10)
        if rotate.startswith("ok") and len(rotate.split()) > 1:
            self.remote_log = rotate.split()[1]
        self.available = True
        # The in-game helper captures in managed code and has no PlayerConnection
        # channel, so deep profiling / category selection cannot be applied here.
        self.deep_profiling = False
        self.areas = None
        self.ignored_options = []
        if deep_profiling:
            self.ignored_options.append("deep_profiling")
        if areas is not None:
            self.ignored_options.append("areas")
        if self.ignored_options:
            logger.warning(
                "[Unity] helper backend ignores options: {}".format(
                    ", ".join(self.ignored_options)
                )
            )
        logger.info(
            "[Unity] helper backend attached: {} (port {})".format(
                self.unity_version, self.remote_port
            )
        )
        return {
            "unity_version": self.unity_version,
            "product_name": self.product_name,
            "port": self.remote_port,
            "deep_profiling": False,
            "areas": None,
            "ignored_options": self.ignored_options,
        }

    def set_options(self, deep_profiling=None, areas=None):
        raise RuntimeError(
            "helper backend cannot change deep profiling or profiler modules"
        )

    def snapshot(self, name):
        self._ensure_channel()
        sock = None
        try:
            sock = self._connect()
            sock.sendall(("snapshot {}\n".format(name)).encode("utf-8"))
            first = self._readline(sock)
            if not first.startswith("started"):
                if first.startswith("ok"):
                    raise RuntimeError("helper did not report a snapshot path")
                raise RuntimeError(first or "snapshot request failed")
            sock.settimeout(300)
            final = self._readline(sock)
        finally:
            if sock is not None:
                sock.close()

        if not final.startswith("ok"):
            raise RuntimeError(final or "snapshot failed")
        parts = final.split()
        remote = parts[1] if len(parts) > 1 else posixpath.join(
            self.device_dir, "{}.snapshot".format(name)
        )
        local = os.path.join(self.staging_dir, posixpath.basename(remote))
        size = self._pull(remote, local)
        self._shell('rm -f "{}"'.format(remote))
        self.files.append({"name": os.path.basename(local), "size": size})
        return {"file": local, "name": os.path.basename(local), "size": size}

    def stop(self):
        result = {"status": 1, "files": []}
        try:
            if self.available:
                try:
                    self._command("stop", timeout=10)
                except OSError:
                    pass
                import time
                time.sleep(0.8)
            remote = self.remote_log or posixpath.join(self.device_dir, PROFILER_FILE)
            size = self._device_file_size(remote)
            if size > 0:
                local = os.path.join(self.staging_dir, posixpath.basename(remote))
                pulled = self._pull(remote, local)
                self.files.append({"name": os.path.basename(local), "size": pulled})
                result["profiler_size"] = pulled
                result["files"].append(
                    {"name": os.path.basename(local), "size": pulled}
                )
            else:
                result["profiler_size"] = 0
        finally:
            self.close()
            self.available = False
        result["files"] = self.files
        return result

    def close(self):
        self._unforward()

    def status(self):
        return {
            "unity_version": self.unity_version,
            "product_name": self.product_name,
            "port": self.remote_port,
            "bytes": None,
            "deep_profiling": self.deep_profiling,
            "areas": self.areas,
            "enabled_categories": self.enabled_categories,
            "ignored_options": self.ignored_options,
        }


class ProtocolBackend(object):
    """采集后端：直接使用 Unity PlayerConnection 协议。"""

    mode = "protocol"

    def __init__(self, device_id, pkg_name, pid, staging_dir):
        self.device = device_id
        self.pkg = pkg_name
        self.pid = pid
        self.staging_dir = staging_dir
        self.session = UnityProtocolSession(device_id, pkg_name, pid)
        self.remote_log = None
        self.unity_version = None
        self.product_name = None
        self.available = False
        self.files = []
        self.deep_profiling = False
        self.areas = None
        self.enabled_categories = None
        self.ignored_options = []

    def start(self, deep_profiling=False, areas=None):
        info = self.session.attach()
        self.unity_version = info.label
        self.product_name = info.project
        self.remote_log = os.path.join(self.staging_dir, PROFILER_FILE)
        normalized = self.session.profile.normalize_areas(areas)
        self.session.start_profiler(
            self.remote_log, deep_profiling=deep_profiling, areas=normalized
        )
        self.deep_profiling = self.session.deep_profiling
        self.areas = normalized
        self.enabled_categories = self.session.enabled_categories
        self.available = True
        logger.info(
            "[Unity] protocol backend attached: {} (port {})".format(
                self.unity_version, info.port
            )
        )
        return {
            "unity_version": self.unity_version,
            "product_name": self.product_name,
            "port": info.port,
            "features": sorted(self.session.profile.features),
            "deep_profiling": self.deep_profiling,
            "areas": self.areas,
            "enabled_categories": self.enabled_categories,
        }

    def set_options(self, deep_profiling=None, areas=None):
        """Apply Deep Profiling and/or a module subset on the live connection."""
        if not self.available:
            raise RuntimeError("unity capture is not running")
        if deep_profiling is not None:
            self.session.set_deep_profiling(deep_profiling)
            self.deep_profiling = self.session.deep_profiling
        if areas is not None:
            normalized = self.session.profile.normalize_areas(areas)
            self.session.set_areas(normalized)
            self.areas = normalized
            self.enabled_categories = self.session.enabled_categories
        return {
            "deep_profiling": self.deep_profiling,
            "areas": self.areas,
            "enabled_categories": self.enabled_categories,
        }

    def snapshot(self, name):
        path = os.path.join(self.staging_dir, name + ".snapshot")
        data = self.session.take_snapshot(path)
        self.files.append({"name": os.path.basename(path), "size": data["size"]})
        return {
            "file": path,
            "name": os.path.basename(path),
            "size": data["size"],
        }

    def stop(self):
        result = {"status": 1, "files": []}
        try:
            self.session.stop_profiler()
            size = 0
            if self.remote_log and os.path.exists(self.remote_log):
                size = os.path.getsize(self.remote_log)
            if size > 0:
                self.files.append({"name": PROFILER_FILE, "size": size})
                result["files"].append({"name": PROFILER_FILE, "size": size})
            result["profiler_size"] = size
        finally:
            self.session.close()
            self.available = False
        result["files"] = self.files
        return result

    def close(self):
        self.session.close()

    def status(self):
        return {
            "unity_version": self.unity_version,
            "product_name": self.product_name,
            "port": self.session.remote_port,
            "bytes": self.session.raw_bytes,
            "deep_profiling": self.session.deep_profiling,
            "areas": self.areas,
            "enabled_categories": self.session.enabled_categories,
            "ignored_options": [],
        }
