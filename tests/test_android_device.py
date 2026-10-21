# -*- coding: utf-8 -*-
"""Android device parsing tests."""
import subprocess
from unittest.mock import patch

from perfcat.device.android import AndroidDevice


def test_get_current_activity_parses_wechat_mini_game_focus():
    device = AndroidDevice()
    dumpsys = "\n".join(
        [
            "mCurrentFocus=Window{5d1a369 u0 "
            "com.tencent.mm/com.tencent.mm.plugin.appbrand.ui.AppBrandUI1}",
            "mFocusedApp=ActivityRecord{a85e2f4 u0 "
            "com.tencent.mm/.plugin.appbrand.ui.AppBrandUI1 t15304}",
        ]
    )

    with patch("perfcat.device.android.adb") as mock_adb:
        mock_adb.shell.return_value = dumpsys

        assert device.getCurrentActivity("device-1") == (
            "com.tencent.mm/com.tencent.mm.plugin.appbrand.ui.AppBrandUI1"
        )

    assert mock_adb.shell.call_args.kwargs["cmd"] == "dumpsys window"


def test_get_startup_time_returns_timing_output():
    device = AndroidDevice()
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=(
            b"Starting: Intent { cmp=com.example/.MainActivity }\n"
            b"ThisTime: 120\nTotalTime: 180\nWaitTime: 200\nComplete\n"
        ),
        stderr=b"",
    )

    with patch("perfcat.device.android.subprocess.run", return_value=result) as run:
        output = device.getStartupTimeByAndroid(
            "com.example/.MainActivity", "device-1"
        )

    assert "TotalTime: 180" in output
    assert run.call_args.args[0][-2:] == ["-W", "com.example/.MainActivity"]


def test_get_startup_time_reports_not_exported_activity():
    device = AndroidDevice()
    result = subprocess.CompletedProcess(
        args=[],
        returncode=255,
        stdout=(
            b"Starting: Intent { cmp=com.tencent.mm/.plugin.appbrand.ui.AppBrandUI1 }\n"
        ),
        stderr=(
            b"java.lang.SecurityException: Permission Denial: "
            b"not exported from uid 10311\n"
        ),
    )

    with patch("perfcat.device.android.subprocess.run", return_value=result):
        try:
            device.getStartupTimeByAndroid(
                "com.tencent.mm/.plugin.appbrand.ui.AppBrandUI1", "device-1"
            )
        except Exception as exc:
            message = str(exc)
        else:
            raise AssertionError("Expected non-exported activity error")

    assert "not exported" in message
    assert "Starting: Intent" in message
