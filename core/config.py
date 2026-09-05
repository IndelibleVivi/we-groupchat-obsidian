"""Configuration management - app config and WeChat data path detection."""
import json
import os
import re
import shlex
import stat
import tempfile
import uuid

from .project_identity import DATA_DIR_NAME, LEGACY_DATA_DIR_NAME
from .platform import LockMode, create_file_lock
from .taxonomy_assignment import FREE_FORM_PROFILE

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.expanduser(
    os.environ.get("WE_GROUPCHAT_OBSIDIAN_DATA_DIR", f"~/{DATA_DIR_NAME}")
)
LEGACY_DATA_DIR = os.path.expanduser(f"~/{LEGACY_DATA_DIR_NAME}")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
LEGACY_DATA_CONFIG_FILE = os.path.join(LEGACY_DATA_DIR, "config.json")
LEGACY_CONFIG_FILE = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "config_revision": 0,
    "db_dir": "",
    "keys_file": os.path.join(DATA_DIR, "all_keys.json"),
    "decrypted_dir": os.path.join(DATA_DIR, "decrypted"),
    "ai_provider": "qwen",  # Options: qwen, ollama, deepseek, claude, openai, custom
    "ai_model": "",          # Empty uses default model; API key stored in macOS Keychain
    "ollama_url": "http://localhost:11434",
    "ollama_model": "qwen3:8b",
    "auto_refresh_on_open": False,
    "ai_base_url": "",
    "image_aes_key": "",
    "show_group_nickname": True,
    "batch_msg_limit": 100,
    "hide_inactive_months": 1,
    "monitor_enabled": False,
    "monitor_interval_minutes": 3,
    "monitor_chat_username": "",
    "monitor_chat_display_name": "",
    "monitor_chats": [],
    "monitor_chat_aliases": {},
    "monitor_chat_taxonomy_profiles": {},
    "monitor_topic": "",
    "monitor_max_messages_per_run": 200,
    "monitor_context_overlap_minutes": 12,
    "monitor_context_max_messages": 80,
    "monitor_cooldown_minutes": 15,
    # Inert compatibility key: the in-process remote preview path is retired.
    "monitor_fetch_links": False,
    "background_notifications_enabled": True,
    "monitor_notify_writes": True,
    "monitor_notify_checkins": False,
    "monitor_ai_provider": "",
    "monitor_ai_model": "",
    "monitor_ai_timeout_seconds": 90,
    "monitor_ai_retry_attempts": 1,
    "monitor_ai_retry_delay_seconds": 3,
    "monitor_ai_failure_backoff_minutes": 10,
    "monitor_knowledge_enabled": True,
    "monitor_knowledge_db": os.path.join(DATA_DIR, "monitor_knowledge.db"),
    "monitor_obsidian_root": os.path.join(DATA_DIR, "obsidian_knowledge"),
    "monitor_obsidian_subdir": "微信群聊/关注推送",
    "daily_digest_enabled": True,
    "daily_digest_notify": True,
    "daily_digest_time": "21:30",
    "daily_digest_timezone": "Asia/Shanghai",
    "daily_digest_dir": "",
    "wechat_source_guard_enabled": False,
    "wechat_source_guard_grace_seconds": 300,
    "wechat_source_guard_interval_seconds": 300,
    "wechat_source_guard_restart_budget": 3,
    "wechat_source_guard_restart_window_seconds": 1800,
    "wechat_source_guard_backoff_base_seconds": 300,
    "wechat_source_guard_stale_seconds": 7200,
    "wechat_source_guard_notification_cooldown_seconds": 3600,
    "wechat_source_guard_pause_until": "",
    "attachment_archive_enabled": False,
    "attachment_archive_kinds": ["file"],
    "attachment_archive_root": os.path.join(DATA_DIR, "attachment_archive"),
    "attachment_archive_max_object_bytes": 512 * 1024 * 1024,
    "attachment_archive_min_free_bytes": 1024 * 1024 * 1024,
    "attachment_archive_retry_base_seconds": 300,
    "attachment_archive_retry_max_seconds": 6 * 60 * 60,
    "attachment_backup_target": "",
    "resource_backup_selected_chats": [],
    "resource_backup_enabled": False,
    "resource_backup_interval_seconds": 300,
    "resource_backup_max_messages_per_scan": 500,
    "resource_backup_min_free_bytes": 1024 * 1024 * 1024,
    "google_drive_file_sync_enabled": False,
    "google_drive_file_sync_paused": False,
    "google_drive_file_sync_selected_chats": [],
    "google_drive_file_sync_interval_seconds": 300,
    "google_drive_file_sync_max_messages_per_scan": 500,
    "google_drive_file_sync_max_uploads_per_run": 20,
    "google_drive_file_sync_max_bytes_per_run": 512 * 1024 * 1024,
    "google_drive_file_sync_root_name": "微信群文件归档",
    "google_drive_file_sync_keep_local_objects": True,
    "google_drive_file_sync_db": os.path.join(DATA_DIR, "google_drive_file_sync.db"),
    "google_drive_file_sync_retry_base_seconds": 300,
    "google_drive_file_sync_retry_max_seconds": 6 * 60 * 60,
    "mcp_enable_send_message": False,
    "mcp_send_mode": "disabled",
    "mcp_send_allowlist": [],
}


