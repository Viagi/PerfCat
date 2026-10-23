# -*- coding: utf-8 -*-
"""增量解析 Unity PlayerConnection 的原始 profiler 流，提取面板所需的计数器。

原始流格式（Unity 2022.2+，已在 2022.3.62f2 上验证）::

    SessionHeader
    BlockHeader (20 bytes) + message payload + BlockFooter (8 bytes)
    ...

每条 message 以 uint16 类型开头，没有单条长度字段，因此只能按类型逐条
解析。这里只实现到 counter 提取所需的程度：

- 全局消息：线程表、SamplerInfo（marker 名称、counter 标记）
- 线程消息：Frame、Begin/EndSample、SampleWithMetadata

Draw Calls / Batches / Triangles / Vertices / SetPass 由 Render Thread 上报，
CPU/GPU 帧耗时、GC、内存由 Main Thread 上报。为了控制 CPU 开销，识别出
这两个线程后，其余线程的 block 只做 framing，不再解析消息。

解析失败时直接丢弃当前 block（block 长度是可靠的），继续处理下一个 block，
因此不会因为个别未知消息类型导致整个流不可用。
"""
import struct
import time

from logzero import logger

SESSION_SIGNATURE = 0x55334450  # "PD3U"
BLOCK_SIGNATURE = 0xB10C7EAD
FOOTER_SIGNATURE = 0xB10CF007
GLOBAL_THREAD = 0xFFFFFFFFFFFFFFFF

# 这些计数器都来自 Render Thread
RENDER_COUNTERS = {
    "Draw Calls Count": ("draw_calls", 1.0),
    "Batches Count": ("batches", 1.0),
    "SetPass Calls Count": ("setpass_calls", 1.0),
    "Triangles Count": ("triangles", 1.0),
    "Vertices Count": ("vertices", 1.0),
    "Shadow Casters Count": ("shadow_casters", 1.0),
}

# 这些计数器来自 Main Thread
MAIN_COUNTERS = {
    "CPU Main Thread Frame Time": ("cpu_frame_ms", 1e-6),
    "CPU Render Thread Frame Time": ("render_frame_ms", 1e-6),
    "CPU Total Frame Time": ("cpu_total_ms", 1e-6),
    "GPU Frame Time": ("gpu_frame_ms", 1e-6),
    "GC Allocated In Frame": ("gc_alloc_kb", 1.0 / 1024.0),
    "Total Used Memory": ("used_memory_mb", 1.0 / 1048576.0),
    "GC Used Memory": ("gc_used_memory_mb", 1.0 / 1048576.0),
}

COUNTER_NAMES = dict(RENDER_COUNTERS)
COUNTER_NAMES.update(MAIN_COUNTERS)

# 面板展示顺序，也用于 missing 列表
VALUE_ORDER = (
    "cpu_frame_ms",
    "gpu_frame_ms",
    "draw_calls",
    "setpass_calls",
    "batches",
    "triangles",
    "vertices",
    "gc_alloc_kb",
    "used_memory_mb",
    "render_frame_ms",
    "cpu_total_ms",
    "shadow_casters",
    "gc_used_memory_mb",
)

# 单位换算后的小数位
DECIMALS = {
    "cpu_frame_ms": 2,
    "gpu_frame_ms": 2,
    "render_frame_ms": 2,
    "cpu_total_ms": 2,
    "gc_alloc_kb": 2,
    "used_memory_mb": 2,
    "gc_used_memory_mb": 2,
    "draw_calls": 0,
    "setpass_calls": 0,
    "batches": 0,
    "triangles": 0,
    "vertices": 0,
    "shadow_casters": 0,
}

SUPPORTED_VERSION = 539099944  # Unity 2022.2+ raw stream version

# message types
MSG_PROFILER_STATE = 0
MSG_SAMPLER_INFO = 1
MSG_CALLSTACK = 3
MSG_ALL_PROFILER_STATS = 4
MSG_AUDIO_INSTANCE_DATA = 5
MSG_UI_SYSTEM_CANVAS = 6
MSG_UI_EVENTS = 7
MSG_METHOD_JIT_INFO = 8
MSG_UNITY_NATIVE_TYPE_INFO = 10
MSG_UNITY_OBJECT_INFO = 11
MSG_GFX_RESOURCE_INFO = 12
MSG_THREAD_INFO = 33
MSG_FRAME = 34
MSG_BEGIN_SAMPLE = 36
MSG_END_SAMPLE = 37
MSG_SAMPLE = 38
MSG_BEGIN_SAMPLE_WITH_INSTANCE_ID = 39
MSG_END_SAMPLE_WITH_INSTANCE_ID = 40
MSG_SAMPLE_WITH_INSTANCE_ID = 41
MSG_BEGIN_SAMPLE_WITH_METADATA = 42
MSG_END_SAMPLE_WITH_METADATA = 43
MSG_SAMPLE_WITH_METADATA = 44
MSG_GC_ALLOC = 52
MSG_LOCAL_ASYNC_METADATA_ANCHOR = 53
MSG_LOCAL_GPU_SAMPLE = 55
MSG_CLEANUP_THREAD = 56
MSG_FLOW_EVENT = 57
MSG_CATEGORY_INFO = 62
MSG_CATEGORY_STATE = 63

