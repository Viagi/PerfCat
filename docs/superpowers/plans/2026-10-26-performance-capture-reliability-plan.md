# 性能采集可靠性与分析能力全面重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将性能牛升级为数据状态明确、时间可对齐、Unity 可逐帧分析、原始数据可复核的性能采集与报告系统。

**Architecture:** 先建立独立的统一数据模型、命令结果和时间/质量基础设施，再以兼容边界替换系统 collector、Unity Protocol/Helper、raw 分析和报告导出。采集层输出结构化样本，报告层只消费结构化样本；展示层负责格式化单位。Unity 实时采样继续低频运行，完整统计从 `profiler.raw` 离线扫描生成。

**Tech Stack:** Python 3、pytest、现有 Android ADB/iOS 采集器、Unity C# Helper、PlayerConnection/PD3U raw parser、现有 HTML/Excel/ZIP 导出。

## Global Constraints

- 允许破坏性升级数据模型；不得把采集失败表示为 `value=0,status=ok`。
- 当前 `perfcattle/templates/analysis.html` 和 `perfcattle/templates/report.html` 的未提交改动必须保留并兼容。
- 所有采样间隔、TTL 和持续时间使用 `time.monotonic()`；epoch 时间仅用于跨进程对齐和展示。
- 采样状态必须区分 `ok`、`cached`、`unsupported`、`permission_denied`、`timeout`、`disconnected`、`invalid`、`truncated`、`missing`。
- Unity raw 截断、协议断流或解析质量不足必须进入 `meta.json`、报告和导出 manifest。
- 实时 Unity 采样仍可使用 0.5～1 秒低频值；全量统计不得依赖导出帧数上限。
- 每个任务先写失败测试，再写最小实现，再运行相关测试并提交独立 commit。
- 不引入新的运行时第三方依赖；使用标准库和仓库已有依赖。

---

## Task 1: 建立统一采样模型与质量元数据

**Files:**
- Create: `perfcattle/core/metrics.py`
- Create: `tests/test_metric_model.py`
- Modify: `perfcattle/collector/base.py`
- Modify: `perfcattle/report/log_io.py`

**Interfaces:**
- `MetricStatus`: `str` enum，包含 Global Constraints 中的 9 个状态。
- `MetricSample`: dataclass，字段为 `metric`, `value`, `unit`, `status`, `source`, `scope`, `aggregation`, `sample_epoch_ms`, `sample_monotonic_ns`, `collected_epoch_ms`, `latency_ms`, `cached`, `stale_age_ms`, `error`。
- `MetricQuality`: dataclass，字段为 `total`, `valid`, `invalid`, `cached`, `missing`, `first_epoch_ms`, `last_epoch_ms`。
- `MetricSample.to_dict() -> dict` 和 `MetricSample.from_dict(dict) -> MetricSample`。
- `finite_number(value) -> bool`：只接受有限的 int/float，不接受 bool、NaN、Infinity。
- `parse_metric_line(line: str) -> MetricSample | None`：解析统一 JSON 行格式，坏行返回 None。

- [ ] **Step 1: 写失败测试。**

```python
# tests/test_metric_model.py
import math
from perfcattle.core.metrics import MetricSample, MetricStatus, finite_number, parse_metric_line


def test_zero_is_a_valid_sample():
    sample = MetricSample.ok(
        metric="gpu_util", value=0, unit="percent", source="test",
        sample_epoch_ms=1, sample_monotonic_ns=2,
    )
    assert sample.status is MetricStatus.OK
    assert sample.value == 0


def test_failed_sample_cannot_carry_a_value():
    sample = MetricSample(
        metric="cpu", value=0, unit="percent", status=MetricStatus.TIMEOUT,
        source="adb", sample_epoch_ms=1, sample_monotonic_ns=2,
    )
    assert sample.value is None


def test_finite_number_rejects_non_finite_and_bool():
    assert finite_number(1.0)
    assert not finite_number(True)
    assert not finite_number(math.nan)
    assert not finite_number(math.inf)


def test_bad_json_line_returns_no_sample():
    assert parse_metric_line('{"metric":"cpu","value":"broken"}') is None
```

- [ ] **Step 2: 运行失败测试。**

Run: `pytest -q tests/test_metric_model.py`
Expected: FAIL because `perfcattle.core.metrics` does not exist.

- [ ] **Step 3: 实现模型。**

