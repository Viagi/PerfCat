# -*- coding: utf-8 -*-
"""Unity raw profiler 单帧热点解析。

报告里的 Unity 热点面板需要三组数据：

- Timeline：每个线程在一帧内的采样色块（start / duration）
- Hierarchy：按调用路径合并后的调用树（Total / Self / Calls / GC）
- Raw Hierarchy：不合并的原始调用树

原始流仍然是 ``PD3U`` block + message。这里只解析重建调用树所需的
消息（ThreadInfo / SamplerInfo / Frame / Begin/EndSample / GCAlloc），
其余消息只做长度跳过。解析时从目标帧的前一帧开始，按时间窗口过滤，
避免渲染线程等异步数据晚到导致丢样。

为了报告打开速度，第一次会扫描整个 raw 文件建立帧索引（帧号 -> block
偏移、主线程时间）和 marker/线程表；之后按偏移 seek，只解析目标帧附近
的数据。索引和帧结果都缓存在进程内。
"""
import os
import struct
import threading

from logzero import logger

from perfcattle.unity import raw_reader as rr


class HotspotError(Exception):
    pass


class _Reader(object):
    __slots__ = ("data", "pos")

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def u8(self):
        value = self.data[self.pos]
        self.pos += 1
        return value

    def u16(self):
        value = struct.unpack_from("<H", self.data, self.pos)[0]
        self.pos += 2
        return value

    def u32(self):
        value = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return value

    def i32(self):
        value = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return value

    def u64(self):
        value = struct.unpack_from("<Q", self.data, self.pos)[0]
        self.pos += 8
        return value

    def i64(self):
        value = struct.unpack_from("<q", self.data, self.pos)[0]
        self.pos += 8
        return value

    def f32(self):
        value = struct.unpack_from("<f", self.data, self.pos)[0]
        self.pos += 4
        return value

    def f64(self):
        value = struct.unpack_from("<d", self.data, self.pos)[0]
        self.pos += 8
        return value

    def string(self):
        length = self.u32()
        start = self.pos
        self.pos += length
        return bytes(self.data[start:self.pos]).decode("utf-8", "replace")

    def skip_string(self):
        length = self.u32()
        self.pos += length
        return length

    def align(self, enabled):
        if enabled:
            self.pos = (self.pos + 3) & ~3


class _Collector(object):
    """Base collector: message parser calls these hooks."""

    def on_frame(self, thread_id, frame_index, frame_time, block_offset):
        pass

    def on_begin(self, thread_id, sampler_id, sample_time, metadata=None):
        pass

    def on_end(self, thread_id, sampler_id, sample_time):
        pass

    def on_gc(self, thread_id, size):
        pass


class _FrameScan(_Collector):
    def __init__(self, main_thread_id):
        self.main_thread_id = main_thread_id
        self.frames = {}
        self.samplers = {}
        self.threads = {}

    def on_frame(self, thread_id, frame_index, frame_time, block_offset):
        entry = self.frames.get(frame_index)
        if entry is None:
            entry = {
                "index": frame_index,
                "offset": block_offset,
                "time": frame_time,
                "main_time": None,
            }
            self.frames[frame_index] = entry
        else:
            if block_offset < entry["offset"]:
                entry["offset"] = block_offset
        if thread_id == self.main_thread_id:
            if entry["main_time"] is None:
                entry["main_time"] = frame_time

    def on_sampler(self, sampler_id, name, flags):
        self.samplers[sampler_id] = (name, flags)

    def on_thread(self, thread_id, name, group):
        self.threads[thread_id] = {"name": name, "group": group}


