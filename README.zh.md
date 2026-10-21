<p align="center">
  <a>中文</a> | <a href="./README.md">English</a> | <a href="./FAQ.md">FAQ</a> | <a href="https://mp.weixin.qq.com/s?__biz=MzkxMzYyNDM2NA==&mid=2247484506&idx=1&sn=b7eb6de68f84bed03001375d08e08ce9&chksm=c17b9819f60c110fd14e652c104237821b95a13da04618e98d2cf27afa798cb45e53cf50f5bd&token=1402046775&lang=zh_CN&poc_token=HKmRi2WjP7gf9CVwvLWQ2cRhrUR3wmbB9-fNZdD4" target="__blank">使用文档</a>
</p>

<p align="center">
<a href="#">
<img src="./perfcat/static/logo/logo.svg" alt="PerfCat" width="100">
</a>
<br>
</p>
<p align="center">
<a href="https://pypi.org/project/perfcat/" target="__blank"><img src="https://img.shields.io/pypi/v/perfcat" alt="perfcat preview"></a>
<a href="https://pepy.tech/project/perfcat" target="__blank"><img src="https://static.pepy.tech/personalized-badge/perfcat?period=total&units=international_system&left_color=grey&right_color=orange&left_text=downloads"></a>
<br>
</p>

## 🔎简介

PerfCat是一个可以实时收集Android/iOS性能数据的web工具。

快速定位分析性能问题，提升应用的性能和品质。无需ROOT/越狱，即插即用。

![PerfCat](perfcat/static/image/readme/home.png)

## 📦环境

- 安装 Python 3.10 + [**Download**](https://www.python.org/downloads/)
- 安装 adb和配置好环境变量 (PerfCat自带的adb不一定适配你的电脑，建议自己安装) [**Download**](https://developer.android.com/studio/releases/platform-tools)

💡 如果Windows用户需要测试iOS，请先安装Itunes. [**参考**](https://github.com/alibaba/taobao-iphone-device)  （不支持iOS17）

## 📥安装

### 默认

```shell
pip install -U perfcat (指定版本：pip install perfcat==版本)
```

### 镜像

```shell
pip install -i  https://mirrors.ustc.edu.cn/pypi/web/simple -U perfcat
```

💡 如果你的网络无法通过 [pip install -U perfcat] 下载, 可以尝试使用镜像下载，但是可能不是最新版本.

## 🚀启动

### 默认

```shell
python -m perfcat
```

### 自定义

```shell
python -m perfcat --host={ip} --port={port}
```

### 日志文件

PerfCat 默认会把运行日志写入当前目录的 `logs/perfcat.log`，控制台只输出错误日志。

```shell
python -m perfcat --log_file=/path/to/perfcat.log
```

## 🏴󠁣󠁩󠁣󠁭󠁿使用python收集

```python
# perfcat version : >= 2.9.0
from perfcat.service.monitor import AppPerformanceMonitor
from perfcat.device.manager import Devices

d = Devices()
processList = d.getPid(deviceId='ca6bd5a5', pkgName='com.bilibili.app.in') # for android
print(processList) # ['{pid}:{packagename}',...]，一个app可能会有多个进程，如果需要指定pid，可以从这里获取

apm = AppPerformanceMonitor(pkgName='com.bilibili.app.in',platform='Android', deviceId='ca6bd5a5', surfaceview=True, 
                            noLog=False, pid=None, record=False, collect_all=False)
# apm = AppPerformanceMonitor(pkgName='com.bilibili.app.in', platform='iOS')
# surfaceview： 为False时是使用gfxinfo方式，需要在手机上设置：(手机开发者 - GPU渲染模式 - adb shell dumpsys gfxinfo) 不推荐使用这种方式
# noLog : False (保存测试数据到log文件中)

# ************* 收集单个性能参数 ************* #
cpu = apm.collectCpu() # %
memory = apm.collectMemory() # MB
memory_detail = apm.collectMemoryDetail() # MB
network = apm.collectNetwork(wifi=True) # KB，wifi=False 时读取移动数据接口流量
fps = apm.collectFps() # HZ
battery = apm.collectBattery() # level:% temperature:°C current:mA voltage:mV power:w
gpu = apm.collectGpu() # % 安卓只支持高通芯片的手机
disk = apm.collectDisk() # KB
thermal = apm.collectThermal() #温度传感器，收集各个部件的温度（一些手机可能没有权限）

# ************* 收集所有性能参数 ************* #
 
if __name__ == '__main__':  #必须要在__name__ == '__main__'里面执行
  apm = AppPerformanceMonitor(pkgName='com.bilibili.app.in',platform='Android', deviceId='ca6bd5a5', surfaceview=True, 
                              noLog=False, pid=None, record=False, collect_all=True, duration=0)
  # apm = AppPerformanceMonitor(pkgName='com.bilibili.app.in', platform='iOS',  deviceId='xxxx', noLog=False, record=False, collect_all=True, duration=0)
  #duration: 执行时长（秒），只有>0的时候才生效，=0时会持续执行
  #record: 是否录制
  apm.collectAll(report_path=None) # report_path='/test/report.html', None则保存在默认路径

# 在另外的python脚本中可以主动终止perfcat服务，无需等待设置的执行时长结束
from perfcat.service.monitor import AppPerformanceMonitor

AppPerformanceMonitor.stop()
```

## 🏴󠁣󠁩󠁣󠁭󠁿使用API收集

### 后台启动服务

```
# perfcat version >= 2.8.7

macOS/Linux: nohup python3 -m perfcat &
Windows: start /min python3 -m perfcat &
```

### 通过api请求数据

```shell
Android: http://{ip}:{port}/apm/collect?platform=Android&deviceid=ca6bd5a5&pkgname=com.bilibili.app.in&target=cpu
iOS: http://{ip}:{port}/apm/collect?platform=iOS&pkgname=com.bilibili.app.in&target=cpu

target in ['cpu','memory','memory_detail','network','fps','battery','gpu']
```

## 🔥功能

* **无需ROOT/越狱:** Android设备无需ROOT，iOS设备无需越狱。高效解决Android & iOS性能测试分析难题。
* **数据完整性:** 可提供FPS、Jank、CPU、GPU、Memory、Battery 、Network、Disk等性能参数，这些您都可以轻松获得。
* **美观的报告看板:** 报告看板，您可以随时随地存储、可视化、编辑、管理和下载使用任何版本的PerfCat收集的所有测试数据。
* **好用的监控设置:** 支持在监控过程中设置告警值、收集时长、访问其他PC机器的移动设备。
* **比对模式:** 支持两台移动设备同时对比测试。

  - 🌱2-devices: 使用两台不同的设备测试同一个app。
  - 🌱2-apps: 使用两台配置相同的设备测试两个不同的app。
* **API收集性能数据:** 支持python、API收集性能数据，帮助用户轻松集成在CI/CD流程。

## 浏览器

<img src="https://cdn.nlark.com/yuque/0/2023/png/153412/1677553244198-96ce5709-f33f-4038-888f-f330d1f74450.png" alt="Chrome" width="50px" height="50px" />

## 终端

- windows: PowerShell
- macOS：iTerm2 (https://iterm2.com/)

## 💕感谢

- https://github.com/alibaba/taobao-iphone-device
- https://github.com/Genymobile/scrcpy

## 联系

关注公众号，直接发私信，作者看到就回复

<img src="./perfcat/static/image/communicate.png" alt="PerfCat" width="300">
