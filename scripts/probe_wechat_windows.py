#!/usr/bin/env python3
"""Run the bounded E1a read-only probe for one exact Windows WeChat selection.

Foreground operator entrypoint only: no background timer, service, tray,
monitor or autostart caller. Both the WeChat executable and the source root
must be selected explicitly; the probe never discovers or scans beyond them.

stdout carries only the versioned, deterministic, content-free receipt.
Review the receipt before copying it anywhere public; local paths and account
identifiers are never part of it.

Exit codes: 0 = unique supported candidate; 1 = probe ran but the selection
is unknown/unreadable/unsupported/ambiguous; 2 = usage or platform error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.platform import PlatformName, create_path_service, detect_platform
from core.windows_source_probe import probe_selection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bounded read-only E1a probe for one selected WeChat "
        "executable and source root. Prints a content-free receipt."
    )
    parser.add_argument(
        "--wechat-exe",
        required=True,
        help="explicitly selected WeChat executable path",
    )
    parser.add_argument(
        "--source-root",
        required=True,
        help="explicitly selected WeChat source root directory",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="indent the JSON receipt for human review",
    )
    args = parser.parse_args(argv)

    if detect_platform() is not PlatformName.WINDOWS:
        print("probe error: the E1a probe runs only on Windows", file=sys.stderr)
        return 2

    receipt = probe_selection(
        args.wechat_exe,
        args.source_root,
        path_service=create_path_service(PlatformName.WINDOWS),
    )
    indent = 2 if args.pretty else None
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=indent))
    print(
        "receipt is allowlist-only; review it before sharing",
        file=sys.stderr,
    )
    return 0 if receipt["verdict"] == "unique_supported_candidate" else 1


if __name__ == "__main__":
    raise SystemExit(main())
