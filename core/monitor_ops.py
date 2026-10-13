"""Content-free operational status for the topic monitor.

This module owns two things and nothing else:

* a *diagnostic-only* runtime observation of the newest monitor attempt per
  chat (wall-clock attempt/success times plus a bounded outcome code), and
* pure derivation of an operator-facing status from that observation and the
  existing durable checkpoint evidence.

It never reads the protected WeChat source, never constructs a ``WeChatDB``,
never stats/realpaths a source root, and never opens CAS payload bytes.  It is
not a checkpoint or progress authority: :class:`MonitorStateStore` remains the
sole checkpoint writer and the source inventory remains the sole completeness
authority.  The observation is thrown away/rebuilt freely and never advances a
source cursor.

The status vocabulary is deliberately narrow so a healthy-but-quiet monitor is
not confused with a stalled one:

* ``healthy``  - newest attempt succeeded and is recent.
* ``blocked``  - durable checkpoint evidence (frozen pending batch, active AI
  backoff) or a blocked newest-attempt outcome code.
* ``stale``    - newest attempt is older than the staleness window.
* ``unknown``  - no usable attempt evidence (restart, unreadable observation).
* ``missing``  - no checkpoint file for a configured chat.
* ``disabled`` - the enabled switch is off.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import stat
import time

from .config import DATA_DIR, active_monitor_chats
from .monitor_result import classify_monitor_result, monitor_status
from .monitor_state import (
    MonitorStateError,
    MonitorStateSnapshot,
    MonitorStateStore,
)
from .monitor_source import MonitorSourceError, pending_monitor_source_batch
from .platform import LockMode
from .state_storage import StateFileNotRegular, StateStorage


MONITOR_OPS_SCHEMA = "we-groupchat-obsidian.monitor-ops.v1"
MONITOR_OPS_FILE = os.path.join(DATA_DIR, "monitor_ops.json")

# Default staleness window: three expected intervals, floored at 15 minutes so
# a single slow or skipped timer tick is not reported as stalled.
MIN_STALE_SECONDS = 900.0
STALE_INTERVAL_MULTIPLIER = 3

# Attempt codes that are not real provider attempts.  They never reset the
# diagnostic observation (a restart, a busy worker or an unconfigured monitor).
SKIPPED_ATTEMPT_CODES = frozenset({
    "initialized",
    "missing_topic",
    "monitor_worker_busy",
})


class MonitorOpsError(RuntimeError):
    """A content-free monitor-observation storage/schema failure."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


