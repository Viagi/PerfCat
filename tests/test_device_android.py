"""Focused Android device parsing tests."""
from unittest.mock import MagicMock, patch

from perfcat.core.platform import Platform
from perfcat.device.android import AndroidDevice


def test_get_device_ids_filters_non_ready_devices():
    device = AndroidDevice()
    mock_result = MagicMock()
    mock_result.stdout = (
        "List of devices attached\n"
        "abc123\tunauthorized\n"
        "def456\tdevice\n"
        "ghi789\toffline\n"
    )

    with patch("perfcat.device.android.subprocess.run", return_value=mock_result):
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
    ), patch("perfcat.device.android.time.sleep"), patch.object(
        device, "_connect_wireless_device"
    ) as connect:
        assert (
            device.getIdbyDevice(f"{serial}(23127PN0CC)", Platform.Android)
            == serial
        )

    connect.assert_called_once_with(serial)


def test_get_pid_parses_main_and_child_processes():
    with patch("perfcat.device.android.adb") as mock_adb:
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


def test_get_pid_falls_back_to_proc_cmdline():
    with patch("perfcat.device.android.adb") as mock_adb:
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
