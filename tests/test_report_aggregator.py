"""Report aggregation regression tests."""
from unittest.mock import patch

from perfcattle.report.log_io import LogIO
from perfcattle.report.aggregator import Aggregator


class TestAggregator:

    def test_set_android_perfs(self, tmp_report_dir, sample_log_file):
        """_setAndroidPerfs 汇总 Android 性能数据"""
        with patch("os.getcwd", return_value=str(tmp_report_dir.parent)):
            log_io = LogIO()
            agg = Aggregator(log_io=log_io)

            # 需要补充更多日志文件以满足 _setAndroidPerfs 需求
            scene_dir = tmp_report_dir / "test_scene"

            # mem_swap.log
            (scene_dir / "mem_swap.log").write_text(
                "10:00:00=10\n10:00:01=12\n10:00:02=11\n"
            )
            # jank.log
            (scene_dir / "jank.log").write_text(
                "10:00:00=1\n10:01:00=2\n"
            )
            # gpu.log
            (scene_dir / "gpu.log").write_text(
                "10:00:00=35\n10:00:01=40\n"
            )
            (scene_dir / "upflow.log").write_text(
                "10:00:00=1024\n10:00:01=512\n"
            )
            (scene_dir / "downflow.log").write_text(
                "10:00:00=2048\n10:00:01=1024\n"
            )
            # file io logs are recorded in KB
            (scene_dir / "file_read.log").write_text(
                "10:00:00=1024\n10:00:01=512\n"
            )
            (scene_dir / "file_write.log").write_text(
                "10:00:00=2048\n"
            )
            (scene_dir / "thermal_cpu.log").write_text(
                "10:00:00=45\n10:00:01=47\n"
            )
            (scene_dir / "thermal_gpu.log").write_text(
                "10:00:00=44\n"
            )

            result = agg._setAndroidPerfs("test_scene")
            assert result is not None
            assert result["app"] == "com.test.app"
            assert result["devices"] == "test_device"
            assert result["platform"] == "Android"
            # 验证 CPU 数据被聚合
            assert "cpuAppRate" in result
            assert "cpuSystemRate" in result
            assert "%" in str(result["cpuAppRate"])
            assert result["jank"] == "3/min"
            assert result["flow_send"] == "1.5MB"
            assert result["flow_recv"] == "3.0MB"
            assert result["file_read"] == "1.5MB"
            assert result["file_write"] == "2.0MB"
            assert result["totalPassPeak"] == "260MB"
            assert result["frameStability"] == "98.62"
            assert result["thermalCpuAvg"] == "46°C"
            assert result["thermalGpuAvg"] == "44°C"
            assert "file_total" not in result
            assert result["summary_flags"]["battery_level"] is True
            assert result["summary_flags"]["mem_peak"] is True
            assert result["summary_flags"]["frame_stability"] is True
            assert result["summary_flags"]["file_io"] is True
            assert result["summary_flags"]["gpu"] is True
            assert result["summary_flags"]["thermal_cpu_avg"] is True
            assert result["summary_flags"]["thermal_gpu_avg"] is True
            # 没有 Unity 日志时，顶部摘要不出现 Unity 卡片
            assert result["unity_flag"] is False
            assert result["summary_flags"]["unity_cpu_frame"] is False

    def test_set_android_perfs_adds_unity_summary(
        self, tmp_report_dir, sample_log_file
    ):
        """Unity 日志存在时，聚合出报告顶部的 Unity 摘要值"""
        with patch("os.getcwd", return_value=str(tmp_report_dir.parent)):
            log_io = LogIO()
            agg = Aggregator(log_io=log_io)
            scene_dir = tmp_report_dir / "test_scene"
            (scene_dir / "unity_stats.log").write_text(
                '10:00:00={"cpu_frame_ms":16.7,"gpu_frame_ms":12.5,'
                '"draw_calls":300,"triangles":230000,"vertices":231000,"frame":1}\n'
                '10:00:01={"cpu_frame_ms":17.1,"gpu_frame_ms":12.9,'
                '"draw_calls":320,"triangles":231000,"vertices":232000,"frame":2}\n'
            )

            result = agg._setAndroidPerfs("test_scene")

            assert result["unity_flag"] is True
            assert result["unityCpuFrame"] == 16.9
            assert result["unityGpuFrame"] == 12.7
            assert result["unityDrawCalls"] == 310.0
            # 三角面 / 顶点统一换算为 k
            assert result["unityTriangles"] == 230.5
            assert result["unityVertices"] == 231.5
            assert result["summary_flags"]["unity_cpu_frame"] is True
            assert result["summary_flags"]["unity_gpu_frame"] is True
            assert result["summary_flags"]["unity_draw_calls"] is True
            assert result["summary_flags"]["unity_triangles"] is True
            assert result["summary_flags"]["unity_vertices"] is True
