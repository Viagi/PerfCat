# -*- encoding: utf-8 -*-
"""HTTP 请求工具 Method 类（从缓存恢复）"""
from __future__ import annotations

from perfcattle.core.settings_cache import ApmSettingsCache


class Method:
    @classmethod
    def _request(cls, request, object):
        """根据请求方法获取参数"""
        match request.method:
            case "POST":
                return request.form[object]
            case "GET":
                return request.args[object]
            case _:
                raise Exception("request method error")

    @classmethod
    def _setValue(cls, value, default=0):
        """安全取值，异常时返回默认值"""
        try:
            result = value
        except (ZeroDivisionError, IndexError, Exception):
            result = default
        return result

    @classmethod
    def _settings(cls, request):
        """从本地文件获取页面设置。"""
        return ApmSettingsCache.load()

    @classmethod
    def _index(cls, target: list, index: int, default=None):
        """安全索引访问"""
        try:
            return target[index]
        except IndexError:
            return default
