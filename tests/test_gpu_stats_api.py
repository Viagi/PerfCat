"""Live GPU stats endpoint contract: the chart must receive numbers."""
from unittest.mock import patch

from flask import Flask

from perfcattle.core.metrics import MetricSample, MetricStatus
from perfcattle.view.apm_api import apm_api


class FakeGpuStats:
    def __init__(self, data):
        self._data = data

    def sample(self):
        return dict(self._data)


def make_client():
    app = Flask(__name__)
    app.register_blueprint(apm_api)
    return app.test_client()


def structured_samples():
    return {
        "gpu_clock": MetricSample.ok("gpu_clock", 587.0, unit="MHz").to_dict(),
        "gpu_mem_app": MetricSample.ok("gpu_mem_app", 128.0, unit="MB").to_dict(),
        "gpu_bw_read": MetricSample.ok("gpu_bw_read", 1205.4, unit="MB/s").to_dict(),
        "gpu_bw_write": MetricSample(
            "gpu_bw_write", 830.1, "MB/s", MetricStatus.CACHED
        ).to_dict(),
        "gpu_temp": MetricSample(
            "gpu_temp", None, "celsius", MetricStatus.PERMISSION_DENIED
        ).to_dict(),
    }


def test_gpu_stats_endpoint_returns_plottable_numbers():
    with patch(
        "perfcattle.view.apm_api.d.getIdbyDevice", return_value="abc123"
    ), patch(
        "perfcattle.view.apm_api.create_gpu_stats",
        return_value=FakeGpuStats(structured_samples()),
    ), patch(
        "perfcattle.view.apm_api.f.add_json_log"
    ) as add_log:
        response = make_client().get(
            "/apm/gpu/stats",
            query_string={
                "platform": "Android",
                "device": "demo",
                "pkgname": "com.example.app",
            },
        )

    payload = response.json
    assert payload["status"] == 1
    # Charts plot these values directly, so they must be bare numbers.
    assert payload["data"] == {
        "gpu_clock": 587.0,
        "gpu_mem_app": 128.0,
        "gpu_bw_read": 1205.4,
        "gpu_bw_write": 830.1,
    }
    # An unreadable counter is a gap in the curve, never a fabricated zero.
    assert "gpu_temp" not in payload["data"]


def test_gpu_stats_endpoint_keeps_structured_samples_in_the_log():
    with patch(
        "perfcattle.view.apm_api.d.getIdbyDevice", return_value="abc123"
    ), patch(
        "perfcattle.view.apm_api.create_gpu_stats",
        return_value=FakeGpuStats(structured_samples()),
    ), patch(
        "perfcattle.view.apm_api.f.add_json_log"
    ) as add_log:
        make_client().get(
            "/apm/gpu/stats",
            query_string={
                "platform": "Android",
                "device": "demo",
                "pkgname": "com.example.app",
            },
        )

    logged = add_log.call_args[0][2]
    assert logged["gpu_bw_read"]["value"] == 1205.4
    assert logged["gpu_bw_read"]["status"] == "ok"
    assert logged["gpu_temp"]["status"] == "permission_denied"
