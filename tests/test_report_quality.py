"""Report aggregation quality regressions: independence, direction flags,
and time-distance gating for derived metrics."""
from unittest.mock import patch

from perfcattle.report.log_io import LogIO
from perfcattle.report.aggregator import Aggregator


class TestReportQuality:

    def test_jank_is_independent_of_fps(self, tmp_report_dir, sample_log_file):
        """Jank must not be zeroed just because the fps log is empty."""
        scene_dir = sample_log_file
        (scene_dir / "fps.log").unlink()
        (scene_dir / "jank.log").write_text("10:00:00=1\n10:01:00=2\n")

        with patch("os.getcwd", return_value=str(tmp_report_dir.parent)):
            agg = Aggregator(log_io=LogIO())
            result = agg._setAndroidPerfs("test_scene")

        assert result["fps"] == 0
        assert result["jank"] == "3/min"

    def test_network_send_recv_flags_are_independent(self, tmp_report_dir, sample_log_file):
        scene_dir = sample_log_file
        # Only an upflow log exists; downflow is absent.
        (scene_dir / "upflow.log").write_text("10:00:00=1024\n")
        if (scene_dir / "downflow.log").exists():
            (scene_dir / "downflow.log").unlink()

        with patch("os.getcwd", return_value=str(tmp_report_dir.parent)):
            agg = Aggregator(log_io=LogIO())
            result = agg._setAndroidPerfs("test_scene")

        assert result["summary_flags"]["network_send"] is True
        assert result["summary_flags"]["network_recv"] is False

    def test_nearest_absolute_respects_max_distance(self):
        points = [(36000.0, 60.0)]  # 10:00:00
        assert LogIO._nearest_absolute(points, 36000.0, max_distance=2.0) == 60.0
        # 10:00:05 is 5 seconds away; a 2-second gate must reject it.
        assert LogIO._nearest_absolute(points, 36005.0, max_distance=2.0) is None
