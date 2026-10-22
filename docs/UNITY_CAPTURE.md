# Unity Profiler / MemoryProfiler 采集技术文档

本文档说明 PerfCat 的 Unity 采集链路、当前兼容版本、协议格式、Deep Profiler 与
Profiler Modules 的实现方式、HTTP/UI 用法、真机实测结果，以及如何扩展新的
Unity 版本或新的 Profiler 功能。

> 结论先说：**PerfCat 只负责采集和归档，不解析 profiler / snapshot 数据。**
> 采集到的原始文件用 Unity Profiler / Memory Profiler 或对应解析工具打开。

---

## 1. 功能概览

### 1.1 能力矩阵

| 功能 | Protocol 模式 | Helper 模式 | UI 入口 | HTTP 入口 |
| --- | --- | --- | --- | --- |
| Profiler 原始流 | 支持 | 支持 | Unity Profiler 开关 | `/apm/unity/start` |
| MemoryProfiler 快照 | 支持 | 支持 | Unity 菜单 → MemoryProfiler → 抓取 | `/apm/unity/snapshot` |
| Deep Profiler | 支持 | 不支持 | Unity 菜单 → Profiler 模块 → Deep Profiler | `start` / `options` 的 `deep_profiling` |
| Profiler Modules 勾选 | 支持 | 不支持 | Unity 菜单 → Profiler 模块 | `start` / `options` 的 `areas` |
| 采集中热切换 | 支持 | 不支持 | 模块弹窗 → 应用 | `/apm/unity/options` |
| 随 APM 自动开始/停止 | 支持 | 支持 | 随 Unity Profiler 开关 | — |
| 随报告归档 | 支持 | 支持 | 分析页下载 | `/apm/unity/files` / `download` |

### 1.2 已验证环境

| 项目 | 值 |
| --- | --- |
| 设备 | Samsung Galaxy S24+（`R5CX21L23NE`），Android 15 |
| Unity | 2022.3.62f2，Android IL2CPP Player |
| 工程 | `com.QiHuan.FishingGame`（巅峰钓鱼_每日构建） |
| PlayerConnection Version | `1048832` |
| 验证内容 | 握手、Profiler 流、Deep Profiler 热切换、Module 子集、MemoryProfiler 快照 |
| 验证日期 | 2026-10-22 |

> 红米 SM8735 / MTK 设备也可用于普通 APM 采集；本文的 Unity 协议数据以
> S24+ 为准。

### 1.3 默认行为

- Profiler Modules：**14 个全部勾选**。
- Deep Profiler：**默认关闭**。
- 用户的选择会保存在浏览器 `localStorage`：
  - `perfcat.unity.areas`：JSON 数组，例如 `["CPU","GPU"]`
  - `perfcat.unity.deep`：`"1"` / `"0"`
- 选择只对下一次或当前采集生效，不影响已经采集到的原始数据。

---

## 2. 快速使用

### 2.1 UI 使用步骤

1. 启动 PerfCat：

   ```bash
   python -m perfcat
   ```

   默认端口 `50003`，按启动日志打开：

   ```text
   http://<host>:50003/?platform=Android&lan=cn
   ```

2. 连接 Android 设备，选择 App 与进程。

3. 打开指标菜单，确认 **Unity Profiler** 开关已打开。

4. 点击 **Profiler 模块**：

   - 14 个 Module 默认全选；
   - 可以全选 / 全不选 / 单独勾选；
   - 需要方法级采样时打开 **Deep Profiler**；
   - 点击 **应用**。

5. 点击开始监控。Unity 采集与 APM 指标同时开始。

6. 需要内存快照时，点击 **MemoryProfiler → 抓取**。

7. 停止监控并生成报告。分析页可以下载：

   - `unity/profiler.raw`
   - `unity/memory_*.snapshot`
   - `unity/meta.json`

### 2.2 采集中修改 Module / Deep Profiler

- 采集已经开始时，打开 **Profiler 模块**，修改后点击 **应用**：
  - Module 选择立即发送给 Player；
  - Deep Profiler 立即切换，但只影响后续帧。
- 未开始采集时点击 **应用**：只保存设置，下一次开始时生效。
- Helper 模式会提示 `ignored_options`，表示 Deep Profiler / Module 选择未生效。

### 2.3 API 快速示例

```bash
# 开始：只采集 CPU + GPU，并开启 Deep Profiler
curl "http://127.0.0.1:50003/apm/unity/start?platform=Android&device=<device>&pkgname=com.example.game&deep_profiling=1&areas=CPU,GPU"

# 采集中切换：全部 14 个 Module，关闭 Deep Profiler
curl "http://127.0.0.1:50003/apm/unity/options?areas=%5B%22CPU%22%2C%22GPU%22%2C%22Rendering%22%2C%22Memory%22%2C%22Audio%22%2C%22Video%22%2C%22Physics%22%2C%22Physics2D%22%2C%22NetworkMessages%22%2C%22NetworkOperations%22%2C%22UI%22%2C%22UIDetails%22%2C%22GlobalIllumination%22%2C%22VirtualTexturing%22%5D&deep_profiling=0"

# 抓取快照
curl "http://127.0.0.1:50003/apm/unity/snapshot"

# 查看状态
curl "http://127.0.0.1:50003/apm/unity/status"

# 停止
curl "http://127.0.0.1:50003/apm/unity/stop"
```

