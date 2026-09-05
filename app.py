"""
WeChat Group Chat AI Summary - macOS menu bar tool
"""
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime

from core.background_jobs import dispatch_background_job

_BACKGROUND_EXIT_CODE = dispatch_background_job(sys.argv[1:])
if _BACKGROUND_EXIT_CODE is not None:
    raise SystemExit(_BACKGROUND_EXIT_CODE)

import rumps

# --- For dialog top-most + custom dialogs ---
try:
    from AppKit import (NSApplication, NSAlert, NSTextField, NSView, NSObject,
                        NSButton, NSImage, NSFont, NSScrollView, NSTextView,
                        NSBezelBorder, NSPopUpButton,
                        NSApplicationActivationPolicyAccessory)
    import objc
    _HAS_APPKIT = True
except ImportError:
    _HAS_APPKIT = False
    NSApplication = None
    NSApplicationActivationPolicyAccessory = None

# ── Menu open detection (NSMenuDelegate) ──────────────────────
if _HAS_APPKIT:
    class _MenuOpenDelegate(NSObject):
        """NSMenuDelegate: detect menu open events to trigger auto-refresh."""

        def init(self):
            self = objc.super(_MenuOpenDelegate, self).init()
            if self is None:
                return None
            self.app_ref = None
            self._last_refresh = 0.0
            return self

        def menuWillOpen_(self, menu):
            app = self.app_ref
            if not app:
                return
            # On menu click, if in done/error state and not summarizing, restore normal icon
            if (not app._summarizing
                    and getattr(app, '_current_status', None) in (ICON_DONE, ICON_ERROR)):
                app._set_status(ICON_NORMAL)
            if (app.config.get("auto_refresh_on_open")
                    and app.db and not app._summarizing):
                now = time.time()
                if now - self._last_refresh > 5:  # At least 5 seconds between refreshes
                    self._last_refresh = now
                    print("[auto-refresh] 菜单打开，后台刷新群聊...")
                    threading.Thread(target=app._do_silent_refresh, daemon=True).start()

from core.config import (
    active_monitor_chats,
    selected_drive_sync_chats,
    load_config,
    update_config,
    merge_monitor_chat_preferences,
    CONFIG_FILE,
    DATA_DIR,
)
from core.app_runtime import AppAlreadyRunning, AppInstanceLock
from core.mcp_config import claude_code_add_command, claude_desktop_config
from core.daily_digest import (
    DAILY_DIGEST_STATE_FILE,
    mark_daily_digest_success,
    notification_summary,
    refresh_pending_daily_digests,
    should_run_daily_digest,
    write_daily_digest,
)
from core.notification_target import (
    notification_data_for_path,
    notification_open_commands_for_path,
    target_path_from_notification,
)
from core.keychain import save_key, load_key
from core.url_safety import redact_urls_in_text
from core.key_extractor import (
    is_wechat_running,
    is_wechat_signed,
    extract_keys,
    get_cached_keys,
    check_new_databases,
)
from core.wechat_db import WeChatDB
from core.wechat_source_guard import WeChatSourceGuard
from core.bookmark import get_bookmark, set_bookmark, get_summary_time, clear_all_bookmarks
from core.chat_groups import (
    load_groups, save_groups, create_group, delete_group,
    add_chat_to_group, remove_chat_from_group, get_group_chats, get_chat_group,
    set_group_summary_time, get_group_summary_time,
)
from core.knowledge import (
    KNOWLEDGE_DB,
    OBSIDIAN_ROOT,
    KnowledgeStore,
    ensure_obsidian_vault,
    safe_obsidian_subdir,
)
from core.attachment_archive import process_pending_from_config
from core.resource_backup import (
    MountedResourceBackup,
    evaluate_link_backfill_outcome,
    evaluate_resource_backup_outcome,
)
from core.resource_capture import (
    ResourceCaptureError,
    SelectedResourceCapture,
    resource_backup_chat_candidates,
    update_resource_backup_selection,
)
from core.google_drive_auth import GoogleDriveAuthError, GoogleDriveOAuth
from core.google_drive_client import GoogleDriveClient
from core.google_drive_file_sync import GoogleDriveFileSync
from core.monitor import (
    HITS_DIR,
    MonitorConfigError,
    TopicMonitor,
    initialize_state_if_needed,
    load_state,
    reset_state_to_now,
    state_file_for_chat,
)
from core.monitor_state import MonitorStateError
from ai.factory import create_provider

# Summary history save directory
SUMMARY_DIR = os.path.join(DATA_DIR, "summaries")
os.makedirs(SUMMARY_DIR, exist_ok=True)

# AI service list
AI_PROVIDERS = [
    ("qwen", "通义千问 (推荐)"),
    ("deepseek", "DeepSeek"),
    ("ollama", "本地 Ollama (免费)"),
    ("claude", "Claude"),
    ("openai", "OpenAI"),
]

# Menu bar icon resources. The menu bar itself uses text because tiny template
# icons are easy to miss on dense/notched MacBook menu bars.
_ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
ICON_PNG = os.path.join(_ICON_DIR, "icon.png")
ICON_LOADING_PNG = os.path.join(_ICON_DIR, "icon_loading.png")
ICON_DONE_PNG = os.path.join(_ICON_DIR, "icon_done.png")
ICON_ERROR_PNG = os.path.join(_ICON_DIR, "icon_error.png")
APP_ICON_PNG = os.path.join(_ICON_DIR, "app_icon.png")
_USE_PNG_ICON = False

# Add space after emoji to force macOS stable width, prevent clipping
ICON_NORMAL = "💬 "
ICON_LOADING = "⏳ "
ICON_DONE = "✅ "
ICON_ERROR = "❌ "

BATCH_CONFIRM_CHAT_COUNT = 5
CUSTOM_SUMMARY_MAX_COUNT = 1000


def _hide_dock_icon(
    has_appkit=_HAS_APPKIT,
    ns_application=NSApplication,
    accessory_policy=NSApplicationActivationPolicyAccessory,
):
    """Keep the menu bar app out of Dock/Cmd-Tab when AppKit is available."""
    if not has_appkit or ns_application is None or accessory_policy is None:
        return False
    try:
        app = ns_application.sharedApplication()
        result = app.setActivationPolicy_(accessory_policy)
        print("[app] Dock 图标已隐藏")
        return True if result is None else bool(result)
    except Exception as e:
        print(f"[app] 隐藏 Dock 图标失败: {e}")
        return False

# PNG icon state mapping
_ICON_PNG_MAP = {
    ICON_NORMAL: ICON_PNG,
    ICON_LOADING: ICON_LOADING_PNG,
    ICON_DONE: ICON_DONE_PNG,
    ICON_ERROR: ICON_ERROR_PNG,
}


class UserCancelled(RuntimeError):
    """Raised when the user asks the current background task to stop."""


def _notification_text(value, limit=700):
    """Normalize notification text and keep the body small enough for macOS."""
    text = "" if value is None else str(value)
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _notify_with_osascript(title, subtitle, message):
    script = """
on run argv
    set notificationTitle to item 1 of argv
    set notificationSubtitle to item 2 of argv
    set notificationMessage to item 3 of argv
    display notification notificationMessage with title notificationTitle subtitle notificationSubtitle sound name "default"
end run
"""
    result = subprocess.run(
        ["osascript", "-e", script, title, subtitle, message],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "osascript notification failed")


def _notify(title, subtitle, message, data=None):
    """Send notification safely, falling back when a backend is unavailable."""
    title = _notification_text(title, 120) or "微信总结"
    subtitle = _notification_text(subtitle, 180)
    message = _notification_text(message, 700)

    try:
        rumps.notification(title, subtitle, message, data=data)
        print(f"[notify] rumps ok: {title} / {subtitle}")
        return
    except Exception as e:
        print(f"[notify] rumps failed: {e}")

    if sys.platform == "darwin":
        try:
            _notify_with_osascript(title, subtitle, message)
            print(f"[notify] osascript ok: {title} / {subtitle}")
            return
        except Exception as e:
            print(f"[notify] osascript failed: {e}")

    print(f"[{title}] {subtitle}: {message}")


@rumps.notifications
def _on_notification_click(notification):
    path = target_path_from_notification(getattr(notification, "data", None))
    if not path:
        print("[notify] clicked without openable target")
        return
    for command in notification_open_commands_for_path(path):
        try:
            result = subprocess.run(command, check=False)
        except Exception as e:
            print(f"[notify] open target failed via {command[1]}: {e}")
            continue
        if result.returncode == 0:
            method = "Obsidian URI" if command[1].startswith("obsidian://") else "default open"
            print(f"[notify] opened target via {method}: {path}")
            return
        print(f"[notify] open target returned {result.returncode} via {command[1]}")
    print(f"[notify] open target failed: {path}")


def _wechat_signing_message():
    return "请重新双击启动.command，完成微信授权"