class ConfigError(RuntimeError):
    """Content-free configuration storage failure."""

    def __init__(self, code):
        super().__init__(str(code))
        self.code = str(code)


class ConfigConflictError(ConfigError):
    """A stale whole-document writer attempted to replace newer config."""


def ensure_private_dir(path=DATA_DIR):
    """Create a local data directory and restrict it to the current user."""
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def ensure_private_file(path):
    """Best-effort private permissions for local config/cache metadata."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _read_json_strict(path):
    try:
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ConfigError("config_not_regular")
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except ConfigError:
        raise
    except (json.JSONDecodeError, OSError) as exc:
        raise ConfigError("config_corrupt") from exc
    if not isinstance(value, dict):
        raise ConfigError("config_corrupt")
    return value


def normalize_path_value(value):
    """Normalize a user-entered path, including shell-escaped Finder/Terminal paths."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "\\" in text:
        try:
            parts = shlex.split(text)
            if len(parts) == 1:
                text = parts[0]
        except ValueError:
            text = text.replace("\\ ", " ").replace("\\~", "~")
    return os.path.expanduser(text)


def _rebase_legacy_data_path(value):
    text = normalize_path_value(value)
    if not text:
        return ""
    legacy_root = os.path.abspath(os.path.expanduser(LEGACY_DATA_DIR))
    data_root = os.path.abspath(os.path.expanduser(DATA_DIR))
    abs_text = os.path.abspath(text)
    if abs_text == legacy_root:
        return data_root
    legacy_prefix = legacy_root + os.sep
    if abs_text.startswith(legacy_prefix):
        return os.path.join(data_root, abs_text[len(legacy_prefix):])
    return text


def _sanitize_chat_map(value, *, require_chatroom_key=True):
    result = {}
    if not isinstance(value, dict):
        return result
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            continue
        key = raw_key.strip()
        item = raw_value.strip()
        if not key or not item:
            continue
        if require_chatroom_key and not key.endswith("@chatroom"):
            continue
        result[key] = item
    return result


def _sanitize_monitor_chat_list(value):
    clean_chats = []
    seen = set()
    if not isinstance(value, list):
        return clean_chats
    for item in value:
        if not isinstance(item, dict):
            continue
        username = item.get("username")
        name = item.get("name")
        if not isinstance(username, str) or not username.strip():
            continue
        username = username.strip()
        if username in seen:
            continue
        seen.add(username)
        clean_chats.append({
            "username": username,
            "name": name.strip() if isinstance(name, str) and name.strip() else username,
        })
    return clean_chats


def _sanitize_drive_chat_list(value):
    clean_chats = []
    seen = set()
    if not isinstance(value, list):
        return clean_chats
    for item in value:
        if not isinstance(item, dict):
            continue
        username = item.get("username")
        alias = item.get("alias")
        if not isinstance(username, str) or not username.strip().endswith("@chatroom"):
            continue
        username = username.strip()
        if username in seen:
            continue
        seen.add(username)
        alias = alias.strip() if isinstance(alias, str) else ""
        clean_chats.append({"username": username, "alias": alias})
    return clean_chats


