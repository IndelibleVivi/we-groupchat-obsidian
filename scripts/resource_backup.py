#!/usr/bin/env python3
"""Capture selected-chat links/files and hand them to a mounted backup target."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time
import uuid

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.config import (
    load_config,
    normalize_path_value,
    update_config,
    selected_resource_backup_chats,
)
from core.key_extractor import get_cached_keys
from core.resource_backup import (
    MountedResourceBackup,
    evaluate_resource_backup_outcome,
    load_resource_backup_settings,
    save_resource_backup_settings,
)
from core.resource_backup_launch_agent import (
    install as install_resource_backup_agent,
    status as resource_backup_agent_status,
    uninstall as uninstall_resource_backup_agent,
)
from core.resource_capture import (
    ResourceCaptureError,
    SelectedResourceCapture,
    resource_backup_chat_candidates,
    update_resource_backup_selection,
)
from core.wechat_db import WeChatDB


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _source(config):
    keys = get_cached_keys() or {}
    if not keys or not config.get("db_dir"):
        return None
    return WeChatDB.for_runtime(config["db_dir"], keys, config=config)


def _from_timestamp(value):
    try:
        return int(datetime.strptime(value, "%Y-%m-%d").timestamp())
    except ValueError as exc:
        raise SystemExit("--from must use YYYY-MM-DD") from exc


def _capture(config, *, source=False):
    return SelectedResourceCapture.from_config(
        config,
        source=_source(config) if source else None,
    )


def _backup(config, *, capture=None, link_export_mode=None):
    return MountedResourceBackup.from_config(
        config,
        capture=capture,
        link_export_mode=link_export_mode,
    )


def _exit_code(result):
    state = str((result or {}).get("state") or "")
    if state in {
        "invalid_target",
        "target_failed",
        "snapshot_unavailable",
        "source_degraded",
        "degraded",
        "source_unavailable",
        "destination_unavailable",
        "target_not_configured",
        "no_selected_chats",
        "pending_resources",
        "worker_busy",
        "install_failed",
        "uninstall_failed",
        "script_missing",
        "long_lived_app_required",
        "plan_required",
        "plan_expired",
        "plan_not_applicable",
        "run_not_found",
        "selection_changed",
        "candidate_mismatch",
        "inventory_unavailable",
        "inventory_changed",
        "consent_revoked",
        "not_run_worker_busy",
        "projection_failed",
        "projection_archive_mismatch",
        "projection_destination_mismatch",
        "projection_owner_missing",
    }:
        return 2
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="resource-backup",
        description=(
            "Capture links/files only from explicitly selected active monitor chats, "
            "write Obsidian resource indexes, and hand CAS objects to a mounted "
            "filesystem target. Mounted handoff is not remote cloud verification."
        ),
    )
    parser.add_argument(
        "--link-export-mode",
        choices=("redacted", "off"),
        default=None,
        help="Export redacted link URLs or omit URL values from the mounted catalog.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("list-chats")
    selected = sub.add_parser("set-selected-chats")
    selected.add_argument("indexes", nargs="+", type=int)
    sub.add_parser("clear-selected-chats")
    sub.add_parser("enable")
    sub.add_parser("disable")
    sub.add_parser("init")
    sub.add_parser("scan")
    backfill = sub.add_parser("backfill")
    backfill_scope = backfill.add_mutually_exclusive_group(required=True)
    backfill_scope.add_argument("--from", dest="from_date")
    backfill_scope.add_argument(
        "--all",
        action="store_true",
        help="Scan all locally available history for the selected chats.",
    )
    backfill.add_argument("--apply", action="store_true")
    backfill.add_argument(
        "--run-id",
        default="",
        help="Apply the exact staged run returned by a previous plan.",
    )
    backfill_links = sub.add_parser(
        "backfill-links",
        help="Plan/apply exact historical links without resolving attachment files.",
    )
    backfill_links_scope = backfill_links.add_mutually_exclusive_group(required=True)
    backfill_links_scope.add_argument("--from", dest="from_date")
    backfill_links_scope.add_argument(
        "--all",
        action="store_true",
        help="Scan all locally available history for exact links.",
    )
    backfill_links.add_argument("--apply", action="store_true")
    backfill_links.add_argument(
        "--run-id",
        default="",
        help="Apply the exact staged run returned by a previous plan.",
    )
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--limit", type=int, default=50)
    sub.add_parser("index")
    sub.add_parser("plan")
    run = sub.add_parser("run")
    run.add_argument("--resolve-limit", type=int, default=50)
    run.add_argument(
        "--resolve-files",
        action="store_true",
        help="Explicitly allow this run to read selected WeChat attachment files.",
    )
    verify = sub.add_parser("verify")
    verify.add_argument("--snapshot-id", default="")
    target = sub.add_parser("set-target")
    target.add_argument("path")
    sub.add_parser("clear-target")
    mode = sub.add_parser("set-link-export-mode")
    mode.add_argument("mode", choices=("redacted", "off"))
    install_agent = sub.add_parser("install-agent")
    install_agent.add_argument("--interval-seconds", type=int, default=None)
    sub.add_parser("uninstall-agent")
    sub.add_parser("agent-status")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if (
        args.command in {"backfill", "backfill-links"}
        and args.apply
        and not args.run_id
    ):
        raise SystemExit("--apply requires the --run-id returned by the plan")
    config = load_config()

    if args.command == "list-chats":
        selected_usernames = {
            chat["username"] for chat in selected_resource_backup_chats(config)
        }
        choices = resource_backup_chat_candidates(config)
        _print({
            "state": "ok",
            "chats": [
                {
                    "index": index,
                    "alias": chat["alias"],
                    "selected": chat["username"] in selected_usernames,
                }
                for index, chat in enumerate(choices, 1)
            ],
        })
        return 0
    if args.command == "set-selected-chats":
        choices = resource_backup_chat_candidates(config)
        indexes = list(dict.fromkeys(args.indexes))
        if not indexes or any(index < 1 or index > len(choices) for index in indexes):
            _print({"state": "invalid_selection", "available_chats": len(choices)})
            return 2
        previous = {
            chat["username"]: chat
            for chat in selected_resource_backup_chats(config)
        }
        now = int(time.time())
        selected = []
        for index in indexes:
            chat = dict(choices[index - 1])
            old = previous.get(chat["username"]) or {}
            chat["selected_since"] = int(old.get("selected_since") or now)
            chat["selection_id"] = str(old.get("selection_id") or uuid.uuid4())
            selected.append(chat)
        try:
            updated, _initialized = update_resource_backup_selection(
                config,
                selected,
            )
        except ResourceCaptureError as exc:
            if exc.code == "capture_worker_busy":
                _print({"state": "worker_busy", "error_code": exc.code})
                return 2
            raise
        _print({
            "state": "configured",
            "selected_chats": len(selected),
            "aliases": [chat["alias"] for chat in selected],
        })
        return 0
    if args.command == "clear-selected-chats":
        try:
            update_resource_backup_selection(config, [])
        except ResourceCaptureError as exc:
            if exc.code == "capture_worker_busy":
                _print({"state": "worker_busy", "error_code": exc.code})
                return 2
            raise
        _print({"state": "configured", "selected_chats": 0})
        return 0
    if args.command in {"enable", "disable"}:
        updated = update_config(patch={
            "resource_backup_enabled": args.command == "enable",
        })
        _print({
            "state": "configured",
            "background_enabled": bool(updated.get("resource_backup_enabled", False)),
            "file_resolution_policy": "explicit_per_run_or_app_session",
            "runtime": "long_lived_app",
        })
        return 0

    if args.command == "set-target":
        target = normalize_path_value(args.path)
        if not target:
            raise SystemExit("target path must not be empty")
        settings = save_resource_backup_settings({"target": target})
        _print({"state": "configured", "target": "configured", "link_export_mode": settings["link_export_mode"]})
        return 0
    if args.command == "clear-target":
        settings = save_resource_backup_settings({"target": ""})
        _print({"state": "target_not_configured", "link_export_mode": settings["link_export_mode"]})
        return 0
    if args.command == "set-link-export-mode":
        settings = save_resource_backup_settings({"link_export_mode": args.mode})
        _print({
            "state": "configured",
            "target": "configured" if settings["target"] else "not configured",
            "link_export_mode": settings["link_export_mode"],
        })
        return 0
    if args.command == "install-agent":
        interval = (
            args.interval_seconds
            if args.interval_seconds is not None
            else config.get("resource_backup_interval_seconds", 300)
        )
        result = install_resource_backup_agent(PROJECT_DIR, interval)
        _print(result)
        return _exit_code(result)
    if args.command == "uninstall-agent":
        result = uninstall_resource_backup_agent()
        _print(result)
        return _exit_code(result)
    if args.command == "agent-status":
        result = resource_backup_agent_status()
        _print(result)
        return 0

    if args.command == "init":
        result = _capture(config).initialize_selected_chat_cursors()
    elif args.command == "scan":
        result = _capture(config, source=True).scan()
    elif args.command == "backfill":
        result = _capture(config, source=True).backfill(
            0 if args.all else _from_timestamp(args.from_date),
            apply=args.apply,
            run_id=args.run_id,
        )
    elif args.command == "backfill-links":
        result = _capture(config, source=True).backfill_links(
            0 if args.all else _from_timestamp(args.from_date),
            apply=args.apply,
            run_id=args.run_id,
        )
    elif args.command == "resolve":
        result = _capture(config).resolve_pending_files(limit=max(1, args.limit))
    elif args.command == "index":
        result = _backup(
            config,
            link_export_mode=args.link_export_mode,
        ).render_obsidian_indexes()
    elif args.command == "plan":
        result = _backup(
            config,
            link_export_mode=args.link_export_mode,
        ).plan()
    elif args.command == "status":
        capture = _capture(config)
        backup_status = _backup(
            config,
            capture=capture,
            link_export_mode=args.link_export_mode,
        ).status()
        result = {
            "state": "ok",
            "settings": {
                "target": "configured" if load_resource_backup_settings()["target"] else "not configured",
                "link_export_mode": load_resource_backup_settings()["link_export_mode"],
                "background_enabled": bool(config.get("resource_backup_enabled", False)),
                "file_resolution_policy": "explicit_per_run_or_app_session",
                "runtime": "long_lived_app",
            },
            "capture": capture.status(),
            "coverage": backup_status.get("coverage") or {},
            "coverage_complete": bool(backup_status.get("coverage_complete")),
            "backup": backup_status,
            "launch_agent": resource_backup_agent_status(),
        }
    elif args.command == "run":
        capture = _capture(config, source=True)
        capture_result = capture.run(
            resolve_limit=max(1, args.resolve_limit),
            resolve_files=bool(args.resolve_files),
        )
        if capture_result.get("state") == "worker_busy":
            backup_result = {"state": "not_run_worker_busy"}
        else:
            backup_result = _backup(
                config,
                capture=capture,
                link_export_mode=args.link_export_mode,
            ).run()
        outcome = evaluate_resource_backup_outcome(
            capture_result,
            backup_result,
        )
        result = {
            "state": outcome["state"],
            "completed": outcome["completed"],
            "operational_success": outcome["operational_success"],
            "coverage_complete": outcome["coverage_complete"],
            "coverage": outcome["coverage"],
            "capture": capture_result,
            "backup": backup_result,
        }
    elif args.command == "verify":
        result = _backup(
            config,
            link_export_mode=args.link_export_mode,
        ).verify(args.snapshot_id)
    else:
        return 1

    _print(result)
    if args.command == "run":
        return 0 if bool(result.get("completed")) else 2
    return _exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
