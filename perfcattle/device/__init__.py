# -*- encoding: utf-8 -*-
"""设备管理层"""
from perfcattle.device.manager import Devices
from perfcattle.device.android import AndroidDevice
from perfcattle.device.ios import iOSDevice

__all__ = ["Devices", "AndroidDevice", "iOSDevice"]
