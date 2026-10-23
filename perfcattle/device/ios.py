# -*- encoding: utf-8 -*-
"""iOS 设备操作封装"""
from logzero import logger
from tidevice._device import Device
from tidevice import Usbmux


class iOSDevice:
    """iOS 设备操作类"""

    def getDeviceInfoByiOS(self):
        """获取所有已连接的 iOS 设备 UDID 列表"""
        deviceInfo = [udid for udid in Usbmux().device_udid_list()]
        return deviceInfo

    def getPkgnameByiOS(self, udid):
        """获取 iOS 设备上所有用户安装的包名"""
        d = Device(udid)
        pkgNames = [
            i.get("CFBundleIdentifier")
            for i in d.installation.iter_installed(app_type="User")
        ]
        return pkgNames

    def getProcessesByiOS(self, udid, package):
        import json
        from pathlib import Path
        import subprocess
        from perfcattle.service.ios_dependencies import IOSDependencies, ios_dependencies

        version = IOSDependencies.device_version(udid, timeout=5)
        if int(version.split(".")[0]) < 17:
            raise RuntimeError("Process selection requires iOS 17 or later; use the default app target on older iOS.")
        runtime = ios_dependencies["modern"]
        if not runtime.python.is_file():
            raise RuntimeError("Start collection to check and install missing iOS dependencies first.")
        worker = Path(__file__).resolve().parents[1] / "collector" / "ios_modern_worker.py"
        result = subprocess.run(
            [str(runtime.python), "-u", str(worker), "--list-processes", udid, package],
            capture_output=True, text=True, encoding="utf-8", timeout=35,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise RuntimeError("iOS process listing failed. Check the local runtime and device connection.")
        data = json.loads(result.stdout)
        if data.get("status") != 1:
            raise RuntimeError(data.get("error") or "iOS process list unavailable")
        return data["processes"]

    def getPhysicalSizeOfiOS(self, deviceId):
        """获取 iOS 设备的物理屏幕尺寸"""
        ios_device = Device(udid=deviceId)
        try:
            if int(ios_device.get_value("ProductVersion").split(".")[0]) >= 17:
                # The legacy screen-info service attempts obsolete DeviceSupport downloads.
                return ""
            screen_info = ios_device.screen_info()
            PhysicalSzie = "{}x{}".format(
                screen_info.get("width"), screen_info.get("height")
            )
        except Exception as e:
            PhysicalSzie = ""
            logger.exception(e)
        return PhysicalSzie

    def getStartupTimeByiOS(self, pkgname):
        """获取 iOS 应用启动时间"""
        import sys
        from perfcattle.service.ios_dependencies import ios_dependencies
        runtime = ios_dependencies['legacy']
        if runtime.check()['state'] == 'ready':
            python = str(runtime.python)
        else:
            try:
                import ios_device
            except ImportError:
                raise RuntimeError("Start collection to check and install py-ios-device first.")
            python = sys.executable
        import subprocess
        try:
            result = subprocess.run(
                [python, "-c", "from ios_device.main import cli; cli()",
                 "instruments", "app_lifecycle", "-b", pkgname],
                capture_output=True,
                timeout=30,
            )
            try:
                text = (
                    result.stdout.decode(encoding="gbk").replace("\x1b[0m", "").strip()
                )
            except UnicodeDecodeError:
                text = (
                    result.stdout.decode(encoding="utf-8", errors="replace")
                    .replace("\x1b[0m", "")
                    .strip()
                )
            return text
        except subprocess.TimeoutExpired:
            logger.warning(f"getStartupTimeByiOS timed out for {pkgname}")
            return ""
        except subprocess.SubprocessError as e:
            logger.error(f"getStartupTimeByiOS failed: {e}")
            return ""
