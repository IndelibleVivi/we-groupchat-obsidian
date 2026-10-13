#!/usr/bin/env python3
"""Operate selected-chat direct Google Drive file sync as explicit one-shot actions."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.config import load_config, update_config
from core.google_drive_auth import GoogleDriveAuthError, GoogleDriveOAuth
from core.google_drive_client import GoogleDriveClient
from core.google_drive_file_sync import GoogleDriveFileSync
from core.key_extractor import get_cached_keys
from core.wechat_db import WeChatDB


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _result_exit_code(value):
    if not isinstance(value, dict):
        return 1
    state = str(value.get("state") or "")
    auth = str(value.get("auth") or "")
    if state in {
        "auth_required",
        "retry_wait",
        "remote_degraded",
        "source_degraded",
        "failed",
        "validation_unavailable",
    }:
        return 1
    if state.endswith("_failed") or auth == "auth_required":
        return 1
    return 0


def _print_result(value):
    _print(value)
    return _result_exit_code(value)


def _source(config):
    keys = get_cached_keys() or {}
    if not keys or not config.get("db_dir"):
        raise SystemExit("WeChat source is unavailable: configure db_dir and extract database keys first.")
    return WeChatDB.for_runtime(config["db_dir"], keys, config=config)


def _service(config, *, source=False, remote=False):
    oauth = GoogleDriveOAuth()
    client = GoogleDriveClient(oauth) if remote else None
    return GoogleDriveFileSync(
        config,
        source=_source(config) if source else None,
        drive_client=client,
        oauth=oauth,
        control_state_func=load_config,
    )


def _set_config(**values):
    return update_config(patch=values)


def _from_timestamp(value):
    try:
        return int(datetime.strptime(value, "%Y-%m-%d").timestamp())
    except ValueError as exc:
        raise SystemExit("--from must use YYYY-MM-DD") from exc


def build_parser():
    parser = argparse.ArgumentParser(
        prog="google-drive-file-sync",
        description="Selected-chat file-only sync to an app-owned Google Drive archive.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("auth")
    auth.add_argument("--client-secrets", required=True)
    sub.add_parser("auth-status")
    sub.add_parser("disconnect")
    sub.add_parser("status")
    sub.add_parser("enable")
    sub.add_parser("disable")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("scan")
    sub.add_parser("run")
    sub.add_parser("reconcile")
    backfill = sub.add_parser("backfill")
    backfill.add_argument("--from", dest="from_date", required=True)
    backfill.add_argument("--apply", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "auth":
        try:
            _print(GoogleDriveOAuth().authorize(args.client_secrets))
            return 0
        except GoogleDriveAuthError as exc:
            _print({"state": "auth_required", "error_code": exc.code})
            return 1
    if args.command == "auth-status":
        return _print_result(GoogleDriveOAuth().status(validate=True))
    if args.command == "disconnect":
        GoogleDriveOAuth().disconnect()
        print("Google Drive refresh token removed from Keychain. Queue and remote files were not deleted.")
        return 0
    if args.command == "enable":
        config = _set_config(
            google_drive_file_sync_enabled=True,
            google_drive_file_sync_paused=False,
        )
        service = _service(config)
        service.initialize_selected_chat_cursors()
        print("Google Drive file sync enabled from now. Auth and chat selection remain separate.")
        return 0
    if args.command == "disable":
        _set_config(google_drive_file_sync_enabled=False)
        print("Google Drive file sync disabled. Queue, CAS, and remote files were retained.")
        return 0
    if args.command == "pause":
        _set_config(google_drive_file_sync_paused=True)
        print("Google Drive file sync paused. No new scan or upload will start.")
        return 0
    if args.command == "resume":
        _set_config(google_drive_file_sync_paused=False)
        print("Google Drive file sync resumed. The durable queue will continue on the next run.")
        return 0

    config = load_config()
    if args.command == "status":
        return _print_result(_service(config).status())
    if args.command == "scan":
        return _print_result(_service(config, source=True).scan())
    if args.command == "run":
        return _print_result(_service(config, source=True, remote=True).run())
    if args.command == "reconcile":
        return _print_result(_service(config, remote=True).reconcile())
    if args.command == "backfill":
        return _print_result(
            _service(config, source=True).backfill(
                _from_timestamp(args.from_date), apply=args.apply
            )
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
