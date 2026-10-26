# -*- coding: utf-8 -*-
"""采集器共享基础类型定义"""
import copy
import os
import threading
import time

from logzero import logger
from perfcattle.core.adb_client import ADB
from perfcattle.core.metrics import MetricSample, MetricStatus, finite_number


class CollectorUnavailableError(RuntimeError):
    """Raised when a collector cannot produce a valid uncached sample."""


class CachedSample:
    """A collector-provided fallback value that must not refresh sampler TTL."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


def adb_shell(client, **kwargs):
    """Run a collector ADB command with structured failure propagation.

    Test doubles that only implement the historical ``shell`` API remain
    supported, while the real ADB client always uses strict structured mode.
    """
    if isinstance(client, ADB):
        return client.shell(strict=True, **kwargs)
    return client.shell(**kwargs)


def adb_read_optional(client, **kwargs):
    """Best-effort read used for probing optional sources.

    Collector code probes many device nodes and commands whose absence is
    normal, not an error: ``/proc/uid_stat`` was removed in modern Android,
    most ``/sys`` GPU nodes are unreadable without root, and
    ``dumpsys ... | grep`` exits non-zero when nothing matches. Those probes
    must yield an empty string so the existing fallback chain still runs;
    required reads keep using ``adb_shell`` and raise a structured failure.
    """
    try:
        return adb_shell(client, **kwargs)
    except Exception as exc:
        logger.debug("optional adb read failed: %s", exc)
        return ""


def adapt_metric_sample(
    name,
    value=None,
    unit=None,
    source=None,
    status=MetricStatus.OK,
    **kwargs,
):
    """Adapt a collector result to the unified metric sample boundary."""
    try:
        metric_status = status if isinstance(status, MetricStatus) else MetricStatus(status)
    except (TypeError, ValueError):
        metric_status = MetricStatus.INVALID
    if metric_status in (MetricStatus.OK, MetricStatus.CACHED):
        if value is None or not finite_number(value):
            metric_status = MetricStatus.INVALID
            value = None
    else:
        value = None
    return MetricSample(
        metric=name,
        value=value,
        unit=unit,
        source=source,
        status=metric_status,
        **kwargs,
    )


_SAMPLER_ADB_CONCURRENCY = max(
    1, int(os.environ.get("PERFCATTLE_SAMPLER_ADB_CONCURRENCY", "2"))
)
_SAMPLER_INITIAL_WAIT_SECONDS = max(
    1.0, float(os.environ.get("PERFCATTLE_SAMPLER_INITIAL_WAIT_SECONDS", "4.0"))
)
_SAMPLER_ADB_SEMAPHORE = threading.BoundedSemaphore(_SAMPLER_ADB_CONCURRENCY)
_SAMPLER_PHASE_OFFSETS = {
    "fps": 0.0,
    "cpu": 0.10,
    "cpu_core": 0.18,
    "memory": 0.22,
    "network": 0.34,
    "gpu": 0.46,
    "file_io": 0.58,
    "memory_detail": 0.66,
    "thermal": 0.74,
    "battery": 0.86,
}


def create_devices():
    from perfcattle.device.manager import Devices

    return Devices()


def create_report_store():
    from perfcattle.report.manager import ReportStore

    return ReportStore()


class Target:
    """性能采集目标枚举"""
    CPU = "cpu"
    Memory = "memory"
    MemoryDetail = "memory_detail"
    Battery = "battery"
    Network = "network"
    FPS = "fps"
    GPU = "gpu"
    GPU_STATS = "gpu_stats"
    DISK = "disk"
    FileIO = "file_io"


class ReusableCollector:
    """按采集目标复用短生命周期 Collector 实例。"""

    _SESSION_IDLE_SECONDS = 60
    SAMPLER_INTERVAL_SECONDS = 1.0
    SAMPLER_STALE_TTL_SECONDS = 5.0

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._object_cache = {}
        cls._session_lock = threading.RLock()

    @classmethod
    def _object_cache_key(cls, *args, **kwargs):
        raise NotImplementedError

    @classmethod
    def _cleanup_idle_objects_locked(cls, now):
        for key, entry in list(cls._object_cache.items()):
            if now - entry["last_access"] > cls._SESSION_IDLE_SECONDS:
                obj = entry.get("object")
                if obj is not None:
                    obj.stop()
                cls._object_cache.pop(key, None)

    @classmethod
    def getObject(cls, *args, **kwargs):
        key = cls._object_cache_key(*args, **kwargs)
        now = time.time()
        with cls._session_lock:
            cls._cleanup_idle_objects_locked(now)
            entry = cls._object_cache.get(key)
            if entry is not None:
                entry["last_access"] = now
                setattr(entry["object"], "_managed_reusable_collector", True)
                return entry["object"]

        obj = cls(*args, **kwargs)
        setattr(obj, "_managed_reusable_collector", True)

        with cls._session_lock:
            cls._cleanup_idle_objects_locked(time.time())
            entry = cls._object_cache.get(key)
            if entry is not None:
                entry["last_access"] = time.time()
                setattr(entry["object"], "_managed_reusable_collector", True)
                return entry["object"]
            cls._object_cache[key] = {"object": obj, "last_access": time.time()}
        return obj

    def _get_devices(self):
        return create_devices()

    def _get_report_store(self):
        return create_report_store()

    def _get_sampler(
        self,
        name,
        sample_func,
        interval=None,
        stale_ttl=None,
        background=True,
        phase_offset=None,
        unified=None,
        unit=None,
        source=None,
    ):
        if not hasattr(self, "_samplers"):
            self._samplers = {}
        sampler = self._samplers.get(name)
        if sampler is None:
            sampler = CollectorSampler(
                name=name,
                sample_func=sample_func,
                interval=(
                    self.SAMPLER_INTERVAL_SECONDS if interval is None else interval
                ),
                stale_ttl=(
                    self.SAMPLER_STALE_TTL_SECONDS
                    if stale_ttl is None
                    else stale_ttl
                ),
                background=background,
                phase_offset=(
                    self._sampler_phase_offset(name)
                    if phase_offset is None
                    else phase_offset
                ),
                unified=unified,
                unit=unit,
                source=source,
            )
            self._samplers[name] = sampler
        return sampler

    def _sampler_phase_offset(self, name):
        metric = str(name).split(":", 1)[0]
        return _SAMPLER_PHASE_OFFSETS.get(metric, 0.0)

    def _sampled_value(
        self,
        name,
        sample_func,
        interval=None,
        stale_ttl=None,
        background=True,
        phase_offset=None,
        unified=None,
        unit=None,
        source=None,
    ):
        sampler = self._get_sampler(
            name,
            sample_func,
            interval=interval,
            stale_ttl=stale_ttl,
            background=background and getattr(
                self, "_managed_reusable_collector", False
            ),
                phase_offset=phase_offset,
                unified=unified,
                unit=unit,
                source=source,
            )

        return sampler.get_or_sample()

    def _cached_sampled_value(self, name, max_age=None):
        sampler = getattr(self, "_samplers", {}).get(name)
        if sampler is None:
            return None
        return sampler.get_latest(max_age=max_age)

    def stop(self):
        for sampler in list(getattr(self, "_samplers", {}).values()):
            sampler.stop()

    @classmethod
    def clear(cls):
        with cls._session_lock:
            for entry in list(cls._object_cache.values()):
                obj = entry.get("object")
                if obj is not None:
                    obj.stop()
            cls._object_cache.clear()


class CollectorSampler:
    """Cache latest collector result and optionally refresh it in the background."""

    def __init__(
        self,
        name,
        sample_func,
        interval=1.0,
        stale_ttl=5.0,
        background=True,
        phase_offset=0.0,
        unified=False,
        unit=None,
        source=None,
        status_on_error=MetricStatus.DISCONNECTED,
    ):
        self.name = name
        self.sample_func = sample_func
        self.interval = max(float(interval or 1.0), 0.1)
        self.stale_ttl = max(float(stale_ttl or self.interval), self.interval)
        self.background = bool(background)
        self.phase_offset = max(float(phase_offset or 0.0), 0.0)
        self.unified = bool(unified) if unified is not None else os.environ.get(
            "PERFCATTLE_UNIFIED_SAMPLER", "0"
        ).lower() in ("1", "true", "yes", "on")
        self.unit = unit
        self.source = source
        self.status_on_error = status_on_error
        self.last_valid_result = None
        self.last_sample_time = None
        self.last_sample_monotonic = None
        self.last_error = None
        self.consecutive_failures = 0
        self._lock = threading.RLock()
        self._sample_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if not self.background:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._sample_loop,
                name="perfcattle-sampler-{}".format(self.name),
                daemon=True,
            )
            self._thread.start()

    def stop(self):
        thread = None
        with self._lock:
            thread = self._thread
            self._stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(self.interval, 1.0))
        with self._lock:
            if thread is self._thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def _age_seconds(self):
        """Return cache age, safely handling samplers persisted before monotonic timestamps."""
        if self.last_sample_time is None:
            return None
        monotonic_sample_time = getattr(self, "last_sample_monotonic", None)
        if monotonic_sample_time is not None:
            age = time.monotonic() - monotonic_sample_time
        else:
            # Wall-clock fallback is only an estimate. A backwards clock jump
            # is unsafe, so treat it as stale rather than fabricating freshness.
            wall_age = time.time() - self.last_sample_time
            if wall_age < 0:
                return None
            age = wall_age
        return max(0.0, age)

    def get_latest(self, max_age=None):
        with self._lock:
            if self.last_valid_result is None or self.last_sample_time is None:
                return None
            age = self._age_seconds()
            if age is None or (max_age is not None and age > max_age):
                return None
            return copy.deepcopy(self.last_valid_result)

    def _as_sample(self, value, status=MetricStatus.OK, error=None, stale_age_ms=None):
        return adapt_metric_sample(
            self.name, value=value, unit=self.unit, source=self.source,
            status=status, collected_epoch_ms=int(time.time() * 1000),
            sample_epoch_ms=(
                int(self.last_sample_time * 1000)
                if self.last_sample_time is not None else None
            ), stale_age_ms=stale_age_ms, error=error,
        )

    def _cached_result(self, max_age=None):
        cached = self.get_latest(max_age=max_age)
        if cached is None:
            return None
        age_seconds = self._age_seconds()
        if age_seconds is None:
            return None
        if not self.unified:
            return cached
        age_ms = max(0.0, age_seconds * 1000)
        return self._as_sample(cached, MetricStatus.CACHED, stale_age_ms=age_ms)

    def _error_text(self, exc):
        result = getattr(exc, "result", None)
        if result is not None:
            return "{}: {}".format(
                result.error_kind or "adb_error",
                result.stderr or str(exc),
            )
        return str(exc)

    def _missing_result(self, error=None):
        if self.unified:
            return self._as_sample(None, MetricStatus.MISSING, error=error)
        return None

    def sample(self):
        try:
            cached = self._cached_result(max_age=self.stale_ttl)
            if cached is not None:
                return cached
            return self.sample_once()
        except Exception as exc:
            cached = self._cached_result(max_age=self.stale_ttl)
            if cached is not None:
                return cached
            if self.unified:
                return self._missing_result(error=self._error_text(exc))
            raise

    def _wait_for_latest(self):
        timeout = min(
            max(self.phase_offset + _SAMPLER_INITIAL_WAIT_SECONDS, 1.0),
            self.stale_ttl,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cached = self._cached_result(max_age=self.stale_ttl)
            if cached is not None:
                return cached
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 0.05))
        cached = self._cached_result(max_age=self.stale_ttl)
        if cached is not None:
            return cached
        raise RuntimeError("{} sampler has no cached value".format(self.name))

    def get_or_sample(self):
        self.start()
        cached = self._cached_result(max_age=self.stale_ttl)
        if cached is not None:
            return cached
        if self.background:
            try:
                return self._wait_for_latest()
            except Exception as exc:
                # The background thread may still be queued behind the shared
                # ADB semaphore right after Start. Falling back to one
                # synchronous sample keeps the first poll from surfacing a
                # transient "no cached value" error to the UI.
                logger.debug(
                    "%s sampler wait failed (%s), sampling synchronously",
                    self.name,
                    exc,
                )
        return self.sample()

    def sample_once(self):
        if not self._sample_lock.acquire(blocking=False):
            cached = self._cached_result(max_age=self.stale_ttl)
            if cached is not None:
                return cached
            if not self._sample_lock.acquire(timeout=min(self.interval, 1.0)):
                if self.unified:
                    return self._missing_result(error="sampler is busy")
                raise RuntimeError("{} sampler is busy".format(self.name))
        try:
            with self._lock:
                has_previous_sample = self.last_valid_result is not None
            acquire_timeout = (
                max(self.interval, 1.0)
                if has_previous_sample
                else max(self.stale_ttl, self.interval, 1.0)
            )
            if not _SAMPLER_ADB_SEMAPHORE.acquire(timeout=acquire_timeout):
                raise RuntimeError("{} sampler adb queue is busy".format(self.name))
            try:
                result = self.sample_func()
            finally:
                _SAMPLER_ADB_SEMAPHORE.release()
            cached_result = isinstance(result, CachedSample)
            if cached_result:
                result = result.value
            now = time.time()
            now_monotonic = time.monotonic()
            with self._lock:
                if not cached_result:
                    self.last_valid_result = copy.deepcopy(result)
                    self.last_sample_time = now
                    self.last_sample_monotonic = now_monotonic
                    self.last_error = None
                    self.consecutive_failures = 0
            if cached_result:
                cached = self._cached_result(max_age=self.stale_ttl)
                if cached is not None:
                    return cached
                raise CollectorUnavailableError("cached collector sample expired")
            return self._as_sample(result) if self.unified else copy.deepcopy(result)
        except Exception as exc:
            with self._lock:
                self.last_error = exc
                self.consecutive_failures += 1
            if self.unified:
                cached = self._cached_result(max_age=self.stale_ttl)
                if cached is not None:
                    return cached
                return self._missing_result(error=self._error_text(exc))
            raise
        finally:
            self._sample_lock.release()

    def _initial_delay(self):
        if self.interval <= 0:
            return 0.0
        return min(self.phase_offset, self.interval)

    def _sample_loop(self):
        next_at = time.monotonic() + self._initial_delay()
        while not self._stop_event.is_set():
            sleep_seconds = next_at - time.monotonic()
            if sleep_seconds > 0 and self._stop_event.wait(sleep_seconds):
                return
            try:
                self.sample_once()
            except Exception as exc:
                if "sampler" in str(exc) and "busy" in str(exc):
                    pass
                else:
                    logger.debug("%s sampler sample failed: %s", self.name, exc)
            next_at += self.interval
            sleep_seconds = next_at - time.monotonic()
            if sleep_seconds <= 0:
                missed_intervals = int(abs(sleep_seconds) // self.interval) + 1
                next_at += missed_intervals * self.interval
