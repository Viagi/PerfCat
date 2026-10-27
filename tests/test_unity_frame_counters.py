# -*- coding: utf-8 -*-
"""逐帧 Unity 计数器：解析、缓存、统计与报告接入。"""
import json
import os
import struct

import pytest

from perfcattle.report.log_io import LogIO
from perfcattle.unity import frame_counters as fc
from tests.test_unity_raw_reader import (
    GLOBAL,
    MAIN_ID,
    RENDER_ID,
    _block,
    _counter,
    _header,
    _message,
    _sampler,
    _thread_info,
)

# 采样器 id：CPU 帧耗时在 main thread，Draw Calls 在 render thread
CPU_ID = 0x6C1
DRAW_ID = 0x6A3


def _frame(index, ticks):
    return _message(34, struct.pack("<I", index) + struct.pack("<Q", ticks))


def build_frames(values, draws=None):
    """构造 ``len(values)`` 帧的流；values 为 CPU 帧耗时（ms）。"""
    samples = [
        _sampler(CPU_ID, "CPU Main Thread Frame Time"),
        _sampler(DRAW_ID, "Draw Calls Count"),
    ]
    blocks = [
        _block(
            GLOBAL,
            [_thread_info(MAIN_ID, "Main Thread"), _thread_info(RENDER_ID, "Render Thread")]
            + samples,
            0,
        )
    ]
    for offset, value in enumerate(values):
        frame = offset + 1
        # 每帧 16.67ms，tick 单位为纳秒
        ticks = frame * 16_666_667
        main = [
            _frame(frame, ticks),
            _counter(CPU_ID, int(value * 1_000_000), sample_time=ticks),
        ]
        blocks.append(_block(MAIN_ID, main, frame * 2))
        if draws:
            blocks.append(
                _block(
                    RENDER_ID,
                    [_counter(DRAW_ID, draws[offset], sample_time=ticks)],
                    frame * 2 + 1,
                )
            )
    return _header() + b"".join(blocks)


@pytest.fixture(autouse=True)
def _reset_state():
    fc.close()
    yield
    fc.close()


def test_build_payload_keeps_every_frame(tmp_path):
    raw = tmp_path / "profiler.raw"
    raw.write_bytes(build_frames([10.0, 12.0, 11.0, 30.0, 13.0]))
    payload = fc.build_payload(str(raw))

    assert payload["frames"] == [1, 2, 3, 4, 5]
    assert payload["complete"] is True
    # 每帧一个样本，而不是每秒抽一帧。
    assert [value for _, value in payload["series"]["cpu_frame_ms"]] == [
        10.0,
        12.0,
        11.0,
        30.0,
        13.0,
    ]
    assert payload["times"][0] == 0.0
    assert payload["times"][-1] > 0.0


def test_frame_stats_expose_peaks_and_low_fps(tmp_path):
    raw = tmp_path / "profiler.raw"
    raw.write_bytes(build_frames([10.0, 12.0, 11.0, 30.0, 13.0]))
    payload = fc.build_payload(str(raw))
    stats = fc.frame_stats(payload)

    cpu = stats["counters"]["cpu_frame_ms"]
    assert cpu["max"] == 30.0
    assert cpu["p50"] == 12.0
    assert cpu["avg"] == 15.2
    # 最慢 1% 只有一帧（5 帧的 1%），1% low 就等于这一帧的耗时。
    assert cpu["low1_ms"] == 30.0
    assert cpu["low1_fps"] == 33.3

    hitch = stats["hitch"]
    assert hitch["metric"] == "cpu_frame_ms"
    assert hitch["threshold_ms"] == 24.0
    assert hitch["count"] == 1
    assert hitch["worst"][0]["frame"] == 4


def test_chart_frames_cap_and_keep_peak():
    frames = list(range(1, 5001))
    values = [[frame, 10.0 + (frame % 7)] for frame in frames]
    values[2500] = [2501, 999.0]
    payload = {
        "frames": frames,
        "times": [index * 0.033 for index in range(len(frames))],
        "series": {"cpu_frame_ms": values},
    }

    selected = fc.chart_frames(payload, max_points=400)
    assert len(selected) <= 400
    assert 2501 in selected  # 尖峰保留在曲线上
    assert selected == sorted(selected)


def test_corrupted_frame_times_are_dropped(tmp_path):
    """重同步误判出的假帧号不能被当成真实帧进入时间轴。"""
    good = [_frame(1, 16_666_667), _counter(CPU_ID, 10_000_000, 16_666_667),
            _frame(2, 33_333_334), _counter(CPU_ID, 12_000_000, 33_333_334),
            _frame(3, 50_000_001), _counter(CPU_ID, 11_000_000, 50_000_001)]
    # 13e18 ns ≈ 412 年，明显是损坏数据
    bogus = [_frame(129302528, 13_000_000_000_000_000_000),
             _counter(CPU_ID, 900, 13_000_000_000_000_000_000)]
    raw = tmp_path / "profiler.raw"
    raw.write_bytes(
        _header()
        + _block(
            GLOBAL,
            [_thread_info(MAIN_ID, "Main Thread"), _sampler(CPU_ID, "CPU Main Thread Frame Time")],
            0,
        )
        + _block(MAIN_ID, good, 1)
        + _block(MAIN_ID, bogus, 2)
    )
    payload = fc.build_payload(str(raw))

    assert payload["frames"] == [1, 2, 3]
    assert payload["parse_stats"]["dropped_frames"] == 1
    assert [value for _, value in payload["series"]["cpu_frame_ms"]] == [10.0, 12.0, 11.0]
    assert max(payload["times"]) < 1.0


