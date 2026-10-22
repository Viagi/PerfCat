# -*- coding: utf-8 -*-
"""GPU 显存读写带宽采集（Adreno 硬件计数器）。

对应 Snapdragon Profiler 的 GPU Memory Stats：
    a7xx / a8xx (GBIF): read = group 13 selectors 34+35, write = 46+47
    a6xx       (VBIF):  read = group 13 selector 63,    write = 84

一个 data beat 为 32 字节，输出单位 MB/s（按 1e6 计算）。
需要 /dev/kgsl-3d0 的 perfcounter 权限；被 SELinux 限制的机型上
prepare() 失败，指标直接缺席，不影响其他采集。
"""
import os
import subprocess
import time

from logzero import logger

from perfcat.core.adb_client import adb
from perfcat.core.platform import Platform

_BIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
    "assets", "bin", "android", "arm64",
)
_LOCAL_TOOL = os.path.join(_BIN_DIR, "gpu_counters")
_REMOTE_DIR = "/data/local/tmp/perfcat"
_REMOTE_TOOL = f"{_REMOTE_DIR}/gpu_counters"
_PERFCOUNTER = "/sys/class/kgsl/kgsl-3d0/perfcounter"

_READY = {}  # deviceId -> True once the helper has been pushed successfully


def _decode(value):
    if isinstance(value, str):
        return value.strip()
    try:
        return value.decode("utf-8").strip()
    except UnicodeDecodeError:
        return value.decode("gbk", errors="replace").strip()


def _run(device_id, args, timeout=None):
    try:
        proc = subprocess.run(
            [adb.adb_path, "-s", device_id, *args],
            capture_output=True,
            timeout=timeout,
        )
        return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


class GpuBandwidth(object):
    """采样 GPU 显存读写带宽（MB/s），设备不支持时返回 {}。"""

    INTERVAL_MS = 500

    def __init__(self, deviceId):
        self.deviceId = deviceId
        self._last_data = None
        self._last_ts = 0.0

    def _shell(self, cmd, timeout=10):
        return _run(self.deviceId, ["shell", cmd], timeout=timeout)

    def _push_tool(self):
        self._shell(f"mkdir -p {_REMOTE_DIR}")
        code, out, err = _run(
            self.deviceId, ["push", _LOCAL_TOOL, _REMOTE_TOOL], timeout=20
        )
        if code != 0:
            logger.debug(f"gpu bandwidth: push failed: {err or out}")
            return False
        self._shell(f"chmod 755 {_REMOTE_TOOL}")
        return True

    def prepare(self):
        if _READY.get(self.deviceId):
            return True
        if not os.path.exists(_LOCAL_TOOL):
            logger.debug("gpu bandwidth: helper binary missing")
            return False
        code, out, err = self._shell(f"ls {_PERFCOUNTER}")
        if code != 0 or "No such file" in (out + err):
            return False
        code, out, err = self._shell(f"echo 1 > {_PERFCOUNTER}")
        if code != 0 or "Permission" in (out + err) or "denied" in (out + err):
            return False
        if not self._push_tool():
            return False
        _READY[self.deviceId] = True
        return True

    def sample(self, platform=Platform.Android):
        if platform != Platform.Android:
            return {}
        if not self.prepare():
            return {}

        now = time.time()
        if self._last_data is not None and now - self._last_ts < 1.5:
            return dict(self._last_data)

        code, out, err = self._shell(
            f"{_REMOTE_TOOL} --bandwidth --interval-ms {self.INTERVAL_MS} --count 1"
        )
        lines = [line for line in (out or "").splitlines() if "," in line]
        for line in reversed(lines):
            parts = line.split(",")
            try:
                read = round(float(parts[1]), 2)
                write = round(float(parts[2]), 2)
            except (IndexError, ValueError):
                continue
            self._last_data = {"gpu_bw_read": read, "gpu_bw_write": write}
            self._last_ts = time.time()
            return dict(self._last_data)

        _READY.pop(self.deviceId, None)
        return {}
