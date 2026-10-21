# -*- encoding: utf-8 -*-
"""FPS collector surface selection regressions."""
from unittest.mock import MagicMock, patch

from perfcat.collector.fps import SurfaceStatsCollector


def make_collector(package_name="com.example.cocos"):
    return SurfaceStatsCollector(
        device="abc123",
        frequency=1,
        package_name=package_name,
        fps_queue=None,
        jank_threshold=166,
        surfaceview=True,
        fps_monitor=MagicMock(),
    )


def test_surface_candidates_prefer_wechat_game_blast_surface():
    collector = make_collector("com.tencent.mm")
    collector.focus_window = "com.tencent.mm/.plugin.appbrand.ui.AppBrandUI1"

    with patch("perfcat.collector.fps.adb") as mock_adb:
        mock_adb.shell.return_value = "\n".join(
            [
                "ActivityRecordInputSink com.tencent.mm/.plugin.appbrand.ui.AppBrandUI1#20458",
                "SurfaceView[com.tencent.mm/com.tencent.mm.plugin.appbrand.ui.AppBrandUI1]#20750",
                "SurfaceView[com.tencent.mm/com.tencent.mm.plugin.appbrand.ui.AppBrandUI1](BLAST)#20751",
                "com.tencent.mm/com.tencent.mm.plugin.appbrand.ui.AppBrandUI1#20726",
            ]
        )
        candidates = collector._surface_candidates()

    assert candidates[0] == (
        "SurfaceView[com.tencent.mm/com.tencent.mm.plugin.appbrand.ui.AppBrandUI1](BLAST)#20751"
    )
    assert not any("InputSink" in candidate for candidate in candidates)


def test_select_surface_latency_falls_back_to_global_frame_data():
    collector = make_collector()
    collector.focus_window = "com.example.cocos/org.cocos2dx.javascript.AppActivity"

    def shell_side_effect(cmd, deviceId, timeout=None):
        if cmd == "dumpsys SurfaceFlinger --list":
            return "com.example.cocos/com.example.MainActivity#0"
        if cmd == "dumpsys SurfaceFlinger --latency":
            return (
                "16666666\n"
                "0 1000000000 1000000000\n"
                "0 1016666666 1016666666\n"
            )
        if cmd.startswith("dumpsys SurfaceFlinger --latency "):
            return "16666666\n"
        return ""

    with patch("perfcat.collector.fps.adb") as mock_adb:
        mock_adb.shell.side_effect = shell_side_effect
        lines = collector._select_surface_latency()

    assert lines == [
        "16666666",
        "0 1000000000 1000000000",
        "0 1016666666 1016666666",
    ]
