# -*- coding: utf-8 -*-
"""Unity Profiler 离线 JSON 导出。

把 ``unity/profiler.raw`` 转成一份不依赖 Unity 的分析用 JSON：

- capture / stream 元信息（Unity 版本、帧数、线程、marker 数）
- 每帧的主线程 / 渲染线程耗时、GC 分配
- 每帧耗时最高的 marker（Merged Hierarchy 的前 N 项）
- 整个采集区间内按 marker 聚合的 calls / total / self / GC
- 最慢的若干帧保留完整 Merged Hierarchy

解析复用 ``perfcattle.unity.hotspots.HotspotReader``，因此导出过程和
分析页看到的是同一套数据，不需要安装 Unity 或 Memory Profiler。

已经生成且与当前 raw 文件、采样选项一致的 JSON 可以直接复用，避免每次
导出都重新解码整条 profiler 原始流（``cached_profiler_json``）。
"""
import datetime
import json
import os

from logzero import logger

from perfcattle.unity.hotspots import HotspotError, HotspotReader

FORMAT = "perfcattle-unity-profiler-json"
FORMAT_VERSION = 1

DEFAULT_MAX_FRAMES = 1000
DEFAULT_TOP_MARKERS = 30
DEFAULT_SLOW_FRAMES = 10

LIMITATIONS = [
    "Frame timings and marker hierarchy are reconstructed from the raw PD3U profiler stream "
    "without Unity; counters, callstacks and object metadata are not included.",
    "Only the main-thread merged hierarchy is exported per frame; other threads keep their "
    "frame totals, and raw (unmerged) call trees are omitted.",
    "When frame_count exceeds max_frames the exporter samples frames evenly and aggregates "
    "marker statistics over the exported frames only.",
]


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _resolve_options(max_frames=None, top_markers=None, slow_frames=None):
    """Normalize export options so they can be stored and compared verbatim."""
    return {
        "max_frames": DEFAULT_MAX_FRAMES if max_frames is None else max_frames,
        "top_markers": DEFAULT_TOP_MARKERS
        if top_markers is None
        else top_markers,
        "slow_frames": DEFAULT_SLOW_FRAMES if slow_frames is None else slow_frames,
    }


def _flatten(roots, parent_path=""):
    """Flatten a merged hierarchy into a list, keeping a readable call path."""
    items = []
    for node in roots or []:
        name = node.get("name") or "<unknown>"
        path = parent_path + " > " + name if parent_path else name
        items.append(
            {
                "name": name,
                "path": path,
                "total_us": node.get("total_us", 0.0),
                "self_us": node.get("self_us", 0.0),
                "calls": node.get("calls", 0),
                "gc_bytes": node.get("gc", 0),
            }
        )
        items.extend(_flatten(node.get("children"), path))
    return items


def _gc_total(roots):
    total = 0
    stack = list(roots or [])
    while stack:
        node = stack.pop()
        total += node.get("gc", 0) or 0
        stack.extend(node.get("children") or [])
    return int(total)


def _select_frames(frames, limit):
    """Pick at most ``limit`` frames, evenly covering the whole capture."""
    if not limit or limit <= 0 or len(frames) <= limit:
        return list(frames), False
    if limit == 1:
        return [frames[0]], True
    last = len(frames) - 1
    picked = []
    previous = -1
    for index in range(limit):
        position = int(round(index * last / float(limit - 1)))
        position = max(0, min(last, position))
        if position == previous:
            continue
        picked.append(frames[position])
        previous = position
    if picked[-1] is not frames[last]:
        picked[-1] = frames[last]
    return picked, True


def _thread_main_us(threads):
    for item in threads or []:
        if item.get("name") == "Main Thread":
            return item.get("total_us")
    return None


