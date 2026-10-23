# -*- encoding: utf-8 -*-
"""Excel 导出模块

负责将性能日志数据导出为 Excel 文件。
"""
import os
import xlwt
from perfcattle.report.log_io import LogIO


class ExcelExporter:
    """Excel 报告导出器"""

    def __init__(self, log_io: LogIO = None):
        """初始化导出器

        Args:
            log_io: LogIO 实例，用于获取报告目录路径。如果为 None 则创建默认实例。
        """
        self.log_io = log_io or LogIO()

    @property
    def report_dir(self):
        return self.log_io.report_dir

    def export_excel(self, platform, scene):
        """导出 Excel 报告

        Args:
            platform: 平台类型（"Android" 或 "iOS"）
            scene: 场景目录名

        Returns:
            str: 生成的 Excel 文件路径
        """
        android_log_file_list = [
            "cpu_app",
            "cpu_sys",
            "mem_total",
            "mem_swap",
            "battery_level",
            "battery_tem",
            "upflow",
            "downflow",
            "file_read",
            "file_write",
            "fps",
            "gpu",
        ]
        ios_log_file_list = [
            "cpu_app",
            "cpu_sys",
            "mem_total",
            "battery_tem",
            "battery_current",
            "battery_voltage",
            "battery_power",
            "upflow",
            "downflow",
            "fps",
            "gpu",
        ]
        log_file_list = (
            android_log_file_list if platform == "Android" else ios_log_file_list
        )
        wb = xlwt.Workbook(encoding="utf-8")
        k = 1
        for name in log_file_list:
            ws1 = wb.add_sheet(name)
            ws1.write(0, 0, "Time")
            ws1.write(0, 1, "Value")
            row = 1  # 起始行
            col = 0  # 起始列
            if os.path.exists(f"{self.report_dir}/{scene}/{name}.log"):
                f = open(f"{self.report_dir}/{scene}/{name}.log", "r", encoding="utf-8")
                for lines in f:
                    target = lines.split("=")
                    k += 1
                    for i in range(len(target)):
                        ws1.write(row, col, target[i])
                        col += 1
                    row += 1
                    col = 0
        xls_path = os.path.join(self.report_dir, scene, f"{scene}.xls")
        wb.save(xls_path)
        return xls_path
