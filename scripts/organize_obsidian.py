#!/usr/bin/env python3
"""Re-export knowledge Markdown into the current Obsidian folder scheme."""
from __future__ import annotations

import os
import sys
import argparse
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.config import load_config
from core.semantic.runtime import legacy_command_blocked
from core.knowledge import KnowledgeStore
from core.source_metadata_plan import (
    SourceMetadataPlanError,
    plan_source_metadata_regeneration,
)


TAXONOMY_ASSIGNMENT_SOURCES = (
    "stored",
    "explicit",
    "legacy_name",
    "stable_alias",
)


def _format_assignment_source_counts(data: dict) -> str:
    counts = dict(data.get("assignment_source_counts") or {})
    return ", ".join(
        f"{source}={int(counts.get(source) or 0)}"
        for source in TAXONOMY_ASSIGNMENT_SOURCES
    )


def format_date_index_plan(plan: dict, limit: int = 20) -> list[str]:
    targets = list(plan.get("targets") or [])
    conflict_count = int(plan.get("conflict_count") or 0)
    lines = [
        f"  日期索引: {len(targets)} 个目标",
        f"  冲突/降级: {conflict_count} 个",
    ]
    for target in targets[:limit]:
        status = target.get("status", "")
        rel_path = target.get("rel_path", "")
        conflict_path = target.get("conflict_path", "")
        suffix = f"（保留已有: {conflict_path}）" if conflict_path else ""
        lines.append(f"  - [{status}] {rel_path}{suffix}")
    if len(targets) > limit:
        lines.append(f"  ... 另有 {len(targets) - limit} 个日期索引目标")
    return lines


def format_date_index_write_result(result: dict, limit: int = 20) -> list[str]:
    skipped = list(result.get("skipped") or [])
    lines = [
        f"  写入/更新: {int(result.get('written_count') or 0)} 个",
        f"  清理旧 generated: {int(result.get('removed_generated_count') or 0)} 个",
        f"  清理旧归档索引: {int(result.get('removed_archive_count') or 0)} 个",
        f"  跳过: {int(result.get('skipped_count') or 0)} 个",
    ]
    for target in skipped[:limit]:
        status = target.get("status", "")
        rel_path = target.get("rel_path", "")
        conflict_path = target.get("conflict_path", "")
        suffix = f"（保留已有: {conflict_path}）" if conflict_path else ""
        lines.append(f"  - [{status}] {rel_path}{suffix}")
    if len(skipped) > limit:
        lines.append(f"  ... 另有 {len(skipped) - limit} 个跳过目标")
    return lines


def format_source_metadata_plan(plan: dict) -> list[str]:
    atomic_paths = list(plan.get("atomic_paths") or [])
    history_paths = list(plan.get("history_summary_paths") or [])
    date_targets = list(plan.get("date_index_targets") or [])
    digest_paths = list(plan.get("daily_digest_paths") or [])
    lines = [
        "Source metadata regeneration dry-run",
        f"  database: {plan.get('database_path', '')}",
        f"  vault root: {plan.get('vault_root', '')}",
        f"  atomic topics: {len(atomic_paths)}",
    ]
    lines.extend(f"  - [knowledge_topic] {path}" for path in atomic_paths)
    lines.append(f"  history summaries: {len(history_paths)}")
    lines.extend(f"  - [history_summary] {path}" for path in history_paths)
    lines.extend([
        f"  date indexes: {len(date_targets)}",
        f"  date conflicts: {int(plan.get('date_index_conflict_count') or 0)}",
        f"  date skips: {int(plan.get('date_index_skip_count') or 0)}",
    ])
    for target in date_targets:
        lines.append(
            f"  - [date_index:{target.get('status', '')}] "
            f"{target.get('rel_path', '')}"
        )
        if target.get("conflict_path"):
            lines.append(f"    conflict: {target['conflict_path']}")
    lines.append(f"  daily digests: {len(digest_paths)}")
    lines.extend(f"  - [daily_digest] {path}" for path in digest_paths)
    lines.append(
        f"  rewrite candidates: {int(plan.get('rewrite_candidate_count') or 0)}"
    )
    return lines


