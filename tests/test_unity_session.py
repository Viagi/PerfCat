"""UnityCapture session lifecycle: duplicate start/stop must not kill logging."""
import os
import tempfile
import time

from perfcattle.unity.manager import UnityCapture


class FakeBackend:
    mode = "protocol"

    def __init__(self):
        self.files = []

    def status(self):
        return {"metrics_supported": True, "metrics_reason": None}

    def stats(self):
        return {
            "available": True,
            "values": {"cpu_frame_ms": 16.0, "draw_calls": 300},
            "frame": 1,
        }

    def stop(self):
        return {"status": 1, "files": []}


def test_duplicate_start_keeps_stats_logger_alive():
    old = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            capture = UnityCapture()
            capture.device = "device"
            capture.pkg = "pkg"
            capture.pid = "1"
            capture.staging_dir = os.path.join(tmp, "report", "unity_test")
            os.makedirs(capture.staging_dir, exist_ok=True)
            capture._adopt(FakeBackend(), {"unity_version": "2022.3"})
            capture._start_stats_logger()
            time.sleep(0.2)
            thread = capture._stats_thread
            assert thread is not None and thread.is_alive()

            # Duplicate start from a page reload must return the existing
            # session without stopping the logger.
            capture.start("device", "pkg", pid="1")
            assert capture._stats_thread is thread
            assert thread.is_alive()
        finally:
            capture._stop_stats_logger()
            os.chdir(old)


def test_duplicate_stop_does_not_rewrite_meta():
    old = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            capture = UnityCapture()
            capture.device = "device"
            capture.pkg = "pkg"
            capture.pid = "1"
            capture.staging_dir = os.path.join(tmp, "report", "unity_test")
            os.makedirs(capture.staging_dir, exist_ok=True)
            capture._adopt(FakeBackend(), {"unity_version": "2022.3"})
            first = capture.stop()
            assert first["status"] == 1
            meta_path = os.path.join(capture.staging_dir, "meta.json")
            assert os.path.exists(meta_path)
            meta = open(meta_path, encoding="utf-8").read()
            assert '"mode": "protocol"' in meta

            second = capture.stop()
            assert second.get("msg") == "no unity session"
            assert open(meta_path, encoding="utf-8").read() == meta
        finally:
            os.chdir(old)