实现 `MetricStatus`、`MetricSample` 和 `MetricQuality`。`MetricSample.__post_init__` 在状态不是 `OK/CACHED` 时强制 `value=None`；`MetricSample.ok()` 填充当前 epoch/monotonic 时间；`to_dict()` 始终输出状态和质量字段；`from_dict()` 校验状态和 finite 数值。

- [ ] **Step 4: 让日志解析使用模型并保留质量统计。**

在 `log_io.py` 增加统一的坏行计数、NaN/Infinity 拒绝和 `MetricQuality` 返回值；保留现有调用方需要的点集适配函数，但不再让单条坏行抛出整个报告接口。

- [ ] **Step 5: 运行测试并提交。**

Run: `pytest -q tests/test_metric_model.py tests/test_report_log_io.py`
Expected: PASS。

```bash
git add perfcattle/core/metrics.py perfcattle/collector/base.py perfcattle/report/log_io.py tests/test_metric_model.py
git commit -m "feat: add structured metric samples and quality states"
```

---

## Task 2: 重构 ADB 命令结果、缓存和 monotonic 采样

**Files:**
- Create: `perfcattle/core/command_result.py`
- Create: `tests/test_adb_command_result.py`
- Modify: `perfcattle/core/adb_client.py:78-145`
- Modify: `perfcattle/collector/base.py:58-312`
- Modify: `tests/test_core_adb_client.py`
- Modify: `tests/test_collection_stability.py`

**Interfaces:**
- `CommandResult`: dataclass，字段 `ok`, `stdout`, `stderr`, `returncode`, `timed_out`, `duration_ms`, `error_kind`。
- `ADB.shell_result(cmd, deviceId, timeout=30) -> CommandResult`。
- 旧 `ADB.shell()` 删除或改为显式返回 `CommandResult`，所有 collector 使用新接口。
- `CollectorSampler.sample()` 使用 monotonic 时间并返回 `MetricSample`。

- [ ] **Step 1: 添加失败测试。**

覆盖非零退出、timeout、异常和成功输出：

```python
def test_shell_result_distinguishes_timeout(monkeypatch):
    result = adb.shell_result("cat /proc/stat", "device", timeout=1)
    assert result.ok is False
    assert result.timed_out is True
    assert result.error_kind == "timeout"
```

同时增加 stale cache 测试：首次成功、连续失败、超过 TTL 后返回 `missing`。

- [ ] **Step 2: 运行测试确认失败。**

Run: `pytest -q tests/test_adb_command_result.py tests/test_core_adb_client.py`
Expected: FAIL because `shell_result` 和结构化 sampler 尚不存在。

- [ ] **Step 3: 实现 `CommandResult` 和 ADB 适配。**

在 `adb_client.py` 保留底层 subprocess 行为，但每个异常映射到明确 `error_kind`；记录命令耗时；非零返回码统一 `ok=False`。所有调用方不再通过空字符串猜测失败。

- [ ] **Step 4: 重构 `CollectorSampler`。**

使用 `time.monotonic()` 控制采样和 stale TTL；保存最后成功样本、连续失败次数、失败原因和 stale age；缓存过期返回 `MetricSample(status=missing/error)`，缓存有效才返回 `cached`。

- [ ] **Step 5: 修正线程池测试契约。**

`collectAll()` 测试按实际 collector 列表或 `len(collectors)` 断言，不再硬编码 9/10；确保 recording 仍在父进程/主 session 中完成。

- [ ] **Step 6: 运行测试并提交。**

Run: `pytest -q tests/test_adb_command_result.py tests/test_core_adb_client.py tests/test_collection_stability.py tests/test_recording_stability.py`
Expected: PASS。

```bash
git add perfcattle/core/command_result.py perfcattle/core/adb_client.py perfcattle/collector/base.py tests/test_adb_command_result.py tests/test_core_adb_client.py tests/test_collection_stability.py
git commit -m "feat: make adb failures explicit and sampler clocks monotonic"
```

---

## Task 3: 修正系统 collector 的指标语义与异常边界

**Files:**
- Create: `tests/test_collector_quality.py`
- Modify: `perfcattle/collector/cpu.py`
- Modify: `perfcattle/collector/memory.py`
- Modify: `perfcattle/collector/gpu.py`
- Modify: `perfcattle/collector/gpu_stats.py`
- Modify: `perfcattle/collector/network.py`
- Modify: `perfcattle/collector/disk.py`
- Modify: `perfcattle/collector/file_io.py`
- Modify: `perfcattle/collector/fps.py`
- Modify: `perfcattle/collector/thermal.py`
- Modify: `tests/test_collector_adb_metrics.py`