def format_taxonomy_migration_plan(plan: dict, limit: int = 20, example_limit: int = 2) -> list[str]:
    mappings = list(plan.get("category_mappings") or [])
    changes = list(plan.get("changes") or [])
    migratable_topic_count = plan.get("migratable_topic_count")
    if migratable_topic_count is None:
        migratable_topic_count = plan.get("scoped_topic_count") or 0
    lines = [
        "Taxonomy migration dry-run",
        f"  profile: {plan.get('profile')} v{plan.get('taxonomy_version')}",
        f"  scoped topics: {int(plan.get('scoped_topic_count') or 0)} / {int(plan.get('total_topic_count') or 0)}",
        f"  migratable topics: {int(migratable_topic_count)}",
        f"  legacy history summaries: {int(plan.get('legacy_history_summary_count') or 0)}",
        f"  assignment sources: {_format_assignment_source_counts(plan)}",
        f"  category changes: {int(plan.get('category_change_count') or 0)}",
        f"  path changes: {int(plan.get('path_change_count') or 0)}",
        f"  unresolved -> 待归类: {int(plan.get('unresolved_count') or 0)}",
        "  category mappings:",
    ]
    if not mappings:
        lines.append("  - none")
    for mapping in mappings[:limit]:
        lines.append(f"  - {mapping['from']} -> {mapping['to']} | {int(mapping.get('count') or 0)}")
        for path in list(mapping.get("example_paths") or [])[:example_limit]:
            lines.append(f"    example: {path}")
    if len(mappings) > limit:
        lines.append(f"  ... {len(mappings) - limit} more mappings")

    lines.append("  example path changes:")
    if not changes:
        lines.append("  - none")
    for change in changes[:limit]:
        lines.append(f"  - {change['from_path']} -> {change['to_path']}")
    if len(changes) > limit:
        lines.append(f"  ... {len(changes) - limit} more path changes")
    return lines


def format_taxonomy_review_brief(
    plan: dict,
    organizer_changes: list[dict],
    *,
    mapping_example_limit: int = 3,
    item_limit: int | None = None,
) -> list[str]:
    mappings = list(plan.get("category_mappings") or [])
    unresolved_items = list(plan.get("unresolved_items") or [])
    metadata_only = [
        change
        for change in list(organizer_changes or [])
        if change.get("reason") == "taxonomy_profile"
    ]
    category_or_path_count = int(plan.get("path_change_count") or 0)
    limit = item_limit if item_limit is not None else max(len(unresolved_items), len(metadata_only))
    if limit < 0:
        limit = 0

    lines = [
        "# Taxonomy Migration Review Brief",
        "",
        "## Summary",
        f"- profile: {plan.get('profile')} v{plan.get('taxonomy_version')}",
        f"- scoped topics: {int(plan.get('scoped_topic_count') or 0)} / {int(plan.get('total_topic_count') or 0)}",
        f"- migratable topics: {int(plan.get('migratable_topic_count') or 0)}",
        f"- legacy history summaries: {int(plan.get('legacy_history_summary_count') or 0)}",
        f"- assignment sources: {_format_assignment_source_counts(plan)}",
        f"- category/path changes: {category_or_path_count}",
        f"- metadata-only backfills: {len(metadata_only)}",
        f"- unresolved 待归类: {int(plan.get('unresolved_count') or 0)}",
        "",
        "## Full Category Mapping",
    ]
    if not mappings:
        lines.append("- none")
    for mapping in mappings:
        lines.append(f"- {mapping['from']} -> {mapping['to']} | {int(mapping.get('count') or 0)}")
        for path in list(mapping.get("example_paths") or [])[:mapping_example_limit]:
            lines.append(f"  - example: {path}")

    lines.extend(["", "## Unresolved 待归类 Items"])
    if not unresolved_items:
        lines.append("- none")
    for item in unresolved_items[:limit]:
        chat = item.get("vault_chat_name") or item.get("source_chat") or ""
        lines.append(
            f"- #{item.get('topic_id')} {item.get('title')} | {chat} | "
            f"{item.get('from')} -> {item.get('to')}"
        )
        lines.append(f"  - from: {item.get('from_path')}")
        lines.append(f"  - to: {item.get('to_path')}")
    if limit < len(unresolved_items):
        lines.append(f"- ... {len(unresolved_items) - limit} more unresolved items")

    lines.extend(["", "## Metadata-Only Backfills"])
    if not metadata_only:
        lines.append("- none")
    for item in metadata_only[:limit]:
        lines.append(f"- {item.get('title')} | {item.get('from')} -> {item.get('to')}")
        lines.append(f"  - path: {item.get('from_path')}")
    if limit < len(metadata_only):
        lines.append(f"- ... {len(metadata_only) - limit} more metadata-only backfills")

    lines.extend(["", "## Apply Boundary"])
    lines.append("- This brief is read-only: it does not update SQLite rows or move Markdown files.")
    lines.append("- Review this brief and `scripts/organize_obsidian.py --dry-run` before running the organizer without flags.")
    return lines


