#!/usr/bin/env python3
"""Audit and safely drain missed monitor messages into Obsidian.

The default mode is read-only. ``--apply`` pauses the managed LaunchAgent,
backs up canonical SQLite/state, drains each configured chat through the normal
TopicMonitor path, rebuilds affected projections, validates canonical storage,
restores the LaunchAgent to its previous loaded state, and writes a content-free
reconciliation receipt for complete, partial, or failed runs.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.config import DATA_DIR, active_monitor_chats, ensure_private_dir, ensure_private_file, load_config
from core.app_runtime import AppAlreadyRunning, AppInstanceLock
from core.daily_digest import _timezone, digest_output_path, write_daily_digest
from core.key_extractor import get_cached_keys
from core.knowledge import KNOWLEDGE_DB, KnowledgeStore, build_message_hash
from core.launch_agent import launch_agent_report
from core.monitor import TopicMonitor, load_state, state_file_for_chat
from core.monitor_source import (
    MonitorSourceError,
    read_monitor_source_batch,
    supports_monitor_source_cursors,
)
from core.wechat_db import WeChatDB

RECEIPTS_DIR = Path(DATA_DIR) / "catch_up_receipts"


def _pending_messages(db, username: str, state_path: str, limit: int) -> dict:
    state = load_state(state_path)
    try:
        checkpoint = float(state.get("last_checked_ts") or 0)
    except (TypeError, ValueError):
        checkpoint = 0
    if supports_monitor_source_cursors(db):
        try:
            batch = read_monitor_source_batch(
                db,
                username,
                state,
                raw_limit=max(1, int(limit)) + 1,
            )
        except MonitorSourceError as exc:
            return {
                "checkpoint": checkpoint,
                "count": None,
                "capped": False,
                "reason": exc.code,
                "source_eof": False,
            }
        return {
            "checkpoint": checkpoint,
            "count": min(batch.raw_count, limit),
            "capped": batch.raw_count > limit or not batch.source_eof,
            "reason": "",
            "source_eof": batch.source_eof,
            "source_inventory_digest": batch.inventory_digest,
        }
    if checkpoint <= 0:
        return {"checkpoint": 0, "count": None, "capped": False, "reason": "missing_checkpoint"}

    try:
        hash_ts = float(state.get("last_checked_message_hash_ts") or -1)
    except (TypeError, ValueError):
        hash_ts = -1
    processed_hashes = {
        str(value) for value in (state.get("last_checked_message_hashes") or []) if value
    } if hash_ts == checkpoint else set()
    rows = db.get_messages(
        username,
        since_ts=max(0, checkpoint - 0.001),
        limit=limit + 1,
        page_forward=True,
    )
    pending = [
        row for row in rows
        if float(row.get("timestamp") or 0) > checkpoint
        or (
            float(row.get("timestamp") or 0) == checkpoint
            and build_message_hash([row]) not in processed_hashes
        )
    ]
    return {
        "checkpoint": checkpoint,
        "count": min(len(pending), limit),
        "capped": len(pending) > limit,
        "reason": "",
    }


def audit_pending(db, chats: list[dict], limit: int = 100000) -> list[dict]:
    report = []
    for chat in chats:
        item = _pending_messages(
            db,
            chat["username"],
            state_file_for_chat(chat["username"]),
            limit,
        )
        item.update({"username": chat["username"], "name": chat["name"]})
        report.append(item)
    return report


def _backup_sqlite(source: Path, destination: Path) -> None:
    if not source.is_file():
        return
    source_conn = sqlite3.connect(str(source))
    destination_conn = sqlite3.connect(str(destination))
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()
    ensure_private_file(destination)


def backup_runtime_state(
    config: dict,
    chats: list[dict],
    backup_base: str | os.PathLike[str] | None = None,
) -> Path:
    base = Path(backup_base or (Path(DATA_DIR) / "backups" / "monitor-catch-up"))
    ensure_private_dir(base)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path(tempfile.mkdtemp(prefix=f"{stamp}-", dir=str(base)))
    ensure_private_dir(root)

    db_path = Path(os.path.expanduser(str(config.get("monitor_knowledge_db") or KNOWLEDGE_DB)))
    _backup_sqlite(db_path, root / "monitor_knowledge.db")

    states_dir = root / "monitor_state"
    ensure_private_dir(states_dir)
    backed_up_states = []
    for chat in chats:
        source = Path(state_file_for_chat(chat["username"]))
        if source.is_file():
            destination = states_dir / source.name
            shutil.copy2(source, destination)
            ensure_private_file(destination)
            backed_up_states.append(source.name)

    manifest = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "knowledge_db_backed_up": db_path.is_file(),
        "state_files": sorted(backed_up_states),
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ensure_private_file(manifest_path)
    return root


def _stop_launch_agent(record) -> None:
    result = subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(record.plist_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "launchctl bootout failed")


def _restore_launch_agent(record) -> None:
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(record.plist_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "service already loaded" not in result.stderr.lower():
        raise RuntimeError(result.stderr.strip() or "launchctl bootstrap failed")
    subprocess.run(
        ["launchctl", "enable", f"gui/{os.getuid()}/{record.label}"],
        capture_output=True,
        text=True,
        check=False,
    )


def drain_monitors(
    monitor_rows: list[tuple[dict, TopicMonitor]],
    max_pages_per_chat: int,
    max_minutes: int,
    monotonic=time.monotonic,
) -> dict:
    deadline = monotonic() + max_minutes * 60
    page_counts = Counter()
    statuses = Counter()
    complete = set()
    blocked = {}
    affected_dates = set()
    per_chat = {
        chat["username"]: {
            "pages": 0,
            "statuses": {},
            "event_ids": [],
            "affected_dates": [],
            "outcome": "pending",
            "blocked_reason": "",
        }
        for chat, _monitor in monitor_rows
    }

    def block(username: str, reason: str) -> None:
        blocked[username] = reason
        per_chat[username]["outcome"] = "blocked"
        per_chat[username]["blocked_reason"] = reason

    while len(complete) + len(blocked) < len(monitor_rows):
        made_progress = False
        for chat, monitor in monitor_rows:
            username = chat["username"]
            if username in complete or username in blocked:
                continue
            if monotonic() >= deadline:
                block(username, "time_limit")
                continue
            if page_counts[username] >= max_pages_per_chat:
                block(username, "page_limit")
                continue
            try:
                result = monitor.check_once()
            except Exception as exc:
                block(username, f"{type(exc).__name__}: {exc}")
                continue

            status = str(result.get("status") or "unknown")
            statuses[status] += 1
            chat_statuses = Counter(per_chat[username]["statuses"])
            chat_statuses[status] += 1
            per_chat[username]["statuses"] = dict(chat_statuses)
            if status == "no_messages":
                if result.get("source_eof") is True:
                    complete.add(username)
                    per_chat[username]["outcome"] = "complete"
                else:
                    block(username, "source_eof_unverified")
                continue
            if status in {
                "ai_backoff",
                "initialized",
                "missing_topic",
                "monitor_state_conflict",
                "monitor_source_cursors_corrupt",
                "source_generation_changed",
                "source_inventory_incomplete",
                "source_inventory_invalid",
                "source_inventory_unavailable",
                "source_shard_unavailable",
                "source_message_decode_failed",
            }:
                block(username, status)
                continue

            page_counts[username] += 1
            per_chat[username]["pages"] += 1
            made_progress = True
            event_written = bool(result.get("knowledge_event_written")) or result.get("knowledge_event_id") is not None
            if event_written:
                result_dates = result.get("affected_dates") or []
                affected_dates.update(result_dates)
                per_chat[username]["affected_dates"] = sorted(set(
                    per_chat[username]["affected_dates"] + list(result_dates)
                ))
                if result.get("knowledge_event_id") is not None:
                    per_chat[username]["event_ids"].append(int(result["knowledge_event_id"]))

        if not made_progress and len(complete) + len(blocked) < len(monitor_rows):
            for chat, _monitor in monitor_rows:
                username = chat["username"]
                if username not in complete and username not in blocked:
                    block(username, "no_progress")

    return {
        "complete": sorted(complete),
        "blocked": blocked,
        "pages": dict(page_counts),
        "statuses": dict(statuses),
        "affected_dates": sorted(affected_dates),
        "per_chat": per_chat,
    }


def _checkpoint_for_chat(username: str) -> float:
    try:
        return float(load_state(state_file_for_chat(username)).get("last_checked_ts") or 0)
    except (TypeError, ValueError):
        return 0


def _safe_projection_receipt(projections: dict | None) -> dict:
    projections = projections or {}
    indexes = projections.get("indexes") or {}
    return {
        "indexes": {
            key: int(indexes.get(key) or 0)
            for key in (
                "written_count",
                "removed_generated_count",
                "removed_archive_count",
                "skipped_count",
            )
        },
        "digests": [
            {
                "date": item.get("date", ""),
                "notes": int(item.get("notes") or 0),
                "actions": int(item.get("actions") or 0),
            }
            for item in (projections.get("digests") or [])
        ],
    }


def _receipt_error_code(value: str) -> str:
    return str(value or "").split(":", 1)[0].strip()[:80]


def build_reconciliation_receipt(
    *,
    run_id: str,
    started_at: str,
    chats: list[dict],
    audit: list[dict],
    checkpoints_after: dict[str, float],
    result: dict | None,
    projections: dict | None,
    validation: dict | None,
    backup_path: Path | None,
    launch_agent: dict,
    transaction_error: str,
    outcome_override: str = "",
) -> dict:
    """Build a content-free receipt for page-level partial commit reconciliation."""
    result = result or {}
    audit_by_username = {item["username"]: item for item in audit}
    details = result.get("per_chat") or {}
    chat_rows = []
    checkpoint_changed = False
    event_ids = []
    for chat in chats:
        username = chat["username"]
        before = float((audit_by_username.get(username) or {}).get("checkpoint") or 0)
        after = float(checkpoints_after.get(username) or 0)
        checkpoint_changed = checkpoint_changed or before != after
        detail = details.get(username) or {}
        event_ids.extend(detail.get("event_ids") or [])
        chat_rows.append({
            "state_id": Path(state_file_for_chat(username)).stem,
            "checkpoint_before": before,
            "checkpoint_after": after,
            "pending_before": (audit_by_username.get(username) or {}).get("count"),
            "pending_count_capped": bool((audit_by_username.get(username) or {}).get("capped")),
            "pages": int(detail.get("pages") or 0),
            "statuses": dict(detail.get("statuses") or {}),
            "event_ids": sorted(set(int(value) for value in (detail.get("event_ids") or []))),
            "affected_dates": sorted(set(detail.get("affected_dates") or [])),
            "outcome": detail.get("outcome") or "not_started",
            "blocked_reason": _receipt_error_code(detail.get("blocked_reason") or ""),
        })

    validation_ok = bool(validation and validation.get("ok"))
    was_loaded = bool(launch_agent.get("was_loaded"))
    restore_attempted = bool(launch_agent.get("restore_attempted"))
    restored = launch_agent.get("restored")
    restore_pending = was_loaded and not restore_attempted
    restore_failed = was_loaded and restore_attempted and restored is not True
    launch_ok = not was_loaded or (restore_attempted and restored is True)
    canonical_complete = bool(
        result
        and not result.get("blocked")
        and validation_ok
        and not transaction_error
    )
    complete = canonical_complete and launch_ok
    progressed = checkpoint_changed or any(row["pages"] for row in chat_rows)
    if outcome_override in {"menu_app_active", "maintenance_lock_busy"}:
        state = "failed"
        outcome = outcome_override
    elif restore_pending and canonical_complete:
        state = "partial"
        outcome = "drain_complete_restore_pending"
    elif restore_failed:
        state = "partial" if canonical_complete or progressed or projections else "failed"
        outcome = "launch_agent_restore_failed"
    elif complete:
        state = "complete"
        outcome = outcome_override or "drained"
    elif progressed or projections:
        state = "partial"
        outcome = outcome_override or "resume_required"
    else:
        state = "failed"
        outcome = outcome_override or "no_progress"

    validation_receipt = {
        key: validation.get(key)
        for key in (
            "ok",
            "quick_check",
            "integrity_check",
            "topics",
            "events",
            "fts",
            "duplicate_hash_groups",
            "error",
        )
        if validation and key in validation
    }
    return {
        "schema_version": 1,
        "run_id": run_id,
        "operation": "monitor_catch_up",
        "commit_policy": "page_level_partial",
        "state": state,
        "outcome": outcome,
        "finalization": (
            "restore_pending"
            if restore_pending
            else "restore_failed"
            if restore_failed
            else "finalized"
        ),
        "resume_supported": (
            state == "partial"
            and outcome == "resume_required"
            and validation_ok
            and launch_ok
        ),
        "started_at": started_at,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "backup": {
            "path": str(backup_path) if backup_path else "",
            "scope": ["knowledge_sqlite", "monitor_checkpoints"] if backup_path else [],
            "full_rollback": False,
        },
        "chats": chat_rows,
        "canonical": {
            "event_ids": sorted(set(int(value) for value in event_ids)),
            "event_count": len(set(event_ids)),
            "affected_dates": sorted(set(result.get("affected_dates") or [])),
        },
        "projections": _safe_projection_receipt(projections),
        "validation": validation_receipt,
        "launch_agent": {
            **launch_agent,
            "error": _receipt_error_code(launch_agent.get("error") or ""),
        },
        "transaction_error": _receipt_error_code(transaction_error),
    }


def write_reconciliation_receipt(
    receipt: dict,
    receipts_dir: str | os.PathLike[str] = RECEIPTS_DIR,
) -> Path:
    root = Path(receipts_dir)
    ensure_private_dir(root)
    path = root / f"{receipt['run_id']}.json"
    temp_fd = -1
    temp_path = ""
    try:
        temp_fd, temp_path = tempfile.mkstemp(
            prefix=f".{receipt['run_id']}.",
            suffix=".tmp",
            dir=root,
        )
        try:
            os.fchmod(temp_fd, 0o600)
        except OSError:
            pass
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            temp_fd = -1
            json.dump(receipt, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = ""
        ensure_private_file(path)
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    return path


def rebuild_projections(config: dict, affected_dates: list[str]) -> dict:
    index_result = KnowledgeStore.from_config(config).write_date_indexes()
    today = datetime.now(_timezone(config.get("daily_digest_timezone"))).strftime("%Y-%m-%d")
    digests = []
    for date_label in sorted(set(affected_dates)):
        digest_path, _ = digest_output_path(config, date_label)
        if date_label < today or os.path.isfile(digest_path):
            digest = write_daily_digest(config, target_date=date_label)
            digests.append({
                "date": date_label,
                "notes": digest["new_notes_count"],
                "actions": digest.get("today_action_count", 0),
                "path": digest["path"],
            })
    return {"indexes": index_result, "digests": digests}


def validate_knowledge_db(config: dict) -> dict:
    db_path = Path(os.path.expanduser(str(config.get("monitor_knowledge_db") or KNOWLEDGE_DB)))
    if not db_path.is_file():
        return {"ok": False, "error": "knowledge_db_missing"}
    conn = sqlite3.connect(str(db_path))
    try:
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        integrity_check = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        topics = int(conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0])
        events = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        fts = int(conn.execute("SELECT COUNT(*) FROM topic_fts").fetchone()[0])
        duplicates = int(conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT COALESCE(NULLIF(TRIM(source_chat_username), ''), source_chat), message_hash
                FROM events
                WHERE TRIM(message_hash) <> ''
                GROUP BY COALESCE(NULLIF(TRIM(source_chat_username), ''), source_chat), message_hash
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0])
    finally:
        conn.close()
    return {
        "ok": quick_check == "ok" and integrity_check == "ok" and topics == fts and duplicates == 0,
        "quick_check": quick_check,
        "integrity_check": integrity_check,
        "topics": topics,
        "events": events,
        "fts": fts,
        "duplicate_hash_groups": duplicates,
    }


def _print_audit(rows: list[dict]) -> None:
    print("补跑审计（只读）")
    total = 0
    unknown = False
    for row in rows:
        if row["count"] is None:
            unknown = True
            value = "无法补跑：缺少 checkpoint"
        else:
            total += row["count"]
            value = f"{row['count']}{'+' if row['capped'] else ''} 条待处理"
        print(f"  {row['name']}: {value}")
    if not unknown:
        print(f"合计: {total}{'+' if any(row['capped'] for row in rows) else ''} 条待处理")


def _load_runtime() -> tuple[dict, list[dict], WeChatDB]:
    config = load_config()
    chats = active_monitor_chats(config)
    if not chats:
        raise RuntimeError("没有配置监控群聊")
    keys = get_cached_keys()
    if not keys:
        raise RuntimeError("没有数据库 key cache；先运行 ./启动.command")
    db_dir = os.path.expanduser(str(config.get("db_dir") or ""))
    if not db_dir or not os.path.isdir(db_dir):
        raise RuntimeError("WeChat db_dir 不可用；先运行 ./启动.command")
    db = WeChatDB.for_runtime(db_dir, keys)
    if hasattr(db, "refresh_cache_view"):
        db.refresh_cache_view()
    return config, chats, db


def _print_projection_summary(projections):
    """Print the real producer contract; do not invent event/topic counters."""
    if projections:
        print(f"  date indexes: {projections['indexes'].get('written_count', 0)}")
        for digest in projections["digests"]:
            print(
                f"  Digest {digest['date']}: {digest['notes']} notes / "
                f"{digest['actions']} actions"
            )


def apply_catch_up(config: dict, chats: list[dict], db, args) -> int:
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
    audit = audit_pending(db, chats, limit=args.audit_limit)
    _print_audit(audit)
    missing = [row["name"] for row in audit if row["count"] is None]
    if missing:
        receipt = build_reconciliation_receipt(
            run_id=run_id,
            started_at=started_at,
            chats=chats,
            audit=audit,
            checkpoints_after={chat["username"]: _checkpoint_for_chat(chat["username"]) for chat in chats},
            result=None,
            projections=None,
            validation=None,
            backup_path=None,
            launch_agent={
                "inspected": False,
                "was_loaded": False,
                "restore_attempted": False,
                "restored": None,
                "error": "",
            },
            transaction_error="missing_checkpoint",
            outcome_override="missing_checkpoint",
        )
        receipt_path = write_reconciliation_receipt(receipt)
        print("拒绝写入：这些群没有可恢复 checkpoint：" + "、".join(missing))
        print(f"reconciliation receipt: {receipt_path}")
        return 2
    if all(row["count"] == 0 for row in audit):
        receipt = build_reconciliation_receipt(
            run_id=run_id,
            started_at=started_at,
            chats=chats,
            audit=audit,
            checkpoints_after={chat["username"]: _checkpoint_for_chat(chat["username"]) for chat in chats},
            result=None,
            projections=None,
            validation=None,
            backup_path=None,
            launch_agent={
                "inspected": False,
                "was_loaded": False,
                "restore_attempted": False,
                "restored": None,
                "error": "",
            },
            transaction_error="",
            outcome_override="no_op",
        )
        receipt["state"] = "complete"
        receipt_path = write_reconciliation_receipt(receipt)
        print("已经追到当前可读数据尾部；没有执行写入、备份或 LaunchAgent 切换。")
        print(f"reconciliation receipt: {receipt_path}")
        return 0

    record = None
    original_status = None
    backup_path = None
    restore_error = ""
    transaction_error = ""
    receipt_error = ""
    receipt_path = None
    receipt = None
    provisional_receipt = None
    result = None
    projections = None
    validation = None
    checkpoints_after = {}
    instance_lock = None
    outcome_override = ""
    launch_agent = {
        "inspected": False,
        "was_loaded": False,
        "restore_attempted": False,
        "restored": None,
        "error": "",
    }

    def current_receipt():
        return build_reconciliation_receipt(
            run_id=run_id,
            started_at=started_at,
            chats=chats,
            audit=audit,
            checkpoints_after=checkpoints_after,
            result=result,
            projections=projections,
            validation=validation,
            backup_path=backup_path,
            launch_agent=launch_agent,
            transaction_error=transaction_error,
            outcome_override=outcome_override,
        )

    try:
        record, original_status = launch_agent_report(PROJECT_DIR)
        launch_agent["inspected"] = True
        launch_agent["was_loaded"] = bool(original_status.loaded)
        if original_status.loaded:
            print(f"暂停 LaunchAgent: {record.label}")
            _stop_launch_agent(record)
        try:
            instance_lock = AppInstanceLock().acquire()
        except AppAlreadyRunning:
            transaction_error = "menu_app_active"
            outcome_override = "menu_app_active"
        if instance_lock is not None:
            backup_path = backup_runtime_state(config, chats)
            print(f"局部恢复备份（SQLite + checkpoints）: {backup_path}")

            monitors = []
            for chat in chats:
                chat_config = dict(config)
                chat_config["monitor_chat_username"] = chat["username"]
                chat_config["monitor_chat_display_name"] = chat["name"]
                monitors.append((chat, TopicMonitor(
                    db,
                    chat_config,
                    state_file=state_file_for_chat(chat["username"]),
                )))
            result = drain_monitors(
                monitors,
                max_pages_per_chat=args.max_pages_per_chat,
                max_minutes=args.max_minutes,
            )
            projections = rebuild_projections(config, result["affected_dates"])
            validation = validate_knowledge_db(config)
    except Exception as exc:
        transaction_error = f"{type(exc).__name__}: {exc}"
    try:
        checkpoints_after = {
            chat["username"]: _checkpoint_for_chat(chat["username"])
            for chat in chats
        }
        provisional_receipt = current_receipt()
        receipt = provisional_receipt
        try:
            receipt_path = write_reconciliation_receipt(provisional_receipt)
        except Exception as exc:
            receipt_error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        receipt_error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if instance_lock is not None:
                instance_lock.release()
        finally:
            if original_status and original_status.loaded:
                launch_agent["restore_attempted"] = True
                try:
                    _restore_launch_agent(record)
                    launch_agent["restored"] = True
                except Exception as exc:
                    restore_error = f"{type(exc).__name__}: {exc}"
                    launch_agent["restored"] = False
                    launch_agent["error"] = restore_error

    if original_status and original_status.loaded:
        try:
            final_receipt = current_receipt()
            final_path = write_reconciliation_receipt(final_receipt)
        except Exception as exc:
            receipt_error = f"{type(exc).__name__}: {exc}"
        else:
            receipt = final_receipt
            receipt_path = final_path
            receipt_error = ""

    if receipt is None:
        print("补跑结果不可用：reconciliation_receipt_failed")
        if restore_error:
            print(f"LaunchAgent restore error: {_receipt_error_code(restore_error)}")
        return 1

    print("\n补跑结果")
    print(f"  state: {receipt['state']} · outcome: {receipt['outcome']}")
    if result:
        print(f"  pages: {sum(result['pages'].values())} · statuses: {result['statuses']}")
        print(f"  完成群聊: {len(result['complete'])}/{len(chats)}")
        if result["blocked"]:
            print(f"  未完成: {result['blocked']}")
    _print_projection_summary(projections)
    if validation:
        print(
            "  canonical: "
            f"quick_check={validation.get('quick_check')} integrity_check={validation.get('integrity_check')} "
            f"topics={validation.get('topics')} events={validation.get('events')} "
            f"fts={validation.get('fts')} duplicates={validation.get('duplicate_hash_groups')}"
        )
    if backup_path:
        print(f"  partial recovery backup (SQLite + checkpoints only): {backup_path}")
    if transaction_error:
        print(f"  transaction error: {transaction_error}")
    if restore_error:
        print(f"  LaunchAgent restore error: {restore_error}")
    if receipt_path:
        print(f"  reconciliation receipt: {receipt_path}")
    if receipt_error:
        print(f"  reconciliation receipt error: {receipt_error}")

    success = receipt["state"] == "complete" and not receipt_error and not restore_error
    return 0 if success else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit or safely catch up missed monitor notes.")
    parser.add_argument("--apply", action="store_true", help="Pause, back up, write notes, rebuild projections, and restore the monitor.")
    parser.add_argument("--audit-limit", type=int, default=100000, help="Maximum pending messages counted per chat in audit mode.")
    parser.add_argument("--max-pages-per-chat", type=int, default=100, help="Safety bound for TopicMonitor pages per chat.")
    parser.add_argument("--max-minutes", type=int, default=45, help="Transaction runtime safety bound.")
    args = parser.parse_args(argv)
    args.audit_limit = max(1, args.audit_limit)
    args.max_pages_per_chat = max(1, args.max_pages_per_chat)
    args.max_minutes = max(1, args.max_minutes)
    try:
        config, chats, db = _load_runtime()
        if args.apply:
            return apply_catch_up(config, chats, db, args)
        _print_audit(audit_pending(db, chats, limit=args.audit_limit))
        print("\n只做了 audit，没有写入。执行补跑：./launchers/补跑遗漏笔记.command --apply")
        return 0
    except Exception as exc:
        print(f"补跑入口不可用: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
