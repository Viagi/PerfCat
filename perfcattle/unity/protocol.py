# -*- coding: utf-8 -*-
"""Unity PlayerConnection 协议层。

本文件只负责“怎么把消息发出去、怎么把帧读回来”，不关心具体 Unity 版本，
也不关心采集文件怎么落盘。版本相关的消息 ID 全部来自
``perfcattle.unity.profiles``。

线格式（与 Unity Editor 一致）::

    uint32 magic = 0x67a54e8f
    uint8[16] message GUID
    uint32 payload length
    uint8[payload]

消息 GUID 由 32 位十六进制字符串经 ``UnityGUID::Generate`` 生成：每 8 个字符
打包成一个 little-endian uint32（等价于每 4 字节组反序）。该规则已用 Player
自己发出的 PlayerInfo 帧验证。
"""
import os
import socket
import struct
import threading
import time

from logzero import logger

from perfcattle.core.adb_client import adb
from perfcattle.unity.profiles import (
    FEATURE_DEEP_PROFILING,
    FEATURE_ENABLED_CATEGORIES,
    FEATURE_MEMORY_SNAPSHOT,
    FEATURE_PROFILER_STREAM,
    get_profile,
    supported_labels,
)
from perfcattle.unity.raw_reader import RawStatsParser

MAGIC = 0x67A54E8F
MAX_PAYLOAD = 0x10000000
PROFILER_PORT_RANGE = range(55000, 55512)

# CaptureFlags: ManagedObjects | NativeObjects | NativeAllocations |
# NativeAllocationSites | NativeStackTraces
DEFAULT_CAPTURE_FLAGS = 31


def guid_from_hex(text):
    """Mirror UnityGUID::Generate for a 32-character hex string."""
    if len(text) != 32:
        raise ValueError("message id must be 32 hex characters")
    out = bytearray()
    for group in range(4):
        chunk = text[group * 8:(group + 1) * 8]
        nibbles = [int(c, 16) for c in chunk]
        word = 0
        for i in range(8):
            word |= nibbles[7 - i] << (28 - 4 * i)
        out += struct.pack("<I", word)
    return bytes(out)


class UnityProtocolError(Exception):
    pass


class UnsupportedUnityVersion(UnityProtocolError):
    def __init__(self, connection_version):
        self.connection_version = connection_version
        supported = ", ".join(supported_labels()) or "none"
        super().__init__(
            "Unity PlayerConnection version {} is not supported yet "
            "(supported: {})".format(connection_version, supported)
        )


class Frame(object):
    __slots__ = ("guid", "payload")

    def __init__(self, guid, payload):
        self.guid = guid
        self.payload = payload


def read_frame(sock, timeout):
    sock.settimeout(timeout)
    header = b""
    while len(header) < 24:
        chunk = sock.recv(24 - len(header))
        if not chunk:
            return None
        header += chunk
    magic = struct.unpack_from("<I", header, 0)[0]
    if magic != MAGIC:
        raise UnityProtocolError("bad frame magic 0x{:08x}".format(magic))
    length = struct.unpack_from("<I", header, 20)[0]
    if length > MAX_PAYLOAD:
        raise UnityProtocolError("frame too large: {}".format(length))
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(min(1 << 16, length - len(payload)))
        if not chunk:
            # The peer closed mid-frame. Do not return a short frame, which
            # would silently truncate profiler.raw while still looking valid.
            raise UnityProtocolError("frame payload truncated")
        payload += chunk
    return Frame(header[4:20], payload)


def build_packet(guid_hex, payload=b""):
    return (
        struct.pack("<I", MAGIC)
        + guid_from_hex(guid_hex)
        + struct.pack("<I", len(payload))
        + payload
    )


