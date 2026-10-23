# -*- coding: utf-8 -*-
"""iOS 能耗采集器"""
import sys
import time
from typing import Optional

from logzero import logger
from tidevice._types import ConnectionType
from tidevice._usbmux import Usbmux
from tidevice._device import Device


class Energy(object):
    def __init__(self, deviceId, packageName):
        self.deviceId = deviceId
        self.packageName = packageName

    def _get_usbmux(self):
        """延迟创建 Usbmux 实例"""
        return Usbmux()

    def _complete_udid(self, udid: Optional[str] = None) -> str:
        um = self._get_usbmux()
        infos = um.device_list()
        # 精确匹配 udid
        for info in infos:
            if info.udid == udid:
                return udid
        if udid:
            sys.exit("Device for %s not detected" % udid)

        if len(infos) == 1:
            return infos[0].udid

        # 仅筛选 USB 连接的设备
        infos = [info for info in infos if info.conn_type == ConnectionType.USB]
        if not udid:
            if len(infos) >= 2:
                sys.exit("More than 2 usb devices detected")
            if len(infos) == 0:
                sys.exit("No local device detected")
            return infos[0].udid
        raise RuntimeError("No matched device", udid)

    def _udid2device(self, udid: Optional[str] = None) -> Device:
        um = self._get_usbmux()
        _udid = self._complete_udid(udid)
        if _udid != udid:
            logger.debug("AutoComplete udid %s", _udid)
        del udid
        return Device(_udid, um)

    def getEnergy(self):
        """获取 iOS 能耗数据"""
        d = self._udid2device(self.deviceId)
        if int(d.get_value("ProductVersion").split(".")[0]) >= 17:
            from perfcattle.collector.ios_perf import IOSPerformance
            return IOSPerformance(self.packageName, self.deviceId).getPerformance("energy")
        ts = d.connect_instruments()
        pid = ts.app_launch(self.packageName)
        ts.start_energy_sampling(pid)
        time.sleep(1.0)
        ret = ts.get_process_energy_stats(pid)
        return ret
