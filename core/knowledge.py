"""Local knowledge base for monitor hits and Obsidian-friendly Markdown."""
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
import time
import unicodedata
from datetime import datetime
from urllib.parse import quote

from .config import DATA_DIR
from .link_preview import is_wechat_record_url
from .project_identity import PROJECT_SLUG
from .url_safety import redact_url_for_display, redact_urls_in_text
from .source_contract import (
    atomic_source_lines,
    aware_iso_from_timestamp,
    is_history_summary,
    projection_source_lines,
)
from .taxonomy_assignment import TaxonomyResolution, resolve_taxonomy_profile

KNOWLEDGE_DB = os.path.join(DATA_DIR, "monitor_knowledge.db")
OBSIDIAN_ROOT = os.path.join(DATA_DIR, "obsidian_knowledge")
OBSIDIAN_SUBDIR = "关注推送"

_PROJECTION_WRITE_LOCKS = tuple(threading.Lock() for _ in range(64))

RELATION_NOTIFY = {"new", "update", "contradiction"}
RELATION_LABELS = {
    "new": "新主题",
    "duplicate": "重复出现",
    "update": "新线索",
    "contradiction": "反转/辟谣",
}

CATEGORY_ALIASES = (
    ("群内八卦", ("八卦", "搞笑", "吃瓜", "瓜", "轶事")),
    ("自建app", ("自建app", "自建 app", "app新功能", "app 新功能")),
    ("设计讨论", ("设计", "自主权", "自主性", "agent")),
    ("技术方法", ("教程", "技巧", "方法", "配置", "资源", "实践")),
    ("工具更新", ("工具", "产品", "应用", "新功能", "更新")),
    ("AI实验", ("实验", "报告", "测评", "修复")),
    ("AI模型", ("模型", "安全", "发布传闻")),
)

HUMAN_AI_INTIMACY_PROFILE = "human_ai_intimacy_v1"
HUMAN_AI_INTIMACY_CATEGORIES = (
    "AI关系与理论",
    "互动实验与玩法",
    "记忆与连续性",
    "角色设计与表达",
    "模型与平台",
    "工具与方法",
    "资源线索",
    "群内动态",
    "风险与边界",
    "待归类",
)
HUMAN_AI_INTIMACY_CATEGORY_MAP = {
    "AI关系与理论": (
        "AI关系与理论",
        "AI伴侣",
        "人机恋",
        "人机关系",
        "人机关系思考",
        "人机关系讨论",
        "AI关系/存在论",
        "AI互动文化",
    ),
    "互动实验与玩法": (
        "互动实验与玩法",
        "AI伴侣交互",
        "AI互动玩法",
        "AI intimacy/玩法",
        "AI互动",
        "AI交互观察",
        "AI情感互动",
        "互动玩法",
        "使用经验",
        "AI实验",
    ),
    "记忆与连续性": (
        "记忆与连续性",
        "AI记忆系统",
        "群聊问答与经验",
    ),
    "角色设计与表达": (
        "角色设计与表达",
        "设计讨论",
    ),
    "模型与平台": (
        "模型与平台",
        "AI模型",
        "服务动态",
        "服务事件",
    ),
    "工具与方法": (
        "工具与方法",
        "工具更新",
        "群内技术讨论",
        "技术方法",
        "自建app",
    ),
    "资源线索": (
        "资源线索",
        "资源",
        "开源项目",
    ),
    "群内动态": (
        "群内动态",
        "群聊观察",
        "群内八卦",
    ),
    "风险与边界": (
        "风险与边界",
        "风险提醒",
        "硬件交互 & 运营风险",
        "AI政策",
        "AI事件",
    ),
    "待归类": (
        "待归类",
        "多话题综合",
        "综合",
        "未分类",
    ),
}
LEGACY_TOOL_MODEL_CATEGORY = "工具与模型"
LEGACY_TOOL_MODEL_RULES = (
    ("风险与边界", ("风险", "封号", "KYC", "kyc", "政策", "审查", "安全", "异常", "连坐", "清洗", "电话验证")),
    ("资源线索", ("资源", "开源", "链接", "仓库", "分享", "项目", "邀请")),
    (
        "模型与平台",
        (
            "模型",
            "Claude",
            "GPT",
            "Gemini",
            "Grok",
            "Kimi",
            "GLM",
            "Qwen",
            "qwen",
            "Anthropic",
            "anthropic",
            "路由",
            "上下文",
            "额度",
            "限额",
            "充值",
            "缓存",
            "API",
            "api",
            "平台",
            "服务",
            "灰测",
            "发布",
            "版本",
            "降智",
            "Pro",
            "Max",
            "账号",
        ),
    ),
    (
        "工具与方法",
        (
            "工具",
            "教程",
            "配置",
            "方法",
            "技巧",
            "脚本",
            "修复",
            "Tmux",
            "tmux",
            "workflow",
            "工作流",
            "MCP",
            "mcp",
            "Obsidian",
            "GitHub",
            "PWA",
            "前端",
            "app",
            "自建",
            "hook",
            "部署",
        ),
    ),
)
TAXONOMY_PROFILES = {
    HUMAN_AI_INTIMACY_PROFILE: {
        "version": 2,
        "folder_categories": HUMAN_AI_INTIMACY_CATEGORIES,
        "unknown_policy": "待归类",
        "category_map": HUMAN_AI_INTIMACY_CATEGORY_MAP,
    }
}


class KnowledgeMetadataQueryError(RuntimeError):
    """Privacy-safe failure for read-only knowledge metadata queries."""


def taxonomy_profile_for_chat(*values):
    for value in values:
        resolution = resolve_taxonomy_profile(
            {},
            set(TAXONOMY_PROFILES),
            source_chat=str(value or ""),
        )
        if resolution.profile:
            return resolution.profile
    return ""

OBSIDIAN_INDEX_CATEGORIES = (
    "AI模型",
    "工具更新",
    "技术方法",
    "AI实验",
    "自建app",
    "设计讨论",
    "群内八卦",
)
OBSIDIAN_CATEGORY_INDEX_FILENAME = "目录.md"
OBSIDIAN_DATE_INDEX_FILENAME = "00-按日期.md"
OBSIDIAN_DATE_INDEX_FALLBACK_FILENAME = "00-按日期.generated.md"
OBSIDIAN_DATE_INDEX_MARKER_V1 = "<!-- wechat-summary:managed-date-index v1 -->"
OBSIDIAN_DATE_INDEX_MARKER_V2 = "<!-- wechat-summary:managed-date-index v2 -->"
OBSIDIAN_DATE_INDEX_MARKER_V3 = f"<!-- {PROJECT_SLUG}:managed-date-index v3 -->"
OBSIDIAN_DATE_INDEX_MARKER = OBSIDIAN_DATE_INDEX_MARKER_V3
OBSIDIAN_DATE_INDEX_MARKERS = (
    OBSIDIAN_DATE_INDEX_MARKER_V3,
    OBSIDIAN_DATE_INDEX_MARKER_V2,
    OBSIDIAN_DATE_INDEX_MARKER_V1,
)
OBSIDIAN_HOME_LINKS = tuple(
    (
        f"[[{OBSIDIAN_SUBDIR}/{category}]]",
        f"[[{OBSIDIAN_SUBDIR}/{category}/目录|{category}]]",
    )
    for category in OBSIDIAN_INDEX_CATEGORIES
)

OBSIDIAN_APP_CONFIG = {
    "alwaysUpdateLinks": True,
    "attachmentFolderPath": "附件",
    "newFileLocation": "current",
    "promptDelete": False,
    "showInlineTitle": True,
    "spellcheck": False,
    "livePreview": True,
}

OBSIDIAN_CORE_PLUGINS = {
    "file-explorer": True,
    "global-search": True,
    "switcher": True,
    "graph": True,
    "backlink": True,
    "outgoing-link": True,
    "tag-pane": True,
    "page-preview": True,
    "daily-notes": False,
    "templates": False,
    "note-composer": False,
    "command-palette": True,
    "slash-command": False,
    "editor-status": False,
    "markdown-importer": False,
    "zk-prefixer": False,
    "random-note": False,
    "outline": True,
    "word-count": False,
    "slides": False,
    "audio-recorder": False,
    "workspaces": False,
    "file-recovery": True,
    "publish": False,
    "sync": False,
    "canvas": True,
    "footnotes": False,
    "properties": True,
    "bookmarks": True,
    "bases": True,
    "webviewer": False,
}

OBSIDIAN_APPEARANCE_CONFIG = {
    "accentColor": "#2563eb",
    "baseFontSize": 15,
    "nativeMenus": True,
    "showViewHeader": True,
}

OBSIDIAN_HOME_NOTE = """# 微信关注推送知识库

这里是微信关注推送自动沉淀的 Obsidian vault。新推送会继续写入 `关注推送/`，文件名和标题都带日期时间。

## 快速入口

- [[关注推送/AI模型/目录|AI模型]]
- [[关注推送/工具更新/目录|工具更新]]
- [[关注推送/技术方法/目录|技术方法]]
- [[关注推送/AI实验/目录|AI实验]]
- [[关注推送/自建app/目录|自建app]]
- [[关注推送/设计讨论/目录|设计讨论]]
- [[关注推送/群内八卦/目录|群内八卦]]

## 常用搜索

```query
path:"关注推送" link
```

```query
path:"关注推送" 新功能 OR 更新 OR 教程 OR 实验 OR 修复
```

## 用法

- 左侧文件夹：按分类看每条推送。
- 顶部搜索：搜产品名、模型名、链接域名或关键词。
- 右侧反向链接/出链：看这条内容和哪些条目有关。
- Graph：点左侧网络图标，看话题之间的连接。
"""


def _json_dumps(value):
    return json.dumps(value or [], ensure_ascii=False)


def _json_loads(value, default=None):
    try:
        data = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return [] if default is None else default
    return data if data is not None else ([] if default is None else default)


def _row_get(row, key, default=""):
    try:
        if key in row.keys():
            return row[key]
    except (AttributeError, KeyError, IndexError):
        pass
    return default


def _normalize_list(value, limit=12):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []

    result = []
    seen = set()
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text[:180])
        if len(result) >= limit:
            break
    return result


def _frontmatter_bool(value):
    return "true" if value else "false"


def _frontmatter_string_list(name, values):
    values = _normalize_list(values)
    if not values:
        return f"{name}: []"
    lines = [f"{name}:"]
    lines.extend(f"  - {_frontmatter_scalar(v)}" for v in values)
    return "\n".join(lines)


def safe_path_part(value, fallback="未分类", max_len=80):
    """Make a filesystem-safe but readable path part."""
    text = str(value or "").strip()
    chars = []
    for ch in text:
        if ch in '<>:"/\\|?*':
            chars.append(" ")
            continue
        category = unicodedata.category(ch)
        if category[0] in {"L", "N"} or ch in {" ", "-", "_", ".", "·", "（", "）", "(", ")", "[", "]", "+"}:
            chars.append(ch)
        else:
            chars.append(" ")

    cleaned = re.sub(r"\s+", " ", "".join(chars)).strip(" .")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_len].rstrip(" .") or fallback


