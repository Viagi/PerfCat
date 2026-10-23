#!/usr/bin/env python
# coding: utf-8
#
# Licensed under MIT
#
import setuptools
from perfcattle import __version__

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setuptools.setup(
    install_requires=[
        "flask>=2.0.1",
        "requests>=2.28.2",
        "logzero",
        "Flask-SocketIO==4.3.1",
        "fire",
        "python-engineio==3.13.2",
        "python-socketio==4.6.0",
        "Werkzeug>=2.3.8,<3",
        "Jinja2==3.0.1",
        "tidevice==0.9.7",
        "tqdm",
        "xlwt",
        "pyfiglet",
        "psutil",
        "opencv-python",
    ],
    version=__version__,
    long_description=long_description,
    python_requires=">=3.10",
    long_description_content_type="text/markdown",
    description="PerfCattle - Real-time collection tool for Android/iOS performance data.",
    packages=setuptools.find_namespace_packages(
        include=["perfcattle", "perfcattle.*"],
    ),
    include_package_data=True,
)
