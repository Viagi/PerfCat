# Task 3 report

## Scope
Implemented the requested collector quality boundaries without changing `templates/analysis.html`, `templates/report.html`, or `.zcodeignore`.

## Changes
- CPU `/proc/<pid>/stat` parsing now locates the comm field using the first `(` and final `)`, so spaces/right parentheses in process names are supported.
- CPU rate calculation rejects negative/invalid deltas, rejects process delta greater than total delta, and clamps valid rates to 0–100 instead of fabricating a fallback zero.
- GPU stats now tracks `previous_busy`, `previous_total`, and `counter_semantics`; cumulative counter samples use adjacent deltas and resets/wraps become `MetricStatus.INVALID`.
- UID network totals are explicitly marked with `scope=uid_total` rather than being presented as Wi-Fi/package transport scope.
- Disk `df` fallback filters overlay/tmpfs-like filesystems, deduplicates mount points, preserves `mount_points`, and marks results `snapshot_only=True`.
- Android `collectAll()` includes disk as an explicit capacity snapshot collector while leaving continuous metric collectors unchanged.

## TDD evidence
Added `tests/test_collector_quality.py`, observed the expected red failures before implementation, then ran the focused suite green.

## Verification
`pytest -q tests/test_collector_quality.py tests/test_collector_adb_metrics.py tests/test_collection_stability.py`

Result: 47 passed, 1269 warnings. Warnings are existing Werkzeug/Python deprecation warnings.

## Follow-up completion

The review follow-up is implemented on top of commit `936c63d`.

- GPU stats probing now declares source semantics, applies cumulative deltas through the actual `sample()` path, treats the first baseline and reset/wrap as invalid, and preserves source/semantics metadata.
- CPU invalid total/process/core deltas now surface structured sampler failure rather than unpacking `None`, returning an empty/fake zero rate, or emitting out-of-range core rates.
- Network and file I/O public paths now retain explicit status/source/scope metadata in `last_sample`; missing values remain `None`. Thermal, memory, FPS, GPU, GPU stats, and monitor output paths likewise preserve status metadata and no longer synthesize jank zero when unavailable.
- Disk snapshots carry epoch/monotonic timestamps, `snapshot_only`, mount metadata, and filtering/deduplication behavior.
- Added public-path quality tests covering missing network/file I/O/thermal samples and CPU invalid deltas while preserving valid zero samples.

Verification: `python -m compileall -q perfcattle`, `pytest -q` -> 156 passed. Existing Werkzeug/Python deprecation warnings remain.
