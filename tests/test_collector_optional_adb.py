"""Optional ADB reads must not break fallback chains.

Collector probes touch nodes that are legitimately absent (uid_stat on modern
Android, most /sys GPU nodes without root, grep-style dumpsys chains). Those
reads must yield an empty string so the existing fallback still runs, while
required reads keep raising a structured failure.
"""
from unittest.mock import MagicMock

import pytest

from perfcattle.collector import gpu_stats as gpu_stats_module
from perfcattle.collector import network as network_module
from perfcattle.collector.base import adb_read_optional
from perfcattle.collector.network import Network


class _FailingClient:
    def shell(self, **kwargs):
        raise RuntimeError("permission denied")


class _HealthyClient:
    def shell(self, **kwargs):
        return "42"


def test_optional_read_returns_empty_on_failure():
    assert adb_read_optional(_FailingClient(), cmd="cat /sys/x") == ""


def test_optional_read_passes_through_success():
    assert adb_read_optional(_HealthyClient(), cmd="cat /sys/x") == "42"


def test_network_falls_back_to_netstats_when_uid_stat_is_missing(monkeypatch):
    network = Network("app", "device", pid="123")
    network._uid = 10001
    monkeypatch.setattr(
        network_module,
        "adb",
        MagicMock(shell=MagicMock(side_effect=RuntimeError("no such file"))),
    )
    calls = []

    def fake_netstats(wifi=True):
        calls.append(wifi)
        return (1.5, 2.5)

    monkeypatch.setattr(network, "_get_netstats_totals", fake_netstats)

    assert network._get_android_net_totals(wifi=True) == (1.5, 2.5)
    assert calls == [True]
    # The absence is recorded as "not supported", not as a zero sample.
    assert network._uid_stat_supported is False


def test_gpu_stats_skips_unreadable_nodes(monkeypatch):
    monkeypatch.setattr(
        gpu_stats_module,
        "adb",
        MagicMock(shell=MagicMock(side_effect=RuntimeError("permission denied"))),
    )
    stats = gpu_stats_module.GpuStats("device")

    # Probing must complete and simply report no sources.
    assert stats.sample() == {}
