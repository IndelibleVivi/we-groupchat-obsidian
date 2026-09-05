"""Daily digest for WeChat monitor knowledge notes and review queue state."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import DATA_DIR, ensure_private_dir, ensure_private_file
from .knowledge import (
    KnowledgeStore,
    _obsidian_link,
    ensure_obsidian_vault,
    safe_obsidian_subdir,
)
from .review_queue import QUEUE_ACTIONABILITIES, ReviewQueue, priority_for_item
from .source_contract import projection_source_lines
from .url_safety import redact_url_for_display, redact_urls_in_text

DAILY_DIGEST_DIR = os.path.join(DATA_DIR, "daily_digests")
DAILY_DIGEST_STATE_FILE = os.path.join(DATA_DIR, "daily_digest_state.json")
DEFAULT_DIGEST_TIME = "21:30"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DIGEST_SECTION_ITEM_LIMIT = 12


def _timezone(name: str | None):
    try:
        return ZoneInfo(str(name or DEFAULT_TIMEZONE))
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def _json_loads(value, default=None):
    try:
        data = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return [] if default is None else default
    return data if data is not None else ([] if default is None else default)


def _parse_digest_time(value: str | None) -> tuple[int, int]:
    text = str(value or DEFAULT_DIGEST_TIME).strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        return 21, 30
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return 21, 30


def _now_dt(config: dict, now_func=time.time) -> datetime:
    return datetime.fromtimestamp(
        now_func(),
        _timezone(config.get("daily_digest_timezone")),
    )


def _day_bounds(now: datetime) -> tuple[float, float]:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def _load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: str, state: dict) -> None:
    ensure_private_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    ensure_private_file(path)


def should_run_daily_digest(state_path: str, config: dict, now_func=time.time) -> bool:
    """Return True when today's digest is due and not yet marked successful."""
    if config.get("daily_digest_enabled") is False:
        return False
    now = _now_dt(config, now_func=now_func)
    hour, minute = _parse_digest_time(config.get("daily_digest_time"))
    if (now.hour, now.minute) < (hour, minute):
        return False
    date_label = now.strftime("%Y-%m-%d")
    state = _load_state(state_path)
    if state.get("last_digest_date") == date_label:
        return False
    return True


def mark_daily_digest_success(state_path: str, config: dict, now_func=time.time) -> None:
    """Mark the local-day digest successful after output has been written."""
    now = _now_dt(config, now_func=now_func)
    state = _load_state(state_path)
    state["last_digest_date"] = now.strftime("%Y-%m-%d")
    state["last_digest_ts"] = now_func()
    _save_state(state_path, state)


def _date_text_bounds(date_label: str) -> tuple[str, str]:
    start = datetime.strptime(date_label, "%Y-%m-%d")
    return (
        start.strftime("%Y-%m-%d 00:00"),
        (start + timedelta(days=1)).strftime("%Y-%m-%d 00:00"),
    )


def source_window_dates(
    config: dict,
    window_start: str = "",
    window_end: str = "",
    fallback_ts: float | None = None,
) -> list[str]:
    """Return every configured-local date touched by a canonical source window."""
    timezone = _timezone(config.get("daily_digest_timezone"))

    def parse(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value or "").strip())
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return parsed.replace(tzinfo=timezone)
        return parsed.astimezone(timezone)

    start = parse(window_start)
    end = parse(window_end)
    if start is not None and end is not None and start <= end:
        current = start.date()
        final = end.date()
        dates = []
        while current <= final:
            dates.append(current.isoformat())
            current += timedelta(days=1)
        return dates

    if fallback_ts is None:
        return []
    try:
        fallback = datetime.fromtimestamp(float(fallback_ts), timezone)
    except (TypeError, ValueError, OSError):
        return []
    return [fallback.strftime("%Y-%m-%d")]


