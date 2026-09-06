#!/usr/bin/env python3
"""Refresh observed WeChat keys; report source completeness separately."""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.key_extractor import recover_keys, process_lookup_available


@dataclass(frozen=True)
class RefreshResult:
    ok: bool
    key_count: int = 0
    missing_databases: list[str] | None = None
    message: str = ""
    status: str = "failed"
    reason: str = ""


def refresh_data_source() -> RefreshResult:
    if not process_lookup_available():
        return RefreshResult(False, message="当前进程无法检测 macOS process list；请在 Terminal/Finder 中运行刷新命令",
                             reason="process_lookup_unavailable")
    result = recover_keys()
    messages = {
        "fresh_verified": "本轮已验证当前可见数据库的 key；历史分片完整性仍须查看 Source inventory。",
        "partial": "仅部分数据库的 key 已验证；尚未恢复完整可读状态。",
        "cache_only": "本轮没有取得新的验证证据；保留旧 cache，不计为刷新成功。",
        "unsupported_build": "本轮未取得可验证 key，运行版本没有匹配的 protected profile；旧 cache 保持不变。",
        "failed": "本轮刷新未完成；请按原因排查，不要清空现有 cache 或重置 checkpoint。",
    }
    return RefreshResult(
        ok=result.ok,
        key_count=len(result.keys),
        missing_databases=list(result.missing_databases),
        message=messages.get(result.status, messages["failed"]),
        status=result.status,
        reason=result.reason,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh verified WeChat database keys.")
    parser.add_argument(
        "--allow-transient-wechat-source-read",
        action="store_true",
        help=(
            "Explicitly allow this one short-lived process to read the protected "
            "WeChat source; macOS may show one App Data consent prompt."
        ),
    )
    args = parser.parse_args(argv)
    if not args.allow_transient_wechat_source_read:
        print(
            "已在读取 WeChat source 之前停止：请优先在长驻菜单 app 里点击「🔄 刷新数据源」。\n"
            "只有确定要让这只短命 CLI 进程读取时，才加上 "
            "--allow-transient-wechat-source-read；macOS 可能另外弹出一次 App Data 权限。",
            file=sys.stderr,
        )
        return 2
    result = refresh_data_source()
    print("微信总结 数据源 key 验证")
    print(f"state: {result.status}")
    print(result.message)
    if result.reason:
        print(f"reason: {result.reason}")
    print(f"本轮验证可用 key: {result.key_count} 个")
    # Default output contains counts/codes, never source-relative paths.
    if result.missing_databases:
        print(f"未验证或不可读的当前源项: {len(result.missing_databases)} 个")
        return 2
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