**Interfaces:**
- 每个公开 collector 方法返回 `MetricSample` 或 `dict[str, MetricSample]`，不再用 0/空字典表达错误。
- GPU 计数器采样器新增 `previous_busy`, `previous_total` 和 `counter_semantics`。
- 网络结果新增 `scope`，仅在底层支持时返回 `wifi`/`mobile`。
- 磁盘结果新增 `mount_points` 和 `snapshot_only`。

- [ ] **Step 1: 为错误路径写测试。**

测试：ADB 权限失败不产生有效 0；GPU 累计计数器按 delta 计算；计数器回绕返回 invalid；UID 统计不伪装 transport；`df` fallback 排除 overlay/tmpfs；CPU 结果拒绝负值和大于 100%；`/proc/<pid>/stat` 支持进程名含空格/右括号。

- [ ] **Step 2: 运行测试确认失败。**

Run: `pytest -q tests/test_collector_quality.py tests/test_collector_adb_metrics.py`
Expected: FAIL on current fallback、GPU、network、disk 和 CPU 边界行为。

- [ ] **Step 3: 修正 CPU 和进程 stat。**

从首个 `(` 与最后一个 `)` 定位 comm；对剩余字段解析；校验所有 delta 非负、`process_delta <= total_delta`，最终百分比限定在 `[0, 100]`；异常返回 `invalid`。

- [ ] **Step 4: 修正 GPU。**

为每个 GPU source 声明 `window_ratio` 或 `cumulative_delta`；累计计数器使用相邻采样 delta；检测回绕/重置；结果携带 source 和 semantics。

- [ ] **Step 5: 修正 network、disk、file IO 和其他 collector。**

网络 UID 总流量返回 `scope=uid_total`；磁盘保留目标 mount point，过滤重复/overlay/tmpfs；jank/FPS/thermal/file IO 统一错误状态和单位。

- [ ] **Step 6: 将 disk 纳入正式采集契约。**

明确容量快照与持续指标的区别；若进入 `collectAll()`，使用统一样本时间并在报告 metadata 标记 `snapshot_only`，避免与持续曲线混淆。

- [ ] **Step 7: 运行测试并提交。**

Run: `pytest -q tests/test_collector_quality.py tests/test_collector_adb_metrics.py tests/test_collection_stability.py`
Expected: PASS。

```bash
git add perfcattle/collector tests/test_collector_quality.py tests/test_collector_adb_metrics.py tests/test_collection_stability.py
git commit -m "fix: make system collector semantics and failures explicit"
```

---

## Task 4: 完善系统日志写入、时间轴和质量聚合

**Files:**
- Create: `perfcattle/report/metric_schema.py`
- Create: `tests/test_report_quality.py`
- Modify: `perfcattle/report/log_io.py`
- Modify: `perfcattle/report/aggregator.py`
- Modify: `perfcattle/service/monitor.py`
- Modify: `tests/test_report_timeline.py`
- Modify: `tests/test_report_aggregator.py`

**Interfaces:**
- `MetricDefinition`: 字段 `unit`, `allowed_statuses`, `aggregation`。
- `read_metric_series(path, definition) -> (list[MetricSample], MetricQuality)`。
- `aggregate_metric(samples, aggregation) -> MetricSample`。
- `session_time_metadata()` 返回 requested/started/first_sample/stopped/last_sample 的 epoch 与 monotonic 字段。

- [ ] **Step 1: 添加坏行、方向和时间测试。**

覆盖坏数值、NaN/Infinity、单向网络、jank 无 FPS、跨午夜、时钟回拨、采样延迟和 bandwidth/FPS 最大时间差。

- [ ] **Step 2: 运行测试确认失败。**

Run: `pytest -q tests/test_report_quality.py tests/test_report_timeline.py tests/test_report_aggregator.py`
Expected: FAIL because当前解析/聚合会抛错、错误置零或不记录质量。

- [ ] **Step 3: 实现 schema 和系列读取。**

为每类指标声明单位和聚合方式；单行坏数据跳过并增加 invalid；只聚合 finite 且 status 有效样本；将 `frame`、source、quality 元数据与数值分开。

- [ ] **Step 4: 修正 aggregator。**

