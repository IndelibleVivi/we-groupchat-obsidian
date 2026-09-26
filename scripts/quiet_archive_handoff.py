#!/usr/bin/env python3
"""Configure, capture and export the private local Quiet Archive handoff."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime
import json
import math
from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.config import load_config, normalize_path_value, update_config
from core.quiet_archive_handoff import QuietArchiveHandoff
from core.resource_capture import SelectedResourceCapture


REFRESH_SCHEMA = "we-groupchat-obsidian.quiet-archive.refresh.v1"
SOURCE_COMMANDS = {"capture", "drain", "refresh", "backfill"}
PROFILE_COMMANDS = {"plan", "init", "adopt-plan", "adopt-apply", "resolve-files"}


def _source(config):
    from core.key_extractor import get_cached_keys
    from core.wechat_db import WeChatDB
    keys = get_cached_keys() or {}
    if not keys or not config.get("db_dir"):
        return None
    return WeChatDB.for_runtime(config["db_dir"], keys)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="Explicit private standalone producer JSON; no app config.")
    sub = parser.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure")
    configure.add_argument("--target", required=True,
                           help="Existing private local directory, separate from mounted backups.")
    sub.add_parser("enable")
    sub.add_parser("disable")
    sub.add_parser("status")
    sub.add_parser("export")
    sub.add_parser("plan")
    sub.add_parser("init")
    adopt = sub.add_parser("adopt-plan")
    adopt.add_argument("--from-ledger", required=True)
    adopt.add_argument("--from-objects", required=True)
    adopt.add_argument("--from-inventory")
    adopt.add_argument("--output", required=True)
    adopt.add_argument("--wait-seconds", type=float, default=0,
                       help="Wait up to 0–300 seconds for capture ownership before freezing the plan.")
    apply = sub.add_parser("adopt-apply")
    apply.add_argument("--plan", required=True)
    resolve = sub.add_parser("resolve-files")
    resolve.add_argument("--allow-transient-wechat-source-read", action="store_true")
    resolve.add_argument("--allow-attachment-read", action="store_true")
    resolve.add_argument("--limit", type=int)
    for name in ("capture", "drain", "refresh", "backfill"):
        command = sub.add_parser(name)
        command.add_argument("--allow-transient-wechat-source-read", action="store_true",
                             help="Authorize this process's protected source reads; macOS may prompt.")
        if name in {"drain", "refresh"}:
            command.add_argument("--max-rounds", type=int)
            command.add_argument("--max-seconds", type=float)
        if name == "backfill":
            scope = command.add_mutually_exclusive_group(required=True)
            scope.add_argument("--all", action="store_true")
            scope.add_argument("--from", dest="from_date")
            command.add_argument("--apply", action="store_true")
            command.add_argument("--run-id", default="")
    return parser


def _refresh(capture, exporter, args):
    try:
        result = capture.drain(max_rounds=args.max_rounds, max_seconds=args.max_seconds)
    except Exception as exc:
        result = {"state": "failed", "error_code": str(getattr(exc, "code", "") or type(exc).__name__)}
    handoff = exporter.run()
    coverage = handoff.get("coverage") or exporter.coverage()
    snapshot = handoff.get("snapshot") or {}
    published = handoff.get("state") in {"written", "unchanged"} and bool(snapshot.get("snapshot_id"))
    eof = bool(published and result.get("state") == "eof"
               and (coverage.get("source_scan") or {}).get("raw_eof"))
    pending = bool(published and result.get("state") == "pending")
    return {
        "schema": REFRESH_SCHEMA, "state": "eof" if eof else ("pending" if pending else "failed"),
        "completed": eof, "snapshot_id": snapshot.get("snapshot_id") if published else None,
        "capture": result, "handoff": handoff, "coverage": coverage,
    }


def _execute(args):
    # This check precedes config/key/source construction and all source stat/open.
    if args.command in SOURCE_COMMANDS | {"resolve-files"} and not args.allow_transient_wechat_source_read:
        return {"state": "source_read_not_authorized",
                "error_code": "allow_transient_wechat_source_read_required"}, 2
    if args.profile:
        return _standalone(args)
    if args.command in PROFILE_COMMANDS:
        return {"state": "standalone_profile_required"}, 2
    if args.command in {"drain", "refresh"}:
        args.max_rounds = 20 if args.max_rounds is None else args.max_rounds
        args.max_seconds = 480 if args.max_seconds is None else args.max_seconds
    if args.command in {"drain", "refresh"} and (
        args.max_rounds < 1 or not math.isfinite(args.max_seconds) or args.max_seconds <= 0
    ):
        return {"state": "invalid_budget"}, 2
    config = load_config()
    if args.command == "configure":
        target = normalize_path_value(args.target)
        if not target:
            return {"state": "invalid_target"}, 2
        update_config(patch={"quiet_archive_handoff_target": target})
        return {"state": "configured", "target": "configured"}, 0
    if args.command in {"enable", "disable"}:
        enabled = args.command == "enable"
        if enabled and not config.get("quiet_archive_handoff_target"):
            return {"state": "target_not_configured"}, 2
        update_config(patch={"quiet_archive_handoff_enabled": enabled})
        return {"state": "enabled" if enabled else "disabled"}, 0
    capture = SelectedResourceCapture.from_config(
        config, source=_source(config) if args.command in SOURCE_COMMANDS else None,
        capture_contexts=True if args.command in SOURCE_COMMANDS else None,
    )
    exporter = QuietArchiveHandoff.from_config(config, capture=capture)
    return _run_capture_command(args, capture, exporter)


def _run_capture_command(args, capture, exporter):
    if args.command == "status":
        return exporter.status(), 0
    if args.command == "export":
        result = exporter.run()
        return result, 0 if result.get("state") in {"written", "unchanged"} else 2
    if args.command == "capture":
        result = capture.scan()
        return result, 0 if result.get("raw_eof") else 2
    if args.command == "drain":
        result = capture.drain(max_rounds=args.max_rounds, max_seconds=args.max_seconds)
        return result, 0 if result.get("state") == "eof" else 2
    if args.command == "refresh":
        result = _refresh(capture, exporter, args)
        return result, 0 if result["completed"] else 2
    timestamp = 0 if args.all else int(datetime.strptime(args.from_date, "%Y-%m-%d").timestamp())
    result = capture.backfill_contexts(timestamp, apply=args.apply, run_id=args.run_id)
    return result, 0 if result.get("state") in {"planned", "applied"} else 2


def _standalone(args):
    from core.quiet_archive_producer import ProducerProfile, adoption_plan, adoption_apply
    if args.command in {"configure", "enable", "disable"}:
        return {"state": "standalone_profile_is_explicit"}, 2
    profile = ProducerProfile.read(args.profile)
    if args.command == "plan":
        return profile.plan(), 0
    if args.command == "init":
        return profile.initialize(), 0
    if args.command == "adopt-plan":
        return adoption_plan(profile, ledger=args.from_ledger, objects=args.from_objects,
                             inventory=args.from_inventory, output=args.output,
                             wait_seconds=args.wait_seconds), 0
    if args.command == "adopt-apply":
        return adoption_apply(profile, args.plan), 0
    if args.command == "status" and not profile.marker():
        return {"state": "not_initialized", "source_read": False, "keys_read": False}, 0
    profile.marker(required=True)
    if args.command in {"drain", "refresh"}:
        budget = profile.value["budget"]
        args.max_rounds = budget["max_rounds"] if args.max_rounds is None else args.max_rounds
        args.max_seconds = budget["max_seconds"] if args.max_seconds is None else args.max_seconds
        if args.max_rounds < 1 or not math.isfinite(args.max_seconds) or args.max_seconds <= 0:
            return {"state": "invalid_budget"}, 2
    if args.command == "resolve-files" and not args.allow_attachment_read:
        return {"state": "attachment_read_not_authorized", "error_code": "allow_attachment_read_required"}, 2
    capture = profile.capture(source=profile.source() if args.command in SOURCE_COMMANDS else None)
    exporter = QuietArchiveHandoff(profile.config(), capture=capture)
    if args.command == "resolve-files":
        limit = profile.value["budget"]["resolve_limit"] if args.limit is None else args.limit
        if limit < 1:
            return {"state": "invalid_budget"}, 2
        with capture.canonical_operation():
            result = capture.resolve_pending_files(limit=limit, consent_check=lambda: True)
            coverage = exporter.coverage()
        return {"state": result["state"], "resolve": result, "coverage": coverage}, (
            0 if result["state"] == "healthy" else 2)
    if args.command == "refresh":
        # Keep this exact capture receipt and export together across app/CLI writers.
        with capture.canonical_operation():
            return _run_capture_command(args, capture, exporter)
    return _run_capture_command(args, capture, exporter)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        # Keep stdout a single machine-readable response even if source diagnostics run.
        with redirect_stdout(sys.stderr):
            result, code = _execute(args)
    except Exception as exc:
        result = {"state": "failed", "error_code": str(getattr(exc, "code", "") or type(exc).__name__)}
        result.update(getattr(exc, "details", {}))
        if args.command == "refresh":
            result.update(schema=REFRESH_SCHEMA, completed=False, snapshot_id=None)
        code = 2
    if args.command == "refresh":
        result.setdefault("schema", REFRESH_SCHEMA)
        result.setdefault("completed", False)
        result.setdefault("snapshot_id", None)
        if result.get("state") not in {"eof", "pending", "failed"}:
            result.setdefault("error_code", result.get("state"))
            result["state"] = "failed"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
