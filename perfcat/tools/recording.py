# -*- encoding: utf-8 -*-
"""屏幕录制工具模块。

使用 scrcpy 进行屏幕录制和投屏。
"""
from __future__ import annotations

import os
import subprocess
import time
import platform
import signal
import re
import json
import functools
import shutil
from datetime import datetime
from logzero import logger
import cv2


class Scrcpy:
    """scrcpy 屏幕录制与投屏工具"""

    _ASSETS_PATH = os.path.join(
        os.path.dirname(os.path.realpath(__file__)),
        os.pardir,
        "assets",
    )
    DEFAULT_SCRCPY_PATH = {
        "64": os.path.join(
            _ASSETS_PATH, "scrcpy", "scrcpy-win64-v4.0", "scrcpy.exe"
        ),
        "32": os.path.join(
            _ASSETS_PATH, "scrcpy", "scrcpy-win32-v4.0", "scrcpy.exe"
        ),
        "default": "scrcpy",
    }
    RECORD_MAX_SIZE = "720"
    RECORD_MAX_FPS = "60"
    RECORD_VIDEO_BIT_RATE = "4M"
    RECORD_NO_AUDIO = True
    RECORD_SCREEN_OFF_TIMEOUT = "3600"

    @classmethod
    def scrcpy_path(cls):
        """根据系统平台和架构选择 scrcpy 可执行文件路径"""
        bit = platform.architecture()[0]
        path = cls.DEFAULT_SCRCPY_PATH["default"]
        if "windows" in platform.system().lower():
            if "64" in bit:
                path = cls.DEFAULT_SCRCPY_PATH["64"]
            elif "32" in bit:
                path = cls.DEFAULT_SCRCPY_PATH["32"]
        return path

    @staticmethod
    def _valid_executable(path):
        if not path:
            return False
        resolved = shutil.which(path) if os.path.basename(path) == path else path
        return bool(
            resolved and os.path.isfile(resolved) and os.path.getsize(resolved) > 0
        )

    @classmethod
    def _companion_adb_path(cls):
        scrcpy_path = cls.scrcpy_path()
        if not scrcpy_path or not os.path.isabs(scrcpy_path):
            return None

        scrcpy_dir = os.path.dirname(scrcpy_path)
        for name in ("adb.exe", "adb"):
            candidate = os.path.join(scrcpy_dir, name)
            if cls._valid_executable(candidate):
                return candidate
        return None

    @classmethod
    def adb_path(cls):
        """返回 scrcpy/display 检测应使用的 adb，尽量与采集链路一致。"""
        try:
            from perfcat.core.adb_client import adb

            if cls._valid_executable(adb.adb_path):
                return adb.adb_path
        except Exception as exc:
            logger.debug("Failed to get PerfCat adb path: %s", exc)

        companion_adb = cls._companion_adb_path()
        if companion_adb:
            return companion_adb
        return None

    @classmethod
    def _scrcpy_cwd(cls):
        scrcpy_path = cls.scrcpy_path()
        if scrcpy_path and os.path.isabs(scrcpy_path):
            scrcpy_dir = os.path.dirname(scrcpy_path)
            if os.path.isdir(scrcpy_dir):
                return scrcpy_dir
        return None

    @classmethod
    def _scrcpy_env(cls):
        env = os.environ.copy()
        adb_path = cls.adb_path()
        if adb_path:
            env["ADB"] = adb_path
        return env

    @staticmethod
    def _option_value(config, *names):
        if not config:
            return None
        for name in names:
            try:
                value = config.get(name)
            except AttributeError:
                value = config[name] if name in config else None
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _positive_int_option(value, default):
        try:
            number = int(str(value).strip())
            if number > 0:
                return str(number)
        except (TypeError, ValueError):
            pass
        return str(default)

    @staticmethod
    def _bit_rate_option(value, default):
        text = str(value or "").strip()
        if re.match(r"^\d+[KkMmGg]?$", text):
            return text.upper()
        return str(default)

    @staticmethod
    def _bool_option(value, default):
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        return default

    @classmethod
    def record_options(cls, config=None):
        """Normalize configurable scrcpy recording options."""
        max_size = cls._option_value(
            config, "max_size", "record_max_size", "recordMaxSize"
        )
        max_fps = cls._option_value(
            config, "max_fps", "record_max_fps", "recordMaxFps"
        )
        bit_rate = cls._option_value(
            config, "video_bit_rate", "record_video_bit_rate", "recordVideoBitRate"
        )
        no_audio = cls._option_value(
            config, "no_audio", "record_no_audio", "recordNoAudio"
        )
        screen_off_timeout = cls._option_value(
            config,
            "screen_off_timeout",
            "record_screen_off_timeout",
            "recordScreenOffTimeout",
        )

        return {
            "max_size": cls._positive_int_option(max_size, cls.RECORD_MAX_SIZE),
            "max_fps": cls._positive_int_option(max_fps, cls.RECORD_MAX_FPS),
            "video_bit_rate": cls._bit_rate_option(
                bit_rate, cls.RECORD_VIDEO_BIT_RATE
            ),
            "no_audio": cls._bool_option(no_audio, cls.RECORD_NO_AUDIO),
            "screen_off_timeout": cls._positive_int_option(
                screen_off_timeout, cls.RECORD_SCREEN_OFF_TIMEOUT
            ),
        }

    @classmethod
    def get_display_id(cls, device):
        """获取合适的显示 ID 用于录制

        Android 模拟器（MuMu、雷电、蓝叠等）通常在副屏运行应用/游戏。
        此方法检测哪个显示器有活动任务并返回合适的显示 ID。

        策略：
        1. 检查 'dumpsys window displays' 找到 rootTasks > 0 的显示器
        2. 在有活动任务的显示器中，优先选择非零显示 ID（副屏）
        3. 检测失败时回退到空字符串（默认显示器）

        Returns:
            str: display_id 字符串或空字符串（默认显示器）
        """
        try:
            adb_path = cls.adb_path() or "adb"
            # 使用 'dumpsys window displays' 查找有活动任务的显示器
            result = subprocess.run(
                [adb_path, "-s", device, "shell", "dumpsys", "window", "displays"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                # 查找 rootTasks > 0 的显示器（有活动窗口）
                # 匹配模式: "Display: mDisplayId=X rootTasks=N"
                pattern = r"Display:\s+mDisplayId=(\d+)\s+rootTasks=(\d+)"
                matches = re.findall(pattern, result.stdout)

                if matches:
                    # 过滤有活动任务的显示器（rootTasks > 0）
                    active_displays = [
                        int(display_id)
                        for display_id, root_tasks in matches
                        if int(root_tasks) > 0
                    ]

                    if active_displays:
                        # 选择最高的有活动任务的显示 ID
                        max_active = max(active_displays)

                        if max_active > 0:
                            # 使用副屏（模拟器游戏显示器）
                            return str(max_active)
                        else:
                            # 只有显示器 0 有任务，使用默认
                            return ""

            # 回退：不需要显示器检测
            return ""

        except Exception as e:
            logger.warning(f"Failed to detect display ID: {e}")
            return ""

    # 存储设备 ID 和进程供 stop_record 使用
    _recording_device = None
    _recording_process = None
    _recording_output_path = None
    _recording_log_path = None
    _recording_log_file = None

    @classmethod
    def _close_recording_log(cls):
        if cls._recording_log_file:
            try:
                cls._recording_log_file.close()
            except Exception:
                pass
            finally:
                cls._recording_log_file = None

    @classmethod
    def _open_recording_log(cls, report_dir, device, options=None):
        log_path = os.path.join(report_dir, "scrcpy_record.log")
        log_file = open(log_path, "ab", buffering=0)
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"\n[{started_at}] start scrcpy recording device={device}\n"
        log_file.write(header.encode("utf-8", errors="ignore"))
        if options:
            options_line = "options={}\n".format(
                json.dumps(options, sort_keys=True)
            )
            log_file.write(options_line.encode("utf-8", errors="ignore"))
        cls._recording_log_path = log_path
        cls._recording_log_file = log_file
        return log_file

    @classmethod
    def _read_recording_log_tail(cls, max_bytes=4096):
        if not cls._recording_log_path or not os.path.exists(cls._recording_log_path):
            return ""
        try:
            with open(cls._recording_log_path, "rb") as log_file:
                log_file.seek(0, os.SEEK_END)
                size = log_file.tell()
                log_file.seek(max(0, size - max_bytes), os.SEEK_SET)
                return log_file.read().decode("utf-8", errors="ignore").strip()
        except Exception as e:
            logger.debug(f"Failed to read scrcpy log tail: {e}")
            return ""

    @classmethod
    def start_record(cls, device, options=None):
        """使用 scrcpy 开始屏幕录制（无时间限制，支持模拟器）

        使用 scrcpy CLI：
        - --no-playback --no-window: 无头后台录制
        - --record: 直接录制到本地 MP4 文件
        - --display-id: 支持模拟器副屏
        - CTRL_BREAK_EVENT 用于优雅关闭（Windows）

        优于 adb screenrecord 的好处：
        - 无时间限制（screenrecord 有 180 秒限制）
        - 直接录制到本地文件（无需 adb pull）
        - 支持模拟器副屏

        Returns:
            int: 0 成功，1 失败
        """
        from perfcat.report.manager import ReportStore

        f = ReportStore()

        if cls._recording_process and cls._recording_process.poll() is None:
            logger.warning(
                "Existing scrcpy recording is still running, stopping it first"
            )
            cls.stop_record()

        cls._recording_device = device
        cls._recording_process = None
        cls._recording_output_path = os.path.join(f.report_dir, "record.mp4")
        cls._recording_log_path = None
        cls._close_recording_log()

        # 检测模拟器的显示 ID（游戏通常运行在副屏）
        display_id = cls.get_display_id(device)
        record_options = cls.record_options(options)

        cmd_args = [
            cls.scrcpy_path(),
            "-s",
            device,
            "--no-playback",  # 不解码视频（降低 CPU 使用）
            "--no-control",  # 只读录屏，避免不必要的控制通道
            "--no-window",  # 无窗口，真正的后台录制
            "--force-adb-forward",  # 无线 adb 下比默认 reverse tunnel 更稳定
            f"--screen-off-timeout={record_options['screen_off_timeout']}",
            f"--record={cls._recording_output_path}",
            "--record-format=mp4",  # MP4 格式用于 HTML5 浏览器兼容
            "--video-codec=h264",  # H.264 广泛兼容
            f"--video-bit-rate={record_options['video_bit_rate']}",
            f"--max-size={record_options['max_size']}",
            f"--max-fps={record_options['max_fps']}",
        ]

        if record_options["no_audio"]:
            cmd_args.append("--no-audio")

        # 为模拟器添加 display-id
        if display_id:
            cmd_args.append(f"--display-id={display_id}")

        try:
            log_file = cls._open_recording_log(
                f.report_dir, device, record_options
            )
            popen_kwargs = {
                "stdout": log_file,
                "stderr": subprocess.STDOUT,
                "cwd": cls._scrcpy_cwd(),
                "env": cls._scrcpy_env(),
            }
            # 启动 scrcpy 进程
            # Windows: CREATE_NEW_PROCESS_GROUP 是 CTRL_BREAK_EVENT 工作的必要条件
            if "windows" in platform.system().lower():
                cls._recording_process = subprocess.Popen(
                    cmd_args,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                    **popen_kwargs,
                )
            else:
                cls._recording_process = subprocess.Popen(
                    cmd_args,
                    start_new_session=True,
                    **popen_kwargs,
                )

            # 短暂等待检查 scrcpy 是否启动成功
            time.sleep(1)
            if cls._recording_process.poll() is not None:
                logger.error(
                    "scrcpy failed to start: %s", cls._read_recording_log_tail()
                )
                cls._recording_process = None
                cls._close_recording_log()
                return 1

            return 0

        except Exception as e:
            logger.error(f"Failed to start scrcpy recording: {e}")
            cls._recording_process = None
            cls._close_recording_log()
            return 1

    @classmethod
    def stop_record(cls):
        """优雅停止屏幕录制

        使用 CTRL_BREAK_EVENT（Windows）或 SIGINT（Unix）发送信号给 scrcpy
        实现优雅关闭。确保 MP4 文件正确写入 moov atom（播放必需）。

        注意：CTRL_BREAK_EVENT 在 Windows 上配合 CREATE_NEW_PROCESS_GROUP 可靠工作，
        而 CTRL_C_EVENT 经常静默失败。
        """
        if not cls._recording_process:
            logger.debug("No recording process to stop")
            return False

        output_path = cls._recording_output_path
        valid_recording = False

        try:
            # 发送优雅关闭信号
            if "windows" in platform.system().lower():
                # Windows: CTRL_BREAK_EVENT 配合 CREATE_NEW_PROCESS_GROUP 可靠工作
                cls._recording_process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                # Unix: SIGINT 实现优雅关闭
                cls._recording_process.send_signal(signal.SIGINT)

            try:
                cls._recording_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning("scrcpy timeout, force killing")
                cls._recording_process.kill()
                cls._recording_process.wait(timeout=5)

            # 等待文件完全写入
            time.sleep(1)

            # 验证输出文件
            if output_path and os.path.exists(output_path):
                file_size = os.path.getsize(output_path)
                valid_recording = file_size > 1000
                if not valid_recording:
                    logger.warning(
                        f"Recording file too small ({file_size} bytes), may be corrupted"
                    )
            else:
                logger.warning(f"Recording file not found: {output_path}")

        except Exception as e:
            logger.exception(f"Error stopping recording: {e}")
            return False
        finally:
            cls._close_recording_log()
            cls._recording_device = None
            cls._recording_process = None
            cls._recording_output_path = None
            cls._recording_log_path = None
        return valid_recording

    @classmethod
    def cast_screen(cls, device):
        """投屏到 PC

        Args:
            device: 设备 ID
        """
        # 检测 Android 模拟器的显示 ID（游戏通常运行在副屏）
        display_id = cls.get_display_id(device)
        display_option = f" --display-id={display_id}" if display_id else ""

        win_cmd = (
            "start /i {scrcpy_path} -s {deviceId}{display_option} --stay-awake".format(
                scrcpy_path=cls.scrcpy_path(),
                deviceId=device,
                display_option=display_option,
            )
        )
        mac_cmd = (
            "nohup {scrcpy_path} -s {deviceId}{display_option} --stay-awake &".format(
                scrcpy_path=cls.scrcpy_path(),
                deviceId=device,
                display_option=display_option,
            )
        )
        if "windows" in platform.system().lower():
            result = os.system(win_cmd)
        else:
            result = os.system(mac_cmd)
        if result != 0:
            logger.error(
                "perfcat's scrcpy is incompatible with your PC, please install scrcpy manually"
            )
        return result

    @classmethod
    def play_video(cls, video):
        """播放录制的视频

        Args:
            video: 视频文件路径
        """
        cap = cv2.VideoCapture(video)
        while cap.isOpened():
            ret, frame = cap.read()
            if ret:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                cv2.namedWindow("frame", 0)
                cv2.resizeWindow("frame", 430, 900)
                cv2.imshow("frame", gray)
                if cv2.waitKey(25) & 0xFF == ord("q") or not cv2.getWindowProperty(
                    "frame", cv2.WND_PROP_VISIBLE
                ):
                    break
            else:
                break
        cap.release()
        cv2.destroyAllWindows()