jank 独立于 FPS；network send/recv 独立 flag；GPU bandwidth 仅在 FPS 时间差不超过一个采样窗口时转换；CPU/FPS 使用数值字段，展示字符串延后生成；保存 sampled/full 语义。

- [ ] **Step 5: 统一 session 时间原点。**

在 `monitor.py` 保存 requested、actual start、first sample、stop requested、last sample、actual stopped 的双时钟信息；图表使用 session origin 和样本自身时间，不使用写文件时间覆盖采样时间。

- [ ] **Step 6: 运行测试并提交。**

Run: `pytest -q tests/test_report_quality.py tests/test_report_timeline.py tests/test_report_aggregator.py tests/test_report_log_io.py`
Expected: PASS。

```bash
git add perfcattle/report/metric_schema.py perfcattle/report/log_io.py perfcattle/report/aggregator.py perfcattle/service/monitor.py tests/test_report_quality.py tests/test_report_timeline.py tests/test_report_aggregator.py
git commit -m "feat: add quality-aware report aggregation and timelines"
```

---

## Task 5: 修复 Unity Protocol、流状态和 Helper 控制通道

**Files:**
- Create: `tests/test_unity_capture_integrity.py`
- Modify: `perfcattle/unity/protocol.py:84-104`
- Modify: `perfcattle/unity/backends.py`
- Modify: `perfcattle/unity/manager.py`
- Modify: `perfcattle/assets/unity/PerfCattleCapture.cs`
- Modify: `perfcattle/assets/unity/PerfCattleCaptureRunner.cs`
- Modify: `tests/test_unity_protocol.py`
- Modify: `tests/test_unity_session.py`
- Modify: `tests/test_unity_auto_connect.py`

**Interfaces:**
- `read_frame()`：短 payload 抛 `UnityProtocolError("frame_payload_truncated")`。
- `UnityProtocolSession.status() -> dict`：至少包含 `stream_state`, `stream_error`, `last_complete_frame`, `raw_complete`。
- `UnityCapture.meta()`：包含 requested/started/stopped/flush 和完整性字段。
- Helper JSON protocol：带 `protocol_version`, `request_id`, `command`, `status`, `error`。

- [ ] **Step 1: 添加截断和状态失败测试。**

构造 header 声明长度大于 socket 实际 payload；断流后断言 `truncated`；stop 前后断言 reader 已退出、文件完整性已确认；snapshot 不阻塞 ping/stats 的模拟测试。

- [ ] **Step 2: 运行测试确认失败。**

Run: `pytest -q tests/test_unity_capture_integrity.py tests/test_unity_protocol.py tests/test_unity_session.py`
Expected: FAIL because当前 `read_frame()` 返回短帧且 stop 状态不完整。

- [ ] **Step 3: 修正 Python Protocol。**

`read_frame()` payload 未读满即抛异常；reader 保存错误与最后完整帧；stop 等待线程退出和文件稳定；在 meta/status 中明确 `error/truncated/complete`。

- [ ] **Step 4: 修正 Helper 命令线程和主线程控制。**

C# `start`、`stop`、`RotateLog()` 统一通过 `PerfCattleCaptureRunner.Enqueue()` 执行；AcceptLoop 为每个 client 创建独立处理任务；snapshot 返回 request ID 并异步完成；增加状态互斥和 recorder DisposeAll；使用 JSON serializer/完整转义。

- [ ] **Step 5: 更新 Python Helper 后端。**

解析带版本号 JSON；snapshot 使用 request ID；停止时轮询文件大小连续稳定并校验 raw，而不是只使用固定 sleep；错误显式返回。

- [ ] **Step 6: 运行测试并提交。**

Run: `pytest -q tests/test_unity_capture_integrity.py tests/test_unity_protocol.py tests/test_unity_session.py tests/test_unity_auto_connect.py`
Expected: PASS。

```bash
git add perfcattle/unity perfcattle/assets/unity tests/test_unity_capture_integrity.py tests/test_unity_protocol.py tests/test_unity_session.py tests/test_unity_auto_connect.py
git commit -m "fix: make unity stream lifecycle and truncation explicit"
```

---

## Task 6: 增加 Unity raw 质量统计和全量逐帧摘要

