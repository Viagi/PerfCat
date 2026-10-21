# -*- encoding: utf-8 -*-
"""跨平台设备管理器"""
from logzero import logger
from perfcat.core.platform import Platform
from perfcat.core.adb_client import adb
from perfcat.tools.http_utils import Method
from perfcat.device.android import AndroidDevice
from perfcat.device.ios import iOSDevice
from tidevice._device import Device


class Devices:
    """跨平台设备管理器，统一调度 Android 和 iOS 设备操作。"""

    def __init__(self, platform=Platform.Android):
        self.platform = platform
        self.adb = adb.adb_path
        self._android = AndroidDevice()
        self._ios = iOSDevice()

    # ========== Android 方法委托 ==========

    def execCmd(self, cmd, timeout=30):
        """执行终端命令，获取输出结果"""
        return self._android.execCmd(cmd, timeout)

    def filterType(self):
        """根据系统选择管道过滤方式"""
        return self._android.filterType()

    def getDeviceIds(self):
        """获取所有已连接的设备 ID"""
        return self._android.getDeviceIds()

    def getDevicesName(self, deviceId):
        """获取 Android 设备名称"""
        return self._android.getDevicesName(deviceId)

    def getDevices(self):
        """获取所有 Android 设备（ID + 设备名）"""
        return self._android.getDevices()

    def getIdbyDevice(self, deviceinfo, platform):
        """根据设备信息获取对应的设备 ID"""
        return self._android.getIdbyDevice(deviceinfo, platform)

    def getSdkVersion(self, deviceId):
        """获取 Android SDK 版本"""
        return self._android.getSdkVersion(deviceId)

    def getCpuCores(self, deviceId):
        """获取 Android CPU 核心数"""
        return self._android.getCpuCores(deviceId)

    def getPid(self, deviceId, pkgName):
        """获取 Android 包名对应的 PID"""
        return self._android.getPid(deviceId, pkgName)

    def checkPkgname(self, pkgname):
        """检查包名是否有效"""
        return self._android.checkPkgname(pkgname)

    def getPkgname(self, deviceId):
        """获取 Android 设备上的所有包名"""
        return self._android.getPkgname(deviceId)

    def get_pc_ip(self):
        """获取本机 IP 地址"""
        return self._android.get_pc_ip()

    def get_device_ip(self, deviceId):
        """获取 Android 设备的 WiFi IP 地址"""
        return self._android.get_device_ip(deviceId)

    def getCurrentActivity(self, deviceId):
        """获取当前 Activity"""
        return self._android.getCurrentActivity(deviceId)

    def getStartupTimeByAndroid(self, activity, deviceId):
        """获取 Android 应用启动时间"""
        return self._android.getStartupTimeByAndroid(activity, deviceId)

    # ========== iOS 方法委托 ==========

    def getProcessesByiOS(self, deviceId, package):
        return self._ios.getProcessesByiOS(deviceId, package)

    def getDeviceInfoByiOS(self):
        """获取所有已连接的 iOS 设备 UDID 列表"""
        return self._ios.getDeviceInfoByiOS()

    def getPkgnameByiOS(self, udid):
        """获取 iOS 设备上所有用户安装的包名"""
        return self._ios.getPkgnameByiOS(udid)

    def getPhysicalSizeOfiOS(self, deviceId):
        """获取 iOS 设备的物理屏幕尺寸"""
        return self._ios.getPhysicalSizeOfiOS(deviceId)

    def getStartupTimeByiOS(self, pkgname):
        """获取 iOS 应用启动时间"""
        return self._ios.getStartupTimeByiOS(pkgname)

    # ========== 跨平台方法 ==========

    def devicesCheck(self, platform, deviceid=None, pkgname=None):
        """检查设备环境"""
        match platform:
            case Platform.Android:
                if len(self.getDeviceIds()) == 0:
                    raise Exception("no devices found")
                if len(self.getPid(deviceId=deviceid, pkgName=pkgname)) == 0:
                    raise Exception("no process found")
            case Platform.iOS:
                if len(self.getDeviceInfoByiOS()) == 0:
                    raise Exception("no devices found")
            case _:
                raise Exception("platform must be Android or iOS")

    def getDeviceDetail(self, deviceId, platform):
        """获取设备详细信息"""
        result = dict()
        match platform:
            case Platform.Android:
                result["brand"] = adb.shell(
                    cmd="getprop ro.product.brand", deviceId=deviceId
                )
                result["name"] = adb.shell(
                    cmd="getprop ro.product.model", deviceId=deviceId
                )
                result["version"] = adb.shell(
                    cmd="getprop ro.build.version.release", deviceId=deviceId
                )
                result["serialno"] = adb.shell(
                    cmd="getprop ro.serialno", deviceId=deviceId
                )
                cmd = f"ip addr show wlan0 | {self.filterType()} link/ether"
                wifiadr_content = adb.shell(cmd=cmd, deviceId=deviceId)
                result["wifiadr"] = Method._index(wifiadr_content.split(), 1, "")
                result["cpu_cores"] = self.getCpuCores(deviceId)
                result["physical_size"] = (
                    adb.shell(cmd="wm size", deviceId=deviceId)
                    .replace("Physical size:", "")
                    .strip()
                )
            case Platform.iOS:
                ios_device = Device(udid=deviceId)
                result["brand"] = ios_device.get_value("DeviceClass", no_session=True)
                result["name"] = ios_device.get_value("DeviceName", no_session=True)
                result["version"] = ios_device.get_value(
                    "ProductVersion", no_session=True
                )
                result["serialno"] = deviceId
                result["wifiadr"] = ios_device.get_value("WiFiAddress", no_session=True)
                result["cpu_cores"] = 0
                result["physical_size"] = self.getPhysicalSizeOfiOS(deviceId)
            case _:
                raise Exception("{} is undefined".format(platform))
        return result
