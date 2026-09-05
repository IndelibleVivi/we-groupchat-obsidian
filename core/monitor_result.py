"""Shared interpretation of monitor outcomes; unknown results never progress.

This module does not run work, mutate cursors, or infer commit success from an
arbitrary payload. The progress codes below belong to TopicMonitor's existing
post-commit result contract. New codes stop catch-up until explicitly admitted.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Literal


_PROGRESS_CODES = frozenset({
    "no_match", "notified", "duplicate", "cooldown", "source_advanced_no_visible",
})
_STATUS_RE = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")


def monitor_status(value) -> str:
    """Return a bounded content-free code, never a chat/error message."""
    if not isinstance(value, str) or _STATUS_RE.fullmatch(value) is None:
        return "unknown"
    return value


@dataclass(frozen=True)
class MonitorOutcome:
    code: str
    action: Literal["progress", "complete", "blocked"]
    reason: str = ""


def classify_monitor_result(result) -> MonitorOutcome:
    """Positive progress allowlist shared by operational consumers."""
    if not isinstance(result, Mapping):
        return MonitorOutcome("unknown", "blocked", "monitor_result_invalid")
    code = monitor_status(result.get("status"))
    if code == "no_messages":
        if result.get("source_eof") is True:
            return MonitorOutcome(code, "complete")
        return MonitorOutcome(code, "blocked", "source_eof_unverified")
    if code in _PROGRESS_CODES:
        return MonitorOutcome(code, "progress")
    return MonitorOutcome(code, "blocked", code)
