# -*- coding: utf-8 -*-
"""逐帧解析归档 ``profiler.raw`` 的 Unity 计数器。

实时面板每秒落一次 ``unity_stats.log``，每次只取"当时最新一帧"，短时
峰值容易被整段错过。归档的 ``profiler.raw`` 里每个计数器本来就是逐帧的
（1 帧 1 个样本），这里离线扫一遍，把完整序列缓存成
``unity/frame_counters.json``，报告再用它做峰值 / 1% low / 卡顿帧统计。

缓存按 raw 的 (size, mtime) 校验，内容不变就直接复用，不会每次打开报告
都重新解析；解析只在后台线程进行，失败或中断都不会影响报告打开。
"""
import json
import os
import threading
import time

from logzero import logger

from perfcattle.unity.raw_reader import RawStatsParser

FORMAT = "perfcattle-unity-frame-counters"
FORMAT_VERSION = 2

RAW_FILE = "profiler.raw"
META_FILE = "meta.json"
CACHE_FILE = "frame_counters.json"

CHUNK_BYTES = 4 * 1024 * 1024
# 报告图表默认给全部帧：峰值/谷值和"点哪一帧看热点"都依赖完整序列。
# 只有超长采集（> 2 万帧）才按桶降到这个上限，避免前端被几十万点压死。
MAX_CHART_POINTS = 20000

# 这些计数器的单位是 ms，额外给出 1% low / 0.1% low（换算成 fps）。
FRAME_TIME_KEYS = ("cpu_frame_ms", "render_frame_ms", "cpu_total_ms", "gpu_frame_ms")
# 卡顿判定：单帧耗时超过中位数的两倍。
HITCH_FACTOR = 2.0
# 同一份 raw 解析失败后的重试间隔（秒）。
RETRY_AFTER = 300
# 帧时间偏离中位数超过这个值就认为是损坏数据（秒）。
MAX_FRAME_DEVIATION_SECONDS = 6 * 3600.0

_build_lock = threading.Lock()
_build_state = {}


class FrameScan(RawStatsParser):
    """RawStatsParser + 记录每一个样本（面板块只保留最新值）。"""

    def __init__(self):
        RawStatsParser.__init__(self)
        self.series = {}
        self.frame_ticks = {}
        self.on_sample = self._capture

    def _capture(self, key, value, frame, frame_time, sample_time):
        if frame is None:
            return
        self.series.setdefault(key, []).append((frame, value))
        if frame_time is not None:
            self.frame_ticks.setdefault(frame, frame_time)


def _timer_scale(scan):
    """tick -> 秒的比例。Unity 的 num/den 把 tick 归一化到纳秒。"""
    num = scan._timer_num or 1
    den = scan._timer_den or 1
    return (float(num) / float(den)) / 1e9


def _clean_ticks(frame_ticks, scale):
    """剔除损坏 block 解析出的假帧。

    重同步时偶尔会把随机字节当成帧时间，得到远超采集时长的 tick，或者
    时间倒退。真实帧的时间集中在中位数附近且单调递增，用这两条兜住。
    """
    if not frame_ticks:
        return {}, set()
    items = sorted(frame_ticks.items())
    ordered = sorted(tick for _, tick in items)
    median = ordered[len(ordered) // 2]
    kept = {}
    rejected = set()
    last = None
    for frame, tick in items:
        if abs(tick - median) * scale > MAX_FRAME_DEVIATION_SECONDS:
            rejected.add(frame)
            continue
        if last is not None and tick <= last:
            rejected.add(frame)
            continue
        kept[frame] = tick
        last = tick
    return kept, rejected


def build_payload(raw_path, meta_path=None):
    """解析 ``raw_path``，返回可直接落盘的 payload。"""
    started = time.time()
    source_bytes = os.path.getsize(raw_path)
    scan = FrameScan()
    read = 0
    with open(raw_path, "rb") as fp:
        while True:
            chunk = fp.read(CHUNK_BYTES)
            if not chunk:
                break
            read += len(chunk)
            scan.feed(chunk)

    scale = _timer_scale(scan)
    ticks, rejected = _clean_ticks(scan.frame_ticks, scale)
    frames = [
        frame
        for frame in sorted(set(ticks) | set(
            frame for samples in scan.series.values() for frame, _ in samples
        ))
        if frame not in rejected
    ]
    origin_tick = ticks.get(frames[0]) if frames else None
    times = []
    for frame in frames:
        tick = ticks.get(frame)
        if tick is None or origin_tick is None:
            times.append(None)
        else:
            times.append(round((tick - origin_tick) * scale, 4))
    # 没有 tick 的帧沿用前一帧的时间，保证时间轴单调。
    _fill_missing_times(times)

    series = {}
    for key, samples in scan.series.items():
        series[key] = [
            [frame, value]
            for frame, value in samples
            if frame not in rejected
        ]

    meta = _read_meta(meta_path)
    truncated = bool(scan._buffer)
    complete = not truncated and not scan._header_error
    if meta.get("raw_complete") is False:
        # 采集期间流本身出过错（Unity 侧没写完整），即使文件尾部对齐也不可信。
        complete = False
    payload = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "source_file": os.path.basename(raw_path),
        "source_bytes": source_bytes,
        "unity_version": scan.unity_version,
        "complete": complete,
        "error": scan._header_error,
        "started_at": meta.get("started_at"),
        "frames": frames,
        "times": times,
        "series": series,
        "parse_stats": {
            "blocks": scan._blocks,
            "parsed": scan._parsed_blocks,
            "skipped": scan._skipped_blocks,
            "bad": scan._bad_blocks,
            "partial": scan._partial_blocks,
            "dropped_frames": len(rejected),
            "frames": len(frames),
            "bytes": read,
        },
    }
    logger.info(
        "[Unity] frame counters ready: %d frames, %d counters, %.1f MB in %.1fs",
        len(frames),
        len(series),
        read / 1048576.0,
        time.time() - started,
    )
    return payload


