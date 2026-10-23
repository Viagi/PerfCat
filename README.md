<p align="center">
  <a>English</a> | <a href="./README.zh.md">中文</a> | <a href="./FAQ.md">FAQ</a> | <a href="https://mp.weixin.qq.com/s?__biz=MzkxMzYyNDM2NA==&mid=2247484506&idx=1&sn=b7eb6de68f84bed03001375d08e08ce9&chksm=c17b9819f60c110fd14e652c104237821b95a13da04618e98d2cf27afa798cb45e53cf50f5bd&token=1402046775&lang=zh_CN&poc_token=HKmRi2WjP7gf9CVwvLWQ2cRhrUR3wmbB9-fNZdD4" target="__blank">使用文档</a>
</p>

<p align="center">
<a href="#">
<img src="./perfcattle/static/logo/logo.svg" alt="PerfCattle 性能牛" width="120">
</a>
<br>
<b>PerfCattle · 性能牛</b>
</p>
<p align="center">
<a href="https://pypi.org/project/perfcattle/" target="__blank"><img src="https://img.shields.io/pypi/v/perfcattle" alt="perfcattle preview"></a>
<a href="https://pepy.tech/project/perfcattle" target="__blank"><img src="https://static.pepy.tech/personalized-badge/perfcattle?period=total&units=international_system&left_color=grey&right_color=orange&left_text=downloads"></a>

<br>
</p>

## 🔎Preview

PerfCattle (性能牛) - Real-time collection tool for Android/iOS performance data.

Quickly locate and analyze performance issues to improve application performance and quality. No need for ROOT/jailbreak, plug and play.

![PerfCattle](perfcattle/static/image/readme/home.png)

## 📦Requirements

