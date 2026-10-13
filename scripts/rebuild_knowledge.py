#!/usr/bin/env python3
"""Explicit operator entrypoint for the canonical semantic rebuild coordinator."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def parser():
    result = argparse.ArgumentParser(description="Plan, execute and recover a private semantic knowledge generation.")
    result.add_argument("--config", help="Existing application configuration file")
    result.add_argument("--runtime-root", help="Application semantic runtime directory")
    result.add_argument("--project-dir", help="Exact managed LaunchAgent project binding")
    commands = result.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Read existing config/knowledge/checkpoints/notes only")
    plan.add_argument("--provider-profile", required=True, help="Explicit provider profile JSON file")
    plan.add_argument("--run-id")
    for name in ("execute", "resume"):
        command = commands.add_parser(name)
        command.add_argument("run_id")
        command.add_argument("--allow-transient-wechat-source-read", action="store_true")
        command.add_argument("--allow-model-processing", action="store_true")
    for name in ("status", "verify", "compact-journal"):
        commands.add_parser(name).add_argument("run_id")
    rollback = commands.add_parser("rollback", help="Plan retained-raw rollback; mutation requires both apply flags")
    rollback.add_argument("run_id")
    rollback.add_argument("--apply", action="store_true")
    rollback.add_argument("--allow-runtime-changes", action="store_true")
    return result


def public_result(value):
    """Default terminal output never contains account, chat, content or paths."""
    allowed = {"run_id", "generation_id", "state", "phase", "code", "complete", "restored",
               "recovery_required", "retained_generation", "requires_runtime_grant", "checkpoint_count",
               "freeze_count", "error_codes", "issues", "counts", "provisional_failure", "configured_admission"}
    result = {key: val for key, val in value.items() if key in allowed}
    if "live_check" in value:
        result["live_check"] = {key: val for key, val in value["live_check"].items()
                                if key in {"complete", "code", "chat_count"}}
    return result


def main(argv=None, *, coordinator_factory=None):
    args = parser().parse_args(argv)
    # These checks deliberately precede config construction, Keychain access,
    # source discovery, protected path inspection and any runtime mutation.
    if args.command in {"execute", "resume"} and not (
            args.allow_transient_wechat_source_read and args.allow_model_processing):
        print(json.dumps({"state": "blocked", "code": "rebuild_explicit_grants_required"}))
        return 2
    if args.command == "rollback" and args.apply and not args.allow_runtime_changes:
        print(json.dumps({"state": "blocked", "code": "rebuild_rollback_grant_required"}))
        return 2
    try:
        from core.config import ConfigStore, DATA_DIR
        from core.semantic.rebuild import RebuildCoordinator, read_json, RebuildError
        root = args.runtime_root or str(Path(DATA_DIR) / "semantic")
        factory = coordinator_factory or RebuildCoordinator
        coordinator = factory(root, ConfigStore(args.config), project_dir=args.project_dir)
        if args.command == "plan":
            profile = read_json(args.provider_profile)
            if profile is None:
                raise RebuildError("rebuild_profile_missing")
            plan = coordinator.plan(profile, run_id=args.run_id)
            result = {"run_id": plan["run_id"], "generation_id": plan["generation_id"], "state": "planned",
                      "counts": plan["scope"], "provider_budget": plan["provider_budget"],
                      "configured_admission": plan["configured_admission"], "storage": plan["storage"]}
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command in {"execute", "resume"}:
            result = getattr(coordinator, args.command)(args.run_id, allow_source_read=True, allow_model_processing=True)
        elif args.command == "rollback":
            result = coordinator.rollback(args.run_id, apply=args.apply, allow_runtime_changes=args.allow_runtime_changes)
        elif args.command == "compact-journal":
            result = coordinator.compact_journal(args.run_id)
        else:
            result = getattr(coordinator, args.command)(args.run_id)
        print(json.dumps(public_result(result), ensure_ascii=False, sort_keys=True))
        if args.command in {"status", "compact-journal"} or result.get("state") in {"run_complete", "rolled_back", "rollback_planned"} or result.get("complete") is True:
            return 0
        return 1
    except Exception as exc:
        code = getattr(exc, "code", "rebuild_operation_failed")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code):
            code = "rebuild_operation_failed"
        print(json.dumps({"state": "incomplete", "code": code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
