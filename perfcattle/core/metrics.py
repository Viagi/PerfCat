"""Unified metric samples and quality metadata."""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class MetricStatus(str, Enum):
    OK = "ok"
    CACHED = "cached"
    UNSUPPORTED = "unsupported"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"
    INVALID = "invalid"
    TRUNCATED = "truncated"
    MISSING = "missing"


def finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass
class MetricSample:
    metric: str
    value: float | int | None = None
    unit: str | None = None
    status: MetricStatus = MetricStatus.OK
    source: str | None = None
    scope: str | None = None
    aggregation: str | None = None
    sample_epoch_ms: int | None = None
    sample_monotonic_ns: int | None = None
    collected_epoch_ms: int | None = None
    latency_ms: float | None = None
    cached: bool = False
    stale_age_ms: float | None = None
    error: str | None = None

    def __post_init__(self):
        if not isinstance(self.status, MetricStatus):
            self.status = MetricStatus(self.status)
        if self.status not in (MetricStatus.OK, MetricStatus.CACHED):
            self.value = None
        elif self.value is None or not finite_number(self.value):
            self.value = None
            self.status = MetricStatus.INVALID
        if self.status is MetricStatus.CACHED:
            self.cached = True
        else:
            self.cached = False

    @classmethod
    def ok(cls, metric, value, unit=None, source=None, scope=None,
           aggregation=None, sample_epoch_ms=None, sample_monotonic_ns=None, **kwargs):
        if not finite_number(value):
            return cls(metric=metric, value=None, unit=unit, status=MetricStatus.INVALID,
                       source=source, scope=scope, aggregation=aggregation,
                       sample_epoch_ms=sample_epoch_ms, sample_monotonic_ns=sample_monotonic_ns,
                       error="value must be a finite number", **kwargs)
        now_epoch_ms = int(time.time() * 1000)
        now_monotonic_ns = time.monotonic_ns()
        return cls(metric=metric, value=value, unit=unit, status=MetricStatus.OK,
                   source=source, scope=scope, aggregation=aggregation,
                   sample_epoch_ms=now_epoch_ms if sample_epoch_ms is None else sample_epoch_ms,
                   sample_monotonic_ns=now_monotonic_ns if sample_monotonic_ns is None else sample_monotonic_ns,
                   collected_epoch_ms=now_epoch_ms, **kwargs)

    def to_dict(self) -> dict:
        result = asdict(self)
        result["status"] = self.status.value
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "MetricSample":
        if not isinstance(data, dict):
            raise ValueError("metric sample must be an object")
        allowed = {field for field in cls.__dataclass_fields__}
        payload = {key: value for key, value in data.items() if key in allowed}
        if "metric" not in payload or not isinstance(payload["metric"], str):
            raise ValueError("metric is required")
        payload["status"] = MetricStatus(payload.get("status", MetricStatus.OK.value))
        if payload["status"] in (MetricStatus.OK, MetricStatus.CACHED):
            if payload.get("value") is not None and not finite_number(payload["value"]):
                raise ValueError("value must be finite")
        return cls(**payload)


@dataclass
class MetricQuality:
    total: int = 0
    valid: int = 0
    invalid: int = 0
    cached: int = 0
    missing: int = 0
    first_epoch_ms: int | None = None
    last_epoch_ms: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def parse_metric_line(line: str) -> MetricSample | None:
    try:
        data = json.loads(line)
        return MetricSample.from_dict(data)
    except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
        return None