class _FrameCollector(_Collector):
    def __init__(self, samplers, threads, target, start_frame=None):
        self.target = target
        self.start_frame = target if start_frame is None else start_frame
        self.samplers = samplers
        self.threads = threads
        self.stacks = {}
        self.samples = {}
        self.next_uid = {}
        self.started = False
        self.seen_target = False
        self.stop = False

    def _stack(self, thread_id):
        stack = self.stacks.get(thread_id)
        if stack is None:
            stack = []
            self.stacks[thread_id] = stack
        return stack

    def _sample_list(self, thread_id):
        items = self.samples.get(thread_id)
        if items is None:
            items = []
            self.samples[thread_id] = items
        return items

    def _uid(self, thread_id):
        value = self.next_uid.get(thread_id, 0)
        self.next_uid[thread_id] = value + 1
        return value

    def on_frame(self, thread_id, frame_index, frame_time, block_offset):
        if not self.started and frame_index == self.start_frame:
            self.started = True
            self.samples.clear()
            self.stacks.clear()
        if frame_index == self.target:
            self.seen_target = True
        elif self.seen_target and frame_index > self.target:
            self.stop = True

    def on_begin(self, thread_id, sampler_id, sample_time, metadata=None):
        stack = self._stack(thread_id)
        parent = stack[-1]["uid"] if stack else None
        stack.append(
            {
                "uid": self._uid(thread_id),
                "id": sampler_id,
                "start": sample_time,
                "parent_uid": parent,
                "gc": 0,
                "meta": metadata,
            }
        )

    def on_end(self, thread_id, sampler_id, sample_time):
        stack = self._stack(thread_id)
        index = None
        for pos in range(len(stack) - 1, -1, -1):
            if stack[pos]["id"] == sampler_id:
                index = pos
                break
        if index is None:
            # Block-level partial messages can drop a BeginSample. Keep the
            # stack aligned by closing the innermost open sample.
            if not stack:
                return
            index = len(stack) - 1
        sample = stack[index]
        sample["end"] = sample_time
        sample["dur"] = sample_time - sample["start"]
        self._sample_list(thread_id).append(sample)
        del stack[index:]

    def on_gc(self, thread_id, size):
        stack = self._stack(thread_id)
        if stack:
            stack[-1]["gc"] += size