---

## 3. 当前兼容版本与采集后端

### 3.1 版本判断

版本判断依据是 Player 握手里的 `[Version]`（PlayerConnection 协议版本号），
不是 `Application.unityVersion` 字符串。

| PlayerConnection Version | Unity 版本 | 采集方式 | 真机验证 |
| --- | --- | --- | --- |
| `1048832` | Unity 2022.3 | 协议模式（无需改游戏）+ 可选脚本模式 | 是，2022.3.62f2 / Android IL2CPP |

版本注册表在 [`perfcat/unity/profiles.py`](../perfcat/unity/profiles.py)：

```python
PROFILES = {
    1048832: UNITY_2022_3,
}
```

界面上的“支持版本”提示来自 `GET /apm/unity/versions`，请以该接口返回为准。
未在注册表中的版本会被明确拒绝（`UnsupportedUnityVersion`），不会盲目发送消息。

### 3.2 两种采集后端

| 后端 | 前提 | 适用场景 | Deep / Modules |
| --- | --- | --- | --- |
| `protocol` | Development Build，Player 监听 55000+ | 不想改游戏；已适配的 Unity 版本 | 支持 |
| `helper` | 工程内放入 `perfcat/assets/unity/PerfCatCapture.cs` | 其他 Unity 版本；最稳定的落盘方式 | 不支持 |

管理器按 **helper → protocol** 顺序尝试，二者都不可用时只提示，不中断 APM 采集。

Helper 模式没有 PlayerConnection 通道，因此无法发送 `set_deep_profiler` /
`set_enabled_categories`；接口会在 `ignored_options` 中列出被忽略的选项。

---

## 4. 代码分层

```text
perfcat/unity/
    __init__.py       导出注册表
    profiles.py       ★ 版本 / 消息 GUID / 能力位 / ProfilerArea 注册中心
    protocol.py       ★ 线协议：帧编解码、PlayerConnection 会话、控制消息
    backends.py       ★ HelperBackend / ProtocolBackend
    manager.py        ★ 会话管理、选项、暂存目录、meta.json、报告归档
perfcat/view/unity_api.py       HTTP 接口
perfcat/templates/index.html    实时页：开关、模块勾选、Deep Profiler、快照按钮
perfcat/templates/analysis.html 分析页：原始文件下载
perfcat/assets/unity/           C# helper 与使用说明
docs/UNITY_CAPTURE.md           本文档
```

依赖方向是单向的：

```text
manager → backends → protocol → profiles
   │                    │
   └── 报告归档          └── 只依赖版本能力，不依赖具体 Unity 版本
```

### 4.1 关键函数

| 层 | 函数 | 作用 |
| --- | --- | --- |
| `profiles.py` | `PROFILER_AREAS` | Area → Category 映射表 |
| `profiles.py` | `UnityProfile.normalize_areas()` | 校验 / 去重 Module 名称 |
| `profiles.py` | `UnityProfile.categories_for_areas()` | 把 Area 转成待发送的 Category |
| `protocol.py` | `set_deep_profiling()` | 发送 Deep Profiler 开关 |
| `protocol.py` | `set_enabled_categories()` | 整体替换 Category 集合 |
| `protocol.py` | `set_areas()` | Area → Category 后发送 |
| `protocol.py` | `start_profiler()` | 开流、启动 reader、第一帧后重发 Category |
| `backends.py` | `ProtocolBackend.set_options()` | 采集中热更新 |
| `manager.py` | `UnityCapture.start()` | 统一入口，helper → protocol |
| `manager.py` | `UnityCapture.apply_options()` | 采集中修改选项 |
| `view/unity_api.py` | `/apm/unity/start` / `options` | HTTP 参数解析 |

### 4.2 扩展原则

- **新增 Unity 版本**：只改 `profiles.py`。
- **新增传输方式**：在 `backends.py` 增加一个后端，实现
  `start / snapshot / stop / close / status / set_options`。
- **新增 Profiler 功能**：在 `profiles.py` 增加能力位和消息 ID，在
  `protocol.py` 增加发送方法，再逐层透出。

---

## 5. 线协议

### 5.1 帧格式

```text
uint32 magic = 0x67a54e8f
uint8[16] message GUID
uint32 payload length
uint8[payload]
```

### 5.2 消息 GUID 编码

Unity 内部的 32 位十六进制字符串不是按文本顺序落盘的：

```text
"2257466d0e0e47da89826cf04e68135c"
  ↓ 每 8 个字符打包成一个 little-endian uint32（等价于每 4 字节组反序）
22 75 64 d6 e0 e0 74 ad 98 28 c6 0f e4 86 31 c5
```

实现见 `perfcat/unity/protocol.py::guid_from_hex`。该变换已用 Player 主动发送的
PlayerInfo 帧验证：

```text
"94d3ae57ca4b4ed98408b827f180853d"
  → 49 3d ea 75 ac b4 e4 9d 48 80 8b 72 1f 08 58 d3
  → 与真机握手帧完全一致
```

