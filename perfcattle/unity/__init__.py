# -*- coding: utf-8 -*-
"""Unity Profiler / MemoryProfiler 采集扩展包。

模块边界：

- ``profiles``  版本与消息表注册中心（新增 Unity 版本只改这里）
- ``protocol``  PlayerConnection 线协议与帧收发
- ``backends``  helper / protocol 两种传输后端
- ``manager``   会话、暂存目录、meta 与报告归档
- ``export``    profiler.raw 的离线 JSON 导出（不依赖 Unity）

技术文档：docs/UNITY_CAPTURE.md
"""
from perfcattle.unity.profiles import (  # noqa: F401
    PROFILES,
    UnityProfile,
    get_profile,
    supported_labels,
    supported_versions,
)

__all__ = [
    "PROFILES",
    "UnityProfile",
    "get_profile",
    "supported_labels",
    "supported_versions",
]
