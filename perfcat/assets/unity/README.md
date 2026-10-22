# PerfCat Unity capture

> 完整技术文档（版本注册表、线协议、扩展新 Unity 版本/新功能的步骤）见仓库
> `docs/UNITY_CAPTURE.md`；从界面下载的 ZIP 中也附带了一份 `UNITY_CAPTURE.md`。

PerfCat 只负责**采集和归档**，不解析 Unity 数据。支持两种接入方式，启动时按顺序自动尝试：

## 1. 协议模式（无需改 Unity 工程）

PerfCat 直接以 Unity Editor 的身份连接 Player 的 PlayerConnection 端口，完成握手后：

* 发送 `kProfilerEnableCapture` 开启 profiler，接收原始 profiler 数据流
* 发送 `kMessageSnapshotRequest` 触发 MemoryProfiler 快照，接收 Begin/Chunk/End 数据

当前已在 **Unity 2022.3.62f2（PlayerConnection version 1048832，Android IL2CPP）** 上实测通过：
2 秒约 10 MB profiler 数据，一次快照约 160 MB / 3～4 秒。

### 前提

* **Development Build**，且 Player 开启了 PlayerConnection 监听（默认 55000 起）。
* MemoryProfiler 快照要求包里包含 `com.unity.memoryprofiler`（`Unity.MemoryProfiler.dll`）。
  没有该包时 profiler 流仍然可用，快照请求不会返回数据。
* 目前只支持 Unity 2022.3 的协议表。其他版本会被识别出来并拒绝，而不是盲目发送消息。

### 协议要点（用于后续版本适配）

```text
每帧 24 字节头：
  uint32 magic = 0x67a54e8f
  uint8[16] message GUID
  uint32 payload length
  uint8[payload]
```

Unity 的消息 GUID 由 32 位十六进制字符串经 `UnityGUID::Generate` 生成；它不是按文本顺序落盘，
而是每 8 个字符打包成一个 little-endian uint32（等价于每 4 字节组反序）。已用 PlayerInfo
握手帧验证过这一变换，见 `perfcat/assets/unity_protocol.py` 里的 `guid_from_hex`。

```text
开启 profiler:  kProfilerEnableCapture + uint32 1   （uint32 0 关闭）
profiler 数据:  kProfilerDataMessage 的 payload 顺序拼接
内存快照:       kMessageSnapshotRequest + uint32 CaptureFlags
                回复 kMessageSnapshotDataBegin / Chunk / End
```

### 体积与开销

* profiler 流约 5～6 MB/s（每帧约 16 KB），几分钟就是几百 MB，注意磁盘空间。
* 快照体积随项目增长，这个测试工程约 160 MB。
* 开启 profiler 会给游戏带来额外开销，APM 指标会受到影响，分析时需要注意。

## 2. 脚本模式（兼容性最好）

在 Unity 工程里放两个脚本，由脚本在设备上写 profiler 二进制日志并接受命令：
这种方式不依赖协议表，适用于其他 Unity 版本。

| 文件 | 作用 |
| --- | --- |
| `PerfCatCapture.cs` | 自动挂载、开启 profiler 二进制日志、监听本机命令端口 |
| `PerfCatCaptureRunner.cs` | 把需要主线程执行的命令（Memory Profiler 快照）排队到 `Update()` |

拷到 Unity 工程的任意目录（例如 `Assets/PerfCat/`）即可，**不需要挂到场景**：
`[RuntimeInitializeOnLoadMethod]` 会在播放器启动时自动运行，编辑器里不生效。

脚本模式下抓到的文件在 `persistentDataPath/perfcat/`，PerfCat 通过 `adb pull` 归档；
协议模式下 PerfCat 直接写本地文件，不需要设备侧目录。

## 输出

```text
report/apm_<时间>/unity/
    profiler.raw              原始 profiler 数据（二进制日志流）
    memory_<时间>.snapshot    Memory Profiler 快照
    meta.json                 设备、包名、Unity 版本、模式、字节数
```

`.snapshot` 用 Memory Profiler 打开。`.raw` 是 profiler 原始数据流/二进制日志，
用对应的 profiler 二进制日志解析工具打开；PerfCat 不做解析。
