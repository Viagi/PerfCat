# -*- encoding: utf-8 -*-
"""设备管理层"""
from perfcat.device.manager import Devices
from perfcat.device.android import AndroidDevice
from perfcat.device.ios import iOSDevice

__all__ = ["Devices", "AndroidDevice", "iOSDevice"]