- Install Python 3.10 + [**Download**](https://www.python.org/downloads/)
- Install adb and configure environment variables (PerfCattle's  adb may not necessarily fit your computer) [**Download**](https://developer.android.com/studio/releases/platform-tools)

💡 If Windows users need to test ios, install and start Itunes. [**Documentation**](https://github.com/alibaba/taobao-iphone-device)  (Not support iOS17)

## 📥Installation

### default

```shell
pip install -U perfcattle    (pip install perfcattle==version)
```

### mirrors

```shell
pip install -i  https://mirrors.ustc.edu.cn/pypi/web/simple -U perfcattle
```

💡 If your network is unable to download through [pip install -U perfcattle], please try using mirrors to download, but the download of PerfCattle may not be the latest version.

## 🚀Quickstart

### default

```shell
python -m perfcattle
```

### customize

```shell
python -m perfcattle --host=ip --port=port
```

### Log file

PerfCattle writes runtime logs to `logs/perfcattle.log` in the current directory by default. The console only shows errors.

```shell
python -m perfcattle --log_file=/path/to/perfcattle.log
```

## 🏴󠁣󠁩󠁣󠁭󠁿Python API

```python
# perfcattle version : >= 2.8.5
from perfcattle.service.monitor import AppPerformanceMonitor
from perfcattle.device.manager import Devices

d = Devices()
processList = d.getPid(deviceId='ca6bd5a5', pkgName='com.bilibili.app.in') # for android
print(processList) # ['{pid}:{packagename}',...]

apm = AppPerformanceMonitor(pkgName='com.bilibili.app.in',platform='Android', deviceId='ca6bd5a5', surfaceview=True, 
                            noLog=False, pid=None, record=False, collect_all=False, duration=0)
# apm = AppPerformanceMonitor(pkgName='com.bilibili.app.in', platform='iOS') only supports one device
# surfaceview： False = gfxinfo (Developer - GPU rendering mode - adb shell dumpsys gfxinfo)
# noLog : False (Save test data to log file)

# ************* Collect a performance parameter ************* #
cpu = apm.collectCpu() # %
memory = apm.collectMemory() # MB
memory_detail = apm.collectMemoryDetail() # MB
network = apm.collectNetwork(wifi=True) # KB
fps = apm.collectFps() # HZ
battery = apm.collectBattery() # level:% temperature:°C current:mA voltage:mV power:w
gpu = apm.collectGpu() # %
disk = apm.collectDisk()
thermal = apm.collectThermal()
# ************* Collect all performance parameter ************* #
 
if __name__ == '__main__':
  apm = AppPerformanceMonitor(pkgName='com.bilibili.app.in',platform='Android', deviceId='ca6bd5a5', surfaceview=True, 
                              noLog=False, pid=None, record=False, collect_all=True, duration=0)
  # apm = AppPerformanceMonitor(pkgName='com.bilibili.app.in', platform='iOS',  deviceId='xxxx', noLog=False, record=False, collect_all=True, duration=0)
  #duration: running time (second)
  #record: record android screen
  apm.collectAll(report_path=None) # report_path='/test/report.html'

# in other python file
from perfcattle.service.monitor import AppPerformanceMonitor

AppPerformanceMonitor.stop() # stop perfcattle
```

## 🏴󠁣󠁩󠁣󠁭󠁿Service API

### Start the service in the background

```
# perfcattle version >= 2.1.5

macOS/Linux: nohup python3 -m perfcattle &
Windows: start /min python3 -m perfcattle &
```

### Request performance data from api

```shell
Android: http://{ip}:{port}/apm/collect?platform=Android&deviceid=ca6bd5a5&pkgname=com.bilibili.app.in&target=cpu
iOS: http://{ip}:{port}/apm/collect?platform=iOS&pkgname=com.bilibili.app.in&target=cpu

target in ['cpu','memory','memory_detail','network','fps','battery','gpu']
```

## 🔥Features

* **No ROOT/Jailbreak:** No need of Root for Android devices, Jailbreak for iOS devices. Efficiently solving the test and analysis challenges in Android & iOS performance.
* **Data Integrality:** We provide the data about CPU, GPU, Memory, Battery, Network,FPS, Jank, etc., which you may easy to get.
* **Beautiful Report:** A beautiful and detailed report analysis, where to store, visualize, edit, manage, and download all the test cases collected with PerfCattle no matter where you are or when is it.
* **Useful Monitoring Settings:** Support setting alarm values, collecting duration, and accessing mobile devices on other PC machines during the monitoring process.
* **PK Model:** Supports simultaneous comparative testing of two mobile devices.

  - 🌱2-devices: test the same app on two different phones.
  - 🌱2-apps: test two different apps on two phones with the same configuration.
* **Multi-Report Compare:** Select 2–6 normal reports of the same app and platform on the report page. The summary table expands per scene, and multi-metric charts are split into separate charts with one series per scene.
* **Collect In Python/API:** Support Python and API to collect performance data, helping users easily integrate into automated testing processes.
* **GPU Extended Stats:** Optional GPU clock, bus vote, temperature, memory and Adreno read/write bandwidth. The report shows bandwidth as **MB/frame**, aligned with the FPS samples.
* **Unity Capture:** Record Unity Profiler / MemoryProfiler data alongside the normal APM session. It tries a drop-in Unity helper first, then falls back to speaking the Editor's PlayerConnection protocol directly (Unity 2022.3 verified, no game changes). Raw files are archived under `report/<scene>/unity/`; exporting a test report bundles them with the performance data and an offline JSON.
* **Unity Profiler JSON Export:** Report export converts `profiler.raw` into JSON (frame/thread timings, hotspot markers, GC allocations, slowest-frame call trees) so captures can be analysed without Unity.
* **Comparison Export:** The compare page exports a ZIP with `compare.xls` / `compare.json` plus every report's original Excel, metadata, Unity Profiler JSON and `.raw` / `.snapshot` binaries.
* **Deep Profiler / Profiler Modules:** Tick the same Profiler Modules as the Unity Profiler and toggle Deep Profiler; in protocol mode both can be changed live during a capture (Deep Profiler requires the build's Deep Profiling Support).
* **Unity Game Panel:** Protocol mode parses the raw Profiler stream directly (Unity 2022.2+), with the in-game helper as an option. It shows CPU/GPU frame time, Draw Calls, SetPass Calls, Batches, Triangles, Vertices, GC Alloc, used memory, plus Render Textures, Used Buffers and vertex/index buffer uploads (count + memory), and writes them to the report and comparison pages on the same time axis as system CPU.
* **Unity Hotspots:** Per-frame Timeline (thread rows of marker blocks), Hierarchy (merged call paths with Total/Self/Calls/GC) and Raw Hierarchy (unmerged calls), with thread selection and sorting in the analysis page.
* **Unity Capture Guide:** [docs/UNITY_CAPTURE.md](docs/UNITY_CAPTURE.md) — supported PlayerConnection versions, wire format, Area→Category mapping, and the checklist for adding a new Unity version or Profiler feature.

## Develop

* https://github.com/pallets/flask
* https://github.com/tabler/tabler

## Browser

<img src="https://cdn.nlark.com/yuque/0/2023/png/153412/1677553244198-96ce5709-f33f-4038-888f-f330d1f74450.png" alt="Chrome" width="50px" height="50px" />

## Terminal

- windows: PowerShell
- macOS：iTerm2 (https://iterm2.com/)

## 💕Thanks

- https://github.com/alibaba/taobao-iphone-device
- https://github.com/Genymobile/scrcpy