def _frontmatter_scalar(value):
    text = str(value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _frontmatter_list(name, values):
    values = _normalize_list(values)
    if not values:
        return f"{name}: []"
    lines = [f"{name}:"]
    lines.extend(f"  - {_frontmatter_scalar(v)}" for v in values)
    return "\n".join(lines)


def _resource_types(links=None, files=None):
    types = []
    if links:
        types.append("link")
    if files:
        types.append("file")
    return types


def _resource_prefix(links=None, files=None):
    types = set(_resource_types(links, files))
    if types == {"link", "file"}:
        return "[链接+文件]"
    if "link" in types:
        return "[链接]"
    if "file" in types:
        return "[文件]"
    return ""


def _display_title(title, links=None, files=None):
    title = redact_urls_in_text(title or "关注内容").strip() or "关注内容"
    prefix = _resource_prefix(links, files)
    return f"{prefix} {title}" if prefix else title


def _file_url(path):
    text = str(path or "").strip()
    if not text:
        return ""
    return "file://" + quote(text)


def _month_from_time(value):
    match = re.search(r"\d{4}-\d{2}", str(value or ""))
    return match.group(0) if match else ""


def _file_month_dir(config, month):
    if not month:
        return ""
    db_dir = os.path.abspath(os.path.expanduser(str(config.get("db_dir") or "")))
    if not db_dir:
        return ""
    wxid_dir = os.path.dirname(db_dir.rstrip(os.sep))
    candidate = os.path.join(wxid_dir, "msg", "file", month)
    return candidate if os.path.isdir(candidate) else ""


def _normalize_file_refs(value, limit=20):
    if not value:
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []

    refs = []
    seen = set()
    for item in value:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        ref = {
            "name": name[:180],
            "time": str(item.get("time") or "").strip()[:40],
            "sender": str(item.get("sender") or "").strip()[:80],
            "month": str(item.get("month") or "").strip()[:7],
            "month_dir": str(item.get("month_dir") or "").strip(),
        }
        key = (ref["name"].lower(), ref["month"], ref["sender"].lower())
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
        if len(refs) >= limit:
            break
    return refs


def _merge_file_refs(old_values, new_values, limit=30):
    refs = []
    seen = set()
    for ref in _normalize_file_refs(old_values, limit=limit) + _normalize_file_refs(new_values, limit=limit):
        key = (ref["name"].lower(), ref["month"], ref["sender"].lower())
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
        if len(refs) >= limit:
            break
    return refs


def extract_file_refs(messages, config=None, limit=20):
    """Extract file mentions from cleaned WeChat message text.

    WeChat file attachments are stored in monthly folders. The message text gives
    us the file name reliably; the monthly folder is a best-effort Finder hint.
    """
    config = config or {}
    refs = []
    seen = set()
    for msg in messages or []:
        text = str(msg.get("text") or msg.get("content") or "")
        for match in re.finditer(r"\[文件\]\s*([^\n\r`]+)", text):
            name = re.split(r"\s{2,}|\s+https?://", match.group(1).strip(), maxsplit=1)[0].strip(" -:：")
            if not name:
                continue
            time_text = str(msg.get("time_str") or "").strip()
            month = _month_from_time(time_text)
            sender = str(msg.get("sender") or msg.get("group_nickname") or "").strip()
            month_dir = _file_month_dir(config, month)
            ref = {
                "name": name[:180],
                "time": time_text[:40],
                "sender": sender[:80],
                "month": month,
                "month_dir": month_dir,
            }
            key = (ref["name"].lower(), ref["month"], ref["sender"].lower())
            if key in seen:
                continue
            seen.add(key)
            refs.append(ref)
            if len(refs) >= limit:
                return refs
    return refs


def _truncate(value, limit):
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _note_time(value):
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", text)
    if match:
        return match.group(0)
    return text[:16]


def _path_time(value):
    return _note_time(value).replace(":", "-")


def _compact_path_date(value):
    match = re.search(r"\d{4}-(\d{2})-(\d{2})", str(value or ""))
    return f"{match.group(1)}-{match.group(2)}" if match else ""


def _note_heading(topic):
    return _display_title(topic["title"], topic.get("links"), topic.get("files"))


def _obsidian_link(obsidian_path, title):
    if not obsidian_path:
        return f"[[{title}]]"
    target = os.path.splitext(obsidian_path)[0]
    return f"[[{target}|{title}]]"


def _render_relation_markdown_line(relation, obsidian_path, title):
    return f"- {relation}:: {_obsidian_link(obsidian_path, redact_urls_in_text(title))}"


def _is_default_obsidian_root(path):
    return os.path.abspath(os.path.expanduser(path or "")) == os.path.abspath(OBSIDIAN_ROOT)


def _write_json_if_missing(path, data):
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return True


def _write_text_if_missing(path, text):
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n")
    return True


def _write_or_migrate_home_note(path):
    if not os.path.exists(path):
        return _write_text_if_missing(path, OBSIDIAN_HOME_NOTE)

    try:
        with open(path, encoding="utf-8") as f:
            current = f.read()
    except OSError:
        return False

    if not current.startswith("# 微信关注推送知识库"):
        return False

    updated = current
    for old_link, new_link in OBSIDIAN_HOME_LINKS:
        updated = updated.replace(f"- {old_link}", f"- {new_link}")
    if updated == current:
        return False

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(updated)
        return True
    except OSError:
        return False


def _category_index_text(category, obsidian_subdir=OBSIDIAN_SUBDIR):
    return f"""# {category}

```query
path:"{obsidian_subdir}/{category}"
```
"""


def _remove_generated_legacy_category_index(root, category, obsidian_subdir=OBSIDIAN_SUBDIR):
    legacy_rel_path = os.path.join(obsidian_subdir, f"{safe_path_part(category)}.md")
    legacy_path = os.path.join(root, legacy_rel_path)
    if not os.path.isfile(legacy_path):
        return False

    try:
        with open(legacy_path, encoding="utf-8") as f:
            current = f.read().strip()
    except OSError:
        return False

    if current != _category_index_text(category, obsidian_subdir).strip():
        return False

    try:
        os.remove(legacy_path)
        return True
    except OSError:
        return False


def ensure_obsidian_vault(obsidian_root=OBSIDIAN_ROOT, include_app_config=None, obsidian_subdir=OBSIDIAN_SUBDIR):
    """Create the app-owned Obsidian vault shell without touching custom vault UI."""
    root = os.path.expanduser(obsidian_root or OBSIDIAN_ROOT)
    obsidian_subdir = safe_obsidian_subdir(obsidian_subdir)
    created = []

    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(root, obsidian_subdir), exist_ok=True)

    if include_app_config is None:
        include_app_config = _is_default_obsidian_root(root)
    if not include_app_config:
        return {"root": root, "created": created}

    obsidian_dir = os.path.join(root, ".obsidian")
    if _write_json_if_missing(os.path.join(obsidian_dir, "app.json"), OBSIDIAN_APP_CONFIG):
        created.append(".obsidian/app.json")
    if _write_json_if_missing(os.path.join(obsidian_dir, "core-plugins.json"), OBSIDIAN_CORE_PLUGINS):
        created.append(".obsidian/core-plugins.json")
    if _write_json_if_missing(os.path.join(obsidian_dir, "appearance.json"), OBSIDIAN_APPEARANCE_CONFIG):
        created.append(".obsidian/appearance.json")
    if _write_or_migrate_home_note(os.path.join(root, "首页.md")):
        created.append("首页.md")

    for category in OBSIDIAN_INDEX_CATEGORIES:
        rel_path = os.path.join(
            obsidian_subdir,
            safe_path_part(category),
            OBSIDIAN_CATEGORY_INDEX_FILENAME,
        )
        text = _category_index_text(category, obsidian_subdir)
        if _write_text_if_missing(os.path.join(root, rel_path), text):
            created.append(rel_path)
        _remove_generated_legacy_category_index(root, category, obsidian_subdir)

    return {"root": root, "created": created}


def safe_obsidian_subdir(value):
    text = str(value or OBSIDIAN_SUBDIR).strip().strip("/")
    parts = []
    for part in re.split(r"[/\\]+", text):
        clean = safe_path_part(part, "", max_len=80)
        if clean and clean not in {".", ".."}:
            parts.append(clean)
    return os.path.join(*parts) if parts else OBSIDIAN_SUBDIR


def build_message_hash(messages):
    h = hashlib.sha256()
    for msg in messages:
        for key in ("timestamp", "sender", "text", "content"):
            h.update(str(msg.get(key, "")).encode("utf-8", errors="ignore"))
            h.update(b"\0")
    return h.hexdigest()


def message_excerpt(messages, limit=8):
    lines = []
    for msg in messages[:limit]:
        time_text = msg.get("time_str") or ""
        sender = msg.get("sender") or msg.get("group_nickname") or ""
        text = msg.get("text") or msg.get("content") or ""
        lines.append(_truncate(f"[{time_text}] {sender}: {text}", 240))
    if len(messages) > limit:
        lines.append(f"... 另有 {len(messages) - limit} 条")
    return "\n".join(lines)


def _chat_aliases(config):
    aliases = config.get("monitor_chat_aliases") if isinstance(config, dict) else {}
    return aliases if isinstance(aliases, dict) else {}


def vault_chat_name(config):
    source_chat = str(config.get("monitor_chat_display_name") or "监控群聊").strip() or "监控群聊"
    username = str(config.get("monitor_chat_username") or "").strip()
    alias = str(_chat_aliases(config).get(username) or "").strip() if username else ""
    return safe_path_part(alias or source_chat, "未命名群聊", max_len=80)


def event_context(messages, config):
    senders = []
    seen = set()
    for msg in messages:
        sender = str(msg.get("sender") or msg.get("group_nickname") or "").strip()
        if sender and sender not in seen:
            seen.add(sender)
            senders.append(sender[:80])
        if len(senders) >= 12:
            break

    return {
        "source_chat": config.get("monitor_chat_display_name", "监控群聊"),
        "source_chat_username": str(config.get("monitor_chat_username") or "").strip(),
        "vault_chat_name": vault_chat_name(config),
        "window_start": messages[0].get("time_str", "") if messages else "",
        "window_end": messages[-1].get("time_str", "") if messages else "",
        "senders": senders,
        "message_hash": build_message_hash(messages),
        "messages_excerpt": message_excerpt(messages),
        "files": extract_file_refs(messages, config),
    }


def attachment_resources(messages, config):
    """Normalize resource envelopes for the canonical attachment outbox."""
    resources = []
    source_chat_username = str(config.get("monitor_chat_username") or "").strip()
    for message_index, message in enumerate(messages or []):
        message_resources = message.get("resources")
        if not isinstance(message_resources, list):
            message_resources = []
        if not message_resources:
            text = str(message.get("text") or message.get("content") or "")
            for index, match in enumerate(re.finditer(r"\[文件\]\s*([^\n\r`]+)", text)):
                name = match.group(1).strip(" -:：")
                if name:
                    message_resources.append({
                        "kind": "file",
                        "resource_index": index,
                        "original_name": name[:240],
                        "extension": os.path.splitext(name)[1].lstrip(".")[:20],
                    })
        if not message_resources:
            continue

        source_message_id = str(message.get("source_message_id") or "").strip()
        if not source_message_id:
            identity = "\0".join([
                "knowledge-source-message-v1",
                source_chat_username,
                str(message.get("timestamp") or ""),
                str(message.get("sender") or message.get("group_nickname") or ""),
                str(message.get("text") or message.get("content") or ""),
                str(message_index),
            ])
            source_message_id = "wgmsg_" + hashlib.sha256(identity.encode()).hexdigest()[:32]

        time_text = str(message.get("time_str") or "")
        month = _month_from_time(time_text)
        sender = str(message.get("sender") or message.get("group_nickname") or "").strip()[:80]
        try:
            source_timestamp = float(message.get("timestamp") or 0)
        except (TypeError, ValueError):
            source_timestamp = 0
        for fallback_index, raw in enumerate(message_resources):
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "").strip().lower()
            if kind not in {"file", "image"}:
                continue
            try:
                resource_index = int(raw.get("resource_index", fallback_index))
            except (TypeError, ValueError):
                resource_index = fallback_index
            normalized = {
                "kind": kind,
                "resource_index": max(0, resource_index),
                "original_name": str(raw.get("original_name") or "").strip()[:240],
                "declared_size": raw.get("declared_size"),
                "declared_hash": str(raw.get("declared_hash") or raw.get("md5") or "").strip().lower()[:128],
                "attach_id": str(raw.get("attach_id") or "").strip()[:240],
                "extension": str(raw.get("extension") or "").strip().lstrip(".")[:20],
                "source_message_id": source_message_id,
                "source_month": month,
                "source_time": time_text[:40],
                "source_timestamp": source_timestamp,
                "source_sender": sender,
            }
            if normalized["declared_size"] is not None:
                try:
                    normalized["declared_size"] = max(0, int(normalized["declared_size"]))
                except (TypeError, ValueError):
                    normalized["declared_size"] = None
            resources.append(normalized)
    return resources


def _is_history_summary_candidate(candidate):
    topic_key = str(candidate.get("topic_key") or "")
    event_type = str(candidate.get("event_type") or "")
    return topic_key.startswith("history-summary:") or event_type == "history_summary"


def _is_history_summary_row(row):
    topic_key = str(row["topic_key"] or "")
    title = str(row["title"] or "")
    return topic_key.startswith("history-summary:") or "历史总结" in title


