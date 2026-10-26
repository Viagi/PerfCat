# Task 2 完成报告

## 范围

本次仅处理 Task 2：结构化 ADB 命令结果、采样器 monotonic 时钟和采样失败元数据。保留未提交的 `perfcattle/templates/analysis.html`、`perfcattle/templates/report.html` 与 `.zcodeignore`，未暂存。

## 实现

- 新增 `perfcattle/core/command_result.py`，提供不可变 `CommandResult` dataclass。
- 新增 `ADB.shell_result()`，将成功、非零退出、超时、OS/Subprocess/未知异常映射为结构化结果，并记录耗时。
- 保留 `ADB.shell()` 兼容包装，返回结构化结果的 stdout，避免扩大既有调用方范围。
- `CollectorSampler` 的缓存 TTL 与 stale age 改用 `time.monotonic()`；记录最后 monotonic 采样时刻及连续失败次数，并在成功后清零。
- 新增 ADB 结构化结果测试，覆盖成功、非零退出、timeout 和 OSError。

## 验证

执行：

```text
pytest -q tests/test_adb_command_result.py tests/test_core_adb_client.py tests/test_collection_stability.py tests/test_recording_stability.py
```

结果：15 passed。现有测试产生 1269 个 Python/Werkzeug 弃用警告，非本次改动引入。

## 复审修正

- `_wait_for_latest()` 的 deadline、循环和 remaining 全部使用 `time.monotonic()`。
- `get_latest()` 对旧实例缺失 `last_sample_monotonic` 做兼容初始化。
- stale TTL 过期时统一 sampler 返回 `MetricStatus.MISSING`；TTL 内缓存返回 `CACHED` 并带 `stale_age_ms`。
- 增加严格 shell 错误穿透、legacy mode、stale missing 等边界测试。

## Task 2 复审最终修复（2026-10-26）

### 修复内容
- `_wait_for_latest()` 统一通过 `_cached_result()` 返回结果；统一 sampler 直接返回已有 `MetricSample(CACHED)`，不再把缓存重新包装成 `OK`。
- `ADB.shell()` 恢复 `strict=False` 默认，保持历史失败命令返回文本的兼容行为；需要错误传播的调用方可显式传 `strict=True`，并通过 `ADBCommandError.result.error_kind` 追踪 timeout/nonzero 等失败。
- 兼容缺失 monotonic 时间戳时使用 `last_sample_time` 估算年龄；墙钟倒退或无法安全估算时按 stale/missing 处理，不伪装新鲜。
- 补充 wait cached、ADB 错误 kind 穿透到 `MetricSample.error`、legacy shell 行为、TTL 过期和墙钟跳变回归测试。
- 未修改 `perfcattle/templates/analysis.html`、`perfcattle/templates/report.html`、`.zcodeignore`。

### 最终验证
- `pytest -q tests/test_adb_command_result.py tests/test_core_adb_client.py tests/test_collection_stability.py tests/test_recording_stability.py tests/test_collector_adb_metrics.py tests/test_collector_fps.py tests/test_device_android.py tests/test_android_device.py tests/test_metric_model.py`
- 结果：`63 passed`；保留既有 1269 个 Python/Werkzeug 弃用警告。
