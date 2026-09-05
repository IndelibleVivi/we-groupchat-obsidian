#!/usr/bin/env python3
"""Write today's local WeChat monitor daily digest."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.config import load_config
from core.daily_digest import refresh_pending_daily_digests, write_daily_digest


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a local WeChat monitor daily digest.")
    parser.add_argument(
        "--date",
        default="",
        help="Digest source date in YYYY-MM-DD; defaults to today.",
    )
    args = parser.parse_args()
    config = load_config()
    digest = write_daily_digest(config, target_date=args.date or None)
    repair = refresh_pending_daily_digests(config)
    if repair.get("state") == "deferred":
        raise RuntimeError(
            "daily_digest_repair_deferred:"
            + str(repair.get("error_code") or "unknown")
        )
    print(f"Daily digest: {digest['path']}")
    print(f"  notes: {digest['new_notes_count']}")
    print(f"  actions: {digest.get('today_action_count', 0)}")
    print(f"  risk: {digest.get('today_risk_count', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