def _sanitize_resource_chat_list(value):
    clean_chats = _sanitize_drive_chat_list(value)
    selected_since = {}
    selection_ids = {}
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username") or "").strip()
            try:
                stamp = max(0, int(item.get("selected_since") or 0))
            except (TypeError, ValueError):
                stamp = 0
            if username and stamp and username not in selected_since:
                selected_since[username] = stamp
            selection_id = str(item.get("selection_id") or "").strip()
            if username and selection_id and username not in selection_ids:
                try:
                    selection_ids[username] = str(uuid.UUID(selection_id))
                except ValueError:
                    pass
    for chat in clean_chats:
        stamp = selected_since.get(chat["username"], 0)
        if stamp:
            chat["selected_since"] = stamp
        if chat["username"] in selection_ids:
            chat["selection_id"] = selection_ids[chat["username"]]
    return clean_chats


def selected_drive_sync_chats(config: dict) -> list[dict]:
    """Return private selected-chat identities and stable user-facing aliases."""
    config = config if isinstance(config, dict) else {}
    return _sanitize_drive_chat_list(config.get("google_drive_file_sync_selected_chats"))


def selected_resource_backup_chats(config: dict) -> list[dict]:
    """Return the mounted-backup disclosure selection, independent of OAuth sync."""
    config = config if isinstance(config, dict) else {}
    return _sanitize_resource_chat_list(config.get("resource_backup_selected_chats"))


def active_monitor_chats(config: dict) -> list[dict]:
    """Return sanitized active chats, preserving the supported v1 singleton form."""
    config = config if isinstance(config, dict) else {}
    chats = _sanitize_monitor_chat_list(config.get("monitor_chats"))
    if chats:
        return chats
    username = str(config.get("monitor_chat_username") or "").strip()
    if not username:
        return []
    name = str(config.get("monitor_chat_display_name") or "").strip() or username
    return [{"username": username, "name": name}]