class UnityPlayerInfo(object):
    def __init__(self):
        self.version = None
        self.project = ""
        self.player_id = ""
        self.raw = ""
        self.port = None

    @property
    def profile(self):
        return get_profile(self.version)

    @property
    def supported(self):
        return self.profile is not None

    @property
    def label(self):
        profile = self.profile
        return profile.label if profile else str(self.version)

    def to_dict(self):
        return {
            "version": self.version,
            "unity": self.label,
            "project": self.project,
            "player_id": self.player_id,
            "port": self.port,
            "supported": self.supported,
            "features": sorted(self.profile.features) if self.profile else [],
        }


def parse_player_info(text, port=None):
    info = UnityPlayerInfo()
    info.raw = text
    info.port = port
    for token in ("Version", "ProjectName", "Id"):
        marker = "[{}]".format(token)
        index = text.find(marker)
        if index < 0:
            continue
        value = text[index + len(marker):].lstrip(" ")
        end = value.find(" [")
        value = value[:end] if end >= 0 else value
        value = value.strip().strip("\x00")
        if token == "Version":
            try:
                info.version = int(value)
            except ValueError:
                info.version = None
        elif token == "ProjectName":
            info.project = value
        else:
            info.player_id = value
    return info


class UnityProtocolSession(object):
    """One player connection shared by the profiler stream and snapshots."""

    def __init__(self, device_id, pkg_name, pid=None):
        self.device_id = device_id
        self.pkg_name = pkg_name
        self.pid = pid
        self.info = UnityPlayerInfo()
        self.profile = None
        self.local_port = None
        self.remote_port = None
        self.sock = None
        self._send_lock = threading.Lock()
        self._reader = None
        self._running = False
        self._raw_file = None
        self._raw_bytes = 0
        self._raw_path = None
        # Attach only streams for the live panel (`_raw_file` is None); the
        # archived stream is opened at the recording start by begin_record.
        self._stream_bytes = 0
        self._snap_lock = threading.Lock()
        self._snap_state = None
        self._error = None
        self._truncated = False
        self.stats_parser = RawStatsParser()
        self.deep_profiling = False
        # None 表示从未发送过 set_enabled_categories，Player 保留自己的默认值。
        self.enabled_categories = None
        self._areas_to_apply = None
        self._area_thread = None

    # ------------------------------------------------------------- adb

    def _shell(self, cmd, timeout=8):
        import subprocess

        try:
            proc = subprocess.run(
                [adb.adb_path, "-s", self.device_id, "shell", cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
            return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()
        except Exception as e:
            return -1, str(e)

    def _forward(self, remote_port):
        import subprocess

        proc = subprocess.run(
            [adb.adb_path, "-s", self.device_id, "forward", "tcp:0",
             "tcp:{}".format(remote_port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
        out = proc.stdout.decode("utf-8", "replace").strip()
        if proc.returncode != 0 or not out.isdigit():
            raise UnityProtocolError("adb forward failed: {}".format(out))
        return int(out)

    def _unforward(self):
        import subprocess

        if self.local_port:
            subprocess.run(
                [adb.adb_path, "-s", self.device_id, "forward", "--remove",
                 "tcp:{}".format(self.local_port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            self.local_port = None

    def _find_pid(self):
        if self.pid:
            return str(self.pid)
        code, out = self._shell("pidof {}".format(self.pkg_name))
        return out.split()[0] if code == 0 and out.split() else None

    def _find_port(self):
        pid = self._find_pid()
        uid = None
        if pid:
            code, out = self._shell("stat -c %u /proc/{}".format(pid))
            if code == 0 and out.isdigit():
                uid = int(out)
        code, table = self._shell("cat /proc/net/tcp /proc/net/tcp6")
        candidates = []
        if code == 0:
            for line in table.splitlines()[1:]:
                fields = line.split()
                if len(fields) < 8 or fields[3] != "0A":
                    continue
                try:
                    port = int(fields[1].rsplit(":", 1)[1], 16)
                    socket_uid = int(fields[7])
                except (ValueError, IndexError):
                    continue
                if port not in PROFILER_PORT_RANGE:
                    continue
                if uid is not None:
                    if socket_uid != uid:
                        continue
                elif not (55000 <= port <= 55010):
                    continue
                candidates.append(port)
        if candidates:
            base = [p for p in candidates if 55000 <= p <= 55010]
            return min(base) if base else min(candidates)
        return 55000

    # ---------------------------------------------------------- attach

    def attach(self):
        """Find the player, connect, read PlayerInfo and resolve the profile."""
        self.remote_port = self._find_port()
        self.local_port = self._forward(self.remote_port)
        try:
            self.sock = socket.create_connection(
                ("127.0.0.1", self.local_port), timeout=5
            )
        except OSError as e:
            self._unforward()
            raise UnityProtocolError(
                "cannot connect to Unity player on {}: {}".format(self.remote_port, e)
            )
        deadline = time.time() + 5
        frames = 0
        try:
            while time.time() < deadline and frames < 4:
                frame = read_frame(self.sock, max(0.2, deadline - time.time()))
                if frame is None:
                    break
                frames += 1
                text = frame.payload.decode("utf-8", "replace")
                if "[IP]" in text or "[Version]" in text:
                    self.info = parse_player_info(text, self.remote_port)
                    break
        except (OSError, UnityProtocolError) as e:
            self.close()
            raise UnityProtocolError("handshake failed: {}".format(e))
        if self.info.version is None:
            self.close()
            raise UnityProtocolError("no PlayerInfo received from Unity player")
        self.profile = get_profile(self.info.version)
        if self.profile is None:
            self.close()
            raise UnsupportedUnityVersion(self.info.version)
        logger.info(
            "[Unity] player {} (connection version {}) project={} port={}".format(
                self.profile.label, self.info.version, self.info.project,
                self.remote_port,
            )
        )
        return self.info

    # --------------------------------------------------------- profiler

    def set_deep_profiling(self, enabled):
        """Toggle Deep Profiling on the player.

        Payload is a single little-endian uint32 (0/1). The build must have
        "Deep Profiling Support" enabled, otherwise the player ignores it.
        """
        if self.profile is None:
            raise UnityProtocolError("not attached to a Unity player")
        if not self.profile.supports(FEATURE_DEEP_PROFILING):
            raise UnityProtocolError(
                "Unity {} profile does not support deep profiling".format(
                    self.profile.label
                )
            )
        enabled = bool(enabled)
        self._send(
            build_packet(
                self.profile.message("profiler", "set_deep_profiler"),
                struct.pack("<I", 1 if enabled else 0),
            )
        )
        self.deep_profiling = enabled
        logger.info("[Unity] deep profiling {}".format("on" if enabled else "off"))

    def set_enabled_categories(self, categories):
        """Replace the player's enabled profiler category set.

        The native handler parses a NUL-separated list of ``ProfilerCategory``
        names and enables exactly those categories. It is a full replacement,
        so callers must always pass the complete desired set. ``None`` means
        "leave the player default untouched".
        """
        if self.profile is None:
            raise UnityProtocolError("not attached to a Unity player")
        if not self.profile.supports(FEATURE_ENABLED_CATEGORIES):
            raise UnityProtocolError(
                "Unity {} profile does not support profiler categories".format(
                    self.profile.label
                )
            )
        if categories is None:
            return
        names = []
        for name in categories:
            name = (name or "").strip()
            if name and name not in names:
                names.append(name)
        payload = b"".join(name.encode("utf-8") + b"\x00" for name in names)
        self._send(
            build_packet(
                self.profile.message("profiler", "set_enabled_categories"),
                payload,
            )
        )
        self.enabled_categories = names
        logger.info(
            "[Unity] enabled categories: {}".format(
                ", ".join(names) if names else "(none)"
            )
        )

    def set_areas(self, areas):
        """Translate ProfilerArea names and apply their categories live."""
        if self.profile is None:
            raise UnityProtocolError("not attached to a Unity player")
        normalized = self.profile.normalize_areas(areas)
        if normalized is None:
            return None
        categories = self.profile.categories_for_areas(normalized)
        self.set_enabled_categories(categories)
        return categories

    def start_profiler(self, raw_path=None, deep_profiling=False, areas=None):
        """Enable capture and optionally stream profiler data into raw_path.

        ``raw_path=None`` starts the stream for the live counters only (the
        state used after attaching). Beginning a recording re-opens a fresh
        stream so ``profiler.raw`` starts with its own PD3U header and marker
        metadata; without that header the hotspot analysis cannot resolve
        marker names.

        ``areas`` is a list of ``ProfilerArea`` names. ``None`` keeps the
        player's default category set; an empty list disables all categories.
        """
        if self.profile is None:
            raise UnityProtocolError("not attached to a Unity player")
        if not self.profile.supports(FEATURE_PROFILER_STREAM):
            raise UnityProtocolError(
                "Unity {} profile does not expose a profiler stream".format(
                    self.profile.label
                )
            )
        if self.profile.supports(FEATURE_DEEP_PROFILING):
            self.set_deep_profiling(bool(deep_profiling))
        self._areas_to_apply = None
        self.enabled_categories = None
        if areas is not None:
            self._areas_to_apply = list(areas)
            # Best effort before the stream starts; the player may not have
            # registered its categories yet, so it is re-applied after the
            # first profiler frame below.
            self.set_areas(self._areas_to_apply)
        self._raw_path = raw_path
        self._stream_bytes = 0
        if raw_path:
            os.makedirs(os.path.dirname(raw_path), exist_ok=True)
            self._raw_file = open(raw_path, "wb")
            self._raw_bytes = 0
        else:
            self._raw_file = None
            self._raw_bytes = 0
        self._running = True
        self._send(
            build_packet(
                self.profile.message("profiler", "enable_capture"),
                struct.pack("<I", 1),
            )
        )
        self._reader = threading.Thread(
            target=self._reader_loop, name="unity-profiler"
        )
        self._reader.daemon = True
        self._reader.start()
        if self._areas_to_apply is not None:
            self._area_thread = threading.Thread(
                target=self._apply_areas_when_ready, name="unity-areas"
            )
            self._area_thread.daemon = True
            self._area_thread.start()

    def _apply_areas_when_ready(self):
        """Re-apply category state once the player has emitted categories.

        At attach time the player's category registry can still be empty; the
        first profiler frames register the built-in categories. Sending the
        list again then makes the selection effective for the whole capture.
        """
        deadline = time.time() + 3.0
        while (
            self._running
            and self._stream_bytes == 0
            and time.time() < deadline
        ):
            time.sleep(0.05)
        if not self._running or self._areas_to_apply is None:
            return
        try:
            self.set_areas(self._areas_to_apply)
        except (OSError, UnityProtocolError) as e:
            self._error = str(e)
            logger.warning("[Unity] cannot apply profiler areas: {}".format(e))

    def begin_record(self, raw_path, deep_profiling=None, areas=None):
        """Re-open the profiler stream so the archive starts at Start.

        Attaching keeps a stream alive for the live panel without writing a
        file. Beginning a recording restarts capture: Unity sends a fresh
        PD3U header, sampler/thread metadata and frames, so ``profiler.raw``
        starts at the recording start without losing marker names.
        """
        if not self._running:
            return
        if self._raw_file is not None:
            return
        self.stop_profiler()
        # The restarted stream begins with a new header; reuse the player's
        # current options so Deep Profiler / Categories stay as configured.
        self.stats_parser = RawStatsParser()
        self.start_profiler(
            raw_path,
            deep_profiling=self.deep_profiling if deep_profiling is None else deep_profiling,
            areas=self.areas if areas is None else areas,
        )

    def stop_profiler(self):
        if not self._running:
            return
        self._areas_to_apply = None
        try:
            self._send(
                build_packet(
                    self.profile.message("profiler", "enable_capture"),
                    struct.pack("<I", 0),
                )
            )
        except OSError:
            pass
        time.sleep(0.8)
        self._running = False
        if self._area_thread:
            self._area_thread.join(timeout=1)
            self._area_thread = None
        if self._reader:
            self._reader.join(timeout=3)
        if self._raw_file:
            try:
                self._raw_file.flush()
                self._raw_file.close()
            except OSError:
                pass
            self._raw_file = None

    # --------------------------------------------------------- snapshot

    def take_snapshot(self, path, flags=DEFAULT_CAPTURE_FLAGS, timeout=300):
        if not self.sock:
            raise UnityProtocolError("not connected")
        if not self.profile.supports(FEATURE_MEMORY_SNAPSHOT):
            raise UnityProtocolError(
                "Unity {} profile does not expose MemoryProfiler snapshots".format(
                    self.profile.label
                )
            )
        with self._snap_lock:
            self._snap_state = {
                "path": path,
                "buffer": bytearray(),
                "done": threading.Event(),
                "error": None,
                "chunks": 0,
            }
            state = self._snap_state
        self._send(
            build_packet(
                self.profile.message("snapshot", "request"),
                struct.pack("<I", flags),
            )
        )
        if not state["done"].wait(timeout):
            with self._snap_lock:
                self._snap_state = None
            raise UnityProtocolError("snapshot timed out")
        if state["error"]:
            raise UnityProtocolError(state["error"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fp:
            fp.write(state["buffer"])
        return {"path": path, "size": len(state["buffer"]), "chunks": state["chunks"]}

    # ------------------------------------------------------- transport

    def _send(self, packet):
        with self._send_lock:
            if not self.sock:
                raise UnityProtocolError("not connected")
            self.sock.sendall(packet)

    def _reader_loop(self):
        import select

        profile = self.profile
        data_guid = guid_from_hex(profile.message("profiler", "data"))
        begin = guid_from_hex(profile.message("snapshot", "begin"))
        chunk = guid_from_hex(profile.message("snapshot", "chunk"))
        end = guid_from_hex(profile.message("snapshot", "end"))
        terminate = guid_from_hex(profile.message("snapshot", "terminate"))
        while self._running:
            try:
                ready, _, _ = select.select([self.sock], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                frame = read_frame(self.sock, 10)
            except (OSError, UnityProtocolError) as e:
                if self._running:
                    self._error = str(e)
                    self._truncated = True
                    logger.warning("[Unity] stream stopped: {}".format(e))
                break
            if frame is None:
                if self._running:
                    self._truncated = True
                break
            if frame.guid == data_guid:
                payload = frame.payload
                self._stream_bytes += len(payload)
                if self._raw_file is not None:
                    try:
                        self._raw_file.write(payload)
                        self._raw_bytes += len(payload)
                    except (OSError, ValueError) as e:
                        self._error = str(e)
                        self._raw_file = None
                try:
                    self.stats_parser.feed(payload)
                except Exception as e:
                    # Parsing must never stop the capture stream.
                    logger.warning("[Unity] stats parse failed: {}".format(e))
            elif frame.guid in (begin, chunk, end, terminate):
                self._on_snapshot_frame(
                    frame.guid, frame.payload, begin, chunk, end, terminate
                )
        self._running = False

    def _on_snapshot_frame(self, guid, payload, begin, chunk, end, terminate):
        with self._snap_lock:
            state = self._snap_state
            if state is None:
                return
            if guid == begin:
                state["buffer"] = bytearray()
            elif guid == chunk:
                state["buffer"] += payload
                state["chunks"] += 1
            elif guid in (end, terminate):
                state["done"].set()

    # ------------------------------------------------------------ close

    def close(self):
        self.stop_profiler()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self._unforward()

    def stats_snapshot(self):
        return self.stats_parser.snapshot()

    @property
    def raw_bytes(self):
        return self._raw_bytes

    @property
    def raw_path(self):
        return self._raw_path

    @property
    def error(self):
        return self._error

    @property
    def raw_complete(self):
        """True when the profiler stream ended cleanly, False when truncated."""
        return not self._truncated