### 5.3 握手

连接 PlayerConnection 后，Player 会主动发送：

1. PlayerInfo（31 字节）
2. TargetInfo（ASCII，包含 `[IP] [Port] [Version] [Id] [PackageName] [ProjectName]`）

采集端解析 `[Version]`，到 `PROFILES` 中查表，决定后续使用哪套消息 GUID。

### 5.4 控制消息总览

| 消息 | payload | 作用 | 状态 |
| --- | --- | --- | --- |
| `enable_capture` | `uint32` 0/1 | 开 / 关 Profiler 流 | 已实现 |
| `set_deep_profiler` | `uint32` 0/1 | Deep Profiler 开关 | 已实现 |
| `set_enabled_categories` | NUL 分隔的 Category 名 | 整体替换启用的 Category | 已实现 |
| `set_memory_record_mode` | `uint32` | Memory 记录模式 | 已登记，待接线 |
| `set_marker_filtering` | 待确认 | Marker 过滤 | 已登记，待接线 |
| `set_audio_capture_flags` | `uint32` | Audio 采集标志 | 已登记，待接线 |
| `functions_data` | 待确认 | Functions 数据 | 已登记，待接线 |
| `object_memory_profile_snapshot` / `data` | 待确认 | Object memory profile | 已登记，待接线 |
| `query_instrumentable_functions` / `function_callees` | 待确认 | 可插桩函数查询 | 已登记，待接线 |

### 5.5 Profiler 流

```text
enable_capture + uint32 1     开启
enable_capture + uint32 0     关闭
profiler.data                 后续数据帧
```

`profiler.data` 的 payload 按顺序拼接即为 `profiler.raw`。第一条 payload 以
`PD3U` 开头（流头），后续数据块以 `ad7e0cb1` 开头。

采集顺序（Protocol 模式）：

```text
attach
  → set_deep_profiler(0/1)
  → set_enabled_categories(...)        # 先发一次，best effort
  → enable_capture(1)
  → 收到第一帧 profiler.data
  → set_enabled_categories(...)        # 再发一次，确保 Category 已注册
  → profiler.data ...（持续）
  → enable_capture(0)
  → close
```

### 5.6 Deep Profiler

**payload**

```text
uint32 enabled    # 0 = 关闭，非 0 = 开启
```

Player 的消息处理函数只读取第一个 `uint32`，非 0 即开启。

**要求**

- Player Settings → **Deep Profiling Support** 必须在构建时开启；
- 未开启的 Player 会忽略该消息，通常只在 logcat 里能看到提示；
- 开启后 profiler 流会包含每个方法调用的采样，`profiler.raw` 体积显著增大。

**发送时机**

- 建议在 `enable_capture` 之前发送；PerfCat 的 `start` 就是这个顺序。
- 采集中也可以切换（`/apm/unity/options?deep_profiling=1`），但只影响后续帧。
- 实测切换后数据量会立即大幅上升，说明 Player 接受了该消息。

### 5.7 Profiler Modules（ProfilerArea）

#### 5.7.1 为什么需要映射

Unity 编辑器里的 Profiler Modules 勾选项对应 `ProfilerArea` 枚举。Player 侧没有
`ProfilerArea` 这个概念，只有 `ProfilerCategory`，所以 PerfCat 在发送前先做
Area → Category 映射。映射参考 `LegacyProfilerAreaUtility` /
`ProfilerCategoryActivator`，并以 Player profiler 流里实际注册的 Category
名称为准（例如 `Virtual Texturing` 带空格）。

| ProfilerArea | 界面名称 | 发送的 Category |
| --- | --- | --- |
| `CPU` | CPU Usage | `Scripts` |
| `GPU` | GPU Usage | `GPU` |
| `Rendering` | Rendering | `Render` |
| `Memory` | Memory | `Memory` |
| `Audio` | Audio | `Audio` |
| `Video` | Video | `Video` |
| `Physics` | Physics | `Physics` |
| `Physics2D` | Physics 2D | `Physics2D` |
| `NetworkMessages` | Network Messages | `Network` |
| `NetworkOperations` | Network Operations | `Network Operations` |
| `UI` | UI | `Gui` |
| `UIDetails` | UI Details | `Gui` |
| `GlobalIllumination` | Global Illumination | `Lighting` |
| `VirtualTexturing` | Virtual Texturing | `Virtual Texturing` |

#### 5.7.2 payload 格式

`set_enabled_categories` 的 payload 不是位图，而是 **NUL 分隔的
`ProfilerCategory.Name` 字符串列表**（带结尾 NUL）：

```text
"Scripts\0GPU\0Render\0"
```

Player 侧的处理流程：

```text
读取 payload
  → 按 \0 切出每个 Category 名称
  → 放入 hash_set
  → 遍历 Player 已注册的所有 Category
  → 命中 hash_set 的 Category：profiler_set_category_enable(id, true)
  → 未命中的 Category：profiler_set_category_enable(id, false)
```

因此它是**整体替换**语义：

- 调用方必须一次发送完整的目标集合；
- 空 payload 表示关闭全部 Category；
- 多个 Area 共享同一个 Category 时，协议层会去重；
- 名称必须与 Player 实际注册的名称完全一致，否则不会命中。

