# -*- coding: utf-8 -*-
"""Android file IO collector.

Reads foreground logical read/write byte counts from ``dumpsys storaged``.
"""
import datetime
import os
import re
import threading
import time

from logzero import logger

from perfcattle.collector.base import (
    ReusableCollector,
    adb_shell,
    _SAMPLER_ADB_SEMAPHORE,
    _SAMPLER_PHASE_OFFSETS,
)
from perfcattle.core.adb_client import adb
from perfcattle.core.platform import Platform
from perfcattle.core.metrics import MetricStatus


class FileIO(ReusableCollector):
    """Collect Android package file read/write deltas in KB."""

    ADB_TIMEOUT = 5
    MIN_SAMPLE_INTERVAL_SECONDS = 0.2
    STORAGED_SAMPLE_INTERVAL_SECONDS = float(
        os.environ.get("PERFCATTLE_FILEIO_STORAGED_SAMPLE_INTERVAL_SECONDS", "0.8")
    )
    STORAGED_PHASE_OFFSET_SECONDS = float(
        os.environ.get(
            "PERFCATTLE_FILEIO_STORAGED_PHASE_OFFSET_SECONDS",
            str(_SAMPLER_PHASE_OFFSETS.get("file_io", 0.58)),
        )
    )
    STORAGED_METRIC = os.environ.get("PERFCATTLE_FILEIO_STORAGED_METRIC", "combined")
    # Use an overlapping storaged window. Local bucket max counters de-duplicate
    # repeated buckets, while the wider window tolerates adb/dumpsys delays.
    STORAGED_HOURS = os.environ.get("PERFCATTLE_FILEIO_STORAGED_HOURS", "0.01")

    _fileio_data_cache = {}

    def __init__(
        self,
        pkgName,
        deviceId,
        platform=Platform.Android,
        pid=None,
        processName=None,
    ):
        self.pkgName = pkgName
        self.deviceId = deviceId
        self.platform = platform
        self.pid = pid
        self.processName = processName
        self._uid = None
        self._last_fileio_data = self._fileio_data_cache.get(self._data_cache_key())
        self._storaged_bucket_totals = {}
        self._storaged_baseline_ready = False
        self._last_fileio_sample_time = None
        self._last_logged_sample_time = None
        self.last_sample_time = None
        self._force_next_sample = False
        self._storaged_sampler_thread = None
        self._storaged_sampler_stop = threading.Event()
        self._storaged_sampler_lock = threading.RLock()
        self.last_source = None
        self.last_scope = None
        self.fallback_reason = None
        self.last_sample = None
        if self.pid is None and self.platform == Platform.Android:
            self._refresh_pid()

    @classmethod
    def _make_cache_key(cls, platform, deviceId, pkgName, pid=None, processName=None):
        return platform, deviceId, pkgName, processName or pid or pkgName

    @classmethod
    def _object_cache_key(
        cls,
        pkgName,
        deviceId,
        platform=Platform.Android,
        pid=None,
        processName=None,
    ):
        return cls._make_cache_key(platform, deviceId, pkgName, pid, processName)

    @classmethod
    def _cleanup_idle_objects_locked(cls, now):
        for key, entry in list(cls._object_cache.items()):
            if now - entry["last_access"] > cls._SESSION_IDLE_SECONDS:
                obj = entry.get("object")
                if obj is not None:
                    obj.stop()
                cls._object_cache.pop(key, None)

    @classmethod
    def clear(cls):
        with cls._session_lock:
            for entry in list(cls._object_cache.values()):
                obj = entry.get("object")
                if obj is not None:
                    obj.stop()
        super().clear()
        cls._fileio_data_cache.clear()

    def _pid_lookup_name(self):
        return self.processName or self.pkgName

    def _supports_package_scope(self):
        return not self.processName or self.processName == self.pkgName

    def _file_io_scope(self):
        return "package" if self._supports_package_scope() else "package_shared"

    def _file_io_fallback_reason(self):
        if self._supports_package_scope():
            return None
        return "using package file IO totals shared by all app processes"

    def _cache_key(self):
        return self._make_cache_key(
            self.platform,
            self.deviceId,
            self.pkgName,
            self.pid,
            self.processName,
        )

    def _data_cache_key(self):
        return self._cache_key()

    def _storaged_metric(self):
        metric = os.environ.get("PERFCATTLE_FILEIO_STORAGED_METRIC", self.STORAGED_METRIC)
        return (metric or "combined").strip().lower()

    def _refresh_pid(self, devices=None):
        if self.platform != Platform.Android:
            return False
        devices = devices or self._get_devices()
        process_list = devices.getPid(
            pkgName=self._pid_lookup_name(), deviceId=self.deviceId
        )
        if not process_list:
            return False
        new_pid = process_list[0].split(":", 1)[0]
        if self.pid and self.pid != new_pid:
            self.stop()
            self._storaged_bucket_totals.clear()
            self._storaged_baseline_ready = False
            self._uid = None
        self.pid = new_pid
        return True

    def _process_exists(self, devices):
        return self._refresh_pid(devices)

    def _read_uid(self):
        if self._uid is not None:
            return self._uid
        output = adb_shell(adb,
            cmd=f"cat /proc/{self.pid}/status",
            deviceId=self.deviceId,
            timeout=self.ADB_TIMEOUT,
        )
        for line in (output or "").splitlines():
            if line.startswith("Uid:"):
                parts = line.split()
                if len(parts) > 1 and parts[1].isdigit():
                    self._uid = int(parts[1])
                    return self._uid
        return self._uid

    def stop(self):
        self._stop_storaged_sampler()

    def _stop_storaged_sampler(self):
        thread = self._storaged_sampler_thread
        if thread is None:
            return
        self._storaged_sampler_stop.set()
        if thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        if thread.is_alive():
            return
        self._storaged_sampler_thread = None

    @staticmethod
    def _numeric_tokens(tokens):
        nums = []
        for token in tokens:
            if re.fullmatch(r"-?\d+", token):
                nums.append(int(token))
        return nums

    @staticmethod
    def _totals_from_storaged_numbers(numbers, metric="combined"):
        if len(numbers) < 4:
            return None
        fg_read = numbers[0] + numbers[2]
        fg_write = numbers[1] + numbers[3]
        bg_read = bg_write = 0
        if len(numbers) >= 8:
            bg_read = numbers[4] + numbers[6]
            bg_write = numbers[5] + numbers[7]
        if metric in ("foreground", "fg"):
            return fg_read, fg_write
        if metric in ("chars", "char"):
            read = numbers[0]
            write = numbers[1]
            if len(numbers) >= 8:
                read += numbers[4]
                write += numbers[5]
            return read, write
        if metric == "bytes":
            read = numbers[2]
            write = numbers[3]
            if len(numbers) >= 8:
                read += numbers[6]
                write += numbers[7]
            return read, write
        if metric in ("combined", "all"):
            return fg_read + bg_read, fg_write + bg_write
        raise ValueError(f"unsupported storaged metric: {metric}")

    @classmethod
    def _parse_storaged_output(cls, output, package_name, uid=None, metric="combined"):
        totals = cls._sum_storaged_events(
            cls._parse_storaged_events(output, package_name, uid=uid, metric=metric)
        )
        return totals

    @classmethod
    def _parse_storaged_events(cls, output, package_name, uid=None, metric="combined"):
        events = []
        bucket_key = None
        exact_matches = []
        uid_matches = []
        target_uid = int(uid) if uid is not None and str(uid).isdigit() else None
        for raw_line in (output or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            bucket_match = re.fullmatch(r"(\d*),(\d+)", line)
            if bucket_match:
                start_value, end_value = bucket_match.groups()
                try:
                    bucket_start = int(start_value) if start_value else None
                    bucket_end = int(end_value)
                    bucket_key = cls._storaged_bucket_key(bucket_start, bucket_end)
                except ValueError:
                    bucket_key = None
                continue

            tokens = line.split()
            has_package = any(
                token.strip(":,") == package_name
                or token.startswith(f"{package_name}:")
                for token in tokens
            )
            numbers = cls._numeric_tokens(tokens)
            line_uid = None
            if tokens and re.fullmatch(r"\d+", tokens[0]) and numbers:
                line_uid = numbers[0]
            has_uid = target_uid is not None and line_uid == target_uid
            if not has_package and not has_uid:
                continue
            if has_uid and numbers and numbers[0] == line_uid:
                numbers = numbers[1:]
            totals = cls._totals_from_storaged_numbers(numbers, metric)
            if totals is None:
                continue
            event = (bucket_key, line, totals[0], totals[1])
            if has_package:
                exact_matches.append(event)
            else:
                uid_matches.append(event)
        return exact_matches or uid_matches

    @staticmethod
    def _storaged_bucket_key(bucket_start, bucket_end):
        # storaged prints the same bucket as ",end" while it is recent, then as
        # "start,end" when the rolling window advances. The end timestamp is the
        # stable bucket identity; using start would count the same bucket twice.
        return bucket_end

    @staticmethod
    def _sum_storaged_events(events):
        if not events:
            return None
        return sum(item[2] for item in events), sum(item[3] for item in events)

    @staticmethod
    def _bucket_storaged_events(events):
        buckets = {}
        for bucket_end, _, read_bytes, write_bytes in events:
            read_total, write_total = buckets.get(bucket_end, (0, 0))
            buckets[bucket_end] = (
                read_total + read_bytes,
                write_total + write_bytes,
            )
        return buckets

    def _read_storaged_events(self):
        uid = self._read_uid()
        output = adb_shell(adb,
            cmd=f"dumpsys storaged --force --hours {self.STORAGED_HOURS}",
            deviceId=self.deviceId,
            timeout=15,
        )
        events = self._parse_storaged_events(
            output, self.pkgName, uid=uid, metric=self._storaged_metric()
        )
        if not events:
            raise ValueError("storaged package events unavailable")
        self.last_scope = self._file_io_scope()
        self.fallback_reason = self._file_io_fallback_reason()
        return events

    @staticmethod
    def _bytes_to_kb(value):
        return round(float(value) / 1024, 2)

    @staticmethod
    def format_log_time(timestamp=None):
        timestamp = time.time() if timestamp is None else timestamp
        return datetime.datetime.fromtimestamp(timestamp).strftime("%H:%M:%S.%f")

    def _get_storaged_delta(self):
        events = self._read_storaged_events()
        if not events and self.fallback_reason:
            return None, None
        bucket_totals = self._bucket_storaged_events(events)
        if not self._storaged_baseline_ready:
            self._storaged_bucket_totals = bucket_totals
            self._storaged_baseline_ready = True
            self.last_source = "storaged"
            return 0, 0
        read_delta = 0
        write_delta = 0
        for bucket_end, current in bucket_totals.items():
            previous = self._storaged_bucket_totals.get(bucket_end, (0, 0))
            read_delta += max(current[0] - previous[0], 0)
            write_delta += max(current[1] - previous[1], 0)
            self._storaged_bucket_totals[bucket_end] = (
                max(previous[0], current[0]),
                max(previous[1], current[1]),
            )
        self.last_source = "storaged"
        if read_delta <= 0 and write_delta <= 0:
            return 0, 0
        return self._bytes_to_kb(read_delta), self._bytes_to_kb(write_delta)

    def _store_fileio_sample(self, read_kb, write_kb, sample_time):
        with self._storaged_sampler_lock:
            self._force_next_sample = False
            self._last_fileio_data = (read_kb, write_kb)
            self._last_fileio_sample_time = sample_time
            self.last_sample_time = sample_time
            self.last_source = "storaged"
            self.fallback_reason = self._file_io_fallback_reason()
            self._fileio_data_cache[self._data_cache_key()] = self._last_fileio_data

    def _sample_storaged_once(self, sample_time=None):
        read_kb, write_kb = self._get_storaged_delta()
        sample_time = time.time() if sample_time is None else sample_time
        self._store_fileio_sample(read_kb, write_kb, sample_time)
        return read_kb, write_kb

    def _with_adb_slot(self, sample_func):
        timeout = max(float(self.STORAGED_SAMPLE_INTERVAL_SECONDS), 1.0)
        if not _SAMPLER_ADB_SEMAPHORE.acquire(timeout=timeout):
            raise RuntimeError("file_io sampler adb queue is busy")
        try:
            return sample_func()
        finally:
            _SAMPLER_ADB_SEMAPHORE.release()

    def _ensure_storaged_sampler(self):
        with self._storaged_sampler_lock:
            thread = self._storaged_sampler_thread
            if thread is not None and thread.is_alive():
                return
            self._storaged_sampler_stop.clear()
            thread = threading.Thread(
                target=self._storaged_sample_loop,
                name=f"perfcattle-fileio-storaged-{self.pkgName}",
                daemon=True,
            )
            self._storaged_sampler_thread = thread
            thread.start()

    def _storaged_sample_loop(self):
        interval = max(0.2, float(self.STORAGED_SAMPLE_INTERVAL_SECONDS))
        phase_offset = min(max(float(self.STORAGED_PHASE_OFFSET_SECONDS), 0.0), interval)
        next_at = time.monotonic() + phase_offset
        while not self._storaged_sampler_stop.is_set():
            sleep_seconds = next_at - time.monotonic()
            if sleep_seconds > 0 and self._storaged_sampler_stop.wait(sleep_seconds):
                return
            try:
                self._with_adb_slot(self._sample_storaged_once)
            except Exception as exc:
                with self._storaged_sampler_lock:
                    self.fallback_reason = str(exc)
                    self.last_source = "storaged"
                if "sampler" not in str(exc) or "busy" not in str(exc):
                    logger.warning("[FileIO] storaged sample failed: %s", exc)

            next_at += interval
            sleep_seconds = next_at - time.monotonic()
            if sleep_seconds <= 0:
                missed_intervals = int(abs(sleep_seconds) // interval) + 1
                next_at += missed_intervals * interval
                sleep_seconds = next_at - time.monotonic()
            if sleep_seconds > 0:
                self._storaged_sampler_stop.wait(sleep_seconds)

    def setAndroidFileIO(self):
        """设置 Android 文件 IO 基线数据。"""
        if self.platform != Platform.Android:
            return 0, 0
        self._stop_storaged_sampler()
        try:
            events = self._with_adb_slot(self._read_storaged_events)
            self._storaged_bucket_totals = self._bucket_storaged_events(events)
            self._storaged_baseline_ready = True
            self.last_source = "storaged"
            self._last_fileio_data = (0, 0)
            self.last_sample_time = time.time()
            self._last_fileio_sample_time = self.last_sample_time
            self._fileio_data_cache[self._data_cache_key()] = self._last_fileio_data
            self._force_next_sample = True
            self._ensure_storaged_sampler()
        except Exception as exc:
            self.fallback_reason = str(exc)
            logger.warning("[FileIO] set baseline failed: %s", exc)
            self.last_source = "storaged"
            self._last_fileio_data = (None, None)
            self.last_sample_time = time.time()
            self._last_fileio_sample_time = self.last_sample_time
            self._fileio_data_cache[self._data_cache_key()] = self._last_fileio_data
        return self._last_fileio_data

    def getAndroidFileIO(self):
        """获取 Android 文件读写增量，单位 KB。"""
        if self.platform != Platform.Android:
            return 0, 0
        sample_time = time.time()
        previous_time = self._last_fileio_sample_time
        data_cache_key = self._data_cache_key()
        cached_data = self._fileio_data_cache.get(data_cache_key)
        if (
            cached_data is not None
            and previous_time is not None
            and not self._force_next_sample
            and sample_time - previous_time < self.MIN_SAMPLE_INTERVAL_SECONDS
        ):
            self._last_fileio_data = cached_data
            self.last_sample_time = self.last_sample_time or previous_time
            return cached_data

        _d = self._get_devices()
        try:
            self._ensure_storaged_sampler()
            with self._storaged_sampler_lock:
                if self._last_fileio_data is None:
                    self._last_fileio_data = self._fileio_data_cache.get(
                        data_cache_key, (0, 0)
                    )
                read_kb, write_kb = self._last_fileio_data
                self.last_source = "storaged"
            return read_kb, write_kb
        except Exception as exc:
            self.fallback_reason = str(exc)
            self.last_source = "storaged"
            self._last_fileio_sample_time = sample_time
            self.last_sample_time = sample_time
            cached_data = self._fileio_data_cache.get(data_cache_key)
            if self._last_fileio_data is None and cached_data is not None:
                self._last_fileio_data = cached_data
            if self._last_fileio_data is not None:
                read_kb, write_kb = self._last_fileio_data
                logger.warning("[FileIO] %s, using cached value", exc)
            else:
                read_kb, write_kb = None, None
                logger.warning("[FileIO] %s, no cached value", exc)
            if not self._process_exists(_d):
                logger.error("[FileIO] %s : No process found", self.pkgName)
        return read_kb, write_kb

    def getFileIOData(self, noLog=False):
        """获取文件读写增量数据，单位 KB。"""
        _f = self._get_report_store()
        read_kb, write_kb = self.getAndroidFileIO()
        status = MetricStatus.OK if read_kb is not None and write_kb is not None else MetricStatus.MISSING
        self.last_sample = {
            "read": read_kb, "write": write_kb, "unit": "KB", "status": status.value,
            "source": self.last_source, "scope": self.last_scope,
            "sample_epoch_ms": int((self.last_sample_time or time.time()) * 1000),
            "error": self.fallback_reason if status is not MetricStatus.OK else None,
        }
        if noLog is False and status is MetricStatus.OK:
            if self.last_sample_time == self._last_logged_sample_time:
                return read_kb, write_kb
            apm_time = self.format_log_time(self.last_sample_time)
            _f.add_log(os.path.join(_f.report_dir, "file_read.log"), apm_time, read_kb)
            _f.add_log(os.path.join(_f.report_dir, "file_write.log"), apm_time, write_kb)
            self._last_logged_sample_time = self.last_sample_time
        return read_kb, write_kb