def test_cache_is_reused_until_raw_changes(tmp_path):
    raw = tmp_path / "profiler.raw"
    raw.write_bytes(build_frames([10.0, 12.0]))
    cache = tmp_path / fc.CACHE_FILE
    payload = fc.build_payload(str(raw))
    fc.write_cache(payload, str(cache))

    assert fc.load_cache(str(raw), str(cache)) is not None
    # raw 变了（重新采集）后缓存必须失效，不能拿旧数据出报告。
    os.utime(str(raw), (os.path.getmtime(str(raw)) + 10,) * 2)
    assert fc.load_cache(str(raw), str(cache)) is None


def test_cache_rejects_other_source(tmp_path):
    raw = tmp_path / "profiler.raw"
    raw.write_bytes(build_frames([10.0]))
    cache = tmp_path / fc.CACHE_FILE
    payload = fc.build_payload(str(raw))
    payload["source_bytes"] += 1
    fc.write_cache(payload, str(cache))
    assert fc.load_cache(str(raw), str(cache)) is None


def _make_scene(tmp_path, with_raw=True):
    report = tmp_path / "report"
    log = LogIO(fileroot=str(tmp_path))
    log.report_dir = str(report)
    scene = report / "scene"
    (scene / "unity").mkdir(parents=True)
    (scene / "apm_session.json").write_text(
        json.dumps({"clock": "10:00:00.500000"}), encoding="utf-8"
    )
    (scene / "unity_stats.log").write_text(
        "10:00:01.000000="
        + json.dumps({"cpu_frame_ms": 16.7, "draw_calls": 320, "frame": 1})
        + "\n10:00:02.000000="
        + json.dumps({"cpu_frame_ms": 17.1, "draw_calls": 330, "frame": 61})
        + "\n",
        encoding="utf-8",
    )
    if with_raw:
        raw = scene / "unity" / "profiler.raw"
        raw.write_bytes(build_frames([10.0, 12.0, 11.0, 30.0, 13.0], draws=[100, 101, 102, 103, 104]))
        (scene / "unity" / "meta.json").write_text(
            json.dumps({"started_at": "2026-10-26 10:00:00"}), encoding="utf-8"
        )
        fc.write_cache(
            fc.build_payload(str(raw), str(scene / "unity" / "meta.json")),
            str(scene / "unity" / fc.CACHE_FILE),
        )
    return log, scene


def test_report_uses_frame_series_when_cache_exists(tmp_path):
    log, _ = _make_scene(tmp_path)
    result = log.getUnityStatsLog("Android", "scene")

    assert result["frame_counters"] == "ready"
    assert result["frame_count"] == 5
    # 逐帧加权平均：1 秒采样的两条样本会算成 16.9，逐帧为 15.2。
    assert result["avg"]["cpu_frame_ms"] == 15.2
    assert len(result["charts"]["cpu_frame_ms"]) == 5
    assert len(result["charts"]["frame"]) == 5
    assert result["peaks"]["counters"]["cpu_frame_ms"]["max"] == 30.0
    assert result["peaks"]["hitch"]["count"] == 1


def test_report_falls_back_to_second_log_without_raw(tmp_path):
    log, _ = _make_scene(tmp_path, with_raw=False)
    result = log.getUnityStatsLog("Android", "scene")

    assert result["frame_counters"] == "missing"
    assert result["avg"]["cpu_frame_ms"] in (16.9, 16.91)
    assert len(result["charts"]["cpu_frame_ms"]) == 2
    assert "peaks" not in result


def test_report_falls_back_when_raw_has_no_counters(tmp_path):
    """raw 里只有帧没有计数器时必须退回 1 秒日志，而不是返回空数据。"""
    log, scene = _make_scene(tmp_path, with_raw=False)
    unity = scene / "unity"
    raw = unity / "profiler.raw"
    raw.write_bytes(
        _header()
        + _block(
            GLOBAL,
            [_thread_info(MAIN_ID, "Main Thread"), _thread_info(RENDER_ID, "Render Thread")],
            0,
        )
        + _block(MAIN_ID, [_frame(1, 16_666_667)], 1)
    )
    fc.write_cache(fc.build_payload(str(raw)), str(unity / fc.CACHE_FILE))

    result = log.getUnityStatsLog("Android", "scene")

    assert result["frame_counters"] == "no_counters"
    assert result["avg"]["cpu_frame_ms"] in (16.9, 16.91)
    assert len(result["charts"]["cpu_frame_ms"]) == 2


def test_missing_raw_reports_missing_state(tmp_path):
    scene = tmp_path / "scene"
    scene.mkdir()
    payload, state = fc.ensure(str(scene))
    assert payload is None
    assert state == "missing"
