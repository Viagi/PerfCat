# -*- encoding: utf-8 -*-
"""Stability checks for long-running scrcpy recording."""
import subprocess

from perfcat.tools import recording
from perfcat.tools.recording import Scrcpy


class FakeProcess:
    def __init__(self):
        self.signals = []
        self.killed = False

    def poll(self):
        return None

    def send_signal(self, signal):
        self.signals.append(signal)

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


def test_scrcpy_recording_redirects_output_to_file(monkeypatch, tmp_path):
    class FakeFile:
        def __init__(self):
            self.report_dir = str(tmp_path)

    process = FakeProcess()
    popen_calls = []

    def fake_popen(args, **kwargs):
        popen_calls.append((args, kwargs))
        return process

    monkeypatch.setattr("perfcat.report.manager.ReportStore", FakeFile)
    monkeypatch.setattr(Scrcpy, "scrcpy_path", classmethod(lambda cls: "scrcpy"))
    monkeypatch.setattr(
        Scrcpy, "get_display_id", classmethod(lambda cls, device: "")
    )
    monkeypatch.setattr(Scrcpy, "adb_path", classmethod(lambda cls: "/tmp/adb"))
    monkeypatch.setattr(recording.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(recording.subprocess, "Popen", fake_popen)

    try:
        assert Scrcpy.start_record("192.168.1.2:5555") == 0
        assert popen_calls
        args, kwargs = popen_calls[0]
        for option in (
            "--no-audio",
            "--no-control",
            "--max-size=720",
            "--max-fps=60",
            "--video-bit-rate=4M",
            "--force-adb-forward",
            "--screen-off-timeout=3600",
        ):
            assert option in args
        assert kwargs["stdout"] not in (subprocess.PIPE, None)
        assert hasattr(kwargs["stdout"], "write")
        assert kwargs["stderr"] == subprocess.STDOUT
        assert kwargs["env"]["ADB"] == "/tmp/adb"
        assert (tmp_path / "scrcpy_record.log").exists()
    finally:
        Scrcpy.stop_record()


def test_scrcpy_record_options_are_configurable():
    assert Scrcpy.record_options(None) == {
        "max_size": "720",
        "max_fps": "60",
        "video_bit_rate": "4M",
        "no_audio": True,
        "screen_off_timeout": "3600",
    }

    assert Scrcpy.record_options(
        {
            "record_max_size": "1080",
            "record_max_fps": "30",
            "record_video_bit_rate": "8m",
            "record_no_audio": "0",
            "record_screen_off_timeout": "7200",
        }
    ) == {
        "max_size": "1080",
        "max_fps": "30",
        "video_bit_rate": "8M",
        "no_audio": False,
        "screen_off_timeout": "7200",
    }