def _fill_missing_times(times):
    """没有帧时间戳的帧用相邻值补齐，保证时间轴单调。"""
    first = next((value for value in times if value is not None), 0.0)
    last = first
    for index, value in enumerate(times):
        if value is None:
            times[index] = last
        else:
            last = value


def _read_meta(meta_path):
    if not meta_path or not os.path.isfile(meta_path):
        return {}
    try:
        with open(meta_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_cache(payload, out_path):
    """原子写入缓存，半途中断不会留下可被误读的半个文件。"""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp_path = out_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, out_path)
    except OSError as e:
        logger.warning("[Unity] cannot write frame counter cache: %s", e)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return None
    return out_path


def load_cache(raw_path, cache_path):
    """缓存有效时返回 payload，否则返回 None。"""
    if not os.path.isfile(raw_path) or not os.path.isfile(cache_path):
        return None
    try:
        stat = os.stat(raw_path)
        if os.path.getmtime(cache_path) < stat.st_mtime:
            return None
        with open(cache_path, "r", encoding="utf-8") as fp:
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
    if payload.get("source_bytes") != stat.st_size:
        return None
    return payload


def ensure(unity_dir, wait=0.0):
    """返回 ``(payload, state)``，缓存缺失时在后台生成并返回 building。

    ``wait>0`` 时最多阻塞等待这么多秒（导出这类显式操作可以等，页面请求
    不应该等）。
    """
    raw_path = os.path.join(unity_dir, RAW_FILE)
    if not os.path.isfile(raw_path):
        return None, "missing"
    cache_path = os.path.join(unity_dir, CACHE_FILE)
    payload = load_cache(raw_path, cache_path)
    if payload is not None:
        return payload, "ready"

    key = os.path.abspath(raw_path)
    signature = _signature(raw_path)
    with _build_lock:
        state = _build_state.get(key) or {}
        running = state.get("state") == "building"
        # 同一份 raw 解析失败后不要每次打开页面都重来一遍；raw 变了或
        # 距上次失败超过 RETRY_AFTER 秒才重新尝试。
        stalled = (
            state.get("state") == "failed"
            and state.get("signature") == signature
            and time.time() - state.get("since", 0) < RETRY_AFTER
        )
        if not running and not stalled:
            _build_state[key] = {"state": "building", "since": time.time()}
            thread = threading.Thread(
                target=_build_worker,
                args=(key, raw_path, cache_path, os.path.join(unity_dir, META_FILE), signature),
                name="unity-frame-counters",
            )
            thread.daemon = True
            thread.start()
    if wait:
        deadline = time.time() + float(wait)
        while time.time() < deadline:
            payload = load_cache(raw_path, cache_path)
            if payload is not None:
                return payload, "ready"
            with _build_lock:
                state = (_build_state.get(key) or {}).get("state")
            if state == "failed":
                break
            time.sleep(0.2)
    with _build_lock:
        current = _build_state.get(key) or {}
    return None, current.get("state", "building")


def _signature(raw_path):
    try:
        stat = os.stat(raw_path)
    except OSError:
        return None
    return (stat.st_size, int(stat.st_mtime))


def _build_worker(key, raw_path, cache_path, meta_path, signature):
    try:
        payload = build_payload(raw_path, meta_path)
        if write_cache(payload, cache_path) is None:
            raise RuntimeError("cannot write frame counter cache")
        with _build_lock:
            _build_state[key] = {"state": "ready", "since": time.time()}
    except Exception as e:  # never break the report page
        logger.warning("[Unity] frame counter build failed: %s", e)
        with _build_lock:
            _build_state[key] = {
                "state": "failed",
                "since": time.time(),
                "signature": signature,
                "message": str(e),
            }


# ------------------------------------------------------------------ stats


def series_values(payload, key):
    """返回 {frame: value}，便于与其它序列对齐。"""
    return {int(frame): value for frame, value in payload["series"].get(key) or []}


