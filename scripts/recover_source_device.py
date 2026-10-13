#!/usr/bin/env python3
"""Explicit stopped-app source-device recovery; default is proof only."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-device", required=True, type=int)
    parser.add_argument("--expected-namespace", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-transient-wechat-source-read", action="store_true")
    args = parser.parse_args(argv)
    if not args.allow_transient_wechat_source_read:
        print(json.dumps({"state": "blocked", "error_code": "transient_source_read_not_authorized"}))
        return 2
    from core.app_runtime import AppAlreadyRunning, AppInstanceLock
    from core.key_extractor import get_cached_keys
    from core.wechat_source_recovery import recover_source_device
    try:
        # Take process ownership before config/keys/source access. The operator
        # stops/restores the exact managed app; this CLI never guesses a job.
        with AppInstanceLock():
            result = recover_source_device(previous_device=args.previous_device,
                                           expected_namespace=args.expected_namespace,
                                           keys=get_cached_keys() or {}, apply=args.apply)
        print(json.dumps(result))
        return 0
    except AppAlreadyRunning:
        print(json.dumps({"state": "blocked", "error_code": "menu_app_already_running"}))
        return 2
    except Exception as exc:
        print(json.dumps({"state": "blocked", "error_code":
                          getattr(exc, "code", "source_recovery_failed")}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