#### 5.7.3 2022.3 Player 实际 Category 表

从真机 profiler 流头解析出的 Category 名称（顺序即流中的注册顺序）：

```text
Render
Scripts
Managed Jobs
Burst Jobs
Gui
Physics
Animation
Ai
Audio
Audio Job
Audio Update Job
Video
Particles
Lighting
Network
Loading
Other
GC
VSync
Overhead
PlayerLoop
Director
Vr
Memory
Internal
FileIO
UI Layout
UI Render
VFX
Build Interface
Input
Virtual Texturing
GPU
Physics2D
Network Operations
UI Details
Debug
```

几个容易写错的名字：

| 常见写法 | 2022.3 Player 实际名称 |
| --- | --- |
| `VirtualTexturing` | `Virtual Texturing`（带空格） |
| `NetworkOperations` | `Network Operations`（带空格） |
| `UIDetails` | Category 使用 `Gui`；`UI Details` 是另一个独立 Category |
| `Physics2D` | `Physics2D` |

#### 5.7.4 发送时机与重发

Player 在 attach 时 Category 注册表可能还是空的；第一帧 profiler 数据到达后，
内置 Category 才会注册完成。因此 PerfCat：

1. `enable_capture` 之前先发一次（best effort）；
2. 收到第一帧 `profiler.data` 后自动重发一次；
3. 采集过程中用户修改勾选时立即重发。

对应实现：`UnityProtocolSession._apply_areas_when_ready()`。

#### 5.7.5 效果范围与注意点

- Category 状态与 Unity 编辑器的 `ProfilerDriver.SetCategoryEnabled` 语义一致，
  影响 Player 侧 Category / Counter 的采集状态。
- 主 profiler 流里已经产生的 marker 是否被完全过滤取决于 Unity 版本；
  **不能仅凭 `profiler.raw` 大小判断勾选是否生效**。请以 Unity Profiler
  打开后的 Module 状态为准。
- 全部取消勾选时发送空 payload，Player 会关闭所有 Category；此时仍可抓快照，
  但 Module / Counter 数据几乎为空。
- 默认不发送该消息时，Player 使用自己的默认 Category 集合。PerfCat 界面默认
  勾选全部 14 个 Module，因此会显式发送这些 Module 对应的 Category 并集，
  而不是 Player 的全部内置 Category。
- Helper 模式不支持该功能。

### 5.8 MemoryProfiler 快照

```text
snapshot.request + uint32 CaptureFlags(31)
    → snapshot.begin
    → snapshot.chunk × N   （每个 64 KB）
    → snapshot.end
```

把 `begin` 之后的所有 `chunk` payload 顺序拼接即为 `.snapshot` 文件
（文件头以 `01 ed bc fa` 开始，内部包含 UTF-16 的 `Project name:` 等元数据）。

要求包里包含 `com.unity.memoryprofiler`；测试工程快照约 160 MB。

---

## 6. UI 行为

### 6.1 入口

- 实时页指标菜单 → **Unity Profiler** 后面的开关；
- 开关下方显示支持的 Unity 版本；
- **Profiler 模块**：打开 14 个 Module 的勾选弹窗；
- **MemoryProfiler**：抓取快照；
- **采集脚本（可选）**：下载 helper ZIP。

### 6.2 模块弹窗

```text
┌──────────────────────────────────────────────┐
│ Unity Profiler 模块                    [x]   │
├──────────────────────────────────────────────┤
│ Deep Profiler                     [switch]   │
│ 需要构建时开启 Deep Profiling Support；       │
│ 切换只影响后续帧，建议开始采集前设置。          │
│                                              │
│ Profiler 模块                 全选   全不选    │
│ [x] CPU Usage       [x] GPU Usage            │
│ [x] Rendering       [x] Memory               │
│ [x] Audio           [x] Video                │
│ [x] Physics         [x] Physics 2D           │
│ [x] Network Messages[x] Network Operations   │
│ [x] UI              [x] UI Details           │
│ [x] Global Illum.   [x] Virtual Texturing    │
│                                              │
│ 发送给 Player 的 Category：Scripts, GPU, ...  │
├──────────────────────────────────────────────┤
│                            取消      应用     │
└──────────────────────────────────────────────┘
```

### 6.3 行为细节

- 默认全选；`localStorage` 会记住上次选择。
- 顶部菜单右侧的 badge 显示摘要，例如 `14/14 · Deep OFF`。
- 未开始采集时点击 **应用**：只保存，下一次开始生效。
- 采集中点击 **应用**：调用 `/apm/unity/options` 立即热更新。
- Helper 模式：提示 `Helper 模式不支持：deep_profiling, areas`。
- 全部取消勾选：badge 变为警告色，发送空 Category 集合。
- 页面刷新后，如果接口返回的 Module 列表发生变化，会过滤掉无效名称。

---

## 7. HTTP 接口