def _audit_link(path: str, title: str) -> str:
    label = str(title or path or "untitled").strip() or "untitled"
    target = str(path or "").strip()
    if target.endswith(".md"):
        target = target[:-3]
    return f"[[{target}|{label}]]" if target else label


def format_knowledge_audit_report(audit: dict, limit: int = 10) -> list[str]:
    relation_counts = dict(audit.get("relation_counts") or {})
    relation_examples = list(audit.get("relation_examples") or [])
    duplicate_examples = list(audit.get("duplicate_examples") or [])
    taxonomy = dict(audit.get("taxonomy") or {})
    category_examples = list(audit.get("category_change_examples") or [])

    lines = [
        "# Knowledge Relation Audit",
        "",
        "## Summary",
        f"- topics: {int(audit.get('total_topics') or 0)}",
        f"- relation edges: {int(audit.get('relation_edge_count') or 0)}",
        f"- duplicate clusters: {int(audit.get('duplicate_group_count') or 0)}",
        f"- path/category cleanup candidates: {int(audit.get('category_change_count') or 0)}",
        "",
        "## Relation Counts",
    ]
    if relation_counts:
        for relation, count in sorted(relation_counts.items()):
            lines.append(f"- {relation}: {int(count)}")
    else:
        lines.append("- none")

    lines.extend(["", "## Relation Examples"])
    if relation_examples:
        for item in relation_examples[:limit]:
            source = _audit_link(item.get("source_path"), item.get("source_title"))
            target = _audit_link(item.get("target_path"), item.get("target_title"))
            lines.append(f"- {item.get('relation')}: {source} -> {target}")
    else:
        lines.append("- none")

    lines.extend(["", "## Duplicate Candidates"])
    if duplicate_examples:
        for item in duplicate_examples[:limit]:
            merged = "; ".join(item.get("merged") or [])
            suffix = f" <- {merged}" if merged else ""
            lines.append(f"- {item.get('primary')}{suffix}")
    else:
        lines.append("- none")

    lines.extend([
        "",
        "## Taxonomy Review",
        f"- profile: {taxonomy.get('profile', '')}",
        f"- scoped topics: {int(taxonomy.get('scoped_topic_count') or 0)}",
        f"- assignment sources: {_format_assignment_source_counts(taxonomy)}",
        f"- unresolved 待归类: {int(taxonomy.get('unresolved_count') or 0)}",
        f"- taxonomy category changes: {int(taxonomy.get('category_change_count') or 0)}",
        f"- taxonomy path changes: {int(taxonomy.get('path_change_count') or 0)}",
    ])

    lines.extend(["", "## Path Cleanup Examples"])
    if category_examples:
        for item in category_examples[:limit]:
            lines.append(f"- {item.get('from_path')} -> {item.get('to_path')}")
    else:
        lines.append("- none")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-export knowledge Markdown into the current Obsidian folder scheme.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Only show planned path changes.")
    mode.add_argument("--date-indexes-only", action="store_true", help="Only write managed date indexes.")
    mode.add_argument("--taxonomy-dry-run", action="store_true", help="Preview controlled taxonomy migration without writing.")
    mode.add_argument("--taxonomy-review-brief", action="store_true", help="Print a read-only taxonomy migration review brief.")
    mode.add_argument("--knowledge-audit", action="store_true", help="Print a read-only knowledge relation audit.")
    mode.add_argument("--source-metadata-dry-run", action="store_true", help="Preview source metadata regeneration counts and paths without writing.")
    parser.add_argument("--taxonomy-profile", default="human_ai_intimacy_v1", help="Taxonomy profile for --taxonomy-dry-run.")
    parser.add_argument("--duplicate-threshold", type=int, default=85, help="Duplicate threshold for --knowledge-audit.")
    parser.add_argument("--taxonomy-review-output", default="", help="Optional Markdown output path for --taxonomy-review-brief.")
    parser.add_argument("--knowledge-audit-output", default="", help="Optional Markdown output path for --knowledge-audit.")
    args = parser.parse_args()

    if legacy_command_blocked():
        return 2
    config = load_config()
    root = os.path.join(
        os.path.expanduser(config.get("monitor_obsidian_root", "")),
        config.get("monitor_obsidian_subdir", ""),
    )
    if args.source_metadata_dry_run:
        try:
            plan = plan_source_metadata_regeneration(config)
        except SourceMetadataPlanError as exc:
            print(f"Source metadata dry-run failed: {exc}", file=sys.stderr)
            return 2
        for line in format_source_metadata_plan(plan):
            print(line)
        return 0

    if args.taxonomy_dry_run:
        store = KnowledgeStore.from_config(config, read_only=True)
        plan = store.plan_taxonomy_migration(args.taxonomy_profile)
        print(f"Obsidian taxonomy migration preview")
        print(f"  root: {root}")
        for line in format_taxonomy_migration_plan(plan):
            print(line)
        return 0

    if args.taxonomy_review_brief:
        store = KnowledgeStore.from_config(config, read_only=True)
        plan = store.plan_taxonomy_migration(args.taxonomy_profile, example_limit=50)
        changes = store.find_category_changes()
        print(f"Obsidian taxonomy migration review")
        print(f"  root: {root}")
        for line in format_taxonomy_review_brief(plan, changes):
            print(line)
        if args.taxonomy_review_output:
            output_path = Path(args.taxonomy_review_output).expanduser()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("\n".join(format_taxonomy_review_brief(plan, changes)).rstrip() + "\n", encoding="utf-8")
            print(f"Taxonomy review written: {output_path}")
        return 0

    if args.knowledge_audit:
        store = KnowledgeStore.from_config(config, read_only=True)
        audit = store.knowledge_audit(
            taxonomy_profile=args.taxonomy_profile,
            duplicate_threshold=args.duplicate_threshold,
        )
        lines = format_knowledge_audit_report(audit)
        for line in lines:
            print(line)
        if args.knowledge_audit_output:
            output_path = Path(args.knowledge_audit_output).expanduser()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
            print(f"Knowledge audit written: {output_path}")
        return 0

    store = KnowledgeStore.from_config(config)
    if args.dry_run:
        changes = store.find_category_changes()
        date_plan = store.plan_date_indexes()
        print("Obsidian 输出整理预览")
        print(f"  root: {root}")
        print(f"  待迁移: {len(changes)} 篇")
        for change in changes[:20]:
            print(f"  - {change['from_path']} -> {change['to_path']}")
        if len(changes) > 20:
            print(f"  ... 另有 {len(changes) - 20} 篇")
        for line in format_date_index_plan(date_plan):
            print(line)
        return 0

    if args.date_indexes_only:
        date_result = store.write_date_indexes()
        print("日期索引更新完成")
        print(f"  root: {root}")
        for line in format_date_index_write_result(date_result):
            print(line)
        return 0

    reexport_count = store.reexport_all()
    print("Obsidian 输出重新导出完成")
    print(f"  root: {root}")
    print(f"  重新导出: {reexport_count} 篇")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