def build_profiler_json(
    raw_path,
    meta=None,
    max_frames=DEFAULT_MAX_FRAMES,
    top_markers=DEFAULT_TOP_MARKERS,
    slow_frames=DEFAULT_SLOW_FRAMES,
):
    """Parse ``raw_path`` and return the Unity-free JSON payload."""
    options = _resolve_options(max_frames, top_markers, slow_frames)
    max_frames = options["max_frames"]
    top_markers = options["top_markers"]
    slow_frames = options["slow_frames"]
    reader = HotspotReader(raw_path)
    info = reader.info()
    all_frames = reader.frames()
    selected, sampled = _select_frames(all_frames, max_frames)

    aggregate = {}
    exported = []
    for item in selected:
        frame_index = item["index"]
        data = reader.frame(frame_index, "main", cache=False)
        hierarchy = data.get("hierarchy") or []
        flat = _flatten(hierarchy)
        for node in flat:
            entry = aggregate.get(node["name"])
            if entry is None:
                entry = {
                    "name": node["name"],
                    "calls": 0,
                    "total_us": 0.0,
                    "self_us": 0.0,
                    "gc_bytes": 0,
                    "max_frame_total_us": 0.0,
                }
                aggregate[node["name"]] = entry
            entry["calls"] += node["calls"]
            entry["total_us"] += node["total_us"]
            entry["self_us"] += node["self_us"]
            entry["gc_bytes"] += node["gc_bytes"]
            entry["max_frame_total_us"] = max(
                entry["max_frame_total_us"], node["total_us"]
            )

        top = sorted(flat, key=lambda node: node["total_us"], reverse=True)
        exported.append(
            {
                "index": frame_index,
                "duration_us": data.get("duration_us"),
                "main_thread_total_us": _thread_main_us(data.get("threads")),
                "gc_bytes": _gc_total(hierarchy),
                "threads": [
                    {
                        "id": thread.get("id"),
                        "name": thread.get("name"),
                        "samples": thread.get("samples"),
                        "total_us": thread.get("total_us"),
                    }
                    for thread in data.get("threads") or []
                ],
                "top_markers": top[:top_markers] if top_markers and top_markers > 0 else top,
            }
        )

    markers = sorted(
        aggregate.values(),
        key=lambda item: (item["total_us"], item["self_us"]),
        reverse=True,
    )
    for item in markers:
        item["total_us"] = round(item["total_us"], 1)
        item["self_us"] = round(item["self_us"], 1)
        item["max_frame_total_us"] = round(item["max_frame_total_us"], 1)

    # Re-read only the slowest frames so the detailed call tree stays bounded.
    slow = sorted(
        exported,
        key=lambda item: item.get("duration_us") or 0.0,
        reverse=True,
    )[: max(0, slow_frames or 0)]
    slow_details = []
    for item in slow:
        try:
            data = reader.frame(item["index"], "main", cache=False)
        except Exception as e:
            logger.warning(
                "[Unity] cannot export slow frame %s: %s", item["index"], e
            )
            continue
        slow_details.append(
            {
                "index": item["index"],
                "duration_us": data.get("duration_us"),
                "main_thread_total_us": _thread_main_us(data.get("threads")),
                "gc_bytes": _gc_total(data.get("hierarchy") or []),
                "hierarchy": data.get("hierarchy") or [],
            }
        )

    capture_meta = dict(meta or {})
    # Do not leak the recording machine's absolute staging path into exports.
    capture_meta.pop("profiler_log", None)
    capture_meta["source_file"] = os.path.basename(raw_path)

    durations = [
        item.get("duration_us")
        for item in exported
        if item.get("duration_us") is not None
    ]
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "generated_at": _now(),
        "source_file": os.path.basename(raw_path),
        "source_bytes": os.path.getsize(raw_path),
        "options": options,
        "capture": capture_meta,
        "stream": info,
        "summary": {
            "frame_count": info["frame_count"],
            "exported_frame_count": len(exported),
            "sampled": sampled,
            "marker_count": info["marker_count"],
            "avg_frame_us": round(sum(durations) / len(durations), 1)
            if durations
            else None,
            "max_frame_us": max(durations) if durations else None,
            "total_gc_bytes": sum(item["gc_bytes"] for item in exported),
            "markers": markers,
        },
        "frames": exported,
        "slow_frames": slow_details,
        "limitations": LIMITATIONS,
    }


def cached_profiler_json(
    raw_path,
    out_path,
    meta_path=None,
    max_frames=None,
    top_markers=None,
    slow_frames=None,
):
    """Return an existing profiler JSON when it is still valid for the capture.

    Rebuilding the JSON means decoding the whole raw stream, which dominates
    export time. The cached payload is reused only when it was produced from
    the same raw file (same size, not older than the raw or the capture
    metadata) with the same sampling options.
    """
    if not os.path.isfile(raw_path) or not os.path.isfile(out_path):
        return None
    try:
        out_mtime = os.path.getmtime(out_path)
        if out_mtime < os.path.getmtime(raw_path):
            return None
        if meta_path and os.path.isfile(meta_path):
            if out_mtime < os.path.getmtime(meta_path):
                return None
        raw_size = os.path.getsize(raw_path)
        with open(out_path, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except (OSError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("format") != FORMAT:
        return None
    if payload.get("format_version") != FORMAT_VERSION:
        return None
    if payload.get("source_file") != os.path.basename(raw_path):
        return None
    if payload.get("source_bytes") != raw_size:
        return None
    if payload.get("options") != _resolve_options(
        max_frames, top_markers, slow_frames
    ):
        return None
    return payload


def write_profiler_json(
    raw_path,
    out_path,
    meta=None,
    max_frames=DEFAULT_MAX_FRAMES,
    top_markers=DEFAULT_TOP_MARKERS,
    slow_frames=DEFAULT_SLOW_FRAMES,
):
    """Build the JSON payload and write it atomically to ``out_path``."""
    payload = build_profiler_json(
        raw_path,
        meta=meta,
        max_frames=max_frames,
        top_markers=top_markers,
        slow_frames=slow_frames,
    )
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    os.replace(tmp_path, out_path)
    return payload


__all__ = [
    "FORMAT",
    "FORMAT_VERSION",
    "DEFAULT_MAX_FRAMES",
    "DEFAULT_TOP_MARKERS",
    "DEFAULT_SLOW_FRAMES",
    "HotspotError",
    "build_profiler_json",
    "cached_profiler_json",
    "write_profiler_json",
]