class _MessageParser(object):
    """Incremental message parser shared by index scan and frame extraction."""

    def __init__(self, version, align):
        self.version = version
        self.align = align

    def parse(self, thread_id, data, collector, block_offset):
        reader = _Reader(data)
        while reader.pos < len(data):
            message_type = reader.u16()
            reader.align(self.align)
            try:
                if not self._message(reader, message_type, thread_id, collector, block_offset):
                    return
            except Exception:
                # A partial metadata blob or a desynchronized block: drop the
                # remainder of this block and continue with the next one.
                return

    def _message(self, r, message_type, thread_id, collector, block_offset):
        if message_type == rr.MSG_PROFILER_STATE:
            r.u32()
            r.u64()
            r.u32()
        elif message_type == rr.MSG_THREAD_INFO:
            tid = r.u64()
            if self.version >= 538444067:
                r.u64()
            r.u32()
            group = r.string()
            r.align(self.align)
            name = r.string()
            r.align(self.align)
            if hasattr(collector, "on_thread"):
                collector.on_thread(tid, name, group)
        elif message_type == rr.MSG_SAMPLER_INFO:
            sampler_id = r.u32()
            flags = r.u16()
            r.u16()
            name = r.string()
            r.align(self.align)
            count = r.u8()
            r.align(self.align)
            for _ in range(count):
                r.u16()
                r.align(self.align)
                r.skip_string()
                r.align(self.align)
            if hasattr(collector, "on_sampler"):
                collector.on_sampler(sampler_id, name, flags)
        elif message_type == rr.MSG_FRAME:
            frame_index = r.u32()
            frame_time = r.u64()
            collector.on_frame(thread_id, frame_index, frame_time, block_offset)
        elif message_type in (rr.MSG_BEGIN_SAMPLE, rr.MSG_SAMPLE):
            r.u8()
            r.align(self.align)
            sampler_id = r.u32()
            sample_time = r.u64()
            collector.on_begin(thread_id, sampler_id, sample_time)
        elif message_type in (
            rr.MSG_BEGIN_SAMPLE_WITH_INSTANCE_ID,
            rr.MSG_SAMPLE_WITH_INSTANCE_ID,
        ):
            r.u8()
            r.align(self.align)
            sampler_id = r.u32()
            sample_time = r.u64()
            r.i32()
            collector.on_begin(thread_id, sampler_id, sample_time)
        elif message_type in (
            rr.MSG_BEGIN_SAMPLE_WITH_METADATA,
            rr.MSG_SAMPLE_WITH_METADATA,
        ):
            r.u8()
            r.align(self.align)
            sampler_id = r.u32()
            sample_time = r.u64()
            count = r.u8()
            r.align(self.align)
            metadata = []
            for _ in range(count):
                metadata.append(self._metadata(r))
            collector.on_begin(thread_id, sampler_id, sample_time, metadata)
        elif message_type in (rr.MSG_END_SAMPLE, rr.MSG_END_SAMPLE_WITH_INSTANCE_ID):
            r.u8()
            r.align(self.align)
            sampler_id = r.u32()
            sample_time = r.u64()
            if message_type == rr.MSG_END_SAMPLE_WITH_INSTANCE_ID:
                r.i32()
            collector.on_end(thread_id, sampler_id, sample_time)
        elif message_type == rr.MSG_GC_ALLOC:
            collector.on_gc(thread_id, r.u32())
        elif message_type == rr.MSG_METHOD_JIT_INFO:
            r.u64()
            r.u64()
            r.u32()
            r.skip_string()
            r.align(self.align)
            r.skip_string()
            r.align(self.align)
            r.u32()
        elif message_type == rr.MSG_CATEGORY_INFO:
            r.u16()
            r.u16()
            r.u16()
            r.skip_string()
            r.align(self.align)
        elif message_type == rr.MSG_CATEGORY_STATE:
            r.u16()
            r.u16()
            r.align(self.align)
        elif message_type == rr.MSG_UNITY_NATIVE_TYPE_INFO:
            r.u32()
            r.u32()
            r.skip_string()
            r.align(self.align)
        elif message_type == rr.MSG_UNITY_OBJECT_INFO:
            r.i32()
            r.i32()
            r.skip_string()
            r.align(self.align)
            r.u32()
            r.i32()
            r.i64()
            r.u64()
        elif message_type == rr.MSG_GFX_RESOURCE_INFO:
            r.u64()
            r.u64()
            r.u64()
        elif message_type == rr.MSG_CALLSTACK:
            r.u32()
            frames = r.u8()
            r.pos += 8 * frames
        elif message_type == rr.MSG_LOCAL_ASYNC_METADATA_ANCHOR:
            r.u32()
        elif message_type == rr.MSG_LOCAL_GPU_SAMPLE:
            r.u32()
            r.u32()
            r.u8()
            r.align(self.align)
        elif message_type == rr.MSG_CLEANUP_THREAD:
            r.u64()
        elif message_type == rr.MSG_FLOW_EVENT:
            r.u8()
            r.align(self.align)
            r.u32()
        elif message_type == rr.MSG_ALL_PROFILER_STATS:
            r.u32()
            count = r.i32()
            if count < 0:
                return False
            r.pos += 4 * count
        elif message_type == rr.MSG_AUDIO_INSTANCE_DATA:
            for _ in range(3):
                count = r.i32()
                if count < 0:
                    return False
                r.pos += 4 * count
            r.skip_string()
            r.align(self.align)
        elif message_type in (rr.MSG_UI_SYSTEM_CANVAS, rr.MSG_UI_EVENTS):
            count = 2 if message_type == rr.MSG_UI_SYSTEM_CANVAS else 3
            for _ in range(count):
                length = r.i32()
                if length < 0:
                    return False
                r.pos += length
                r.align(self.align)
        else:
            return False
        if r.pos > len(r.data):
            return False
        return True

    def _metadata(self, r):
        meta_type = r.u8()
        r.align(self.align)
        if meta_type in (rr.META_INSTANCE_ID, rr.META_INT32):
            return r.i32()
        if meta_type in (rr.META_UINT32, rr.META_GFX_RESOURCE_ID, rr.META_ENTITY_ID):
            return r.u32()
        if meta_type == rr.META_INT64:
            return r.i64()
        if meta_type == rr.META_UINT64:
            return r.u64()
        if meta_type == rr.META_FLOAT:
            return r.f32()
        if meta_type == rr.META_DOUBLE:
            return r.f64()
        if meta_type in (rr.META_STRING, rr.META_STRING16, rr.META_BLOB8):
            length = r.i32()
            if length < 0:
                raise ValueError("bad metadata length")
            r.pos += length
            r.align(self.align)
            return None
        if meta_type == rr.META_VEC3:
            r.pos += 12
            return None
        if meta_type in (rr.META_NONE, rr.META_COUNT):
            return None
        raise ValueError("unknown metadata type {}".format(meta_type))