def _sanitize_config(saved):
    cfg = dict(DEFAULT_CONFIG)
    if not isinstance(saved, dict):
        return cfg

    revision = saved.get("config_revision")
    if isinstance(revision, int) and revision >= 0:
        cfg["config_revision"] = revision

    for key in (
        "ai_provider", "ai_model", "ollama_url", "ollama_model",
        "ai_base_url", "monitor_chat_username", "monitor_chat_display_name",
        "monitor_topic", "monitor_ai_provider", "monitor_ai_model",
        "monitor_knowledge_db", "monitor_obsidian_root", "monitor_obsidian_subdir",
        "daily_digest_time", "daily_digest_timezone", "daily_digest_dir",
        "wechat_source_guard_pause_until",
        "google_drive_file_sync_root_name", "google_drive_file_sync_db",
        "image_aes_key",
    ):
        value = saved.get(key)
        if isinstance(value, str):
            cfg[key] = value

    monitor_chats = saved.get("monitor_chats")
    if isinstance(monitor_chats, list):
        cfg["monitor_chats"] = _sanitize_monitor_chat_list(monitor_chats)

    selected_drive_chats = saved.get("google_drive_file_sync_selected_chats")
    if isinstance(selected_drive_chats, list):
        cfg["google_drive_file_sync_selected_chats"] = _sanitize_drive_chat_list(
            selected_drive_chats
        )

    selected_resource_chats = saved.get("resource_backup_selected_chats")
    if isinstance(selected_resource_chats, list):
        cfg["resource_backup_selected_chats"] = _sanitize_resource_chat_list(
            selected_resource_chats
        )

    cfg["monitor_chat_aliases"] = _sanitize_chat_map(saved.get("monitor_chat_aliases"))
    cfg["monitor_chat_taxonomy_profiles"] = _sanitize_chat_map(
        saved.get("monitor_chat_taxonomy_profiles")
    )

    for key in (
        "auto_refresh_on_open", "show_group_nickname", "monitor_enabled",
        "monitor_knowledge_enabled",
        "background_notifications_enabled",
        "monitor_notify_writes", "monitor_notify_checkins",
        "daily_digest_enabled", "daily_digest_notify",
        "wechat_source_guard_enabled", "attachment_archive_enabled",
        "resource_backup_enabled",
        "google_drive_file_sync_enabled", "google_drive_file_sync_paused",
        "google_drive_file_sync_keep_local_objects",
        "mcp_enable_send_message",
    ):
        value = saved.get(key)
        if isinstance(value, bool):
            cfg[key] = value

    # Old configs may contain monitor_fetch_links=true. Never revive the
    # retired network path while loading or rewriting those files.
    cfg["monitor_fetch_links"] = False

    # Legacy MCP send keys remain loadable for old config files, but are inert.
    # In particular, never translate the retired boolean into an active mode.
    mode = str(saved.get("mcp_send_mode") or cfg["mcp_send_mode"]).strip().lower()
    if mode in {"disabled", "dry_run", "allowlist", "enabled"}:
        cfg["mcp_send_mode"] = mode

    allowlist = saved.get("mcp_send_allowlist")
    if isinstance(allowlist, list):
        cfg["mcp_send_allowlist"] = [
            item.strip()
            for item in allowlist
            if isinstance(item, str) and item.strip()
        ]

    int_ranges = {
        "batch_msg_limit": (1, 5000),
        "hide_inactive_months": (0, 60),
        "monitor_interval_minutes": (1, 1440),
        "monitor_max_messages_per_run": (1, 1000),
        "monitor_context_overlap_minutes": (0, 120),
        "monitor_context_max_messages": (0, 300),
        "monitor_cooldown_minutes": (0, 1440),
        "monitor_ai_timeout_seconds": (10, 300),
        "monitor_ai_retry_attempts": (0, 3),
        "monitor_ai_retry_delay_seconds": (0, 60),
        "monitor_ai_failure_backoff_minutes": (1, 1440),
        "wechat_source_guard_grace_seconds": (0, 86400),
        "wechat_source_guard_interval_seconds": (60, 86400),
        "wechat_source_guard_restart_budget": (1, 20),
        "wechat_source_guard_restart_window_seconds": (60, 86400),
        "wechat_source_guard_backoff_base_seconds": (1, 86400),
        "wechat_source_guard_stale_seconds": (60, 604800),
        "wechat_source_guard_notification_cooldown_seconds": (60, 604800),
        "attachment_archive_max_object_bytes": (1024 * 1024, 10 * 1024 * 1024 * 1024),
        "attachment_archive_min_free_bytes": (0, 1024 * 1024 * 1024 * 1024),
        "attachment_archive_retry_base_seconds": (1, 86400),
        "attachment_archive_retry_max_seconds": (1, 604800),
        "resource_backup_interval_seconds": (60, 86400),
        "resource_backup_max_messages_per_scan": (1, 5000),
        "resource_backup_min_free_bytes": (0, 1024 * 1024 * 1024 * 1024),
        "google_drive_file_sync_interval_seconds": (60, 86400),
        "google_drive_file_sync_max_messages_per_scan": (1, 5000),
        "google_drive_file_sync_max_uploads_per_run": (1, 1000),
        "google_drive_file_sync_max_bytes_per_run": (1024 * 1024, 100 * 1024 * 1024 * 1024),
        "google_drive_file_sync_retry_base_seconds": (1, 86400),
        "google_drive_file_sync_retry_max_seconds": (1, 604800),
    }
    for key, (min_value, max_value) in int_ranges.items():
        value = saved.get(key)
        if isinstance(value, int) and min_value <= value <= max_value:
            cfg[key] = value

    db_dir = normalize_path_value(saved.get("db_dir", ""))
    if isinstance(db_dir, str):
        cfg["db_dir"] = db_dir

    keys_file = _rebase_legacy_data_path(saved.get("keys_file", ""))
    if isinstance(keys_file, str) and keys_file:
        cfg["keys_file"] = keys_file

    decrypted_dir = _rebase_legacy_data_path(saved.get("decrypted_dir", ""))
    if isinstance(decrypted_dir, str) and decrypted_dir:
        cfg["decrypted_dir"] = decrypted_dir

    monitor_obsidian_root = _rebase_legacy_data_path(saved.get("monitor_obsidian_root", ""))
    if monitor_obsidian_root:
        cfg["monitor_obsidian_root"] = monitor_obsidian_root

    monitor_knowledge_db = _rebase_legacy_data_path(saved.get("monitor_knowledge_db", ""))
    if monitor_knowledge_db:
        cfg["monitor_knowledge_db"] = monitor_knowledge_db

    daily_digest_dir = normalize_path_value(saved.get("daily_digest_dir", ""))
    if daily_digest_dir:
        cfg["daily_digest_dir"] = daily_digest_dir

    pause_until = str(saved.get("wechat_source_guard_pause_until") or "").strip()
    if pause_until == "indefinite" or re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})",
        pause_until,
    ):
        cfg["wechat_source_guard_pause_until"] = pause_until
    else:
        cfg["wechat_source_guard_pause_until"] = ""

    kinds = saved.get("attachment_archive_kinds")
    if isinstance(kinds, list):
        cfg["attachment_archive_kinds"] = list(dict.fromkeys(
            item.strip().lower()
            for item in kinds
            if isinstance(item, str) and item.strip().lower() in {"file", "image"}
        )) or ["file"]

    attachment_archive_root = _rebase_legacy_data_path(saved.get("attachment_archive_root", ""))
    if attachment_archive_root:
        cfg["attachment_archive_root"] = attachment_archive_root

    attachment_backup_target = normalize_path_value(saved.get("attachment_backup_target", ""))
    if attachment_backup_target:
        cfg["attachment_backup_target"] = attachment_backup_target

    drive_sync_db = _rebase_legacy_data_path(saved.get("google_drive_file_sync_db", ""))
    if drive_sync_db:
        cfg["google_drive_file_sync_db"] = drive_sync_db

    root_name = str(saved.get("google_drive_file_sync_root_name") or "").strip()
    if root_name and len(root_name) <= 120 and not any(ord(char) < 32 for char in root_name):
        cfg["google_drive_file_sync_root_name"] = root_name

    # This release never deletes shared CAS objects. Keep the persisted policy honest.
    cfg["google_drive_file_sync_keep_local_objects"] = True

    return cfg


