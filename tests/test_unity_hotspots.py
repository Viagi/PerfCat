"""Frame hotspot extraction: Timeline threads, merged and raw call trees."""
import struct

from perfcattle.unity.hotspots import HotspotReader
from tests.test_unity_raw_reader import (
    BLOCK_SIG,
    FOOTER_SIG,
    GLOBAL,
    SESSION_SIG,
    VERSION,
    _block,
    _message,
    _sampler,
    _string,
    _thread_info,
)

MAIN_ID = 0x1111
RENDER_ID = 0x2222


def _header():
    return (
        struct.pack("<IBBH I Q Q Q", SESSION_SIG, 1, 0, 11, VERSION, 1, 1, MAIN_ID)
        + struct.pack("<5i", 2022, 3, 62, 2, 2)
    )


def _frame(index, time):
    return _message(
        34, struct.pack("<I", index) + struct.pack("<Q", time)
    )


def _begin(sampler_id, time):
    return _message(
        36, b"\x00" + struct.pack("<I", sampler_id) + struct.pack("<Q", time)
    )


def _end(sampler_id, time):
    return _message(
        37, b"\x00" + struct.pack("<I", sampler_id) + struct.pack("<Q", time)
    )


def _gc(size):
    return _message(52, struct.pack("<I", size))


def _stream(tmp_path):
    t1 = 1_000_000_000
    t2 = 1_100_000_000
    t3 = 1_200_000_000
    header = _header()
    meta = _block(
        GLOBAL,
        [
            _thread_info(MAIN_ID, "Main Thread"),
            _thread_info(RENDER_ID, "Render Thread"),
            _sampler(0x10, "Main Root"),
            _sampler(0x11, "Main Child"),
            _sampler(0x20, "Render Work"),
        ],
        0,
    )
    # kFrame marks the END of a frame: frame 2 spans [t1, t2].
    frame1 = _block(MAIN_ID, [_frame(1, t1)], 1)
    frame2 = _block(MAIN_ID, [_frame(2, t2)], 2)
    main_samples = _block(
        MAIN_ID,
        [
            _begin(0x10, t1 + 1_000_000),
            _begin(0x11, t1 + 5_000_000),
            _gc(2048),
            _end(0x11, t1 + 15_000_000),
            _end(0x10, t1 + 31_000_000),
        ],
        3,
    )
    render_samples = _block(
        RENDER_ID,
        [
            _begin(0x20, t1 + 20_000_000),
            _end(0x20, t1 + 30_000_000),
        ],
        4,
    )
    frame3 = _block(MAIN_ID, [_frame(3, t3)], 5)
    path = tmp_path / "profiler.raw"
    path.write_bytes(header + meta + frame1 + frame2 + main_samples + render_samples + frame3)
    return str(path)


def test_frame_hotspots(tmp_path):
    reader = HotspotReader(_stream(tmp_path))
    frames = reader.frames()
    assert [item["index"] for item in frames] == [1, 2, 3]

    data = reader.frame(2, "main")
    assert data["status"] == 1
    assert data["duration_us"] == 100000.0
    names = {item["name"]: item for item in data["threads"]}
    assert "Main Thread" in names and "Render Thread" in names
    assert names["Main Thread"]["samples"] == 2
    assert names["Render Thread"]["samples"] == 1

    assert len(data["hierarchy"]) == 1
    root = data["hierarchy"][0]
    assert root["name"] == "Main Root"
    assert root["total_us"] == 30000.0
    assert root["self_us"] == 20000.0
    assert root["calls"] == 1
    assert len(root["children"]) == 1
    child = root["children"][0]
    assert child["name"] == "Main Child"
    assert child["total_us"] == 10000.0
    assert child["gc"] == 2048

    # Raw hierarchy is unmerged and keeps one node per actual call.
    assert data["raw_hierarchy"][0]["name"] == "Main Root"
    assert data["raw_hierarchy"][0]["children"][0]["calls"] == 1


def test_thread_selection(tmp_path):
    reader = HotspotReader(_stream(tmp_path))
    data = reader.frame(2, "render")
    assert len(data["hierarchy"]) == 1
    assert data["hierarchy"][0]["name"] == "Render Work"
    assert data["hierarchy"][0]["total_us"] == 10000.0


def test_incomplete_stream_is_flagged(tmp_path):
    path = _stream(tmp_path)
    raw = open(path, "rb").read()
    # Drop the last block footer + a byte of its payload.
    open(path, "wb").write(raw[:-9])

    reader = HotspotReader(path)
    info = reader.info()
    assert info["complete"] is False
    assert info["error"] in ("truncated block", "bad footer signature", "bad block signature")
    assert info["frame_count"] >= 0