class _ThreadAggregator(object):
    """Turns filtered flat samples into raw tree and merged hierarchy."""

    def __init__(self, samples, samplers):
        self.samples = samples
        self.samplers = samplers

    def build(self):
        children = {}
        roots = []
        for index, sample in enumerate(self.samples):
            sample["index"] = index
            sample["children"] = []
            sample["self"] = sample["dur"]
            parent = sample.get("parent")
            if parent is None:
                roots.append(index)
            else:
                children.setdefault(parent, []).append(index)

        for parent, items in children.items():
            total_children = 0.0
            for child in items:
                total_children += self.samples[child]["dur"]
            self.samples[parent]["self"] = max(
                0.0, self.samples[parent]["dur"] - total_children
            )
            self.samples[parent]["children"] = items

        raw = [self._raw_node(index) for index in roots]
        merged = self._merged(roots)
        return raw, merged

    def _name(self, sample):
        return self.samplers.get(sample["id"], ("marker_{}".format(sample["id"]), 0))[0]

    def _raw_node(self, index):
        sample = self.samples[index]
        node = {
            "name": self._name(sample),
            "total_us": round(sample["dur"], 1),
            "self_us": round(sample["self"], 1),
            "calls": 1,
            "gc": sample["gc"],
            "children": [],
        }
        for child in sample["children"]:
            node["children"].append(self._raw_node(child))
        return node

    def _merged(self, roots):
        # Merge identical call paths. Each merged node owns its own child map,
        # so the same marker under different parents stays separate.
        output = []

        def add(index, parent_children, parent_map):
            sample = self.samples[index]
            name = self._name(sample)
            entry = parent_map.get(name)
            if entry is None:
                entry = {
                    "name": name,
                    "total_us": 0.0,
                    "self_us": 0.0,
                    "calls": 0,
                    "gc": 0,
                    "children": [],
                }
                parent_map[name] = entry
                parent_children.append(entry)
            entry["total_us"] += sample["dur"]
            entry["self_us"] += sample["self"]
            entry["calls"] += 1
            entry["gc"] += sample["gc"]
            child_map = entry.setdefault("_child_map", {})
            for child in sample["children"]:
                add(child, entry["children"], child_map)

        for root in roots:
            add(root, output, {})

        def clean(items):
            for item in items:
                item["total_us"] = round(item["total_us"], 1)
                item["self_us"] = round(item["self_us"], 1)
                item.pop("_child_map", None)
                clean(item["children"])
                item["children"].sort(
                    key=lambda child: child["total_us"], reverse=True
                )
            items.sort(key=lambda item: item["total_us"], reverse=True)

        clean(output)
        return output


