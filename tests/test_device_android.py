"""Focused Android device parsing tests."""
from unittest.mock import MagicMock, patch

from perfcattle.core.platform import Platform
from perfcattle.device.android import AndroidDevice


def test_get_device_ids_filters_non_ready_devices():
    device = AndroidDevice()
    mock_result = MagicMock()
    mock_result.stdout = (
        "List of devices attached\n"
        "abc123\tunauthorized\n"
        "def456\tdevice\n"
        "ghi789\toffline\n"
    )

    with patch("perfcattle.device.android.subprocess.run", return_value=mock_result):
        assert device.getDeviceIds() == ["def456"]


def test_get_id_by_device_reconnects_wireless_device_once():
    device = AndroidDevice()
    serial = "192.168.1.139:37557"

    with patch.object(
        device,
        "getDeviceStates",
        side_effect=[
            {},
            {},
            {},
            {},
            {serial: "device"},
        ],
    ), patch("perfcattle.device.android.time.sleep"), patch.object(
        device, "_connect_wireless_device"
    ) as connect:
        assert (
            device.getIdbyDevice(f"{serial}(23127PN0CC)", Platform.Android)
            == serial
        )

    connect.assert_called_once_with(serial)


def test_get_pid_parses_main_and_child_processes():
    with patch("perfcattle.device.android.adb") as mock_adb:
        mock_adb.adb_path = "adb"
        device = AndroidDevice()

        def shell_side_effect(cmd, deviceId, timeout=30):
            if cmd.startswith("pidof"):
                return "1234"
            if cmd == "ps -A -o PID,NAME":
                return (
                    "PID NAME\n"
                    "1234 com.example.cocos\n"
                    "2345 com.example.cocos:remote\n"
                )
            return ""

        mock_adb.shell.side_effect = shell_side_effect
        pids = device.getPid("abc123", "com.example.cocos")

    assert pids == [
        "1234:com.example.cocos",
        "2345:com.example.cocos:remote",
    ]


def test_get_pid_fast_only_uses_pidof():
    with patch("perfcattle.device.android.adb") as mock_adb:
        mock_adb.adb_path = "adb"
        device = AndroidDevice()
        mock_adb.shell.return_value = "1234"

        pids = device.getPid("abc123", "com.example.cocos", fast=True)

    assert pids == ["1234:com.example.cocos"]
    # Fast mode must not run the slow ps / /proc fallbacks.
    assert mock_adb.shell.call_count == 1


def test_get_pid_falls_back_to_proc_cmdline():
    with patch("perfcattle.device.android.adb") as mock_adb:
        mock_adb.adb_path = "adb"
        device = AndroidDevice()

        def shell_side_effect(cmd, deviceId, timeout=30):
            if cmd.startswith("for p in /proc"):
                return "3456:com.example.cocos\n"
            return ""

        mock_adb.shell.side_effect = shell_side_effect
        assert device.getPid("abc123", "com.example.cocos") == [
            "3456:com.example.cocos"
        ]


def test_launch_app_uses_monkey_for_selected_package():
    with patch("perfcattle.device.android.adb") as mock_adb:
        device = AndroidDevice()
        mock_adb.shell.return_value = "Events injected: 1"

        assert device.launchApp("abc123", "com.example.app") is True

    mock_adb.shell.assert_called_once()
    assert "monkey -p com.example.app" in mock_adb.shell.call_args.kwargs["cmd"]


def test_launch_app_falls_back_to_resolved_launcher_activity():
    with patch("perfcattle.device.android.adb") as mock_adb:
        device = AndroidDevice()

        def shell_side_effect(cmd, deviceId, timeout=30):
            if cmd.startswith("monkey"):
                return "** No activities found to run, monkey aborted."
            if cmd.startswith("cmd package resolve-activity"):
                return (
                    "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\n"
                    "com.example.app/.MainActivity"
                )
            if cmd.startswith("am start -n"):
                return "Starting: Intent { cmp=com.example.app/.MainActivity }"
            return ""

        mock_adb.shell.side_effect = shell_side_effect

        assert device.launchApp("abc123", "com.example.app") is True

    commands = [call.kwargs["cmd"] for call in mock_adb.shell.call_args_list]
    assert "am start -n com.example.app/.MainActivity" in commands


def test_launch_app_rejects_unsafe_package_name():
    device = AndroidDevice()

    try:
        device.launchApp("abc123", "com.example; rm -rf /")
    except Exception as exc:
        assert "Invalid package name" in str(exc)
    else:
        raise AssertionError("unsafe package name should be rejected")
