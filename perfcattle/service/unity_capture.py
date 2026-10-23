# -*- coding: utf-8 -*-
"""向后兼容入口：Unity 采集管理器已迁移到 perfcattle.unity.manager。"""
from perfcattle.unity.manager import UnityCapture, unity_capture

__all__ = ["UnityCapture", "unity_capture"]