def frame_index(payload):
    return list(payload.get("frames") or [])


def _percentile(ordered, ratio):
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _mean_slowest(values, fraction):
    """慢帧均值：1% low 通常取最慢 1% 帧耗时的平均。"""
    if not values:
        return None
    count = max(1, int(round(len(values) * fraction)))
    ordered = sorted(values, reverse=True)
    return sum(ordered[:count]) / float(count)


def series_stats(values):
    """单条序列的统计量。``values`` 为数值列表。"""
    if not values:
        return {}
    ordered = sorted(values)
    stats = {
        "count": len(values),
        "avg": round(sum(values) / float(len(values)), 3),
        "min": ordered[0],
        "max": ordered[-1],
        "p50": round(_percentile(ordered, 0.50), 3),
        "p95": round(_percentile(ordered, 0.95), 3),
        "p99": round(_percentile(ordered, 0.99), 3),
    }
    return stats


def frame_stats(payload):
    """逐帧统计 + 卡顿帧，供报告展示"峰值"而不是平均值。"""
    result = {"counters": {}, "hitches": {}}
    for key, samples in payload.get("series", {}).items():
        values = [value for _, value in samples]
        stats = series_stats(values)
        if key in FRAME_TIME_KEYS and stats:
            low1 = _mean_slowest(values, 0.01)
            low01 = _mean_slowest(values, 0.001)
            stats["low1_ms"] = round(low1, 2)
            stats["low1_fps"] = round(1000.0 / low1, 1) if low1 else None
            stats["low01_ms"] = round(low01, 2)
            stats["low01_fps"] = round(1000.0 / low01, 1) if low01 else None
        result["counters"][key] = stats

    for key in ("cpu_frame_ms", "gpu_frame_ms"):
        summary = _hitch_summary(payload, key)
        if summary:
            result["hitches"][key] = summary
    result["hitch"] = (
        result["hitches"].get("cpu_frame_ms")
        or result["hitches"].get("gpu_frame_ms")
    )
    return result


def _hitch_summary(payload, key):
    samples = (payload.get("series") or {}).get(key)
    if not samples:
        return None
    values = [value for _, value in samples]
    median = _percentile(sorted(values), 0.50) or 0.0
    threshold = round(median * HITCH_FACTOR, 3)
    times = dict(zip(payload.get("frames") or [], payload.get("times") or []))
    hitches = sum(1 for _, value in samples if value > threshold)
    worst = []
    for frame, value in sorted(samples, key=lambda item: item[1], reverse=True)[:10]:
        if value <= threshold:
            break
        worst.append({"frame": frame, "value": value, "t": times.get(frame)})
    return {
        "metric": key,
        "threshold_ms": threshold,
        "count": hitches,
        "ratio": round(hitches / float(len(values)), 4) if values else 0.0,
        "worst": worst,
    }


# -------------------------------------------------------------- downsampling


def chart_frames(payload, max_points=MAX_CHART_POINTS):
    """返回用于画图的帧号。

    默认原样返回全部帧（报告要能看到每一帧的波峰波谷，也要能点中任意一帧
    看热点函数）；只有超过 ``max_points`` 的超长采集才按桶降采样，并保留
    每桶的最慢/最快帧，尖峰仍然可见。
    """
    frames = payload.get("frames") or []
    if not frames:
        return []
    if len(frames) <= max_points:
        return list(frames)

    primary = None
    for key in FRAME_TIME_KEYS:
        if payload.get("series", {}).get(key):
            primary = key
            break
    if primary is None:
        keys = list(payload.get("series") or {})
        primary = keys[0] if keys else None
    lookup = series_values(payload, primary) if primary else {}

    buckets = max(1, max_points // 2)
    size = (len(frames) + buckets - 1) // buckets
    selected = []
    for start in range(0, len(frames), size):
        chunk = frames[start:start + size]
        scored = [(lookup[frame], frame) for frame in chunk if frame in lookup]
        if scored:
            # 每桶保留最慢和最快的一帧，尖峰不会被降采样抹掉。
            selected.append(min(scored)[1])
            selected.append(max(scored)[1])
        else:
            selected.append(chunk[-1])
    return sorted(set(selected))


def downsample(payload, frames):
    """把序列裁剪到 ``frames`` 子集。"""
    allowed = set(frames)
    result = {}
    for key, samples in payload.get("series", {}).items():
        result[key] = [[frame, value] for frame, value in samples if frame in allowed]
    return result


def close():
    """测试辅助：清空后台状态。"""
    with _build_lock:
        _build_state.clear()


__all__ = [
    "CACHE_FILE",
    "FORMAT",
    "FORMAT_VERSION",
    "MAX_CHART_POINTS",
    "build_payload",
    "chart_frames",
    "close",
    "downsample",
    "ensure",
    "frame_stats",
    "load_cache",
    "series_values",
    "write_cache",
]
