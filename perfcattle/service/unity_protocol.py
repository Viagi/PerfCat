# -*- coding: utf-8 -*-
"""向后兼容入口：PlayerConnection 协议已迁移到 perfcattle.unity.protocol。"""
from perfcattle.unity.protocol import (  # noqa: F401
    DEFAULT_CAPTURE_FLAGS,
    MAGIC,
    Frame,
    UnityPlayerInfo,
    UnityProtocolError,
    UnityProtocolSession,
    UnsupportedUnityVersion,
    build_packet,
    guid_from_hex,
    parse_player_info,
    read_frame,
)

__all__ = [
    "DEFAULT_CAPTURE_FLAGS",
    "MAGIC",
    "Frame",
    "UnityPlayerInfo",
    "UnityProtocolError",
    "UnityProtocolSession",
    "UnsupportedUnityVersion",
    "build_packet",
    "guid_from_hex",
    "parse_player_info",
    "read_frame",
]