class HotspotReader(object):
    def __init__(self, raw_path):
        self.raw_path = raw_path
        self.lock = threading.RLock()
        self._index = None
        self._frames = {}
        self._frame_cache = {}

    # ------------------------------------------------------------- header

    def _read_header(self, fp):
        data = fp.read(36)
        if len(data) < 36:
            raise HotspotError("raw stream header is incomplete")
        signature, little, align, platform, version, num, den, main = struct.unpack(
            "<IBBH I Q Q Q", data
        )
        if signature != rr.SESSION_SIGNATURE:
            raise HotspotError("not a PD3U profiler stream")
        if version < rr.SUPPORTED_VERSION:
            raise HotspotError(
                "unsupported profiler stream version 0x{:x}".format(version)
            )
        fp.read(20)  # unity version
        return {
            "version": version,
            "align": bool(align),
            "num": num,
            "den": den,
            "main_thread": main,
            "header_end": fp.tell(),
            "scale": (float(num) / float(den) if den else 1.0) / 1000.0,
        }

    # ------------------------------------------------------------- index

    def _build_index(self):
        header = None
        collector = None
        parser = None
        with open(self.raw_path, "rb") as fp:
            header = self._read_header(fp)
            collector = _FrameScan(header["main_thread"])
            parser = _MessageParser(header["version"], header["align"])
            offset = fp.tell()
            while True:
                block_header = fp.read(20)
                if len(block_header) < 20:
                    break
                signature, block_id, thread_id, length = struct.unpack(
                    "<IIQI", block_header
                )
                if signature != rr.BLOCK_SIGNATURE:
                    break
                data = fp.read(length)
                footer = fp.read(8)
                if len(data) < length or len(footer) < 8:
                    break
                footer_next, footer_sig = struct.unpack("<II", footer)
                if footer_sig != rr.FOOTER_SIGNATURE:
                    break
                parser.parse(thread_id, data, collector, offset)
                offset = fp.tell()
        frames = collector.frames
        self._index = {
            "header": header,
            "frames": frames,
            "samplers": collector.samplers,
            "threads": collector.threads,
        }
        self._frame_cache.clear()
        logger.info(
            "[Unity] hotspot index ready: %d frames, %d markers, %d threads",
            len(frames),
            len(collector.samplers),
            len(collector.threads),
        )

    def _ensure_index(self):
        with self.lock:
            if self._index is None:
                self._build_index()
            return self._index

    def frames(self):
        index = self._ensure_index()
        items = sorted(index["frames"].values(), key=lambda item: item["index"])
        return [
            {"index": item["index"], "time": item["time"]}
            for item in items
        ]

    def info(self):
        """Return capture metadata without decoding individual frames."""
        index = self._ensure_index()
        header = index["header"]
        threads = [
            {
                "id": str(thread_id),
                "name": item.get("name", "Thread 0x{:x}".format(thread_id)),
                "group": item.get("group") or "",
            }
            for thread_id, item in sorted(index["threads"].items())
        ]
        return {
            "stream_version": header["version"],
            "align": header["align"],
            "sample_rate_num": header["num"],
            "sample_rate_den": header["den"],
            "main_thread": str(header["main_thread"]),
            "frame_count": len(index["frames"]),
            "marker_count": len(index["samplers"]),
            "threads": threads,
        }

    # -------------------------------------------------------------- frame

    def frame(self, target, selected_thread="main", cache=True):
        index = self._ensure_index()
        with self.lock:
            key = (target, selected_thread)
            cached = self._frame_cache.get(key)
            if cached is not None:
                return cached
            result = self._read_frame(index, target, selected_thread)
            if cache:
                self._frame_cache[key] = result
            return result

    def _read_frame(self, index, target, selected_thread):
        frames = index["frames"]
        entry = frames.get(target)
        if entry is None:
            raise HotspotError("frame {} not found".format(target))
        header = index["header"]
        scale = header["scale"]

        ordered = sorted(frames.values(), key=lambda item: item["index"])
        position = next(
            pos for pos, item in enumerate(ordered) if item["index"] == target
        )
        prev = ordered[position - 1] if position > 0 else None

        # kFrame marks the END of a frame. Frame N therefore covers the
        # interval between the previous main-thread marker and marker N.
        frame_end_raw = entry["main_time"] or entry["time"]
        frame_start_raw = prev["main_time"] if prev else None
        if frame_start_raw is None:
            later = [
                item["main_time"]
                for item in ordered[position + 1:]
                if item["main_time"] is not None
            ]
            interval = (min(later) - frame_end_raw) if later else int(16666 / scale)
            frame_start_raw = frame_end_raw - max(interval, int(1000 / scale))
        if frame_start_raw >= frame_end_raw:
            frame_start_raw = frame_end_raw - int(16666 / scale)

        start_offset = prev["offset"] if prev else header["header_end"]
        start_frame = prev["index"] if prev else -1

        collector = _FrameCollector(
            index["samplers"], index["threads"], target, start_frame=start_frame
        )
        parser = _MessageParser(header["version"], header["align"])
        with open(self.raw_path, "rb") as fp:
            fp.seek(start_offset)
            while not collector.stop:
                block_header = fp.read(20)
                if len(block_header) < 20:
                    break
                signature, block_id, thread_id, length = struct.unpack(
                    "<IIQI", block_header
                )
                if signature != rr.BLOCK_SIGNATURE:
                    break
                data = fp.read(length)
                footer = fp.read(8)
                if len(data) < length or len(footer) < 8:
                    break
                offset = fp.tell() - length - 28
                parser.parse(thread_id, data, collector, offset)

        frame_start = frame_start_raw
        frame_end = frame_end_raw

        timelines = {}
        threads = []
        selected_samples = {}
        for thread_id, items in collector.samples.items():
            thread_info = index["threads"].get(thread_id, {})
            thread_name = thread_info.get("name", "Thread 0x{:x}".format(thread_id))
            thread_group = thread_info.get("group") or ""
            if thread_group:
                thread_name = "{}/{}".format(thread_group, thread_name)
            selected = []
            for sample in items:
                start = sample["start"]
                end = sample.get("end", sample["start"] + sample.get("dur", 0))
                if end <= frame_start or start >= frame_end:
                    continue
                clipped_start = max(start, frame_start)
                clipped_end = min(end, frame_end)
                if clipped_end <= clipped_start:
                    continue
                duration = clipped_end - clipped_start
                selected.append(
                    {
                        "id": sample["id"],
                        "name": index["samplers"].get(
                            sample["id"],
                            ("marker_{}".format(sample["id"]), 0),
                        )[0],
                        "start": round((clipped_start - frame_start) * scale, 1),
                        "dur": round(duration * scale, 1),
                        "gc": sample["gc"],
                        "uid": sample["uid"],
                        "parent_uid": sample.get("parent_uid"),
                    }
                )
            if not selected:
                continue

            for position, sample in enumerate(selected):
                sample["index"] = position

            # Timeline payload: no internal bookkeeping fields.
            timeline = [
                {
                    "id": sample["id"],
                    "name": sample["name"],
                    "start": sample["start"],
                    "dur": sample["dur"],
                    "gc": sample["gc"],
                }
                for sample in selected
            ]
            timelines[str(thread_id)] = {
                "id": str(thread_id),
                "name": thread_name,
                "samples": timeline,
            }
            selected_samples[str(thread_id)] = selected
            total_us = round(sum(sample["dur"] for sample in selected), 1)
            threads.append(
                {
                    "id": str(thread_id),
                    "name": thread_name,
                    "samples": len(selected),
                    "total_us": total_us,
                }
            )

        threads.sort(
            key=lambda item: (
                0 if item["name"] == "Main Thread" else 1,
                0 if item["name"] == "Render Thread" else 1,
                -item["total_us"],
            )
        )

        # Pick the requested thread for hierarchy. "all" uses Main Thread.
        if selected_thread and selected_thread not in ("all", "main", "render"):
            wanted = selected_thread
        elif selected_thread == "render":
            wanted = "Render Thread"
        else:
            wanted = "Main Thread"
        thread_key = None
        for key, value in timelines.items():
            if value["name"] == wanted or value["id"] == wanted:
                thread_key = key
                break
        hierarchy = []
        raw_hierarchy = []
        if thread_key is not None:
            # Reconstruct the call tree from time containment. This is more
            # robust than relying on every Begin/End pair surviving a block
            # boundary: a child is nested in the nearest enclosing sample.
            tree_samples = sorted(
                (dict(sample) for sample in selected_samples[thread_key]),
                key=lambda sample: (sample["start"], -sample["dur"]),
            )
            stack = []
            for position, sample in enumerate(tree_samples):
                sample["index"] = position
                sample_end = sample["start"] + sample["dur"]
                while stack:
                    parent = stack[-1]
                    if parent["start"] + parent["dur"] > sample["start"]:
                        break
                    stack.pop()
                sample["parent"] = stack[-1]["index"] if stack else None
                stack.append(sample)
            aggregator = _ThreadAggregator(tree_samples, index["samplers"])
            raw_hierarchy, hierarchy = aggregator.build()

        return {
            "status": 1,
            "frame": target,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "duration_us": round((frame_end - frame_start) * scale, 1),
            "threads": threads,
            "timeline": timelines,
            "selected_thread": thread_key,
            "hierarchy": hierarchy,
            "raw_hierarchy": raw_hierarchy,
            "samplers": len(index["samplers"]),
        }


# --------------------------------------------------------------- registry

_readers = {}
_readers_lock = threading.Lock()


def get_hotspot_reader(raw_path):
    if not raw_path or not os.path.isfile(raw_path):
        raise HotspotError("profiler.raw not found")
    key = (os.path.abspath(raw_path), os.path.getmtime(raw_path), os.path.getsize(raw_path))
    with _readers_lock:
        reader = _readers.get(key)
        if reader is None:
            reader = HotspotReader(raw_path)
            _readers[key] = reader
        return reader