| 路由 | 说明 |
| --- | --- |
| `GET /apm/unity/versions` | 支持的 Unity 版本、能力位、Area/Category 清单、默认勾选 |
| `GET /apm/unity/start` | 连接 Player，开始采集；参数 `platform/device/pkgname/process/deep_profiling/areas` |
| `GET /apm/unity/options` | 采集中修改 `deep_profiling` / `areas`（不传的字段保持不变） |
| `GET /apm/unity/snapshot` | 抓取 MemoryProfiler 快照 |
| `GET /apm/unity/status` | 当前模式、版本、字节数、Deep Profiler / Module 状态 |
| `GET /apm/unity/stop` | 停止采集并完成文件 |
| `GET /apm/unity/files?scene=` | 列出场景下的 unity 文件 |
| `GET /apm/unity/download?scene=&file=` | 下载原始文件 |
| `GET /apm/unity/helper` | 下载 Unity 侧脚本 ZIP |

### 7.1 `areas` 参数

支持三种写法：

```text
areas=["CPU","GPU"]     JSON 数组
areas=CPU,GPU           逗号分隔
areas=CPU&areas=GPU     重复参数
```

- 省略 `areas`：保留 Player 默认值，不发送 `set_enabled_categories`；
- 空数组 / 空字符串：发送空集合，关闭全部 Category；
- 非法名称：返回错误，不会静默忽略。

### 7.2 `deep_profiling` 参数

接受 `1/0`、`true/false`、`on/off`、`yes/no`。

### 7.3 `/apm/unity/versions` 返回

```json
{
  "status": 1,
  "versions": [
    {
      "connection_version": 1048832,
      "label": "2022.3",
      "verified": true,
      "deep_profiling": true,
      "enabled_categories": true,
      "areas": [
        {"name": "CPU", "label": "CPU Usage", "category": "Scripts", "default_enabled": true}
      ]
    }
  ],
  "areas": [ ... ],
  "default_areas": ["CPU", "GPU", "..."],
  "default_deep_profiling": false
}
```

### 7.4 `/apm/unity/start` 返回

```json
{
  "status": 1,
  "available": true,
  "mode": "protocol",
  "unity_version": "2022.3",
  "product_name": "巅峰钓鱼_每日构建",
  "deep_profiling": false,
  "areas": ["CPU", "GPU"],
  "enabled_categories": ["Scripts", "GPU"],
  "ignored_options": [],
  "supported_versions": [ ... ]
}
```

### 7.5 `/apm/unity/options` 返回

与 `status` 相同。Helper 模式会返回：

```json
{"status": 0, "msg": "helper backend cannot change deep profiling or profiler modules"}
```

### 7.6 不可用时的返回

```json
{"status": 1, "available": false, "msg": "helper ...; protocol: ..."}
```

---

## 8. 采集产物与 meta.json

```text
report/apm_<时间>/unity/
    profiler.raw              原始 profiler 流 / 二进制日志
    memory_<时间>.snapshot    Memory Profiler 快照
    meta.json                 设备、包名、模式、Unity 版本、Deep Profiler、
                              Profiler Modules、实际发送的 Category、字节数
```

`meta.json` 示例：

```json
{
  "app": "com.QiHuan.FishingGame",
  "device": "R5CX21L23NE",
  "pid": "29278",
  "mode": "protocol",
  "unity_version": "2022.3",
  "product_name": "巅峰钓鱼_每日构建",
  "deep_profiling": true,
  "areas": ["CPU", "GPU"],
  "enabled_categories": ["Scripts", "GPU"],
  "ignored_options": [],
  "profiler_log": "F:\\PerfCat\\report\\unity_20261022_170613\\profiler.raw",
  "started_at": "2026-10-22 17:06:13",
  "stopped_at": "2026-10-22 17:06:20",
  "files": [
    {"name": "profiler.raw", "size": 96245489}
  ]
}
```

---

## 9. 真机实测记录

### 9.1 环境

- 设备：S24+（`R5CX21L23NE`），Android 15
- Unity：2022.3.62f2，IL2CPP
- 工程：`com.QiHuan.FishingGame`
- 采集方式：Protocol

### 9.2 Deep Profiler

| 阶段 | 时间 | 数据量 |
| --- | --- | --- |
| 普通采集（CPU + GPU） | 1 s | 6.9 MB |
| 切到 Deep Profiler | +2 s | +81.3 MB（约 40 MB/s） |

结论：

- `set_deep_profiler` 被 Player 接受；
- 开启后数据量立即大幅上升；
- 该构建已开启 Deep Profiling Support。

### 9.3 Module 子集

```text
areas = ["CPU", "GPU"]
enabled_categories = ["Scripts", "GPU"]
```

- 开始采集后 `status.enabled_categories` 与选择一致；
- 采集过程中调用 `/apm/unity/options` 切换 Deep Profiler，状态立即更新；
- 原始文件首字节为 `50 44 33 55`（`PD3U`）。

### 9.4 完整会话

```text
start(areas=[CPU, GPU], deep=False)
  4 s → 36.2 MB
apply_options(deep=True)
  2 s → 累计 78.4 MB
stop
  最终 profiler.raw = 96.2 MB
meta.json 正确记录 deep_profiling / areas / enabled_categories
```

### 9.5 数据量参考

