#!/usr/bin/env python3
"""Print a privacy-safe health check for the local we-groupchat-obsidian monitor."""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.config import DATA_DIR, active_monitor_chats, load_config
from core.attachment_archive import AttachmentArchive
from core.attachment_backup import AttachmentBackup
from core.google_drive_auth import GoogleDriveOAuth
from core.google_drive_file_sync import GoogleDriveFileSync
from core.knowledge import TAXONOMY_PROFILES
from core.link_preview import LINK_PREVIEW_STATE
from core.key_extractor import (
    EXTRACT_LOG,
    check_new_databases,
    get_cached_keys,
    is_wechat_running,
    is_wechat_signed,
    process_lookup_available,
)
from core.keychain import load_key
from core.daily_digest import digest_output_path
from core.review_queue import QUEUE_DIR, ReviewQueue
from core.relation_audit import audit_relations
from core.launch_agent import (
    launch_agent_report,
    launch_agent_status,
    parse_launch_agent_status,
)
from core.notification_identity import notification_identity_status_for_launch_agent
from core.project_identity import SOURCE_GUARD_LAUNCH_AGENT_LABEL
from core.monitor import STATE_DIR as MONITOR_STATE_DIR, state_file_for_chat
from core.monitor_state import MonitorStateError, MonitorStateStore
from core.resource_backup import inspect_mounted_resource_backup
from core.source_inventory import (
    SOURCE_STATES,
    SourceInventoryError,
    SourceInventoryStore,
    source_namespaces_for_root,
)
from core.taxonomy_assignment import taxonomy_assignment_summary
from core.wechat_source_guard import source_guard_status

AUTOSTART_ERR_LOG = Path(DATA_DIR) / "logs" / "autostart.err.log"
AUTOSTART_OUT_LOG = Path(DATA_DIR) / "logs" / "autostart.out.log"

_MONITOR_RUNTIME_STATES = frozenset({
    "ai_backoff",
    "cooldown",
    "duplicate",
    "initialized",
    "missing_topic",
    "monitor_source_cursors_corrupt",
    "monitor_state_conflict",
    "monitor_state_corrupt",
    "monitor_state_missing_checkpoint",
    "no_match",
    "no_messages",
    "notified",
    "source_advanced_no_visible",
    "source_generation_changed",
    "source_inventory_incomplete",
})


def ok(value: bool) -> str:
    return "OK" if value else "WARN"


