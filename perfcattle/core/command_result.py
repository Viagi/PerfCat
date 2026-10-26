"""Structured results for external command execution."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    timed_out: bool = False
    duration_ms: float = 0.0
    error_kind: str | None = None
