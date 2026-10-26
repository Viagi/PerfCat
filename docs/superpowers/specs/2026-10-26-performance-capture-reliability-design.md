# 性能采集可靠性与分析能力全面重构设计

- 日期：2026-10-26
- 状态：已确认，待实施
- 范围：系统性能采集、Unity 性能采集、报告聚合、导出与测试

## 1. 背景与目标

当前系统已经具备 Android/iOS 系统指标采集、Unity Helper/PlayerConnection 双后端、报告页面和导出能力，但仍存在以下共性问题：采集错误可能被转换为 0 或旧缓存值；采样时间和实际采样时间不一致；Unity 实时数据以低频最新值为主，无法表达短时尖峰；Unity raw 截断和协议断流可能被当成正常完成；报告缺少完整的数据质量和来源语义；导出包缺少完整原始采集链路。

本次重构目标：

1. 任何失败、缺失、缓存、设备不支持和数据截断都能被明确表达，不能伪装为正常数值。
2. 所有指标统一记录数值、单位、来源、范围、聚合语义、时间和质量状态。
3. 采样调度使用 monotonic clock，报告对齐同时保留 epoch 时间。
4. Unity 保持低开销实时监控，并从 raw 生成全量逐帧分析摘要。
5. 报告提供慢帧、GC、p95/p99、丢失数据和解析质量信息。
6. 导出包包含可以独立复核报告的全部原始数据和 manifest。
7. 允许破坏性升级数据模型；当前 `analysis.html`、`report.html` 未提交改动必须保留并兼容。

## 2. 统一数据模型

系统和 Unity 采样结果统一为如下逻辑结构：

```json
{
  "metric": "cpu_app_percent",
  "value": 42.3,
  "unit": "percent",
  "status": "ok",
  "source": "proc_pid_stat",
  "scope": "package",
  "aggregation": "window_delta",
  "sample_epoch_ms": 1730000000123,
  "sample_monotonic_ns": 123456789,
  "collected_epoch_ms": 1730000000240,
  "latency_ms": 117,
  "cached": false,
  "stale_age_ms": 0,
  "error": null
}
```

`status` 使用固定枚举：`ok`、`cached`、`unsupported`、`permission_denied`、`timeout`、`disconnected`、`invalid`、`truncated`、`missing`。

约束：

- `status` 不是 `ok` 或 `cached` 时，`value` 必须为 `null`。
- `cached` 必须包含上次成功时间、连续失败次数和 `stale_age_ms`。
- 数值 0 是合法值，不能作为错误标志。
- 单位、来源、范围和聚合方式使用独立字段，展示字符串由前端生成。
- 原始样本、窗口聚合、累计计数器差值和最近值必须明确区分。

## 3. 系统采集重构

### 3.1 ADB 命令结果

`ADB.shell()` 改为返回结构化命令结果，至少包含 `ok`、`stdout`、`stderr`、`returncode`、`timed_out` 和 `duration_ms`。采集器不得依据空字符串猜测失败类型。

统一处理非零返回码、超时、权限拒绝、设备离线、空输出和 PID 消失，并把具体原因转化为采样状态。

### 3.2 缓存和采样调度

每个 sampler 保存最近成功样本、连续失败次数、首次失败时间、最后失败原因和最大 stale 时间。缓存过期后返回错误或缺失状态，不再无限使用旧值。

所有间隔、TTL 和 duration 判断使用 `time.monotonic()`；每条记录同时保存 monotonic 与 epoch 时间，并记录请求开始、完成和写入延迟。重型指标使用独立频率和超时，采集器并发和实际任务数不再通过测试硬编码。

### 3.3 指标语义修正

- GPU 根据设备节点语义选择窗口比例或累计 delta，输出来源和语义。
- Wi-Fi/mobile 只有在底层支持 transport 区分时才分别输出；否则标记 `uid_total`。
- 磁盘明确为容量快照或持续 IO；过滤 overlay/tmpfs 和重复挂载点。
- CPU 校验所有 delta 和最终范围，拒绝负值、越界和计数器重置后的异常样本。
- `/proc/<pid>/stat` 通过括号边界解析进程名，支持空格和右括号。
- jank、FPS、网络发送和接收分别维护有效性状态。
- 日志写入采用会话级写入器或队列，减少每条样本重复打开文件的开销。

## 4. Unity 采集与解析重构

