# -*- encoding: utf-8 -*-
"""安装工具模块。

负责 APK/IPA 文件的上传、下载和安装。
"""
from __future__ import annotations

import os
from urllib.request import urlopen
import ssl
import requests
from logzero import logger
from tqdm import tqdm
from perfcattle.core.adb_client import adb


class Install:
    """应用安装工具"""

    def uploadFile(self, file_path, file_obj):
        """保存上传文件

        Args:
            file_path: 保存路径
            file_obj: 文件对象

        Returns:
            bool: 是否保存成功
        """
        try:
            file_obj.save(file_path)
            return True
        except Exception as e:
            logger.exception(e)
            return False

    def downloadLink(self, filelink=None, path=None, name=None):
        """从链接下载文件

        Args:
            filelink: 下载链接
            path: 保存目录
            name: 文件名

        Returns:
            bool: 是否下载成功
        """
        try:
            ssl._create_default_https_context = ssl._create_unverified_context
            file_size = int(urlopen(filelink).info().get("Content-Length", -1))
            header = {"Range": "bytes=%s-%s" % (0, file_size)}
            pbar = tqdm(
                total=file_size,
                initial=0,
                unit="B",
                unit_scale=True,
                desc=filelink.split("/")[-1],
            )
            req = requests.get(filelink, headers=header, stream=True)
            with open(os.path.join(path, name), "ab") as f:
                for chunk in req.iter_content(chunk_size=1024):
                    if chunk:
                        f.write(chunk)
                        pbar.update(1024)
            pbar.close()
            return True
        except Exception as e:
            logger.exception(e)
            return False

    def installAPK(self, path):
        """安装 APK 文件

        Args:
            path: APK 文件路径

        Returns:
            tuple: (是否成功, 命令结果)
        """
        result = adb.shell_noDevice(cmd=["install", "-r", path], timeout=300)
        if result == 0:
            os.remove(path)
            return True, result
        else:
            return False, result

    def installIPA(self, path):
        """安装 IPA 文件

        Args:
            path: IPA 文件路径

        Returns:
            tuple: (是否成功, 命令结果)
        """
        result = os.system("tidevice install {}".format(path))
        if result == 0:
            os.remove(path)
            return True, result
        else:
            return False, result
