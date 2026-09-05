#!/usr/bin/env python3
"""Re-sign one exact WeChat bundle after explicit operator authorization."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.wechat_resign import WeChatResignError, resign_wechat_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument("--allow-wechat-resign", action="store_true")
    parser.add_argument("--explicit-target", action="store_true")
    args = parser.parse_args()
    try:
        result = resign_wechat_bundle(
            args.app,
            allow=args.allow_wechat_resign,
            explicit_target=args.explicit_target,
        )
    except WeChatResignError as exc:
        print(f"WeChat re-sign failed: {exc.code}", file=sys.stderr)
        return 2
    print(
        "WeChat re-sign verified: "
        f"{result.bundle.version} ({result.bundle.build}) · "
        f"{result.running.build_identity[2]} · {result.bundle.app_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