- 普通采集：约 5–10 MB/s，取决于场景、Module 和加载阶段；
- 开流后的第一段数据可能包含 Player 的初始缓冲，速率会偏高；
- Deep Profiler：约 20–40 MB/s，几分钟即可达到数 GB；
- MemoryProfiler 快照：测试工程约 160 MB（历史测试）。

---

## 10. 限制与排错

### 10.1 已知限制

| 限制 | 说明 |
| --- | --- |
| 仅 Android | 当前协议/helper 只支持 Android |
| Deep Profiler 需要构建支持 | 未开启 Deep Profiling Support 时消息无效 |
| Helper 模式不支持 Deep / Modules | 没有 PlayerConnection 通道 |
| 原始文件不解析 | 需要 Unity Profiler / Memory Profiler 打开 |
| Category 过滤范围 | 不同 Unity 版本对 marker 的过滤行为不同，不能只看 raw 大小 |
| 快照依赖包 | 需要 `com.unity.memoryprofiler` |
| 数据量 | Deep Profiler 会产生非常大的文件，注意磁盘 |

### 10.2 常见问题

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| `helper unavailable ... protocol: cannot connect` | 非 Development Build / 端口未开 / 游戏未启动 | 使用 Development Build，启动游戏，检查 55000+ |
| `no PlayerInfo received` / handshake timeout | Player 还在加载，或 PlayerConnection 监听队列被上一次大流量连接占满 | 等待几秒；必要时 `adb shell am force-stop <pkg>` 后重启游戏 |
| `Unsupported Unity version` | `PROFILES` 没有该 `[Version]` | 按第 11 节新增 profile |
| Deep 开关无效果 | 构建未开启 Deep Profiling Support | 重新构建 Player |
| Module 勾选后 raw 大小没变 | marker 流与 Category/Counter 采集是两套语义 | 用 Unity Profiler 打开确认 Module 状态 |
| 快照失败 | 包里没有 MemoryProfiler / Player 未连接 | 检查包与连接状态 |
| 页面没有新入口 | 服务还是旧进程 | 重启 `python -m perfcat` |

### 10.3 大流量后的连接问题

实测在短时间抓取约 95 MB 后，Player 的 PlayerConnection 监听队列可能出现
`CLOSE_WAIT`，新的握手会超时。处理方式：

```bash
adb shell am force-stop com.QiHuan.FishingGame
# 重新启动游戏，等待 PlayerConnection 重新监听
```

采集大流量场景时建议：

- 不要频繁 start/stop；
- 停止后等待 1–2 秒再关闭连接；
- 需要重新连接时先确认游戏进程和 55000+ 端口状态。

---

## 11. 适配新的 Unity 版本

### 11.1 判断协议版本

先正常启动游戏，连接 PlayerConnection 抓握手：

```python
from perfcat.unity.protocol import read_frame
# 连上 adb forward 后的本地端口，读取前两帧
# 第二帧是 ASCII，形如：
# [Port] 55000 [Version] 1048832 [Id] AndroidPlayer(...) ...
```

`[Version]` 就是 `PlayerConnection` 协议版本号，也是 `PROFILES` 的 key。

### 11.2 从 libunity.so 提取消息 GUID

需要目标版本的 `lib/arm64-v8a/libunity.so`（或对应 ABI）。Unity 的 Profiler /
MemorySnapshot 消息表在静态初始化时由 `UnityGUID::Generate(string_ref)` 生成，
输入是 32 位十六进制字符串。

定位方法：

1. 先确认 `.so` 是否带符号表：

   ```bash
   readelf -s libunity.so | grep ProfilerConnection
   ```

   2022.3 的符号可见，例如：

   ```text
   HandleSetDeepProfilerModeMessage
   HandleSetEnabledCategoriesMessage
   EnableProfilerMessage
   kProfilerSetDeepProfilerModeMessage
   kProfilerSetEnabledCategoriesMessage
   ```

2. 在静态初始化函数中找 `UnityGUID::Generate` 调用链，形如：

   ```asm
   adrp x8, #0x2cc5000
   add  x8, x8, #0x2f0        ; 目标 GUID 槽位
   adrp x9, #0x36a000
   add  x9, x9, #0x66d        ; 32 字符十六进制字符串
   stp  x0, x1, [x8]          ; 上一次 Generate 的结果
   mov  x0, sp
   stp  x9, x22, [sp]
   bl   UnityGUID::Generate
   ```

3. 把字符串按 8 字符一组、以 little-endian uint32 打包，即得到线上 GUID
   （可直接调用 `perfcat.unity.protocol.guid_from_hex` 验证）。
4. 用同样的方法处理 `MemorySnapshotManager` 的 `kMessageSnapshotRequest`、
   `kMessageSnapshotDataBegin`、`Chunk`、`End`、`Terminate`。

> 注意：`.bss` 里的符号在文件里是空的，真正的 GUID 来自静态初始化时
> `UnityGUID::Generate` 解析的 32 位字符串。不要直接读 `.bss`。

### 11.3 确认控制消息的 handler

如果符号表可用，直接按符号名定位：

```text
ProfilerConnection::HandleSetDeepProfilerModeMessage
ProfilerConnection::HandleSetEnabledCategoriesMessage
ProfilerConnection::EnableProfilerMessage
```

