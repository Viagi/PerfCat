# -*- encoding: utf-8 -*-
"""ADB 封装。"""
import os
import platform
import shutil
import shlex
import stat
import subprocess
from logzero import logger

ASSETS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "assets"
)
DEFAULT_ADB_PATH = {
    "Windows": os.path.join(ASSETS_PATH, "adb", "windows", "adb.exe"),
    "Darwin": os.path.join(ASSETS_PATH, "adb", "mac", "adb"),
    "Linux": os.path.join(ASSETS_PATH, "adb", "linux", "adb"),
    "Linux-x86_64": os.path.join(ASSETS_PATH, "adb", "linux", "adb"),
    "Linux-armv7l": os.path.join(ASSETS_PATH, "adb", "linux_arm", "adb"),
}


def make_file_executable(file_path):
    """如果路径没有可执行权限，执行 chmod +x"""
    if os.path.isfile(file_path):
        mode = os.lstat(file_path)[stat.ST_MODE]
        executable = True if mode & stat.S_IXUSR else False
        if not executable:
            os.chmod(file_path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return True
    return False


def is_valid_adb_file(file_path):
    return bool(
        file_path and os.path.isfile(file_path) and os.path.getsize(file_path) > 0
    )


def builtin_adb_path():
    """返回内置 adb 可执行文件路径"""
    system = platform.system()
    machine = platform.machine()
    adb_on_path = shutil.which("adb")
    if adb_on_path:
        return adb_on_path

    adb_path = DEFAULT_ADB_PATH.get('{}-{}'.format(system, machine))
    if not adb_path:
        adb_path = DEFAULT_ADB_PATH.get(system)
    if not is_valid_adb_file(adb_path):
        return "adb"

    if "ANDROID_HOME" in os.environ:
        del os.environ["ANDROID_HOME"]
    if system != "Windows":
        make_file_executable(adb_path)
    return adb_path


class ADB:
    def __init__(self):
        self.adb_path = builtin_adb_path()

    def _decode_output(self, value):
        if isinstance(value, str):
            return value.strip()
        try:
            return value.decode("utf-8").strip()
        except UnicodeDecodeError:
            return value.decode("gbk", errors="replace").strip()

    def _command_args(self, cmd):
        if isinstance(cmd, (list, tuple)):
            return [str(part) for part in cmd]
        return shlex.split(cmd)

    def shell(self, cmd, deviceId, timeout=30):
        """执行 adb shell 命令，改进编码处理"""
        try:
            result = subprocess.run(
                [self.adb_path, "-s", deviceId, "shell", cmd],
                capture_output=True,
                timeout=timeout,
            )
            output = self._decode_output(result.stdout)
            error = self._decode_output(result.stderr)
            if result.returncode != 0 and error:
                logger.debug("adb shell failed: %s, stderr: %s", cmd, error)
            return output
        except subprocess.TimeoutExpired:
            logger.warning("adb shell timed out after %ss: %s", timeout, cmd)
            return ""
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("adb shell failed: %s, error: %s", cmd, exc)
            return ""
        except Exception:
            logger.exception("adb shell failed: %s", cmd)
            return ""

    def tcp_shell(self, deviceId, cmd, timeout=30):
        try:
            result = subprocess.run(
                [self.adb_path, "-s", deviceId, *self._command_args(cmd)],
                capture_output=True,
                timeout=timeout,
            )
            error = self._decode_output(result.stderr)
            if result.returncode != 0 and error:
                logger.error("adb command failed: %s, stderr: %s", cmd, error)
            return result.returncode
        except ValueError as exc:
            logger.error("invalid adb command: %s, error: %s", cmd, exc)
            return 1
        except subprocess.TimeoutExpired:
            logger.warning("adb command timed out after %ss: %s", timeout, cmd)
            return 1
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("adb command failed: %s, error: %s", cmd, exc)
            return 1

    def shell_noDevice(self, cmd, timeout=30):
        try:
            result = subprocess.run(
                [self.adb_path, *self._command_args(cmd)],
                capture_output=True,
                timeout=timeout,
            )
            error = self._decode_output(result.stderr)
            if result.returncode != 0 and error:
                logger.error("adb command failed: %s, stderr: %s", cmd, error)
            return result.returncode
        except ValueError as exc:
            logger.error("invalid adb command: %s, error: %s", cmd, exc)
            return 1
        except subprocess.TimeoutExpired:
            logger.warning("adb command timed out after %ss: %s", timeout, cmd)
            return 1
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("adb command failed: %s, error: %s", cmd, exc)
            return 1


adb = ADB()
