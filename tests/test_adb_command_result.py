"""Structured ADB command result tests."""
from unittest.mock import MagicMock, patch

from perfcattle.core.adb_client import ADB


def test_shell_result_distinguishes_timeout():
    client = ADB.__new__(ADB)
    client.adb_path = "adb"
    with patch(
        "perfcattle.core.adb_client.subprocess.run",
        side_effect=__import__("subprocess").TimeoutExpired("adb", 1),
    ):
        result = client.shell_result("cat /proc/stat", "device", timeout=1)
    assert result.ok is False
    assert result.timed_out is True
    assert result.error_kind == "timeout"


def test_shell_result_captures_success_and_nonzero_output():
    client = ADB.__new__(ADB)
    client.adb_path = "adb"
    success = MagicMock(stdout=b"ok\n", stderr=b"", returncode=0)
    with patch("perfcattle.core.adb_client.subprocess.run", return_value=success):
        result = client.shell_result("echo ok", "device")
    assert result.ok is True
    assert result.stdout == "ok"
    assert result.stderr == ""
    assert result.error_kind is None
    failed = MagicMock(stdout=b"", stderr=b"bad\n", returncode=7)
    with patch("perfcattle.core.adb_client.subprocess.run", return_value=failed):
        result = client.shell_result("echo ok", "device")
    assert result.ok is False
    assert result.returncode == 7
    assert result.stderr == "bad"
    assert result.error_kind == "returncode"


def test_shell_result_maps_os_error():
    client = ADB.__new__(ADB)
    client.adb_path = "adb"
    with patch(
        "perfcattle.core.adb_client.subprocess.run",
        side_effect=OSError("missing adb"),
    ):
        result = client.shell_result("echo ok", "device")
    assert result.ok is False
    assert result.error_kind == "os_error"
    assert "missing adb" in result.stderr