def _topic_rows(
    config: dict,
    start_ts: float,
    end_ts: float,
    date_label: str,
) -> list[dict]:
    db_path = os.path.expanduser(str(config.get("monitor_knowledge_db") or ""))
    if not db_path or not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        start_text, end_text = _date_text_bounds(date_label)
        rows = conn.execute(
            """
            SELECT t.*,
                   MAX(e.created_at) AS latest_event_created_at,
                   MAX(NULLIF(TRIM(e.window_end), '')) AS latest_source_end
            FROM topics t
            JOIN events e ON e.topic_id = t.topic_id
            WHERE (
                    TRIM(COALESCE(e.window_start, '')) <> ''
                AND TRIM(COALESCE(e.window_end, '')) <> ''
                AND e.window_start < ?
                AND e.window_end >= ?
            ) OR (
                    (
                        TRIM(COALESCE(e.window_start, '')) = ''
                     OR TRIM(COALESCE(e.window_end, '')) = ''
                    )
                AND e.created_at >= ?
                AND e.created_at < ?
            )
            GROUP BY t.topic_id
            ORDER BY COALESCE(latest_source_end, t.last_seen) DESC, t.topic_id DESC
            """,
            (end_text, start_text, start_ts, end_ts),
        ).fetchall()
        return [_topic_from_row(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _new_notes_count(
    config: dict,
    start_ts: float,
    end_ts: float,
    date_label: str,
) -> int:
    db_path = os.path.expanduser(str(config.get("monitor_knowledge_db") or ""))
    if not db_path or not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    try:
        start_text, end_text = _date_text_bounds(date_label)
        return int(conn.execute(
            """
            SELECT COUNT(*)
            FROM topics
            WHERE (
                    TRIM(COALESCE(first_seen, '')) <> ''
                AND first_seen >= ?
                AND first_seen < ?
            ) OR (
                    TRIM(COALESCE(first_seen, '')) = ''
                AND created_at >= ?
                AND created_at < ?
            )
            """,
            (start_text, end_text, start_ts, end_ts),
        ).fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _topic_from_row(row: sqlite3.Row) -> dict:
    resources = {
        "files": _json_loads(row["files_json"]),
        "links": [
            redact_url_for_display(link)
            for link in _json_loads(row["links_json"])
        ],
    }
    item = {
        "title": redact_urls_in_text(row["title"]),
        "summary": redact_urls_in_text(row["summary"]),
        "resources": resources,
    }
    return {
        "topic_id": int(row["topic_id"]),
        "title": item["title"],
        "summary": item["summary"],
        "category": redact_urls_in_text(row["category"]),
        "source_chat": redact_urls_in_text(row["source_chat"]),
        "last_seen": row["last_seen"],
        "obsidian_path": row["obsidian_path"],
        "priority": priority_for_item(item),
        "resources": resources,
    }


def _topic_note_refs(config: dict, topic_ids: list[int]) -> dict[int, dict]:
    clean_ids = []
    for value in topic_ids:
        try:
            topic_id = int(value)
        except (TypeError, ValueError):
            continue
        if topic_id > 0 and topic_id not in clean_ids:
            clean_ids.append(topic_id)
    if not clean_ids:
        return {}

    db_path = os.path.expanduser(str(config.get("monitor_knowledge_db") or ""))
    if not db_path or not os.path.exists(db_path):
        return {}

    placeholders = ",".join("?" for _ in clean_ids)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT topic_id, title, obsidian_path FROM topics WHERE topic_id IN ({placeholders})",
            clean_ids,
        ).fetchall()
        return {
            int(row["topic_id"]): {
                "title": redact_urls_in_text(row["title"]),
                "obsidian_path": row["obsidian_path"],
            }
            for row in rows
        }
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _with_current_note_refs(config: dict, items: list[dict]) -> list[dict]:
    refs = _topic_note_refs(config, [item.get("knowledge_topic_id") for item in items])
    if not refs:
        return items

    refreshed = []
    for item in items:
        current = dict(item)
        try:
            topic_id = int(item.get("knowledge_topic_id"))
        except (TypeError, ValueError):
            topic_id = 0
        ref = refs.get(topic_id)
        if ref:
            current["title"] = ref.get("title") or current.get("title")
            current["obsidian_path"] = ref.get("obsidian_path") or current.get("obsidian_path")
        refreshed.append(current)
    return refreshed


def _queue_created_ts(value, tz) -> float:
    try:
        created_at = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return 0
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        created_at = created_at.replace(tzinfo=tz)
    return created_at.timestamp()


def _today_action_items(config: dict, start_ts: float, end_ts: float) -> list[dict]:
    queue = ReviewQueue.from_config(config)
    tz = _timezone(config.get("daily_digest_timezone"))
    items = []
    for item in queue.pending():
        if item.get("actionability") not in QUEUE_ACTIONABILITIES:
            continue
        window_start_ts = _queue_created_ts(item.get("window_start"), tz)
        window_end_ts = _queue_created_ts(item.get("window_end"), tz)
        if window_start_ts or window_end_ts:
            logical_start = window_start_ts or window_end_ts
            logical_end = window_end_ts or window_start_ts
            in_day = logical_start < end_ts and logical_end >= start_ts
        else:
            created_ts = _queue_created_ts(item.get("created_at"), tz)
            in_day = start_ts <= created_ts < end_ts
        if in_day:
            items.append(item)
    return _with_current_note_refs(config, items)


def _short(value: str, limit: int = 180) -> str:
    text = " ".join(redact_urls_in_text(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _note_label(title: str, obsidian_path: str | None = None, limit: int = 160) -> str:
    label = _short(title, limit)
    return _obsidian_link(obsidian_path, label) if obsidian_path else label


def _append_overflow(lines: list[str], items: list[dict], label: str) -> None:
    hidden_count = max(0, len(items) - DIGEST_SECTION_ITEM_LIMIT)
    if hidden_count:
        lines.append(f"- ... 另有 {hidden_count} 条{label}未展开")


def _render_digest(digest: dict) -> str:
    lines = [
        "---",
        *projection_source_lines("daily_digest"),
        "---",
        f"# WeChat Daily Digest - {digest['date']}",
        "",
        f"- Generated: {digest['generated_at']}",
        f"- 今日新增 notes: {digest['new_notes_count']}",
        f"- 今日行动项: {digest['today_action_count']}",
        "",
        "## 今日值得回看",
    ]
    if digest["topics"]:
        for topic in digest["topics"]:
            path = topic.get("obsidian_path") or ""
            title = _note_label(topic.get("title"), path)
            lines.append(
                f"- {topic['priority']} · {title} · "
                f"{topic['source_chat']} / {topic['category']}"
            )
    else:
        lines.append("- None")

    lines.extend(["", "## 今日资源机会"])
    resource_items = [
        item for item in digest["today_action_items"]
        if item.get("actionability") in {"follow_up_resource", "import_resource", "evaluate_reference"}
    ]
    if resource_items:
        for item in resource_items[:DIGEST_SECTION_ITEM_LIMIT]:
            title = _note_label(item.get("title"), item.get("obsidian_path"), 120)
            lines.append(
                f"- {item.get('priority')} · {title} · "
                f"{item.get('id')} · {item.get('actionability')}"
            )
        _append_overflow(lines, resource_items, "今日资源机会")
    else:
        lines.append("- None")

    lines.extend(["", "## 风险与边界"])
    risk_items = [
        item for item in digest["today_action_items"]
        if item.get("actionability") == "review_risk"
    ]
    if risk_items:
        for item in risk_items[:DIGEST_SECTION_ITEM_LIMIT]:
            title = _note_label(item.get("title"), item.get("obsidian_path"), 120)
            lines.append(f"- {item.get('priority')} · {title} · {item.get('id')}")
        _append_overflow(lines, risk_items, "风险与边界")
    else:
        lines.append("- None")

    return "\n".join(lines).rstrip() + "\n"


def _obsidian_digest_root(config: dict) -> tuple[str, str]:
    obsidian_root = os.path.expanduser(str(config.get("monitor_obsidian_root") or "").strip())
    if not obsidian_root:
        return "", ""
    obsidian_subdir = safe_obsidian_subdir(config.get("monitor_obsidian_subdir"))
    relative_root = os.path.join(obsidian_subdir, "Daily Digest")
    return os.path.join(obsidian_root, relative_root), relative_root


def _digest_month(date_label: str) -> str:
    try:
        return datetime.strptime(date_label, "%Y-%m-%d").strftime("%Y-%m")
    except ValueError:
        return ""


def archive_historical_daily_digests(
    config: dict,
    now_func=time.time,
) -> list[tuple[str, str]]:
    """Move root-level Obsidian digests outside the current month into YYYY-MM folders."""
    custom_dir = os.path.expanduser(str(config.get("daily_digest_dir") or "").strip())
    if custom_dir and os.path.abspath(custom_dir) != os.path.abspath(DAILY_DIGEST_DIR):
        return []

    digest_root, _relative_root = _obsidian_digest_root(config)
    if not digest_root or not os.path.isdir(digest_root):
        return []

    current_month = _now_dt(config, now_func=now_func).strftime("%Y-%m")
    moved = []
    suffix = " Daily Digest.md"
    for filename in sorted(os.listdir(digest_root)):
        if not filename.endswith(suffix):
            continue
        date_label = filename[:-len(suffix)]
        month = _digest_month(date_label)
        if not month or month == current_month:
            continue
        source = os.path.join(digest_root, filename)
        if not os.path.isfile(source):
            continue
        destination_dir = os.path.join(digest_root, month)
        destination = os.path.join(destination_dir, filename)
        if os.path.exists(destination):
            continue
        os.makedirs(destination_dir, exist_ok=True)
        os.rename(source, destination)
        moved.append((source, destination))
    return moved


def digest_output_path(
    config: dict,
    date_label: str,
    now_func=time.time,
) -> tuple[str, str]:
    """Return full path and Obsidian-relative path for one daily digest."""
    custom_dir = os.path.expanduser(str(config.get("daily_digest_dir") or "").strip())
    if custom_dir and os.path.abspath(custom_dir) != os.path.abspath(DAILY_DIGEST_DIR):
        return os.path.join(custom_dir, f"{date_label}-daily-digest.md"), ""

    digest_root, relative_root = _obsidian_digest_root(config)
    if digest_root:
        month = _digest_month(date_label)
        current_month = _now_dt(config, now_func=now_func).strftime("%Y-%m")
        if month and month != current_month:
            digest_root = os.path.join(digest_root, month)
            relative_root = os.path.join(relative_root, month)
        filename = f"{date_label} Daily Digest.md"
        obsidian_path = os.path.join(relative_root, filename)
        return os.path.join(digest_root, filename), obsidian_path

    return os.path.join(DAILY_DIGEST_DIR, f"{date_label}-daily-digest.md"), ""


def build_daily_digest(
    config: dict,
    now_func=time.time,
    target_date: str | None = None,
) -> dict:
    now = _now_dt(config, now_func=now_func)
    target = now
    if target_date:
        try:
            target = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=now.tzinfo)
        except ValueError as exc:
            raise ValueError("target_date must use YYYY-MM-DD") from exc
    date_label = target.strftime("%Y-%m-%d")
    start_ts, end_ts = _day_bounds(target)
    topics = _topic_rows(config, start_ts, end_ts, date_label)
    today_action_items = _today_action_items(config, start_ts, end_ts)
    digest = {
        "date": date_label,
        "generated_at": now.strftime("%Y-%m-%d %H:%M %Z"),
        "new_notes_count": _new_notes_count(config, start_ts, end_ts, date_label),
        "today_action_count": len(today_action_items),
        "today_risk_count": sum(
            1 for item in today_action_items
            if item.get("actionability") == "review_risk"
        ),
        "topics": topics[:12],
        "today_action_items": today_action_items,
    }
    digest["pending_review_count"] = 0
    digest["engineering_candidates"] = digest["today_action_items"]
    digest["markdown"] = _render_digest(digest)
    return digest


def _atomic_write_digest(path: str, text: str) -> None:
    """Durably publish one app-owned Digest without truncating the last good file."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def write_daily_digest(
    config: dict,
    now_func=time.time,
    target_date: str | None = None,
) -> dict:
    archive_historical_daily_digests(config, now_func=now_func)
    digest = build_daily_digest(
        config,
        now_func=now_func,
        target_date=target_date,
    )
    path, obsidian_path = digest_output_path(
        config,
        digest["date"],
        now_func=now_func,
    )
    if obsidian_path:
        ensure_obsidian_vault(
            config.get("monitor_obsidian_root"),
            obsidian_subdir=config.get("monitor_obsidian_subdir"),
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
    else:
        ensure_private_dir(os.path.dirname(path))
    _atomic_write_digest(path, digest["markdown"])
    digest["path"] = path
    digest["obsidian_path"] = obsidian_path
    return digest


def refresh_existing_daily_digest(
    config: dict,
    source_ts: float,
    now_func=time.time,
) -> dict | None:
    """Refresh an already-published digest after a late source-day write."""
    source_days = source_window_dates(config, fallback_ts=source_ts)
    refreshed = refresh_existing_daily_digests(
        config,
        source_days,
        now_func=now_func,
    )
    return refreshed[0] if refreshed else None


def refresh_existing_daily_digests(
    config: dict,
    date_labels: list[str],
    now_func=time.time,
) -> list[dict]:
    """Refresh only Daily Digests that have already been published."""
    archive_historical_daily_digests(config, now_func=now_func)
    refreshed = []
    for date_label in sorted(set(date_labels)):
        path, _obsidian_path = digest_output_path(
            config,
            date_label,
            now_func=now_func,
        )
        if not os.path.isfile(path):
            continue
        refreshed.append(write_daily_digest(
            config,
            now_func=now_func,
            target_date=date_label,
        ))
    return refreshed


def refresh_pending_daily_digests(config: dict, now_func=time.time) -> dict:
    """Repair existing Digest projections from a durable event journal.

    Canonical events and invalidations commit in one SQLite transaction. A
    crash may leave Markdown stale, but it cannot erase the repair trigger.
    Only a successfully rewritten prefix is acknowledged.
    """
    store = KnowledgeStore.from_config(config)
    if not os.path.isfile(store.db_path):
        return {
            "state": "idle",
            "affected_dates": [],
            "written_dates": [],
            "acknowledged_changes": 0,
        }
    batch = store.pending_daily_digest_changes()
    changes = list(batch.get("changes") or [])
    if not changes:
        return {
            "state": "idle",
            "affected_dates": [],
            "written_dates": [],
            "acknowledged_changes": 0,
        }
    dates = []
    for change in changes:
        fallback = change.get("fallback_timestamp")
        try:
            if float(fallback or 0) <= 0:
                fallback = change.get("created_at")
        except (TypeError, ValueError):
            fallback = change.get("created_at")
        dates.extend(source_window_dates(
            config,
            str(change.get("window_start") or ""),
            str(change.get("window_end") or ""),
            fallback_ts=fallback,
        ))
    affected_dates = sorted(set(dates))
    expected_existing = []
    for date_label in affected_dates:
        path, _ = digest_output_path(config, date_label, now_func=now_func)
        if os.path.isfile(path):
            expected_existing.append(date_label)
    try:
        refreshed = refresh_existing_daily_digests(
            config,
            affected_dates,
            now_func=now_func,
        )
    except Exception as exc:
        return {
            "state": "deferred",
            "affected_dates": affected_dates,
            "written_dates": [],
            "acknowledged_changes": 0,
            "error_code": type(exc).__name__,
        }
    written_dates = [item["date"] for item in refreshed]
    if written_dates != expected_existing:
        return {
            "state": "deferred",
            "affected_dates": affected_dates,
            "written_dates": written_dates,
            "acknowledged_changes": 0,
            "error_code": "daily_digest_projection_changed",
        }
    try:
        acknowledged = store.ack_daily_digest_changes(
            int(batch.get("cutoff_change_id") or 0)
        )
    except Exception as exc:
        return {
            "state": "deferred",
            "affected_dates": affected_dates,
            "written_dates": written_dates,
            "acknowledged_changes": 0,
            "error_code": type(exc).__name__,
        }
    return {
        "state": "refreshed",
        "affected_dates": affected_dates,
        "written_dates": written_dates,
        "acknowledged_changes": acknowledged,
    }


def notification_summary(digest: dict) -> tuple[str, str]:
    subtitle = (
        f"{digest['date']} · {digest['new_notes_count']} notes · "
        f"{digest.get('today_action_count', 0)} actions · "
        f"{digest.get('today_risk_count', 0)} risk"
    )
    message = f"Digest: {digest.get('path', '')}"
    return subtitle, message
