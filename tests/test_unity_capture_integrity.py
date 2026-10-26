"""Unity stream integrity regressions: truncated frames and clean-stop state."""
import struct

import pytest

from perfcattle.unity.protocol import (
    MAGIC,
    UnityProtocolError,
    build_packet,
    guid_from_hex,
    read_frame,
)


class _FakeSocket:
    def __init__(self, chunks):
        self._buffer = b"".join(chunks)

    def settimeout(self, timeout):
        pass

    def recv(self, size):
        if not self._buffer:
            return b""
        out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out


def _frame_header(guid_hex, length):
    return (
        struct.pack("<I", MAGIC)
        + guid_from_hex(guid_hex)
        + struct.pack("<I", length)
    )


def test_read_frame_raises_on_truncated_payload():
    header = _frame_header("0" * 32, 10)
    sock = _FakeSocket([header, b"short"])  # payload declared 10, only 5 bytes

    with pytest.raises(UnityProtocolError, match="truncated"):
        read_frame(sock, timeout=1)


def test_read_frame_returns_none_on_clean_header_eof():
    sock = _FakeSocket([b""])  # closed before any header bytes

    assert read_frame(sock, timeout=1) is None


def test_read_frame_reads_complete_frame():
    guid = "0" * 32
    payload = b"1234567890"
    packet = build_packet(guid, payload)
    sock = _FakeSocket([packet])

    frame = read_frame(sock, timeout=1)
    assert frame.payload == payload


def test_session_has_areas_attribute_by_default():
    from perfcattle.unity.protocol import UnityProtocolSession

    session = UnityProtocolSession("device", "com.example.game")
    # Regression: begin_record referenced self.areas, which was only defined
    # on the backend, not the session. The session must own the attribute.
    assert session.areas is None
