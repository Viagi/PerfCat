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

## Task 2 最终验收修复（2026-10-26）

- 新增 `collector.base.adb_shell()` 统一适配：真实 `ADB` 调用 strict structured mode，失败抛出携带 `CommandResult` 的 `ADBCommandError`；测试 double 保持历史 shell 兼容。
- cpu/disk/battery/fps/network/device/android/memory/gpu/thermal/file_io/gpu_stats 等实际路径迁移到该适配器。
- collector 无缓存时不再把 ADB 失败伪装为 0；失败继续穿透 sampler，统一 sampler 可填充 `MetricSample.error`。
- `CollectorSampler._age_seconds()` 使用 `getattr` 安全读取缺失的 `last_sample_monotonic`，回退 `last_sample_time`，墙钟倒退按 missing/stale 处理。
- 补充 `delattr` 缺失属性回归测试。

验证：
`pytest -q tests/test_collection_stability.py tests/test_collector_adb_metrics.py tests/test_collector_fps.py tests/test_device_android.py tests/test_android_device.py` → 41 passed。

## Task 2 失败零值修复（2026-10-26）

- FPS 无缓存且采样失败时抛出 `CollectorUnavailableError`，不再返回 `(0, 0)`；已有缓存仍按兼容路径返回。
- GPU 空、非法或分母为零输出且无缓存时抛出 `CollectorUnavailableError`；已有缓存仍可回退。
- GPU 合法 `0 / 100` 仍返回数值 `0`，区分真实零值与不可用结果。
- 新增 FPS 无缓存失败、GPU 不可用及 GPU 成功零值回归测试。
- 扫描其余 collector 的 `0` 返回：`file_io.py` 中为基线/无增量或非 Android 合法路径，不属于失败伪装；`fps.py` 的 `return 0` 是 surface rank 评分，不是采样结果。

验证：
`pytest -q tests/test_collection_stability.py tests/test_collector_adb_metrics.py tests/test_collector_fps.py tests/test_device_android.py tests/test_android_device.py tests/test_metric_model.py` → `57 passed`；保留既有 1269 个 Python/Werkzeug 弃用警告。

## Task 2 最后 Important：缓存 fallback 不刷新 sampler TTL（2026-10-26）

### 修复内容
- 新增 `CachedSample` 明确标记 collector-level fallback；`CollectorSampler` 返回该结果时只包装为 `CACHED`，不更新 `last_valid_result`、`last_sample_time` 或 `last_sample_monotonic`。
- FPS/GPU 采样失败时继续兼容已有缓存展示，但缓存年龄仍从最近一次真实成功采样计算；超过 `stale_ttl` 后统一返回 `MISSING`。
- 保留真实零值（例如 GPU `0 / 100`）为 `OK` 成功结果，不将其误判为 unavailable/cache。
- 未修改 templates 或 `.zcodeignore`。

### 新增测试与验证
- 覆盖真实成功后连续 cached fallback、fallback 状态、TTL 时间戳不刷新、TTL 到期最终 `MISSING`，以及真实零值成功。
- `pytest -q tests/test_collection_stability.py -k 'cached_sample_fallback or real_zero'` → `2 passed`。
- `pytest -q tests/test_collection_stability.py tests/test_collector_adb_metrics.py tests/test_collector_fps.py tests/test_device_android.py tests/test_android_device.py tests/test_metric_model.py` → `60 passed`；保留既有 1269 个 Python/Werkzeug 弃用警告。
