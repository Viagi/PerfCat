# -*- encoding: utf-8 -*-
"""pytest 全局配置和共享 fixtures"""
import os
import sys
import tempfile
import shutil
import pytest

# 确保 perfcat 包可以导入
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def tmp_report_dir(tmp_path):
    """创建临时报告目录"""
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    return report_dir


@pytest.fixture
def sample_log_file(tmp_report_dir):
    """创建带有样例数据的日志文件"""
    scene_dir = tmp_report_dir / "test_scene"
    scene_dir.mkdir()

    # cpu_app.log
    cpu_log = scene_dir / "cpu_app.log"
    cpu_log.write_text(
        "10:00:00=15.5\n"
        "10:00:01=20.3\n"
        "10:00:02=18.7\n"
    )

    # cpu_sys.log
    cpu_sys_log = scene_dir / "cpu_sys.log"
    cpu_sys_log.write_text(
        "10:00:00=30.1\n"
        "10:00:01=35.2\n"
        "10:00:02=28.9\n"
    )

    # mem_total.log
    mem_log = scene_dir / "mem_total.log"
    mem_log.write_text(
        "10:00:00=256\n"
        "10:00:01=260\n"
        "10:00:02=258\n"
    )

    # fps.log
    fps_log = scene_dir / "fps.log"
    fps_log.write_text(
        "10:00:00=60\n"
        "10:00:01=58\n"
        "10:00:02=59\n"
    )

    # battery_level.log
    battery_level = scene_dir / "battery_level.log"
    battery_level.write_text(
        "10:00:00=95\n"
        "10:00:01=94\n"
    )

    # battery_tem.log
    battery_tem = scene_dir / "battery_tem.log"
    battery_tem.write_text(
        "10:00:00=32\n"
        "10:00:01=33\n"
    )

    # result.json
    result_json = scene_dir / "result.json"
    import json
    result_json.write_text(json.dumps({
        "app": "com.test.app",
        "devices": "test_device",
        "platform": "Android",
        "ctime": "2024-01-01-12-00-00",
        "video": 0,
    }))

    return scene_dir
