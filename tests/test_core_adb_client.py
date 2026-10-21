"""ADB client tests."""
from unittest.mock import MagicMock, patch

from perfcat.core.adb_client import ADB


class TestADBClient:

    def test_shell_uses_argument_list(self):
        client = ADB.__new__(ADB)
        client.adb_path = "adb"
        mock_result = MagicMock()
        mock_result.stdout = b"ok\n"
        mock_result.stderr = b""
        mock_result.returncode = 0

        with patch(
            "perfcat.core.adb_client.subprocess.run", return_value=mock_result
        ) as run:
            result = client.shell("dumpsys SurfaceFlinger --list", "device-1")

        assert result == "ok"
        run.assert_called_once_with(
            ["adb", "-s", "device-1", "shell", "dumpsys SurfaceFlinger --list"],
            capture_output=True,
            timeout=30,
        )

    def test_shell_no_device_accepts_list_args(self):
        client = ADB.__new__(ADB)
        client.adb_path = "adb"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = b""

        with patch(
            "perfcat.core.adb_client.subprocess.run", return_value=mock_result
        ) as run:
            result = client.shell_noDevice(["install", "-r", "/tmp/My App.apk"])

        assert result == 0
        run.assert_called_once_with(
            ["adb", "install", "-r", "/tmp/My App.apk"],
            capture_output=True,
            timeout=30,
        )