如果符号被裁剪，可以通过 `kProfiler*Message` 全局变量的交叉引用定位静态初始化，
再找对应的 handler 注册。

### 11.4 确认 Category 名称

不同版本的 Category 名称可能不同。最可靠的方法是抓一段 profiler 流，解析流头
中的 Category 名称表（UTF-8 名称 + 长度前缀），例如 2022.3 的：

```text
Render, Scripts, Gui, Physics, ..., Virtual Texturing, GPU, Physics2D,
Network Operations, UI Details
```

把新版本的名称补进 `PROFILER_AREAS`，不要直接复用旧版本的名称。

### 11.5 新增 profile

在 `profiles.py` 中增加一个 `UnityProfile`：

```python
UNITY_2023_1 = UnityProfile(
    connection_version=1050000,          # 握手里的 [Version]
    label="2023.1",
    verified=True,                        # 真机验证后才置 True
    features=frozenset({
        FEATURE_PROFILER_STREAM,
        FEATURE_MEMORY_SNAPSHOT,
        FEATURE_DEEP_PROFILING,
        FEATURE_ENABLED_CATEGORIES,
        # 只填实际提取到的能力
    }),
    profiler={...},                        # 见 2022.3 的键名
    snapshot={...},
    notes="Verified on 2023.1.x, Android IL2CPP.",
    areas=PROFILER_AREAS,                  # 如名称不同则单独定义
)

PROFILES[UNITY_2023_1.connection_version] = UNITY_2023_1
```

键名保持与 2022.3 一致：

```text
profiler: player_info, data, enable_capture, set_deep_profiler,
          set_memory_record_mode, set_audio_capture_flags,
          object_memory_profile_snapshot, object_memory_profile_data,
          query_instrumentable_functions, query_function_callees,
          functions_data, set_marker_filtering, set_enabled_categories
snapshot: request, begin, chunk, end, terminate
```

缺失的消息不要编造：能力位不加入，`protocol.py` 会在调用时返回明确错误。

### 11.6 验证清单

```text
[ ] GET /apm/unity/versions 能看到新版本，verified 正确
[ ] Start 后 status.bytes 持续增长
[ ] Stop 后 unity/profiler.raw 大小正常，首字节是 PD3U
[ ] CAPTURE 后 unity/memory_*.snapshot 大小正常，首字节 01 ED BC FA
[ ] 生成报告后分析页能下载 unity/ 下的文件
[ ] 游戏进程在采集/快照后仍然存活
[ ] Deep Profiler 开关发送后流仍在增长（构建需开启 Deep Profiling Support）
[ ] 只勾选部分 Module 后，status.enabled_categories 与选择一致，流仍在增长
[ ] 从流头解析出的 Category 名称与 PROFILER_AREAS 一致
[ ] 至少在一台真机上验证，再置 verified=True
```

---

## 12. 扩展新的 Profiler 功能

目前 2022.3 的消息表里已经包含以下能力：

| 能力位 | 消息 | 状态 |
| --- | --- | --- |
| `profiler_stream` | `enable_capture` + `data` | 已实现 |
| `memory_snapshot` | `snapshot.*` | 已实现 |
| `deep_profiling` | `set_deep_profiler` | 已实现（UI 开关 + HTTP 参数） |
| `enabled_categories` | `set_enabled_categories` | 已实现（Module 勾选 + 热更新） |
| `memory_record_mode` | `set_memory_record_mode` | 消息已登记，待接线 |
| `marker_filtering` | `set_marker_filtering` | 消息已登记，待接线 |
| `functions_data` | `functions_data` | 消息已登记，待接线 |
| `object_memory_profile` | `object_memory_profile_*` | 消息已登记，待接线 |
| `instrumentable_functions` / `function_callees` | `query_*` | 消息已登记，待接线 |
| `audio_capture_flags` | `set_audio_capture_flags` | 消息已登记，待接线 |

### 12.1 扩展步骤

1. 在 `profiles.py` 确认能力位与消息存在。
2. 在 `UnityProtocolSession` 增加一个方法，用 `self.profile.message(group, name)`
   取 GUID，`build_packet()` 发消息。
3. 在 `ProtocolBackend` / `UnityCapture` 增加对外的动作。
4. 在 `perfcat/view/unity_api.py` 增加 HTTP 路由。
5. 在 `index.html` / `analysis.html` 增加入口。
6. 在 profile 的 `features` 中声明能力，调用方先做能力检查。

### 12.2 示例：新增一个 uint32 参数的消息

```python
def set_memory_record_mode(self, mode):
    if not self.profile.supports(FEATURE_MEMORY_RECORD_MODE):
        raise UnityProtocolError("memory record mode is not supported")
    self._send(build_packet(
        self.profile.message("profiler", "set_memory_record_mode"),
        struct.pack("<I", int(mode)),
    ))
```

### 12.3 示例：验证新消息

```text
1. 发送消息前抓 2 秒流，记录稳态字节速率
2. 发送消息后再抓 2 秒流
3. 对比速率 / 文件头 / Unity Profiler 打开后的状态
4. 确认游戏进程存活，没有断流
```

Deep Profiler 和 Category 都是按这个流程验证的。

