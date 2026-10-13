#!/usr/bin/env python3
"""Inspect and mark local monitor review queue items."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.review_queue import ACTIVE_STATUS, QUEUE_DIR, TERMINAL_STATUSES, ReviewQueue
from core.semantic.runtime import legacy_command_blocked


def _short(value: str, limit: int = 96) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def cmd_list(queue: ReviewQueue, args: argparse.Namespace) -> int:
    items = queue.list(args.status)
    if not items:
        print("No review queue items.")
        return 0
    for item in items:
        resources = item.get("resources") or {}
        files = resources.get("files") or []
        links = resources.get("links") or []
        resource_bits = []
        if files:
            resource_bits.append(f"{len(files)} file")
        if links:
            resource_bits.append(f"{len(links)} link")
        resources_text = ", ".join(resource_bits) or "note"
        print(
            f"{item.get('id')} [{item.get('status')}/{item.get('priority')}] "
            f"{item.get('suggested_action')} · {resources_text} · {_short(item.get('title'))}"
        )
    return 0


def cmd_show(queue: ReviewQueue, args: argparse.Namespace) -> int:
    item = queue.get(args.id)
    if not item:
        print(f"Review queue item not found: {args.id}", file=sys.stderr)
        return 1
    print(json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_mark(queue: ReviewQueue, args: argparse.Namespace) -> int:
    try:
        item = queue.mark(args.id, args.status)
    except (KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{item['id']} -> {item['status']}")
    return 0


def cmd_audit(queue: ReviewQueue, args: argparse.Namespace) -> int:
    audit = queue.audit(stale_days=args.stale_days, sensitive=args.sensitive)
    print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_cleanup(queue: ReviewQueue, args: argparse.Namespace) -> int:
    try:
        result = queue.cleanup_legacy_digest_only(
            dry_run=not args.apply,
            status=args.status,
            limit=args.limit,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-dir", default=QUEUE_DIR)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List review queue items")
    list_parser.add_argument("--status", default=ACTIVE_STATUS)

    show_parser = subparsers.add_parser("show", help="Show one review queue item as JSON")
    show_parser.add_argument("id")

    mark_parser = subparsers.add_parser("mark", help="Mark one item reviewed, ignored, or imported")
    mark_parser.add_argument("id")
    mark_parser.add_argument("status", choices=sorted(TERMINAL_STATUSES))

    audit_parser = subparsers.add_parser("audit", help="Dry-run review queue cleanup report")
    audit_parser.add_argument("--stale-days", type=int, default=14)
    audit_parser.add_argument("--sensitive", action="store_true", help="Include bounded titles in risk preview")

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="Dry-run or apply legacy digest-only queue cleanup",
    )
    cleanup_parser.add_argument("--apply", action="store_true", help="Actually mark matched items.")
    cleanup_parser.add_argument("--status", choices=sorted(TERMINAL_STATUSES), default="reviewed")
    cleanup_parser.add_argument("--limit", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if legacy_command_blocked():
        return 2
    queue = ReviewQueue(args.queue_dir)
    if args.command == "list":
        return cmd_list(queue, args)
    if args.command == "show":
        return cmd_show(queue, args)
    if args.command == "mark":
        return cmd_mark(queue, args)
    if args.command == "audit":
        return cmd_audit(queue, args)
    if args.command == "cleanup":
        return cmd_cleanup(queue, args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