# metadata value types
META_NONE = 0
META_INSTANCE_ID = 1
META_INT32 = 2
META_UINT32 = 3
META_INT64 = 4
META_UINT64 = 5
META_FLOAT = 6
META_DOUBLE = 7
META_STRING = 8
META_STRING16 = 9
META_VEC3 = 10
META_BLOB8 = 11
META_GFX_RESOURCE_ID = 12
META_ENTITY_ID = 13
META_COUNT = 14


class _Incomplete(Exception):
    """当前 block 的数据不够解析完一条消息。"""


class _UnknownMessage(Exception):
    def __init__(self, message_type):
        self.message_type = message_type


class RawStatsParser(object):
    """Incremental parser for the raw PlayerConnection profiler stream."""

    def __init__(self):
        self._buffer = bytearray()
        self._header_done = False
        self._header_error = None
        self.version = None
        self.unity_version = None
        self.align_enabled = False
        self._samplers = {}
        self._thread_names = {}
        self._wanted_threads = set()
        self._has_main_thread = False
        self._has_render_thread = False
        self._filtering = False
        self._latest = {}
        self._latest_markers = {}
        self._latest_frame = None
        self._latest_sample_time = None
        self._last_update = None
        self._last_error = None
        self._blocks = 0
        self._parsed_blocks = 0
        self._skipped_blocks = 0
        self._bad_blocks = 0
        self._partial_blocks = 0
        self._lost_bytes = 0
        self._frames = 0

    # ------------------------------------------------------------- feed

    def feed(self, data):
        if not data:
            return
        self._buffer += data
        try:
            if not self._header_done:
                self._parse_header()
            if not self._header_done:
                return
            self._parse_blocks()
        except Exception as e:  # never break the capture thread
            self._header_error = str(e)
            logger.warning("[Unity] raw stats parser error: {}".format(e))
            self._buffer.clear()

    def _parse_header(self):
        if len(self._buffer) < 36:
            return
        signature, little, align, platform, version, num, den, main = struct.unpack_from(
            "<IBBH I Q Q Q", self._buffer, 0
        )
        if signature != SESSION_SIGNATURE:
            self._header_done = True
            self._header_error = "not a PD3U stream"
            self._buffer.clear()
            return
        if version < SUPPORTED_VERSION:
            self._header_done = True
            self._header_error = "unsupported_versions 0x{:x}".format(version)
            self._buffer.clear()
            return
        offset = 36
        unity_version = None
        if version >= SUPPORTED_VERSION:
            if len(self._buffer) < offset + 20:
                return
            unity_version = struct.unpack_from("<5i", self._buffer, offset)
            offset += 20
        self.version = version
        self.unity_version = ".".join(str(item) for item in unity_version[:3])
        self.align_enabled = bool(align)
        self._header_done = True
        self._buffer = self._buffer[offset:]
        logger.info(
            "[Unity] raw stats parser attached: version=0x{:x} ({})".format(
                version, self.unity_version
            )
        )

    def _parse_blocks(self):
        buf = self._buffer
        while True:
            if len(buf) < 20:
                return
            signature, block_id, thread_id, length = struct.unpack_from("<IIQI", buf, 0)
            if signature != BLOCK_SIGNATURE:
                self._resync(buf)
                return
            if length > 16 * 1024 * 1024:
                # Corrupted length: drop the header and resync.
                del buf[:4]
                self._bad_blocks += 1
                continue
            end = 20 + length
            if len(buf) < end + 8:
                return
            footer_next, footer_sig = struct.unpack_from("<II", buf, end)
            if footer_sig != FOOTER_SIGNATURE:
                # Drop this header and resync on the next signature.
                del buf[:4]
                self._bad_blocks += 1
                continue
            block = memoryview(buf)[20:end]
            self._blocks += 1
            try:
                self._parse_block(thread_id, block)
            finally:
                block.release()
            del buf[:end + 8]
            if not buf:
                return

    def _resync(self, buf):
        index = buf.find(struct.pack("<I", BLOCK_SIGNATURE))
        if index < 0:
            keep = min(len(buf), 3)
            dropped = len(buf) - keep
            if dropped:
                del buf[:dropped]
                self._lost_bytes += dropped
            return
        if index:
            self._lost_bytes += index
            del buf[:index]

    # ------------------------------------------------------------ block

    def _parse_block(self, thread_id, block):
        if thread_id == GLOBAL_THREAD:
            self._parsed_blocks += 1
            self._parse_messages(GLOBAL_THREAD, block)
            return
        if self._filtering and thread_id not in self._wanted_threads:
            self._skipped_blocks += 1
            return
        self._parsed_blocks += 1
        self._parse_messages(thread_id, block)

    def _parse_messages(self, thread_id, data):
        pos = 0
        size = len(data)
        unpack_from = struct.unpack_from
        align = self.align_enabled
        advanced = False
        try:
            while pos < size:
                if pos + 2 > size:
                    raise _Incomplete()
                message_type = unpack_from("<H", data, pos)[0]
                pos += 2
                if align:
                    pos = (pos + 3) & ~3
                advanced = True

                if message_type in (
                    MSG_BEGIN_SAMPLE,
                    MSG_END_SAMPLE,
                    MSG_SAMPLE,
                ):
                    if pos + 13 > size:
                        raise _Incomplete()
                    # flags (1) + id (4) + time (8)
                    pos += 1
                    if align:
                        pos = (pos + 3) & ~3
                    pos += 12
                elif message_type in (
                    MSG_BEGIN_SAMPLE_WITH_INSTANCE_ID,
                    MSG_SAMPLE_WITH_INSTANCE_ID,
                ):
                    if pos + 17 > size:
                        raise _Incomplete()
                    pos += 1
                    if align:
                        pos = (pos + 3) & ~3
                    pos += 16
                elif message_type == MSG_END_SAMPLE_WITH_INSTANCE_ID:
                    if pos + 17 > size:
                        raise _Incomplete()
                    pos += 1
                    if align:
                        pos = (pos + 3) & ~3
                    pos += 16
                elif message_type in (
                    MSG_BEGIN_SAMPLE_WITH_METADATA,
                    MSG_SAMPLE_WITH_METADATA,
                ):
                    pos = self._read_sample_with_metadata(thread_id, data, pos)
                elif message_type == MSG_FRAME:
                    if pos + 12 > size:
                        raise _Incomplete()
                    frame_index = unpack_from("<I", data, pos)[0]
                    pos += 12
                    self._latest_frame = frame_index
                    self._frames += 1
                elif message_type == MSG_THREAD_INFO:
                    pos = self._read_thread_info(data, pos)
                elif message_type == MSG_SAMPLER_INFO:
                    pos = self._read_sampler_info(data, pos)
                elif message_type == MSG_PROFILER_STATE:
                    if pos + 16 > size:
                        raise _Incomplete()
                    pos += 16
                elif message_type == MSG_METHOD_JIT_INFO:
                    if pos + 20 > size:
                        raise _Incomplete()
                    pos += 20
                    pos = self._skip_string(data, pos)
                    pos = self._skip_string(data, pos)
                    if pos + 4 > size:
                        raise _Incomplete()
                    pos += 4
                elif message_type == MSG_CATEGORY_INFO:
                    if pos + 6 > size:
                        raise _Incomplete()
                    pos += 6
                    pos = self._skip_string(data, pos)
                elif message_type == MSG_CATEGORY_STATE:
                    if pos + 4 > size:
                        raise _Incomplete()
                    pos += 4
                elif message_type == MSG_UNITY_NATIVE_TYPE_INFO:
                    if pos + 8 > size:
                        raise _Incomplete()
                    pos += 8
                    pos = self._skip_string(data, pos)
                elif message_type == MSG_UNITY_OBJECT_INFO:
                    if pos + 8 > size:
                        raise _Incomplete()
                    pos += 8
                    pos = self._skip_string(data, pos)
                    if pos + 24 > size:
                        raise _Incomplete()
                    pos += 24
                elif message_type == MSG_GFX_RESOURCE_INFO:
                    if pos + 24 > size:
                        raise _Incomplete()
                    pos += 24
                elif message_type == MSG_GC_ALLOC:
                    if pos + 4 > size:
                        raise _Incomplete()
                    pos += 4
                elif message_type == MSG_CALLSTACK:
                    if pos + 5 > size:
                        raise _Incomplete()
                    frames = data[pos + 4]
                    pos += 5 + 8 * frames
                    if pos > size:
                        raise _Incomplete()
                elif message_type == MSG_LOCAL_ASYNC_METADATA_ANCHOR:
                    if pos + 4 > size:
                        raise _Incomplete()
                    pos += 4
                elif message_type == MSG_LOCAL_GPU_SAMPLE:
                    if pos + 9 > size:
                        raise _Incomplete()
                    pos += 9
                    if align:
                        pos = (pos + 3) & ~3
                elif message_type == MSG_CLEANUP_THREAD:
                    if pos + 8 > size:
                        raise _Incomplete()
                    pos += 8
                elif message_type == MSG_FLOW_EVENT:
                    if pos + 5 > size:
                        raise _Incomplete()
                    pos += 1
                    if align:
                        pos = (pos + 3) & ~3
                    pos += 4
                elif message_type == MSG_ALL_PROFILER_STATS:
                    if pos + 8 > size:
                        raise _Incomplete()
                    pos += 4
                    count = unpack_from("<i", data, pos)[0]
                    pos += 4
                    if count < 0 or pos + 4 * count > size:
                        raise _Incomplete()
                    pos += 4 * count
                elif message_type == MSG_AUDIO_INSTANCE_DATA:
                    for _ in range(3):
                        if pos + 4 > size:
                            raise _Incomplete()
                        count = unpack_from("<i", data, pos)[0]
                        pos += 4
                        if count < 0 or pos + 4 * count > size:
                            raise _Incomplete()
                        pos += 4 * count
                    pos = self._skip_string(data, pos)
                elif message_type in (MSG_UI_SYSTEM_CANVAS, MSG_UI_EVENTS):
                    count = 2 if message_type == MSG_UI_SYSTEM_CANVAS else 3
                    for _ in range(count):
                        if pos + 4 > size:
                            raise _Incomplete()
                        length = unpack_from("<i", data, pos)[0]
                        pos += 4
                        if length < 0 or pos + length > size:
                            raise _Incomplete()
                        pos += length
                        if align:
                            pos = (pos + 3) & ~3
                else:
                    raise _UnknownMessage(message_type)
        except _Incomplete:
            self._partial_blocks += 1
            return
        except _UnknownMessage as e:
            self._bad_blocks += 1
            self._last_error = "unknown message {}".format(e.message_type)
            return
        except Exception as e:
            self._bad_blocks += 1
            self._last_error = str(e)
            return

    # ---------------------------------------------------------- readers

    def _skip_string(self, data, pos):
        if pos + 4 > len(data):
            raise _Incomplete()
        length = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if length > 0x100000:
            raise _Incomplete()
        pos += length
        if pos > len(data):
            raise _Incomplete()
        if self.align_enabled:
            pos = (pos + 3) & ~3
        return pos

    def _read_thread_info(self, data, pos):
        size = len(data)
        if pos + 8 > size:
            raise _Incomplete()
        thread_id = struct.unpack_from("<Q", data, pos)[0]
        pos += 8
        if self.version >= 538444067:
            if pos + 8 > size:
                raise _Incomplete()
            pos += 8
        if pos + 4 > size:
            raise _Incomplete()
        pos += 4
        pos = self._skip_string(data, pos)
        name_pos = pos
        pos = self._skip_string(data, pos)
        name = self._read_string(data, name_pos)
        if thread_id not in self._thread_names:
            self._thread_names[thread_id] = name
            is_main = name == "Main Thread"
            is_render = ("Render" in name) or ("Gfx" in name)
            if is_main:
                self._has_main_thread = True
            if is_render:
                self._has_render_thread = True
            if is_main or is_render:
                self._wanted_threads.add(thread_id)
            if self._has_main_thread and self._has_render_thread:
                self._filtering = True
        return pos

    def _read_string(self, data, pos):
        length = struct.unpack_from("<I", data, pos)[0]
        start = pos + 4
        return bytes(data[start:start + length]).decode("utf-8", "replace")

    def _read_sampler_info(self, data, pos):
        size = len(data)
        if pos + 8 > size:
            raise _Incomplete()
        sampler_id = struct.unpack_from("<I", data, pos)[0]
        flags = struct.unpack_from("<H", data, pos + 4)[0]
        name_pos = pos + 8
        pos = self._skip_string(data, name_pos)
        if pos + 1 > size:
            raise _Incomplete()
        count = data[pos]
        pos += 1
        if self.align_enabled:
            pos = (pos + 3) & ~3
        for _ in range(count):
            if pos + 2 > size:
                raise _Incomplete()
            pos += 2
            if self.align_enabled:
                pos = (pos + 3) & ~3
            pos = self._skip_string(data, pos)
        if flags & 0x80:
            name = self._read_string(data, name_pos)
            self._samplers[sampler_id] = name
        return pos

    def _read_sample_with_metadata(self, thread_id, data, pos):
        size = len(data)
        if pos + 15 > size:
            raise _Incomplete()
        flags = data[pos]
        pos += 1
        if self.align_enabled:
            pos = (pos + 3) & ~3
        sampler_id = struct.unpack_from("<I", data, pos)[0]
        sample_time = struct.unpack_from("<Q", data, pos + 4)[0]
        pos += 12
        count = data[pos]
        pos += 1
        if self.align_enabled:
            pos = (pos + 3) & ~3

        value = None
        for index in range(count):
            if pos + 1 > size:
                raise _Incomplete()
            meta_type = data[pos]
            pos += 1
            if self.align_enabled:
                pos = (pos + 3) & ~3
            pos, meta_value = self._read_metadata_value(data, pos, meta_type)
            if index == 0:
                value = meta_value

        if value is not None:
            name = self._samplers.get(sampler_id)
            if name:
                self._store_counter(name, value, sample_time)
        return pos

    def _read_metadata_value(self, data, pos, meta_type):
        size = len(data)
        if meta_type == META_NONE:
            return pos, None
        if meta_type in (META_INSTANCE_ID, META_INT32):
            if pos + 4 > size:
                raise _Incomplete()
            return pos + 4, struct.unpack_from("<i", data, pos)[0]
        if meta_type in (META_UINT32, META_GFX_RESOURCE_ID, META_ENTITY_ID):
            if pos + 4 > size:
                raise _Incomplete()
            return pos + 4, struct.unpack_from("<I", data, pos)[0]
        if meta_type == META_INT64:
            if pos + 8 > size:
                raise _Incomplete()
            return pos + 8, struct.unpack_from("<q", data, pos)[0]
        if meta_type == META_UINT64:
            if pos + 8 > size:
                raise _Incomplete()
            return pos + 8, struct.unpack_from("<Q", data, pos)[0]
        if meta_type == META_FLOAT:
            if pos + 4 > size:
                raise _Incomplete()
            return pos + 4, struct.unpack_from("<f", data, pos)[0]
        if meta_type == META_DOUBLE:
            if pos + 8 > size:
                raise _Incomplete()
            return pos + 8, struct.unpack_from("<d", data, pos)[0]
        if meta_type in (META_STRING, META_STRING16, META_BLOB8):
            if pos + 4 > size:
                raise _Incomplete()
            length = struct.unpack_from("<i", data, pos)[0]
            pos += 4
            if length < 0 or pos + length > size:
                raise _Incomplete()
            pos += length
            if self.align_enabled:
                pos = (pos + 3) & ~3
            return pos, None
        if meta_type == META_VEC3:
            if pos + 12 > size:
                raise _Incomplete()
            return pos + 12, None
        if meta_type == META_COUNT:
            return pos, None
        raise _Incomplete()

    # ---------------------------------------------------------- counter

    def _store_counter(self, name, value, sample_time):
        mapping = COUNTER_NAMES.get(name)
        if not mapping:
            return
        key, scale = mapping
        try:
            number = float(value) * scale
        except (TypeError, ValueError):
            return
        if key in DECIMALS and DECIMALS[key] == 0:
            number = int(round(number))
        else:
            number = round(number, 2)
        self._latest[key] = number
        self._latest_markers[key] = name
        if sample_time is not None:
            self._latest_sample_time = sample_time
        self._last_update = time.time()

    # --------------------------------------------------------- snapshot

    def snapshot(self):
        available = bool(self._latest)
        missing = [key for key in VALUE_ORDER if key not in self._latest]
        reason = None
        if self._header_error:
            reason = "unsupported_version" if "unsupported" in self._header_error else "parse_error"
        elif not self._header_done:
            reason = "not_started"
        elif not available:
            reason = "no_markers"
        data = {
            "available": available,
            "reason": reason,
            "values": dict(self._latest),
            "markers": dict(self._latest_markers),
            "missing": missing,
            "frame": self._latest_frame,
            "unity_version": self.unity_version,
            "stats": {
                "blocks": self._blocks,
                "parsed": self._parsed_blocks,
                "skipped": self._skipped_blocks,
                "bad": self._bad_blocks,
                "partial": self._partial_blocks,
                "lost_bytes": self._lost_bytes,
                "frames": self._frames,
            },
        }
        if self._last_error:
            data["msg"] = self._last_error
        return data

    def close(self):
        self._buffer.clear()


def parse_raw_counters(data):
    """Convenience helper for tests: parse a complete raw stream."""
    parser = RawStatsParser()
    parser.feed(data)
    return parser.snapshot()