---

## 13. 参考实现位置

| 内容 | 文件 |
| --- | --- |
| 版本注册表 / Area 映射 | `perfcat/unity/profiles.py` |
| 帧格式 / GUID 编码 / 控制消息 | `perfcat/unity/protocol.py` |
| helper / protocol 后端 | `perfcat/unity/backends.py` |
| 会话、选项、meta、归档 | `perfcat/unity/manager.py` |
| HTTP 接口 | `perfcat/view/unity_api.py` |
| Unity 脚本 | `perfcat/assets/unity/PerfCatCapture.cs` |
| 实时页入口 | `perfcat/templates/index.html` |
| 分析页下载 | `perfcat/templates/analysis.html` |
| 兼容层 shim | `perfcat/service/unity_capture.py` / `unity_protocol.py` |

---

## 14. 附录

### A. Unity 2022.3 消息 GUID

Profiler：

| 名称 | GUID |
| --- | --- |
| `player_info` | `94d3ae57ca4b4ed98408b827f180853d` |
| `data` | `c58d77184f4b4b59b3fffc6f800ae10e` |
| `enable_capture` | `2257466d0e0e47da89826cf04e68135c` |
| `set_deep_profiler` | `bf0c550cd24d498cbb28380a8467622d` |
| `set_memory_record_mode` | `c48d097f8fea463494b8f08b0b55d05a` |
| `set_audio_capture_flags` | `1e792ecb5c9f4a8381d0d03528b6ae7b` |
| `object_memory_profile_snapshot` | `14473694eb0a4963870aaab63efb7507` |
| `object_memory_profile_data` | `8584ee18ea264718873cd92b109a0761` |
| `query_instrumentable_functions` | `302b3998e168478eb8713b086c7693a9` |
| `query_function_callees` | `d8f38a5539cc4b608792c273efe6a969` |
| `functions_data` | `e2acb618e8c8465a901eb7b6f667cc41` |
| `set_marker_filtering` | `18207525e148469ea059ec2cdfb026a5` |
| `set_enabled_categories` | `77a4016e6b86420b8ede43de4e66a3f5` |

Memory snapshot：

| 名称 | GUID |
| --- | --- |
| `request` | `4b9386de901a4ae2bb70084edca4c7f4` |
| `begin` | `c626f85c1d3492369e59250f015f8129` |
| `chunk` | `edcadf462a69df81547ff4f30a52530c` |
| `end` | `80491b35c7645ea780edbe7c1d834138` |
| `terminate` | `249d2ef8bbc643e1808e45b25a5418b8` |

### B. 2022.3 反汇编要点

以下地址来自 `libunity.so`（2022.3.62f2，arm64），仅用于技术参考；不同版本/Hash
会变化。

| 符号 | 地址 | 说明 |
| --- | --- | --- |
| `HandleSetDeepProfilerModeMessage` | `0x1128b44` | 读取 payload 首 uint32 作为 bool |
| `HandleSetEnabledCategoriesMessage` | `0x1128e30` | 解析 NUL 分隔名称并整体替换 Category |
| `EnableProfilerMessage` | `0x1128ad0` | 读取 payload 首 uint32 启停采集 |
| `profiler_set_category_enable` | `0xeabeb4` | 设置单个 Category 的启用位 |
| `ProfilerManager::GetAllCategories` | `0xea711c` | 获取所有已注册 Category |
| `Profiler::SetCategoryEnabled` | `0x1126dac` | 修改 Category flags |
| `profiler_is_category_enabled` | `0xeabb10` | 读取 Category 启用位 |
| `Profiler::StartNewFrame` | `0x1126aac` | 每帧检查 Category 状态 |

Deep Profiler handler 的核心逻辑：

```asm
ldr x8, [x0, #0x10]    ; payload
ldr w20, [x8]          ; enabled
cmp w20, #0
cset w1, ne
bl  SetDeepProfilerEnabled
```

Category handler 的核心逻辑：

```text
payload size = [x19 + 0x18]
payload ptr  = [x19 + 0x10]
逐字节扫描，遇到 \0 就生成一个 string_ref 并插入 hash_set
GetAllCategories() → 对每个 Category 执行 hash_set::find
命中 → profiler_set_category_enable(id, true)
未命中 → profiler_set_category_enable(id, false)
```

GUID 全局变量（`.bss`，运行时由静态初始化生成）：

```text
kProfilerSetDeepProfilerModeMessage    0x2cc52f0
kProfilerSetEnabledCategoriesMessage   0x2cc5380
kProfilerEnableCaptureMessage          0x2cc52e0
kProfilerDataMessage                   0x2cc52d0
```

### C. 术语

| 术语 | 说明 |
| --- | --- |
| PlayerConnection | Unity Player 与 Editor/工具之间的 TCP 协议 |
| ProfilerArea | Unity 编辑器侧 Profiler Module 的枚举 |
| ProfilerCategory | Player 侧实际用于采集/计数的 Category |
| Deep Profiler | 方法级采样模式，需要构建支持 |
| helper | 游戏内集成的 C# 采集脚本 |
| protocol | 直接模拟 Unity Editor 的 PlayerConnection 协议 |