### 4.1 生命周期

连接、录制、流状态和文件完整性分离，状态包括：`disconnected`、`connected`、`arming`、`recording`、`stopping`、`flushing`、`complete`、`error`、`truncated`。状态、错误、最后完整帧和文件完整性写入 `meta.json`。

### 4.2 Protocol 和 raw 完整性

`read_frame()` payload 未读满时必须抛出明确截断异常，不得返回短帧。reader 记录最后完整 frame、错误、丢失字节和解析状态。停止过程等待 reader 退出、flush 完成、文件大小稳定，并对 header/footer/block 做校验；固定 sleep 不能作为唯一的完整性判断。

未来 raw 版本不能仅按下限兼容，未知更高版本默认标记为 `experimental` 或 `unsupported`，并记录 parser profile。

### 4.3 Helper

Profiler start/stop 状态切换统一通过 Unity 主线程执行。snapshot 改为异步 request，避免阻塞 stats、ping 和 stop。控制协议采用带版本号的 JSON。ProfilerRecorder 增加释放生命周期，并记录匹配到的 marker、线程和 `value_semantics: last_value`。

### 4.4 逐帧分析

实时面板可以继续使用 0.5～1 秒低频最新值，但离线分析从 `profiler.raw` 生成全量逐帧摘要，包括 frame ID、起止时间、CPU Main、Render、GPU、GC、Draw Calls、慢帧标志等。

全量统计输出：平均值、最大值、p95、p99、慢帧数量与比例、GC 帧数、frame gap、bad/partial block、lost bytes、未匹配 Begin/End、完整性和质量等级。抽样只用于展示，不得影响全量统计。

热点默认按完整调用路径聚合，同时提供名称聚合视图。调用树发生重建时必须标记 `tree_reconstructed`。

## 5. 报告和导出

普通日志和 JSON 行解析统一跳过坏行并记录 `invalid_lines`，拒绝 NaN、Infinity、空值和非法时间。聚合只处理 finite 且状态有效的数据，并按字段 schema 区分 average、sum、max、last 和 metadata。

报告保留结构化值、单位、状态、来源、范围和 aggregation；页面展示层负责格式化。GPU bandwidth 转 MB/frame 时保存原始 MB/s、使用的 FPS 时间戳及时间差，超过容差返回缺失而不是强行换算。

导出 ZIP 固定包含：

```text
manifest.json
apm_session.json
all system *.log
all structured *.json
unity_stats.log
profiler.raw
profiler.json
unity meta/quality files
README.md
```

manifest 记录每个文件的大小、指标、单位、来源、样本数、起止时间、坏行/缓存/缺失数量、Unity raw 完整性、parser profile 和版本。Excel/HTML 使用结构化数据生成，不替代原始数据。

## 6. 测试策略

新增测试覆盖：

1. ADB 非零退出、超时、权限、断连、空输出、缓存过期和恢复。
2. GPU delta、计数器回绕、Wi-Fi/mobile scope、磁盘多挂载点、CPU 越界和异常进程名。
3. monotonic 调度、epoch/monotonic 双时间、时钟回拨、跨午夜和采样延迟。
4. Unity payload 截断、footer/header 损坏、断流、flush 未完成、未来版本、frame gap、bad/partial block、路径聚合和慢帧统计。
5. 坏日志行、NaN/Infinity、单向网络、jank 无 FPS、sampled/full summary 和结构化状态。
6. ZIP 成员完整性、manifest、HTML/Excel 兼容性和当前模板改动保护。

验收要求：现有测试和新增测试全部通过；采集失败不能产生 `value=0,status=ok`；Unity 截断必须标记为 `truncated`；不完整 raw 必须在报告和 manifest 中告警；全量 Unity 统计不受导出帧数限制；导出包可独立复核原始曲线。

## 7. 实施顺序

1. 建立统一模型、CommandResult、时间和质量基础设施。
2. 重构 ADB 和系统 collector。
3. 重构 Unity Protocol、Helper 状态机和 raw 完整性。
4. 增加 Unity 全量逐帧摘要和质量统计。
5. 重构报告聚合和结构化输出。
6. 重构 ZIP、Excel 和 HTML 数据入口。
7. 保留并兼容当前模板未提交修改。
8. 补齐测试、运行完整测试和静态检查。
9. 使用断连、截断、坏日志和采样错位样本做最终验收。
