# -*- encoding: utf-8 -*-
"""Android 设备操作封装"""
import platform
import re
import shlex
import subprocess
import socket
import threading
import time
from logzero import logger
from perfcattle.core.adb_client import adb
from perfcattle.collector.base import adb_shell
from perfcattle.core.platform import Platform


class AndroidDevice:
    """Android 设备操作类"""

    FOCUS_MARKERS = (
        "mCurrentFocus",
        "mFocusedWindow",
        "mFocusedApp",
        "topResumedActivity",
    )
    ACTIVITY_RE = re.compile(r"([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)")
    PACKAGE_RE = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+")
    WIRELESS_LOOKUP_RETRIES = 3
    WIRELESS_POST_CONNECT_RETRIES = 2
    WIRELESS_LOOKUP_DELAY = 0.35
    WIRELESS_RECONNECT_COOLDOWN = 2.0
    _wireless_reconnect_lock = threading.Lock()
    _wireless_reconnect_at = {}

    def __init__(self):
        self.adb = adb.adb_path

    def execCmd(self, cmd, timeout=30):
        """执行终端命令，获取输出结果"""
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, timeout=timeout
            )
            # 先尝试 GBK（Windows 中文环境），再尝试 UTF-8
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
            logger.warning(f"Command timed out: {cmd}")
            return ""
        except subprocess.SubprocessError as e:
            logger.error(f"Command failed: {cmd}, error: {e}")
            return ""

    def filterType(self):
        """根据系统选择管道过滤方式"""
        filtertype = "findstr" if platform.system() == Platform.Windows else "grep"
        return filtertype

    def _parse_device_states(self, output):
        """解析 adb devices 输出，返回设备状态映射。"""
        states = {}
        lines = (output or "").strip().split("\n")
        for line in lines[1:]:  # 跳过表头 "List of devices attached"
            if line.strip():
                parts = line.strip().split()
                if len(parts) >= 2:
                    states[parts[0]] = parts[1]
        return states

    def getDeviceStates(self):
        """获取所有 adb 已知设备的状态。"""
        try:
            result = subprocess.run(
                [self.adb, "devices"], capture_output=True, text=True, timeout=10
            )
            return self._parse_device_states(result.stdout)
        except subprocess.TimeoutExpired:
            logger.warning("adb devices command timed out")
            return {}
        except (OSError, subprocess.SubprocessError) as e:
            logger.error(f"Failed to get device ids: {e}")
            return {}

    def getDeviceIds(self):
        """获取所有已连接的设备 ID"""
        states = self.getDeviceStates()
        not_ready = {
            device_id: state
            for device_id, state in states.items()
            if state != "device"
        }
        if not_ready:
            logger.debug("adb devices has non-ready entries: %s", not_ready)
        return [
            device_id
            for device_id, state in states.items()
            if state == "device"
        ]

    def _is_wireless_adb_id(self, deviceId):
        if not deviceId or deviceId.startswith("emulator-") or ":" not in deviceId:
            return False
        host, port = deviceId.rsplit(":", 1)
        return bool(host and port.isdigit())

    def _wait_for_ready_device(self, deviceId, attempts, delay):
        states = {}
        for _ in range(attempts):
            time.sleep(delay)
            states = self.getDeviceStates()
            if states.get(deviceId) == "device":
                return True, states
        return False, states

    def _connect_wireless_device(self, deviceId):
        with self._wireless_reconnect_lock:
            now = time.monotonic()
            last_reconnect = self._wireless_reconnect_at.get(deviceId, 0)
            if now - last_reconnect < self.WIRELESS_RECONNECT_COOLDOWN:
                logger.debug(
                    "adb reconnect skipped for %s: recently attempted", deviceId
                )
                return
            self._wireless_reconnect_at[deviceId] = now

            self._reconnect_wireless_device(deviceId)

    def _reconnect_wireless_device(self, deviceId):
        try:
            subprocess.run(
                [self.adb, "disconnect", deviceId],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            logger.warning("adb disconnect timed out for %s", deviceId)
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("adb disconnect failed for %s: %s", deviceId, e)

        try:
            result = subprocess.run(
                [self.adb, "connect", deviceId],
                capture_output=True,
                text=True,
                timeout=8,
            )
            output = " ".join(
                part.strip()
                for part in (result.stdout, result.stderr)
                if part and part.strip()
            )
            if result.returncode == 0:
                logger.warning("adb reconnect attempted for %s: %s", deviceId, output)
            else:
                logger.warning("adb reconnect failed for %s: %s", deviceId, output)
        except subprocess.TimeoutExpired:
            logger.warning("adb reconnect timed out for %s", deviceId)
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("adb reconnect failed for %s: %s", deviceId, e)

    def _resolve_android_device_id(self, deviceinfo):
        deviceId = re.sub("\\(.*?\\)|\\{.*?}|\\[.*?]", "", deviceinfo).strip()
        states = self.getDeviceStates()
        if states.get(deviceId) == "device":
            return deviceId

        if self._is_wireless_adb_id(deviceId):
            logger.warning(
                "wireless adb device not ready, retrying: expected=%s, devices=%s",
                deviceId,
                states,
            )
            ready, states = self._wait_for_ready_device(
                deviceId, self.WIRELESS_LOOKUP_RETRIES, self.WIRELESS_LOOKUP_DELAY
            )
            if ready:
                return deviceId

            self._connect_wireless_device(deviceId)
            ready, states = self._wait_for_ready_device(
                deviceId,
                self.WIRELESS_POST_CONNECT_RETRIES,
                self.WIRELESS_LOOKUP_DELAY,
            )
            if ready:
                return deviceId

        logger.warning(
            "adb device not found: expected=%s, devices=%s",
            deviceId,
            states,
        )
        raise Exception("no device found")

    def getDevicesName(self, deviceId):
        """获取 Android 设备名称"""
        try:
            result = subprocess.run(
                [self.adb, "-s", deviceId, "shell", "getprop", "ro.product.model"],
                capture_output=True,
                timeout=10,
            )
            # 先尝试 UTF-8，再尝试 GBK（Windows 中文环境）
            try:
                devices_name = result.stdout.decode("utf-8").strip()
            except UnicodeDecodeError:
                devices_name = result.stdout.decode("gbk", errors="replace").strip()
            return devices_name if devices_name else "Unknown"
        except subprocess.TimeoutExpired:
            logger.warning(f"Get device name timed out for {deviceId}")
            return "Unknown"
        except (OSError, subprocess.SubprocessError) as e:
            logger.error(f"Failed to get device name: {e}")
            return "Unknown"

    def getDevices(self):
        """获取所有 Android 设备（ID + 设备名）"""
        DeviceIds = self.getDeviceIds()
        Devices = [f"{id}({self.getDevicesName(id)})" for id in DeviceIds]
        return Devices

    def getIdbyDevice(self, deviceinfo, platform):
        """根据设备信息获取对应的设备 ID"""
        if platform == Platform.Android:
            deviceId = self._resolve_android_device_id(deviceinfo)
        else:
            deviceId = deviceinfo
        return deviceId

    def getSdkVersion(self, deviceId):
        """获取 Android SDK 版本"""
        version = adb_shell(adb, cmd="getprop ro.build.version.sdk", deviceId=deviceId)
        return version

    def getCpuCores(self, deviceId):
        """获取 Android CPU 核心数"""
        cmd = "cat /sys/devices/system/cpu/online"
        result = adb_shell(adb, cmd=cmd, deviceId=deviceId)
        try:
            nums = int(result.split("-")[1]) + 1
        except (IndexError, ValueError, AttributeError):
            nums = 1
        return nums

    def _process_name_matches(self, name, pkgName):
        name = (name or "").strip().replace("\x00", "")
        return name == pkgName or name.startswith(f"{pkgName}:")

    def _append_process(self, processes, seen_pids, pid, name):
        if not pid or not str(pid).isdigit() or pid in seen_pids:
            return
        processes.append(f"{pid}:{name}")
        seen_pids.add(pid)

    def _parse_pidof_output(self, output, pkgName, processes, seen_pids):
        for pid in (output or "").split():
            if pid.isdigit():
                self._append_process(processes, seen_pids, pid, pkgName)

    def _parse_ps_output(self, output, pkgName, processes, seen_pids):
        for line in (output or "").splitlines():
            parts = line.split()
            if not parts or parts[0].upper() in {"USER", "UID", "PID"}:
                continue

            pid = ""
            name_candidates = []
            for index, value in enumerate(parts[:4]):
                if value.isdigit():
                    pid = value
                    name_candidates.extend(
                        [
                            parts[-1],
                            " ".join(parts[index + 1 :]),
                        ]
                    )
                    break

            for name in name_candidates:
                if self._process_name_matches(name, pkgName):
                    self._append_process(processes, seen_pids, pid, name)
                    break

    def _parse_cmdline_output(self, output, pkgName, processes, seen_pids):
        for line in (output or "").splitlines():
            if ":" not in line:
                continue
            pid, name = line.split(":", 1)
            name = name.strip()
            if self._process_name_matches(name, pkgName):
                self._append_process(processes, seen_pids, pid, name)

    def getPid(self, deviceId, pkgName, fast=False):
        """获取 Android 包名对应的 PID。

        ``fast=True`` 只执行 ``pidof``（必要时加一次 ``ps``），用于界面上的
        后台轮询；完整模式才会继续扫描 ``ps`` / ``/proc`` 以发现子进程。
        """
        processList = []
        seen_pids = set()
        try:
            pidof_output = adb_shell(adb,
                cmd=f"pidof {shlex.quote(pkgName)}", deviceId=deviceId, timeout=5
            )
            self._parse_pidof_output(pidof_output, pkgName, processList, seen_pids)

            if fast:
                if not processList:
                    # pidof is unavailable on a few Android builds; a single
                    # ps call is still far cheaper than the full fallback chain.
                    try:
                        ps_output = adb_shell(adb,
                            cmd="ps -A -o PID,NAME",
                            deviceId=deviceId,
                            timeout=3,
                        )
                        self._parse_ps_output(
                            ps_output, pkgName, processList, seen_pids
                        )
                    except Exception as exc:
                        logger.debug(
                            f"fast pid fallback failed for {pkgName}: {exc}"
                        )
                processList.sort(
                    key=lambda item: (
                        0 if item.split(":", 1)[1] == pkgName else 1,
                        item,
                    )
                )
                if not processList:
                    logger.debug(f"{pkgName}: no pid found (fast)")
                return processList

            for ps_cmd in ("ps -A -o PID,NAME", "ps -ef", "ps"):
                ps_output = adb_shell(adb, cmd=ps_cmd, deviceId=deviceId, timeout=10)
                self._parse_ps_output(ps_output, pkgName, processList, seen_pids)

            if not processList:
                cmdline_output = adb_shell(adb,
                    cmd=(
                        "for p in /proc/[0-9]*; do "
                        "pid=${p##*/}; "
                        "name=$(tr '\\0' ' ' < \"$p/cmdline\" 2>/dev/null); "
                        "echo \"$pid:$name\"; "
                        "done"
                    ),
                    deviceId=deviceId,
                    timeout=10,
                )
                self._parse_cmdline_output(
                    cmdline_output, pkgName, processList, seen_pids
                )

            processList.sort(
                key=lambda item: (
                    0 if item.split(":", 1)[1] == pkgName else 1,
                    item,
                )
            )

            if not processList:
                logger.warning(f"{pkgName}: no pid found")
        except subprocess.TimeoutExpired:
            logger.warning(f"getPid timed out for {pkgName}")
        except Exception as e:
            logger.exception(e)
        return processList

    def checkPkgname(self, pkgname):
        """检查包名是否有效（排除 Google 系统包）"""
        flag = True
        replace_list = ["com.google"]
        for i in replace_list:
            if i in pkgname:
                flag = False
        return flag

    def getPkgname(self, deviceId):
        """获取 Android 设备上的所有包名"""
        def parse_packages(output):
            return [
                p.lstrip("package").lstrip(":").strip()
                for p in output.strip().split("\n")
                if p.strip()
            ]

        try:
            # 优先使用 --user 0 参数
            result = subprocess.run(
                [
                    self.adb,
                    "-s",
                    deviceId,
                    "shell",
                    "pm",
                    "list",
                    "packages",
                    "--user",
                    "0",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            pkglist = parse_packages(result.stdout)
            if pkglist:
                return pkglist

            # 退回到获取所有包名
            result = subprocess.run(
                [self.adb, "-s", deviceId, "shell", "pm", "list", "packages"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return parse_packages(result.stdout)
        except subprocess.TimeoutExpired:
            logger.warning(f"getPkgname timed out for {deviceId}")
            return []
        except (OSError, subprocess.SubprocessError) as e:
            logger.error(f"Failed to get packages: {e}")
            return []

    def get_pc_ip(self):
        """获取本机 IP 地址"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        except Exception:
            logger.error("get local ip failed")
            ip = "127.0.0.1"
        finally:
            s.close()
        return ip

    def get_device_ip(self, deviceId):
        """获取 Android 设备的 WiFi IP 地址"""
        try:
            result = subprocess.run(
                [self.adb, "-s", deviceId, "shell", "ip", "addr", "show", "wlan0"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            content = result.stdout
            math_obj = re.search(r"inet\s(\d+\.\d+\.\d+\.\d+).*?wlan0", content)
            if math_obj and math_obj.group(1):
                return math_obj.group(1)
        except subprocess.TimeoutExpired:
            logger.warning(f"get_device_ip timed out for {deviceId}")
        except (OSError, subprocess.SubprocessError) as e:
            logger.error(f"Failed to get device IP: {e}")
        return None

    def getCurrentActivity(self, deviceId):
        """获取当前 Activity"""
        for cmd in (
            "dumpsys window",
            "dumpsys window windows",
            "dumpsys window displays",
            "dumpsys activity activities",
        ):
            result = adb_shell(adb, cmd=cmd, deviceId=deviceId, timeout=10)
            activity = self._parse_current_activity(result)
            if activity:
                return activity
        raise Exception("No activity found")

    def _parse_current_activity(self, dumpsys_output):
        for marker in self.FOCUS_MARKERS:
            for line in (dumpsys_output or "").splitlines():
                if marker not in line:
                    continue
                match = self.ACTIVITY_RE.search(line)
                if match:
                    return match.group(1).rstrip("}")
        return ""

    def launchApp(self, deviceId, pkgName):
        """按包名启动应用，已在前台时则切回前台。"""
        pkgName = (pkgName or "").strip()
        if not self.PACKAGE_RE.fullmatch(pkgName):
            raise Exception("Invalid package name")

        output = adb_shell(adb,
            cmd=f"monkey -p {pkgName} -c android.intent.category.LAUNCHER 1",
            deviceId=deviceId,
            timeout=15,
        )
        if "Events injected" in (output or ""):
            return True

        detail = (output or "").strip()
        component = self._resolve_launcher_activity(deviceId, pkgName)
        if component:
            started = adb_shell(adb,
                cmd=f"am start -n {component}", deviceId=deviceId, timeout=15
            )
            started = (started or "").strip()
            if started and "Error" not in started and "Exception" not in started:
                return True
            detail = started or detail
        raise Exception(detail or "No launcher activity found for this package")

    def _resolve_launcher_activity(self, deviceId, pkgName):
        output = adb_shell(adb,
            cmd=f"cmd package resolve-activity --brief {pkgName}",
            deviceId=deviceId,
            timeout=10,
        )
        for line in reversed((output or "").splitlines()):
            line = line.strip()
            if self.ACTIVITY_RE.fullmatch(line):
                return line
        return ""

    def getStartupTimeByAndroid(self, activity, deviceId):
        """获取 Android 应用启动时间"""
        return self._run_startup_time(activity, deviceId)

    def _run_startup_time(self, activity, deviceId):
        try:
            result = subprocess.run(
                [self.adb, "-s", deviceId, "shell", "am", "start", "-W", activity],
                capture_output=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired as exc:
            output = self._decode_process_output(exc.output)
            error = self._decode_process_output(exc.stderr)
            detail = "\n".join(part for part in (output, error) if part)
            message = "am start -W timed out after 20s"
            if detail:
                message = f"{message}\n{detail}"
            raise Exception(message)
        except (OSError, subprocess.SubprocessError) as exc:
            raise Exception(f"am start -W failed: {exc}")

        output = self._decode_process_output(result.stdout)
        error = self._decode_process_output(result.stderr)
        detail = "\n".join(part for part in (output, error) if part)

        if result.returncode != 0:
            raise Exception(self._startup_error_message(detail))
        if not self._has_startup_timing(output):
            raise Exception(self._startup_error_message(detail))
        return output

    @staticmethod
    def _decode_process_output(value):
        if not value:
            return ""
        if isinstance(value, str):
            return value.strip()
        try:
            return value.decode("utf-8").strip()
        except UnicodeDecodeError:
            return value.decode("gbk", errors="replace").strip()

    @staticmethod
    def _has_startup_timing(output):
        return any(key in (output or "") for key in ("ThisTime:", "TotalTime:", "WaitTime:"))

    @staticmethod
    def _startup_error_message(detail):
        if not detail:
            return "Start-up time unavailable: am start -W returned no timing data"
        if "SecurityException" in detail and "not exported" in detail:
            return (
                "Start-up time unavailable: the activity is not exported and cannot "
                "be started by adb shell. Use the app's exported launcher activity "
                "instead.\n"
                f"{detail}"
            )
        return f"Start-up time unavailable: am start -W returned no timing data\n{detail}"
