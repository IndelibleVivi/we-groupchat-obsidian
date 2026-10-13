#!/usr/bin/env python3
"""Summarize historical group-chat messages into the Obsidian knowledge store."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from ai.factory import create_provider
from core.config import load_config
from core.semantic.runtime import legacy_command_blocked
from core.key_extractor import get_cached_keys
from core.knowledge import KnowledgeStore
from core.wechat_db import WeChatDB


CHUNK_SIZE = 300


def prompt(text: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    value = input(f"{text}{suffix}: ").strip()
    if value:
        return value
    return default or ""


def parse_selection(text: str, max_count: int) -> list[int]:
    selected: list[int] = []
    seen = set()
    for token in text.replace("，", ",").replace("、", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit():
            raise ValueError("请输入数字编号，多个编号用逗号分隔，例如 1,3")
        idx = int(token)
        if idx < 1 or idx > max_count:
            raise ValueError(f"编号 {idx} 超出范围 1-{max_count}")
        if idx not in seen:
            selected.append(idx)
            seen.add(idx)
    return selected


def list_groups(db: WeChatDB, config: dict, search: str = "") -> list[dict]:
    configured = [
        item for item in config.get("monitor_chats", [])
        if isinstance(item, dict) and item.get("username")
    ]
    groups = configured + [
        g for g in db.get_recent_sessions(limit=300)
        if g.get("is_group") and g.get("username") not in {c["username"] for c in configured}
    ]
    if not groups:
        groups = db.get_groups(include_unnamed=True)
    if search:
        needle = search.casefold()
        groups = [g for g in groups if needle in g.get("name", "").casefold()]
    return groups


def choose_groups(groups: list[dict], limit: int) -> list[dict]:
    max_show = min(len(groups), limit)
    print("\n选择要回填历史总结的群聊")
    for idx, group in enumerate(groups[:max_show], 1):
        marker = " *" if idx == 1 else ""
        print(f"  {idx}. {group['name']}{marker}")
    if len(groups) > max_show:
        print(f"  ... 只显示前 {max_show} 个；可用 --search 缩小列表。")

    while True:
        raw = prompt("输入编号，多个群用逗号分隔", "1")
        try:
            selected = parse_selection(raw, max_show)
            if selected:
                return [groups[idx - 1] for idx in selected]
            print("至少选一个群。")
        except ValueError as exc:
            print(exc)


def date_range(days: int, end_date_text: str = "") -> list[datetime]:
    if end_date_text:
        end_date = datetime.strptime(end_date_text, "%Y-%m-%d").date()
    else:
        end_date = datetime.now().date()
    days = max(1, min(90, days))
    start_date = end_date - timedelta(days=days - 1)
    return [
        datetime.combine(start_date + timedelta(days=offset), dt_time.min)
        for offset in range(days)
    ]


def messages_for_day(db: WeChatDB, username: str, day: datetime) -> list[dict]:
    start_ts = day.timestamp()
    end_ts = (day + timedelta(days=1)).timestamp()
    messages = db.get_messages(username, since_ts=start_ts, limit=10000)
    return [m for m in messages if m.get("timestamp", 0) < end_ts]


def combine_summaries(summaries: list[dict]) -> str:
    if len(summaries) == 1:
        return summaries[0]["text"]
    parts = []
    for item in summaries:
        start_short = item["start"].split(" ", 1)[-1]
        end_short = item["end"].split(" ", 1)[-1]
        parts.append(
            f"## {start_short} ~ {end_short}（{item['count']}条消息）\n"
            f"{item['text']}"
        )
    return "\n\n".join(parts)


def topic_exists(store: KnowledgeStore, topic_key: str) -> bool:
    conn = store.connect()
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM topics WHERE topic_key = ? LIMIT 1",
            (topic_key,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def summarize_day(ai, db: WeChatDB, config: dict, group: dict, day: datetime) -> dict | None:
    messages = messages_for_day(db, group["username"], day)
    if not messages:
        return None

    chunks = [messages[i:i + CHUNK_SIZE] for i in range(0, len(messages), CHUNK_SIZE)]
    summaries = []
    for idx, chunk in enumerate(chunks, 1):
        chunk_text = db.format_messages_for_ai(
            chunk,
            show_group_nickname=config.get("show_group_nickname", True),
        )
        prompt_text = ai.build_prompt(
            group_name=group["name"],
            messages_text=chunk_text,
            start_time=chunk[0]["time_str"],
            end_time=chunk[-1]["time_str"],
            msg_count=len(chunk),
        )
        print(
            f"    第 {idx}/{len(chunks)} 段："
            f"{chunk[0]['time_str']} ~ {chunk[-1]['time_str']} · {len(chunk)} 条"
        )
        summary = ai.summarize(prompt_text)
        summaries.append({
            "text": summary,
            "start": chunk[0]["time_str"],
            "end": chunk[-1]["time_str"],
            "count": len(chunk),
        })

    return {
        "messages": messages,
        "summary": combine_summaries(summaries),
        "count": len(messages),
        "chunks": len(chunks),
        "start": messages[0]["time_str"],
        "end": messages[-1]["time_str"],
    }


def write_knowledge_note(store: KnowledgeStore, config: dict, group: dict, day: datetime, result: dict) -> str:
    day_text = day.strftime("%Y-%m-%d")
    topic_key = f"history-summary:{group['username']}:{day_text}"
    candidate = {
        "title": f"{group['name']} · {day_text} 历史总结",
        "summary": result["summary"],
        "topic_key": topic_key,
        "category": "技术方法",
        "entities": [group["name"], "历史总结"],
        "key_facts": [
            f"{day_text} 共 {result['count']} 条消息",
            f"时间范围：{result['start']} ~ {result['end']}",
            f"分段数：{result['chunks']}",
        ],
        "links": [],
        "event_type": "history_summary",
        "status_hint": "resolved",
    }
    event_config = dict(config)
    event_config["monitor_chat_display_name"] = group["name"]
    info = store.apply_event(
        candidate,
        result["messages"],
        event_config,
        {"relation": "new", "reason": "historical daily backfill"},
    )
    return info.get("knowledge_path", "")


def backfill(args: argparse.Namespace) -> int:
    if legacy_command_blocked():
        return 2
    config = load_config()
    keys = get_cached_keys()
    if not keys:
        print("没有找到数据库 key cache。先运行 ./启动.command 完成微信授权和 key 提取。")
        return 1
    if not config.get("db_dir") or not os.path.isdir(config["db_dir"]):
        print("没有找到 WeChat db_dir。先运行 ./启动.command 自动检测微信数据库路径。")
        return 1

    db = WeChatDB.for_runtime(config["db_dir"], keys, config=config)
    groups = list_groups(db, config, args.search)
    if not groups:
        print("没有找到群聊。")
        return 1
    selected_groups = choose_groups(groups, args.limit)

    days_default = str(args.days or 7)
    days = int(prompt("回填最近多少天（包含今天，最多 90）", days_default))
    dates = date_range(days, args.end_date)
    print(
        "\n会调用 AI 总结："
        f"{'、'.join(g['name'] for g in selected_groups)} · "
        f"{dates[0].strftime('%Y-%m-%d')} ~ {dates[-1].strftime('%Y-%m-%d')}"
    )
    print("同一天已存在的历史总结会跳过；如需重做，用 --force。")

    ai = create_provider(config)
    store = KnowledgeStore.from_config(config)
    written = []
    skipped = 0
    empty = 0

    for group in selected_groups:
        for day in dates:
            day_text = day.strftime("%Y-%m-%d")
            topic_key = f"history-summary:{group['username']}:{day_text}"
            if topic_exists(store, topic_key) and not args.force:
                print(f"\n[{group['name']}] {day_text}: 已存在，跳过")
                skipped += 1
                continue

            print(f"\n[{group['name']}] {day_text}: 读取消息...")
            result = summarize_day(ai, db, config, group, day)
            if not result:
                print("  没有消息")
                empty += 1
                continue
            path = write_knowledge_note(store, config, group, day, result)
            written.append(path)
            print(f"  已写入: {path}")

    print("\n历史总结回填完成")
    print(f"  写入: {len(written)} 篇")
    print(f"  跳过: {skipped} 篇")
    print(f"  无消息: {empty} 天")
    if written:
        print(f"  最近写入: {written[-1]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill historical WeChat summaries into Obsidian.")
    parser.add_argument("--search", default="", help="Filter group list by display name.")
    parser.add_argument("--limit", type=int, default=80, help="How many groups to show.")
    parser.add_argument("--days", type=int, default=7, help="Default number of days to backfill.")
    parser.add_argument("--end-date", default="", help="End date in YYYY-MM-DD; defaults to today.")
    parser.add_argument("--force", action="store_true", help="Create notes even if the same day already exists.")
    return backfill(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
