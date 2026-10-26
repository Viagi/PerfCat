import math
from unittest.mock import patch

from perfcattle.core.metrics import (
    MetricQuality,
    MetricSample,
    MetricStatus,
    finite_number,
    parse_metric_line,
)
from perfcattle.report.log_io import LogIO


def test_zero_is_a_valid_sample():
    sample = MetricSample.ok(
        metric="gpu_util", value=0, unit="percent", source="test",
        sample_epoch_ms=1, sample_monotonic_ns=2,
    )
    assert sample.status is MetricStatus.OK
    assert sample.value == 0


def test_failed_sample_cannot_carry_a_value():
    sample = MetricSample(
        metric="cpu", value=0, unit="percent", status=MetricStatus.TIMEOUT,
        source="adb", sample_epoch_ms=1, sample_monotonic_ns=2,
    )
    assert sample.value is None


def test_finite_number_rejects_non_finite_and_bool():
    assert finite_number(1.0)
    assert not finite_number(True)
    assert not finite_number(math.nan)
    assert not finite_number(math.inf)


def test_bad_json_line_returns_no_sample():
    assert parse_metric_line('{"metric":"cpu","value":"broken"}') is None


def test_sample_round_trip_preserves_quality_fields():
    sample = MetricSample.ok(
        metric="cpu", value=12.5, unit="percent", source="adb",
        scope="package", aggregation="last", sample_epoch_ms=10,
        sample_monotonic_ns=20,
    )
    restored = MetricSample.from_dict(sample.to_dict())
    assert restored == sample
    assert restored.to_dict()["status"] == "ok"
    assert restored.to_dict()["cached"] is False


def test_quality_counts_samples():
    quality = MetricQuality(total=3, valid=2, invalid=1, cached=1, missing=0)
    assert quality.to_dict()["total"] == 3


def test_read_log_skips_bad_and_nonfinite_lines_with_quality(tmp_report_dir):
    scene = tmp_report_dir / "scene"
    scene.mkdir()
    (scene / "cpu.log").write_text(
        "10:00:00=0\n10:00:01=NaN\nnot-a-line\n10:00:02=2.5\n"
    )
    with patch("os.getcwd", return_value=str(tmp_report_dir.parent)):
        log_io = LogIO()
        points, values, quality = log_io.readLogWithQuality("scene", "cpu.log")
    assert values == [0, 2.5]
    assert len(points) == 2
    assert quality.total == 4
    assert quality.valid == 2
    assert quality.invalid == 2
