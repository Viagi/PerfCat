"""Raw PD3U profiler stream counter extraction."""
import struct

from perfcattle.unity.raw_reader import RawStatsParser

SESSION_SIG = 0x55334450
BLOCK_SIG = 0xB10C7EAD
FOOTER_SIG = 0xB10CF007
GLOBAL = 0xFFFFFFFFFFFFFFFF
VERSION = 539099944

MAIN_ID = 0x1111
RENDER_ID = 0x2222


def _string(value):
    raw = value.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def _header():
    return (
        struct.pack("<IBBH I Q Q Q", SESSION_SIG, 1, 0, 11, VERSION, 1, 1, 0)
        + struct.pack("<5i", 2022, 3, 62, 2, 2)
    )


def _message(message_type, payload):
    return struct.pack("<H", message_type) + payload


def _thread_info(thread_id, name):
    return _message(
        33,
        struct.pack("<Q", thread_id)
        + struct.pack("<Q", 0)
        + struct.pack("<I", 0)
        + _string("")
        + _string(name),
    )


def _sampler(sampler_id, name):
    return _message(
        1,
        struct.pack("<I", sampler_id)
        + struct.pack("<H", 0x88)
        + struct.pack("<H", 0)
        + _string(name)
        + b"\x00",
    )


def _frame(index):
    return _message(34, struct.pack("<I", index) + struct.pack("<Q", index * 1000))


def _counter(sampler_id, value, sample_time=0):
    return _message(
        44,
        b"\x00"
        + struct.pack("<I", sampler_id)
        + struct.pack("<Q", sample_time)
        + b"\x01"
        + b"\x05"
        + struct.pack("<Q", value),
    )


def _block(thread_id, messages, block_id=0):
    data = b"".join(messages)
    return (
        struct.pack("<IIQI", BLOCK_SIG, block_id, thread_id, len(data))
        + data
        + struct.pack("<II", block_id + 1, FOOTER_SIG)
    )


def build_stream():
    samplers = [
        _sampler(0x6A2, "Batches Count"),
        _sampler(0x6A3, "Draw Calls Count"),
        _sampler(0x6A4, "Triangles Count"),
        _sampler(0x6A5, "Vertices Count"),
        _sampler(0x6A6, "Render Textures Count"),
        _sampler(0x6A7, "Render Textures Bytes"),
        _sampler(0x6A8, "Render Textures Changes Count"),
        _sampler(0x6A9, "Used Buffers Count"),
        _sampler(0x6AA, "Used Buffers Bytes"),
        _sampler(0x6AB, "Vertex Buffer Upload In Frame Count"),
        _sampler(0x6AC, "Vertex Buffer Upload In Frame Bytes"),
        _sampler(0x6AD, "Index Buffer Upload In Frame Count"),
        _sampler(0x6AE, "Index Buffer Upload In Frame Bytes"),
        _sampler(0x6C1, "CPU Main Thread Frame Time"),
        _sampler(0x6C3, "GPU Frame Time"),
    ]
    global_block = _block(GLOBAL, [_thread_info(MAIN_ID, "Main Thread"),
                                   _thread_info(RENDER_ID, "Render Thread")] + samplers, 0)
    main_block = _block(
        MAIN_ID,
        [_frame(42), _counter(0x6C1, 12_500_000), _counter(0x6C3, 20_000_000)],
        1,
    )
    render_block = _block(
        RENDER_ID,
        [
            _counter(0x6A2, 120),
            _counter(0x6A3, 300),
            _counter(0x6A4, 1000),
            _counter(0x6A5, 1200),
            _counter(0x6A6, 55),
            _counter(0x6A7, 50_436_506),
            _counter(0x6A8, 12),
            _counter(0x6A9, 1356),
            _counter(0x6AA, 53_372_518),
            _counter(0x6AB, 16),
            _counter(0x6AC, 144_077),
            _counter(0x6AD, 16),
            _counter(0x6AE, 9830),
        ],
        2,
    )
    return _header() + global_block + main_block + render_block


def test_extracts_counters_and_geometry():
    parser = RawStatsParser()
    parser.feed(build_stream())
    snap = parser.snapshot()
    assert snap["available"] is True
    assert snap["unity_version"] == "2022.3.62"
    assert snap["frame"] == 42
    assert snap["values"]["draw_calls"] == 300
    assert snap["values"]["triangles"] == 1000
    assert snap["values"]["vertices"] == 1200
    assert snap["values"]["batches"] == 120
    assert snap["values"]["cpu_frame_ms"] == 12.5
    assert snap["values"]["gpu_frame_ms"] == 20.0
    assert snap["values"]["render_textures"] == 55
    assert snap["values"]["render_textures_mb"] == 48.1
    assert snap["values"]["render_textures_changes"] == 12
    assert snap["values"]["used_buffers"] == 1356
    assert snap["values"]["used_buffers_mb"] == 50.9
    assert snap["values"]["vertex_upload"] == 16
    assert snap["values"]["vertex_upload_kb"] == 140.7
    assert snap["values"]["index_upload"] == 16
    assert snap["values"]["index_upload_kb"] == 9.6


def test_chunked_feed_matches_single_feed():
    stream = build_stream()
    parser = RawStatsParser()
    for index in range(0, len(stream), 7):
        parser.feed(stream[index:index + 7])
    snap = parser.snapshot()
    assert snap["values"]["draw_calls"] == 300
    assert snap["values"]["triangles"] == 1000
    assert snap["values"]["vertices"] == 1200
    assert snap["values"]["render_textures_mb"] == 48.1
    assert snap["stats"]["lost_bytes"] == 0


def test_unsupported_version_reports_reason():
    header = (
        struct.pack("<IBBH I Q Q Q", SESSION_SIG, 1, 0, 11, 538513698, 1, 1, 0)
        + struct.pack("<5i", 2020, 1, 0, 0, 0)
    )
    parser = RawStatsParser()
    parser.feed(header)
    snap = parser.snapshot()
    assert snap["available"] is False
    assert snap["reason"] == "unsupported_version"


def test_unknown_message_does_not_break_next_block():
    header = _header()
    global_block = _block(
        GLOBAL,
        [_thread_info(MAIN_ID, "Main Thread"), _thread_info(RENDER_ID, "Render Thread"),
         _sampler(0x6A3, "Draw Calls Count")],
    )
    bad_block = _block(RENDER_ID, [_message(999, b"\x00\x01\x02")], 1)
    good_block = _block(RENDER_ID, [_counter(0x6A3, 321)], 2)
    parser = RawStatsParser()
    parser.feed(header + global_block + bad_block + good_block)
    snap = parser.snapshot()
    assert snap["values"]["draw_calls"] == 321
    assert snap["stats"]["bad"] == 1


def test_batching_counters_are_mapped():
    """Draw Call breakdown counters (SetPass / dynamic / static / instanced)
    must be extracted from the render thread stream."""
    from perfcattle.unity.raw_reader import RENDER_COUNTERS

    expected = {
        "Draw Calls Count": "draw_calls",
        "SetPass Calls Count": "setpass_calls",
        "Batches Count": "batches",
        "Static Batches Count": "static_batches",
        "Dynamic Batches Count": "dynamic_batches",
        "Instanced Batches Count": "instanced_batches",
        "Static Batched Draw Calls Count": "static_batched_draws",
        "Dynamic Batched Draw Calls Count": "dynamic_batched_draws",
        "Instanced Batched Draw Calls Count": "instanced_batched_draws",
    }
    for marker, key in expected.items():
        assert RENDER_COUNTERS[marker][0] == key