**Files:**
- Create: `perfcattle/unity/quality.py`
- Create: `tests/test_unity_quality.py`
- Modify: `perfcattle/unity/raw_reader.py`
- Modify: `perfcattle/unity/hotspots.py`
- Modify: `perfcattle/unity/export.py`
- Modify: `perfcattle/unity/manager.py`
- Modify: `tests/test_unity_raw_reader.py`
- Modify: `tests/test_unity_hotspots.py`
- Modify: `tests/test_unity_export.py`

**Interfaces:**
- `RawQuality`: `complete`, `bad_blocks`, `partial_blocks`, `lost_bytes`, `frame_gaps`, `unmatched_samples`, `warnings`, `quality`。
- `RawStatsParser.snapshot() -> dict`：增加 per-frame identity 和 quality。
- `HotspotReader.scan_all() -> dict`：返回全量 frames、quality 和 full summary。
- `build_frame_summary(frames) -> dict`：返回 avg/max/p95/p99、slow frame、GC frame、frame gap。
- `export_profiler_json(..., max_frames)`：页面 frames 可抽样，但 `summary` 必须来自全量扫描。

- [ ] **Step 1: 添加失败测试。**

覆盖损坏 block、footer 错误、frame gap、未匹配 Begin/End、30/90/120 FPS 的 frame duration、超过 max_frames 时 summary 仍使用全量 frame、marker 按 path 聚合。

- [ ] **Step 2: 运行测试确认失败。**

Run: `pytest -q tests/test_unity_quality.py tests/test_unity_raw_reader.py tests/test_unity_hotspots.py tests/test_unity_export.py`
Expected: FAIL because当前损坏 raw 只 break、summary 依赖抽样、marker 只按 name 聚合。

- [ ] **Step 3: 扩展 raw parser 质量。**

记录完整/不完整状态、错误 offset、丢失字节、frame gap、最后完整 frame、识别线程和 missing counter；未知未来版本标记 unsupported/experimental；按 frame 保存 counter identity，避免 `_latest` 混合不同帧。

- [ ] **Step 4: 扩展 HotspotReader。**

遇到 block/footer 错误记录 warning 和最后有效 offset；在可恢复时尝试 resync，无法恢复时返回 `complete=false`；真实 frame 时间优先，不能可靠推断时设置 `estimated=true`，不使用固定 16.666ms 伪装精确值。

- [ ] **Step 5: 实现全量摘要和路径聚合。**

全量扫描计算 avg/max/p95/p99、慢帧、GC、frame gap 和质量；导出页面数据才按 max_frames 抽样；`markers_by_path` 为默认聚合，`markers_by_name` 单独提供；树重建标记 `tree_reconstructed`。

- [ ] **Step 6: 运行测试并提交。**

Run: `pytest -q tests/test_unity_quality.py tests/test_unity_raw_reader.py tests/test_unity_hotspots.py tests/test_unity_export.py tests/test_unity_report.py`
Expected: PASS。

```bash
git add perfcattle/unity/raw_reader.py perfcattle/unity/hotspots.py perfcattle/unity/export.py perfcattle/unity/quality.py perfcattle/unity/manager.py tests/test_unity_quality.py tests/test_unity_raw_reader.py tests/test_unity_hotspots.py tests/test_unity_export.py
git commit -m "feat: add full-frame unity summaries and raw quality"
```

---

## Task 7: 重构报告导出和 manifest

**Files:**
- Create: `perfcattle/report/manifest.py`
- Create: `tests/test_report_manifest.py`
- Modify: `perfcattle/report/package_export.py`
- Modify: `perfcattle/report/excel_export.py`
- Modify: `perfcattle/report/html_export.py`
- Modify: `perfcattle/view/report_api.py`
- Modify: `perfcattle/templates/analysis.html`
- Modify: `perfcattle/templates/report.html`
- Modify: `tests/test_compare_export.py`
- Modify: `tests/test_compare_multi.py`

**Interfaces:**
- `build_manifest(report_path) -> dict`：列出所有原始文件、大小、指标、单位、样本数、时间范围、质量和来源。
- `ReportPackageExporter.collect()`：包含 manifest、session、所有系统日志、结构化 JSON、Unity stats/raw/json/meta/quality 和 README。
- `ExcelExporter`：导出结构化数值、单位和 quality；不把 Excel 当作原始数据替代。

- [ ] **Step 1: 添加导出完整性失败测试。**

构造临时报告目录，写入 CPU、memory、FPS、jank、network、GPU、thermal、file IO、`apm_session.json` 和 Unity 文件；断言 ZIP 每类文件均存在，manifest 的大小和 checksum/quality 正确。

