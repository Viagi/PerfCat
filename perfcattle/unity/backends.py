# -*- coding: utf-8 -*-
"""Unity 采集后端。

两种后端实现同一组语义：

- ``HelperBackend``   游戏内集成 PerfCattleCapture.cs，通过设备文件 + 本机端口命令
- ``ProtocolBackend`` 直接模拟 Unity Editor 的 PlayerConnection 协议

上层 ``UnityCapture`` 只负责选择后端、创建暂存目录、写 meta 和归档，
不关心具体是哪种传输方式。新增后端只需实现 ``start/snapshot/stop/close``。
"""
import json
import os
import posixpath
import socket
import subprocess

from logzero import logger

from perfcattle.core.adb_client import adb
from perfcattle.unity.protocol import UnityProtocolSession

DEVICE_DIR_TEMPLATE = "/sdcard/Android/data/{pkg}/files/perfcattle"
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
    """采集后端：游戏内 PerfCattleCapture 组件。"""

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
        self.metrics_supported = None
        self.metrics_reason = None
        self.last_stats = None

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
                "PerfCattleCapture helper not found in {}".format(self.device_dir)
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

    # ----------------------------------------------------- game stats

    @staticmethod
    def _parse_stats_reply(reply):
        """Parse ``ok <json>`` from the helper's stats command."""
        text = (reply or "").strip()
        if text.startswith("err"):
            reason = text[3:].strip() or "helper_error"
            return {
                "available": False,
                "reason": "helper_outdated" if "unknown_command" in reason else reason,
            }
        _, _, payload = text.partition(" ")
        if not payload.strip().startswith("{"):
            return {"available": False, "reason": "invalid_stats_reply"}
        try:
            data = json.loads(payload)
        except ValueError:
            return {"available": False, "reason": "invalid_stats_json"}
        if not isinstance(data, dict):
            return {"available": False, "reason": "invalid_stats_json"}
        data.setdefault("available", False)
        return data

    def _detect_stats(self):
        try:
            reply = self._command("stats", timeout=5)
        except OSError as e:
            self.metrics_supported = None
            self.metrics_reason = "stats_channel_error: {}".format(e)
            return
        data = self._parse_stats_reply(reply)
        if (reply or "").strip().startswith("err"):
            self.metrics_supported = False
            self.metrics_reason = data.get("reason", "helper_error")
            return
        if data.get("reason") == "invalid_stats_reply":
            self.metrics_supported = False
            self.metrics_reason = "invalid_stats_reply"
            return
        self.metrics_supported = True
        self.metrics_reason = None
        self.last_stats = data

    def stats(self):
        """Return live Unity counters reported by the in-game helper."""
        if not self.available:
            return {"available": False, "reason": "not_running"}
        if self.metrics_supported is False:
            return {"available": False, "reason": self.metrics_reason}
        try:
            self._ensure_channel()
            reply = self._command("stats", timeout=5)
        except OSError as e:
            return {"available": False, "reason": "helper_unreachable", "msg": str(e)}
        data = self._parse_stats_reply(reply)
        if data.get("reason") == "helper_outdated":
            self.metrics_supported = False
            self.metrics_reason = "helper_outdated"
        self.last_stats = data
        return data

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
        self._detect_stats()
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

    def begin_record(self):
        """Rotate the on-device profiler log at recording start.

        Attaching only needs the stats channel; the binary log that is
        archived with the report must begin at the Start click. Rotating the
        helper log discards the frames captured while only the panel was
        live, so ``profiler.raw`` matches the system-metric timeline.
        """
        import time

        if not self.available:
            return None
        stamp = "record_{}".format(
            time.strftime("%Y%m%d_%H%M%S", time.localtime())
        )
        try:
            reply = self._command("start unity_{}".format(stamp), timeout=10)
        except OSError as e:
            logger.warning("[Unity] cannot rotate helper log: {}".format(e))
            return None
        parts = (reply or "").split()
        if not reply.startswith("ok") or len(parts) < 2:
            logger.warning(
                "[Unity] helper did not rotate log: {}".format(reply)
            )
            return None
        previous = self.remote_log
        self.remote_log = parts[1]
        if previous and previous != self.remote_log:
            self._shell('rm -f "{}"'.format(previous))
        logger.info("[Unity] recording started: {}".format(self.remote_log))
        return self.remote_log

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

    def stop(self, discard=False):
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
            if discard:
                # Auto-connect closed before recording: drop the device log
                # instead of pulling a potentially large file.
                self._shell('rm -f "{}"'.format(remote))
                result["profiler_size"] = 0
                return result
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
            "metrics_supported": self.metrics_supported,
            "metrics_reason": self.metrics_reason,
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
        self.metrics_supported = True
        self.metrics_reason = None

    def start(self, deep_profiling=False, areas=None):
        info = self.session.attach()
        self.unity_version = info.label
        self.product_name = info.project
        self.remote_log = os.path.join(self.staging_dir, PROFILER_FILE)
        normalized = self.session.profile.normalize_areas(areas)
        # Attach without a raw file: the stream feeds the live panel, while
        # the archived file starts at the recording start (begin_record).
        self.session.start_profiler(
            None, deep_profiling=deep_profiling, areas=normalized
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

    def begin_record(self):
        """Restart capture so ``profiler.raw`` starts at the recording start.

        Attaching only streams for the live panel; the restarted stream brings
        a fresh PD3U header and marker metadata, so the archived file is a
        self-contained profiler capture.
        """
        self.session.begin_record(self.remote_log)
        return self.remote_log

    def stats(self):
        """Parse live counters out of the raw profiler stream."""
        if not self.available:
            return {"available": False, "reason": "not_running"}
        data = self.session.stats_snapshot()
        data.setdefault("mode", self.mode)
        return data

    def stop(self, discard=False):
        result = {"status": 1, "files": []}
        try:
            self.session.stop_profiler()
            size = 0
            if self.remote_log and os.path.exists(self.remote_log):
                size = os.path.getsize(self.remote_log)
            if discard:
                if self.remote_log and os.path.exists(self.remote_log):
                    try:
                        os.remove(self.remote_log)
                    except OSError:
                        pass
                result["profiler_size"] = 0
                return result
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
            "metrics_supported": True,
            "metrics_reason": None,
        }