def recent_log_line(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    meaningful = [line.strip() for line in lines if line.strip()]
    if not meaningful:
        return ""
    return " / ".join(meaningful[-2:])[:240]


def recent_autostart_ai_success() -> bool:
    if not AUTOSTART_OUT_LOG.exists():
        return False
    try:
        text = AUTOSTART_OUT_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "[ai] 返回" in text[-20000:]


def latest_notification_backend_status(path: str | os.PathLike[str] = AUTOSTART_OUT_LOG) -> tuple[str, bool]:
    path = Path(path)
    if not path.exists():
        return "", False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", False
    for line in reversed(lines):
        match = re.search(r"\[notify\]\s+([^:]+):", line)
        if match:
            backend_status = match.group(1).strip()
            return backend_status, "failed" in backend_status.lower()
    return "", False


def autostart_log_status() -> tuple[str, str, bool]:
    if not AUTOSTART_ERR_LOG.exists():
        return "", "", False
    err_line = recent_log_line(AUTOSTART_ERR_LOG)
    if not err_line:
        return "", "", False
    try:
        err_mtime = AUTOSTART_ERR_LOG.stat().st_mtime
        out_mtime = AUTOSTART_OUT_LOG.stat().st_mtime if AUTOSTART_OUT_LOG.exists() else 0
    except OSError:
        err_mtime = 0
        out_mtime = 0
    failed = any(
        marker in err_line.lower()
        for marker in ("operation not permitted", "permission denied", "traceback", "error")
    )
    if out_mtime > err_mtime:
        return "Stale autostart stderr", err_line, False
    return "Last autostart stderr", err_line, failed


def count_markdown(root: str) -> tuple[int, str]:
    if not os.path.isdir(root):
        return 0, ""
    count = 0
    latest_path = ""
    latest_mtime = -1.0
    for dirpath, _dirs, files in os.walk(root):
        for filename in files:
            if not filename.endswith(".md"):
                continue
            path = os.path.join(dirpath, filename)
            count += 1
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_path = path
    return count, latest_path


def recent_topics(db_path: str) -> tuple[int, list[str]]:
    if not db_path or not os.path.exists(db_path):
        return 0, []
    conn = sqlite3.connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        rows = conn.execute(
            "SELECT title FROM topics ORDER BY topic_id DESC LIMIT 5"
        ).fetchall()
        return int(total), [row[0] for row in rows]
    except sqlite3.Error:
        return 0, []
    finally:
        conn.close()


def review_queue_pending_count(queue_dir: str | os.PathLike[str] = QUEUE_DIR) -> int:
    return ReviewQueue(queue_dir).pending_count()


def relation_integrity_status(db_path: str | os.PathLike[str]) -> tuple[str, bool]:
    """Return a privacy-safe one-line relation integrity summary."""
    report = audit_relations(db_path, sensitive=False, example_limit=0)
    if not report.get("available") or report.get("error"):
        return "unavailable", True
    dominant = report.get("dominant_relation") or "none"
    text = (
        f"relations {int(report.get('total_relations') or 0)}; "
        f"known broken {int(report.get('known_broken_reason_count') or 0)}; "
        f"relation failures {int(report.get('broader_relation_failure_count') or 0)}; "
        f"cross-chat {int(report.get('cross_chat_edge_count') or 0)}; "
        f"risky cross-chat {int(report.get('cross_chat_risky_edge_count') or 0)}; "
        f"self-loops {int(report.get('self_loop_count') or 0)}; "
        f"replays {int(report.get('exact_replay_group_count') or 0)} groups / "
        f"{int(report.get('exact_replay_excess_event_count') or 0)} excess; "
        f"orphans {int(report.get('orphan_event_count') or 0) + int(report.get('orphan_relation_count') or 0)}; "
        f"FTS {'ok' if report.get('fts_matches_topics') else 'mismatch'}; "
        f"dominant {dominant} {float(report.get('dominant_relation_ratio') or 0):.1%}"
    )
    return text, bool(report.get("warnings"))


def _path_status(path: str | os.PathLike[str]) -> str:
    return "configured" if path else "not configured"


def _sensitive_log_status(delete: bool = False) -> tuple[str, bool]:
    path = Path(EXTRACT_LOG)
    if not path.exists():
        return "absent", False
    if delete:
        try:
            path.unlink()
            return "deleted", False
        except OSError:
            return "delete failed", True
    return "present", True


def latest_monitor_runtime_result(
    path: str | os.PathLike[str] = AUTOSTART_OUT_LOG,
) -> str:
    """Return the last content-free monitor result code from the app log."""
    try:
        lines = Path(path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return "unknown"
    for line in reversed(lines):
        text = line.strip()
        if text.startswith("[monitor] 命中["):
            return "notified"
        match = re.match(r"^\[monitor\]\s+([a-z][a-z0-9_]*)(?::|$)", text)
        if match and match.group(1) in _MONITOR_RUNTIME_STATES:
            return match.group(1)
    return "unknown"


def _monitor_cursor_progress(data) -> tuple[dict, bool]:
    cursors = (data or {}).get("source_cursors")
    progress = {
        "shard_cursors": 0,
        "generation_bound": 0,
        "inventory_bound": bool((data or {}).get("source_inventory_digest")),
        "legacy_checkpoint_only": False,
    }
    if cursors is None:
        progress["legacy_checkpoint_only"] = bool(
            (data or {}).get("last_checked_ts")
        )
        return progress, True
    if not isinstance(cursors, dict):
        return progress, False
    progress["shard_cursors"] = len(cursors)
    for shard_id, item in cursors.items():
        if (
            not isinstance(shard_id, str)
            or not isinstance(item, dict)
            or not isinstance(item.get("generation_id"), str)
            or not item.get("generation_id")
            or not isinstance(item.get("cursor_token"), str)
        ):
            return progress, False
        progress["generation_bound"] += 1
    return progress, True


def monitor_state_health(
    config,
    *,
    state_dir: str | os.PathLike[str] = MONITOR_STATE_DIR,
    runtime_log: str | os.PathLike[str] = AUTOSTART_OUT_LOG,
) -> dict:
    """Inspect per-chat monitor state without printing chat identity or paths."""
    chats = active_monitor_chats(config or {})
    counts = {"healthy": 0, "missing": 0, "corrupt": 0}
    cursor_chats = 0
    shard_cursors = 0
    generation_bound = 0
    inventory_bound = 0
    legacy_states = 0
    for chat in chats:
        path = state_file_for_chat(chat.get("username", ""), state_dir=state_dir)
        try:
            snapshot = MonitorStateStore(path).inspect()
        except MonitorStateError:
            counts["corrupt"] += 1
            continue
        if not snapshot.existed:
            counts["missing"] += 1
            continue
        progress, valid = _monitor_cursor_progress(snapshot.data)
        if not valid:
            counts["corrupt"] += 1
            continue
        if not snapshot.data.get("last_checked_ts") and not progress["shard_cursors"]:
            counts["missing"] += 1
            continue
        counts["healthy"] += 1
        if snapshot.revision == 0:
            legacy_states += 1
        if progress["shard_cursors"]:
            cursor_chats += 1
        shard_cursors += int(progress["shard_cursors"])
        generation_bound += int(progress["generation_bound"])
        inventory_bound += int(progress["inventory_bound"])

    runtime_result = latest_monitor_runtime_result(runtime_log)
    if runtime_result == "monitor_state_conflict":
        state = "conflict"
    elif counts["corrupt"]:
        state = "corrupt"
    elif counts["missing"] or not chats:
        state = "missing"
    else:
        state = "healthy"
    return {
        "state": state,
        "chat_count": len(chats),
        "counts": counts,
        "runtime_result": runtime_result,
        "cursor_chats": cursor_chats,
        "shard_cursors": shard_cursors,
        "generation_bound": generation_bound,
        "inventory_bound_chats": inventory_bound,
        "legacy_states": legacy_states,
    }


def source_inventory_health(
    config,
    *,
    store=None,
    sensitive: bool = False,
) -> dict:
    """Inspect the configured source inventory without scanning or mutation."""
    db_dir = str((config or {}).get("db_dir") or "").strip()
    empty_counts = {state: 0 for state in sorted(SOURCE_STATES)}
    if not db_dir:
        return {
            "state": "unconfigured",
            "complete": False,
            "counts": empty_counts,
            "error_codes": ["source_not_configured"],
            "shards": [],
        }
    _cache_namespace, source_namespace = source_namespaces_for_root(db_dir)
    try:
        snapshot = (store or SourceInventoryStore()).inspect(source_namespace)
    except SourceInventoryError as exc:
        return {
            "state": "degraded",
            "complete": False,
            "counts": empty_counts,
            "error_codes": [exc.code],
            "shards": [],
        }
    if "source_inventory_uninitialized" in snapshot.error_codes:
        state = "uninitialized"
    else:
        state = "complete" if snapshot.complete else "degraded"
    return {
        "state": state,
        "complete": bool(snapshot.complete),
        "counts": dict(snapshot.counts),
        "error_codes": list(snapshot.error_codes),
        "shards": (
            snapshot.as_dict(sensitive=True).get("shards", [])
            if sensitive else []
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print local we-groupchat-obsidian health status.")
    parser.add_argument(
        "--sensitive",
        action="store_true",
        help="Print local paths, chat names, topic titles, and log fragments.",
    )
    parser.add_argument(
        "--delete-sensitive-key-log",
        action="store_true",
        help="Delete legacy extract_keys.log if it exists.",
    )
    args = parser.parse_args(argv)

    config = load_config()
    keys = get_cached_keys() or {}
    api_key_available = bool(load_key("ai-api-key")) or config.get("ai_provider") == "ollama"
    live_ai_success = recent_autostart_ai_success()
    wechat_signed = is_wechat_signed()
    can_lookup_processes = process_lookup_available()
    wechat_running = is_wechat_running() if can_lookup_processes else None
    agent_record, agent_status = launch_agent_report(PROJECT_DIR)
    guard_plist = (
        Path.home()
        / "Library"
        / "LaunchAgents"
        / f"{SOURCE_GUARD_LAUNCH_AGENT_LABEL}.plist"
    )
    guard_agent_status = launch_agent_status(SOURCE_GUARD_LAUNCH_AGENT_LABEL)
    autostart_log_label, autostart_log_line, autostart_log_failed = autostart_log_status()
    notify_backend, notify_failed = latest_notification_backend_status()
    notify_identity = notification_identity_status_for_launch_agent(agent_record)
    obsidian_root = os.path.join(
        os.path.expanduser(config.get("monitor_obsidian_root", "")),
        config.get("monitor_obsidian_subdir", ""),
    )
    md_count, latest_md = count_markdown(obsidian_root)
    topic_count, topic_titles = recent_topics(os.path.expanduser(config.get("monitor_knowledge_db", "")))
    relation_status, relation_failed = relation_integrity_status(
        os.path.expanduser(config.get("monitor_knowledge_db", ""))
    )
    pending_reviews = review_queue_pending_count()
    key_log_status, key_log_warn = _sensitive_log_status(delete=args.delete_sensitive_key_log)
    guard = source_guard_status(config)
    attachment_archive = AttachmentArchive.from_config(config).status()
    attachment_backup = AttachmentBackup.from_config(config).status()
    drive_sync = GoogleDriveFileSync.inspect_status(
        config,
        oauth=GoogleDriveOAuth(),
    )
    monitor_state = monitor_state_health(config)
    source_inventory = source_inventory_health(
        config,
        sensitive=args.sensitive,
    )
    mounted_backup = inspect_mounted_resource_backup(config)

    print("微信总结 health check")
    print("")
    db_dir = config.get("db_dir") or ""
    db_label = db_dir if args.sensitive else _path_status(db_dir)
    print(f"[{ok(bool(db_dir and os.path.isdir(db_dir)))}] WeChat DB: {db_label}")
    print(f"[{ok(bool(keys))}] DB keys cache: {len(keys)} 个数据库 key")
    if api_key_available:
        ai_note = "Keychain 可读取"
    elif live_ai_success:
        ai_note = "当前进程读不到 Keychain；live monitor 最近 AI 调用成功"
    else:
        ai_note = "未检测到 API key，或当前进程无 Keychain 读取权限"
    print(f"[{ok(api_key_available or live_ai_success)}] AI key/provider: {config.get('ai_provider')} / {config.get('ai_model') or 'default'} ({ai_note})")
    print(f"[{ok(wechat_signed)}] WeChat re-sign: {'已授权' if wechat_signed else '需要重新授权或无法检测'}")
    if wechat_running is None:
        print("[WARN] WeChat running: 当前进程无法检测 macOS process list")
    else:
        print(f"[{ok(wechat_running)}] WeChat running: {'运行中' if wechat_running else '未运行'}")
    print("")
    print(f"[{ok(bool(config.get('monitor_enabled')))}] Monitor enabled: {config.get('monitor_enabled')}")
    print(f"[OK] Monitor interval: {config.get('monitor_interval_minutes')} 分钟")
    chats = active_monitor_chats(config)
    if chats:
        if args.sensitive:
            print("[OK] Monitor chats: " + "、".join(chat.get("name", chat.get("username", "")) for chat in chats))
        else:
            print(f"[OK] Monitor chats: {len(chats)} selected")
    else:
        print("[WARN] Monitor chats: 未选择")
    summary = taxonomy_assignment_summary(config, chats, set(TAXONOMY_PROFILES))
    print(
        "[{}] Taxonomy presets: explicit {} / legacy {} / unknown {} / free-form {}".format(
            "WARN" if summary["legacy_name"] or summary["unknown"] else "OK",
            summary["explicit"],
            summary["legacy_name"],
            summary["unknown"],
            summary["free_form"],
        )
    )
    print(f"[{ok(bool(config.get('monitor_topic')))}] Monitor topic: {'已设置' if config.get('monitor_topic') else '未设置'}")
    monitor_counts = monitor_state["counts"]
    monitor_enabled = bool(config.get("monitor_enabled", False))
    monitor_ok = monitor_state["state"] == "healthy" or (
        not monitor_enabled and monitor_state["state"] == "missing"
    )
    print(
        f"[{ok(monitor_ok)}] Monitor state: {monitor_state['state']}; "
        f"healthy={monitor_counts['healthy']}; missing={monitor_counts['missing']}; "
        f"corrupt={monitor_counts['corrupt']}; "
        f"last_runtime={monitor_state['runtime_result']}; "
        f"legacy={monitor_state['legacy_states']}"
    )
    print(
        f"[{ok(monitor_ok)}] Monitor raw cursor progress: "
        f"checkpointed_chats={monitor_counts['healthy']}/{monitor_state['chat_count']}; "
        f"cursor_chats={monitor_state['cursor_chats']}; "
        f"shard_cursors={monitor_state['shard_cursors']}; "
        f"generation_bound={monitor_state['generation_bound']}; "
        f"inventory_bound_chats={monitor_state['inventory_bound_chats']}"
    )
    inventory_counts = source_inventory["counts"]
    inventory_ok = source_inventory["state"] == "complete" or (
        not monitor_enabled and source_inventory["state"] == "unconfigured"
    )
    print(
        f"[{ok(inventory_ok)}] Source inventory: {source_inventory['state']}; "
        f"present={inventory_counts.get('present', 0)}; "
        f"generation_changed={inventory_counts.get('generation_changed', 0)}; "
        f"missing={inventory_counts.get('missing_file', 0)}; "
        f"cache_only={inventory_counts.get('cache_only', 0)}; "
        f"key_missing={inventory_counts.get('key_missing', 0)}; "
        f"unreadable={inventory_counts.get('unreadable', 0)}; "
        f"errors={len(source_inventory['error_codes'])}"
    )
    if args.sensitive:
        for shard in source_inventory.get("shards") or []:
            print(
                "  - Source shard: "
                f"{shard.get('relative_path', '-')}; "
                f"logical={shard.get('logical_shard_id', '-')}; "
                f"generation={shard.get('generation_id', '-')}; "
                f"state={shard.get('state', 'unknown')}"
            )
    mounted_state = mounted_backup.get("state") or "unknown"
    mounted_ok = (
        not mounted_backup.get("enabled")
        or mounted_state == "ready"
    )
    delivery_counts = mounted_backup.get("delivery_counts") or {}
    print(
        f"[{ok(mounted_ok)}] Mounted resource handoff: "
        f"runtime={mounted_state}; "
        f"handoff={mounted_backup.get('handoff_semantics') or 'unknown'}; "
        f"target={'configured' if mounted_backup.get('target_configured') else 'not_configured'}; "
        f"delegated_objects={int(delivery_counts.get('sync_delegated') or 0)}; "
        "provider_side_sync=unknown; remote_verified=False"
    )
    print(
        f"[OK] Link preview: state={LINK_PREVIEW_STATE}; network_requests=0"
    )
    print("[OK] MCP compatibility: legacy_read_only; send=mcp_send_retired")
    print(
        "[OK] Windows: W0.2B.1 portable lock/path source only; "
        "product_support=not_claimed"
    )
    print("")
    obsidian_label = obsidian_root if args.sensitive else _path_status(obsidian_root)
    print(f"[{ok(os.path.isdir(obsidian_root))}] Obsidian output: {obsidian_label}")
    print(f"[OK] Markdown notes: {md_count} 篇")
    if latest_md and args.sensitive:
        print(f"[OK] Latest note: {latest_md}")
    elif latest_md:
        print("[OK] Latest note: present")
    print(f"[OK] Knowledge topics: {topic_count} 个")
    print(f"[{ok(not relation_failed)}] Relation integrity: {relation_status}")
    if args.sensitive:
        for title in topic_titles:
            print(f"  - {title}")
    print(f"[OK] Pending review queue: {pending_reviews} 个")
    digest_path, _digest_obsidian_path = digest_output_path(config, "YYYY-MM-DD")
    digest_state = "enabled" if config.get("daily_digest_enabled", True) else "disabled"
    digest_target = digest_path if args.sensitive else _path_status(digest_path)
    print(
        f"[{ok(config.get('daily_digest_enabled', True))}] Daily digest: "
        f"{digest_state} at {config.get('daily_digest_time', '21:30')} "
        f"{config.get('daily_digest_timezone', 'Asia/Shanghai')} -> {digest_target}"
    )
    guard_state = guard.get("state") or "unknown"
    guard_enabled = bool(config.get("wechat_source_guard_enabled", False))
    guard_ok = (
        not guard_enabled
        or guard_state in {"healthy", "missing_grace", "restart_backoff", "paused"}
    )
    process_state = (
        "unknown" if wechat_running is None else "running" if wechat_running else "absent"
    )
    print(
        f"[{ok(guard_ok)}] WeChat source guard: "
        f"{'enabled' if guard_enabled else 'disabled'} / "
        f"{guard_state}; last={guard.get('last_result') or 'unknown'}; "
        f"process={process_state}; pause={guard.get('pause_until') or '-'}; "
        f"missing={int(guard.get('missing_duration') or 0)}s; "
        f"budget={guard.get('restart_budget_remaining', 0)}; "
        f"backoff={guard.get('backoff_until') or '-'}; "
        f"freshness={guard.get('source_freshness') or 'unknown'}"
    )
    guard_agent_ok = not guard_plist.exists() and not guard_agent_status.loaded
    print(
        f"[{ok(guard_agent_ok)}] Source guard runtime: long_lived_app; "
        f"legacy_agent_installed={guard_plist.exists()}; "
        f"legacy_agent_loaded={guard_agent_status.loaded}"
    )
    archive_enabled = bool(config.get("attachment_archive_enabled", False))
    archive_counts = attachment_archive.get("counts") or {}
    archive_count_text = ", ".join(
        f"{key}={archive_counts[key]}" for key in sorted(archive_counts)
    ) or "empty"
    archive_ok = not archive_enabled or attachment_archive.get("state") in {
        "healthy",
        "knowledge_db_missing",
    }
    print(
        f"[{ok(archive_ok)}] Attachment archive: "
        f"{'enabled' if archive_enabled else 'disabled'} / {attachment_archive.get('state')}; "
        f"objects={attachment_archive.get('objects', 0)}; {archive_count_text}"
    )
    backup_target = config.get("attachment_backup_target") or ""
    if args.sensitive and backup_target:
        backup_label = os.path.expanduser(backup_target)
    else:
        backup_label = "configured" if backup_target else "not configured (optional)"
    backup_ok = not backup_target or attachment_backup.get("state") == "configured"
    print(
        f"[{ok(backup_ok)}] Attachment backup target: {backup_label}; "
        f"complete snapshots={attachment_backup.get('complete_snapshots', 0)}"
    )
    drive_enabled = bool(config.get("google_drive_file_sync_enabled", False))
    drive_paused = bool(config.get("google_drive_file_sync_paused", False))
    drive_auth = drive_sync.get("auth") or "auth_required"
    drive_root = drive_sync.get("root_state") or "unknown"
    drive_ok = (
        not drive_enabled
        or drive_paused
        or (
            drive_auth == "connected"
            and int(drive_sync.get("selected_chat_count") or 0) > 0
            and drive_root not in {"missing", "trashed", "duplicate", "invalid"}
            and drive_sync.get("last_error_code") != "local_ledger_unreadable"
        )
    )
    drive_counts = drive_sync.get("queue_counts") or {}
    drive_count_text = ", ".join(
        f"{key}={drive_counts[key]}" for key in sorted(drive_counts)
    ) or "empty"
    print(
        f"[{ok(drive_ok)}] Google Drive selected-chat files: "
        f"{'enabled' if drive_enabled else 'disabled'} / "
        f"{'paused' if drive_paused else 'active'}; "
        f"auth={drive_auth}; selected={int(drive_sync.get('selected_chat_count') or 0)}; "
        f"queue={drive_count_text}; last_scan={float(drive_sync.get('last_scan_at') or 0):.0f}; "
        f"last_verified_upload={float(drive_sync.get('last_verified_upload_at') or 0):.0f}; "
        f"next_retry={float(drive_sync.get('next_retry_at') or 0):.0f}; "
        f"root={drive_root}; objects={int(drive_sync.get('uploaded_unique_objects') or 0)}; "
        f"shortcuts={int(drive_sync.get('shortcut_placements') or 0)}; "
        f"last_error={drive_sync.get('last_error_code') or '-'}"
    )
    verified_objects = int(drive_sync.get("uploaded_unique_objects") or 0)
    if drive_sync.get("last_error_code") == "local_ledger_unreadable":
        drive_remote_state = "unknown"
    elif verified_objects:
        drive_remote_state = "verified_ledger_objects"
    else:
        drive_remote_state = "not_yet_verified"
    print(
        f"[{ok(drive_remote_state != 'unknown')}] Google Drive API remote verification: "
        f"{drive_remote_state}; verified_objects={verified_objects}; "
        f"last_verified={float(drive_sync.get('last_verified_upload_at') or 0):.0f}; "
        "separate_from_source_inventory=True"
    )
    print("")
    plist_label = agent_record.plist_path if args.sensitive else ("present" if agent_record.plist_path.exists() else "missing")
    print(f"[{ok(agent_record.plist_path.exists())}] LaunchAgent plist: {plist_label}")
    if args.sensitive:
        print(f"[OK] LaunchAgent label: {agent_record.label}")
    else:
        print("[OK] LaunchAgent label: configured")
    print(f"[{ok(agent_status.loaded)}] LaunchAgent loaded: {agent_status.loaded}")
    state_detail = agent_status.state or agent_status.job_state or "unknown"
    if agent_status.last_exit_code:
        state_detail = f"{state_detail}; last exit code {agent_status.last_exit_code}"
    print(f"[{ok(agent_status.running)}] LaunchAgent running: {agent_status.running} ({state_detail})")
    if autostart_log_line:
        if args.sensitive:
            print(f"[{ok(not autostart_log_failed)}] {autostart_log_label}: {autostart_log_line}")
        else:
            print(f"[{ok(not autostart_log_failed)}] {autostart_log_label}: present (redacted; use --sensitive)")
    if notify_backend:
        print(f"[{ok(not notify_failed)}] Last notification backend: {notify_backend}")
    identity_name = notify_identity.get("bundle_name") or "unknown"
    identity_id = notify_identity.get("bundle_identifier") or "missing bundle id"
    expected_id = notify_identity.get("expected_bundle_identifier") or ""
    identity_detail = f"{identity_name} / {identity_id}"
    if not notify_identity.get("ok") and expected_id:
        identity_detail = f"{identity_detail} (expected {expected_id})"
    print(f"[{ok(bool(notify_identity.get('ok')))}] Notification identity: {identity_detail}")
    if args.sensitive and notify_identity.get("bundle_path"):
        print(f"[OK] Notification bundle path: {notify_identity.get('bundle_path')}")
    if key_log_status != "absent":
        print(f"[{ok(not key_log_warn)}] Sensitive key extraction log: {key_log_status}")
    print("")

    if keys and db_dir and os.path.isdir(db_dir):
        missing = check_new_databases(db_dir, keys)
        if missing:
            print(f"[WARN] New encrypted DBs missing keys: {len(missing)} 个")
            print("      微信更新/新增数据库后，可能需要重新运行 ./启动.command 提取 key。")
        else:
            print("[OK] New encrypted DBs missing keys: 0 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
