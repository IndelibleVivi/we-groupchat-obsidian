#!/usr/bin/env python3
"""Terminal wizard for configuring background group-chat monitoring."""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.config import (
    active_monitor_chats,
    load_config,
    merge_monitor_chat_preferences,
    normalize_path_value,
    update_config,
)
from core.key_extractor import get_cached_keys
from core.keychain import load_key, save_key
from core.knowledge import (
    HUMAN_AI_INTIMACY_PROFILE,
    KnowledgeMetadataQueryError,
    KnowledgeStore,
    TAXONOMY_PROFILES,
    ensure_obsidian_vault,
    safe_obsidian_subdir,
)
from core.monitor import reset_state_to_now, state_file_for_chat
from core.taxonomy_assignment import resolve_taxonomy_profile
from core.wechat_db import WeChatDB


DEFAULT_TOPIC = (
    "AI workflow / agent practice / Claude Code、Codex、memory、Obsidian、"
    "MCP、模型更新、工具链经验；请忽略普通闲聊，"
    "只在出现值得回看、能整理成知识笔记的内容时提醒。"
)
DEFAULT_SUBDIR = "微信群聊/关注推送"


def prompt(text: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    value = input(f"{text}{suffix}: ").strip()
    if value:
        return value
    return default or ""


def prompt_yes_no(text: str, default: bool = True) -> bool:
    mark = "Y/n" if default else "y/N"
    while True:
        value = input(f"{text} [{mark}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "是", "好"}:
            return True
        if value in {"n", "no", "否", "不用"}:
            return False
        print("请输入 y 或 n。")


def parse_selection(text: str, max_count: int) -> list[int]:
    selected: list[int] = []
    seen = set()
    for token in text.replace("，", ",").replace("、", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit():
            raise ValueError("请输入数字编号，多个编号用逗号分隔，例如 1,3,5")
        idx = int(token)
        if idx < 1 or idx > max_count:
            raise ValueError(f"编号 {idx} 超出范围 1-{max_count}")
        if idx not in seen:
            selected.append(idx)
            seen.add(idx)
    return selected


def choose_taxonomy_profile(group: dict, config: dict) -> str:
    username = str(group.get("username") or "").strip()
    resolution = resolve_taxonomy_profile(
        config,
        set(TAXONOMY_PROFILES),
        source_chat_username=username,
        source_chat=str(group.get("name") or ""),
        vault_chat_name=str(
            (config.get("monitor_chat_aliases") or {}).get(username) or ""
        ),
    )
    default = "1" if resolution.profile == HUMAN_AI_INTIMACY_PROFILE else "2"
    print(f"\n{group['name']} 的知识库分类")
    print("  1. 内置人机关系 taxonomy")
    print("  2. 自由分类")
    while True:
        value = prompt("选择分类方式", default)
        if value == "1":
            return HUMAN_AI_INTIMACY_PROFILE
        if value == "2":
            return ""
        print("请输入 1 或 2。")


def choose_vault_alias(group: dict, config: dict, candidates: list[str]) -> str:
    username = str(group.get("username") or "").strip()
    existing = str(
        (config.get("monitor_chat_aliases") or {}).get(username) or ""
    ).strip()
    if existing:
        return existing
    candidates = list(
        dict.fromkeys(str(value).strip() for value in candidates if str(value).strip())
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return str(group.get("name") or username).strip() or username
    print(f"\n{group['name']} 找到多个历史 vault 文件夹，请明确保留哪一个：")
    for index, candidate in enumerate(candidates, 1):
        print(f"  {index}. {candidate}")
    while True:
        value = prompt("选择稳定 vault 文件夹", "1")
        if value.isdigit() and 1 <= int(value) <= len(candidates):
            return candidates[int(value) - 1]
        print(f"请输入 1-{len(candidates)}。")


def candidate_obsidian_vaults() -> list[str]:
    roots = [
        Path("~/Library/Mobile Documents/iCloud~md~obsidian/Documents").expanduser(),
        Path("~/Library/Mobile Documents").expanduser(),
        Path("~/Documents").expanduser(),
        Path("~/Library/CloudStorage").expanduser(),
    ]
    candidates: list[str] = []
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        try:
            children = list(root.iterdir()) if root.name != "Documents" else [root, *root.iterdir()]
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            has_obsidian = (child / ".obsidian").is_dir()
            looks_like_vault = has_obsidian or root.name == "Documents"
            if not looks_like_vault:
                continue
            path = str(child)
            if path not in seen:
                candidates.append(path)
                seen.add(path)
    return candidates[:12]


def choose_obsidian_root(current: str) -> str:
    print("\nObsidian vault 路径")
    candidates = candidate_obsidian_vaults()
    default = os.path.expanduser(current or "")
    if candidates:
        for idx, path in enumerate(candidates, 1):
            marker = " 当前" if os.path.abspath(path) == os.path.abspath(default) else ""
            print(f"  {idx}. {path}{marker}")
        print("  0. 手动输入路径")
        raw = prompt("选择 vault 编号", "1" if not default or default not in candidates else str(candidates.index(default) + 1))
        if raw.isdigit():
            choice = int(raw)
            if choice == 0:
                return normalize_path_value(prompt("粘贴 vault 绝对路径", default))
            if 1 <= choice <= len(candidates):
                return candidates[choice - 1]
        print("没认出编号，改为把输入当路径。")
        return normalize_path_value(raw)
    return normalize_path_value(prompt("粘贴 vault 绝对路径", default))


def list_groups(db: WeChatDB, search: str = "") -> list[dict]:
    groups = [g for g in db.get_recent_sessions(limit=300) if g.get("is_group")]
    if not groups:
        groups = db.get_groups(include_unnamed=True)
    if search:
        needle = search.casefold()
        groups = [g for g in groups if needle in g.get("name", "").casefold()]
    return groups


def configure(args: argparse.Namespace) -> int:
    config = load_config()
    original_config = dict(config)
    keys = get_cached_keys()
    if not keys:
        print("没有找到可用的数据库 key cache。先运行一次 ./启动.command 完成微信授权和 key 提取。")
        return 1
    if not config.get("db_dir") or not os.path.isdir(config["db_dir"]):
        print("没有找到 WeChat db_dir。先运行一次 ./启动.command 让程序自动检测微信数据库路径。")
        return 1

    db = WeChatDB.for_runtime(config["db_dir"], keys, config=config)
    groups = list_groups(db, args.search)
    if not groups:
        print("没有找到群聊。可以先在微信里打开目标群，再重新运行这个配置。")
        return 1

    print("\n选择要关注的群聊")
    max_show = min(len(groups), args.limit)
    current_usernames = {
        item.get("username")
        for item in active_monitor_chats(config)
    }
    default_indexes = [
        str(i)
        for i, group in enumerate(groups[:max_show], 1)
        if group["username"] in current_usernames
    ]
    for idx, group in enumerate(groups[:max_show], 1):
        marker = " *" if group["username"] in current_usernames else ""
        print(f"  {idx}. {group['name']}{marker}")
    if len(groups) > max_show:
        print(f"  ... 只显示最近 {max_show} 个；可用 --search 缩小列表。")

    default_selection = ",".join(default_indexes) or "1"
    while True:
        raw = prompt("输入编号，多个群用逗号分隔", default_selection)
        try:
            selected = parse_selection(raw, max_show)
            if selected:
                break
            print("至少选一个群。")
        except ValueError as exc:
            print(exc)

    selected_groups = [groups[idx - 1] for idx in selected]
    for group in selected_groups:
        username = str(group.get("username") or "").strip()
        if not username.endswith("@chatroom"):
            print(
                f"拒绝保存 {group.get('name') or username}："
                "缺少稳定的 @chatroom username。"
            )
            return 1

    knowledge_store = KnowledgeStore.from_config(config, read_only=True)
    profile_by_username = {}
    alias_by_username = {}
    for group in selected_groups:
        username = str(group["username"]).strip()
        try:
            candidates = knowledge_store.vault_chat_alias_candidates(username)
        except KnowledgeMetadataQueryError:
            print("无法读取历史 vault metadata，配置未保存。")
            return 1
        profile_by_username[username] = choose_taxonomy_profile(group, config)
        alias_by_username[username] = choose_vault_alias(
            group,
            config,
            candidates,
        )
    config = merge_monitor_chat_preferences(
        config,
        selected_groups,
        profile_by_username=profile_by_username,
        alias_by_username=alias_by_username,
    )

    print("\nAI 设置")
    configured_provider = (config.get("ai_provider") or "").strip().lower()
    provider_default = "deepseek" if configured_provider in {"", "qwen"} else configured_provider
    provider = prompt("AI provider", provider_default).strip().lower()
    model_default = config.get("ai_model") or ("deepseek-v4-flash" if provider == "deepseek" else "")
    model = prompt("AI model", model_default).strip()
    if provider != "ollama":
        existing_key = load_key("ai-api-key")
        if existing_key:
            if prompt_yes_no("Keychain 里已有 API key，要更新吗？", False):
                key = getpass.getpass("输入新的 API key（不会显示）: ").strip()
                if key and not save_key("ai-api-key", key):
                    print("写入 Keychain 失败。")
                    return 1
        else:
            key = getpass.getpass("输入 API key（不会显示，会写入 macOS Keychain）: ").strip()
            if not key:
                print("没有 API key，无法启用云端模型。")
                return 1
            if not save_key("ai-api-key", key):
                print("写入 Keychain 失败。")
                return 1

    print("\n关注描述")
    topic = prompt("你想被提醒的内容", config.get("monitor_topic") or DEFAULT_TOPIC)
    interval = prompt("检查间隔分钟", str(config.get("monitor_interval_minutes") or 3))
    try:
        interval_minutes = max(1, min(1440, int(interval)))
    except ValueError:
        interval_minutes = 3

    root = choose_obsidian_root(config.get("monitor_obsidian_root", ""))
    subdir = safe_obsidian_subdir(prompt("vault 内输出子目录", config.get("monitor_obsidian_subdir") or DEFAULT_SUBDIR))
    ensure_obsidian_vault(root, obsidian_subdir=subdir)

    config["ai_provider"] = provider
    config["ai_model"] = model
    config["monitor_ai_provider"] = ""
    config["monitor_ai_model"] = ""
    config["monitor_chats"] = [
        {"username": group["username"], "name": group["name"]}
        for group in selected_groups
    ]
    first = selected_groups[0]
    config["monitor_chat_username"] = first["username"]
    config["monitor_chat_display_name"] = first["name"]
    config["monitor_topic"] = topic
    config["monitor_interval_minutes"] = interval_minutes
    config["monitor_obsidian_root"] = root
    config["monitor_obsidian_subdir"] = subdir
    config["monitor_enabled"] = True
    changed = {
        key: value
        for key, value in config.items()
        if key != "config_revision" and original_config.get(key) != value
    }
    update_config(patch=changed)

    for group in selected_groups:
        reset_state_to_now(state_file_for_chat(group["username"]))

    print("\n已保存关注推送配置：")
    print("  群聊: " + "、".join(group["name"] for group in selected_groups))
    print(f"  AI: {provider} / {model or 'default'}")
    print(f"  检查间隔: {interval_minutes} 分钟")
    print(f"  Obsidian: {os.path.join(root, subdir)}")
    print("\n现在运行 ./启动.command 即可开始后台跟踪新增消息。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure WeChat monitor without using the menu bar.")
    parser.add_argument("--search", default="", help="Filter group list by display name.")
    parser.add_argument("--limit", type=int, default=80, help="How many recent groups to show.")
    return configure(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
