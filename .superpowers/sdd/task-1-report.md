# Task 1 完整报告

## 改动文件
- `perfcattle/core/metrics.py`：新增 `MetricStatus`、`MetricSample`、`MetricQuality`、`finite_number`、`parse_metric_line`。
- `tests/test_metric_model.py`：按 TDD 新增模型、有限数值、序列化和日志质量统计测试。
- `perfcattle/report/log_io.py`：普通日志解析拒绝 NaN/Infinity，跳过坏行并统计 `MetricQuality`；新增 `readLogWithQuality`，保留原有点集接口。
- `perfcattle/collector/base.py`：未修改；本任务要求的基础采样模型接口独立于现有 sampler 接入，后续任务再扩展 collector 产出结构化样本。

用户已有的 `perfcattle/templates/analysis.html`、`perfcattle/templates/report.html` 未修改、未暂存、未提交。

## 接口
- `MetricStatus`：`ok`、`cached`、`unsupported`、`permission_denied`、`timeout`、`disconnected`、`invalid`、`truncated`、`missing`。
- `MetricSample`：指定字段 dataclass；非 `OK/CACHED` 状态强制 `value=None`；`ok()` 填充当前 epoch/monotonic 时间；支持 `to_dict()` / `from_dict()`。
- `MetricQuality`：统计 `total`、`valid`、`invalid`、`cached`、`missing`、首尾 epoch 时间，并提供 `to_dict()`。
- `finite_number(value)`：只接受有限 int/float，不接受 bool、NaN、Infinity。
- `parse_metric_line(line)`：解析统一 JSON 行，非法 JSON、非法状态或非 finite 值返回 `None`。
- `LogIO.readLogWithQuality(scene, filename, origin=None)`：返回 `(points, values, MetricQuality)`；旧 `readLog()` 和点集适配调用保持兼容。

## 测试命令和输出
- `pytest -q tests/test_metric_model.py`（首次运行，模块不存在，按要求失败：`ModuleNotFoundError`）
- `pytest -q tests/test_metric_model.py`：`7 passed`
- `pytest -q tests/test_metric_model.py tests/test_report_log_io.py`：`9 passed`
- `git diff --check`：无 diff 错误；仅报告现有模板 CRLF 警告。

## 遗留风险
- `perfcattle/collector/base.py` 尚未把现有 `CollectorSampler` 的返回值统一包装为 `MetricSample`；该行为属于后续采集器重构任务，本任务未扩大范围。
- JSON 行日志（如 GPU/Unity structured logs）尚未接入 `MetricQuality`，当前质量统计覆盖普通 `readLog` 路径。
- `MetricQuality.first_epoch_ms/last_epoch_ms` 对旧 `HH:MM:SS` 日志使用当日相对 epoch 毫秒，不含真实日期；后续 session 元数据任务应补齐真实 epoch。
- 当前分支仍有用户未提交模板改动及 `.zcodeignore` 未跟踪文件，提交时只会包含本任务文件。

## Task 1 复核与完整修复报告（2026-10-26）

### 复核范围
- `perfcattle/core/metrics.py`
- `perfcattle/collector/base.py`
- `perfcattle/report/log_io.py`
- `tests/test_metric_model.py`
- `tests/test_report_log_io.py`

### 修复确认
- `perfcattle/core/metrics.py`：`finite_number()` 拒绝 `bool`、NaN、Infinity，并捕获超大数的 `OverflowError`；`MetricSample` 对 `OK/CACHED` 缺失或非法值统一转为 `INVALID`，非成功状态清除值，并保持 `cached` 标志与状态一致；JSON 行解析捕获溢出异常。
- `perfcattle/collector/base.py`：新增 `adapt_metric_sample()`，把采集器输出统一转换为 `MetricSample`；状态值非法时转为 `INVALID`，成功/缓存状态拒绝空值和非有限值，失败状态不携带数值。
- `perfcattle/report/log_io.py`：普通日志读取抽出 `_read_log_with_quality()`，跳过格式错误、时间错误、非数值及非有限数值；准确维护 `MetricQuality` 的总数、有效数、无效数、缺失标记及首尾时间；`readLogWithQuality()` 返回质量对象，原 `readLog()` 接口保持兼容。
- 用户已有的 `perfcattle/templates/analysis.html`、`perfcattle/templates/report.html` 和 `.zcodeignore` 未纳入本次暂存或提交。

### 最新验证
- `pytest -q tests/test_metric_model.py tests/test_report_log_io.py`：`9 passed`。
- `git diff --check`：通过；仅出现 Git 关于 LF/CRLF 转换的既有工作区警告，无 whitespace 错误。

### 提交边界
本次提交仅包含上述三个源码文件；Task 1 测试文件已存在于当前提交历史且工作区无测试改动，因此不会产生重复变更。报告文件本身也不纳入本次提交。

### Concerns
- `MetricQuality.first_epoch_ms/last_epoch_ms` 对旧 `HH:MM:SS` 日志仍是当日秒数换算的毫秒，不含真实日期。
- JSON 行日志（如 GPU/Unity structured logs）仍未接入 `MetricQuality`。
- `CollectorSampler` 的内部返回值尚未全面改为 `MetricSample`；本次通过适配函数提供统一边界，未扩大采样器重构范围。

## Task 1 复审修复（基于 5a86c6d）

### 完成内容
- `CollectorSampler` 增加可选统一模式（构造参数或 `PERFCATTLE_UNIFIED_SAMPLER`），`sample_once()`/`sample()` 在启用时返回 `MetricSample`，覆盖成功、失败、缓存和过期缓存，并维护 `stale_age_ms`；默认旧模式保持原始返回值兼容。
- `adapt_metric_sample()` 作为 collector 边界适配器，规范状态、数值和失败值。
- `LogIO` 使用 `apm_session.json` 的真实 `epoch`，或 `date + clock` 换算 epoch；跨午夜按 session 起点计算首尾时间，避免仅返回当天秒数毫秒。
- 增加统一 sampler、适配器、session epoch、跨午夜、无序日志等回归测试；保留用户已有模板和 `.zcodeignore` 不变。
- 修正采集任务数与稳定性测试的一致性。

### 验证
- `pytest -q tests/test_metric_model.py tests/test_report_log_io.py tests/test_collection_stability.py tests/test_recording_stability.py`：19 passed。
- `git diff --check`：通过；仅有 Git 的既有 LF/CRLF 转换提示。

### Concerns
- 统一模式是可选配置，旧 collector 调用默认仍保持兼容的原始结果；需要逐步为明确指标传入 `unified=True` 或启用环境变量。
- structured JSON 日志仍沿用既有图表解析路径，未改变其公开返回接口。