class WeGroupchatObsidianApp(rumps.App):
    def __init__(self):
        if _USE_PNG_ICON:
            super().__init__("微信总结", icon=ICON_PNG, template=True, quit_button="退出")
            self.title = "微信总结"
        else:
            super().__init__("微信总结", title="微信总结", quit_button="退出")
        _hide_dock_icon()
        # Set app icon (replace Python rocket icon in dialogs and Dock)
        if _HAS_APPKIT and os.path.isfile(APP_ICON_PNG):
            try:
                ns_icon = NSImage.alloc().initWithContentsOfFile_(APP_ICON_PNG)
                if ns_icon:
                    NSApplication.sharedApplication().setApplicationIconImage_(ns_icon)
            except Exception:
                pass
        self.config = load_config()
        self._resource_file_resolution_session_enabled = False
        self._resource_file_resolution_session_epoch = 0
        self.db = None
        self.ai = None
        self._summarizing = False
        self._cancel_requested = False
        self._active_task = ""
        self._last_summary = None
        self._current_status = ICON_NORMAL
        self._monitor_timer = None
        self._monitor_wakeup_timer = None
        self._monitor_lock = threading.Lock()
        self._monitor_last_error = ""
        self._monitor_last_dispatch_ts = 0
        self._daily_digest_timer = None
        self._daily_digest_lock = threading.Lock()
        self._drive_sync_timer = None
        self._drive_sync_lock = threading.Lock()
        self._resource_backup_timer = None
        self._resource_backup_lock = threading.Lock()
        self._source_guard_timer = None
        self._source_guard_lock = threading.Lock()
        self._config_reconcile_timer = None

        # Build menu
        self.menu = [
            rumps.MenuItem("刷新群聊列表", callback=self.refresh_groups),
            rumps.MenuItem("🔍 关键词搜索", callback=self._on_search_click),
            rumps.MenuItem("⛔ 停止当前任务", callback=self._cancel_current_task),
            rumps.separator,
            # Dynamic area: ungrouped chats (📎) inserted via insert_after
            rumps.separator,
            # Dynamic area: groups (📂) inserted via insert_before "📋 ..."
            # Dynamic area: latest summary (📝) inserted before "📋 ..."
            rumps.MenuItem("📋 最近总结"),
            rumps.separator,
            self._build_mcp_menu(),
            self._build_monitor_menu(),
            self._build_resource_backup_menu(),
            self._build_drive_sync_menu(),
            self._build_settings_menu(),
            rumps.MenuItem("🔄 刷新数据源", callback=self.reextract_keys),
        ]

        self._rebuild_summary_history()

        # Main thread queue: background threads safely update UI via this queue
        self._main_queue = queue.Queue()
        self._queue_timer = rumps.Timer(self._process_main_queue, 0.3)
        self._queue_timer.start()

        # Auto-refresh on menu open (NSMenuDelegate)
        self._menu_delegate = None
        if _HAS_APPKIT:
            self._setup_delegate_timer = rumps.Timer(self._setup_menu_delegate, 1)
            self._setup_delegate_timer.start()

        self._configure_monitor_timer()
        self._configure_daily_digest_timer()
        self._configure_resource_backup_timer()
        self._configure_drive_sync_timer()
        self._configure_source_guard_timer()
        self._config_reconcile_timer = rumps.Timer(
            self._on_config_reconcile_timer,
            2,
        )
        self._config_reconcile_timer.start()

        # Background initialization
        threading.Thread(target=self._init_background, daemon=True).start()

    # ── Safely set menu bar title ───────────────────────────────

    def _set_status(self, new_title):
        """Safely set menu bar status icon."""
        try:
            if _USE_PNG_ICON:
                # Keep a visible label; tiny template icons are easy to miss.
                png_path = _ICON_PNG_MAP.get(new_title, ICON_PNG)
                self.icon = png_path
                self.title = "微信总结"
            else:
                self.title = "微信总结"
            self._current_status = new_title
        except Exception:
            self.title = new_title

    # ── Settings menu ────────────────────────────────────────

    def _update_config(self, *, patch=None, mutator=None):
        self.config = update_config(mutator, patch=patch)
        return self.config

    @staticmethod
    def _runtime_config_values(config, keys):
        return tuple(config.get(key) for key in keys)

    def _on_config_reconcile_timer(self, _):
        try:
            current = load_config()
        except Exception as exc:
            print(f"[config] reconcile read failed: {type(exc).__name__}")
            return
        if int(current.get("config_revision") or 0) == int(
            self.config.get("config_revision") or 0
        ):
            return
        self._reconcile_runtime_config(current)

    def _reconcile_runtime_config(self, current):
        previous = self.config
        self.config = current
        runtime_groups = (
            (
                ("monitor_enabled", "monitor_interval_minutes"),
                self._configure_monitor_timer,
            ),
            (
                (
                    "daily_digest_enabled", "daily_digest_time",
                    "daily_digest_timezone",
                ),
                self._configure_daily_digest_timer,
            ),
            (
                ("resource_backup_enabled", "resource_backup_interval_seconds"),
                self._configure_resource_backup_timer,
            ),
            (
                (
                    "google_drive_file_sync_enabled",
                    "google_drive_file_sync_paused",
                    "google_drive_file_sync_interval_seconds",
                ),
                self._configure_drive_sync_timer,
            ),
            (
                (
                    "wechat_source_guard_enabled",
                    "wechat_source_guard_interval_seconds",
                ),
                self._configure_source_guard_timer,
            ),
        )
        for keys, reconcile in runtime_groups:
            if self._runtime_config_values(previous, keys) != self._runtime_config_values(
                current, keys
            ):
                reconcile()
        self._rebuild_settings_menu()
        self._rebuild_monitor_menu()
        self._rebuild_resource_backup_menu()
        self._rebuild_drive_sync_menu()
        print(
            "[config] reconciled revision "
            f"{int(current.get('config_revision') or 0)}"
        )

    def _build_settings_menu(self):
        """Build settings submenu."""
        settings = rumps.MenuItem("⚙️ 设置")

        # AI service selection
        ai_menu = rumps.MenuItem("🤖 AI 服务")
        current = self.config.get("ai_provider", "qwen")
        for key, label in AI_PROVIDERS:
            prefix = "✅ " if key == current else "    "
            item = rumps.MenuItem(
                f"{prefix}{label}",
                callback=self._make_provider_callback(key),
            )
            ai_menu.add(item)
        settings.add(ai_menu)

        # API Key settings
        has_key = bool(load_key("ai-api-key"))
        key_status = "已设置 ✅" if has_key else "未设置 ❌"
        settings.add(rumps.MenuItem(
            f"🔑 API Key ({key_status})",
            callback=self._set_api_key,
        ))
        model = self.config.get("ai_model", "").strip() or "默认"
        settings.add(rumps.MenuItem(
            f"🧠 AI 模型 ({model})",
            callback=self._set_ai_model,
        ))

        # Reset
        settings.add(rumps.separator)
        settings.add(rumps.MenuItem(
            "🗑️ 重置所有书签",
            callback=self._reset_bookmarks,
        ))

        # Current status
        settings.add(rumps.separator)
        settings.add(rumps.MenuItem(
            "📂 打开配置文件",
            callback=self.open_config_file,
        ))
        settings.add(rumps.MenuItem(
            "📁 打开总结目录",
            callback=self._open_summary_dir,
        ))

        # Auto-refresh toggle
        settings.add(rumps.separator)
        auto_refresh = self.config.get("auto_refresh_on_open", False)
        refresh_prefix = "✅ " if auto_refresh else "      "
        settings.add(rumps.MenuItem(
            f"{refresh_prefix}打开菜单时自动刷新",
            callback=self._toggle_auto_refresh,
        ))

        # Show group nickname toggle
        show_nickname = self.config.get("show_group_nickname", True)
        nick_prefix = "✅ " if show_nickname else "      "
        settings.add(rumps.MenuItem(
            f"{nick_prefix}总结中显示群昵称",
            callback=self._toggle_group_nickname,
        ))

        # Batch summary message limit per group
        batch_limit = self.config.get("batch_msg_limit", 100)
        batch_menu = rumps.MenuItem("📊 小组总结每群条数")
        for val in [50, 100, 200, 500]:
            prefix = "✅ " if batch_limit == val else "      "
            batch_menu.add(rumps.MenuItem(
                f"{prefix}{val} 条",
                callback=self._make_batch_limit_callback(val),
            ))
        settings.add(batch_menu)

        # Hide inactive chats
        hide_months = self.config.get("hide_inactive_months", 1)
        hide_menu = rumps.MenuItem("🕐 隐藏不活跃群聊")
        options = [("关闭", 0), ("1 个月", 1), ("3 个月", 3), ("6 个月", 6)]
        for label, val in options:
            prefix = "✅ " if hide_months == val else "      "
            hide_menu.add(rumps.MenuItem(
                f"{prefix}{label}",
                callback=self._make_hide_inactive_callback(val),
            ))
        settings.add(hide_menu)

        return settings

    def _rebuild_settings_menu(self):
        """Rebuild settings menu (after config change)."""
        if "⚙️ 设置" in self.menu:
            del self.menu["⚙️ 设置"]
        self.menu.insert_before("🔄 刷新数据源", self._build_settings_menu())

    def _rebuild_monitor_menu(self):
        """Rebuild top-level monitor menu."""
        if "🔔 关注推送" in self.menu:
            del self.menu["🔔 关注推送"]
        self.menu.insert_after("🔌 MCP 服务", self._build_monitor_menu())

    # ── Mounted selected-resource backup ─────────────────────

    def _configure_source_guard_timer(self):
        if self._source_guard_timer:
            try:
                self._source_guard_timer.stop()
            except Exception:
                pass
            self._source_guard_timer = None
        if not self.config.get("wechat_source_guard_enabled", False):
            return
        interval = max(
            60,
            int(self.config.get("wechat_source_guard_interval_seconds", 300)),
        )
        self._source_guard_timer = rumps.Timer(
            self._on_source_guard_timer,
            interval,
        )
        self._source_guard_timer.start()
        print(f"[source-guard] long-lived timer started: every {interval} seconds")

    def _on_source_guard_timer(self, _):
        if self.config.get("wechat_source_guard_enabled", False):
            self._start_source_guard_consumer()

    def _start_source_guard_consumer(self):
        threading.Thread(
            target=self._run_source_guard_consumer,
            daemon=True,
        ).start()

    def _run_source_guard_consumer(self):
        if not self._source_guard_lock.acquire(blocking=False):
            return
        try:
            result = WeChatSourceGuard(load_config()).check()
            print(
                "[source-guard] "
                f"state={result.get('state')} result={result.get('last_result')}"
            )
        except Exception as exc:
            print(f"[source-guard] check failed: {type(exc).__name__}")
        finally:
            self._source_guard_lock.release()

    def _resource_capture_service(self, *, source=False, config=None):
        config = dict(config or self.config)
        return SelectedResourceCapture.from_config(
            config,
            source=self.db if source else None,
        )

    def _build_resource_backup_menu(self):
        menu = rumps.MenuItem("🔗 资源索引与本地备份")
        try:
            capture_service = self._resource_capture_service()
            status = capture_service.status()
            counts = status.get("counts") or {}
            links = sum(
                int(count or 0)
                for key, count in counts.items()
                if str(key).startswith("link:")
            )
            files = sum(
                int(count or 0)
                for key, count in counts.items()
                if str(key).startswith("file:")
            )
            pending = int(status.get("pending_files") or 0)
            selected = int(status.get("selected_chats") or 0)
            backup_status = MountedResourceBackup.from_config(
                self.config,
                capture=capture_service,
            ).status()
            coverage = backup_status.get("coverage") or {}
            delivered_objects = int(coverage.get("delivered_objects") or 0)
            delivered_occurrences = int(
                coverage.get("delivered_occurrences") or 0
            )
            if "non_delivered_occurrences" in coverage:
                pending = int(coverage.get("non_delivered_occurrences") or 0)
        except Exception:
            links = files = pending = selected = 0
            delivered_objects = delivered_occurrences = 0
        enabled = bool(self.config.get("resource_backup_enabled", False))
        resolve_files = bool(self._resource_file_resolution_session_enabled)
        menu.add(rumps.MenuItem(
            f"状态: {'后台更新已开启' if enabled else '后台更新已关闭'}"
        ))
        menu.add(rumps.MenuItem(
            f"群聊: {selected} · 链接: {links} · 附件记录: {files}"
        ))
        menu.add(rumps.MenuItem(
            f"已备份: {delivered_objects} 个文件 · "
            f"{delivered_occurrences} 次出现 · 待补齐: {pending} 条"
        ))
        menu.add(rumps.MenuItem(
            "附件解析: 本次会话已允许" if resolve_files
            else "附件解析: 本次会话未允许"
        ))
        menu.add(rumps.separator)
        menu.add(rumps.MenuItem(
            "⏹ 关闭后台更新" if enabled else "▶️ 开启后台更新",
            callback=self._toggle_resource_backup,
        ))
        menu.add(rumps.MenuItem(
            "✅ 自动解析微信附件（本次 app 会话）"
            if resolve_files
            else "⬜ 自动解析微信附件（需显式授权）",
            callback=self._toggle_resource_file_resolution,
        ))
        menu.add(rumps.MenuItem(
            "🔄 立即更新资源索引",
            callback=self._run_resource_backup_now,
        ))
        menu.add(rumps.MenuItem(
            "📂 在 Finder 打开文件备份",
            callback=self._open_resource_backup_portal,
        ))
        menu.add(rumps.MenuItem(
            "📚 补历史链接...",
            callback=self._request_link_backfill,
        ))
        menu.add(rumps.MenuItem(
            "🎯 选择群聊...",
            callback=self._select_resource_backup_chats,
        ))
        return menu

    def _rebuild_resource_backup_menu(self):
        if "🔗 资源索引与本地备份" in self.menu:
            del self.menu["🔗 资源索引与本地备份"]
        anchor = "🔔 关注推送" if "🔔 关注推送" in self.menu else "🔌 MCP 服务"
        self.menu.insert_after(anchor, self._build_resource_backup_menu())

    def _configure_resource_backup_timer(self):
        if self._resource_backup_timer:
            try:
                self._resource_backup_timer.stop()
            except Exception:
                pass
            self._resource_backup_timer = None
        if not self.config.get("resource_backup_enabled", False):
            return
        interval = max(
            60,
            int(self.config.get("resource_backup_interval_seconds", 300)),
        )
        self._resource_backup_timer = rumps.Timer(
            self._on_resource_backup_timer,
            interval,
        )
        self._resource_backup_timer.start()
        print(f"[resource-backup] long-lived timer started: every {interval} seconds")

    def _on_resource_backup_timer(self, _):
        if self.db:
            self._start_resource_backup_consumer(manual=False)

    def _toggle_resource_backup(self, _):
        enabled = not bool(self.config.get("resource_backup_enabled", False))
        self._update_config(patch={"resource_backup_enabled": enabled})
        if enabled:
            self._resource_capture_service().initialize_selected_chat_cursors()
        self._configure_resource_backup_timer()
        self._rebuild_resource_backup_menu()
        _notify(
            "资源索引与本地备份",
            "后台更新已开启" if enabled else "后台更新已关闭",
            (
                "在长驻菜单栏进程内更新；文件解析仍由独立开关控制。"
                if enabled
                else "不会再开始新的扫描；ledger、CAS 与已生成索引全部保留。"
            ),
        )

    def _toggle_resource_file_resolution(self, _):
        self._delayed_run(self._show_resource_file_resolution_dialog)

    def _show_resource_file_resolution_dialog(self):
        enabled = not bool(self._resource_file_resolution_session_enabled)
        if enabled:
            self._bring_to_front()
            try:
                confirmed = self._confirm_dialog(
                    "允许本次 app 会话解析微信附件？",
                    "macOS 可能显示一次“访问其他 App 数据”提示。授权仅用于显式选中群聊的微信附件 cache；补链接不需要此权限。关闭本开关会立即停止新的文件解析。",
                    ok="开启",
                )
            finally:
                self._release_front()
            if not confirmed:
                return
        self._resource_file_resolution_session_enabled = enabled
        self._resource_file_resolution_session_epoch = (
            int(getattr(self, "_resource_file_resolution_session_epoch", 0)) + 1
        )
        self._rebuild_resource_backup_menu()
        if enabled and self.db:
            self._start_resource_backup_consumer(manual=True)
        else:
            _notify(
                "资源索引与本地备份",
                "自动文件解析已关闭",
                "链接与 metadata 索引仍会更新；不会再读取微信附件 bytes。",
            )

    def _run_resource_backup_now(self, _):
        if not self.db:
            _notify("资源索引与本地备份", "数据源未就绪", "请稍后再试。")
            return
        self._start_resource_backup_consumer(manual=True)

    def _open_resource_backup_portal(self, _):
        backup = MountedResourceBackup.from_config(load_config())
        portal_path = backup.existing_target_portal_path()
        if not portal_path:
            _notify(
                "资源索引与本地备份",
                "还没有可打开的文件备份入口",
                "请先选择群聊并运行一次“立即更新资源索引”。",
            )
            return
        subprocess.run(["open", "-R", portal_path])

    def _start_resource_backup_consumer(self, *, manual):
        threading.Thread(
            target=self._run_resource_backup_consumer,
            kwargs={"manual": manual},
            daemon=True,
        ).start()

    def _run_resource_backup_consumer(self, *, manual):
        if not self._resource_backup_lock.acquire(blocking=False):
            if manual:
                _notify("资源索引与本地备份", "正在运行", "当前更新尚未结束。")
            return
        try:
            config = load_config()
            if not manual and not config.get("resource_backup_enabled", False):
                result = {
                    "capture": {
                        "state": "disabled",
                        "scan": {"state": "disabled"},
                        "resolve": {"state": "skipped"},
                    },
                    "backup": {"state": "not_run"},
                }
                return
            capture = self._resource_capture_service(source=True, config=config)
            consent_epoch = int(getattr(
                self,
                "_resource_file_resolution_session_epoch",
                0,
            ))
            resolve_files = bool(
                self._resource_file_resolution_session_enabled
            )
            capture_run_kwargs = dict(
                resolve_limit=50,
                resolve_files=resolve_files,
            )
            if resolve_files:
                capture_run_kwargs["consent_check"] = (
                    lambda: bool(self._resource_file_resolution_session_enabled)
                    and int(getattr(
                        self,
                        "_resource_file_resolution_session_epoch",
                        0,
                    )) == consent_epoch
                )
            capture_result = capture.run(**capture_run_kwargs)
            if capture_result.get("state") == "worker_busy":
                backup_result = {"state": "not_run_worker_busy"}
            else:
                backup_result = MountedResourceBackup.from_config(
                    config,
                    capture=capture,
                ).run()
            result = {"capture": capture_result, "backup": backup_result}
            print(
                "[resource-backup] "
                f"capture={capture_result.get('state')} "
                f"backup={backup_result.get('state')} "
                f"resolve={capture_result.get('resolve', {}).get('state')}"
            )
        except Exception as exc:
            print(f"[resource-backup] worker failed: {type(exc).__name__}")
            result = {
                "capture": {"state": "failed"},
                "backup": {"state": "failed", "error_code": type(exc).__name__},
            }
        finally:
            self._resource_backup_lock.release()
        self._run_on_main(self._finish_resource_backup_run, result, manual)

    def _finish_resource_backup_run(self, result, manual):
        self.config = load_config()
        self._rebuild_resource_backup_menu()
        if not manual:
            return
        capture = result.get("capture") or {}
        backup = result.get("backup") or {}
        scan = capture.get("scan") or {}
        resolve = capture.get("resolve") or {}
        capture_state = str(capture.get("state") or "unknown")
        projection_state = str(
            (backup.get("obsidian") or {}).get("state") or "unknown"
        )
        handoff_state = str(backup.get("state") or "unknown")
        outcome = evaluate_resource_backup_outcome(capture, backup)
        coverage = outcome.get("coverage") or {}
        backlog = int(coverage.get("non_delivered_occurrences") or 0)
        if not outcome["operational_success"]:
            title = "本轮未完成"
        elif outcome["coverage_complete"]:
            title = "更新完成"
        elif int(backup.get("copied") or 0) or int(resolve.get("ready_local") or 0):
            title = "附件备份有进展"
        else:
            title = "索引已更新，附件仍待补齐"
        _notify(
            "资源索引与本地备份",
            title,
            f"新增链接 {int(scan.get('captured_links') or 0)} · "
            f"新增文件 {int(scan.get('captured_files') or 0)} · "
            f"本地文件 {int(resolve.get('ready_local') or 0)} · "
            f"已备份 {int(coverage.get('delivered_objects') or 0)} 个文件 / "
            f"{int(coverage.get('delivered_occurrences') or 0)} 次出现 · "
            f"待补齐 {backlog} 条 · "
            f"capture={capture_state} · projection={projection_state} · "
            f"handoff={handoff_state}",
        )

    def _request_link_backfill(self, _):
        self._delayed_run(self._show_link_backfill_dialog)

    def _show_link_backfill_dialog(self):
        self._bring_to_front()
        try:
            clicked, value = self._input_dialog(
                "补历史链接",
                "输入起始日期 YYYY-MM-DD，或输入 all 扫描本地仍可读的全部历史。先生成 plan，确认后才写入；不会读取附件 bytes。",
                default_text=datetime.now().strftime("%Y-%m-01"),
                ok="生成计划",
                width=420,
            )
        finally:
            self._release_front()
        if not clicked:
            return
        value = value.strip().lower()
        if value == "all":
            from_timestamp = 0
            scope = "all"
        else:
            try:
                from_timestamp = int(datetime.strptime(value, "%Y-%m-%d").timestamp())
            except ValueError:
                _notify("资源索引与本地备份", "日期格式错误", "请输入 YYYY-MM-DD 或 all。")
                return
            scope = value
        self._begin_task("补历史链接计划")
        threading.Thread(
            target=self._plan_link_backfill,
            args=(from_timestamp, scope),
            daemon=True,
        ).start()

    def _plan_link_backfill(self, from_timestamp, scope):
        if not self._resource_backup_lock.acquire(blocking=False):
            self._run_on_main(
                self._confirm_link_backfill_plan,
                from_timestamp,
                scope,
                {"state": "busy", "source_complete": False},
            )
            return
        try:
            if not self.db:
                result = {"state": "source_unavailable", "source_complete": False}
            else:
                result = self._resource_capture_service(
                    source=True,
                    config=load_config(),
                ).backfill_links(from_timestamp, apply=False)
        except Exception as exc:
            result = {
                "state": "failed",
                "source_complete": False,
                "error_code": type(exc).__name__,
            }
        finally:
            self._resource_backup_lock.release()
        self._run_on_main(
            self._confirm_link_backfill_plan,
            from_timestamp,
            scope,
            result,
        )

    def _confirm_link_backfill_plan(self, from_timestamp, scope, result):
        self._finish_task()
        if not result.get("source_complete"):
            _notify(
                "资源索引与本地备份",
                "历史链接计划未完成",
                f"state={result.get('state') or 'failed'} · error={result.get('error_code') or 'none'}；没有写入。",
            )
            return
        run_id = str(result.get("run_id") or "")
        candidate_digest = str(result.get("candidate_digest") or "")
        if not run_id or not candidate_digest:
            _notify(
                "资源索引与本地备份",
                "历史链接计划未完成",
                "plan identity 缺失；没有写入。",
            )
            return
        self._bring_to_front()
        try:
            confirmed = self._confirm_dialog(
                "确认补历史链接？",
                f"范围: {scope}\n已完整扫描 {int(result.get('scanned') or 0)} 条 source rows\n发现 {int(result.get('discovered_links') or 0)} 个 exact link occurrences\nplan: {run_id}\ndigest: {candidate_digest[:16]}…\n\n确认后只消费这份 staged plan，写入本地 ledger 并重建 Obsidian/挂载目录索引；不会重新扫描 source，也不会读取附件 bytes。",
                ok="写入",
            )
        finally:
            self._release_front()
        if not confirmed:
            return
        self._begin_task("补历史链接")
        threading.Thread(
            target=self._apply_link_backfill,
            args=(from_timestamp, run_id),
            daemon=True,
        ).start()

    def _apply_link_backfill(self, from_timestamp, run_id):
        if not self._resource_backup_lock.acquire(blocking=False):
            self._run_on_main(
                self._finish_link_backfill,
                {"state": "busy", "source_complete": False},
                {"state": "not_run"},
            )
            return
        try:
            config = load_config()
            capture = self._resource_capture_service(source=False, config=config)
            result = capture.backfill_links(
                from_timestamp,
                apply=True,
                run_id=run_id,
            )
            if result.get("source_complete"):
                projection = MountedResourceBackup.from_config(
                    config,
                    capture=capture,
                ).run()
            else:
                projection = {"state": "not_run"}
        except Exception as exc:
            result = {
                "state": "failed",
                "source_complete": False,
                "error_code": type(exc).__name__,
            }
            projection = {"state": "not_run"}
        finally:
            self._resource_backup_lock.release()
        self._run_on_main(self._finish_link_backfill, result, projection)

    def _finish_link_backfill(self, result, projection):
        self._finish_task()
        self.config = load_config()
        self._rebuild_resource_backup_menu()
        outcome = evaluate_link_backfill_outcome(result, projection)
        if outcome["completed"]:
            _notify(
                "资源索引与本地备份",
                "历史链接已补齐",
                f"发现 {int(result.get('discovered_links') or 0)} · "
                f"新增 {int(result.get('inserted_links') or 0)} · "
                f"projection={outcome['projection_state']} · "
                f"handoff={outcome['handoff_state']}",
            )
        else:
            _notify(
                "资源索引与本地备份",
                "历史链接未写完",
                f"state={outcome['state']} · error={result.get('error_code') or 'none'}",
            )

    def _select_resource_backup_chats(self, _):
        self._delayed_run(self._show_resource_backup_chat_dialog)

    def _show_resource_backup_chat_dialog(self):
        choices = resource_backup_chat_candidates(self.config)
        if not choices:
            _notify("资源索引与本地备份", "没有候选群聊", "请先在关注推送中选择群聊。")
            return
        selected = {
            chat.get("username"): chat
            for chat in self.config.get("resource_backup_selected_chats") or []
        }
        lines = [f"{index}. {chat['alias']}" for index, chat in enumerate(choices, 1)]
        self._bring_to_front()
        try:
            clicked, text = self._input_dialog(
                "选择资源备份群聊",
                "只有同时处于关注推送且在这里显式选中的群才会进入索引/备份。多个编号用逗号分隔。\n\n"
                + "\n".join(lines),
                default_text=",".join(
                    str(index)
                    for index, chat in enumerate(choices, 1)
                    if chat["username"] in selected
                ),
                ok="保存",
                width=520,
            )
        finally:
            self._release_front()
        if not clicked:
            return
        try:
            indexes = self._parse_monitor_chat_selection(text, len(choices)) if text.strip() else []
        except ValueError:
            _notify("资源索引与本地备份", "输入错误", "请输入列表里的数字编号。")
            return
        now = int(time.time())
        selected_chats = [
            {
                **choices[index - 1],
                "selected_since": int(
                    (selected.get(choices[index - 1]["username"]) or {}).get("selected_since")
                    or now
                ),
                "selection_id": str(
                    (selected.get(choices[index - 1]["username"]) or {}).get(
                        "selection_id"
                    )
                    or uuid.uuid4()
                ),
            }
            for index in indexes
        ]
        try:
            updated, _initialized = update_resource_backup_selection(
                self.config,
                selected_chats,
            )
            self.config = updated
        except ResourceCaptureError as exc:
            if exc.code == "capture_worker_busy":
                _notify(
                    "资源索引与本地备份",
                    "当前更新尚未结束",
                    "群聊选择没有改变，请稍后再试。",
                )
                return
            raise
        self._rebuild_resource_backup_menu()
        _notify("资源索引与本地备份", "群聊选择已保存", f"当前选择 {len(indexes)} 个群。")

    # ── Selected-chat Google Drive file sync ─────────────────

    def _drive_sync_service(self, *, remote=False, config=None):
        config = dict(config or self.config)
        oauth = GoogleDriveOAuth()
        return GoogleDriveFileSync(
            config,
            source=self.db,
            drive_client=GoogleDriveClient(oauth) if remote else None,
            oauth=oauth,
            notifier=lambda title, message: _notify(
                "Google Drive 群文件备份", title, message
            ),
            control_state_func=load_config,
        )

    def _drive_sync_status(self):
        try:
            return GoogleDriveFileSync.inspect_status(
                self.config,
                oauth=GoogleDriveOAuth(),
            )
        except Exception as exc:
            print(f"[drive-sync] local status unavailable: {type(exc).__name__}")
            return {
                "state": "unavailable",
                "auth": "auth_required",
                "selected_chat_count": len(selected_drive_sync_chats(self.config)),
                "queue_counts": {},
                "root_state": "unknown",
            }

    def _build_drive_sync_menu(self):
        drive = rumps.MenuItem("☁️ Google Drive 群文件备份")
        status = self._drive_sync_status()
        state_labels = {
            "disabled": "已关闭",
            "paused": "已暂停",
            "enabled": "已开启",
            "unavailable": "本地状态不可用",
        }
        state = state_labels.get(status.get("state"), str(status.get("state") or "未知"))
        auth = "已授权" if status.get("auth") == "connected" else "需要授权"
        counts = status.get("queue_counts") or {}
        pending = sum(
            int(count or 0)
            for item_state, count in counts.items()
            if item_state != "complete"
        )
        drive.add(rumps.MenuItem(f"状态: {state} · {auth}"))
        drive.add(rumps.MenuItem(
            f"群聊: {int(status.get('selected_chat_count') or 0)} · 待处理: {pending}"
        ))
        drive.add(rumps.MenuItem(f"Root: {status.get('root_state') or 'unknown'}"))
        drive.add(rumps.separator)

        enabled = bool(self.config.get("google_drive_file_sync_enabled", False))
        paused = bool(self.config.get("google_drive_file_sync_paused", False))
        drive.add(rumps.MenuItem(
            "⏹ 关闭同步" if enabled else "▶️ 开启同步",
            callback=self._toggle_drive_sync,
        ))
        if enabled:
            drive.add(rumps.MenuItem(
                "▶️ 恢复同步" if paused else "⏸ 暂停同步",
                callback=self._toggle_drive_sync_pause,
            ))
        else:
            drive.add(rumps.MenuItem("⏸ 暂停（当前已关闭）"))
        drive.add(rumps.MenuItem("🔄 立即同步一次", callback=self._sync_drive_now))
        drive.add(rumps.MenuItem("🎯 选择群聊...", callback=self._select_drive_sync_chats))
        drive.add(rumps.separator)
        drive.add(rumps.MenuItem("📂 打开 Drive 根目录", callback=self._open_drive_sync_root))
        drive.add(rumps.MenuItem("🔐 重新授权...", callback=self._reauthorize_drive_sync))
        return drive

    def _rebuild_drive_sync_menu(self):
        if "☁️ Google Drive 群文件备份" in self.menu:
            del self.menu["☁️ Google Drive 群文件备份"]
        anchor = (
            "🔗 资源索引与本地备份"
            if "🔗 资源索引与本地备份" in self.menu
            else "🔔 关注推送"
            if "🔔 关注推送" in self.menu
            else "🔌 MCP 服务"
        )
        self.menu.insert_after(anchor, self._build_drive_sync_menu())

    def _configure_drive_sync_timer(self):
        if self._drive_sync_timer:
            try:
                self._drive_sync_timer.stop()
            except Exception:
                pass
            self._drive_sync_timer = None
        if not self.config.get("google_drive_file_sync_enabled", False):
            return
        if self.config.get("google_drive_file_sync_paused", False):
            return
        interval = max(
            60,
            int(self.config.get("google_drive_file_sync_interval_seconds", 300)),
        )
        self._drive_sync_timer = rumps.Timer(self._on_drive_sync_timer, interval)
        self._drive_sync_timer.start()
        print(f"[drive-sync] timer started: every {interval} seconds")

    def _on_drive_sync_timer(self, _):
        if not self.db:
            return
        self._start_drive_sync_consumer(manual=False)

    def _toggle_drive_sync(self, _):
        if self.config.get("google_drive_file_sync_enabled", False):
            self._update_config(patch={"google_drive_file_sync_enabled": False})
            self._configure_drive_sync_timer()
            self._rebuild_drive_sync_menu()
            _notify(
                "Google Drive 群文件备份",
                "已关闭",
                "不会再开始新的扫描或上传；queue、local CAS 和远端文件均已保留。",
            )
            return

        self._update_config(patch={
            "google_drive_file_sync_enabled": True,
            "google_drive_file_sync_paused": False,
        })
        self._drive_sync_service().initialize_selected_chat_cursors()
        self._configure_drive_sync_timer()
        self._rebuild_drive_sync_menu()
        _notify(
            "Google Drive 群文件备份",
            "已开启",
            "从当前时间开始。授权、选择群聊和历史 backfill 仍是独立动作。",
        )

    def _toggle_drive_sync_pause(self, _):
        if not self.config.get("google_drive_file_sync_enabled", False):
            return
        paused = not self.config.get("google_drive_file_sync_paused", False)
        self._update_config(patch={"google_drive_file_sync_paused": paused})
        self._configure_drive_sync_timer()
        self._rebuild_drive_sync_menu()
        _notify(
            "Google Drive 群文件备份",
            "已暂停" if paused else "已恢复",
            "不会开始新的扫描或上传。" if paused else "durable queue 会在下一轮继续。",
        )

    def _sync_drive_now(self, _):
        self._start_drive_sync_consumer(manual=True)

    def _start_drive_sync_consumer(self, *, manual):
        threading.Thread(
            target=self._run_drive_sync_consumer,
            kwargs={"manual": manual},
            daemon=True,
        ).start()

    def _run_drive_sync_consumer(self, *, manual):
        if not self._drive_sync_lock.acquire(blocking=False):
            if manual:
                _notify(
                    "Google Drive 群文件备份", "正在同步", "当前 one-shot worker 尚未结束。"
                )
            return
        try:
            if not self.db:
                result = {"state": "source_unavailable", "error_code": "source_unavailable"}
            else:
                config = load_config()
                result = self._drive_sync_service(
                    remote=True,
                    config=config,
                ).run()
            print(
                "[drive-sync] "
                f"state={result.get('state')} scanned={result.get('scanned', 0)} "
                f"queued={result.get('queued', 0)} uploaded={result.get('uploaded', 0)} "
                f"shortcuts={result.get('shortcuts', 0)} error={result.get('error_code', '')}"
            )
            self._run_on_main(self._finish_drive_sync_run, result, manual)
        except Exception as exc:
            print(f"[drive-sync] worker failed: {type(exc).__name__}")
            result = {"state": "failed", "error_code": type(exc).__name__}
            self._run_on_main(self._finish_drive_sync_run, result, manual)
        finally:
            self._drive_sync_lock.release()

    def _finish_drive_sync_run(self, result, manual):
        self.config = load_config()
        self._rebuild_drive_sync_menu()
        if not manual:
            return
        state = result.get("state") or "unknown"
        if state == "healthy":
            _notify(
                "Google Drive 群文件备份",
                "同步完成",
                f"上传 {int(result.get('uploaded', 0))} 个 object，"
                f"创建 {int(result.get('shortcuts', 0))} 个 shortcut。",
            )
        else:
            _notify(
                "Google Drive 群文件备份",
                "本轮未完成" if state not in {"disabled", "paused"} else "没有运行",
                f"state={state} · error={result.get('error_code') or 'none'}",
            )

    def _select_drive_sync_chats(self, _):
        self._delayed_run(self._show_drive_sync_chat_dialog)

    def _show_drive_sync_chat_dialog(self):
        self._bring_to_front()
        try:
            if not self.db:
                _notify(
                    "Google Drive 群文件备份",
                    "还没初始化",
                    "请等微信数据加载完成后再选择群聊。",
                )
                return
            groups = self.db.get_recent_sessions(limit=200)
            groups = [group for group in groups if group.get("is_group")]
            if not groups:
                groups = self.db.get_groups(include_unnamed=True)
            if not groups:
                _notify(
                    "Google Drive 群文件备份",
                    "没有找到群聊",
                    "请确认微信已登录并有群聊记录。",
                )
                return
            groups = groups[:80]
            current = {
                chat["username"]: chat.get("alias") or ""
                for chat in selected_drive_sync_chats(self.config)
            }
            lines = [f"{index}. {group['name']}" for index, group in enumerate(groups, 1)]
            clicked, text = self._input_dialog(
                "选择 Google Drive 文件备份群聊",
                "输入群聊编号；多个群用逗号分隔。留空会清除选择，但不会删除 queue、CAS 或远端文件。\n\n"
                + "\n".join(lines),
                default_text=",".join(
                    str(index + 1)
                    for index, group in enumerate(groups)
                    if group["username"] in current
                ),
                ok="保存",
                width=540,
            )
            if not clicked:
                return
            try:
                selected = (
                    self._parse_monitor_chat_selection(text, len(groups))
                    if text.strip()
                    else []
                )
            except ValueError:
                _notify(
                    "Google Drive 群文件备份",
                    "输入错误",
                    "请输入列表里的数字编号，多个编号用逗号分隔。",
                )
                return
            selected_chats = []
            for index in selected:
                group = groups[index - 1]
                alias = current.get(group["username"])
                if not alias:
                    candidate = str(group.get("name") or "").strip()
                    alias = "" if candidate.endswith("@chatroom") else candidate
                selected_chats.append({
                    "username": group["username"],
                    "alias": alias,
                })
            self._update_config(patch={
                "google_drive_file_sync_selected_chats": selected_chats,
            })
            if self.config.get("google_drive_file_sync_enabled", False):
                self._drive_sync_service().initialize_selected_chat_cursors()
            self._rebuild_drive_sync_menu()
            _notify(
                "Google Drive 群文件备份",
                "群聊选择已保存",
                f"当前选择 {len(selected_chats)} 个群；这一步没有启用同步或上传文件。",
            )
        finally:
            self._release_front()

    def _open_drive_sync_root(self, _):
        link = str(self._drive_sync_status().get("root_web_view_link") or "")
        if not link:
            _notify(
                "Google Drive 群文件备份",
                "Root 尚未建立",
                "完成授权并至少成功运行一次后才能打开。",
            )
            return
        subprocess.run(["open", link])

    def _reauthorize_drive_sync(self, _):
        self._delayed_run(self._show_drive_sync_auth_dialog)

    def _show_drive_sync_auth_dialog(self):
        self._bring_to_front()
        try:
            clicked, path = self._input_dialog(
                "Google Drive OAuth 授权",
                "输入你自己的 Installed desktop app OAuth client JSON 路径。确认后会复制到 private runtime location，并打开 system browser；不会启用 sync 或选择群聊。",
                default_text="",
                ok="开始授权",
                width=560,
            )
            if not clicked or not path.strip():
                return
            threading.Thread(
                target=self._run_drive_sync_auth,
                args=(path.strip(),),
                daemon=True,
            ).start()
        finally:
            self._release_front()

    def _run_drive_sync_auth(self, client_path):
        try:
            status = GoogleDriveOAuth().authorize(client_path)
            _notify(
                "Google Drive 群文件备份",
                "授权完成",
                "refresh token 已存入 macOS Keychain；sync 和群聊选择没有被改变。",
            )
            print(f"[drive-sync] auth state={status.get('state')}")
        except GoogleDriveAuthError as exc:
            _notify(
                "Google Drive 群文件备份",
                "授权失败",
                f"error={exc.code}",
            )
        finally:
            self._run_on_main(self._rebuild_drive_sync_menu)

    # ── Topic monitor menu ────────────────────────────────────

    def _build_monitor_menu(self):
        """Build macOS notification monitor submenu."""
        monitor = rumps.MenuItem("🔔 关注推送")

        enabled = self.config.get("monitor_enabled", False)
        interval = self.config.get("monitor_interval_minutes", 3)
        chat_name = self._monitor_chat_label()
        provider = self.config.get("monitor_ai_provider") or self.config.get("ai_provider", "qwen")
        provider_name = dict(AI_PROVIDERS).get(provider, provider)
        topic = self.config.get("monitor_topic", "").strip()
        topic_label = topic[:24] + "..." if len(topic) > 24 else topic

        status = "已开启" if enabled else "已暂停"
        monitor.add(rumps.MenuItem(f"状态: {status} · 每 {interval} 分钟"))
        monitor.add(rumps.MenuItem(f"群聊: {chat_name}"))
        monitor.add(rumps.MenuItem(f"AI: {provider_name}"))
        monitor.add(rumps.MenuItem(f"关注: {topic_label or '未设置'}"))
        monitor.add(rumps.separator)

        toggle_label = "⏸ 暂停监控" if enabled else "▶️ 启用监控"
        monitor.add(rumps.MenuItem(toggle_label, callback=self._toggle_monitor))
        monitor.add(rumps.MenuItem("🎯 选择监控群聊...", callback=self._set_monitor_chat))
        monitor.add(rumps.MenuItem("📝 设置关注描述...", callback=self._set_monitor_topic))
        monitor.add(rumps.MenuItem("⏱ 设置检查间隔...", callback=self._set_monitor_interval))
        monitor.add(rumps.separator)
        notifications_enabled = self.config.get("background_notifications_enabled", True)
        notification_label = "🔔 后台通知：开" if notifications_enabled else "🔕 后台通知：关"
        monitor.add(rumps.MenuItem(
            notification_label,
            callback=self._toggle_background_notifications,
        ))
        monitor.add(rumps.MenuItem("🧪 测试检查一次", callback=self._test_monitor_once))
        monitor.add(rumps.MenuItem("🧪 测试系统通知", callback=self._test_monitor_notification))
        checkin_label = "✅ 心跳通知：开" if self.config.get("monitor_notify_checkins") else "☑️ 心跳通知：关"
        monitor.add(rumps.MenuItem(checkin_label, callback=self._toggle_monitor_checkins))
        monitor.add(rumps.MenuItem("📁 打开命中记录目录", callback=self._open_monitor_hits_dir))
        monitor.add(rumps.MenuItem("📂 设置 Obsidian 仓库位置...", callback=self._set_monitor_obsidian_root))
        monitor.add(rumps.MenuItem("🧭 设置 Obsidian 子目录...", callback=self._set_monitor_obsidian_subdir))
        monitor.add(rumps.MenuItem("🗂 打开知识库目录", callback=self._open_monitor_knowledge_dir))
        monitor.add(rumps.MenuItem("🧹 整理去重 + 重导出...", callback=self._run_monitor_maintenance))
        return monitor

    def _configure_monitor_timer(self):
        """Start/stop the monitor timer based on config."""
        if self._monitor_timer:
            try:
                self._monitor_timer.stop()
            except Exception:
                pass
            self._monitor_timer = None

        if not self.config.get("monitor_enabled", False):
            self._stop_monitor_wakeup_timer()
            return
        if not self.config.get("monitor_topic", "").strip():
            self._stop_monitor_wakeup_timer()
            return
        if not self._monitor_chats():
            self._stop_monitor_wakeup_timer()
            return

        try:
            self._initialize_monitor_states_if_needed()
        except MonitorStateError as exc:
            print(f"[monitor] {exc.code}")
        except Exception:
            traceback.print_exc()

        interval_seconds = max(1, self.config.get("monitor_interval_minutes", 3)) * 60
        self._monitor_timer = rumps.Timer(self._on_monitor_timer, interval_seconds)
        self._monitor_timer.start()
        self._start_monitor_wakeup_timer()
        print(f"[monitor] 已启动，每 {interval_seconds // 60} 分钟检查一次")

    def _start_monitor_wakeup_timer(self):
        """Poll cheaply so a sleeping Mac can catch up soon after wake."""
        if self._monitor_wakeup_timer:
            return
        self._monitor_wakeup_timer = rumps.Timer(self._on_monitor_wakeup_timer, 60)
        self._monitor_wakeup_timer.start()

    def _stop_monitor_wakeup_timer(self):
        if not self._monitor_wakeup_timer:
            return
        try:
            self._monitor_wakeup_timer.stop()
        except Exception:
            pass
        self._monitor_wakeup_timer = None

    def _on_monitor_timer(self, _):
        """Timer callback: dispatch monitor work to a background thread."""
        if not self.config.get("monitor_enabled", False) or not self.db:
            return
        self._monitor_last_dispatch_ts = time.time()
        threading.Thread(
            target=self._run_monitor_check,
            kwargs={"manual": False, "dry_run": False},
            daemon=True,
        ).start()

    def _on_monitor_wakeup_timer(self, _):
        """Run a catch-up check after sleep if the normal timer missed its slot."""
        if not self.config.get("monitor_enabled", False) or not self.db:
            return
        interval_seconds = max(1, self.config.get("monitor_interval_minutes", 3)) * 60
        last_checked = self._last_monitor_checked_ts()
        now = time.time()
        if self._monitor_lock.locked():
            return
        if self._monitor_last_dispatch_ts and now - self._monitor_last_dispatch_ts < 60:
            return
        if last_checked and now - last_checked < interval_seconds:
            return
        self._monitor_last_dispatch_ts = now
        print("[monitor] wake/checkpoint catch-up")
        threading.Thread(
            target=self._run_monitor_check,
            kwargs={"manual": False, "dry_run": False},
            daemon=True,
        ).start()

    def _last_monitor_checked_ts(self):
        values = []
        for chat in self._monitor_chats():
            try:
                state = load_state(state_file_for_chat(chat["username"]))
            except MonitorStateError as exc:
                print(f"[monitor] {exc.code}")
                return 0
            try:
                values.append(float(state.get("last_checked_ts") or 0))
            except (TypeError, ValueError):
                pass
        return min(values) if values else 0

    def _toggle_monitor(self, _):
        current = self.config.get("monitor_enabled", False)
        if current:
            self._update_config(patch={"monitor_enabled": False})
            self._configure_monitor_timer()
            _notify("关注推送", "已暂停", "后台关注推送已暂停")
            self._rebuild_monitor_menu()
            return

        if not self.config.get("monitor_topic", "").strip():
            _notify("关注推送", "先设置关注描述", "写下你想盯什么内容后再开启")
            self._delayed_run(self._show_monitor_topic_dialog, True)
            return
        if not self._monitor_chats():
            _notify("关注推送", "先选择群聊", "选择要后台监控的微信群后再开启")
            self._delayed_run(self._show_monitor_chat_dialog)
            return

        try:
            self._initialize_monitor_states_if_needed()
        except MonitorStateError as exc:
            _notify("关注推送", "监控状态不可用", exc.code)
            return
        self._update_config(patch={"monitor_enabled": True})
        self._configure_monitor_timer()
        _notify("关注推送", "已开启", "从当前时间开始，只检查新增消息")
        self._rebuild_monitor_menu()

    def _set_monitor_topic(self, _):
        self._delayed_run(self._show_monitor_topic_dialog, False)

    def _set_monitor_chat(self, _):
        self._delayed_run(self._show_monitor_chat_dialog)

    def _monitor_chats(self):
        return active_monitor_chats(self.config)

    def _monitor_chat_label(self):
        chats = self._monitor_chats()
        if not chats:
            return "未选择"
        if len(chats) == 1:
            return chats[0]["name"]
        names = "、".join(chat["name"] for chat in chats[:3])
        if len(chats) > 3:
            names += f" 等 {len(chats)} 个群"
        else:
            names += f"（{len(chats)} 个群）"
        return names

    def _reset_monitor_states_to_now(self):
        chats = self._monitor_chats()
        if not chats:
            reset_state_to_now()
            return
        for chat in chats:
            reset_state_to_now(state_file_for_chat(chat["username"]))

    def _initialize_monitor_states_if_needed(self):
        chats = self._monitor_chats()
        if not chats:
            initialize_state_if_needed()
            return
        for chat in chats:
            initialize_state_if_needed(state_file_for_chat(chat["username"]))

    @staticmethod
    def _parse_monitor_chat_selection(text, max_count):
        selected = []
        seen = set()
        tokens = re.split(r"[\s,，、]+", text.strip())
        for token in tokens:
            if not token:
                continue
            try:
                idx = int(token)
            except ValueError:
                raise ValueError("请输入数字编号，多个编号用逗号分隔")
            if idx < 1 or idx > max_count:
                raise ValueError("编号超出列表范围")
            if idx not in seen:
                seen.add(idx)
                selected.append(idx)
        return selected

    def _show_monitor_chat_dialog(self):
        self._bring_to_front()
        try:
            if not self.db:
                _notify("关注推送", "还没初始化", "请等微信数据加载完成后再选择群聊")
                return

            groups = self.db.get_recent_sessions(limit=200)
            groups = [g for g in groups if g.get("is_group")]
            if not groups:
                groups = self.db.get_groups(include_unnamed=True)
            if not groups:
                _notify("关注推送", "没有找到群聊", "请确认微信已登录并有群聊记录")
                return

            lines = []
            for idx, group in enumerate(groups[:80], 1):
                lines.append(f"{idx}. {group['name']}")
            extra = ""
            if len(groups) > 80:
                extra = f"\n\n只显示最近 80 个群聊；可先在微信里打开目标群让它变成最近会话。"

            clicked, text = self._input_dialog(
                "选择监控群聊",
                "输入要后台监控的群聊编号；多个群用逗号分隔，例如 1,3,5。\n\n"
                + "\n".join(lines) + extra,
                default_text=",".join(
                    str(i + 1)
                    for i, group in enumerate(groups[:80])
                    if group["username"] in {c["username"] for c in self._monitor_chats()}
                ) or "1",
                ok="保存",
                width=520,
            )
            if not clicked:
                return
            try:
                selected = self._parse_monitor_chat_selection(text, min(len(groups), 80))
            except ValueError:
                _notify("关注推送", "输入错误", "请输入列表里的数字编号，多个编号用逗号分隔")
                return
            if not selected:
                _notify("关注推送", "未选择群聊", "请输入至少一个群聊编号")
                return

            selected_groups = [groups[idx - 1] for idx in selected]
            first = selected_groups[0]
            def mutate(config):
                updated = merge_monitor_chat_preferences(config, selected_groups)
                updated["monitor_chats"] = [
                    {"username": group["username"], "name": group["name"]}
                    for group in selected_groups
                ]
                updated["monitor_chat_username"] = first["username"]
                updated["monitor_chat_display_name"] = first["name"]
                return updated

            self._update_config(mutator=mutate)
            self._reset_monitor_states_to_now()
            self._configure_monitor_timer()
            _notify("关注推送", "监控群聊已更新", f"从现在开始监控：{self._monitor_chat_label()}")
            self._rebuild_monitor_menu()
        finally:
            self._release_front()

    def _show_monitor_topic_dialog(self, enable_after=False):
        self._bring_to_front()
        try:
            clicked, text = self._input_dialog(
                "设置关注描述",
                "描述你想被提醒的内容。\n例如：工作项目的新进展、AI 工具或模型的重要更新、生活安排中的时间提醒。",
                default_text=self.config.get("monitor_topic", ""),
                ok="保存",
                width=460,
            )
            if not clicked:
                return
            topic = text.strip()
            topic_changed = []

            def mutate(config):
                topic_changed.append(topic != config.get("monitor_topic", ""))
                config["monitor_topic"] = topic
                if enable_after and topic and active_monitor_chats(config):
                    config["monitor_enabled"] = True
                return config

            self._update_config(mutator=mutate)
            if any(topic_changed):
                self._reset_monitor_states_to_now()
            if enable_after and topic and self._monitor_chats():
                self._initialize_monitor_states_if_needed()
            self._configure_monitor_timer()
            if topic:
                if self._monitor_chats():
                    _notify("关注推送", "关注描述已保存", "已按新描述监控新增消息")
                else:
                    _notify("关注推送", "关注描述已保存", "选择群聊后才会开始监控")
                if enable_after and not self._monitor_chats():
                    _notify("关注推送", "还需要选择群聊", "请选择要后台监控的微信群")
                    self._delayed_run(self._show_monitor_chat_dialog)
            else:
                _notify("关注推送", "关注描述已清空", "设置描述前不会调用 AI 检查")
            self._rebuild_monitor_menu()
        finally:
            self._release_front()

    def _set_monitor_interval(self, _):
        self._delayed_run(self._show_monitor_interval_dialog)

    def _show_monitor_interval_dialog(self):
        self._bring_to_front()
        try:
            clicked, text = self._input_dialog(
                "设置检查间隔",
                "输入分钟数（1-1440）。建议这个高流量群先用 3 分钟。",
                default_text=str(self.config.get("monitor_interval_minutes", 3)),
                ok="保存",
                width=260,
            )
            if not clicked:
                return
            try:
                minutes = int(text.strip())
            except ValueError:
                _notify("关注推送", "输入错误", "请输入正整数分钟数")
                return
            if minutes < 1 or minutes > 1440:
                _notify("关注推送", "输入错误", "请输入 1-1440 之间的分钟数")
                return
            self._update_config(patch={"monitor_interval_minutes": minutes})
            self._configure_monitor_timer()
            _notify("关注推送", "检查间隔已更新", f"每 {minutes} 分钟检查一次")
            self._rebuild_monitor_menu()
        finally:
            self._release_front()

    def _test_monitor_once(self, _):
        if not self.db:
            _notify("关注推送", "还没初始化", "请等微信数据加载完成后再测试")
            return
        if not self.config.get("monitor_topic", "").strip():
            _notify("关注推送", "先设置关注描述", "测试前需要知道你想盯什么")
            self._delayed_run(self._show_monitor_topic_dialog, False)
            return
        if not self._monitor_chats():
            _notify("关注推送", "先选择群聊", "测试前需要知道要盯哪个微信群")
            self._delayed_run(self._show_monitor_chat_dialog)
            return
        threading.Thread(
            target=self._run_monitor_check,
            kwargs={"manual": True, "dry_run": True},
            daemon=True,
        ).start()

    def _test_monitor_notification(self, _):
        _notify("关注推送", "系统通知可用", "如果你看到这条，macOS 通知链路是通的")

    def _toggle_background_notifications(self, _):
        enabled = not bool(self.config.get("background_notifications_enabled", True))
        self._update_config(patch={"background_notifications_enabled": enabled})
        state = "已开启" if enabled else "已关闭"
        detail = (
            "后台命中、错误和 Daily Digest 可以显示 banner"
            if enabled
            else "监控与 Obsidian 写入继续运行，只静音自动 banner"
        )
        _notify("关注推送", f"后台通知{state}", detail)
        self._rebuild_monitor_menu()

    def _toggle_monitor_checkins(self, _):
        enabled = not bool(self.config.get("monitor_notify_checkins"))
        self._update_config(patch={"monitor_notify_checkins": enabled})
        state = "已开启" if enabled else "已关闭"
        detail = "后台检查即使未命中也会报平安" if enabled else "之后只在命中、写入或报错时提醒"
        _notify("关注推送", f"心跳通知{state}", detail)
        self._rebuild_monitor_menu()

    def _run_monitor_check(self, manual=False, dry_run=False):
        if not self._monitor_lock.acquire(blocking=False):
            if manual:
                _notify("关注推送", "正在检查", "上一轮检查还没结束")
            return

        try:
            chats = self._monitor_chats()
            if not chats:
                raise MonitorConfigError("监控群聊未配置")
            if self.db and hasattr(self.db, "refresh_cache_view"):
                self.db.refresh_cache_view()
                print("[monitor] refreshed WeChat DB cache view")
            had_error = False
            for chat in chats:
                try:
                    chat_config = dict(self.config)
                    chat_config["monitor_chat_username"] = chat["username"]
                    chat_config["monitor_chat_display_name"] = chat["name"]
                    monitor = TopicMonitor(
                        self.db,
                        chat_config,
                        state_file=state_file_for_chat(chat["username"]),
                    )
                    result = monitor.check_once(dry_run=dry_run)
                    self._handle_monitor_result(result, manual=manual, dry_run=dry_run)
                except Exception as e:
                    had_error = True
                    print(
                        redact_urls_in_text(traceback.format_exc()),
                        file=sys.stderr,
                        end="",
                    )
                    self._handle_monitor_error(
                        f"{chat['name']}: 检查失败: {e}",
                        manual,
                    )
            if not had_error:
                self._monitor_last_error = ""
        except MonitorConfigError as e:
            self._handle_monitor_error(str(e), manual)
        except Exception as e:
            print(
                redact_urls_in_text(traceback.format_exc()),
                file=sys.stderr,
                end="",
            )
            self._handle_monitor_error(f"检查失败: {e}", manual)
        finally:
            self._monitor_lock.release()

    def _handle_monitor_result(self, result, manual=False, dry_run=False):
        status = result.get("status")
        decision = result.get("decision") or {}
        projection_warnings = result.get("knowledge_projection_warnings") or []
        if projection_warnings:
            summary = ", ".join(
                (
                    f"{warning.get('surface', 'unknown')}/"
                    f"{warning.get('error_type', 'OSError')}/"
                    f"errno={warning.get('errno')}"
                )
                for warning in projection_warnings
            )
            print(f"[monitor] projection warning; canonical event saved: {summary}")

        event_written = bool(result.get("knowledge_event_written"))
        event_reused = bool(result.get("knowledge_event_reused"))
        if not dry_run and (event_written or event_reused):
            digest_refresh = refresh_pending_daily_digests(self.config)
            for date_label in digest_refresh.get("written_dates") or []:
                print(
                    "[daily-digest] refreshed after canonical event: "
                    f"{date_label}"
                )
            if digest_refresh.get("state") == "deferred":
                print(
                    "[daily-digest] canonical-event refresh deferred: "
                    f"{digest_refresh.get('error_code', 'unknown')}"
                )
            if not dry_run and self.config.get("attachment_archive_enabled", False):
                self._start_attachment_archive_consumer()

        if status == "notified":
            hit_path = result.get("hit_path", "")
            knowledge_path = result.get("knowledge_path", "")
            should_notify = bool(result.get("notify_now", True))
            notification_enabled = self.config.get("background_notifications_enabled", True)
            notification_allowed = (
                notification_enabled
                and self.config.get("monitor_notify_writes", True)
            )
            if should_notify and notification_allowed:
                subtitle = result.get("title", "发现关注内容")
                message = result.get("summary", "有值得关注的新消息")
                if knowledge_path:
                    message = f"{message}\n\n已写入 Obsidian: {knowledge_path}"
                queue_item = result.get("review_queue_item") or {}
                if queue_item:
                    message = (
                        f"{message}\n\nReview queue: {queue_item.get('id')} "
                        f"· {queue_item.get('suggested_action')}"
                    )
                _notify(
                    "关注推送",
                    subtitle,
                    message,
                    data=notification_data_for_path(knowledge_path or hit_path),
                )
            queue_id = (result.get("review_queue_item") or {}).get("id", "")
            if should_notify and not notification_allowed:
                gate = "notification-muted"
            elif should_notify:
                gate = "notify"
            else:
                gate = f"digest-only/{result.get('review_priority', 'P3')}"
            print(f"[monitor] 命中[{gate}]: {hit_path} 知识库: {knowledge_path} review_queue: {queue_id}")
            return

        if status == "matched":
            relation = result.get("relation")
            prefix = f"测试命中/{relation}" if relation else "测试命中（不写记录）"
            _notify("关注推送", f"{prefix}: {result.get('title', '发现关注内容')}",
                    result.get("summary", "有值得关注的新消息"))
            return

        if not manual:
            print(f"[monitor] {status}: {result.get('message', '')}")
            if (
                self.config.get("background_notifications_enabled", True)
                and self.config.get("monitor_notify_checkins")
            ):
                messages = {
                    "initialized": ("已开始监控", "正式检查会从当前时间之后的新消息开始"),
                    "no_messages": ("后台检查完成", "没有新消息"),
                    "source_advanced_no_visible": ("后台检查完成", "已跳过系统或不可展示消息"),
                    "no_match": ("后台检查完成", f"检查了 {result.get('message_count', 0)} 条，未命中关注内容"),
                    "cooldown": ("命中但在冷却中", "同一主题短时间内不会重复提醒"),
                    "duplicate": ("重复内容", "知识库判断没有新线索，这次不推送"),
                    "ai_backoff": ("AI 暂时不可用", result.get("message", "稍后会自动重试")),
                }
                title, message = messages.get(status, ("后台检查完成", str(result)))
                _notify("关注推送", title, message)
            return

        messages = {
            "initialized": ("已开始监控", "正式检查会从当前时间之后的新消息开始"),
            "missing_topic": ("还没设置关注描述", "设置后才会调用 AI 检查"),
            "no_messages": ("没有新消息", "这次测试窗口里没有新增内容"),
            "source_advanced_no_visible": ("没有可展示消息", "源游标已安全推进"),
            "no_match": ("未命中", f"检查了 {result.get('message_count', 0)} 条，没有值得提醒的内容"),
            "cooldown": ("命中但在冷却中", "同一主题短时间内不会重复提醒"),
            "duplicate": ("重复内容，已静默记录", "知识库判断没有新线索，这次不推送"),
            "ai_backoff": ("AI 暂时不可用", result.get("message", "稍后会自动重试")),
        }
        title, message = messages.get(status, ("检查完成", str(result)))
        _notify("关注推送", title, message)

    def _start_attachment_archive_consumer(self):
        """Consume the attachment outbox after the canonical event committed."""
        threading.Thread(
            target=self._run_attachment_archive_consumer,
            daemon=True,
        ).start()

    def _run_attachment_archive_consumer(self):
        try:
            result = process_pending_from_config(dict(self.config))
            if result.get("processed"):
                print(
                    "[attachment-archive] "
                    f"processed={result['processed']} "
                    f"archived={result['archived']} failed={result['failed']}"
                )
        except Exception as exc:
            print(f"[attachment-archive] consumer failed: {type(exc).__name__}")

    def _handle_monitor_error(self, message, manual=False):
        safe_message = redact_urls_in_text(message)
        print(f"[monitor] monitor_runtime_error: {safe_message}")
        notifications_enabled = self.config.get("background_notifications_enabled", True)
        if manual or (notifications_enabled and safe_message != self._monitor_last_error):
            _notify("关注推送", "检查失败", safe_message[:180])
        self._monitor_last_error = safe_message

    def _configure_daily_digest_timer(self):
        if self._daily_digest_timer:
            try:
                self._daily_digest_timer.stop()
            except Exception:
                pass
            self._daily_digest_timer = None
        if not self.config.get("daily_digest_enabled", True):
            return
        self._daily_digest_timer = rumps.Timer(self._on_daily_digest_timer, 60)
        self._daily_digest_timer.start()
        print(
            "[daily-digest] 已启动，"
            f"{self.config.get('daily_digest_time', '21:30')} "
            f"{self.config.get('daily_digest_timezone', 'Asia/Shanghai')} 检查"
        )

    def _on_daily_digest_timer(self, _):
        if not self.config.get("daily_digest_enabled", True):
            return
        threading.Thread(target=self._run_daily_digest, daemon=True).start()

    def _run_daily_digest(self):
        if not self._daily_digest_lock.acquire(blocking=False):
            return
        try:
            due = should_run_daily_digest(DAILY_DIGEST_STATE_FILE, self.config)
            digest = write_daily_digest(self.config) if due else None
            repair = refresh_pending_daily_digests(self.config)
            if repair.get("state") == "deferred":
                raise RuntimeError(
                    "daily_digest_repair_deferred:"
                    + str(repair.get("error_code") or "unknown")
                )
            if digest is not None:
                mark_daily_digest_success(DAILY_DIGEST_STATE_FILE, self.config)
                print(
                    f"[daily-digest] 写入: {digest['path']} "
                    f"notes={digest['new_notes_count']} "
                    f"actions={digest.get('today_action_count', 0)} "
                    f"risk={digest.get('today_risk_count', 0)}"
                )
                if (
                    self.config.get("background_notifications_enabled", True)
                    and self.config.get("daily_digest_notify", True)
                ):
                    subtitle, message = notification_summary(digest)
                    _notify(
                        "关注推送 Daily Digest",
                        subtitle,
                        message,
                        data=notification_data_for_path(digest.get("path")),
                    )
        except Exception as e:
            traceback.print_exc()
            if self.config.get("background_notifications_enabled", True):
                _notify("关注推送 Daily Digest", "生成失败", str(e)[:180])
        finally:
            self._daily_digest_lock.release()

    def _open_monitor_hits_dir(self, _):
        os.makedirs(HITS_DIR, exist_ok=True)
        subprocess.run(["open", HITS_DIR])

    def _open_monitor_knowledge_dir(self, _):
        root = self.config.get("monitor_obsidian_root") or OBSIDIAN_ROOT
        subdir = self.config.get("monitor_obsidian_subdir")
        ensure_obsidian_vault(root, obsidian_subdir=subdir)
        subprocess.run(["open", os.path.join(os.path.expanduser(root), safe_obsidian_subdir(subdir))])

    def _set_monitor_obsidian_root(self, _):
        self._delayed_run(self._show_monitor_obsidian_root_dialog)

    def _show_monitor_obsidian_root_dialog(self):
        self._bring_to_front()
        try:
            current = self.config.get("monitor_obsidian_root") or OBSIDIAN_ROOT
            subdir = safe_obsidian_subdir(self.config.get("monitor_obsidian_subdir"))
            clicked, text = self._input_dialog(
                "设置 Obsidian 仓库位置",
                "粘贴你的 Obsidian 仓库（vault）路径。\n"
                f"命中笔记会写到该目录下的「{subdir}」子文件夹，\n"
                "在 Obsidian 里就能用 Bases、关系图谱自动整理。\n\n"
                "留空可恢复默认位置。",
                default_text=current,
                ok="保存",
                width=480,
            )
            if not clicked:
                return
            path = os.path.expanduser(text.strip())
            if not path:
                self._update_config(patch={"monitor_obsidian_root": OBSIDIAN_ROOT})
                ensure_obsidian_vault(
                    OBSIDIAN_ROOT,
                    obsidian_subdir=self.config.get("monitor_obsidian_subdir"),
                )
                _notify("关注推送", "已恢复默认知识库位置", OBSIDIAN_ROOT)
                self._rebuild_monitor_menu()
                return
            parent = os.path.dirname(path.rstrip("/")) or "/"
            if not os.path.isdir(path) and not os.path.isdir(parent):
                _notify("关注推送", "路径无效", "目录及其上层都不存在，请检查后重试")
                return
            try:
                os.makedirs(path, exist_ok=True)
            except OSError as e:
                _notify("关注推送", "无法创建目录", str(e)[:180])
                return
            self._update_config(patch={"monitor_obsidian_root": path})
            ensure_obsidian_vault(path, obsidian_subdir=self.config.get("monitor_obsidian_subdir"))
            _notify("关注推送", "知识库位置已更新", path)
            self._rebuild_monitor_menu()
        finally:
            self._release_front()

    def _set_monitor_obsidian_subdir(self, _):
        self._delayed_run(self._show_monitor_obsidian_subdir_dialog)

    def _show_monitor_obsidian_subdir_dialog(self):
        self._bring_to_front()
        try:
            current = safe_obsidian_subdir(self.config.get("monitor_obsidian_subdir"))
            clicked, text = self._input_dialog(
                "设置 Obsidian 子目录",
                "输入相对于 vault 根目录的子路径。\n"
                "建议：微信群聊/关注推送\n\n"
                "留空会恢复默认值。",
                default_text=current,
                ok="保存",
                width=420,
            )
            if not clicked:
                return
            subdir = safe_obsidian_subdir(text.strip() or "微信群聊/关注推送")
            self._update_config(patch={"monitor_obsidian_subdir": subdir})
            root = self.config.get("monitor_obsidian_root") or OBSIDIAN_ROOT
            ensure_obsidian_vault(root, obsidian_subdir=subdir)
            _notify("关注推送", "Obsidian 子目录已更新", subdir)
            self._rebuild_monitor_menu()
        finally:
            self._release_front()

    def _knowledge_ready(self):
        if not self.config.get("monitor_knowledge_enabled", False):
            return False
        db = self.config.get("monitor_knowledge_db") or KNOWLEDGE_DB
        return os.path.exists(os.path.expanduser(db))

    def _run_monitor_maintenance(self, _):
        if not self._knowledge_ready():
            _notify("关注推送", "暂无知识库", "命中并记录一些内容后再来整理")
            return
        threading.Thread(target=self._maintenance_scan, daemon=True).start()

    def _maintenance_scan(self):
        if not self._monitor_lock.acquire(blocking=False):
            _notify("关注推送", "正在等待", "当前监控检查还没结束，整理会稍后继续")
            if not self._monitor_lock.acquire(timeout=90):
                _notify("关注推送", "仍在忙", "当前检查耗时较久，稍后再点一次整理")
                return
        try:
            store = KnowledgeStore.from_config(self.config)
            plan = store.run_maintenance(dry_run=True)
        except Exception as e:
            traceback.print_exc()
            self._monitor_lock.release()
            _notify("关注推送", "整理失败", str(e)[:180])
            return
        self._monitor_lock.release()
        self._run_on_main(self._maintenance_confirm, plan)

    def _maintenance_confirm(self, plan):
        confirmed = False
        try:
            groups = plan.get("duplicate_groups", [])
            category_changes = plan.get("category_changes", [])
            total = plan.get("total_topics", 0)
            removed = plan.get("removed_count", 0)
            lines = []
            for g in groups[:8]:
                merged = "、".join(g.get("merged", []))
                lines.append(f"· {merged} → {g.get('primary', '')}")
            if len(groups) > 8:
                lines.append(f"…… 另有 {len(groups) - 8} 组")

            category_lines = []
            for change in category_changes[:10]:
                if change.get("reason") == "title":
                    category_lines.append(f"· 标题补时间：{change.get('title', '')}")
                else:
                    category_lines.append(
                        f"· {change.get('from', '')} → {change.get('to', '')}：{change.get('title', '')}"
                    )
            if len(category_changes) > 10:
                category_lines.append(f"…… 另有 {len(category_changes) - 10} 篇")

            if groups:
                head = (
                    f"发现 {plan['group_count']} 组疑似重复，"
                    f"{plan['merge_note_count']} 篇将合并成 {plan['group_count']} 篇。\n"
                    f"另有 {len(category_changes)} 篇会归并分类或补齐时间标题。\n"
                    f"随后把全部 {total - removed} 篇重新导出到当前 Obsidian 仓库"
                    f"（回填双链 / event_count）。\n\n"
                    + "\n".join(lines)
                    + ("\n\n分类归并：\n" + "\n".join(category_lines) if category_lines else "")
                    + "\n\n这会删除被合并的笔记，并移动归并分类后的笔记，确定整理吗？"
                )
                ok_label = "开始整理"
            elif category_changes:
                head = (
                    "没有发现重复主题。\n"
                    f"发现 {len(category_changes)} 篇笔记可以归并分类或补齐时间标题，"
                    f"随后把全部 {total} 篇重新导出到当前 Obsidian 仓库。\n\n"
                    + "\n".join(category_lines)
                    + "\n\n这会移动这些笔记到归并后的文件夹或带时间的新文件名，确定整理吗？"
                )
                ok_label = "整理并重导出"
            else:
                head = (
                    "没有发现重复主题。\n"
                    f"要把全部 {total} 篇笔记重新导出到当前 Obsidian 仓库吗？\n"
                    "（回填双链 / event_count，也可用于迁移到新仓库）"
                )
                ok_label = "重新导出"

            self._bring_to_front()
            try:
                confirmed = self._confirm_dialog("整理知识库", head, ok=ok_label)
            finally:
                self._release_front()
        except Exception:
            traceback.print_exc()
            confirmed = False

        if not confirmed:
            _notify("关注推送", "已取消整理", "没有任何改动")
            return
        threading.Thread(target=self._maintenance_execute, daemon=True).start()

    def _maintenance_execute(self):
        if not self._monitor_lock.acquire(blocking=False):
            _notify("关注推送", "正在等待", "当前监控检查还没结束，整理会稍后继续")
            if not self._monitor_lock.acquire(timeout=90):
                _notify("关注推送", "仍在忙", "当前检查耗时较久，稍后再点一次整理")
                return
        try:
            store = KnowledgeStore.from_config(self.config)
            result = store.run_maintenance(dry_run=False)
            _notify(
                "关注推送", "整理完成",
                f"合并 {result['removed_count']} 篇重复，"
                f"归并 {result.get('category_change_count', 0)} 篇分类，"
                f"重新导出 {result['reexport_count']} 篇",
            )
        except Exception as e:
            traceback.print_exc()
            _notify("关注推送", "整理失败", str(e)[:180])
        finally:
            self._monitor_lock.release()

    # ── MCP service menu ──────────────────────────────────────

    def _check_mcp_ready(self):
        """Check if MCP Server can start normally, return issue list (empty = ready)."""
        project_dir = os.path.dirname(os.path.abspath(__file__))
        venv_python = os.path.join(project_dir, ".venv", "bin", "python3")
        mcp_server = os.path.join(project_dir, "mcp_server.py")

        issues = []
        if not os.path.isfile(venv_python):
            issues.append("Python 虚拟环境未安装")
        if not os.path.isfile(mcp_server):
            issues.append("mcp_server.py 不存在")
        db_dir = self.config.get("db_dir", "")
        if not db_dir or not os.path.isdir(db_dir):
            issues.append("数据库目录未配置")
        if not get_cached_keys():
            issues.append("数据库密钥未提取")
        return issues

    def _is_mcp_running(self):
        """Detect if mcp_server.py process is running."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "mcp_server.py"],
                capture_output=True, text=True,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _get_mcp_config_snippet(self, client="claude_desktop"):
        """Generate MCP client configuration."""
        project_dir = os.path.dirname(os.path.abspath(__file__))
        venv_python = os.path.join(project_dir, ".venv", "bin", "python3")
        mcp_server = os.path.join(project_dir, "mcp_server.py")

        if client == "claude_desktop":
            return claude_desktop_config(venv_python, mcp_server)
        return claude_code_add_command(venv_python, mcp_server)

    def _build_mcp_menu(self):
        """Build MCP service submenu."""
        mcp = rumps.MenuItem("🔌 MCP 服务")

        # Status
        issues = self._check_mcp_ready()
        if issues:
            status_text = f"❌ {issues[0]}"
        elif self._is_mcp_running():
            status_text = "✅ 运行中"
        else:
            status_text = "✅ 就绪"
        mcp.add(rumps.MenuItem(f"状态: {status_text}"))

        mcp.add(rumps.separator)

        mcp.add(rumps.MenuItem(
            "📋 复制 Claude Desktop 配置",
            callback=self._copy_claude_desktop_config,
        ))
        mcp.add(rumps.MenuItem(
            "📋 复制 Claude Code 命令",
            callback=self._copy_claude_code_config,
        ))

        mcp.add(rumps.separator)

        mcp.add(rumps.MenuItem(
            "🧪 测试 MCP 服务",
            callback=self._test_mcp_server,
        ))

        return mcp

    def _rebuild_mcp_menu(self):
        """Rebuild MCP service menu."""
        if "🔌 MCP 服务" in self.menu:
            del self.menu["🔌 MCP 服务"]
        self.menu.insert_before("⚙️ 设置", self._build_mcp_menu())

    def _copy_claude_desktop_config(self, _):
        snippet = self._get_mcp_config_snippet("claude_desktop")
        process = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        process.communicate(snippet.encode("utf-8"))
        _notify("MCP 服务", "已复制到剪贴板",
                "粘贴到 claude_desktop_config.json 即可")

    def _copy_claude_code_config(self, _):
        snippet = self._get_mcp_config_snippet("claude_code")
        process = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        process.communicate(snippet.encode("utf-8"))
        _notify("MCP 服务", "已复制到剪贴板",
                "在终端粘贴执行即可添加 MCP 服务")

    def _test_mcp_server(self, _):
        """Test if MCP service can start normally."""
        threading.Thread(target=self._do_mcp_test, daemon=True).start()

    def _do_mcp_test(self):
        project_dir = os.path.dirname(os.path.abspath(__file__))
        venv_python = os.path.join(project_dir, ".venv", "bin", "python3")
        mcp_server = os.path.join(project_dir, "mcp_server.py")

        if not os.path.isfile(venv_python):
            _notify("MCP 服务", "测试失败", "Python 虚拟环境未安装")
            return

        try:
            proc = subprocess.Popen(
                [venv_python, mcp_server],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(2)
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                _notify("MCP 服务", "测试通过 ✅", "MCP 服务器启动正常")
            else:
                stderr = proc.stderr.read().decode(errors="replace")
                _notify("MCP 服务", "启动失败 ❌", stderr[:200] or "未知错误")
        except Exception as e:
            _notify("MCP 服务", "测试失败 ❌", str(e)[:200])

    def _toggle_auto_refresh(self, _):
        """Toggle 'auto-refresh on menu open' setting."""
        current = self.config.get("auto_refresh_on_open", False)
        self._update_config(patch={"auto_refresh_on_open": not current})
        state = "开启" if not current else "关闭"
        _notify("微信总结", "设置已更新", f"自动刷新已{state}")
        self._rebuild_settings_menu()

    def _toggle_group_nickname(self, _):
        """Toggle 'show group nickname in summary' setting."""
        current = self.config.get("show_group_nickname", True)
        self._update_config(patch={"show_group_nickname": not current})
        state = "开启" if not current else "关闭"
        _notify("微信总结", "设置已更新", f"总结中显示群昵称已{state}")
        self._rebuild_settings_menu()

    def _make_batch_limit_callback(self, val):
        def callback(_):
            self._update_config(patch={"batch_msg_limit": val})
            _notify("微信总结", "设置已更新", f"小组总结每群条数: {val}")
            self._rebuild_settings_menu()
        return callback

    def _make_hide_inactive_callback(self, months):
        def callback(_):
            self._update_config(patch={"hide_inactive_months": months})
            label = f"{months} 个月" if months > 0 else "关闭"
            _notify("微信总结", "设置已更新", f"隐藏不活跃群聊: {label}")
            self._rebuild_settings_menu()
            self._rebuild_chat_menu()
        return callback

    def _make_provider_callback(self, provider_key):
        def callback(sender):
            self._update_config(patch={"ai_provider": provider_key})
            self.ai = None  # Recreate on next summary

            provider_name = dict(AI_PROVIDERS).get(provider_key, provider_key)
            _notify("微信总结", "AI 服务已切换", f"当前使用: {provider_name}")
            print(f"[config] AI 切换为: {provider_key}")

            # If provider needs key and none is set, prompt to configure
            if provider_key != "ollama" and not load_key("ai-api-key"):
                self._set_api_key(None)
            else:
                self._rebuild_settings_menu()
        return callback

    def _bring_to_front(self):
        """Bring app to front, temporarily set as Regular app to capture keyboard input."""
        if _HAS_APPKIT:
            try:
                app = NSApplication.sharedApplication()
                # Ensure dialogs and Dock show correct app icon (not Python rocket)
                if os.path.isfile(APP_ICON_PNG):
                    ns_icon = NSImage.alloc().initWithContentsOfFile_(APP_ICON_PNG)
                    if ns_icon:
                        app.setApplicationIconImage_(ns_icon)
                app.setActivationPolicy_(0)   # Regular -> get keyboard focus
                app.activateIgnoringOtherApps_(True)
            except Exception:
                pass

    def _begin_task(self, label):
        self._summarizing = True
        self._cancel_requested = False
        self._active_task = label
        self._set_status(ICON_LOADING)

    def _finish_task(self):
        self._summarizing = False
        self._cancel_requested = False
        self._active_task = ""

    def _check_cancelled(self):
        if self._cancel_requested:
            raise UserCancelled("用户已取消当前任务")

    def _cancel_current_task(self, _):
        if not self._summarizing:
            _notify("微信总结", "当前没有任务", "没有正在运行的总结或搜索")
            return
        self._cancel_requested = True
        label = self._active_task or "当前任务"
        _notify("微信总结", "已请求停止", f"{label} 会在当前 API 请求结束后停止")

    def _input_dialog(self, title, message, default_text="",
                      ok="确定", cancel="取消", width=300):
        """Show input dialog with correct app icon (replaces rumps.Window).

        Returns:
            (clicked: bool, text: str)
        """
        if _HAS_APPKIT:
            alert = NSAlert.alloc().init()
            alert.setMessageText_(title)
            alert.setInformativeText_(message)
            if os.path.isfile(APP_ICON_PNG):
                _icon = NSImage.alloc().initWithContentsOfFile_(APP_ICON_PNG)
                if _icon:
                    alert.setIcon_(_icon)
            alert.addButtonWithTitle_(ok)
            alert.addButtonWithTitle_(cancel)
            field = NSTextField.alloc().initWithFrame_(((0, 0), (width, 24)))
            field.setStringValue_(default_text)
            alert.setAccessoryView_(field)
            alert.window().setInitialFirstResponder_(field)
            result = alert.runModal()
            clicked = (result == 1000)
            text = str(field.stringValue()) if clicked else ""
            return clicked, text
        else:
            window = rumps.Window(
                message=message, title=title, default_text=default_text,
                ok=ok, cancel=cancel, dimensions=(width, 24),
            )
            resp = window.run()
            return bool(resp.clicked), (resp.text if resp.clicked else "")

    def _confirm_dialog(self, title, message, ok="确定", cancel="取消"):
        """Show confirmation dialog with correct app icon (no input field).

        Returns:
            bool: whether OK was clicked
        """
        if _HAS_APPKIT:
            alert = NSAlert.alloc().init()
            alert.setMessageText_(title)
            alert.setInformativeText_(message)
            if os.path.isfile(APP_ICON_PNG):
                _icon = NSImage.alloc().initWithContentsOfFile_(APP_ICON_PNG)
                if _icon:
                    alert.setIcon_(_icon)
            alert.addButtonWithTitle_(ok)
            alert.addButtonWithTitle_(cancel)
            return alert.runModal() == 1000
        else:
            window = rumps.Window(
                message=message, title=title, default_text="",
                ok=ok, cancel=cancel, dimensions=(0, 0),
            )
            return bool(window.run().clicked)

    def _release_front(self):
        """Restore as menu bar app (hide Dock icon)."""
        if _HAS_APPKIT:
            try:
                NSApplication.sharedApplication().setActivationPolicy_(1)  # Accessory
            except Exception:
                pass

    def _delayed_run(self, func, *args):
        """Delay execution on main thread, let macOS close menu before showing dialog (NSWindow must be created on main thread)."""
        def _fire(timer):
            timer.stop()
            func(*args)
        t = rumps.Timer(_fire, 0.3)
        t.start()

    def _run_on_main(self, func, *args):
        """Execute on main thread (required for menu modifications from background threads)."""
        self._main_queue.put((func, args))

    def _process_main_queue(self, _):
        """Main thread timer callback: process UI updates submitted by background threads."""
        while not self._main_queue.empty():
            try:
                func, args = self._main_queue.get_nowait()
                func(*args)
            except queue.Empty:
                break
            except Exception:
                traceback.print_exc()

    def _setup_menu_delegate(self, timer):
        """Install menu open detection delegate (runs once)."""
        timer.stop()
        try:
            delegate = _MenuOpenDelegate.alloc().init()
            delegate.app_ref = self
            # Install delegate via rumps Menu wrapper's underlying NSMenu
            ns_menu = self.menu._menu
            if ns_menu:
                ns_menu.setDelegate_(delegate)
                self._menu_delegate = delegate  # prevent GC
                print("[init] ✓ 菜单打开自动刷新已安装")
        except Exception as e:
            print(f"[init] 菜单回调安装失败（不影响使用）: {e}")

    def _do_silent_refresh(self):
        """Silently refresh chat list (auto-triggered on menu open, no notifications)."""
        try:
            if self.db:
                self._run_on_main(self._rebuild_chat_menu)
                self._run_on_main(self._rebuild_mcp_menu)
                print("[auto-refresh] ✓ 群聊列表已刷新")
        except Exception:
            traceback.print_exc()

    def _set_api_key(self, _):
        """Show API Key dialog (delayed execution, let menu close first)."""
        self._delayed_run(self._show_api_key_dialog)

    def _set_ai_model(self, _):
        """Show AI model dialog."""
        self._delayed_run(self._show_ai_model_dialog)

    def _show_ai_model_dialog(self):
        provider = self.config.get("ai_provider", "qwen")
        provider_name = dict(AI_PROVIDERS).get(provider, provider)
        current = self.config.get("ai_model", "")

        self._bring_to_front()
        try:
            clicked, text = self._input_dialog(
                "设置 AI 模型",
                f"当前 AI 服务：{provider_name}\n"
                "留空表示使用该服务的默认模型。\n"
                "例如 DeepSeek 可填 deepseek-v4-flash。",
                default_text=current,
                ok="保存",
                width=380,
            )
            if not clicked:
                return
            self._update_config(patch={"ai_model": text.strip()})
            self.ai = None
            model = self.config["ai_model"] or "默认"
            _notify("微信总结", "AI 模型已更新", model)
            self._rebuild_settings_menu()
            self._rebuild_monitor_menu()
        finally:
            self._release_front()

    def _show_api_key_dialog(self):
        provider = self.config.get("ai_provider", "qwen")
        provider_name = dict(AI_PROVIDERS).get(provider, provider)

        hints = {
            "qwen": "通义千问 Key 获取：dashscope.console.aliyun.com",
            "deepseek": "DeepSeek Key 获取：platform.deepseek.com",
            "claude": "Claude Key 获取：console.anthropic.com",
            "openai": "OpenAI Key 获取：platform.openai.com",
        }
        hint = hints.get(provider, "请输入 API Key")

        self._bring_to_front()
        try:
            clicked, text = self._input_dialog(
                "设置 API Key",
                f"当前 AI 服务：{provider_name}\n{hint}\n\nKey 将安全存储在 macOS 钥匙串中",
                ok="保存", width=380,
            )

            if clicked and text.strip():
                key = text.strip()
                if save_key("ai-api-key", key):
                    self.ai = None
                    _notify("微信总结", "API Key 已保存", "密钥已安全存储在 macOS 钥匙串中")
                    self._rebuild_settings_menu()
                else:
                    _notify("微信总结", "保存失败", "无法写入钥匙串")
        finally:
            self._release_front()

    def _reset_bookmarks(self, _):
        """Clear all bookmarks (delayed execution)."""
        self._delayed_run(self._show_reset_bookmarks_dialog)

    def _show_reset_bookmarks_dialog(self):
        self._bring_to_front()
        try:
            confirmed = self._confirm_dialog(
                "重置所有书签",
                "清除后，所有群聊将变为「未总结」状态，\n下次点击总结时会重新读取最近消息。\n\n确定要重置吗？",
                ok="确定重置",
            )
            if confirmed:
                clear_all_bookmarks()
                if self.db:
                    self.db.invalidate_cache()
                    self._rebuild_chat_menu()
                _notify("微信总结", "已重置", "所有书签已清除，所有群聊已恢复为未总结")
                print("[config] 所有书签已清除，缓存已刷新")
        finally:
            self._release_front()

    def open_config_file(self, _):
        if not os.path.exists(CONFIG_FILE):
            self._update_config()
        subprocess.run(["open", CONFIG_FILE])

    def _open_summary_dir(self, _):
        subprocess.run(["open", SUMMARY_DIR])

    # ── Initialization ──────────────────────────────────────────

    def _init_background(self):
        print("[init] 开始后台初始化...")
        self._set_status(ICON_LOADING)

        keys = get_cached_keys()
        print(f"[init] 缓存密钥: {'有' if keys else '无'}")
        signed = is_wechat_signed()
        print(f"[init] 微信签名: {'正常' if signed else '需要重新授权'}")

        if not signed and keys:
            _notify("微信总结", "检测到微信签名已失效",
                    f"当前缓存密钥仍可使用；如读不到新消息，{_wechat_signing_message()}")

        if not keys:
            if not is_wechat_running():
                _notify("微信总结", "初始化失败", "请先启动微信并登录")
                self._set_status(ICON_ERROR)
                return
            if not signed:
                _notify("微信总结", "微信需要重新授权", _wechat_signing_message())
                self._set_status(ICON_ERROR)
                return
            _notify("微信总结", "首次运行", "正在同步数据源...")
            keys = extract_keys()
            if not keys:
                _notify("微信总结", "数据源同步失败", "请确认微信已登录且已重签名")
                self._set_status(ICON_ERROR)
                return

        print(f"[init] db_dir: {self.config.get('db_dir')}")
        if not self.config.get("db_dir") or not os.path.isdir(self.config["db_dir"]):
            _notify("微信总结", "未找到微信数据目录", "请检查配置")
            self._set_status(ICON_ERROR)
            return

        print("[init] 正在加载数据库...")
        self.db = WeChatDB.for_runtime(self.config["db_dir"], keys)
        if self.config.get("attachment_archive_enabled", False):
            self._start_attachment_archive_consumer()
        if self.config.get("resource_backup_enabled", False):
            self._start_resource_backup_consumer(manual=False)
        if self.config.get("wechat_source_guard_enabled", False):
            self._start_source_guard_consumer()
        if (
            self.config.get("google_drive_file_sync_enabled", False)
            and not self.config.get("google_drive_file_sync_paused", False)
        ):
            self._start_drive_sync_consumer(manual=False)

        print("[init] 正在刷新群聊列表...")
        self._run_on_main(self._rebuild_chat_menu)

        # Check if any new encrypted databases are missing keys
        try:
            missing = check_new_databases(self.config["db_dir"], keys)
            if missing:
                names = ", ".join(os.path.basename(m) for m in missing)
                print(f"[init] ⚠ 发现 {len(missing)} 个数据库缺少密钥: {names}")
                _notify("微信总结", f"发现 {len(missing)} 个新数据库",
                        f"建议点击「🔄 刷新数据源」更新\n{names}")
            else:
                print("[init] ✓ 所有数据库密钥完整")
        except Exception as e:
            print(f"[init] 数据库检测出错: {e}")

        self._set_status(ICON_NORMAL)
        print("[init] ✓ 初始化完成！")
        _notify("微信总结", "就绪", "点击菜单栏选择群聊进行总结")

    # ── Chat list + groups (unified dynamic menu management) ────────────────

    def _build_chat_title(self, session):
        """Build menu title for a single group chat."""
        name = session["name"]
        username = session["username"]
        unread = session["unread"]

        last_summary = get_summary_time(username)
        bookmark_ts = get_bookmark(username)

        title = f"📎 {name}"
        has_summarized = bool(last_summary) or bookmark_ts > 0

        if has_summarized:
            display_time = last_summary or datetime.fromtimestamp(bookmark_ts).strftime("%Y-%m-%d %H:%M")
            title += f"  ⏱{display_time}"
            new_count = self.db.count_messages_since(username, bookmark_ts)
            if new_count > 0:
                title += f" · 有{new_count}条更新"
            print(f"[refresh]   {name}: 已总结 ({display_time}), 更新={new_count}")
        else:
            if unread > 0:
                title += f" (未总结 · {unread}条未读)"
            else:
                title += " (未总结)"
            print(f"[refresh]   {name}: 未总结, 微信未读={unread}")

        return title

    def _rebuild_chat_menu(self):
        """Rebuild dynamic menu: ungrouped chats + group submenus."""
        # Clear old dynamic items (📎 ungrouped chats + 📂 groups)
        keys_to_remove = [k for k in self.menu.keys()
                          if isinstance(k, str) and (k.startswith("📎") or k.startswith("📂"))]
        for key in keys_to_remove:
            del self.menu[key]

        if not self.db:
            return

        sessions = self.db.get_recent_sessions(limit=200)
        group_sessions = [s for s in sessions if s["is_group"]]

        # ── Filter inactive chats ──
        hide_months = self.config.get("hide_inactive_months", 1)
        if hide_months > 0:
            import time as _time
            cutoff_ts = _time.time() - hide_months * 30 * 86400
            group_sessions = [s for s in group_sessions if s["timestamp"] >= cutoff_ts]

        # Find chats that are already in groups
        groups = load_groups()
        grouped_usernames = set()
        for grp in groups:
            grouped_usernames.update(grp["chats"])

        # ── Ungrouped chats (reverse insert_after refresh button) ──
        ungrouped = [s for s in group_sessions if s["username"] not in grouped_usernames]

        if ungrouped:
            for session in reversed(ungrouped[:20]):
                title = self._build_chat_title(session)
                item = rumps.MenuItem(title)
                item.add(rumps.MenuItem("📝 总结新消息", callback=self._make_summary_callback(session)))
                item.add(rumps.MenuItem("🔧 自定义总结…", callback=self._make_custom_summary_callback(session)))
                item.add(rumps.MenuItem("📅 按天总结…", callback=self._make_daily_summary_callback(session)))
                self.menu.insert_after("🔍 关键词搜索", item)
        elif not groups:
            self.menu.insert_after("🔍 关键词搜索", rumps.MenuItem("📎 (暂无群聊)"))

        # ── Groups (insert_before in order before recent summaries) ──
        if groups:
            self._load_contacts_if_needed()
            for grp in groups:
                grp_menu = self._build_group_submenu(grp)
                self.menu.insert_before("📋 最近总结", grp_menu)

        # New group button (always at the bottom of group area)
        self.menu.insert_before("📋 最近总结",
                                rumps.MenuItem("📂 ✨ 新建分组…", callback=self._create_group))

    def _make_summary_callback(self, session):
        def callback(sender):
            if self._summarizing:
                _notify("微信总结", "请等待", "正在总结中...")
                return
            self._set_status(ICON_NORMAL)
            threading.Thread(
                target=self._summarize_group, args=(session,), daemon=True
            ).start()
        return callback

    def _make_custom_summary_callback(self, session):
        def callback(_):
            if self._summarizing:
                _notify("微信总结", "请等待", "正在总结中...")
                return
            self._delayed_run(self._show_custom_summary_dialog, session)
        return callback

    def _show_custom_summary_dialog(self, session):
        group_name = session["name"]
        self._bring_to_front()
        try:
            if not _HAS_APPKIT:
                # Fallback: use _input_dialog with single input field
                clicked, text = self._input_dialog(
                    "自定义总结",
                    f"群聊：{group_name}\n\n输入条数（如 50）或分钟数加m（如 30m）",
                    default_text="50", ok="开始总结",
                )
                if clicked and text.strip():
                    text = text.strip()
                    if text.lower().endswith("m"):
                        minutes = int(text[:-1])
                        threading.Thread(
                            target=self._summarize_group,
                            args=(session,),
                            kwargs={"custom_minutes": minutes},
                            daemon=True,
                        ).start()
                    else:
                        count = int(text)
                        if count <= 0 or count > CUSTOM_SUMMARY_MAX_COUNT:
                            _notify("微信总结", "输入错误", f"消息条数请输入 1-{CUSTOM_SUMMARY_MAX_COUNT} 的整数")
                            return
                        threading.Thread(
                            target=self._summarize_group,
                            args=(session,),
                            kwargs={"custom_count": count},
                            daemon=True,
                        ).start()
                return

            # ── PyObjC dual input fields ──
            alert = NSAlert.alloc().init()
            alert.setMessageText_("自定义总结")
            alert.setInformativeText_(
                f"群聊：{group_name}\n以下两项填一项即可（不要都填）"
            )
            # Set dialog icon
            if os.path.isfile(APP_ICON_PNG):
                _icon = NSImage.alloc().initWithContentsOfFile_(APP_ICON_PNG)
                if _icon:
                    alert.setIcon_(_icon)
            alert.addButtonWithTitle_("开始总结")
            alert.addButtonWithTitle_("取消")

            view = NSView.alloc().initWithFrame_(((0, 0), (300, 60)))

            label1 = NSTextField.alloc().initWithFrame_(((0, 35), (80, 22)))
            label1.setStringValue_("消息条数：")
            label1.setBezeled_(False)
            label1.setEditable_(False)
            label1.setDrawsBackground_(False)
            view.addSubview_(label1)

            field1 = NSTextField.alloc().initWithFrame_(((80, 35), (210, 22)))
            field1.setPlaceholderString_("如 50")
            view.addSubview_(field1)

            label2 = NSTextField.alloc().initWithFrame_(((0, 5), (80, 22)))
            label2.setStringValue_("最近分钟：")
            label2.setBezeled_(False)
            label2.setEditable_(False)
            label2.setDrawsBackground_(False)
            view.addSubview_(label2)

            field2 = NSTextField.alloc().initWithFrame_(((80, 5), (210, 22)))
            field2.setPlaceholderString_("如 30 = 最近30分钟")
            view.addSubview_(field2)

            alert.setAccessoryView_(view)
            alert.window().setInitialFirstResponder_(field1)

            result = alert.runModal()
            if result != 1000:  # NSAlertFirstButtonReturn
                return

            count_str = str(field1.stringValue()).strip()
            minutes_str = str(field2.stringValue()).strip()

            if count_str and minutes_str:
                _notify("微信总结", "输入错误", "请只填一项，不要两项都填")
                return
            if not count_str and not minutes_str:
                _notify("微信总结", "输入错误", "请至少填写一项")
                return

            if count_str:
                try:
                    count = int(count_str)
                    if count <= 0 or count > CUSTOM_SUMMARY_MAX_COUNT:
                        raise ValueError
                except ValueError:
                    _notify("微信总结", "输入错误", f"消息条数请输入 1-{CUSTOM_SUMMARY_MAX_COUNT} 的整数")
                    return
                threading.Thread(
                    target=self._summarize_group,
                    args=(session,),
                    kwargs={"custom_count": count},
                    daemon=True,
                ).start()
            else:
                try:
                    minutes = int(minutes_str)
                    if minutes <= 0:
                        raise ValueError
                except ValueError:
                    _notify("微信总结", "输入错误", "分钟数请输入正整数")
                    return
                threading.Thread(
                    target=self._summarize_group,
                    args=(session,),
                    kwargs={"custom_minutes": minutes},
                    daemon=True,
                ).start()
        except Exception:
            traceback.print_exc()
        finally:
            self._release_front()

    def _make_daily_summary_callback(self, session):
        def callback(_):
            if self._summarizing:
                _notify("微信总结", "请等待", "正在总结中...")
                return
            self._delayed_run(self._show_daily_summary_dialog, session)
        return callback

    def _show_daily_summary_dialog(self, session):
        import calendar
        group_name = session["name"]
        now = datetime.now()
        self._bring_to_front()
        try:
            if not _HAS_APPKIT:
                clicked, text = self._input_dialog(
                    "按天总结",
                    f"群聊：{group_name}\n\n输入日期，格式 YYYY-MM-DD\n例如：2026-05-12",
                    default_text=now.strftime("%Y-%m-%d"), ok="开始总结",
                )
                if clicked and text.strip():
                    try:
                        target_date = datetime.strptime(text.strip(), "%Y-%m-%d")
                    except ValueError:
                        _notify("微信总结", "日期格式错误", "请使用 YYYY-MM-DD 格式")
                        return
                    threading.Thread(
                        target=self._daily_summarize,
                        args=(session, target_date),
                        daemon=True,
                    ).start()
                return

            alert = NSAlert.alloc().init()
            alert.setMessageText_("📅 按天总结")
            alert.setInformativeText_(f"群聊：{group_name}\n选择要总结的日期")
            if os.path.isfile(APP_ICON_PNG):
                _icon = NSImage.alloc().initWithContentsOfFile_(APP_ICON_PNG)
                if _icon:
                    alert.setIcon_(_icon)
            alert.addButtonWithTitle_("开始总结")
            alert.addButtonWithTitle_("取消")

            view = NSView.alloc().initWithFrame_(((0, 0), (320, 32)))

            lbl_y = NSTextField.alloc().initWithFrame_(((0, 5), (30, 22)))
            lbl_y.setStringValue_("年")
            lbl_y.setBezeled_(False)
            lbl_y.setEditable_(False)
            lbl_y.setDrawsBackground_(False)
            view.addSubview_(lbl_y)

            years = [str(y) for y in range(now.year - 2, now.year + 1)]
            popup_year = NSPopUpButton.alloc().initWithFrame_pullsDown_(((28, 5), (75, 22)), False)
            popup_year.addItemsWithTitles_(years)
            popup_year.selectItemWithTitle_(str(now.year))
            view.addSubview_(popup_year)

            lbl_m = NSTextField.alloc().initWithFrame_(((110, 5), (30, 22)))
            lbl_m.setStringValue_("月")
            lbl_m.setBezeled_(False)
            lbl_m.setEditable_(False)
            lbl_m.setDrawsBackground_(False)
            view.addSubview_(lbl_m)

            months = [str(m) for m in range(1, 13)]
            popup_month = NSPopUpButton.alloc().initWithFrame_pullsDown_(((138, 5), (55, 22)), False)
            popup_month.addItemsWithTitles_(months)
            popup_month.selectItemWithTitle_(str(now.month))
            view.addSubview_(popup_month)

            lbl_d = NSTextField.alloc().initWithFrame_(((200, 5), (30, 22)))
            lbl_d.setStringValue_("日")
            lbl_d.setBezeled_(False)
            lbl_d.setEditable_(False)
            lbl_d.setDrawsBackground_(False)
            view.addSubview_(lbl_d)

            days = [str(d) for d in range(1, 32)]
            popup_day = NSPopUpButton.alloc().initWithFrame_pullsDown_(((228, 5), (55, 22)), False)
            popup_day.addItemsWithTitles_(days)
            popup_day.selectItemWithTitle_(str(now.day))
            view.addSubview_(popup_day)

            alert.setAccessoryView_(view)

            result = alert.runModal()
            if result != 1000:
                return

            year = int(str(popup_year.titleOfSelectedItem()))
            month = int(str(popup_month.titleOfSelectedItem()))
            day = int(str(popup_day.titleOfSelectedItem()))

            max_day = calendar.monthrange(year, month)[1]
            if day > max_day:
                _notify("微信总结", "日期无效", f"{year}年{month}月只有{max_day}天")
                return

            target_date = datetime(year, month, day)
            if target_date > now:
                _notify("微信总结", "日期无效", "不能选择未来的日期")
                return

            threading.Thread(
                target=self._daily_summarize,
                args=(session, target_date),
                daemon=True,
            ).start()

        except Exception:
            traceback.print_exc()
        finally:
            self._release_front()

    def _daily_summarize(self, session, target_date):
        """Summarize messages for a single day, chunked by 300 messages."""
        from datetime import timedelta

        CHUNK_SIZE = 300

        self._begin_task(f"按天总结：{session['name']}")

        try:
            username = session["username"]
            group_name = session["name"]
            date_str = target_date.strftime("%Y-%m-%d")

            day_start_ts = target_date.timestamp()
            day_end_ts = (target_date + timedelta(days=1)).timestamp()

            print(f"[daily] {group_name}: 按天总结 {date_str}")

            messages = self.db.get_messages(username, since_ts=day_start_ts, limit=10000)
            messages = [m for m in messages if m["timestamp"] < day_end_ts]

            if not messages:
                _notify("微信总结", group_name, f"{date_str} 没有消息")
                return

            msg_count = len(messages)
            start_time = messages[0]["time_str"]
            end_time = messages[-1]["time_str"]

            if not self.ai:
                try:
                    self.ai = create_provider(self.config)
                except Exception as e:
                    _notify("微信总结", "AI 未配置", str(e))
                    if "Key" in str(e):
                        self._set_api_key(None)
                    return

            chunks = [messages[i:i + CHUNK_SIZE] for i in range(0, len(messages), CHUNK_SIZE)]
            total_chunks = len(chunks)
            print(f"[daily] {group_name}: {date_str} 共 {msg_count} 条消息，分 {total_chunks} 段总结")

            _notify("微信总结", f"📅 {group_name}",
                    f"{date_str} · {msg_count}条消息 · 分{total_chunks}段，开始总结...")

            summaries = []
            for idx, chunk in enumerate(chunks, 1):
                self._check_cancelled()
                chunk_start = chunk[0]["time_str"]
                chunk_end = chunk[-1]["time_str"]
                chunk_text = self.db.format_messages_for_ai(
                    chunk,
                    show_group_nickname=self.config.get("show_group_nickname", True),
                )

                start_short = chunk_start.split(" ", 1)[-1]
                end_short = chunk_end.split(" ", 1)[-1]
                print(f"[daily]   第 {idx}/{total_chunks} 段: {len(chunk)} 条 ({start_short} ~ {end_short})，调用 AI...")
                _notify("微信总结", f"📅 {group_name}",
                        f"正在总结第 {idx}/{total_chunks} 段（{start_short} ~ {end_short}）...")

                prompt = self.ai.build_prompt(
                    group_name=group_name,
                    messages_text=chunk_text,
                    start_time=chunk_start,
                    end_time=chunk_end,
                    msg_count=len(chunk),
                )
                try:
                    summary = self.ai.summarize(prompt)
                    self._check_cancelled()
                except UserCancelled:
                    raise
                except Exception as e:
                    print(f"[daily]   ✗ 第 {idx} 段失败: {e}")
                    summary = f"（此段总结失败：{e}）"
                summaries.append({
                    "text": summary,
                    "start": chunk_start,
                    "end": chunk_end,
                    "count": len(chunk),
                })

            full_summary = self._combine_daily_summaries(summaries, total_chunks)

            summary_file = self._save_daily_summary(
                group_name, full_summary, msg_count, date_str,
                start_time, end_time, total_chunks,
            )

            self._last_summary = {
                "group": f"📅 {group_name}",
                "text": full_summary,
                "file": summary_file,
                "msg_count": msg_count,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self._run_on_main(self._refresh_menu_after_summary)

            _notify("微信总结", f"📅 {group_name}",
                    f"{date_str} · {msg_count}条消息 · {total_chunks}段已总结")
            print(f"[daily] ✓ {group_name} {date_str} 总结完成")

            subprocess.run(["open", summary_file])
            self._set_status(ICON_DONE)

        except UserCancelled:
            _notify("微信总结", "已停止", f"{group_name} 的按天总结已停止")
            self._set_status(ICON_NORMAL)
        except Exception as e:
            _notify("微信总结", "按天总结失败", str(e))
            traceback.print_exc()
            self._set_status(ICON_ERROR)
        finally:
            self._finish_task()

    def _combine_daily_summaries(self, summaries, total_chunks):
        if total_chunks == 1:
            return summaries[0]["text"]
        parts = []
        for s in summaries:
            start_short = s["start"].split(" ", 1)[-1]
            end_short = s["end"].split(" ", 1)[-1]
            header = f"## {start_short} ~ {end_short}（{s['count']}条消息）\n"
            parts.append(header + s["text"])
        return "\n\n".join(parts)

    def _save_daily_summary(self, group_name, summary, msg_count, date_str,
                            start_time, end_time, chunk_count):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in group_name)
        filename = f"daily_{safe_name}_{date_str}_{timestamp}.txt"
        filepath = os.path.join(SUMMARY_DIR, filename)

        chunk_note = f"（分 {chunk_count} 段总结）" if chunk_count > 1 else ""
        header = (
            f"{'='*50}\n"
            f"  📅 按天总结：{group_name}\n"
            f"  日期：{date_str}\n"
            f"  {msg_count} 条消息 · {start_time} ~ {end_time}{chunk_note}\n"
            f"  生成时间：{datetime.now().strftime('%Y年%m月%d日 %H:%M')}\n"
            f"{'='*50}\n\n"
        )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(header + summary)
        return filepath

    # ── Summary logic ────────────────────────────────────────

    def _summarize_group(self, session, custom_count=None, custom_minutes=None):
        self._begin_task(f"总结：{session['name']}")

        try:
            username = session["username"]
            group_name = session["name"]
            total_new_count = None

            if custom_minutes:
                since_ts = time.time() - custom_minutes * 60
                print(f"[summary] {group_name}: 自定义总结最近 {custom_minutes} 分钟...")
                messages = self.db.get_messages(username, since_ts=since_ts, limit=500)
            elif custom_count:
                print(f"[summary] {group_name}: 自定义总结最近 {custom_count} 条...")
                messages = self.db.get_messages(username, since_ts=0, limit=custom_count)
            else:
                since_ts = get_bookmark(username)
                if since_ts > 0:
                    since_str = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M")
                    print(f"[summary] {group_name}: 读取 {since_str} 之后的新消息...")
                else:
                    print(f"[summary] {group_name}: 首次总结，读取最近消息...")
                if since_ts > 0:
                    total_new_count = self.db.count_messages_since(username, since_ts)
                messages = self.db.get_messages(
                    username,
                    since_ts=since_ts,
                    limit=500,
                    page_forward=since_ts > 0,
                )
            if not messages:
                _notify("微信总结", group_name, "没有新消息")
                return

            messages_text = self.db.format_messages_for_ai(messages, show_group_nickname=self.config.get("show_group_nickname", True))
            start_time = messages[0]["time_str"]
            end_time = messages[-1]["time_str"]
            msg_count = len(messages)

            print(f"[summary] {group_name}: 共 {msg_count} 条消息 ({start_time} ~ {end_time}), 正在调用 AI...")
            self._check_cancelled()

            if not self.ai:
                try:
                    self.ai = create_provider(self.config)
                except Exception as e:
                    _notify("微信总结", "AI 未配置", str(e))
                    if "Key" in str(e):
                        self._set_api_key(None)
                    return

            prompt = self.ai.build_prompt(
                group_name=group_name,
                messages_text=messages_text,
                start_time=start_time,
                end_time=end_time,
                msg_count=msg_count,
            )

            summary = self.ai.summarize(prompt)
            self._check_cancelled()

            # Update bookmark
            set_bookmark(username, messages[-1]["timestamp"])
            remaining_count = 0
            if total_new_count is not None:
                remaining_count = self.db.count_messages_since(username, messages[-1]["timestamp"])

            # Save to file
            summary_file = self._save_summary(group_name, summary, msg_count, start_time, end_time)

            # Update menu
            self._last_summary = {
                "group": group_name,
                "text": summary,
                "file": summary_file,
                "msg_count": msg_count,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self._run_on_main(self._refresh_menu_after_summary)

            done_message = f"{msg_count}条消息已总结"
            if remaining_count > 0:
                done_message += f"，仍有约{remaining_count}条，继续点「总结新消息」"
            _notify("微信总结", f"✅ {group_name}", done_message)
            print(f"[summary] ✓ {group_name} 总结完成")

            # Auto-open summary file
            subprocess.run(["open", summary_file])

            self._set_status(ICON_DONE)

        except UserCancelled:
            _notify("微信总结", "已停止", f"{group_name} 的总结已停止")
            self._set_status(ICON_NORMAL)
        except Exception as e:
            _notify("微信总结", "总结失败", str(e))
            traceback.print_exc()
            self._set_status(ICON_ERROR)
        finally:
            self._finish_task()

    def _save_summary(self, group_name, summary, msg_count, start_time, end_time):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in group_name)
        filename = f"{safe_name}_{timestamp}.txt"
        filepath = os.path.join(SUMMARY_DIR, filename)

        header = (
            f"{'='*50}\n"
            f"  {group_name}\n"
            f"  {msg_count} 条消息 · {start_time} ~ {end_time}\n"
            f"  生成时间：{datetime.now().strftime('%Y年%m月%d日 %H:%M')}\n"
            f"{'='*50}\n\n"
        )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(header + summary)
        return filepath

    # ── Summary menu display ──────────────────────────────────

    def _refresh_menu_after_summary(self):
        """Refresh all menus after summary completes (must be called on main thread)."""
        self._rebuild_chat_menu()
        self._update_latest_summary()
        self._rebuild_summary_history()

    def _update_latest_summary(self):
        """Update latest summary display (above recent summaries menu)."""
        for key in list(self.menu.keys()):
            if isinstance(key, str) and key.startswith("📝"):
                del self.menu[key]

        s = self._last_summary
        if not s:
            return

        title = f"📝 {s['group']}（{s['msg_count']}条 · {s['time']}）"
        parent = rumps.MenuItem(title)

        # Preview first few lines
        for line in s["text"].strip().split("\n")[:6]:
            line = line.strip()
            if not line:
                continue
            display = line[:45] + "…" if len(line) > 45 else line
            parent.add(rumps.MenuItem(display))

        parent.add(rumps.separator)
        parent.add(rumps.MenuItem("📋 复制到剪贴板", callback=self._copy_summary))
        parent.add(rumps.MenuItem("📄 查看完整内容", callback=self._make_open_file_callback(s["file"])))

        self.menu.insert_before("📋 最近总结", parent)

    def _copy_summary(self, _):
        if not self._last_summary:
            return
        process = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        process.communicate(self._last_summary["text"].encode("utf-8"))
        _notify("微信总结", "已复制", "总结内容已复制到剪贴板")

    def _rebuild_summary_history(self):
        """Rebuild recent summaries submenu (excludes the latest one, already shown separately)."""
        if "📋 最近总结" in self.menu:
            del self.menu["📋 最近总结"]

        parent = rumps.MenuItem("📋 最近总结")
        summaries = self._get_recent_summaries(limit=15)

        # Exclude the latest summary already shown separately above
        latest_file = self._last_summary.get("file") if self._last_summary else None

        has_items = False
        for s in summaries:
            if s["path"] == latest_file:
                continue
            item = rumps.MenuItem(s["display"], callback=self._make_open_file_callback(s["path"]))
            parent.add(item)
            has_items = True

        if has_items:
            parent.add(rumps.separator)
        parent.add(rumps.MenuItem("📁 打开总结目录", callback=self._open_summary_dir))

        self.menu.insert_before("⚙️ 设置", parent)

    def _get_recent_summaries(self, limit=15):
        """Read recent summary file list from summary directory."""
        summaries = []
        if not os.path.isdir(SUMMARY_DIR):
            return summaries

        for f in os.listdir(SUMMARY_DIR):
            if not f.endswith(".txt"):
                continue
            path = os.path.join(SUMMARY_DIR, f)
            mtime = os.path.getmtime(path)

            # Read group name from second line of file header
            try:
                with open(path, encoding="utf-8") as fh:
                    fh.readline()  # skip "===="
                    group_name = fh.readline().strip()
            except Exception:
                group_name = f[:-4]

            time_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            display = f"{group_name}（{time_str}）"
            summaries.append({"path": path, "display": display, "mtime": mtime})

        summaries.sort(key=lambda x: x["mtime"], reverse=True)
        return summaries[:limit]

    def _make_open_file_callback(self, filepath):
        def callback(_):
            subprocess.run(["open", filepath])
        return callback

    # ── Group management ────────────────────────────────────────

    def _build_group_submenu(self, grp):
        """Build submenu for a single group."""
        grp_name = grp["name"]
        chat_count = len(grp["chats"])

        grp_summary_time = get_group_summary_time(grp_name)
        if grp_summary_time:
            grp_title = f"📂 {grp_name}（上次总结 {grp_summary_time}）"
        elif chat_count > 0:
            grp_title = f"📂 {grp_name}（{chat_count}个群 · 未总结）"
        else:
            grp_title = f"📂 {grp_name}（空）"

        grp_menu = rumps.MenuItem(grp_title)

        if grp["chats"]:
            for chat_user in grp["chats"]:
                display = self._get_chat_display_name(chat_user)
                bookmark_ts = get_bookmark(chat_user)

                if bookmark_ts > 0:
                    new_count = self.db.count_messages_since(chat_user, bookmark_ts)
                    if new_count > 0:
                        chat_label = f"   {display}（{new_count}条未读）"
                    else:
                        chat_label = f"   {display}（无更新）"
                else:
                    chat_label = f"   {display}（未总结）"

                chat_item = rumps.MenuItem(chat_label)
                chat_session = {"username": chat_user, "name": display, "is_group": True}
                chat_item.add(rumps.MenuItem("📝 总结新消息", callback=self._make_summary_callback(chat_session)))
                chat_item.add(rumps.MenuItem("🔧 自定义总结…", callback=self._make_custom_summary_callback(chat_session)))
                chat_item.add(rumps.MenuItem("📅 按天总结…", callback=self._make_daily_summary_callback(chat_session)))
                chat_item.add(rumps.separator)
                chat_item.add(rumps.MenuItem("❌ 从分组移除", callback=self._make_remove_from_group_callback(grp_name, chat_user)))
                grp_menu.add(chat_item)

            grp_menu.add(rumps.separator)

        grp_menu.add(rumps.MenuItem("➕ 添加群聊…", callback=self._make_add_to_group_callback(grp_name)))
        grp_menu.add(rumps.separator)
        grp_menu.add(rumps.MenuItem(f"🚀 一键总结「{grp_name}」", callback=self._make_batch_summary_callback(grp_name)))
        grp_menu.add(rumps.MenuItem("🗑️ 删除分组", callback=self._make_delete_group_callback(grp_name)))

        return grp_menu

    def _load_contacts_if_needed(self):
        """Ensure contacts are loaded."""
        if self.db:
            self.db._load_contacts()

    def _get_chat_display_name(self, username):
        """Get display name for a group chat."""
        if self.db and self.db._contacts:
            return self.db._contacts.get(username, username)
        return username

    def _create_group(self, _):
        """Create new group (delayed dialog)."""
        self._delayed_run(self._show_create_group_dialog)

    def _show_create_group_dialog(self):
        self._bring_to_front()
        try:
            clicked, text = self._input_dialog(
                "新建分组",
                "请输入分组名称，例如：购物群、工作群、学习群",
                ok="创建",
            )
            if clicked and text.strip():
                name = text.strip()
                if create_group(name):
                    _notify("微信总结", "分组已创建", f"「{name}」，现在可以添加群聊了")
                    self._rebuild_chat_menu()
                else:
                    _notify("微信总结", "创建失败", f"「{name}」已存在")
        finally:
            self._release_front()

    def _make_delete_group_callback(self, group_name):
        def callback(_):
            self._delayed_run(self._show_delete_group_dialog, group_name)
        return callback

    def _show_delete_group_dialog(self, group_name):
        self._bring_to_front()
        try:
            confirmed = self._confirm_dialog(
                "删除分组",
                f"确定要删除分组「{group_name}」吗？\n（不会影响群聊本身，只是移除分组）",
                ok="确定删除",
            )
            if confirmed:
                delete_group(group_name)
                _notify("微信总结", "已删除", f"分组「{group_name}」已移除")
                self._rebuild_chat_menu()
        finally:
            self._release_front()

    def _make_add_to_group_callback(self, group_name):
        def callback(_):
            self._delayed_run(self._show_add_to_group_dialog, group_name)
        return callback

    def _show_add_to_group_dialog(self, group_name):
        if not self.db:
            _notify("微信总结", "未初始化", "请先确保微信已登录")
            return

        # Get all group chats from contact.db (not limited by session count)
        group_sessions = self.db.get_groups()

        if not group_sessions:
            _notify("微信总结", "暂无群聊", "请先刷新群聊列表")
            return

        # Chats already in this group
        existing = set(get_group_chats(group_name))

        # Build selection list (exclude already added)
        available = [s for s in group_sessions if s["username"] not in existing]
        if not available:
            _notify("微信总结", "无可添加群聊", "所有群聊已在该分组中")
            return

        self._bring_to_front()
        try:
            lines = []
            for i, s in enumerate(available, 1):
                lines.append(f"{i}. {s['name']}")
            msg = f"输入要添加到「{group_name}」的群聊序号（多个用逗号分隔）：\n\n" + "\n".join(lines)

            clicked, text = self._input_dialog(
                f"添加群聊到「{group_name}」", msg,
                ok="添加", width=380,
            )
            if clicked and text.strip():
                added = []
                for part in text.strip().replace("，", ",").split(","):
                    try:
                        idx = int(part.strip()) - 1
                        if 0 <= idx < len(available):
                            s = available[idx]
                            add_chat_to_group(group_name, s["username"])
                            added.append(s["name"])
                    except ValueError:
                        pass
                if added:
                    _notify("微信总结", f"已添加到「{group_name}」", "、".join(added))
                    self._rebuild_chat_menu()
        finally:
            self._release_front()

    def _make_remove_from_group_callback(self, group_name, chat_username):
        def callback(_):
            display = self._get_chat_display_name(chat_username)
            remove_chat_from_group(group_name, chat_username)
            _notify("微信总结", "已移除", f"「{display}」已从「{group_name}」移除")
            self._rebuild_chat_menu()
        return callback

    def _make_batch_summary_callback(self, group_name):
        def callback(_):
            if self._summarizing:
                _notify("微信总结", "请等待", "正在总结中...")
                return
            self._set_status(ICON_NORMAL)
            chat_count = len(get_group_chats(group_name))
            if chat_count > BATCH_CONFIRM_CHAT_COUNT:
                self._delayed_run(self._confirm_batch_summary, group_name, chat_count)
                return
            threading.Thread(
                target=self._batch_summarize, args=(group_name,), daemon=True
            ).start()
        return callback

    def _confirm_batch_summary(self, group_name, chat_count):
        self._bring_to_front()
        try:
            batch_limit = self.config.get("batch_msg_limit", 100)
            confirmed = self._confirm_dialog(
                "确认批量总结",
                f"分组「{group_name}」包含 {chat_count} 个群。\n"
                f"当前设置为每群最多 {batch_limit} 条，"
                f"本次最多会读取约 {chat_count * batch_limit} 条消息。\n\n"
                "确定开始吗？",
                ok="开始总结",
            )
            if not confirmed:
                return
            if self._summarizing:
                _notify("微信总结", "请等待", "正在总结中...")
                return
            threading.Thread(
                target=self._batch_summarize, args=(group_name,), daemon=True
            ).start()
        finally:
            self._release_front()

    def _batch_summarize(self, group_name):
        """Batch summarize all chats in a group."""
        self._begin_task(f"批量总结：{group_name}")

        try:
            chat_usernames = get_group_chats(group_name)
            if not chat_usernames:
                _notify("微信总结", group_name, "分组中没有群聊")
                return

            if not self.ai:
                try:
                    self.ai = create_provider(self.config)
                except Exception as e:
                    _notify("微信总结", "AI 未配置", str(e))
                    if "Key" in str(e):
                        self._set_api_key(None)
                    return

            print(f"[batch] 开始批量总结分组「{group_name}」，共 {len(chat_usernames)} 个群...")

            groups_data = []
            total_msgs = 0
            total_remaining = 0

            for username in chat_usernames:
                self._check_cancelled()
                chat_name = self._get_chat_display_name(username)
                since_ts = get_bookmark(username)

                batch_limit = self.config.get("batch_msg_limit", 100)
                total_new_count = self.db.count_messages_since(username, since_ts) if since_ts > 0 else None
                messages = self.db.get_messages(
                    username,
                    since_ts=since_ts,
                    limit=batch_limit,
                    page_forward=since_ts > 0,
                )

                if messages:
                    messages_text = self.db.format_messages_for_ai(messages, show_group_nickname=self.config.get("show_group_nickname", True))
                    start_time = messages[0]["time_str"]
                    end_time = messages[-1]["time_str"]
                    msg_count = len(messages)
                    total_msgs += msg_count
                    if total_new_count is not None:
                        total_remaining += self.db.count_messages_since(username, messages[-1]["timestamp"])

                    groups_data.append({
                        "name": chat_name,
                        "username": username,
                        "messages_text": messages_text,
                        "start_time": start_time,
                        "end_time": end_time,
                        "msg_count": msg_count,
                        "last_msg_ts": messages[-1]["timestamp"],
                    })
                    print(f"[batch]   {chat_name}: {msg_count} 条消息（限 {batch_limit}）")
                else:
                    groups_data.append({
                        "name": chat_name,
                        "username": username,
                        "messages_text": "",
                        "start_time": "",
                        "end_time": "",
                        "msg_count": 0,
                        "last_msg_ts": 0,
                    })
                    print(f"[batch]   {chat_name}: 无新消息")

            if total_msgs == 0:
                _notify("微信总结", group_name, "所有群聊都没有新消息")
                return

            print(f"[batch] 共 {total_msgs} 条消息，正在调用 AI...")
            self._check_cancelled()

            prompt = self.ai.build_batch_prompt(group_name, groups_data)
            summary = self.ai.summarize(prompt)
            self._check_cancelled()

            # Update bookmarks for all chats with messages
            for g in groups_data:
                if g["last_msg_ts"] > 0:
                    set_bookmark(g["username"], g["last_msg_ts"])

            # Record group summary time
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            set_group_summary_time(group_name, now_str)

            # Save summary
            summary_file = self._save_batch_summary(group_name, summary, groups_data, total_msgs)

            # Update menu
            self._last_summary = {
                "group": f"📂 {group_name}",
                "text": summary,
                "file": summary_file,
                "msg_count": total_msgs,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self._run_on_main(self._refresh_menu_after_summary)

            done_message = f"{len(groups_data)}个群 · {total_msgs}条消息已总结"
            if total_remaining > 0:
                done_message += f"，仍有约{total_remaining}条，继续点可接着总结"
            _notify("微信总结", f"✅ {group_name}", done_message)
            print(f"[batch] ✓ 分组「{group_name}」总结完成")

            # Auto-open summary file
            subprocess.run(["open", summary_file])

            self._set_status(ICON_DONE)

        except UserCancelled:
            _notify("微信总结", "已停止", f"分组「{group_name}」批量总结已停止")
            self._set_status(ICON_NORMAL)
        except Exception as e:
            _notify("微信总结", "批量总结失败", str(e))
            traceback.print_exc()
            self._set_status(ICON_ERROR)
        finally:
            self._finish_task()

    def _save_batch_summary(self, group_name, summary, groups_data, total_msgs):
        """Save batch summary."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in group_name)
        filename = f"batch_{safe_name}_{timestamp}.txt"
        filepath = os.path.join(SUMMARY_DIR, filename)

        group_list = ", ".join(g["name"] for g in groups_data)
        header = (
            f"{'='*50}\n"
            f"  📂 分组总结：{group_name}\n"
            f"  包含群聊：{group_list}\n"
            f"  共 {total_msgs} 条消息\n"
            f"  生成时间：{datetime.now().strftime('%Y年%m月%d日 %H:%M')}\n"
            f"{'='*50}\n\n"
        )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(header + summary)
        return filepath

    # ── Keyword search ──────────────────────────────────────

    def _on_search_click(self, _):
        """Search menu item clicked (delayed dialog, let menu close first)."""
        if not self.db:
            _notify("微信总结", "未初始化", "请先确保微信已登录")
            return
        if self._summarizing:
            _notify("微信总结", "请等待", "正在处理中...")
            return
        self._delayed_run(self._show_search_dialog)

    def _show_search_dialog(self):
        """Show keyword search dialog."""
        if not self.db:
            return

        # Get all group chats from contact.db (not limited by session count)
        group_sessions = self.db.get_groups()

        if not group_sessions:
            _notify("微信总结", "暂无群聊", "请先刷新群聊列表")
            return

        self._bring_to_front()
        try:
            if not _HAS_APPKIT:
                # Fallback: single input field
                self._show_search_dialog_fallback(group_sessions)
                return

            # ── PyObjC multi-input dialog ──
            alert = NSAlert.alloc().init()
            if os.path.isfile(APP_ICON_PNG):
                _icon = NSImage.alloc().initWithContentsOfFile_(APP_ICON_PNG)
                if _icon:
                    alert.setIcon_(_icon)
            alert.setMessageText_("🔍 关键词搜索")
            alert.setInformativeText_("多个关键词用空格分隔（布尔与搜索：必须同时出现）")
            alert.addButtonWithTitle_("开始搜索")
            alert.addButtonWithTitle_("取消")

            # Build group chat list text
            group_lines = []
            for i, s in enumerate(group_sessions, 1):
                group_lines.append(f"{i}. {s['name']}")
            groups_text = "\n".join(group_lines)

            # Custom view: input fields + scrollable chat list
            view = NSView.alloc().initWithFrame_(((0, 0), (380, 310)))

            # Row 4 (y=283): Keywords
            lbl_kw = NSTextField.alloc().initWithFrame_(((0, 283), (80, 22)))
            lbl_kw.setStringValue_("关键词：")
            lbl_kw.setBezeled_(False)
            lbl_kw.setEditable_(False)
            lbl_kw.setDrawsBackground_(False)
            view.addSubview_(lbl_kw)

            field_kw = NSTextField.alloc().initWithFrame_(((80, 283), (290, 22)))
            field_kw.setPlaceholderString_("如 claude api")
            view.addSubview_(field_kw)

            # Row 3 (y=253): Start date
            lbl_start = NSTextField.alloc().initWithFrame_(((0, 253), (80, 22)))
            lbl_start.setStringValue_("开始日期：")
            lbl_start.setBezeled_(False)
            lbl_start.setEditable_(False)
            lbl_start.setDrawsBackground_(False)
            view.addSubview_(lbl_start)

            field_start = NSTextField.alloc().initWithFrame_(((80, 253), (290, 22)))
            field_start.setPlaceholderString_("如 2026-03-01")
            view.addSubview_(field_start)

            # Row 2 (y=223): End date
            lbl_end = NSTextField.alloc().initWithFrame_(((0, 223), (80, 22)))
            lbl_end.setStringValue_("结束日期：")
            lbl_end.setBezeled_(False)
            lbl_end.setEditable_(False)
            lbl_end.setDrawsBackground_(False)
            view.addSubview_(lbl_end)

            field_end = NSTextField.alloc().initWithFrame_(((80, 223), (290, 22)))
            field_end.setPlaceholderString_("留空 = 今天")
            view.addSubview_(field_end)

            # Row 1 (y=193): Chat scope
            lbl_scope = NSTextField.alloc().initWithFrame_(((0, 193), (80, 22)))
            lbl_scope.setStringValue_("群聊范围：")
            lbl_scope.setBezeled_(False)
            lbl_scope.setEditable_(False)
            lbl_scope.setDrawsBackground_(False)
            view.addSubview_(lbl_scope)

            field_scope = NSTextField.alloc().initWithFrame_(((80, 193), (290, 22)))
            field_scope.setPlaceholderString_("全部 或 序号如 1,3,5")
            field_scope.setStringValue_("全部")
            view.addSubview_(field_scope)

            # Row 0 (y=163): AI summary checkbox
            checkbox_ai = NSButton.alloc().initWithFrame_(((80, 163), (290, 22)))
            checkbox_ai.setButtonType_(3)  # NSSwitchButton (checkbox)
            checkbox_ai.setTitle_("用 AI 总结搜索结果")
            checkbox_ai.setState_(0)  # Default unchecked
            view.addSubview_(checkbox_ai)

            # Scrollable chat list (fixed height, won't fill the screen)
            lbl_groups = NSTextField.alloc().initWithFrame_(((0, 133), (380, 22)))
            lbl_groups.setStringValue_(f"可选群聊（共 {len(group_sessions)} 个）：")
            lbl_groups.setBezeled_(False)
            lbl_groups.setEditable_(False)
            lbl_groups.setDrawsBackground_(False)
            view.addSubview_(lbl_groups)

            scroll = NSScrollView.alloc().initWithFrame_(((0, 0), (380, 130)))
            scroll.setHasVerticalScroller_(True)
            scroll.setBorderType_(NSBezelBorder)
            text_view = NSTextView.alloc().initWithFrame_(((0, 0), (360, 130)))
            text_view.setEditable_(False)
            text_view.setString_(groups_text)
            text_view.setFont_(NSFont.systemFontOfSize_(11))
            scroll.setDocumentView_(text_view)
            view.addSubview_(scroll)

            alert.setAccessoryView_(view)
            alert.window().setInitialFirstResponder_(field_kw)

            result = alert.runModal()
            if result != 1000:  # NSAlertFirstButtonReturn
                return

            # ── Read input ──
            kw_str = str(field_kw.stringValue()).strip()
            start_str = str(field_start.stringValue()).strip()
            end_str = str(field_end.stringValue()).strip()
            scope_str = str(field_scope.stringValue()).strip()
            use_ai = checkbox_ai.state() == 1

            # ── Validate input ──
            if not kw_str:
                _notify("微信总结", "输入错误", "请输入搜索关键词")
                return

            keywords = kw_str.split()

            # Parse start date
            if not start_str:
                _notify("微信总结", "输入错误", "请输入开始日期")
                return
            try:
                start_ts = datetime.strptime(start_str, "%Y-%m-%d").timestamp()
            except ValueError:
                _notify("微信总结", "日期格式错误", "请使用 YYYY-MM-DD 格式，如 2026-03-01")
                return

            # Parse end date
            if end_str:
                try:
                    # Set end date to 23:59:59 of the day
                    end_ts = datetime.strptime(end_str, "%Y-%m-%d").timestamp() + 86399
                except ValueError:
                    _notify("微信总结", "日期格式错误", "请使用 YYYY-MM-DD 格式，如 2026-03-09")
                    return
            else:
                end_ts = time.time()  # Empty = current time

            if start_ts > end_ts:
                _notify("微信总结", "日期错误", "开始日期不能晚于结束日期")
                return

            # Parse chat scope
            if not scope_str or scope_str == "全部":
                search_usernames = [s["username"] for s in group_sessions]
            else:
                search_usernames = []
                for part in scope_str.replace("，", ",").split(","):
                    try:
                        idx = int(part.strip()) - 1
                        if 0 <= idx < len(group_sessions):
                            search_usernames.append(group_sessions[idx]["username"])
                    except ValueError:
                        pass
                if not search_usernames:
                    _notify("微信总结", "输入错误", "未选择有效群聊，请输入「全部」或群聊序号")
                    return

            # ── Start background search ──
            print(f"[搜索] 关键词={keywords}, 群聊数={len(search_usernames)}, "
                  f"时间={start_str}~{end_str or '今天'}, AI={use_ai}")
            threading.Thread(
                target=self._do_search,
                args=(keywords, kw_str, search_usernames, start_ts, end_ts, use_ai),
                daemon=True,
            ).start()

        except Exception as e:
            print(f"[搜索] ❌ 异常：{e}")
            traceback.print_exc()
        finally:
            self._release_front()

    def _show_search_dialog_fallback(self, group_sessions):
        """Fallback search dialog when AppKit is unavailable."""
        clicked, text = self._input_dialog(
            "🔍 关键词搜索",
            "格式：关键词|开始日期|结束日期\n"
            "例如：claude api|2026-03-01|2026-03-09\n\n"
            "多个关键词用空格分隔（必须同时出现）\n"
            "结束日期留空则为今天\n"
            "将搜索所有群聊，不使用 AI 总结",
            ok="搜索", width=380,
        )
        if not clicked or not text.strip():
            return

        parts = text.strip().split("|")
        if len(parts) < 2:
            _notify("微信总结", "格式错误", "请用 | 分隔关键词和日期")
            return

        kw_str = parts[0].strip()
        keywords = kw_str.split()
        if not keywords:
            _notify("微信总结", "输入错误", "请输入关键词")
            return

        try:
            start_ts = datetime.strptime(parts[1].strip(), "%Y-%m-%d").timestamp()
        except ValueError:
            _notify("微信总结", "日期格式错误", "请使用 YYYY-MM-DD 格式")
            return

        if len(parts) >= 3 and parts[2].strip():
            try:
                end_ts = datetime.strptime(parts[2].strip(), "%Y-%m-%d").timestamp() + 86399
            except ValueError:
                end_ts = time.time()
        else:
            end_ts = time.time()

        search_usernames = [s["username"] for s in group_sessions]

        threading.Thread(
            target=self._do_search,
            args=(keywords, kw_str, search_usernames, start_ts, end_ts, False),
            daemon=True,
        ).start()

    def _do_search(self, keywords, kw_str, usernames, start_ts, end_ts, use_ai):
        """Execute keyword search in background (read-only, does not modify any bookmarks or data)."""
        self._begin_task(f"搜索：{kw_str}")

        try:
            start_display = datetime.fromtimestamp(start_ts).strftime("%m-%d")
            end_display = datetime.fromtimestamp(end_ts).strftime("%m-%d")

            print(f"[搜索] 搜索关键词：{kw_str}，范围：{start_display}~{end_display}，"
                  f"群聊数：{len(usernames)}，AI总结：{use_ai}")

            # Get data coverage range (inform user which chats have how much data)
            coverage = self.db.get_fts_coverage(usernames)
            if coverage:
                for uname in usernames:
                    cov = coverage.get(uname)
                    if cov:
                        e = datetime.fromtimestamp(cov["earliest"]).strftime("%Y-%m-%d")
                        l = datetime.fromtimestamp(cov["latest"]).strftime("%Y-%m-%d")
                        gname = self.db._contacts.get(uname, uname) if self.db._contacts else uname
                        print(f"[搜索]   {gname}: 数据范围 {e} ~ {l} ({cov['count']}条)")

            # Execute search (prefer FTS full-text index, covers all historical data)
            results = self.db.search_messages(keywords, usernames, start_ts, end_ts)

            total_count = sum(len(msgs) for msgs in results.values())

            if total_count == 0:
                # Build data coverage description
                coverage_note = ""
                if coverage:
                    lines = []
                    for uname in usernames:
                        cov = coverage.get(uname)
                        gname = self.db._contacts.get(uname, uname) if self.db._contacts else uname
                        if cov:
                            e = datetime.fromtimestamp(cov["earliest"]).strftime("%Y-%m-%d")
                            lines.append(f"  {gname}: 数据从 {e} 起")
                        else:
                            lines.append(f"  {gname}: 无数据")
                    coverage_note = "\n数据覆盖：\n" + "\n".join(lines)

                print(f"[搜索] ⚠ 搜索完成，未找到包含 {keywords} 的消息")
                _notify("微信总结", "搜索完成 · 0 条结果",
                        f"未找到包含「{kw_str}」的消息")
                self._set_status(ICON_NORMAL)
                return

            print(f"[搜索] 命中 {total_count} 条消息，涉及 {len(results)} 个群")

            if use_ai:
                # AI summary mode
                self._check_cancelled()
                if not self.ai:
                    try:
                        self.ai = create_provider(self.config)
                    except Exception as e:
                        _notify("微信总结", "AI 未配置", str(e))
                        return

                prompt = self.ai.build_search_prompt(kw_str, results, start_display, end_display)
                print(f"[search] 正在调用 AI 总结...")
                summary = self.ai.summarize(prompt)
                self._check_cancelled()

                filepath = self._save_search_result(
                    kw_str, results, total_count, start_display, end_display,
                    ai_summary=summary
                )
            else:
                # Raw text mode
                filepath = self._save_search_result(
                    kw_str, results, total_count, start_display, end_display,
                    ai_summary=None
                )

            # Update latest summary display
            self._last_summary = {
                "group": f"🔍 搜索：{kw_str}",
                "text": summary if use_ai else f"搜索「{kw_str}」命中 {total_count} 条消息",
                "file": filepath,
                "msg_count": total_count,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self._run_on_main(self._refresh_menu_after_summary)

            _notify("微信总结", f"🔍 搜索完成", f"「{kw_str}」命中 {total_count} 条消息")
            print(f"[搜索] ✓ 搜索完成，结果已保存")

            subprocess.run(["open", filepath])
            self._set_status(ICON_DONE)

        except UserCancelled:
            _notify("微信总结", "已停止", f"搜索「{kw_str}」已停止")
            self._set_status(ICON_NORMAL)
        except Exception as e:
            _notify("微信总结", "搜索失败", str(e))
            traceback.print_exc()
            self._set_status(ICON_ERROR)
        finally:
            self._finish_task()
            # Safety net: ensure icon doesn't get stuck on ⏳
            if self.title == ICON_LOADING:
                self._set_status(ICON_NORMAL)

    def _save_search_result(self, kw_str, results, total_count, start_display, end_display, ai_summary=None):
        """Save search results to file (does not modify any bookmarks)."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_kw = "".join(c if c.isalnum() or c in "._-" else "_" for c in kw_str)

        if ai_summary:
            filename = f"search_ai_{safe_kw}_{timestamp}.txt"
        else:
            filename = f"search_{safe_kw}_{timestamp}.txt"

        filepath = os.path.join(SUMMARY_DIR, filename)

        group_count = len(results)
        mode_label = "AI总结" if ai_summary else "原文"

        # Calculate actual data range for each chat
        actual_ranges = []
        for username, messages in results.items():
            if messages:
                group_name = messages[0]["group_name"]
                earliest = min(m["timestamp"] for m in messages)
                latest = max(m["timestamp"] for m in messages)
                e = datetime.fromtimestamp(earliest).strftime("%m-%d")
                l = datetime.fromtimestamp(latest).strftime("%m-%d")
                actual_ranges.append(f"    {group_name}: {e} ~ {l} ({len(messages)}条)")

        header = (
            f"{'='*50}\n"
            f"  🔍 关键词搜索（{mode_label}）：{kw_str}\n"
            f"  时间范围：{start_display} ~ {end_display}\n"
            f"  搜索群聊：{group_count} 个群\n"
            f"  命中消息：{total_count} 条\n"
            f"  生成时间：{datetime.now().strftime('%Y年%m月%d日 %H:%M')}\n"
        )
        if actual_ranges:
            header += "  各群命中范围：\n" + "\n".join(actual_ranges) + "\n"
        header += f"{'='*50}\n\n"

        if ai_summary:
            content = header + ai_summary
        else:
            # Raw text mode: display grouped by chat
            parts = []
            for username, messages in results.items():
                group_name = messages[0]["group_name"] if messages else username
                count = len(messages)
                lines = [f"--- 📌 {group_name}（{count}条命中）---\n"]
                for msg in messages:
                    if msg["sender"]:
                        lines.append(f"[{msg['time_str']}] {msg['sender']}: {msg['text']}")
                    else:
                        lines.append(f"[{msg['time_str']}] {msg['text']}")
                parts.append("\n".join(lines))
            content = header + "\n\n".join(parts) + "\n"

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return filepath

    # ── Menu bar buttons ─────────────────────────────────────

    @rumps.clicked("刷新群聊列表")
    def refresh_groups(self, _):
        if not self.db:
            _notify("微信总结", "未初始化", "请先确保微信已登录")
            return
        self._set_status(ICON_LOADING)
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self):
        try:
            self._run_on_main(self._rebuild_chat_menu)
            _notify("微信总结", "刷新完成", "群聊列表已更新")
        except Exception as e:
            _notify("微信总结", "刷新失败", str(e))
        finally:
            self._set_status(ICON_NORMAL)

    @rumps.clicked("🔄 刷新数据源")
    def reextract_keys(self, _):
        print("[keys] 点击🔄 刷新数据源")
        if not is_wechat_running():
            print("[keys] ✗ 微信未运行")
            _notify("微信总结", "微信未运行", "请先启动微信并登录")
            return
        if not is_wechat_signed():
            print("[keys] ✗ 微信未签名")
            _notify("微信总结", "微信需要重新授权", _wechat_signing_message())
            return
        print("[keys] 开始刷新数据源...")
        threading.Thread(target=self._do_reextract, daemon=True).start()

    def _do_reextract(self):
        self._set_status(ICON_LOADING)
        _notify("微信总结", "正在刷新数据源", "需要管理员权限...")
        try:
            keys = extract_keys()
            print(f"[keys] extract_keys 返回: {len(keys) if keys else 0} 个密钥")
            if keys:
                self.db = WeChatDB.for_runtime(self.config["db_dir"], keys)
                self._run_on_main(self._rebuild_chat_menu)
                _notify("微信总结", "数据源刷新成功", f"已同步 {len(keys)} 个数据库")
            else:
                _notify("微信总结", "刷新失败", _wechat_signing_message())
        except Exception as e:
            print(f"[keys] ✗ 刷新异常: {e}")
            traceback.print_exc()
            _notify("微信总结", "刷新失败", str(e))
        self._set_status(ICON_NORMAL)


if __name__ == "__main__":
    print("微信总结 启动中...")
    try:
        with AppInstanceLock():
            WeGroupchatObsidianApp().run()
    except AppAlreadyRunning:
        print("[app] canonical menu app is already running")
        raise SystemExit(2)