class KnowledgeStore:
    """SQLite-backed monitor knowledge base with Markdown mirror output."""

    def __init__(
        self,
        db_path=KNOWLEDGE_DB,
        obsidian_root=OBSIDIAN_ROOT,
        obsidian_subdir=OBSIDIAN_SUBDIR,
        now_func=time.time,
        read_only=False,
        taxonomy_assignments=None,
        taxonomy_aliases=None,
        attachment_archive_root=None,
    ):
        self.db_path = os.path.expanduser(db_path)
        self.obsidian_root = os.path.expanduser(obsidian_root)
        self.obsidian_subdir = safe_obsidian_subdir(obsidian_subdir)
        self.now_func = now_func
        self.read_only = read_only
        self.taxonomy_assignments = dict(taxonomy_assignments or {})
        self.taxonomy_aliases = dict(taxonomy_aliases or {})
        self.attachment_archive_root = os.path.expanduser(
            attachment_archive_root or os.path.join(DATA_DIR, "attachment_archive")
        )

    @classmethod
    def from_config(cls, config, now_func=time.time, read_only=False):
        return cls(
            config.get("monitor_knowledge_db") or KNOWLEDGE_DB,
            config.get("monitor_obsidian_root") or OBSIDIAN_ROOT,
            config.get("monitor_obsidian_subdir") or OBSIDIAN_SUBDIR,
            now_func=now_func,
            read_only=read_only,
            taxonomy_assignments=config.get("monitor_chat_taxonomy_profiles") or {},
            taxonomy_aliases=config.get("monitor_chat_aliases") or {},
            attachment_archive_root=config.get("attachment_archive_root"),
        )

    def connect(self):
        if self.read_only and not os.path.exists(self.db_path):
            return None
        if not self.read_only:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        if not self.read_only:
            self._ensure_schema(conn)
        return conn

    def vault_chat_alias_candidates(self, username: str) -> list[str]:
        username = str(username or "").strip()
        if not username:
            return []
        conn = None
        try:
            conn = self.connect()
            if conn is None:
                return []
            rows = conn.execute(
                """
                SELECT DISTINCT vault_chat_name
                FROM topics
                WHERE source_chat_username = ?
                  AND TRIM(COALESCE(vault_chat_name, '')) <> ''
                ORDER BY vault_chat_name
                """,
                (username,),
            ).fetchall()
            return [str(row[0]) for row in rows]
        except sqlite3.Error as exc:
            raise KnowledgeMetadataQueryError(
                "knowledge metadata query failed"
            ) from exc
        finally:
            if conn is not None:
                conn.close()

    def _ensure_schema(self, conn):
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS topics (
                topic_id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_key TEXT,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                entities_json TEXT NOT NULL,
                semantic_tags_json TEXT NOT NULL DEFAULT '[]',
                key_facts_json TEXT NOT NULL,
                links_json TEXT NOT NULL,
                files_json TEXT NOT NULL DEFAULT '[]',
                source_chat TEXT NOT NULL,
                source_chat_username TEXT NOT NULL DEFAULT '',
                vault_chat_name TEXT NOT NULL DEFAULT '',
                taxonomy_profile TEXT NOT NULL DEFAULT '',
                taxonomy_version INTEGER NOT NULL DEFAULT 0,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                obsidian_path TEXT NOT NULL,
                event_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_topics_topic_key ON topics(topic_key);
            CREATE INDEX IF NOT EXISTS idx_topics_last_seen ON topics(last_seen);

            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER NOT NULL,
                relation TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                category TEXT NOT NULL,
                semantic_tags_json TEXT NOT NULL DEFAULT '[]',
                event_type TEXT NOT NULL,
                status_hint TEXT NOT NULL,
                source_chat TEXT NOT NULL,
                source_chat_username TEXT NOT NULL DEFAULT '',
                vault_chat_name TEXT NOT NULL DEFAULT '',
                taxonomy_profile TEXT NOT NULL DEFAULT '',
                taxonomy_version INTEGER NOT NULL DEFAULT 0,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                senders_json TEXT NOT NULL,
                links_json TEXT NOT NULL,
                files_json TEXT NOT NULL DEFAULT '[]',
                message_hash TEXT NOT NULL,
                source_batch_id TEXT NOT NULL DEFAULT '',
                messages_excerpt TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(topic_id) REFERENCES topics(topic_id)
            );

            CREATE INDEX IF NOT EXISTS idx_events_topic_id ON events(topic_id);
            CREATE INDEX IF NOT EXISTS idx_events_message_hash ON events(message_hash);

            CREATE TABLE IF NOT EXISTS daily_digest_changes (
                change_id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_start TEXT NOT NULL DEFAULT '',
                window_end TEXT NOT NULL DEFAULT '',
                fallback_timestamp REAL NOT NULL DEFAULT 0,
                change_kind TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS relations (
                relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_topic_id INTEGER NOT NULL,
                target_topic_id INTEGER NOT NULL,
                relation TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(source_topic_id, target_topic_id, relation)
            );

            CREATE TABLE IF NOT EXISTS attachment_mentions (
                mention_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER,
                topic_id INTEGER,
                source_message_id TEXT NOT NULL,
                resource_index INTEGER NOT NULL,
                kind TEXT NOT NULL,
                original_name TEXT NOT NULL DEFAULT '',
                declared_size INTEGER,
                declared_hash TEXT NOT NULL DEFAULT '',
                attach_id TEXT NOT NULL DEFAULT '',
                extension TEXT NOT NULL DEFAULT '',
                source_month TEXT NOT NULL DEFAULT '',
                source_time TEXT NOT NULL DEFAULT '',
                source_timestamp REAL NOT NULL DEFAULT 0,
                source_sender TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                resolution_method TEXT NOT NULL DEFAULT '',
                object_sha256 TEXT NOT NULL DEFAULT '',
                last_error_code TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(event_id, source_message_id, resource_index),
                FOREIGN KEY(event_id) REFERENCES events(event_id),
                FOREIGN KEY(topic_id) REFERENCES topics(topic_id)
            );

            CREATE INDEX IF NOT EXISTS idx_attachment_mentions_status
                ON attachment_mentions(status, mention_id);
            CREATE INDEX IF NOT EXISTS idx_attachment_mentions_topic
                ON attachment_mentions(topic_id, mention_id);

            CREATE TABLE IF NOT EXISTS attachment_objects (
                sha256 TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                object_relpath TEXT NOT NULL UNIQUE,
                original_name TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS attachment_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                mention_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                resolution_method TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                FOREIGN KEY(mention_id) REFERENCES attachment_mentions(mention_id)
            );

            CREATE TABLE IF NOT EXISTS attachment_worker_state (
                worker_name TEXT PRIMARY KEY,
                wake_generation INTEGER NOT NULL DEFAULT 0,
                drained_generation INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS topic_fts USING fts5(
                topic_id UNINDEXED,
                title,
                category,
                summary,
                entities,
                key_facts,
                links
            );
            """
        )
        self._ensure_column(conn, "topics", "files_json", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column(conn, "topics", "source_chat_username", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "topics", "vault_chat_name", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "topics", "semantic_tags_json", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column(conn, "topics", "taxonomy_profile", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "topics", "taxonomy_version", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "events", "files_json", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column(conn, "events", "source_chat_username", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "events", "vault_chat_name", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "events", "semantic_tags_json", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column(conn, "events", "taxonomy_profile", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "events", "taxonomy_version", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "events", "source_batch_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "attachment_mentions", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "attachment_mentions", "next_retry_at", "REAL NOT NULL DEFAULT 0")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_attachment_mentions_retry
            ON attachment_mentions(status, next_retry_at, mention_id)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_events_source_batch
            ON events(source_batch_id)
            WHERE source_batch_id <> ''
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO attachment_worker_state(
                worker_name, wake_generation, drained_generation, updated_at
            ) VALUES ('attachment_archive', 0, 0, 0)
            """
        )
        conn.commit()

    @staticmethod
    def _ensure_column(conn, table, column, definition):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def find_candidates(self, candidate, limit=5):
        conn = self.connect()
        if conn is None:
            return []
        try:
            seen = set()
            rows = []

            topic_key = candidate.get("topic_key", "")
            if topic_key:
                for row in conn.execute(
                    "SELECT * FROM topics WHERE topic_key = ? ORDER BY updated_at DESC LIMIT ?",
                    (topic_key, limit),
                ):
                    rows.append(row)
                    seen.add(row["topic_id"])

            fts_query = self._build_fts_query(candidate)
            if fts_query:
                try:
                    for row in conn.execute(
                        """
                        SELECT t.*
                        FROM topic_fts f
                        JOIN topics t ON t.topic_id = f.topic_id
                        WHERE topic_fts MATCH ?
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (fts_query, limit * 2),
                    ):
                        if row["topic_id"] not in seen:
                            rows.append(row)
                            seen.add(row["topic_id"])
                except sqlite3.Error:
                    pass

            for row in conn.execute("SELECT * FROM topics ORDER BY updated_at DESC LIMIT 80"):
                if row["topic_id"] not in seen:
                    rows.append(row)
                    seen.add(row["topic_id"])

            scored = []
            candidate_is_history_summary = _is_history_summary_candidate(candidate)
            for row in rows:
                if not candidate_is_history_summary and _is_history_summary_row(row):
                    continue
                score = self._score_candidate(candidate, row)
                if score > 0:
                    scored.append((score, row))

            scored.sort(key=lambda item: item[0], reverse=True)
            return [self._topic_dict(row, score) for score, row in scored[:limit]]
        except sqlite3.Error:
            if self.read_only:
                return []
            raise
        finally:
            conn.close()

    def apply_event(
        self,
        candidate,
        messages,
        config,
        relation_decision,
        *,
        source_batch_id="",
    ):
        if self.read_only:
            raise RuntimeError("knowledge store is read-only")

        relation = normalize_relation(relation_decision.get("relation"))
        target_topic_id = relation_decision.get("target_topic_id")
        reason = str(relation_decision.get("reason") or "").strip()
        ctx = event_context(messages, config)
        candidate = self._prepare_candidate_for_context(candidate, ctx, config)
        now = self.now_func()
        source_batch_id = str(source_batch_id or "").strip()[:96]

        conn = self.connect()
        try:
            if source_batch_id:
                existing = self._source_batch_result(conn, source_batch_id)
                if existing:
                    return self._repair_reused_source_batch_projection(conn, existing)
            projection_warnings = []
            linked_topic_id = None
            if relation == "new" or not target_topic_id:
                topic_id = self._create_topic(conn, candidate, ctx, now)
                relation = "new"
            else:
                row = conn.execute("SELECT * FROM topics WHERE topic_id = ?", (target_topic_id,)).fetchone()
                if row is None:
                    topic_id = self._create_topic(conn, candidate, ctx, now)
                    relation = "new"
                elif relation in {"update", "contradiction"}:
                    linked_topic_id = int(row["topic_id"])
                    topic_id = self._create_topic(conn, candidate, ctx, now)
                else:
                    topic_id = int(row["topic_id"])

            try:
                event_id = self._insert_event(
                    conn,
                    topic_id,
                    candidate,
                    ctx,
                    relation,
                    now,
                    source_batch_id=source_batch_id,
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                existing = (
                    self._source_batch_result(conn, source_batch_id)
                    if source_batch_id
                    else None
                )
                if existing:
                    return self._repair_reused_source_batch_projection(conn, existing)
                raise
            self._register_attachment_mentions(
                conn,
                event_id,
                topic_id,
                messages,
                config,
                now,
            )
            self._record_daily_digest_change(
                conn,
                ctx,
                messages,
                "canonical_event",
                now,
            )
            if relation in {"update", "contradiction"} and linked_topic_id:
                self._bump_new_topic_event_count(conn, topic_id, now)
                rel_name = "updates" if relation == "update" else "contradicts"
                self._insert_relation(conn, topic_id, linked_topic_id, rel_name, reason, now)
            elif relation in {"update", "contradiction"}:
                self._update_topic(conn, topic_id, candidate, ctx, relation, now)
                rel_name = "updates" if relation == "update" else "contradicts"
                self._insert_relation(conn, topic_id, topic_id, rel_name, reason, now)
            elif relation == "new":
                self._bump_new_topic_event_count(conn, topic_id, now)
                self._link_related(conn, topic_id, relation_decision, now)
            elif relation == "duplicate":
                self._insert_relation(conn, topic_id, topic_id, "duplicate_of", reason, now)

            try:
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            topic_row = conn.execute(
                "SELECT * FROM topics WHERE topic_id = ?",
                (topic_id,),
            ).fetchone()
            topic = self._topic_dict(topic_row) if topic_row else None
            topic_markdown_written = False
            try:
                self._write_topic_markdown(conn, topic_id)
                topic_markdown_written = True
            except OSError as exc:
                projection_warnings.append({
                    "surface": "topic_markdown",
                    "error_type": type(exc).__name__,
                    "errno": exc.errno,
                })

            if topic_markdown_written:
                try:
                    self.write_date_indexes()
                except OSError as exc:
                    projection_warnings.append({
                        "surface": "date_indexes",
                        "error_type": type(exc).__name__,
                        "errno": exc.errno,
                    })

            return {
                "relation": relation,
                "topic_id": topic_id,
                "event_id": event_id,
                "obsidian_path": topic.get("obsidian_path", "") if topic else "",
                "knowledge_path": (
                    self.full_obsidian_path(topic.get("obsidian_path", ""))
                    if topic and topic_markdown_written
                    else ""
                ),
                "projection_warnings": projection_warnings,
                "reused": False,
            }
        finally:
            conn.close()

    def _source_batch_result(self, conn, source_batch_id):
        row = conn.execute(
            """
            SELECT e.event_id, e.topic_id, e.relation,
                   e.window_start, e.window_end, e.created_at,
                   t.obsidian_path
            FROM events AS e
            JOIN topics AS t ON t.topic_id = e.topic_id
            WHERE e.source_batch_id = ?
            LIMIT 1
            """,
            (str(source_batch_id),),
        ).fetchone()
        if row is None:
            return None
        obsidian_path = str(row["obsidian_path"] or "")
        return {
            "relation": str(row["relation"] or "duplicate"),
            "topic_id": int(row["topic_id"]),
            "event_id": int(row["event_id"]),
            "obsidian_path": obsidian_path,
            "knowledge_path": self.full_obsidian_path(obsidian_path),
            "projection_warnings": [],
            "reused": True,
            "window_start": str(row["window_start"] or ""),
            "window_end": str(row["window_end"] or ""),
            "event_created_at": float(row["created_at"] or 0),
        }

    def recover_source_batch(self, source_batch_id):
        """Return and repair an already committed batch without model replay."""
        batch_id = str(source_batch_id or "").strip()[:96]
        if not batch_id:
            return None
        conn = self.connect()
        try:
            existing = self._source_batch_result(conn, batch_id)
            if existing is None:
                return None
            return self._repair_reused_source_batch_projection(conn, existing)
        finally:
            conn.close()

    def _repair_reused_source_batch_projection(self, conn, result):
        repaired = dict(result)
        knowledge_path = str(repaired.get("knowledge_path") or "")
        projection_warnings = []
        topic_ready = bool(knowledge_path and os.path.isfile(knowledge_path))
        if not topic_ready:
            try:
                self._write_topic_markdown(conn, int(repaired["topic_id"]))
            except OSError as exc:
                projection_warnings.append({
                    "surface": "topic_markdown",
                    "error_type": type(exc).__name__,
                    "errno": exc.errno,
                })
                repaired["knowledge_path"] = ""
            else:
                topic_ready = True

        if topic_ready:
            try:
                self.write_date_indexes()
            except OSError as exc:
                projection_warnings.append({
                    "surface": "date_indexes",
                    "error_type": type(exc).__name__,
                    "errno": exc.errno,
                })

        repaired["projection_warnings"] = projection_warnings
        return repaired

    @staticmethod
    def _record_daily_digest_change(conn, ctx, messages, change_kind, now):
        """Journal a Digest invalidation in the canonical-event transaction."""
        fallback_timestamp = float(now)
        for message in reversed(messages or []):
            try:
                candidate = float(message.get("timestamp") or 0)
            except (AttributeError, TypeError, ValueError):
                continue
            if candidate > 0:
                fallback_timestamp = candidate
                break
        conn.execute(
            """
            INSERT INTO daily_digest_changes(
                window_start, window_end, fallback_timestamp,
                change_kind, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(ctx.get("window_start") or ""),
                str(ctx.get("window_end") or ""),
                fallback_timestamp,
                str(change_kind or "knowledge"),
                now,
            ),
        )

    def pending_daily_digest_changes(self, limit=5000):
        """Peek one durable invalidation prefix without acknowledging it."""
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT change_id, window_start, window_end,
                       fallback_timestamp, change_kind, created_at
                FROM daily_digest_changes
                ORDER BY change_id
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return {"cutoff_change_id": 0, "changes": []}
        return {
            "cutoff_change_id": int(rows[-1]["change_id"]),
            "changes": [dict(row) for row in rows],
        }

    def ack_daily_digest_changes(self, cutoff_change_id):
        """Acknowledge only the exact invalidation prefix already refreshed."""
        if self.read_only:
            raise RuntimeError("knowledge store is read-only")
        cutoff = max(0, int(cutoff_change_id or 0))
        if cutoff == 0:
            return 0
        conn = self.connect()
        try:
            cursor = conn.execute(
                "DELETE FROM daily_digest_changes WHERE change_id <= ?",
                (cutoff,),
            )
            conn.commit()
            return int(cursor.rowcount or 0)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _register_attachment_mentions(conn, event_id, topic_id, messages, config, now):
        for resource in attachment_resources(messages, config):
            conn.execute(
                """
                INSERT OR IGNORE INTO attachment_mentions (
                    event_id, topic_id, source_message_id, resource_index, kind,
                    original_name, declared_size, declared_hash, attach_id, extension,
                    source_month, source_time, source_timestamp, source_sender,
                    status, resolution_method, object_sha256, last_error_code,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'pending', '', '', '', ?, ?)
                """,
                (
                    event_id,
                    topic_id,
                    resource["source_message_id"],
                    resource["resource_index"],
                    resource["kind"],
                    resource["original_name"],
                    resource["declared_size"],
                    resource["declared_hash"],
                    resource["attach_id"],
                    resource["extension"],
                    resource["source_month"],
                    resource["source_time"],
                    resource["source_timestamp"],
                    resource["source_sender"],
                    now,
                    now,
                ),
            )

    def get_topic(self, topic_id):
        conn = self.connect()
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT * FROM topics WHERE topic_id = ?", (topic_id,)).fetchone()
            return self._topic_dict(row) if row else None
        finally:
            conn.close()

    def topic_id_for_message_hash(self, message_hash, *, source_chat_username="", source_chat=""):
        """Return the same-chat topic that recorded an exact message hash."""
        message_hash = str(message_hash or "").strip()
        username = str(source_chat_username or "").strip()
        display_name = str(source_chat or "").strip()
        if not message_hash or not (username or display_name):
            return None
        conn = self.connect()
        if conn is None:
            return None
        try:
            if username:
                row = conn.execute(
                    """
                    SELECT topic_id
                    FROM events
                    WHERE message_hash = ?
                      AND (
                          source_chat_username = ?
                          OR (COALESCE(source_chat_username, '') = '' AND source_chat = ?)
                      )
                    ORDER BY event_id
                    LIMIT 1
                    """,
                    (message_hash, username, display_name),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT topic_id
                    FROM events
                    WHERE message_hash = ?
                      AND COALESCE(source_chat_username, '') = ''
                      AND source_chat = ?
                    ORDER BY event_id
                    LIMIT 1
                    """,
                    (message_hash, display_name),
                ).fetchone()
            return int(row["topic_id"]) if row else None
        except sqlite3.Error:
            if self.read_only:
                return None
            raise
        finally:
            conn.close()

    def full_obsidian_path(self, obsidian_path):
        return os.path.join(self.obsidian_root, obsidian_path) if obsidian_path else ""

    @staticmethod
    def _prepare_candidate_for_context(candidate, ctx, config):
        candidate = dict(candidate or {})
        candidate["semantic_tags"] = _normalize_list(candidate.get("semantic_tags"), limit=12)

        if _is_history_summary_candidate(candidate):
            candidate["category"] = normalize_category(candidate.get("category"))
            candidate["taxonomy_profile"] = ""
            candidate["taxonomy_version"] = 0
            return candidate

        resolution = resolve_taxonomy_profile(
            config,
            set(TAXONOMY_PROFILES),
            source_chat_username=ctx.get("source_chat_username", ""),
            source_chat=ctx.get("source_chat", ""),
            vault_chat_name=ctx.get("vault_chat_name", ""),
        )
        profile = resolution.profile
        if profile:
            profile_data = TAXONOMY_PROFILES[profile]
            candidate["category"] = normalize_taxonomy_category(
                candidate.get("raw_category") or candidate.get("category"),
                profile,
            )
            candidate["taxonomy_profile"] = profile
            candidate["taxonomy_version"] = int(profile_data["version"])
        else:
            candidate["category"] = normalize_category(candidate.get("category"))
            candidate["taxonomy_profile"] = ""
            candidate["taxonomy_version"] = 0
        return candidate

    def _create_topic(self, conn, candidate, ctx, now):
        now_text = self._now_text(now)
        category = candidate.get("category") or normalize_category(candidate.get("category"))
        status = normalize_status(candidate.get("status_hint") or "tracking")
        title = candidate.get("title") or "关注内容"
        cursor = conn.execute(
            """
            INSERT INTO topics (
                topic_key, title, category, status, summary, entities_json,
                semantic_tags_json, key_facts_json, links_json, files_json, source_chat,
                source_chat_username, vault_chat_name, taxonomy_profile, taxonomy_version, first_seen, last_seen,
                obsidian_path, event_count, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, ?, ?)
            """,
            (
                candidate.get("topic_key", ""),
                title,
                category,
                status,
                candidate.get("summary", ""),
                _json_dumps(candidate.get("entities")),
                _json_dumps(candidate.get("semantic_tags")),
                _json_dumps(candidate.get("key_facts")),
                _json_dumps(candidate.get("links")),
                _json_dumps(_merge_file_refs(candidate.get("files"), ctx.get("files"), limit=30)),
                ctx["source_chat"],
                ctx["source_chat_username"],
                ctx["vault_chat_name"],
                candidate.get("taxonomy_profile", ""),
                int(candidate.get("taxonomy_version") or 0),
                ctx["window_start"] or now_text,
                ctx["window_end"] or now_text,
                now,
                now,
            ),
        )
        topic_id = int(cursor.lastrowid)
        first_seen = ctx["window_start"] or now_text
        obsidian_path = self._unique_obsidian_path(
            conn,
            topic_id,
            category,
            title,
            source_chat=ctx["source_chat"],
            vault_chat_name=ctx["vault_chat_name"],
            first_seen=first_seen,
        )
        conn.execute("UPDATE topics SET obsidian_path = ? WHERE topic_id = ?", (obsidian_path, topic_id))
        self._upsert_fts(conn, topic_id)
        return topic_id

    def _update_topic(self, conn, topic_id, candidate, ctx, relation, now):
        row = conn.execute("SELECT * FROM topics WHERE topic_id = ?", (topic_id,)).fetchone()
        if row is None:
            return
        entities = merge_lists(_json_loads(row["entities_json"]), candidate.get("entities"))
        semantic_tags = merge_lists(
            _json_loads(_row_get(row, "semantic_tags_json", "[]")),
            candidate.get("semantic_tags"),
            limit=24,
        )
        key_facts = merge_lists(_json_loads(row["key_facts_json"]), candidate.get("key_facts"), limit=40)
        links = merge_lists(_json_loads(row["links_json"]), candidate.get("links"), limit=30)
        files = _merge_file_refs(_json_loads(row["files_json"]), ctx.get("files") or candidate.get("files"), limit=30)
        status = "disputed" if relation == "contradiction" else normalize_status(candidate.get("status_hint") or row["status"])
        conn.execute(
            """
            UPDATE topics
            SET summary = ?, status = ?, entities_json = ?, semantic_tags_json = ?, key_facts_json = ?,
                links_json = ?, files_json = ?, last_seen = ?, event_count = event_count + 1,
                updated_at = ?
            WHERE topic_id = ?
            """,
            (
                candidate.get("summary") or row["summary"],
                status,
                _json_dumps(entities),
                _json_dumps(semantic_tags),
                _json_dumps(key_facts),
                _json_dumps(links),
                _json_dumps(files),
                ctx["window_end"] or self._now_text(now),
                now,
                topic_id,
            ),
        )
        self._upsert_fts(conn, topic_id)

    def _bump_new_topic_event_count(self, conn, topic_id, now):
        conn.execute(
            "UPDATE topics SET event_count = event_count + 1, updated_at = ? WHERE topic_id = ?",
            (now, topic_id),
        )

    def _insert_event(
        self,
        conn,
        topic_id,
        candidate,
        ctx,
        relation,
        now,
        *,
        source_batch_id="",
    ):
        cursor = conn.execute(
            """
            INSERT INTO events (
                topic_id, relation, title, summary, category, semantic_tags_json, event_type,
                status_hint, source_chat, source_chat_username, vault_chat_name, taxonomy_profile, taxonomy_version,
                window_start, window_end, senders_json, links_json, files_json,
                message_hash, source_batch_id, messages_excerpt, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                topic_id,
                relation,
                candidate.get("title", ""),
                candidate.get("summary", ""),
                candidate.get("category") or normalize_category(candidate.get("category")),
                _json_dumps(candidate.get("semantic_tags")),
                candidate.get("event_type", ""),
                candidate.get("status_hint", ""),
                ctx["source_chat"],
                ctx["source_chat_username"],
                ctx["vault_chat_name"],
                candidate.get("taxonomy_profile", ""),
                int(candidate.get("taxonomy_version") or 0),
                ctx["window_start"],
                ctx["window_end"],
                _json_dumps(ctx["senders"]),
                _json_dumps(candidate.get("links")),
                _json_dumps(_merge_file_refs(candidate.get("files"), ctx.get("files"), limit=30)),
                ctx["message_hash"],
                str(source_batch_id or ""),
                ctx["messages_excerpt"],
                now,
            ),
        )
        return int(cursor.lastrowid)

    def _insert_relation(self, conn, source_topic_id, target_topic_id, relation, reason, now):
        conn.execute(
            """
            INSERT OR IGNORE INTO relations (
                source_topic_id, target_topic_id, relation, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (source_topic_id, target_topic_id, relation, reason, now),
        )

    def _link_related(self, conn, topic_id, relation_decision, now):
        """Link a freshly created topic to semantically nearby existing topics.

        These cross-topic `related` edges are what populate the "相关主题"
        section and let Obsidian's graph view connect notes; without them the
        relations table only ever held self-loops.
        """
        related_ids = relation_decision.get("related_topic_ids") or []
        seen = set()
        for rid in related_ids:
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                continue
            if rid == topic_id or rid in seen:
                continue
            seen.add(rid)
            if conn.execute("SELECT 1 FROM topics WHERE topic_id = ?", (rid,)).fetchone() is None:
                continue
            self._insert_relation(conn, topic_id, rid, "related", "语义相邻主题", now)

    def _upsert_fts(self, conn, topic_id):
        row = conn.execute("SELECT * FROM topics WHERE topic_id = ?", (topic_id,)).fetchone()
        if row is None:
            return
        conn.execute("DELETE FROM topic_fts WHERE topic_id = ?", (topic_id,))
        conn.execute(
            """
            INSERT INTO topic_fts(topic_id, title, category, summary, entities, key_facts, links)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                topic_id,
                row["title"],
                row["category"],
                row["summary"],
                " ".join(
                    _json_loads(row["entities_json"])
                    + _json_loads(_row_get(row, "semantic_tags_json", "[]"))
                ),
                " ".join(_json_loads(row["key_facts_json"])),
                " ".join(
                    _json_loads(row["links_json"])
                    + [f["name"] for f in _normalize_file_refs(_json_loads(row["files_json"]))]
                ),
            ),
        )

    def _write_topic_markdown(self, conn, topic_id):
        topic_row = conn.execute("SELECT * FROM topics WHERE topic_id = ?", (topic_id,)).fetchone()
        if topic_row is None:
            return
        text = self.render_topic_markdown(conn, topic_id)
        topic = self._topic_dict(topic_row)
        ensure_obsidian_vault(self.obsidian_root, obsidian_subdir=self.obsidian_subdir)
        path = self.full_obsidian_path(topic["obsidian_path"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._atomic_write_text(path, text)

    def render_topic_markdown(self, conn, topic_id):
        topic_cursor = conn.execute("SELECT * FROM topics WHERE topic_id = ?", (topic_id,))
        topic_row = topic_cursor.fetchone()
        if topic_row is None:
            raise ValueError("topic_not_found")
        if not isinstance(topic_row, sqlite3.Row):
            topic_row = dict(zip((column[0] for column in topic_cursor.description), topic_row))
        events = conn.execute(
            "SELECT * FROM events WHERE topic_id = ? ORDER BY created_at, event_id",
            (topic_id,),
        )
        event_columns = [column[0] for column in events.description]
        event_rows = events.fetchall()
        if event_rows and not isinstance(event_rows[0], sqlite3.Row):
            event_rows = [dict(zip(event_columns, row)) for row in event_rows]
        relations = conn.execute(
            """
            SELECT r.relation, r.reason, r.target_topic_id, t.title, t.obsidian_path
            FROM relations r
            JOIN topics t ON t.topic_id = r.target_topic_id
            WHERE r.source_topic_id = ?
            ORDER BY r.created_at, r.relation, r.target_topic_id, r.relation_id
            """,
            (topic_id,),
        )
        relation_columns = [column[0] for column in relations.description]
        relation_rows = relations.fetchall()
        if relation_rows and not isinstance(relation_rows[0], sqlite3.Row):
            relation_rows = [dict(zip(relation_columns, row)) for row in relation_rows]
        attachment_rows = conn.execute(
            """
            SELECT m.kind, m.original_name, m.source_month, m.source_time,
                   m.source_sender, m.status, m.resolution_method,
                   m.object_sha256, o.object_relpath
            FROM attachment_mentions m
            LEFT JOIN attachment_objects o ON o.sha256 = m.object_sha256
            WHERE m.topic_id = ?
            ORDER BY m.mention_id
            """,
            (topic_id,),
        ).fetchall()
        return self._render_markdown(
            self._topic_dict(topic_row),
            event_rows,
            relation_rows,
            attachment_mentions=attachment_rows,
        )

    def _render_markdown(
        self, topic, events, relations, *, include_source_contract=True,
        attachment_mentions=(),
    ):
        title = redact_urls_in_text(topic["title"])
        entities = [redact_urls_in_text(value) for value in topic["entities"]]
        links = topic["links"]
        display_links = [redact_url_for_display(link) for link in links]
        files = topic.get("files") or []
        key_facts = [
            redact_urls_in_text(value) for value in topic["key_facts"]
        ]
        semantic_tags = [
            redact_urls_in_text(value)
            for value in (topic.get("semantic_tags") or [])
        ]
        tags = ["wechat-monitor", safe_path_part(topic["category"], "uncategorized").replace(" ", "-")]
        display_title = _display_title(title, links, files)
        resource_types = _resource_types(links, files)
        for mention in attachment_mentions or ():
            kind = str(_row_get(mention, "kind", "") or "").strip()
            if kind and kind not in resource_types:
                resource_types.append(kind)
        archive_by_name = {}
        for mention in attachment_mentions or ():
            name = str(mention["original_name"] or "").strip().lower()
            if not name:
                continue
            current = archive_by_name.get(name)
            if current is None or str(mention["status"]) in {"archived", "original_archived"}:
                archive_by_name[name] = mention
        source_lines = []
        if include_source_contract:
            if is_history_summary(topic, events):
                source_lines = projection_source_lines("history_summary")
            else:
                source_lines = atomic_source_lines(
                    topic["topic_id"],
                    aware_iso_from_timestamp(topic["updated_at"]),
                )

        lines = [
            "---",
            *source_lines,
            f"title: {_frontmatter_scalar(title)}",
            f"created: {_frontmatter_scalar(topic['first_seen'])}",
            f"updated: {_frontmatter_scalar(topic['last_seen'])}",
            f"category: {_frontmatter_scalar(topic['category'])}",
            f"status: {_frontmatter_scalar(topic['status'])}",
            f"first_seen: {_frontmatter_scalar(topic['first_seen'])}",
            f"last_seen: {_frontmatter_scalar(topic['last_seen'])}",
            f"event_count: {int(topic['event_count'])}",
            f"has_links: {_frontmatter_bool(bool(links))}",
            f"has_files: {_frontmatter_bool(bool(files))}",
            f"has_attachments: {_frontmatter_bool(bool(attachment_mentions))}",
            f"source_chat: {_frontmatter_scalar(topic['source_chat'])}",
            f"source_chat_username: {_frontmatter_scalar(topic.get('source_chat_username', ''))}",
            f"vault_chat_name: {_frontmatter_scalar(topic.get('vault_chat_name', ''))}",
            f"taxonomy_profile: {_frontmatter_scalar(topic.get('taxonomy_profile', ''))}",
            f"taxonomy_version: {int(topic.get('taxonomy_version') or 0)}",
            _frontmatter_list("entities", entities),
            _frontmatter_list("semantic_tags", semantic_tags),
            _frontmatter_string_list("resource_types", resource_types),
            _frontmatter_list("tags", tags),
            "---",
            "",
            f"# {display_title}",
            "",
            "## 摘要",
            redact_urls_in_text(topic["summary"]) or "（暂无摘要）",
            "",
        ]

        lines.extend(["## 关键事实"])
        if key_facts:
            lines.extend(f"- {fact}" for fact in key_facts)
        else:
            lines.append("- （暂无关键事实）")

        if links or files or attachment_mentions:
            lines.extend(["", "## 资源"])
            if links:
                lines.extend(["", "### 链接"])
                lines.extend(f"- {link}" for link in display_links)
            if files:
                lines.extend(["", "### 文件"])
                for item in files:
                    details = " · ".join(
                        redact_urls_in_text(value)
                        for value in (item.get("time"), item.get("sender"))
                        if value
                    )
                    line = f"- {redact_urls_in_text(item['name'])}"
                    if details:
                        line += f"（{details}）"
                    lines.append(line)
                    archived = archive_by_name.get(str(item["name"]).strip().lower())
                    if archived is not None:
                        lines.append(f"  - 归档状态：{archived['status']}")
                        if archived["object_relpath"]:
                            object_path = os.path.join(
                                self.attachment_archive_root,
                                str(archived["object_relpath"]),
                            )
                            lines.append(f"  - 本地归档：{_file_url(object_path)}")
                    if item.get("month_dir"):
                        lines.append(f"  - 月份目录：{_file_url(item['month_dir'])}")
            listed_names = {
                str(item.get("name") or "").strip().lower()
                for item in files
                if str(item.get("name") or "").strip()
            }
            remaining_mentions = [
                mention
                for mention in attachment_mentions or ()
                if not str(_row_get(mention, "original_name", "") or "").strip().lower()
                or str(_row_get(mention, "original_name", "") or "").strip().lower() not in listed_names
            ]
            if remaining_mentions:
                lines.extend(["", "### 附件归档"])
                for mention in remaining_mentions:
                    kind = str(_row_get(mention, "kind", "attachment") or "attachment")
                    name = str(_row_get(mention, "original_name", "") or "").strip()
                    label = redact_urls_in_text(
                        name or ("图片附件" if kind == "image" else "附件")
                    )
                    details = " · ".join(
                        value
                        for value in (
                            str(_row_get(mention, "source_time", "") or "").strip(),
                            str(_row_get(mention, "source_sender", "") or "").strip(),
                        )
                        if value
                    )
                    lines.append(f"- {label}" + (f"（{details}）" if details else ""))
                    lines.append(f"  - 归档状态：{_row_get(mention, 'status', 'unknown')}")
                    object_relpath = str(_row_get(mention, "object_relpath", "") or "")
                    if object_relpath:
                        object_path = os.path.join(self.attachment_archive_root, object_relpath)
                        lines.append(f"  - 本地归档：{_file_url(object_path)}")
                    source_month = str(_row_get(mention, "source_month", "") or "")
                    if source_month:
                        lines.append(f"  - 来源月份：{source_month}")

        relation_lines = []
        for rel in relations:
            if (
                int(rel["target_topic_id"]) == int(topic["topic_id"])
                and rel["relation"] in {"updates", "duplicate_of", "contradicts"}
            ):
                continue
            relation_lines.append(
                _render_relation_markdown_line(
                    rel["relation"],
                    rel["obsidian_path"],
                    rel["title"],
                )
            )
        if relation_lines:
            lines.extend(["", "## 相关主题"])
            lines.extend(relation_lines)

        lines.extend(["", "## 来源"])
        for event in events:
            senders = ", ".join(_json_loads(event["senders_json"]))
            when = event["window_end"] or datetime.fromtimestamp(event["created_at"]).strftime("%Y-%m-%d %H:%M")
            label = RELATION_LABELS.get(event["relation"], event["relation"])
            window = f"{event['window_start']} ~ {event['window_end']}".strip(" ~")
            lines.append(f"- {when} · {label} · {senders or '未知'} · {window}")

        return "\n".join(lines).rstrip() + "\n"

    def rewrite_topic_markdown(self, topic_id):
        if self.read_only:
            raise RuntimeError("knowledge store is read-only")
        conn = self.connect()
        try:
            self._write_topic_markdown(conn, int(topic_id))
        finally:
            conn.close()

    def _unique_obsidian_path(
        self,
        conn,
        topic_id,
        category,
        title,
        source_chat="",
        vault_chat_name="",
        first_seen="",
        current_path="",
        reserved_paths=None,
    ):
        reserved_paths = set(reserved_paths or [])
        chat_part = safe_path_part(vault_chat_name or source_chat, "未命名群聊", max_len=80)
        category_part = safe_path_part(category)
        links = []
        files = []
        row = conn.execute(
            "SELECT links_json, files_json FROM topics WHERE topic_id = ?",
            (topic_id,),
        ).fetchone()
        if row is not None:
            links = _json_loads(row["links_json"])
            files = _normalize_file_refs(_json_loads(row["files_json"]))
        title_part = safe_path_part(_display_title(title, links, files), "关注内容", max_len=90)
        filename = title_part
        rel_path = os.path.join(self.obsidian_subdir, chat_part, category_part, f"{filename}.md")
        existing = conn.execute(
            "SELECT topic_id FROM topics WHERE obsidian_path = ? AND topic_id != ?",
            (rel_path, topic_id),
        ).fetchone()
        full_path = self.full_obsidian_path(rel_path)
        if (
            existing is None
            and rel_path not in reserved_paths
            and (not os.path.exists(full_path) or rel_path == current_path)
        ):
            return rel_path

        date_part = safe_path_part(_compact_path_date(first_seen), "", max_len=5)
        if date_part:
            dated_filename = f"{date_part} {title_part}".strip()
            dated_rel_path = os.path.join(self.obsidian_subdir, chat_part, category_part, f"{dated_filename}.md")
            dated_existing = conn.execute(
                "SELECT topic_id FROM topics WHERE obsidian_path = ? AND topic_id != ?",
                (dated_rel_path, topic_id),
            ).fetchone()
            dated_full_path = self.full_obsidian_path(dated_rel_path)
            if (
                dated_existing is None
                and dated_rel_path not in reserved_paths
                and (not os.path.exists(dated_full_path) or dated_rel_path == current_path)
            ):
                return dated_rel_path
        return os.path.join(self.obsidian_subdir, chat_part, category_part, f"{filename}-{topic_id}.md")

    def _chat_folder_from_path(self, obsidian_path):
        parts = [p for p in re.split(r"[/\\]+", str(obsidian_path or "")) if p]
        subdir_parts = [p for p in re.split(r"[/\\]+", self.obsidian_subdir) if p]
        if subdir_parts and parts[:len(subdir_parts)] == subdir_parts:
            remainder = parts[len(subdir_parts):]
            if remainder:
                return remainder[0]
        return ""

    def _topic_dict(self, row, score=None):
        data = {
            "topic_id": int(row["topic_id"]),
            "topic_key": row["topic_key"],
            "title": row["title"],
            "category": row["category"],
            "status": row["status"],
            "summary": row["summary"],
            "entities": _json_loads(row["entities_json"]),
            "semantic_tags": _json_loads(_row_get(row, "semantic_tags_json", "[]")),
            "key_facts": _json_loads(row["key_facts_json"]),
            "links": _json_loads(row["links_json"]),
            "files": _normalize_file_refs(_json_loads(row["files_json"])),
            "source_chat": row["source_chat"],
            "source_chat_username": _row_get(row, "source_chat_username", ""),
            "vault_chat_name": _row_get(row, "vault_chat_name", "") or self._chat_folder_from_path(row["obsidian_path"]) or row["source_chat"],
            "taxonomy_profile": _row_get(row, "taxonomy_profile", ""),
            "taxonomy_version": int(_row_get(row, "taxonomy_version", 0) or 0),
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "obsidian_path": row["obsidian_path"],
            "event_count": int(row["event_count"]),
            "updated_at": float(row["updated_at"]),
        }
        if score is not None:
            data["score"] = score
        return data

    @staticmethod
    def _date_label(value):
        text = str(value or "")
        match = re.search(r"\d{4}-\d{2}-\d{2}", text)
        return match.group(0) if match else "未标日期"

    @staticmethod
    def _time_label(value):
        text = str(value or "")
        matches = re.findall(r"\d{2}[:\-]\d{2}", text)
        return matches[-1].replace("-", ":") if matches else "--:--"

    def _topic_date_label(self, topic):
        return self._date_label(topic.get("first_seen") or topic.get("last_seen"))

    def _ordered_date_index_topics(self, topics):
        return sorted(
            topics,
            key=lambda topic: (
                self._parse_note_ts(topic.get("first_seen") or topic.get("last_seen")),
                topic["topic_id"],
            ),
            reverse=True,
        )

    def _date_index_scope_specs(self, scope, chat, topics, include_chat):
        return [{
            "kind": "overview",
            "scope": scope,
            "chat": chat,
            "month": "",
            "rel_path": os.path.join(self.obsidian_subdir, chat, OBSIDIAN_DATE_INDEX_FILENAME)
            if chat else os.path.join(self.obsidian_subdir, OBSIDIAN_DATE_INDEX_FILENAME),
            "topics": topics,
            "include_chat": include_chat,
            "archives": [],
        }]

    def _date_index_archive_dir_rel_path(self, chat):
        if chat:
            return os.path.join(self.obsidian_subdir, chat, "按日期")
        return os.path.join(self.obsidian_subdir, "按日期")

    def _date_index_specs(self, topics=None):
        if topics is None:
            topics = self.list_topics()
        if not topics:
            return []

        specs = self._date_index_scope_specs("global", "", topics, True)

        by_chat = {}
        for topic in topics:
            chat = self._chat_folder_from_path(topic["obsidian_path"]) or topic.get("vault_chat_name") or topic["source_chat"]
            by_chat.setdefault(chat, []).append(topic)
        for chat in sorted(by_chat):
            specs.extend(self._date_index_scope_specs("chat", chat, by_chat[chat], False))
        return specs

    def render_managed_date_indexes(self, conn, paths=None):
        cursor = conn.execute("SELECT * FROM topics ORDER BY topic_id")
        columns = [column[0] for column in cursor.description]
        rows = cursor.fetchall()
        topics = [
            self._topic_dict(row if isinstance(row, sqlite3.Row) else dict(zip(columns, row)))
            for row in rows
        ]
        selected_paths = None
        if paths is not None:
            selected_paths = {str(path).replace("\\", "/") for path in paths}

        rendered = {}
        for spec in self._date_index_specs(topics):
            target = self._date_index_target(spec["rel_path"])
            if target["status"] not in {"update", "fallback_update"}:
                continue
            rel_path = target["rel_path"].replace("\\", "/")
            if selected_paths is not None and rel_path not in selected_paths:
                continue
            rendered[rel_path] = self._render_date_index(spec)
        return rendered

    @staticmethod
    def _is_managed_date_index(path):
        with open(path, encoding="utf-8") as f:
            first_line = f.readline().strip()
            if first_line in OBSIDIAN_DATE_INDEX_MARKERS:
                return True
            if first_line != "---":
                return False
            metadata_lines = [f.readline().strip() for _ in range(4)]
            closing_line = f.readline().strip()
            marker_line = f.readline().strip()
            return (
                metadata_lines == projection_source_lines("date_index")
                and closing_line == "---"
                and marker_line in OBSIDIAN_DATE_INDEX_MARKERS
            )

    @staticmethod
    def _date_index_fallback_rel_path(rel_path):
        root, ext = os.path.splitext(rel_path)
        return f"{root}.generated{ext or '.md'}"

    def _remove_managed_date_index_fallback(self, rel_path):
        fallback_rel = self._date_index_fallback_rel_path(rel_path)
        if fallback_rel == rel_path:
            return False
        fallback_path = self.full_obsidian_path(fallback_rel)
        if not os.path.exists(fallback_path):
            return False
        if not self._is_managed_date_index(fallback_path):
            return False
        os.remove(fallback_path)
        return True

    def _remove_managed_legacy_date_archive_dir(self, chat):
        archive_dir = self.full_obsidian_path(self._date_index_archive_dir_rel_path(chat))
        if not os.path.isdir(archive_dir):
            return 0

        removed = 0
        for name in os.listdir(archive_dir):
            path = os.path.join(archive_dir, name)
            if not os.path.isfile(path):
                continue
            if not name.endswith(".md"):
                continue
            if not self._is_managed_date_index(path):
                continue
            os.remove(path)
            removed += 1

        try:
            os.rmdir(archive_dir)
        except OSError:
            pass
        return removed

    def _date_index_target(self, rel_path):
        path = self.full_obsidian_path(rel_path)
        if not os.path.exists(path):
            return {
                "rel_path": rel_path,
                "path": path,
                "status": "create",
                "conflict_path": "",
            }
        if self._is_managed_date_index(path):
            return {
                "rel_path": rel_path,
                "path": path,
                "status": "update",
                "conflict_path": "",
            }

        fallback_rel = self._date_index_fallback_rel_path(rel_path)
        fallback_path = self.full_obsidian_path(fallback_rel)
        if not os.path.exists(fallback_path):
            return {
                "rel_path": fallback_rel,
                "path": fallback_path,
                "status": "fallback",
                "conflict_path": path,
            }
        if self._is_managed_date_index(fallback_path):
            return {
                "rel_path": fallback_rel,
                "path": fallback_path,
                "status": "fallback_update",
                "conflict_path": path,
            }
        return {
            "rel_path": fallback_rel,
            "path": fallback_path,
            "status": "conflict",
            "conflict_path": path,
        }

    def plan_date_indexes(self):
        targets = []
        for spec in self._date_index_specs():
            target = self._date_index_target(spec["rel_path"])
            targets.append({
                "scope": spec["scope"],
                "chat": spec["chat"],
                "kind": spec.get("kind", ""),
                "month": spec.get("month", ""),
                "rel_path": target["rel_path"],
                "path": target["path"],
                "status": target["status"],
                "conflict_path": target["conflict_path"],
            })
        conflict_count = sum(1 for target in targets if target["status"] in {"fallback", "fallback_update", "conflict"})
        return {
            "targets": targets,
            "target_count": len(targets),
            "conflict_count": conflict_count,
        }

    def _date_index_topic_line(self, topic, include_chat):
        display_title = _display_title(topic["title"], topic.get("links"), topic.get("files"))
        link = _obsidian_link(topic["obsidian_path"], display_title)
        when = self._time_label(topic.get("first_seen") or topic.get("last_seen"))
        if include_chat:
            chat = self._chat_folder_from_path(topic["obsidian_path"]) or topic.get("vault_chat_name") or topic["source_chat"]
            meta = f"{chat} / {topic['category']}"
        else:
            meta = topic["category"]
        return f"- {when} · {meta} · {link}"

    def _append_date_index_topic_groups(self, lines, topics, include_chat, heading_level):
        current_date = None
        marker = "#" * heading_level
        for topic in self._ordered_date_index_topics(topics):
            date_label = self._topic_date_label(topic)
            if date_label != current_date:
                if current_date is not None:
                    lines.append("")
                lines.append(f"{marker} {date_label}")
                current_date = date_label
            lines.append(self._date_index_topic_line(topic, include_chat))

    def _render_date_overview_index(self, spec):
        lines = [
            "---",
            *projection_source_lines("date_index"),
            "---",
            OBSIDIAN_DATE_INDEX_MARKER,
            "# 按日期",
            "",
            "按时间查看关注推送笔记。这里仅保存链接，不复制笔记正文。",
            "",
            "## 全部",
            "",
        ]
        if spec["topics"]:
            self._append_date_index_topic_groups(lines, spec["topics"], spec["include_chat"], 3)
        else:
            lines.append("- 最近没有关注推送笔记。")
        return "\n".join(lines).rstrip() + "\n"

    def _render_date_index(self, spec):
        return self._render_date_overview_index(spec)

    @staticmethod
    def _atomic_write_text(path, text):
        target = os.path.abspath(path)
        directory = os.path.dirname(target)
        lock_index = hashlib.sha256(target.encode("utf-8")).digest()[0] % len(
            _PROJECTION_WRITE_LOCKS
        )
        with _PROJECTION_WRITE_LOCKS[lock_index]:
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(target)}.",
                suffix=".tmp",
                dir=directory,
            )
            try:
                try:
                    target_mode = stat.S_IMODE(os.stat(target, follow_symlinks=False).st_mode)
                except OSError:
                    target_mode = 0o644
                os.fchmod(fd, target_mode)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    fd = -1
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, target)
                tmp_path = ""
            finally:
                if fd >= 0:
                    os.close(fd)
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

    def write_date_indexes(self):
        if self.read_only:
            raise RuntimeError("knowledge store is read-only")

        ensure_obsidian_vault(self.obsidian_root, obsidian_subdir=self.obsidian_subdir)
        written = 0
        removed_generated = 0
        removed_archives = 0
        skipped = []
        for spec in self._date_index_specs():
            target = self._date_index_target(spec["rel_path"])
            if target["status"] == "conflict":
                skipped.append(target)
                continue
            os.makedirs(os.path.dirname(target["path"]), exist_ok=True)
            self._atomic_write_text(target["path"], self._render_date_index(spec))
            written += 1
            if target["rel_path"] == spec["rel_path"]:
                removed_generated += int(self._remove_managed_date_index_fallback(spec["rel_path"]))
                removed_archives += self._remove_managed_legacy_date_archive_dir(spec["chat"])
        return {
            "written_count": written,
            "removed_generated_count": removed_generated,
            "removed_archive_count": removed_archives,
            "skipped": skipped,
            "skipped_count": len(skipped),
        }

    def _build_fts_query(self, candidate):
        text = " ".join([
            candidate.get("title", ""),
            candidate.get("topic_key", ""),
            " ".join(candidate.get("entities") or []),
            " ".join(candidate.get("semantic_tags") or []),
            " ".join(candidate.get("links") or []),
            " ".join(candidate.get("key_facts") or []),
            " ".join(f["name"] for f in _normalize_file_refs(candidate.get("files"))),
        ])
        tokens = []
        for token in re.findall(r"[0-9A-Za-z_\-.]{2,}|[\u4e00-\u9fff]{2,}", text):
            token = token.strip(".-")
            if token and token.lower() not in {t.lower() for t in tokens}:
                tokens.append(token)
            if len(tokens) >= 8:
                break
        return " OR ".join(f'"{token}"' for token in tokens)

    def _score_candidate(self, candidate, row):
        score = 0
        if candidate.get("topic_key") and candidate.get("topic_key") == row["topic_key"]:
            score += 100

        candidate_links = {x.lower() for x in candidate.get("links") or []}
        row_links = {x.lower() for x in _json_loads(row["links_json"])}
        score += len(candidate_links & row_links) * 80

        candidate_entities = {x.lower() for x in candidate.get("entities") or []}
        row_entities = {x.lower() for x in _json_loads(row["entities_json"])}
        score += len(candidate_entities & row_entities) * 25

        candidate_tags = {x.lower() for x in candidate.get("semantic_tags") or []}
        row_tags = {x.lower() for x in _json_loads(_row_get(row, "semantic_tags_json", "[]"))}
        score += len(candidate_tags & row_tags) * 15

        haystack = " ".join([
            row["title"],
            row["summary"],
            " ".join(_json_loads(row["key_facts_json"])),
            " ".join(_json_loads(_row_get(row, "semantic_tags_json", "[]"))),
        ]).lower()
        for token in re.findall(r"[0-9A-Za-z_\-.]{3,}|[\u4e00-\u9fff]{2,}", candidate.get("title", "").lower()):
            if token in haystack:
                score += 8
        score += self._continuity_boost(candidate, row)
        return score

    def _continuity_boost(self, candidate, row):
        """Prefer nearby same-chat topics so interrupted discussions can stitch together."""
        source_chat = str(candidate.get("source_chat") or "").strip()
        source_username = str(candidate.get("source_chat_username") or "").strip()
        row_username = str(_row_get(row, "source_chat_username", "") or "").strip()
        if source_username and row_username:
            if source_username != row_username:
                return 0
        elif not source_chat or source_chat != row["source_chat"]:
            return 0

        if not self._has_continuity_signal(candidate, row):
            return 0

        candidate_ts = self._parse_note_ts(
            candidate.get("window_start")
            or candidate.get("first_seen")
            or candidate.get("window_end")
            or candidate.get("last_seen")
        )
        row_ts = self._parse_note_ts(row["last_seen"] or row["first_seen"])
        if not candidate_ts or not row_ts:
            return 0

        gap_minutes = abs(candidate_ts - row_ts) / 60
        if gap_minutes <= 15:
            return 65
        if gap_minutes <= 60:
            return 40
        if gap_minutes <= 120:
            return 20
        return 0

    def _has_continuity_signal(self, candidate, row):
        candidate_links = {x.lower() for x in candidate.get("links") or []}
        row_links = {x.lower() for x in _json_loads(row["links_json"])}
        if candidate_links & row_links:
            return True

        weak_entities = {
            "ai", "claude", "openai", "anthropic", "opus", "haiku",
            "gpt", "gemini", "deepseek", "46", "47", "48", "336",
        }
        candidate_entities = {self._entity_key(x) for x in candidate.get("entities") or []}
        row_entities = {self._entity_key(x) for x in _json_loads(row["entities_json"])}
        shared_entities = {x for x in candidate_entities & row_entities if x and x not in weak_entities}
        if shared_entities:
            return True

        candidate_text = " ".join([
            candidate.get("title", ""),
            candidate.get("summary", ""),
            " ".join(candidate.get("key_facts") or []),
        ])
        row_text = " ".join([
            row["title"],
            row["summary"],
            " ".join(_json_loads(row["key_facts_json"])),
        ])
        shared_count, overlap = self._title_overlap(candidate_text, row_text)
        return shared_count >= 2 or overlap >= 0.35

    @staticmethod
    def _entity_key(value):
        chars = []
        for ch in str(value or "").lower():
            if unicodedata.category(ch)[0] in {"L", "N"}:
                chars.append(ch)
        return "".join(chars)

    @staticmethod
    def _parse_note_ts(value):
        text = str(value or "").strip()
        match = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}[:\-]\d{2}", text)
        if not match:
            return 0
        date_part, time_part = match.group(0).split()
        stamp = f"{date_part} {time_part.replace('-', ':')}"
        try:
            return datetime.strptime(stamp, "%Y-%m-%d %H:%M").timestamp()
        except ValueError:
            return 0

    # ── 维护：去重合并 + 全量重导出 ──────────────────────────

    def list_topics(self):
        conn = self.connect()
        if conn is None:
            return []
        try:
            return [
                self._topic_dict(r)
                for r in conn.execute("SELECT * FROM topics ORDER BY first_seen, topic_id")
            ]
        finally:
            conn.close()

    @staticmethod
    def _title_tokens(text):
        """Extract enough signal for maintenance without merging broad AI topics."""
        text = (text or "").lower()
        tokens = set()
        for token in re.findall(r"[0-9a-z][0-9a-z_.-]{1,}", text):
            tokens.add(token.strip("._-"))
        for chunk in re.findall(r"[一-鿿]{2,}", text):
            tokens.add(chunk)
            for i in range(len(chunk) - 1):
                tokens.add(chunk[i:i + 2])

        weak = {
            "ai", "claude", "codex", "openai", "deepseek", "模型", "讨论",
            "分享", "技巧", "群友", "新功能", "新版本", "消息", "体验",
            "工具", "链接", "发布", "传闻", "实际", "热聊",
        }
        return {t for t in tokens if t and t not in weak}

    @classmethod
    def _title_overlap(cls, a, b):
        a_tokens = cls._title_tokens(a)
        b_tokens = cls._title_tokens(b)
        if not a_tokens or not b_tokens:
            return 0, 0
        shared = a_tokens & b_tokens
        return len(shared), len(shared) / min(len(a_tokens), len(b_tokens))

    @classmethod
    def _topic_similarity(cls, a, b):
        ak = (a.get("topic_key") or "").strip().lower()
        bk = (b.get("topic_key") or "").strip().lower()
        if ak and ak == bk:
            return 100

        at = (a.get("title") or "").strip().lower()
        bt = (b.get("title") or "").strip().lower()
        if at and at == bt:
            return 100

        shared_title_count, title_overlap = cls._title_overlap(at, bt)
        a_links = {x.lower() for x in a.get("links") or []}
        b_links = {x.lower() for x in b.get("links") or []}
        shared_links = a_links & b_links
        if shared_links and (title_overlap >= 0.35 or shared_title_count >= 2):
            return 95

        a_facts = " ".join(a.get("key_facts") or []).lower()
        b_facts = " ".join(b.get("key_facts") or []).lower()
        fact_overlap_count, fact_overlap = cls._title_overlap(a_facts, b_facts)
        if title_overlap >= 0.75 and shared_title_count >= 3:
            return 90
        if title_overlap >= 0.6 and shared_title_count >= 2 and fact_overlap >= 0.45:
            return 90
        if shared_links and fact_overlap_count >= 2:
            return 90

        return 0

    @staticmethod
    def _pick_primary(group):
        return sorted(
            group,
            key=lambda t: (-t["event_count"], t["first_seen"], t["topic_id"]),
        )[0]

    @staticmethod
    def _maintenance_chat_key(topic):
        username = str(topic.get("source_chat_username") or "").strip().casefold()
        if username:
            return f"username:{username}"
        name = str(topic.get("vault_chat_name") or topic.get("source_chat") or "").strip().casefold()
        return f"name:{name}" if name else ""

    @classmethod
    def _duplicate_block_keys(cls, topic):
        chat_key = cls._maintenance_chat_key(topic)
        if not chat_key:
            return set()
        keys = set()
        topic_key = str(topic.get("topic_key") or "").strip().casefold()
        if topic_key:
            keys.add((chat_key, "topic_key", topic_key))
        title = str(topic.get("title") or "").strip().casefold()
        if title:
            keys.add((chat_key, "title", title))
        for link in topic.get("links") or []:
            normalized = str(link).strip().casefold()
            if normalized:
                keys.add((chat_key, "link", normalized))
        for token in sorted(cls._title_tokens(title)):
            if len(token) >= 2:
                keys.add((chat_key, "title_token", token))
        return keys

    @classmethod
    def _duplicate_pair_eligible(cls, first, second):
        first_key = str(first.get("topic_key") or "").strip().casefold()
        second_key = str(second.get("topic_key") or "").strip().casefold()
        if first_key and first_key == second_key:
            return True
        first_title = str(first.get("title") or "").strip().casefold()
        second_title = str(second.get("title") or "").strip().casefold()
        if first_title and first_title == second_title:
            return True
        first_links = {str(link).strip().casefold() for link in first.get("links") or [] if str(link).strip()}
        second_links = {str(link).strip().casefold() for link in second.get("links") or [] if str(link).strip()}
        if first_links & second_links:
            return True
        return len(cls._title_tokens(first_title) & cls._title_tokens(second_title)) >= 2

    def find_duplicate_groups(self, threshold=85):
        """Group direct, same-chat duplicate evidence without transitive chaining."""
        topics = [
            topic
            for topic in self.list_topics()
            if not _is_history_summary_candidate(topic)
            and "历史总结" not in str(topic.get("title") or "")
        ]
        by_id = {topic["topic_id"]: topic for topic in topics}
        blocks = {}
        for topic in topics:
            for key in self._duplicate_block_keys(topic):
                blocks.setdefault(key, []).append(topic["topic_id"])

        candidate_pairs = set()
        for topic_ids in blocks.values():
            unique_ids = sorted(set(topic_ids))
            for index, first_id in enumerate(unique_ids):
                for second_id in unique_ids[index + 1:]:
                    candidate_pairs.add((first_id, second_id))

        direct_neighbors = {topic_id: set() for topic_id in by_id}
        for first_id, second_id in sorted(candidate_pairs):
            if not self._duplicate_pair_eligible(by_id[first_id], by_id[second_id]):
                continue
            if self._topic_similarity(by_id[first_id], by_id[second_id]) < threshold:
                continue
            direct_neighbors[first_id].add(second_id)
            direct_neighbors[second_id].add(first_id)

        ordered = sorted(
            topics,
            key=lambda topic: (-topic["event_count"], topic["first_seen"], topic["topic_id"]),
        )
        assigned = set()
        groups = []
        for primary in ordered:
            primary_id = primary["topic_id"]
            if primary_id in assigned:
                continue
            members = [primary]
            for topic_id in sorted(direct_neighbors[primary_id]):
                if topic_id not in assigned:
                    members.append(by_id[topic_id])
            if len(members) < 2:
                continue
            assigned.update(topic["topic_id"] for topic in members)
            groups.append(members)
        return groups

    def _merge_group(self, conn, group):
        primary = self._pick_primary(group)
        primary_id = primary["topic_id"]
        entities = list(primary["entities"])
        semantic_tags = list(primary.get("semantic_tags") or [])
        key_facts = list(primary["key_facts"])
        links = list(primary["links"])
        files = list(primary.get("files") or [])
        status = primary["status"]
        first_seen = primary["first_seen"]
        last_seen = primary["last_seen"]
        event_total = primary["event_count"]
        removed_paths = []

        for t in group:
            if t["topic_id"] == primary_id:
                continue
            tid = t["topic_id"]
            entities = merge_lists(entities, t["entities"], limit=40)
            semantic_tags = merge_lists(semantic_tags, t.get("semantic_tags"), limit=24)
            key_facts = merge_lists(key_facts, t["key_facts"], limit=60)
            links = merge_lists(links, t["links"], limit=40)
            files = _merge_file_refs(files, t.get("files"), limit=40)
            if t["status"] == "disputed":
                status = "disputed"
            if t["first_seen"] and (not first_seen or t["first_seen"] < first_seen):
                first_seen = t["first_seen"]
            if t["last_seen"] and (not last_seen or t["last_seen"] > last_seen):
                last_seen = t["last_seen"]
            event_total += t["event_count"]
            conn.execute("UPDATE events SET topic_id = ? WHERE topic_id = ?", (primary_id, tid))
            relation_rows = conn.execute(
                """
                SELECT source_topic_id, target_topic_id, relation, reason, created_at
                FROM relations
                WHERE source_topic_id = ? OR target_topic_id = ?
                ORDER BY relation_id
                """,
                (tid, tid),
            ).fetchall()
            for relation_row in relation_rows:
                original_source = int(relation_row["source_topic_id"])
                original_target = int(relation_row["target_topic_id"])
                new_source = primary_id if original_source == tid else original_source
                new_target = primary_id if original_target == tid else original_target
                if new_source == new_target and original_source != original_target:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO relations (
                        source_topic_id, target_topic_id, relation, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        new_source,
                        new_target,
                        relation_row["relation"],
                        relation_row["reason"],
                        relation_row["created_at"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE relations
                    SET reason = ?, created_at = ?
                    WHERE source_topic_id = ?
                      AND target_topic_id = ?
                      AND relation = ?
                      AND created_at > ?
                    """,
                    (
                        relation_row["reason"],
                        relation_row["created_at"],
                        new_source,
                        new_target,
                        relation_row["relation"],
                        relation_row["created_at"],
                    ),
                )
            conn.execute(
                "DELETE FROM relations WHERE source_topic_id = ? OR target_topic_id = ?",
                (tid, tid),
            )
            conn.execute("DELETE FROM topic_fts WHERE topic_id = ?", (tid,))
            conn.execute("DELETE FROM topics WHERE topic_id = ?", (tid,))
            removed_paths.append(self.full_obsidian_path(t["obsidian_path"]))

        conn.execute(
            """
            UPDATE topics
            SET entities_json = ?, semantic_tags_json = ?, key_facts_json = ?, links_json = ?, files_json = ?, status = ?,
                first_seen = ?, last_seen = ?, event_count = ?, updated_at = ?
            WHERE topic_id = ?
            """,
            (
                _json_dumps(entities), _json_dumps(semantic_tags), _json_dumps(key_facts), _json_dumps(links),
                _json_dumps(files), status, first_seen, last_seen, event_total, self.now_func(), primary_id,
            ),
        )
        self._upsert_fts(conn, primary_id)
        return primary_id, removed_paths

    def find_category_changes(self):
        """Return topics whose folder or filename should be normalized."""
        changes = []
        topics = self.list_topics()
        conn = self.connect()
        try:
            for topic in topics:
                canonical = self._canonical_category_for_topic(topic)
                profile = self._taxonomy_profile_for_topic(topic)
                profile_data = TAXONOMY_PROFILES.get(profile)
                taxonomy_version = int(profile_data["version"]) if profile_data else 0
                expected_path = self._unique_obsidian_path(
                    conn,
                    topic["topic_id"],
                    canonical,
                    topic["title"],
                    source_chat=topic["source_chat"],
                    vault_chat_name=topic.get("vault_chat_name", ""),
                    first_seen=topic["first_seen"],
                    current_path=topic["obsidian_path"],
                )
                path_needs_update = expected_path != topic["obsidian_path"]
                category_changed = canonical != topic["category"]
                metadata_changed = (
                    _row_get(topic, "taxonomy_profile", "") != profile
                    or int(_row_get(topic, "taxonomy_version", 0) or 0) != taxonomy_version
                )
                if category_changed or path_needs_update or metadata_changed:
                    changes.append({
                        "title": topic["title"],
                        "from": topic["category"],
                        "to": canonical,
                        "reason": "category" if category_changed else ("title" if path_needs_update else "taxonomy_profile"),
                        "from_path": topic["obsidian_path"],
                        "to_path": expected_path,
                    })
        finally:
            if conn is not None:
                conn.close()
        return changes

    def _topic_matches_taxonomy_profile(self, topic, profile):
        return self._taxonomy_profile_for_topic(topic) == profile

    def _taxonomy_resolution_for_stable_alias(self, *values):
        candidates = {
            str(value or "").strip()
            for value in values
            if str(value or "").strip()
        }
        matched_profiles = set()
        for username, raw_alias in self.taxonomy_aliases.items():
            alias = str(raw_alias or "").strip()
            if not alias or alias not in candidates:
                continue
            assigned = str(self.taxonomy_assignments.get(username) or "").strip()
            if assigned in TAXONOMY_PROFILES:
                matched_profiles.add(assigned)
        if len(matched_profiles) == 1:
            return TaxonomyResolution(matched_profiles.pop(), "stable_alias")
        if len(matched_profiles) > 1:
            return TaxonomyResolution("", "ambiguous_alias")
        return TaxonomyResolution("", "free_form")

    def _taxonomy_resolution_for_topic(self, topic):
        if _is_history_summary_row(topic):
            return TaxonomyResolution("", "free_form")
        stored = str(_row_get(topic, "taxonomy_profile", "") or "")
        if stored in TAXONOMY_PROFILES:
            return TaxonomyResolution(stored, "stored")
        username = str(_row_get(topic, "source_chat_username", "") or "").strip()
        assigned = str(self.taxonomy_assignments.get(username) or "").strip()
        if username and assigned in TAXONOMY_PROFILES:
            return TaxonomyResolution(assigned, "explicit")
        if username:
            return TaxonomyResolution("", "free_form")
        profile = taxonomy_profile_for_chat(_row_get(topic, "source_chat", ""))
        if profile:
            return TaxonomyResolution(profile, "legacy_name")
        return self._taxonomy_resolution_for_stable_alias(
            _row_get(topic, "vault_chat_name", ""),
            self._chat_folder_from_path(_row_get(topic, "obsidian_path", "")),
        )

    def _taxonomy_profile_for_topic(self, topic):
        return self._taxonomy_resolution_for_topic(topic).profile

    def _canonical_category_for_topic(self, topic):
        profile = self._taxonomy_profile_for_topic(topic)
        if profile in TAXONOMY_PROFILES:
            if (
                profile == HUMAN_AI_INTIMACY_PROFILE
                and _taxonomy_key(topic["category"]) == _taxonomy_key(LEGACY_TOOL_MODEL_CATEGORY)
            ):
                return _legacy_tool_model_category_for_topic(topic)
            return normalize_taxonomy_category(topic["category"], profile)
        return normalize_category(topic["category"])

    def taxonomy_projection(self, profile=HUMAN_AI_INTIMACY_PROFILE, *, conn=None):
        profile_data = TAXONOMY_PROFILES.get(profile)
        if profile_data is None:
            raise ValueError(f"unknown taxonomy profile: {profile}")

        owns_connection = conn is None
        if owns_connection:
            conn = self.connect()
        if conn is None:
            return {
                "profile": profile,
                "taxonomy_version": int(profile_data["version"]),
                "topic_changes": [],
                "render_topic_ids": [],
                "managed_date_index_paths": [],
            }

        shadow = sqlite3.connect(":memory:")
        shadow.row_factory = sqlite3.Row
        try:
            topics = [
                self._topic_dict(row)
                for row in conn.execute("SELECT * FROM topics ORDER BY topic_id")
            ]
            topic_changes = []
            reserved_paths = set()
            for topic in topics:
                if self._taxonomy_resolution_for_topic(topic).profile != profile:
                    continue
                if _is_history_summary_row(topic):
                    continue
                if (
                    profile == HUMAN_AI_INTIMACY_PROFILE
                    and _taxonomy_key(topic["category"]) == _taxonomy_key(LEGACY_TOOL_MODEL_CATEGORY)
                ):
                    new_category = _legacy_tool_model_category_for_topic(topic)
                else:
                    new_category = normalize_taxonomy_category(topic["category"], profile)
                new_path = self._unique_obsidian_path(
                    conn,
                    topic["topic_id"],
                    new_category,
                    topic["title"],
                    source_chat=topic["source_chat"],
                    vault_chat_name=topic.get("vault_chat_name", ""),
                    first_seen=topic["first_seen"],
                    current_path=topic["obsidian_path"],
                    reserved_paths=reserved_paths,
                )
                reserved_paths.add(new_path)
                before = {
                    "category": topic["category"],
                    "obsidian_path": topic["obsidian_path"],
                    "taxonomy_profile": topic.get("taxonomy_profile", ""),
                    "taxonomy_version": int(topic.get("taxonomy_version") or 0),
                }
                after = {
                    "category": new_category,
                    "obsidian_path": new_path,
                    "taxonomy_profile": profile,
                    "taxonomy_version": int(profile_data["version"]),
                }
                if before != after:
                    topic_changes.append({
                        "topic_id": int(topic["topic_id"]),
                        "before": before,
                        "after": after,
                    })

            changed_topic_ids = [change["topic_id"] for change in topic_changes]
            changed_link_target_ids = [
                change["topic_id"]
                for change in topic_changes
                if change["before"]["obsidian_path"] != change["after"]["obsidian_path"]
            ]
            relation_source_ids = []
            if changed_link_target_ids:
                placeholders = ", ".join("?" for _ in changed_link_target_ids)
                relation_source_ids = [
                    int(row["source_topic_id"])
                    for row in conn.execute(
                        f"""
                        SELECT DISTINCT r.source_topic_id, source.topic_key, source.title
                        FROM relations r
                        JOIN topics source ON source.topic_id = r.source_topic_id
                        WHERE r.target_topic_id IN ({placeholders})
                        ORDER BY r.source_topic_id
                        """,
                        changed_link_target_ids,
                    )
                    if not _is_history_summary_row(row)
                ]
            render_topic_ids = sorted(set(changed_topic_ids + relation_source_ids))

            conn.backup(shadow)
            self.apply_taxonomy_projection(shadow, {
                "topic_changes": topic_changes,
            })
            source_indexes = self.render_managed_date_indexes(conn)
            projected_indexes = self.render_managed_date_indexes(
                shadow,
                paths=set(source_indexes),
            )
            managed_date_index_paths = sorted(
                path
                for path, source_text in source_indexes.items()
                if projected_indexes.get(path) != source_text
            )
            return {
                "profile": profile,
                "taxonomy_version": int(profile_data["version"]),
                "topic_changes": topic_changes,
                "render_topic_ids": render_topic_ids,
                "managed_date_index_paths": managed_date_index_paths,
            }
        finally:
            shadow.close()
            if owns_connection:
                conn.close()

    def apply_taxonomy_projection(self, conn, projection):
        original_row_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            for change in projection["topic_changes"]:
                before = change["before"]
                after = change["after"]
                cursor = conn.execute(
                    """
                    UPDATE topics
                    SET category = ?, obsidian_path = ?, taxonomy_profile = ?, taxonomy_version = ?
                    WHERE topic_id = ?
                      AND category = ?
                      AND obsidian_path = ?
                      AND taxonomy_profile = ?
                      AND taxonomy_version = ?
                    """,
                    (
                        after["category"], after["obsidian_path"],
                        after["taxonomy_profile"], after["taxonomy_version"],
                        change["topic_id"], before["category"], before["obsidian_path"],
                        before["taxonomy_profile"], before["taxonomy_version"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("taxonomy_projection_drift")
                conn.execute(
                    """
                    UPDATE events
                    SET category = ?, taxonomy_profile = ?, taxonomy_version = ?
                    WHERE topic_id = ?
                    """,
                    (
                        after["category"], after["taxonomy_profile"],
                        after["taxonomy_version"], change["topic_id"],
                    ),
                )
                self._upsert_fts(conn, change["topic_id"])
        finally:
            conn.row_factory = original_row_factory

    def plan_taxonomy_migration(self, profile=HUMAN_AI_INTIMACY_PROFILE, example_limit=3):
        """Preview folder taxonomy migration without changing SQLite or Markdown files."""
        profile_data = TAXONOMY_PROFILES.get(profile)
        if profile_data is None:
            raise ValueError(f"unknown taxonomy profile: {profile}")

        topics = self.list_topics()
        changes = []
        scoped_topic_count = 0
        legacy_history_summary_count = 0
        unresolved_count = 0
        unresolved_items = []
        assignment_source_counts = {}
        mapping_index = {}
        reserved_paths = set()
        conn = self.connect()
        try:
            for topic in topics:
                resolution = self._taxonomy_resolution_for_topic(topic)
                if resolution.profile != profile:
                    continue
                scoped_topic_count += 1
                assignment_source_counts[resolution.source] = (
                    assignment_source_counts.get(resolution.source, 0) + 1
                )
                if _is_history_summary_row(topic):
                    legacy_history_summary_count += 1
                    continue
                old_category = topic["category"]
                if (
                    profile == HUMAN_AI_INTIMACY_PROFILE
                    and _taxonomy_key(old_category) == _taxonomy_key(LEGACY_TOOL_MODEL_CATEGORY)
                ):
                    new_category = _legacy_tool_model_category_for_topic(topic)
                else:
                    new_category = normalize_taxonomy_category(old_category, profile)
                if new_category == profile_data["unknown_policy"]:
                    unresolved_count += 1

                expected_path = self._unique_obsidian_path(
                    conn,
                    topic["topic_id"],
                    new_category,
                    topic["title"],
                    source_chat=topic["source_chat"],
                    vault_chat_name=topic.get("vault_chat_name", ""),
                    first_seen=topic["first_seen"],
                    current_path=topic["obsidian_path"],
                    reserved_paths=reserved_paths,
                )
                reserved_paths.add(expected_path)
                category_changed = new_category != old_category
                path_changed = expected_path != topic["obsidian_path"]
                if category_changed:
                    key = (old_category, new_category)
                    if key not in mapping_index:
                        mapping_index[key] = {
                            "from": old_category,
                            "to": new_category,
                            "count": 0,
                            "example_paths": [],
                        }
                    mapping = mapping_index[key]
                    mapping["count"] += 1
                    if len(mapping["example_paths"]) < example_limit:
                        mapping["example_paths"].append(topic["obsidian_path"])
                if category_changed or path_changed:
                    changes.append({
                        "topic_id": topic["topic_id"],
                        "title": topic["title"],
                        "source_chat": topic["source_chat"],
                        "vault_chat_name": topic.get("vault_chat_name", ""),
                        "from": old_category,
                        "to": new_category,
                        "reason": "category" if category_changed else "title",
                        "from_path": topic["obsidian_path"],
                        "to_path": expected_path,
                    })
                if new_category == profile_data["unknown_policy"]:
                    unresolved_items.append({
                        "topic_id": topic["topic_id"],
                        "title": topic["title"],
                        "source_chat": topic["source_chat"],
                        "vault_chat_name": topic.get("vault_chat_name", ""),
                        "from": old_category,
                        "to": new_category,
                        "from_path": topic["obsidian_path"],
                        "to_path": expected_path,
                    })
        finally:
            if conn is not None:
                conn.close()

        category_mappings = sorted(
            mapping_index.values(),
            key=lambda row: (-row["count"], row["from"], row["to"]),
        )
        return {
            "profile": profile,
            "taxonomy_version": profile_data["version"],
            "assignment_source_counts": assignment_source_counts,
            "folder_categories": list(profile_data["folder_categories"]),
            "total_topic_count": len(topics),
            "scoped_topic_count": scoped_topic_count,
            "migratable_topic_count": scoped_topic_count - legacy_history_summary_count,
            "legacy_history_summary_count": legacy_history_summary_count,
            "category_mappings": category_mappings,
            "category_change_count": sum(row["count"] for row in category_mappings),
            "path_change_count": len(changes),
            "unresolved_count": unresolved_count,
            "unresolved_items": unresolved_items,
            "changes": changes,
        }

    def knowledge_audit(
        self,
        *,
        taxonomy_profile=HUMAN_AI_INTIMACY_PROFILE,
        duplicate_threshold=85,
        example_limit=5,
    ):
        """Return a read-only summary of note relation and maintenance surfaces."""
        topics = self.list_topics()
        relation_counts = {}
        relation_examples = []
        conn = self.connect()
        try:
            if conn is not None:
                for row in conn.execute(
                    "SELECT relation, COUNT(*) AS count FROM relations GROUP BY relation ORDER BY relation"
                ):
                    relation_counts[row["relation"]] = int(row["count"])
                rows = conn.execute(
                    """
                    SELECT
                        r.relation,
                        s.title AS source_title,
                        s.obsidian_path AS source_path,
                        t.title AS target_title,
                        t.obsidian_path AS target_path
                    FROM relations r
                    JOIN topics s ON s.topic_id = r.source_topic_id
                    JOIN topics t ON t.topic_id = r.target_topic_id
                    ORDER BY r.created_at DESC, r.relation
                    LIMIT ?
                    """,
                    (int(example_limit),),
                ).fetchall()
                relation_examples = [
                    {
                        "relation": row["relation"],
                        "source_title": row["source_title"],
                        "source_path": row["source_path"],
                        "target_title": row["target_title"],
                        "target_path": row["target_path"],
                    }
                    for row in rows
                ]
        finally:
            if conn is not None:
                conn.close()

        duplicate_groups = self.find_duplicate_groups(threshold=duplicate_threshold)
        duplicate_examples = []
        for group in duplicate_groups[:example_limit]:
            primary = self._pick_primary(group)
            duplicate_examples.append({
                "primary": primary["title"],
                "merged": [
                    topic["title"]
                    for topic in group
                    if topic["topic_id"] != primary["topic_id"]
                ][:example_limit],
            })

        try:
            taxonomy_plan = self.plan_taxonomy_migration(taxonomy_profile, example_limit=1)
            taxonomy = {
                "profile": taxonomy_plan["profile"],
                "taxonomy_version": taxonomy_plan["taxonomy_version"],
                "assignment_source_counts": dict(
                    taxonomy_plan.get("assignment_source_counts") or {}
                ),
                "scoped_topic_count": taxonomy_plan["scoped_topic_count"],
                "migratable_topic_count": taxonomy_plan["migratable_topic_count"],
                "legacy_history_summary_count": taxonomy_plan["legacy_history_summary_count"],
                "category_change_count": taxonomy_plan["category_change_count"],
                "path_change_count": taxonomy_plan["path_change_count"],
                "unresolved_count": taxonomy_plan["unresolved_count"],
            }
        except ValueError:
            taxonomy = {
                "profile": taxonomy_profile,
                "taxonomy_version": 0,
                "assignment_source_counts": {},
                "scoped_topic_count": 0,
                "migratable_topic_count": 0,
                "legacy_history_summary_count": 0,
                "category_change_count": 0,
                "path_change_count": 0,
                "unresolved_count": 0,
            }

        category_changes = self.find_category_changes()
        return {
            "total_topics": len(topics),
            "relation_edge_count": sum(relation_counts.values()),
            "relation_counts": relation_counts,
            "relation_examples": relation_examples,
            "duplicate_group_count": len(duplicate_groups),
            "duplicate_examples": duplicate_examples,
            "taxonomy": taxonomy,
            "category_change_count": len(category_changes),
            "category_change_examples": category_changes[:example_limit],
        }

    def _canonicalize_categories(self, conn):
        changes = []
        removed_paths = []
        rows = conn.execute("SELECT * FROM topics ORDER BY topic_id").fetchall()
        for row in rows:
            topic_id = int(row["topic_id"])
            old_category = row["category"]
            old_profile = _row_get(row, "taxonomy_profile", "")
            old_version = int(_row_get(row, "taxonomy_version", 0) or 0)
            profile = self._taxonomy_profile_for_topic(row)
            profile_data = TAXONOMY_PROFILES.get(profile)
            new_version = int(profile_data["version"]) if profile_data else 0
            canonical = self._canonical_category_for_topic(row)
            old_path = row["obsidian_path"]
            new_path = self._unique_obsidian_path(
                conn,
                topic_id,
                canonical,
                row["title"],
                source_chat=row["source_chat"],
                vault_chat_name=_row_get(row, "vault_chat_name", "") or self._chat_folder_from_path(old_path),
                first_seen=row["first_seen"],
                current_path=old_path,
            )
            metadata_changed = old_profile != profile or old_version != new_version
            if old_category == canonical and old_path == new_path and not metadata_changed:
                continue

            conn.execute(
                """
                UPDATE topics
                SET category = ?, obsidian_path = ?, taxonomy_profile = ?, taxonomy_version = ?, updated_at = ?
                WHERE topic_id = ?
                """,
                (canonical, new_path, profile, new_version, self.now_func(), topic_id),
            )
            conn.execute(
                """
                UPDATE events
                SET category = ?, taxonomy_profile = ?, taxonomy_version = ?
                WHERE topic_id = ?
                """,
                (canonical, profile, new_version, topic_id),
            )
            self._upsert_fts(conn, topic_id)
            if old_path != new_path:
                removed_paths.append(self.full_obsidian_path(old_path))
            changes.append({
                "title": row["title"],
                "from": old_category,
                "to": canonical,
                "reason": "category" if old_category != canonical else ("title" if old_path != new_path else "taxonomy_profile"),
                "from_path": old_path,
                "to_path": new_path,
            })
        return changes, removed_paths

    def _remove_empty_obsidian_dirs(self):
        root = os.path.join(self.obsidian_root, self.obsidian_subdir)
        if not os.path.isdir(root):
            return 0
        removed = 0
        for current, _, _ in os.walk(root, topdown=False):
            if current == root:
                continue
            try:
                os.rmdir(current)
                removed += 1
            except OSError:
                pass
        return removed

    def reexport_all(self):
        """Rewrite every topic's Markdown to the current obsidian_root."""
        conn = self.connect()
        if conn is None:
            return 0
        count = 0
        try:
            ids = [r["topic_id"] for r in conn.execute("SELECT topic_id FROM topics")]
            for tid in ids:
                self._write_topic_markdown(conn, tid)
                count += 1
        finally:
            conn.close()
        self.write_date_indexes()
        return count

    def _verify_exported_topic_paths(self):
        missing = []
        for topic in self.list_topics():
            path = self.full_obsidian_path(topic.get("obsidian_path"))
            if not path or not os.path.isfile(path):
                missing.append(path or f"topic:{topic['topic_id']}")
        if missing:
            raise RuntimeError(f"knowledge reexport missing {len(missing)} topic file(s)")

    def _remove_obsolete_topic_paths(self, removed_paths):
        surviving_paths = {
            self.full_obsidian_path(topic.get("obsidian_path"))
            for topic in self.list_topics()
            if topic.get("obsidian_path")
        }
        for path in removed_paths:
            try:
                if path and path not in surviving_paths and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    def reorganize_paths(self):
        """Move topics to the current folder scheme and re-export all Markdown."""
        if self.read_only:
            raise RuntimeError("knowledge store is read-only")

        conn = self.connect()
        removed_paths = []
        changes = []
        try:
            changes, removed_paths = self._canonicalize_categories(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        reexport_count = self.reexport_all()
        self._verify_exported_topic_paths()
        self._remove_obsolete_topic_paths(removed_paths)

        return {
            "path_changes": changes,
            "path_change_count": len(changes),
            "reexport_count": reexport_count,
            "removed_empty_dirs": self._remove_empty_obsidian_dirs(),
            "date_indexes": self.plan_date_indexes(),
        }

    def run_maintenance(self, dry_run=False, threshold=85):
        """Merge duplicate topics, fold category folders, then re-export all notes."""
        if self.read_only:
            raise RuntimeError("knowledge store is read-only")

        groups = self.find_duplicate_groups(threshold=threshold)
        category_changes = self.find_category_changes()
        summary = []
        for g in groups:
            primary = self._pick_primary(g)
            summary.append({
                "primary": primary["title"],
                "merged": [t["title"] for t in g if t["topic_id"] != primary["topic_id"]],
            })
        merge_note_count = sum(len(g) for g in groups)
        result = {
            "duplicate_groups": summary,
            "group_count": len(groups),
            "merge_note_count": merge_note_count,
            "removed_count": merge_note_count - len(groups),
            "total_topics": len(self.list_topics()),
            "category_changes": category_changes,
            "category_change_count": len(category_changes),
        }
        if dry_run:
            result["reexport_count"] = result["total_topics"] - result["removed_count"]
            result["date_indexes"] = self.plan_date_indexes()
            return result

        conn = self.connect()
        removed_paths = []
        applied_category_changes = []
        try:
            for g in groups:
                _, paths = self._merge_group(conn, g)
                removed_paths.extend(paths)
            applied_category_changes, category_paths = self._canonicalize_categories(conn)
            removed_paths.extend(category_paths)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        reexport_count = self.reexport_all()
        self._verify_exported_topic_paths()
        self._remove_obsolete_topic_paths(removed_paths)

        result["category_changes"] = applied_category_changes
        result["category_change_count"] = len(applied_category_changes)
        result["reexport_count"] = reexport_count
        result["removed_empty_dirs"] = self._remove_empty_obsidian_dirs()
        result["date_indexes"] = self.plan_date_indexes()
        return result

    @staticmethod
    def _now_text(ts):
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def normalize_candidate(decision):
    title = str(decision.get("title") or "发现关注内容").strip()[:80] or "发现关注内容"
    summary = str(decision.get("summary") or decision.get("digest") or "").strip()
    raw_category = str(decision.get("raw_category") or decision.get("category") or "").strip()
    raw_links = _normalize_list(decision.get("links"), limit=20)
    if not raw_links:
        raw_links = _normalize_list(re.findall(r"https?://[^\s）)]+", summary), limit=20)
    has_private_record_link = any(is_wechat_record_url(link) for link in raw_links)
    links = [link for link in raw_links if not is_wechat_record_url(link)]
    files = _normalize_file_refs(decision.get("files"), limit=20)
    resource_status = _normalize_resource_status(decision.get("resource_status"))
    resource_lead = _truthy(decision.get("resource_lead")) or resource_status in {
        "mentioned_private",
        "mentioned_pending",
    }
    if resource_status == "linked" and not links and not files and has_private_record_link:
        resource_status = "mentioned_private"
        resource_lead = True
    if resource_status == "none" and resource_lead:
        resource_status = "mentioned_pending"
    return {
        "title": title,
        "summary": summary[:1800],
        "topic_key": str(decision.get("topic_key") or title).strip()[:100],
        "category": normalize_category(decision.get("category")),
        "raw_category": raw_category[:80],
        "entities": _normalize_list(decision.get("entities"), limit=16),
        "semantic_tags": _normalize_list(decision.get("semantic_tags"), limit=12),
        "key_facts": _normalize_list(decision.get("key_facts"), limit=20),
        "links": links,
        "files": files,
        "event_type": str(decision.get("event_type") or "").strip()[:80],
        "status_hint": str(decision.get("status_hint") or "").strip()[:80],
        "resource_lead": resource_lead,
        "resource_status": resource_status,
        "lead_key": str(decision.get("lead_key") or decision.get("topic_key") or title).strip()[:100],
    }


def _truthy(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def _normalize_resource_status(value):
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "private": "mentioned_private",
        "pending": "mentioned_pending",
        "to_be_shared": "mentioned_pending",
        "not_shared_yet": "mentioned_pending",
        "file": "attached",
        "files": "attached",
        "attachment": "attached",
        "url": "linked",
        "link": "linked",
    }
    text = aliases.get(text, text)
    if text in {"attached", "linked", "mentioned_private", "mentioned_pending", "none"}:
        return text
    return "none"


def normalize_relation(value):
    text = str(value or "").strip().lower()
    mapping = {
        "same": "duplicate",
        "repeat": "duplicate",
        "repeated": "duplicate",
        "duplicated": "duplicate",
        "duplicate": "duplicate",
        "old": "duplicate",
        "update": "update",
        "updated": "update",
        "new_info": "update",
        "new": "new",
        "fresh": "new",
        "contradiction": "contradiction",
        "conflict": "contradiction",
        "correction": "contradiction",
        "debunk": "contradiction",
        "rumor_debunked": "contradiction",
    }
    return mapping.get(text, "new")


def normalize_category(value):
    text = str(value or "").strip()
    if not text:
        return "未分类"
    compact = re.sub(r"[\s,，/、]+", "", text).lower()
    for canonical, needles in CATEGORY_ALIASES:
        if any(needle.lower().replace(" ", "") in compact for needle in needles):
            return canonical
    return text[:40]


def _taxonomy_key(value):
    text = str(value or "").strip().lower()
    text = text.replace("♥︎", "").replace("♥", "")
    return re.sub(r"[\s,，/、&+]+", "", text)


def _legacy_tool_model_category_for_topic(topic):
    haystack = str(_row_get(topic, "title", "") or "").lower()
    for canonical, needles in LEGACY_TOOL_MODEL_RULES:
        if any(str(needle).lower() in haystack for needle in needles):
            return canonical
    return "待归类"


def normalize_taxonomy_category(value, profile=HUMAN_AI_INTIMACY_PROFILE):
    profile_data = TAXONOMY_PROFILES.get(profile)
    if profile_data is None:
        raise ValueError(f"unknown taxonomy profile: {profile}")
    text = str(value or "").strip()
    if not text:
        return profile_data["unknown_policy"]

    lookup = {}
    for canonical, aliases in profile_data["category_map"].items():
        lookup[_taxonomy_key(canonical)] = canonical
        for alias in aliases:
            lookup[_taxonomy_key(alias)] = canonical
    return lookup.get(_taxonomy_key(text), profile_data["unknown_policy"])


def normalize_status(value):
    text = str(value or "").strip().lower()
    if text in {"resolved", "confirmed", "disputed", "tracking", "rumor"}:
        return text
    return "tracking"


def merge_lists(old_values, new_values, limit=30):
    merged = []
    seen = set()
    for value in _normalize_list(old_values, limit=limit) + _normalize_list(new_values, limit=limit):
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(value)
        if len(merged) >= limit:
            break
    return merged