class ConfigStore:
    """Locked, revisioned, atomic JSON configuration store.

    Patch updates reload the canonical document while holding an exclusive
    platform file lock. Whole-document replacement remains available for
    first-run and migration code, but rejects stale revisions instead of losing
    an unrelated concurrent update.
    """

    def __init__(self, path=None, *, file_lock=None):
        self.path = os.path.abspath(os.path.expanduser(path or CONFIG_FILE))
        self.lock_path = self.path + ".lock"
        self._file_lock = file_lock

    def _lock_service(self):
        if self._file_lock is None:
            self._file_lock = create_file_lock()
        return self._file_lock

    def _lock(self, exclusive):
        ensure_private_dir(os.path.dirname(self.path))
        return self._lock_service().acquire(
            self.lock_path,
            mode=LockMode.EXCLUSIVE if exclusive else LockMode.SHARED,
            blocking=True,
        )

    @staticmethod
    def _unlock(lock_handle):
        lock_handle.close()

    def _read_locked(self):
        if not os.path.lexists(self.path):
            return None
        return _sanitize_config(_read_json_strict(self.path))

    def read(self):
        fd = self._lock(False)
        try:
            return self._read_locked()
        finally:
            self._unlock(fd)

    def _write_locked(self, value):
        directory = os.path.dirname(self.path)
        ensure_private_dir(directory)
        temp_fd, temp_path = tempfile.mkstemp(
            prefix=".config-", suffix=".json", dir=directory
        )
        try:
            fchmod = getattr(os, "fchmod", None)
            if callable(fchmod):
                try:
                    fchmod(temp_fd, 0o600)
                except OSError:
                    pass
            with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
                temp_fd = -1
                json.dump(value, handle, indent=4, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            temp_path = ""
            ensure_private_file(self.path)
            try:
                directory_fd = os.open(directory, os.O_RDONLY)
            except OSError:
                directory_fd = -1
            if directory_fd >= 0:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    pass
                finally:
                    os.close(directory_fd)
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def update(self, mutator):
        if not callable(mutator):
            raise TypeError("mutator must be callable")
        fd = self._lock(True)
        try:
            current = self._read_locked()
            missing = current is None
            if current is None:
                current = _load_initial_config_without_primary()
            working = dict(current)
            result = mutator(working)
            if result is not None:
                if not isinstance(result, dict):
                    raise TypeError("config mutator must return dict or None")
                working = result
            normalized = _sanitize_config(working)
            current_revision = int(current.get("config_revision") or 0)
            normalized["config_revision"] = current_revision
            if not missing and normalized == current:
                return current
            normalized["config_revision"] = current_revision + 1
            self._write_locked(normalized)
            return normalized
        finally:
            self._unlock(fd)

    def replace(self, value, *, expected_revision=None):
        fd = self._lock(True)
        try:
            current = self._read_locked()
            current_revision = int((current or {}).get("config_revision") or 0)
            if (
                current is not None
                and expected_revision is not None
                and int(expected_revision) != current_revision
            ):
                raise ConfigConflictError("config_revision_conflict")
            normalized = _sanitize_config(value)
            normalized["config_revision"] = current_revision
            if current is not None and normalized == current:
                return current
            normalized["config_revision"] = current_revision + 1
            self._write_locked(normalized)
            return normalized
        finally:
            self._unlock(fd)


def merge_monitor_chat_preferences(
    config: dict,
    groups: list[dict],
    *,
    profile_by_username: dict[str, str] | None = None,
    alias_by_username: dict[str, str] | None = None,
) -> dict:
    aliases = dict(config.get("monitor_chat_aliases") or {})
    profiles = dict(config.get("monitor_chat_taxonomy_profiles") or {})
    profile_by_username = profile_by_username or {}
    alias_by_username = alias_by_username or {}
    for group in groups:
        username = str(group.get("username") or "").strip()
        name = str(group.get("name") or "").strip()
        if not username.endswith("@chatroom"):
            raise ValueError("selected chat lacks a stable @chatroom username")
        aliases.setdefault(username, alias_by_username.get(username) or name or username)
        if username in profile_by_username:
            profile = str(profile_by_username[username] or "").strip()
            if profile:
                profiles[username] = profile
            else:
                profiles[username] = FREE_FORM_PROFILE
    updated = dict(config)
    updated["monitor_chat_aliases"] = aliases
    updated["monitor_chat_taxonomy_profiles"] = profiles
    return updated


def _load_initial_config_without_primary():
    legacy_saved = _read_json(LEGACY_DATA_CONFIG_FILE)
    if legacy_saved is not None:
        return _sanitize_config(legacy_saved)

    legacy = _read_json(LEGACY_CONFIG_FILE)
    if legacy is None:
        return dict(DEFAULT_CONFIG)

    return _sanitize_config(legacy)


def _load_saved_config():
    stored = ConfigStore().read()
    if stored is not None:
        return stored
    return ConfigStore().update(lambda current: current)


def auto_detect_db_dir():
    """Auto-detect macOS WeChat database path."""
    bases = [
        os.path.expanduser(
            "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
        ),
        os.path.expanduser(
            "~/Library/Containers/com.tencent.xinWeChat/Data/Documents"
        ),
        os.path.expanduser(
            "~/Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support"
        ),
    ]

    candidates = []
    seen = set()
    for base in bases:
        if not os.path.isdir(base):
            continue

        for root, dirs, _files in os.walk(base):
            for dirname in dirs:
                if dirname != "db_storage":
                    continue
                storage = os.path.join(root, dirname)
                if storage in seen:
                    continue
                seen.add(storage)
                candidates.append(storage)

    if not candidates:
        return None

    preferred = []
    for path in candidates:
        score = 0
        if "/xwechat_files/" in path.replace("\\", "/"):
            score += 2
        if os.path.isfile(os.path.join(path, "contact", "contact.db")):
            score += 2
        if os.path.isfile(os.path.join(path, "session", "session.db")):
            score += 2
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        preferred.append((score, mtime, path))

    preferred.sort(reverse=True)
    return preferred[0][2]


def load_config():
    """Load config and install an auto-detected source only when still unset.

    Detection deliberately happens outside the config lock because walking the
    WeChat container can be slow.  The conditional install happens inside the
    locked mutator, so a concurrent explicit choice always wins.  A non-empty
    path remains authoritative even while its volume/container is unavailable.
    """
    ensure_private_dir(DATA_DIR)

    cfg = _load_saved_config()
    if not cfg["db_dir"]:
        detected = auto_detect_db_dir()
        if detected:
            def install_if_still_unset(current):
                if not str(current.get("db_dir") or "").strip():
                    current["db_dir"] = detected
                return current

            cfg = update_config(mutator=install_if_still_unset)

    return cfg


def update_config(mutator=None, *, patch=None):
    """Atomically update only the requested config fields."""
    changes = dict(patch or {})

    def apply(current):
        current.update(changes)
        if mutator is None:
            return current
        return mutator(current)

    return ConfigStore().update(apply)


def save_config(cfg):
    """Atomically replace config, rejecting a stale whole-document writer."""
    value = dict(cfg or {})
    expected_revision = value.get("config_revision")
    return ConfigStore().replace(value, expected_revision=expected_revision)