- [ ] **Step 2: 运行测试确认失败。**

Run: `pytest -q tests/test_report_manifest.py tests/test_compare_export.py tests/test_compare_multi.py`
Expected: FAIL because当前 ZIP 缺少原始 APM 日志和 session metadata。

- [ ] **Step 3: 实现 manifest。**

扫描报告目录，读取结构化 metadata，生成版本化 manifest；对缺失、坏行、cached、truncated 等状态计数；README 明确原始值、聚合值和抽样值的区别。

- [ ] **Step 4: 扩展 ZIP 和 Excel。**

ZIP 包含全部系统日志、`apm_session.json`、Unity stats/raw/json/meta/quality、manifest 和 README；Excel 增加 Unity stats、GPU、thermal、memory detail、jank 和质量摘要。

- [ ] **Step 5: 保留当前模板改动并接入结构化字段。**

不得恢复或覆盖现有 `analysis.html`、`report.html` 的工作区内容；页面同时支持结构化 `{value, unit, status}` 和展示格式化；数据质量警告放在报告顶部。

- [ ] **Step 6: 运行测试并提交。**

Run: `pytest -q tests/test_report_manifest.py tests/test_compare_export.py tests/test_compare_multi.py tests/test_unity_report.py`
Expected: PASS。

```bash
git add perfcattle/report perfcattle/view/report_api.py perfcattle/templates/analysis.html perfcattle/templates/report.html tests/test_report_manifest.py tests/test_compare_export.py tests/test_compare_multi.py
git commit -m "feat: export complete raw data and quality manifest"
```

---

## Task 8: 全链路回归、真实异常样本和文档

**Files:**
- Create: `tests/fixtures/corrupt_unity_frame.bin`
- Create: `tests/fixtures/invalid_metrics.jsonl`
- Create: `tests/test_capture_end_to_end.py`
- Modify: `docs/UNITY_CAPTURE.md`
- Modify: `README.md`
- Modify: `tests/conftest.py`

**Interfaces:**
- `pytest` 全套测试必须可在无真实 Android/iOS/Unity 设备环境运行。
- `README` 和 `UNITY_CAPTURE.md` 记录 status、单位、source、采样语义、raw 完整性和导出内容。

- [ ] **Step 1: 固化异常 fixture。**

创建包含短 payload、错误 footer、坏 JSON 行、NaN 和非法状态的最小 fixture；fixture 必须只用于测试，不包含真实设备数据。

- [ ] **Step 2: 添加端到端测试。**

模拟一次采集 session：系统样本含 0、cached、missing；Unity raw 含正常帧和截断尾部；生成报告和 ZIP；断言报告保留状态、全量 summary、质量警告，ZIP 可独立读取 manifest 和原始文件。

- [ ] **Step 3: 更新文档。**

记录新的数据模型、状态枚举、时间字段、Unity 实时/逐帧数据区别、Helper 协议、raw 版本策略、导出 ZIP 目录和数据质量含义。

- [ ] **Step 4: 运行全量测试。**

Run: `pytest -q`
Expected: 全部通过；不得以跳过异常测试或将错误转换为 0 来消除失败。

- [ ] **Step 5: 运行静态/语法检查。**

Run: `python -m compileall perfcattle tests`
Expected: 无语法错误。

- [ ] **Step 6: 检查工作区和提交。**

Run: `git diff --check && git status --short`
Expected: 无 whitespace 错误；确认只包含设计要求的源代码、测试、文档和用户原有模板改动。

```bash
git add tests/fixtures tests/test_capture_end_to_end.py tests/conftest.py docs/UNITY_CAPTURE.md README.md
git commit -m "test: verify end-to-end capture quality and exports"
```

---

## 计划自检

- 统一模型：Task 1。
- ADB、缓存、monotonic：Task 2。
- GPU、CPU、网络、磁盘、文件 IO：Task 3。
- 报告质量、jank、网络方向、时间轴：Task 4。
- Unity 截断、断流、flush、Helper：Task 5。
- Unity raw 完整性、逐帧统计、p95/p99、路径聚合：Task 6。
- ZIP、Excel、HTML、manifest：Task 7。
- 异常 fixture、端到端回归、文档和全量测试：Task 8。

所有任务均指定了文件、接口、失败测试、实现边界、验证命令和提交点；计划不要求新的运行时依赖，也不覆盖用户当前未提交模板修改。