def _to_float(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if isinstance(value, bool) or not math.isfinite(number) or number < 0:
        return 0.0
    return number


def _to_int(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0


@dataclass(frozen=True)
class MonitorOpsSnapshot:
    data: dict
    existed: bool


_EMPTY_RECORD = {
    "last_attempt_ts": 0.0,
    "last_attempt_code": "",
    "last_attempt_blocked": False,
    "last_success_ts": 0.0,
    "last_success_code": "",
    "progress_ts": 0.0,
    "progress_cursor_count": 0,
}


def _normalize_record(record) -> dict:
    if not isinstance(record, dict):
        raise MonitorOpsError("monitor_ops_corrupt")
    if not isinstance(record.get("last_attempt_blocked"), bool):
        raise MonitorOpsError("monitor_ops_corrupt")
    attempt_code = monitor_status(record.get("last_attempt_code") or "")
    success_code = monitor_status(record.get("last_success_code") or "")
    return {
        "last_attempt_ts": _to_float(record.get("last_attempt_ts")),
        "last_attempt_code": "" if attempt_code == "unknown" else attempt_code,
        "last_attempt_blocked": bool(record.get("last_attempt_blocked")),
        "last_success_ts": _to_float(record.get("last_success_ts")),
        "last_success_code": "" if success_code == "unknown" else success_code,
        "progress_ts": _to_float(record.get("progress_ts")),
        "progress_cursor_count": _to_int(record.get("progress_cursor_count")),
    }


def _empty_payload() -> dict:
    return {"schema": MONITOR_OPS_SCHEMA, "revision": 0, "chats": {}}


def _validate_payload(value) -> dict:
    if not isinstance(value, dict) or value.get("schema") != MONITOR_OPS_SCHEMA:
        raise MonitorOpsError("monitor_ops_corrupt")
    revision = value.get("revision")
    chats = value.get("chats")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or not isinstance(chats, dict)
    ):
        raise MonitorOpsError("monitor_ops_corrupt")
    normalized = {}
    for key, record in chats.items():
        if not isinstance(key, str) or not key:
            raise MonitorOpsError("monitor_ops_corrupt")
        normalized[key] = _normalize_record(record)
    return {"schema": MONITOR_OPS_SCHEMA, "revision": revision, "chats": normalized}


class MonitorOpsStore:
    """Own the diagnostic attempt observation for every monitored chat.

    The store is intentionally separate from :class:`MonitorStateStore`: it is
    not a checkpoint and must never gate source progress.  ``inspect`` is
    strictly read-only and never creates the parent directory or a lock file.
    """

    def __init__(self, path=MONITOR_OPS_FILE, *, file_lock=None, platform_services=None):
        self.path = os.path.abspath(os.path.expanduser(os.fspath(path)))
        self._file_lock = file_lock
        self._storage = StateStorage(self.path, platform_services=platform_services)

    def _lock_service(self):
        if self._file_lock is None:
            self._file_lock = self._storage.services.require("locks")
        return self._file_lock

    def _lock(self):
        try:
            path = self._storage.prepare()
        except StateFileNotRegular as exc:
            raise MonitorOpsError("monitor_ops_not_regular") from exc
        except OSError as exc:
            raise MonitorOpsError("monitor_ops_lock_unavailable") from exc
        try:
            return self._lock_service().acquire(
                path + ".lock", mode=LockMode.EXCLUSIVE, blocking=True,
            )
        except OSError as exc:
            raise MonitorOpsError("monitor_ops_lock_unavailable") from exc

    @staticmethod
    def _unlock(lock_handle) -> None:
        lock_handle.close()

    def _read_file(self):
        try:
            file_stat = os.lstat(self.path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise MonitorOpsError("monitor_ops_unreadable") from exc
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise MonitorOpsError("monitor_ops_not_regular")

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._storage.operational_path(), flags)
        except OSError as exc:
            raise MonitorOpsError("monitor_ops_unreadable") from exc
        try:
            opened_stat = os.fstat(fd)
            if not stat.S_ISREG(opened_stat.st_mode):
                raise MonitorOpsError("monitor_ops_not_regular")
            try:
                with os.fdopen(fd, encoding="utf-8") as handle:
                    fd = -1
                    payload = json.load(handle)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise MonitorOpsError("monitor_ops_corrupt") from exc
            except OSError as exc:
                raise MonitorOpsError("monitor_ops_unreadable") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        return _validate_payload(payload)

    def _write_file(self, payload: dict) -> None:
        encoded = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        try:
            self._storage.write_bytes(encoded)
        except OSError as exc:
            raise MonitorOpsError("monitor_ops_write_failed") from exc

    def inspect(self) -> MonitorOpsSnapshot:
        """Read the observation without creating or migrating anything."""
        if not os.path.lexists(self.path):
            return MonitorOpsSnapshot(data=_empty_payload(), existed=False)
        payload = self._read_file()
        if payload is None:
            return MonitorOpsSnapshot(data=_empty_payload(), existed=False)
        return MonitorOpsSnapshot(data=payload, existed=True)

    def record_attempt(
        self,
        chat_key,
        *,
        ts,
        code,
        blocked,
        progress_ts=0.0,
        progress_cursor_count=0,
    ) -> MonitorOpsSnapshot:
        """Merge one content-free attempt observation into the durable file."""
        key = str(chat_key or "")
        if not key:
            raise MonitorOpsError("monitor_ops_chat_key_invalid")
        attempt_ts = _to_float(ts)
        attempt_code = monitor_status(code)
        if attempt_code == "unknown":
            attempt_code = ""
        lock_fd = self._lock()
        try:
            payload = self._read_file() or _empty_payload()
            chats = dict(payload["chats"])
            record = dict(chats.get(key) or _EMPTY_RECORD)
            if attempt_ts < record["last_attempt_ts"]:
                return MonitorOpsSnapshot(data=payload, existed=True)
            record["last_attempt_ts"] = attempt_ts
            record["last_attempt_code"] = attempt_code
            record["last_attempt_blocked"] = bool(blocked)
            record["progress_ts"] = _to_float(progress_ts)
            record["progress_cursor_count"] = _to_int(progress_cursor_count)
            if not blocked:
                record["last_success_ts"] = attempt_ts
                record["last_success_code"] = attempt_code
            chats[key] = _normalize_record(record)
            payload = {
                "schema": MONITOR_OPS_SCHEMA,
                "revision": int(payload["revision"]) + 1,
                "chats": chats,
            }
            self._write_file(payload)
            return MonitorOpsSnapshot(data=payload, existed=True)
        finally:
            self._unlock(lock_fd)


def bounded_attempt_code(value) -> str:
    """Return a bounded content-free code for a result dict or exception."""
    code = getattr(value, "code", None)
    if isinstance(code, str) and code:
        return monitor_status(code)
    if isinstance(value, dict):
        return monitor_status(value.get("status"))
    return "monitor_runtime_error"


def classify_attempt(value) -> tuple[str, str]:
    """Return ``(code, action)`` for one attempt.

    ``action`` is one of ``success``, ``blocked``, ``skipped`` or ``unknown``.
    Only the newest attempt is consulted; an older success never overrides a
    newer blocked or unknown outcome.
    """
    code = bounded_attempt_code(value)
    if code in SKIPPED_ATTEMPT_CODES:
        return code, "skipped"
    if code == "unknown":
        return code, "unknown"
    candidate = value if isinstance(value, dict) else {"status": code}
    outcome = classify_monitor_result(candidate)
    return (outcome.reason or code) if outcome.action == "blocked" else code, outcome.action


def stale_after_seconds(
    interval_minutes,
    *,
    multiplier: int = STALE_INTERVAL_MULTIPLIER,
    minimum: float = MIN_STALE_SECONDS,
) -> float:
    try:
        minutes = int(interval_minutes)
    except (TypeError, ValueError):
        minutes = 3
    minutes = max(1, minutes)
    return max(float(minimum), minutes * 60.0 * int(multiplier))


def evaluate_chat(
    snapshot: MonitorStateSnapshot,
    observation,
    *,
    enabled: bool,
    now: float,
    stale_after: float,
) -> dict:
    """Return a content-free operational record for one chat checkpoint."""
    record = dict(_EMPTY_RECORD)
    record["state"] = "unknown"
    record["blocked_code"] = ""
    if not enabled:
        record["state"] = "disabled"
        return record
    if not snapshot.existed:
        record["state"] = "missing"
        return record

    data = snapshot.data if isinstance(snapshot.data, dict) else {}
    record["progress_ts"] = _to_float(data.get("last_checked_ts"))
    cursors = data.get("source_cursors")
    if isinstance(cursors, dict):
        record["progress_cursor_count"] = len(cursors)

    blocked_code = ""
    try:
        if pending_monitor_source_batch(data) is not None:
            blocked_code = "monitor_pending_source_batch"
    except MonitorSourceError as exc:
        blocked_code = monitor_status(exc.code)
        if blocked_code == "unknown":
            blocked_code = "monitor_pending_batch_corrupt"

    ai_next_retry_after = _to_float(data.get("ai_next_retry_after"))
    if ai_next_retry_after > now and not blocked_code:
        code = monitor_status(data.get("ai_last_error_code") or "ai_backoff")
        blocked_code = "ai_backoff" if code == "unknown" else code

    obs = observation if isinstance(observation, dict) else {}
    obs_ts = _to_float(obs.get("last_attempt_ts"))
    obs_code = monitor_status(obs.get("last_attempt_code") or "")
    obs_blocked = bool(obs.get("last_attempt_blocked"))
    record["last_attempt_ts"] = obs_ts
    record["last_attempt_code"] = "" if obs_code == "unknown" else obs_code
    record["last_success_ts"] = _to_float(obs.get("last_success_ts"))
    success_code = monitor_status(obs.get("last_success_code") or "")
    record["last_success_code"] = "" if success_code == "unknown" else success_code
    if blocked_code:
        record["state"] = "blocked"
        record["blocked_code"] = blocked_code
    elif obs_ts <= 0 or obs_ts > now + 30:
        record["state"] = "unknown"
    elif obs_blocked:
        record["state"] = "blocked"
        record["blocked_code"] = (
            record["last_attempt_code"] or "monitor_attempt_blocked"
        )
    elif record["last_attempt_code"] == "":
        record["state"] = "unknown"
    elif obs_ts != _to_float(obs.get("last_success_ts")):
        record["state"] = "unknown"
    elif stale_after > 0 and (now - obs_ts) > stale_after:
        record["state"] = "stale"
    else:
        record["state"] = "healthy"
    return record


def summarize(chats: list[dict], *, enabled: bool) -> dict:
    """Collapse per-chat operational records into one operator summary."""
    counts = {
        "healthy": 0,
        "blocked": 0,
        "stale": 0,
        "unknown": 0,
        "missing": 0,
        "disabled": 0,
    }
    blocked_codes = set()
    last_attempt_ts = 0.0
    last_attempt_code = ""
    last_success_ts = 0.0
    progress_ts = 0.0
    for chat in chats:
        state = str(chat.get("state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
        if chat.get("blocked_code"):
            blocked_codes.add(str(chat["blocked_code"]))
        attempt_ts = _to_float(chat.get("last_attempt_ts"))
        if attempt_ts > last_attempt_ts:
            last_attempt_ts = attempt_ts
            last_attempt_code = str(chat.get("last_attempt_code") or "")
        last_success_ts = max(last_success_ts, _to_float(chat.get("last_success_ts")))
        progress_ts = max(progress_ts, _to_float(chat.get("progress_ts")))

    if not chats:
        state = "unconfigured"
    elif not enabled:
        state = "disabled"
    elif counts["blocked"]:
        state = "blocked"
    elif counts["missing"]:
        state = "unknown"
    elif counts["stale"]:
        state = "stale"
    elif counts["unknown"]:
        state = "unknown"
    elif counts["healthy"]:
        state = "healthy"
    else:
        state = "unknown"

    return {
        "enabled": bool(enabled),
        "state": state,
        "counts": counts,
        "blocked_codes": sorted(blocked_codes),
        "last_attempt_ts": last_attempt_ts,
        "last_attempt_code": last_attempt_code,
        "last_attempt_blocked": bool(blocked_codes),
        "last_success_ts": last_success_ts,
        "progress_watermark_ts": progress_ts,
    }


def monitor_operational_status(
    config,
    *,
    state_dir=None,
    ops_path=None,
    now=None,
    stale_window=None,
) -> dict:
    """Inspect existing durable evidence; never touches the protected source."""
    from .monitor import STATE_DIR, state_file_for_chat

    config = config if isinstance(config, dict) else {}
    now = float(now if now is not None else time.time())
    enabled = bool(config.get("monitor_enabled", False))
    if stale_window is None:
        stale_window = stale_after_seconds(config.get("monitor_interval_minutes", 3))
    state_dir = state_dir or STATE_DIR
    ops_path = ops_path or MONITOR_OPS_FILE

    try:
        observations = MonitorOpsStore(ops_path).inspect().data.get("chats") or {}
    except MonitorOpsError:
        observations = {}

    chats = active_monitor_chats(config)
    results = []
    for chat in chats:
        path = state_file_for_chat(chat.get("username", ""), state_dir=state_dir)
        key = os.path.splitext(os.path.basename(path))[0]
        try:
            snapshot = MonitorStateStore(path).inspect()
        except MonitorStateError as exc:
            record = dict(_EMPTY_RECORD)
            record["state"] = "unknown"
            record["blocked_code"] = monitor_status(exc.code)
            results.append(record)
            continue
        observation = observations.get(key) if isinstance(observations, dict) else None
        results.append(evaluate_chat(
            snapshot,
            observation,
            enabled=enabled,
            now=now,
            stale_after=stale_window,
        ))

    summary = summarize(results, enabled=enabled)
    summary["chat_count"] = len(chats)
    summary["stale_after_seconds"] = stale_window
    return summary


def observe_monitor_attempt(state_store, value, *, ops_store=None, now=None):
    """Record a production attempt without changing its business outcome."""
    code, action = classify_attempt(value)
    if action == "skipped":
        return
    try:
        snapshot = state_store.inspect()
        progress = snapshot.data
        key = os.path.splitext(os.path.basename(state_store.path))[0]
        (ops_store or MonitorOpsStore()).record_attempt(
            key, ts=time.time() if now is None else now, code=code,
            blocked=action not in {"progress", "complete"},
            progress_ts=progress.get("last_checked_ts", 0),
            progress_cursor_count=len(progress.get("source_cursors") or {}))
    except (MonitorOpsError, MonitorStateError, OSError, AttributeError):
        # Diagnostics cannot undo committed canonical work or become another
        # progress gate. Missing evidence remains unknown in the next inspect.
        print("[monitor-ops] observation_unavailable")
