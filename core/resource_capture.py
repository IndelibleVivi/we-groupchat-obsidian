"""Deterministic selected-chat resources and opt-in private visible messages.

This module deliberately separates source capture from every backup transport.
A source cursor advances only in the same SQLite transaction that durably records
all link/file occurrences and opted-in contexts found in that source page.
Page receipts distinguish raw EOF from inventory completeness. File bytes are resolved
later through the shared AttachmentArchive CAS; link identity is the exact URL
string observed in the WeChat message, not an AI-produced summary.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import random
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .attachment_archive import AttachmentArchive
from .config import (
    DATA_DIR,
    active_monitor_chats,
    load_config,
    selected_resource_backup_chats,
    update_config,
)
from .url_safety import URL_RE
from .source_adapter import (
    SourceUnavailableError,
    bind_source_inventory,
    normalize_source_error,
    read_source_page,
    source_snapshot,
    source_state_evidence,
)


SCHEMA_VERSION = 4
BACKFILL_PAGE_SIZE = 1_000
BACKFILL_RUN_TTL_SECONDS = 24 * 60 * 60
LINK_ID_DOMAIN = b"we-groupchat-resource-link-v1\0"
CHAT_ID_DOMAIN = "we-groupchat-resource-chat-v1\0"
RETRYABLE_FILE_STATES = (
    "waiting_cache",
    "insufficient_local_space",
    "retry_wait",
)


class ResourceCaptureError(RuntimeError):
    """Privacy-safe resource-capture failure with a stable error code."""

    def __init__(self, code: str):
        super().__init__(str(code))
        self.code = str(code)


def _capture_db_path(config):
    value = (config or {}).get("resource_capture_db") or os.path.join(
        DATA_DIR, "resource_capture.db"
    )
    return os.path.abspath(os.path.expanduser(value))


def resource_capture_db_path(config):
    """Return the canonical capture-ledger path without opening it."""
    return _capture_db_path(config)


@contextmanager
def resource_capture_operation_lock(config):
    """Serialize capture operations and selected-chat config mutations.

    The lock order is always capture operation lock -> config store lock.  UI
    and CLI selection writers use this surface before patching config, while a
    capture worker reloads canonical config only after acquiring the same lock.
    """
    db_path = _capture_db_path(config)
    os.makedirs(os.path.dirname(db_path), mode=0o700, exist_ok=True)
    lock_path = db_path + ".capture.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise ResourceCaptureError("capture_worker_busy") from exc
            raise
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _safe_label(value, fallback="未命名群聊", limit=180):
    text = "".join(" " if ord(char) < 32 else char for char in str(value or ""))
    text = text.replace("/", "／").replace(":", "：").strip().strip(".")
    text = re.sub(r"\s+", " ", text)
    return (text[:limit] or fallback)


def _month(timestamp):
    return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m")


def _exact_links(text):
    """Return exact HTTP(S) strings in source order, deduplicated byte-for-byte."""
    links = []
    seen = set()
    for match in URL_RE.finditer(str(text or "")):
        url = match.group(0)
        if url and url not in seen:
            seen.add(url)
            links.append(url)
    return links


def _link_title(text, url):
    """Recover the title from WeChat's deterministic ``[链接] title URL`` form."""
    value = str(text or "").strip()
    if not value.startswith("[链接]"):
        return ""
    value = value[len("[链接]"):].strip()
    position = value.find(url)
    if position >= 0:
        value = value[:position].strip()
    return _safe_label(value, "", limit=180)


def _url_sha256(url):
    return hashlib.sha256(LINK_ID_DOMAIN + str(url).encode("utf-8")).hexdigest()


def eligible_selected_chats(config):
    """Return only chats that are both monitored and explicitly backup-selected.

    The intersection is the external-disclosure boundary.  A chat selected in an
    obsolete Drive configuration but no longer active in monitor configuration is
    intentionally excluded.
    """
    config = config if isinstance(config, dict) else {}
    active = {
        str(chat.get("username") or "").strip(): str(chat.get("name") or "").strip()
        for chat in active_monitor_chats(config)
        if str(chat.get("username") or "").strip()
    }
    aliases = config.get("monitor_chat_aliases")
    aliases = aliases if isinstance(aliases, dict) else {}
    result = []
    for index, selected in enumerate(selected_resource_backup_chats(config), 1):
        username = str(selected.get("username") or "").strip()
        if username not in active:
            continue
        fallback = f"未命名群聊 {index}"
        alias = ""
        for candidate in (
            selected.get("alias"), aliases.get(username), active[username]
        ):
            value = str(candidate or "").strip()
            if value and "@chatroom" not in value.casefold():
                alias = value
                break
        result.append({
            "username": username,
            "alias": _safe_label(alias, fallback),
            "selected_since": max(0, int(selected.get("selected_since") or 0)),
            "selection_id": str(selected.get("selection_id") or ""),
        })
    return result


def resource_backup_chat_candidates(config):
    """Return active chats in stable config order with privacy-safe labels."""
    config = config if isinstance(config, dict) else {}
    aliases = config.get("monitor_chat_aliases")
    aliases = aliases if isinstance(aliases, dict) else {}
    result = []
    for chat in active_monitor_chats(config):
        username = str(chat.get("username") or "").strip()
        if not username:
            continue
        fallback = f"未命名群聊 {len(result) + 1}"
        alias = (
            str(aliases.get(username) or "").strip()
            or str(chat.get("name") or "").strip()
        )
        if not alias or alias.endswith("@chatroom"):
            alias = fallback
        result.append({"username": username, "alias": _safe_label(alias, fallback)})
    return result


class SelectedResourceCapture:
    """Capture resources and opted-in visible contexts into one private ledger."""

    def __init__(
        self,
        config,
        *,
        source=None,
        now_func=time.time,
        random_func=random.random,
        archive_id_factory=None,
        backfill_run_id_factory=None,
        config_loader=None,
        capture_contexts=None,
    ):
        self.config = dict(config or {})
        self.source = source
        self.now_func = now_func
        self.random_func = random_func
        self.archive_id_factory = archive_id_factory or (lambda: str(uuid.uuid4()))
        self.backfill_run_id_factory = backfill_run_id_factory or (
            lambda: str(uuid.uuid4())
        )
        self.config_loader = config_loader
        self.capture_contexts = capture_contexts
        self.db_path = _capture_db_path(self.config)
        self.archive_root = os.path.abspath(os.path.expanduser(
            self.config.get("attachment_archive_root")
            or os.path.join(DATA_DIR, "attachment_archive")
        ))
        self.archive = AttachmentArchive(
            self.config.get("monitor_knowledge_db") or os.path.join(DATA_DIR, "monitor_knowledge.db"),
            self.archive_root,
            db_dir=self.config.get("db_dir") or "",
            archive_kinds=("file",),
            max_object_bytes=self.config.get("attachment_archive_max_object_bytes", 512 * 1024 * 1024),
            min_free_bytes=self.config.get("attachment_archive_min_free_bytes", 1024 * 1024 * 1024),
            retry_base_seconds=self.config.get("attachment_archive_retry_base_seconds", 300),
            retry_max_seconds=self.config.get("attachment_archive_retry_max_seconds", 6 * 60 * 60),
            now_func=now_func,
        )
        self._operation_depth = 0
        self._operation_owner = None
        self._initialized = False
        self._archive_id = ""

    @classmethod
    def from_config(cls, config, **kwargs):
        kwargs.setdefault("config_loader", load_config)
        return cls(config, **kwargs)

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _operation_lock(self):
        """Own the canonical selection and ledger for one complete operation."""
        owner = threading.get_ident()
        if self._operation_depth and self._operation_owner == owner:
            yield
            return
        with resource_capture_operation_lock(self.config):
            self._operation_depth = 1
            self._operation_owner = owner
            try:
                self._reload_canonical_config_locked()
                self._ensure_initialized_locked()
                yield
            finally:
                self._operation_depth = 0
                self._operation_owner = None

    def _owns_operation(self):
        return bool(
            self._operation_depth
            and self._operation_owner == threading.get_ident()
        )

    @contextmanager
    def canonical_operation(self):
        """Hold canonical selection authority across a downstream operation."""
        with self._operation_lock():
            yield self

    def _ensure_initialized_locked(self):
        if self._initialized:
            return
        self._ensure_schema()
        self._archive_id = self._ensure_archive_id()
        self._initialized = True

    @property
    def archive_id(self):
        if not self._initialized:
            with self._operation_lock():
                pass
        return self._archive_id

    def _reload_canonical_config_locked(self):
        if self.config_loader is None:
            return self.config
        latest = dict(self.config_loader() or {})
        latest_db_path = _capture_db_path(latest)
        latest_archive_root = os.path.abspath(os.path.expanduser(
            latest.get("attachment_archive_root")
            or os.path.join(DATA_DIR, "attachment_archive")
        ))
        if latest_db_path != self.db_path or latest_archive_root != self.archive_root:
            raise ResourceCaptureError("capture_config_changed")
        source_db_dir = str(getattr(self.source, "db_dir", "") or "")
        latest_source_db_dir = str(latest.get("db_dir") or "")
        if source_db_dir and (
            os.path.abspath(os.path.expanduser(source_db_dir))
            != os.path.abspath(os.path.expanduser(latest_source_db_dir))
        ):
            raise ResourceCaptureError("capture_config_changed")
        self.config = latest
        return self.config

    def _ensure_schema(self):
        os.makedirs(os.path.dirname(self.db_path), mode=0o700, exist_ok=True)
        try:
            os.chmod(os.path.dirname(self.db_path), 0o700)
        except OSError:
            pass
        conn = self._connect()
        try:
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if user_version > SCHEMA_VERSION:
                raise ResourceCaptureError("schema_too_new")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS resource_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS resource_chats (
                    chat_username TEXT PRIMARY KEY,
                    chat_key TEXT NOT NULL,
                    chat_alias TEXT NOT NULL,
                    start_timestamp INTEGER NOT NULL,
                    selected_since INTEGER NOT NULL DEFAULT 0,
                    selection_id TEXT NOT NULL DEFAULT '',
                    selection_epoch INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS resource_shards (
                    chat_username TEXT NOT NULL,
                    source_shard_id TEXT NOT NULL,
                    cursor_timestamp INTEGER NOT NULL,
                    cursor_message_ids_json TEXT NOT NULL DEFAULT '[]',
                    source_cursor_token TEXT NOT NULL DEFAULT '',
                    source_state TEXT NOT NULL DEFAULT 'healthy',
                    last_error_code TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(chat_username, source_shard_id),
                    FOREIGN KEY(chat_username) REFERENCES resource_chats(chat_username)
                );

                CREATE TABLE IF NOT EXISTS resource_occurrences (
                    occurrence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_username TEXT NOT NULL,
                    chat_key TEXT NOT NULL,
                    chat_alias TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    resource_index INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    source_timestamp INTEGER NOT NULL,
                    source_month TEXT NOT NULL,
                    source_time TEXT NOT NULL DEFAULT '',
                    source_sender TEXT NOT NULL DEFAULT '',
                    original_name TEXT NOT NULL DEFAULT '',
                    observed_url TEXT NOT NULL DEFAULT '',
                    url_sha256 TEXT NOT NULL DEFAULT '',
                    declared_size INTEGER,
                    declared_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    resolution_method TEXT NOT NULL DEFAULT '',
                    object_sha256 TEXT NOT NULL DEFAULT '',
                    object_size INTEGER,
                    object_relpath TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(chat_username, source_message_id, kind, resource_index),
                    CHECK(kind IN ('link', 'file'))
                );
                CREATE INDEX IF NOT EXISTS idx_resource_occurrences_chat_time
                    ON resource_occurrences(chat_key, source_timestamp, occurrence_id);
                CREATE INDEX IF NOT EXISTS idx_resource_occurrences_status
                    ON resource_occurrences(status, next_retry_at, occurrence_id);
                CREATE INDEX IF NOT EXISTS idx_resource_occurrences_object
                    ON resource_occurrences(object_sha256, occurrence_id);

                CREATE TABLE IF NOT EXISTS resource_contexts (
                    chat_username TEXT NOT NULL,
                    chat_key TEXT NOT NULL,
                    chat_alias TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    source_timestamp INTEGER NOT NULL,
                    source_time TEXT NOT NULL,
                    message_type INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    PRIMARY KEY(chat_username, source_message_id)
                );
                CREATE TABLE IF NOT EXISTS resource_context_history (
                    chat_key TEXT NOT NULL,
                    selection_id TEXT NOT NULL,
                    from_timestamp INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    inventory_digest TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    PRIMARY KEY(chat_key, selection_id, from_timestamp, origin)
                );

                CREATE TABLE IF NOT EXISTS resource_backfill_runs (
                    run_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    from_timestamp INTEGER NOT NULL,
                    selected_chat_digest TEXT NOT NULL,
                    inventory_digest TEXT NOT NULL DEFAULT '',
                    source_manifest_digest TEXT NOT NULL DEFAULT '',
                    candidate_digest TEXT NOT NULL DEFAULT '',
                    source_complete INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    scanned INTEGER NOT NULL DEFAULT 0,
                    discovered_links INTEGER NOT NULL DEFAULT 0,
                    discovered_files INTEGER NOT NULL DEFAULT 0,
                    inserted_links INTEGER NOT NULL DEFAULT 0,
                    inserted_files INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    applied_at REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS resource_backfill_staged_occurrences (
                    run_id TEXT NOT NULL,
                    chat_username TEXT NOT NULL,
                    chat_key TEXT NOT NULL,
                    chat_alias TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    resource_index INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    source_timestamp INTEGER NOT NULL,
                    source_month TEXT NOT NULL,
                    source_time TEXT NOT NULL DEFAULT '',
                    source_sender TEXT NOT NULL DEFAULT '',
                    original_name TEXT NOT NULL DEFAULT '',
                    observed_url TEXT NOT NULL DEFAULT '',
                    url_sha256 TEXT NOT NULL DEFAULT '',
                    declared_size INTEGER,
                    declared_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    candidate_sha256 TEXT NOT NULL,
                    PRIMARY KEY(
                        run_id, chat_username, source_message_id, kind,
                        resource_index
                    ),
                    FOREIGN KEY(run_id) REFERENCES resource_backfill_runs(run_id)
                        ON DELETE CASCADE,
                    CHECK(kind IN ('link', 'file'))
                );
                CREATE INDEX IF NOT EXISTS idx_resource_backfill_stage_run
                    ON resource_backfill_staged_occurrences(
                        run_id, candidate_sha256
                    );
                CREATE TABLE IF NOT EXISTS resource_backfill_staged_contexts (
                    run_id TEXT NOT NULL,
                    chat_username TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    PRIMARY KEY(run_id, chat_username, source_message_id),
                    FOREIGN KEY(run_id) REFERENCES resource_backfill_runs(run_id)
                        ON DELETE CASCADE
                );
                """
            )
            conn.execute("BEGIN IMMEDIATE")
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(resource_chats)")
            }
            if "selected_since" not in columns:
                conn.execute(
                    "ALTER TABLE resource_chats "
                    "ADD COLUMN selected_since INTEGER NOT NULL DEFAULT 0"
                )
            if "selection_epoch" not in columns:
                conn.execute(
                    "ALTER TABLE resource_chats "
                    "ADD COLUMN selection_epoch INTEGER NOT NULL DEFAULT 1"
                )
            if "selection_id" not in columns:
                conn.execute(
                    "ALTER TABLE resource_chats "
                    "ADD COLUMN selection_id TEXT NOT NULL DEFAULT ''"
                )
            if "context_history_gap" not in columns:
                conn.execute(
                    "ALTER TABLE resource_chats "
                    "ADD COLUMN context_history_gap INTEGER NOT NULL DEFAULT 0"
                )
                # Old cursors consumed text that was never retained. Even zero
                # resource rows cannot prove that those pages had no messages.
                conn.execute("""
                    UPDATE resource_chats SET context_history_gap = 1
                    WHERE chat_username IN (SELECT chat_username FROM resource_shards)
                       OR chat_username IN (SELECT chat_username FROM resource_occurrences)
                """)
            shard_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(resource_shards)")
            }
            if "source_cursor_token" not in shard_columns:
                conn.execute(
                    "ALTER TABLE resource_shards "
                    "ADD COLUMN source_cursor_token TEXT NOT NULL DEFAULT ''"
                )
            backfill_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(resource_backfill_runs)")
            }
            if "inventory_digest" not in backfill_columns:
                conn.execute(
                    "ALTER TABLE resource_backfill_runs "
                    "ADD COLUMN inventory_digest TEXT NOT NULL DEFAULT ''"
                )
            for column in ("discovered_contexts", "inserted_contexts"):
                if column not in backfill_columns:
                    conn.execute(
                        f"ALTER TABLE resource_backfill_runs ADD COLUMN {column} "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
            if "includes_contexts" not in backfill_columns:
                conn.execute("ALTER TABLE resource_backfill_runs "
                             "ADD COLUMN includes_contexts INTEGER NOT NULL DEFAULT 0")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _meta_get(self, key, default=""):
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM resource_meta WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default
        finally:
            conn.close()

    def _meta_set(self, key, value):
        self._meta_set_many({key: value})

    def _meta_set_many(self, values):
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT INTO resource_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(str(key), str(value)) for key, value in values.items()],
            )
            conn.commit()
        finally:
            conn.close()

    def _record_source_inventory_evidence(
        self,
        binding,
        *,
        degraded_shards=None,
        error_code="",
    ):
        self._meta_set_many({
            **source_state_evidence(
                binding,
                degraded_shards=degraded_shards,
                error_code=error_code,
            ),
            "last_scan_at": self.now_func(),
        })

    def source_inventory_evidence(self):
        """Return path-free source completeness evidence for backup snapshots."""
        raw = self._meta_get("source_inventory_evidence", "")
        try:
            value = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            value = {}
        if not isinstance(value, dict) or not value:
            return {
                "schema": "we-groupchat-obsidian.source-inventory.v1",
                "inventory_revision": 0,
                "inventory_digest": "",
                "complete": False,
                "counts": {},
                "error_codes": ["source_inventory_uninitialized"],
            }
        return value

    def _ensure_archive_id(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM resource_meta WHERE key = 'archive_id'"
            ).fetchone()
            if row is not None:
                conn.commit()
                value = str(row["value"] or "")
                try:
                    return str(uuid.UUID(value))
                except ValueError as exc:
                    raise ResourceCaptureError("archive_id_invalid") from exc
            has_identity_bound_data = any(
                int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("resource_chats", "resource_occurrences")
            )
            if has_identity_bound_data:
                raise ResourceCaptureError("archive_identity_missing")
            candidate = str(self.archive_id_factory())
            try:
                candidate = str(uuid.UUID(candidate))
            except ValueError as exc:
                raise ResourceCaptureError("archive_id_invalid") from exc
            conn.execute(
                "INSERT OR IGNORE INTO resource_meta(key, value) VALUES ('archive_id', ?)",
                (candidate,),
            )
            row = conn.execute(
                "SELECT value FROM resource_meta WHERE key = 'archive_id'"
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        value = str(row["value"] if row else "")
        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise ResourceCaptureError("archive_id_invalid") from exc

    def _chat_key(self, username):
        return hashlib.sha256(
            f"{CHAT_ID_DOMAIN}{self.archive_id}\0{username}".encode("utf-8")
        ).hexdigest()

    def _selected_chats_locked(self):
        return [
            {
                "username": chat["username"],
                "alias": chat["alias"],
                "chat_key": self._chat_key(chat["username"]),
                "selected_since": int(chat.get("selected_since") or 0),
                "selection_id": str(chat.get("selection_id") or ""),
            }
            for chat in eligible_selected_chats(self.config)
        ]

    def selected_chats(self):
        with self._operation_lock():
            return self._selected_chats_locked()

    def selected_chat_keys(self):
        return [chat["chat_key"] for chat in self.selected_chats()]

    def initialize_selected_chat_cursors(self, start_timestamp=None):
        try:
            with self._operation_lock():
                self._reload_canonical_config_locked()
                return self._initialize_selected_chat_cursors_locked(start_timestamp)
        except ResourceCaptureError as exc:
            if exc.code == "capture_worker_busy":
                return {
                    "state": "worker_busy",
                    "selected_chats": 0,
                    "new_chats": 0,
                    "reselected_chats": 0,
                    "error_code": exc.code,
                }
            raise

    def _initialize_selected_chat_cursors_locked(self, start_timestamp=None):
        default_start = int(
            self.now_func() if start_timestamp is None else start_timestamp
        )
        chats = self._selected_chats_locked()
        inserted = 0
        reselected = 0
        conn = self._connect()
        try:
            for chat in chats:
                existing = conn.execute(
                    "SELECT * FROM resource_chats WHERE chat_username = ?",
                    (chat["username"],),
                ).fetchone()
                selected_since = int(chat.get("selected_since") or 0)
                selection_id = str(chat.get("selection_id") or "")
                start = (
                    default_start
                    if start_timestamp is not None or not selected_since
                    else selected_since
                )
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO resource_chats(
                            chat_username, chat_key, chat_alias, start_timestamp,
                            selected_since, selection_id, selection_epoch, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                        """,
                        (
                            chat["username"], chat["chat_key"], chat["alias"],
                            start, selected_since, selection_id, self.now_func(),
                        ),
                    )
                    inserted += 1
                else:
                    existing_selection_id = str(existing["selection_id"] or "")
                    existing_selected_since = int(existing["selected_since"] or 0)
                    adopt_legacy_selection_id = bool(
                        selection_id
                        and not existing_selection_id
                        and selected_since == existing_selected_since
                    )
                    epoch_changed = bool(
                        (
                            selection_id
                            and not adopt_legacy_selection_id
                            and existing_selection_id != selection_id
                        )
                        or (
                            not selection_id
                            and selected_since
                            and existing_selected_since != selected_since
                        )
                    )
                    if adopt_legacy_selection_id:
                        conn.execute(
                            """
                            UPDATE resource_chats
                            SET chat_key = ?, chat_alias = ?, selection_id = ?,
                                updated_at = ?
                            WHERE chat_username = ?
                            """,
                            (
                                chat["chat_key"], chat["alias"], selection_id,
                                self.now_func(), chat["username"],
                            ),
                        )
                    elif epoch_changed:
                        conn.execute(
                            """
                            UPDATE resource_chats
                            SET chat_key = ?, chat_alias = ?, start_timestamp = ?,
                                selected_since = ?, selection_id = ?,
                                selection_epoch = selection_epoch + 1,
                                updated_at = ?
                            WHERE chat_username = ?
                            """,
                            (
                                chat["chat_key"], chat["alias"], start,
                                selected_since, selection_id, self.now_func(),
                                chat["username"],
                            ),
                        )
                        conn.execute(
                            "DELETE FROM resource_shards WHERE chat_username = ?",
                            (chat["username"],),
                        )
                        reselected += 1
                    else:
                        conn.execute(
                            """
                            UPDATE resource_chats
                            SET chat_key = ?, chat_alias = ?, updated_at = ?
                            WHERE chat_username = ?
                            """,
                            (
                                chat["chat_key"], chat["alias"], self.now_func(),
                                chat["username"],
                            ),
                        )
                conn.execute(
                    """
                    UPDATE resource_occurrences
                    SET chat_key = ?, chat_alias = ?, updated_at = ?
                    WHERE chat_username = ?
                    """,
                    (chat["chat_key"], chat["alias"], self.now_func(), chat["username"]),
                )
            conn.commit()
        finally:
            conn.close()
        return {
            "state": "initialized",
            "selected_chats": len(chats),
            "new_chats": inserted,
            "reselected_chats": reselected,
            "start_timestamp": default_start,
        }

    def _chat_row(self, username):
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT * FROM resource_chats WHERE chat_username = ?", (username,)
            ).fetchone()
        finally:
            conn.close()

    def _shard_state(self, chat_row, source_shard_id):
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO resource_shards(
                    chat_username, source_shard_id, cursor_timestamp,
                    cursor_message_ids_json, source_state, last_error_code, updated_at
                ) VALUES (?, ?, ?, '[]', 'healthy', '', ?)
                """,
                (
                    chat_row["chat_username"], source_shard_id,
                    int(chat_row["start_timestamp"]), self.now_func(),
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM resource_shards
                WHERE chat_username = ? AND source_shard_id = ?
                """,
                (chat_row["chat_username"], source_shard_id),
            ).fetchone()
            conn.commit()
            return row
        finally:
            conn.close()

    def _mark_shard_degraded(self, username, source_shard_id, code):
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE resource_shards
                SET source_state = 'source_degraded', last_error_code = ?, updated_at = ?
                WHERE chat_username = ? AND source_shard_id = ?
                """,
                (code, self.now_func(), username, source_shard_id),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _cursor_after(messages, old_timestamp, old_ids):
        if not messages:
            return old_timestamp, set(old_ids)
        newest = max(int(message.get("timestamp") or 0) for message in messages)
        identities = {
            str(message.get("source_message_id") or "")
            for message in messages
            if int(message.get("timestamp") or 0) == newest
        }
        if newest == old_timestamp:
            identities.update(old_ids)
        identities.discard("")
        return newest, identities

    @staticmethod
    def _inserted(cursor):
        return max(0, int(cursor.rowcount or 0))

    @staticmethod
    def _candidate_sha256(candidate):
        payload = json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _normalized_occurrence_candidates(
        self,
        chat,
        messages,
        *,
        include_links=True,
        include_files=True,
    ):
        for message in messages:
            source_message_id = str(message.get("source_message_id") or "").strip()
            timestamp = int(message.get("timestamp") or 0)
            if not source_message_id or not timestamp:
                continue
            source_time = str(message.get("time_str") or "")[:40]
            sender = str(
                message.get("sender") or message.get("group_nickname") or ""
            )[:80]
            month = _month(timestamp)
            text = str(message.get("text") or message.get("content") or "")

            for index, url in enumerate(_exact_links(text) if include_links else ()):
                yield {
                    "chat_username": chat["username"],
                    "chat_key": chat["chat_key"],
                    "chat_alias": chat["alias"],
                    "source_message_id": source_message_id,
                    "resource_index": index,
                    "kind": "link",
                    "source_timestamp": timestamp,
                    "source_month": month,
                    "source_time": source_time,
                    "source_sender": sender,
                    "original_name": _link_title(text, url),
                    "observed_url": url,
                    "url_sha256": _url_sha256(url),
                    "declared_size": None,
                    "declared_hash": "",
                    "status": "ready_metadata",
                }

            resources = (message.get("resources") or []) if include_files else ()
            for fallback_index, resource in enumerate(resources):
                if (
                    not isinstance(resource, dict)
                    or str(resource.get("kind") or "") != "file"
                ):
                    continue
                try:
                    resource_index = max(
                        0, int(resource.get("resource_index", fallback_index))
                    )
                except (TypeError, ValueError):
                    resource_index = fallback_index
                declared_size = resource.get("declared_size")
                if declared_size is not None:
                    try:
                        declared_size = max(0, int(declared_size))
                    except (TypeError, ValueError):
                        declared_size = None
                yield {
                    "chat_username": chat["username"],
                    "chat_key": chat["chat_key"],
                    "chat_alias": chat["alias"],
                    "source_message_id": source_message_id,
                    "resource_index": resource_index,
                    "kind": "file",
                    "source_timestamp": timestamp,
                    "source_month": month,
                    "source_time": source_time,
                    "source_sender": sender,
                    "original_name": _safe_label(
                        resource.get("original_name"), "attachment"
                    ),
                    "observed_url": "",
                    "url_sha256": "",
                    "declared_size": declared_size,
                    "declared_hash": str(
                        resource.get("declared_hash") or resource.get("md5") or ""
                    ).lower()[:128],
                    "status": "queued",
                }

    @staticmethod
    def _insert_normalized_occurrence(conn, candidate, now):
        return conn.execute(
            """
            INSERT OR IGNORE INTO resource_occurrences(
                chat_username, chat_key, chat_alias, source_message_id,
                resource_index, kind, source_timestamp, source_month,
                source_time, source_sender, original_name, observed_url,
                url_sha256, declared_size, declared_hash, status,
                created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                candidate["chat_username"], candidate["chat_key"],
                candidate["chat_alias"], candidate["source_message_id"],
                candidate["resource_index"], candidate["kind"],
                candidate["source_timestamp"], candidate["source_month"],
                candidate["source_time"], candidate["source_sender"],
                candidate["original_name"], candidate["observed_url"],
                candidate["url_sha256"], candidate["declared_size"],
                candidate["declared_hash"], candidate["status"], now, now,
            ),
        )

    def _insert_occurrences(
        self,
        conn,
        chat,
        messages,
        now,
        *,
        include_links=True,
        include_files=True,
    ):
        captured_links = 0
        captured_files = 0
        candidates = self._normalized_occurrence_candidates(
            chat,
            messages,
            include_links=include_links,
            include_files=include_files,
        )
        for candidate in candidates:
            inserted = self._inserted(
                self._insert_normalized_occurrence(conn, candidate, now)
            )
            if candidate["kind"] == "link":
                captured_links += inserted
            else:
                captured_files += inserted
        return captured_links, captured_files

    @staticmethod
    def _context_candidates(chat, messages):
        for message in messages:
            text = str(message.get("text") or message.get("content") or "")
            identity = str(message.get("source_message_id") or "").strip()
            timestamp = int(message.get("timestamp") or 0)
            if not identity or not timestamp or not text.strip():
                continue
            yield {
                "chat_username": chat["username"], "chat_key": chat["chat_key"],
                "chat_alias": chat["alias"], "source_message_id": identity,
                "source_timestamp": timestamp,
                "source_time": str(message.get("time_str") or ""),
                "message_type": int(message.get("type") or 0), "text": text,
            }

    def _captures_contexts(self):
        return bool(self.config.get("quiet_archive_handoff_enabled", False)
                    if self.capture_contexts is None else self.capture_contexts)

    @staticmethod
    def _insert_context(conn, candidate):
        return conn.execute("""
            INSERT INTO resource_contexts(
                chat_username, chat_key, chat_alias, source_message_id,
                source_timestamp, source_time, message_type, text
            ) VALUES (:chat_username, :chat_key, :chat_alias, :source_message_id,
                      :source_timestamp, :source_time, :message_type, :text)
            ON CONFLICT(chat_username, source_message_id) DO UPDATE SET
                chat_key=excluded.chat_key, chat_alias=excluded.chat_alias,
                source_timestamp=excluded.source_timestamp,
                source_time=excluded.source_time, message_type=excluded.message_type,
                text=excluded.text
            WHERE resource_contexts.text <> excluded.text
               OR resource_contexts.chat_alias <> excluded.chat_alias
               OR resource_contexts.source_timestamp <> excluded.source_timestamp
               OR resource_contexts.source_time <> excluded.source_time
               OR resource_contexts.message_type <> excluded.message_type
        """, candidate)

    def _observed_at(self):
        return datetime.fromtimestamp(self.now_func(), tz=timezone.utc).isoformat()

    @staticmethod
    def _write_scan_receipt(conn, receipt):
        conn.execute(
            "INSERT INTO resource_meta(key, value) VALUES ('context_scan_receipt', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(receipt, ensure_ascii=False, sort_keys=True),),
        )

    def _history_range(self, conn, chat, from_timestamp, digest, origin):
        conn.execute("""
            INSERT INTO resource_context_history(
                chat_key, selection_id, from_timestamp, observed_at,
                inventory_digest, origin
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_key, selection_id, from_timestamp, origin)
            DO UPDATE SET observed_at=excluded.observed_at,
                          inventory_digest=excluded.inventory_digest
        """, (chat["chat_key"], chat["selection_id"], int(from_timestamp),
              self._observed_at(), digest, origin))

    def contexts(self):
        """Selected private visible messages; projection owns URL redaction."""
        with self._operation_lock():
            chats = self._selected_chats_locked()
            aliases = {chat["chat_key"]: chat["alias"] for chat in chats}
            if not aliases:
                return []
            conn = self._connect()
            try:
                rows = [dict(row) for row in conn.execute(
                    "SELECT * FROM resource_contexts WHERE chat_key IN ({}) "
                    "ORDER BY chat_key, source_timestamp, source_message_id".format(
                        ",".join("?" for _ in aliases)
                    ), list(aliases),
                )]
                for row in rows:
                    row["chat_alias"] = aliases[row["chat_key"]]
                return rows
            finally:
                conn.close()

    def context_coverage(self):
        """Local durable evidence only; never opens or refreshes the source."""
        with self._operation_lock():
            chats = self._selected_chats_locked()
            keys = [chat["chat_key"] for chat in chats]
            selection = [{key: chat[key] for key in (
                "chat_key", "selection_id", "selected_since"
            )} for chat in chats]
            raw = self._meta_get("context_scan_receipt", "")
            receipt = json.loads(raw) if raw else {}
            if receipt.get("selection") != selection:
                receipt = {}
            inventory = receipt.get("inventory") or {
                "revision": 0, "digest": "", "complete": False,
                "counts": {}, "error_codes": ["source_not_observed"],
            }
            scan = receipt.get("source_scan") or {
                "state": "not_started", "raw_eof": False,
                "raw_rows_scanned": 0, "visible_messages_captured": 0,
                "pending_shards": 0, "failed_shards": 0, "shards": [],
            }
            total = gaps = missing = 0
            ranges = []
            conn = self._connect()
            try:
                if keys:
                    placeholders = ",".join("?" for _ in keys)
                    total = conn.execute(
                        f"SELECT COUNT(*) FROM resource_contexts WHERE chat_key IN ({placeholders})",
                        keys,
                    ).fetchone()[0]
                    gaps = conn.execute(
                        f"SELECT COUNT(*) FROM resource_chats WHERE chat_key IN ({placeholders}) "
                        "AND context_history_gap=1", keys,
                    ).fetchone()[0]
                    missing = conn.execute(f"""
                        SELECT COUNT(*) FROM (
                            SELECT DISTINCT r.chat_key, r.source_message_id
                            FROM resource_occurrences r
                            WHERE r.chat_key IN ({placeholders}) AND NOT EXISTS (
                                SELECT 1 FROM resource_contexts c
                                WHERE c.chat_key=r.chat_key AND c.source_message_id=r.source_message_id
                            )
                        )
                    """, keys).fetchone()[0]
                    ranges = [dict(row) for row in conn.execute(
                        f"SELECT * FROM resource_context_history WHERE chat_key IN ({placeholders}) "
                        "ORDER BY chat_key, from_timestamp, origin", keys,
                    )]
            finally:
                conn.close()
            return {
                "schema": "we-groupchat-obsidian.quiet-archive.coverage.v1",
                "capture_run_id": receipt.get("capture_run_id", ""),
                "observed_at": receipt.get("observed_at", ""),
                "selection": selection, "inventory": inventory, "source_scan": scan,
                "contexts": {
                    "total_messages": total, "history_gap_chat_count": gaps,
                    "legacy_resource_messages_without_context": missing,
                    "unmeasured_history_gap": bool(gaps), "history_ranges": ranges,
                },
                "errors": receipt.get("errors", []),
            }

    def drain(self, *, max_rounds=10, max_seconds=480):
        """Explicit bounded catch-up, using the same scanner and operation lock."""
        results = []
        started = time.monotonic()
        with self._operation_lock():
            for _ in range(max(1, int(max_rounds))):
                result = self.scan()
                results.append(result)
                if result.get("raw_eof") or result.get("state") != "healthy":
                    break
                if time.monotonic() - started >= max(0, float(max_seconds)):
                    break
        return {"state": "eof" if results[-1].get("raw_eof") else (
            "pending" if results[-1].get("state") == "healthy" else results[-1]["state"]
        ), "rounds": len(results), "scan": results[-1], "coverage": self.context_coverage()}

    def scan(self):
        try:
            with self._operation_lock():
                self._reload_canonical_config_locked()
                with source_snapshot(self.source):
                    return self._scan_locked()
        except ResourceCaptureError as exc:
            if exc.code == "capture_worker_busy":
                return {
                    "state": "worker_busy",
                    "scanned": 0,
                    "captured_links": 0,
                    "captured_files": 0,
                    "error_code": exc.code,
                }
            raise

    def _scan_locked(self):
        chats = self.selected_chats()
        if not chats:
            return {"state": "no_selected_chats", "scanned": 0,
                    "captured_links": 0, "captured_files": 0, "raw_eof": False}
        binding = bind_source_inventory(self.source, chats)
        receipt = {
            "capture_run_id": str(uuid.uuid4()), "observed_at": self._observed_at(),
            "selection": [{key: chat[key] for key in (
                "chat_key", "selection_id", "selected_since"
            )} for chat in chats],
            "inventory": {
                "revision": int(binding.get("inventory_revision") or 0),
                "digest": str(binding.get("inventory_digest") or ""),
                "complete": bool(binding.get("complete")),
                "counts": dict(binding.get("counts") or {}),
                "error_codes": list(binding.get("error_codes") or []),
            },
            "source_scan": {"state": "pending", "raw_eof": False,
                            "raw_rows_scanned": 0, "visible_messages_captured": 0,
                            "pending_shards": 0, "failed_shards": 0, "shards": []},
            "errors": [],
        }
        scan = receipt["source_scan"]
        def save_receipt():
            conn = self._connect()
            try:
                self._write_scan_receipt(conn, receipt)
                conn.commit()
            finally:
                conn.close()
        if self.source is None:
            scan["state"] = "failed"
            receipt["errors"] = ["source_unavailable"]
            save_receipt()
            self._record_source_inventory_evidence(binding)
            return {"state": "source_unavailable", "scanned": 0,
                    "captured_links": 0, "captured_files": 0,
                    "error_code": "source_unavailable", "source_complete": False,
                    "inventory_digest": "", "inventory_revision": 0,
                    "source_counts": {}, "source_error_codes": ["source_unavailable"],
                    "raw_eof": False}
        initialized = self._initialize_selected_chat_cursors_locked()["new_chats"]
        max_messages = max(1, int(self.config.get("resource_backup_max_messages_per_scan", 500)))
        captured_links = captured_files = 0
        degraded_shards = int(binding.get("degraded_shards") or 0)
        source_error_code = str(binding.get("error_code") or "")
        work = []
        for chat in chats:
            chat_row = self._chat_row(chat["username"])
            if chat_row is None:
                raise ResourceCaptureError("chat_state_missing")
            for shard_id in (binding.get("shards_by_username") or {}).get(chat["username"], []):
                shard = self._shard_state(chat_row, shard_id)
                evidence = {
                    "chat_key": chat["chat_key"], "source_generation_id": shard_id,
                    "cursor_before": str(shard["source_cursor_token"] or ""),
                    "cursor_after": str(shard["source_cursor_token"] or ""),
                    "raw_rows_scanned": 0, "raw_eof": False,
                    "state": "pending", "error_code": "",
                }
                scan["shards"].append(evidence)
                work.append((chat, shard, evidence))
        scan["pending_shards"] = len(work)
        save_receipt()
        try:
            for chat, shard, evidence in work:
                shard_id = evidence["source_generation_id"]
                cursor_timestamp = int(shard["cursor_timestamp"])
                seen_ids = set(json.loads(shard["cursor_message_ids_json"] or "[]"))
                try:
                    messages, next_cursor_token, exhausted = read_source_page(
                        self.source, chat["username"], shard_id,
                        cursor_token=evidence["cursor_before"],
                        cursor_timestamp=cursor_timestamp, seen_ids=seen_ids, limit=max_messages,
                    )
                    if not messages and not exhausted:
                        raise SourceUnavailableError("source_cursor_stalled")
                    if messages and callable(getattr(self.source, "get_cursor_page_for_shard", None)) and next_cursor_token == evidence["cursor_before"]:
                        raise SourceUnavailableError("source_cursor_stalled")
                except SourceUnavailableError as exc:
                    code = normalize_source_error(exc)
                    self._mark_shard_degraded(chat["username"], shard_id, code)
                    degraded_shards += 1
                    source_error_code = code
                    evidence.update(state="failed", error_code=code)
                    scan["failed_shards"] += 1
                    scan["pending_shards"] -= 1
                    receipt["errors"].append(code)
                    save_receipt()
                    continue
                new_timestamp, new_ids = self._cursor_after(messages, cursor_timestamp, seen_ids)
                if callable(getattr(self.source, "get_cursor_page_for_shard", None)):
                    new_ids = set()
                candidates = list(self._context_candidates(chat, messages)) if self._captures_contexts() else []
                conn = self._connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    links, files = self._insert_occurrences(conn, chat, messages, self.now_func())
                    for candidate in candidates:
                        self._insert_context(conn, candidate)
                    if not self._captures_contexts() and any(
                        str(message.get("text") or message.get("content") or "").strip()
                        for message in messages
                    ):
                        conn.execute("UPDATE resource_chats SET context_history_gap=1 "
                                     "WHERE chat_username=?", (chat["username"],))
                    updated = conn.execute("""
                        UPDATE resource_shards SET cursor_timestamp=?, cursor_message_ids_json=?,
                            source_cursor_token=?, source_state='healthy', last_error_code='', updated_at=?
                        WHERE chat_username=? AND source_shard_id=?
                          AND cursor_timestamp=? AND source_cursor_token=?
                    """, (new_timestamp, json.dumps(sorted(new_ids)), next_cursor_token,
                          self.now_func(), chat["username"], shard_id, cursor_timestamp,
                          evidence["cursor_before"]))
                    if int(updated.rowcount or 0) != 1:
                        raise ResourceCaptureError("source_cursor_changed")
                    evidence.update(cursor_after=next_cursor_token, raw_rows_scanned=len(messages),
                                    raw_eof=bool(exhausted), state="eof" if exhausted else "pending")
                    scan["raw_rows_scanned"] += len(messages)
                    scan["visible_messages_captured"] += len(candidates)
                    if exhausted:
                        scan["pending_shards"] -= 1
                    self._write_scan_receipt(conn, receipt)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    # Recover only committed page evidence before recording failure.
                    receipt = json.loads(self._meta_get("context_scan_receipt"))
                    scan = receipt["source_scan"]
                    raise
                finally:
                    conn.close()
                captured_links += links
                captured_files += files
            final_binding = bind_source_inventory(self.source, chats)
            if (not final_binding.get("complete") or
                    final_binding.get("inventory_digest") != binding.get("inventory_digest")):
                degraded_shards += 1
                source_error_code = str(final_binding.get("error_code") or "source_generation_changed")
                receipt["errors"].append(source_error_code)
            scan["raw_eof"] = bool(binding.get("complete") and work and not degraded_shards
                                    and not scan["pending_shards"] and not scan["failed_shards"])
            scan["state"] = "degraded" if degraded_shards else ("eof" if scan["raw_eof"] else "pending")
            conn = self._connect()
            try:
                self._write_scan_receipt(conn, receipt)
                if scan["raw_eof"] and self._captures_contexts():
                    for chat in chats:
                        row = self._chat_row(chat["username"])
                        if not row["context_history_gap"]:
                            self._history_range(conn, chat, row["start_timestamp"],
                                                binding["inventory_digest"], "incremental")
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            scan["state"] = "failed"
            scan["raw_eof"] = False
            receipt["errors"].append(str(getattr(exc, "code", "") or type(exc).__name__))
            save_receipt()
            raise
        self._record_source_inventory_evidence(binding, degraded_shards=degraded_shards,
                                               error_code=source_error_code)
        return {
            "state": "source_degraded" if degraded_shards else "healthy",
            "scanned": scan["raw_rows_scanned"], "captured_links": captured_links,
            "captured_files": captured_files, "captured_contexts": scan["visible_messages_captured"],
            "initialized_chats": initialized, "degraded_shards": degraded_shards,
            "error_code": source_error_code, "source_complete": bool(binding.get("complete")),
            "inventory_digest": str(binding.get("inventory_digest") or ""),
            "inventory_revision": int(binding.get("inventory_revision") or 0),
            "source_counts": dict(binding.get("counts") or {}),
            "source_error_codes": list(binding.get("error_codes") or []),
            "raw_eof": scan["raw_eof"], "pending_shards": scan["pending_shards"],
        }

    def backfill(self, from_timestamp, *, apply=False, run_id=""):
        """Stage or apply one identity-bound link-and-file backfill run."""
        return self._run_backfill(
            from_timestamp,
            apply=apply,
            run_id=run_id,
            include_links=True,
            include_files=True,
            mode="links_and_files",
        )

    def backfill_links(self, from_timestamp, *, apply=False, run_id=""):
        """Stage or apply exact historical links without attachment access."""
        return self._run_backfill(
            from_timestamp,
            apply=apply,
            run_id=run_id,
            include_links=True,
            include_files=False,
            mode="links_only",
        )

    def backfill_contexts(self, from_timestamp, *, apply=False, run_id=""):
        """Stage/apply visible history without queueing resources or reading bytes."""
        return self._run_backfill(
            from_timestamp, apply=apply, run_id=run_id,
            include_links=False, include_files=False, mode="contexts_only",
        )

    @staticmethod
    def _digest_json(value):
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _selected_chat_digest(self, chats=None):
        chats = self.selected_chats() if chats is None else chats
        return self._digest_json([
            {
                "username": chat["username"],
                "chat_key": chat["chat_key"],
                "alias": chat["alias"],
                "selected_since": int(chat.get("selected_since") or 0),
                "selection_id": str(chat.get("selection_id") or ""),
            }
            for chat in chats
        ])

    def cleanup_backfill_runs(self):
        if not self._owns_operation():
            with self._operation_lock():
                return self.cleanup_backfill_runs()
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM resource_backfill_runs WHERE expires_at <= ?",
                (self.now_func(),),
            )
            removed = self._inserted(cursor)
            conn.commit()
            return removed
        finally:
            conn.close()

    def _create_backfill_run(self, mode, from_timestamp, chats):
        run_id = str(self.backfill_run_id_factory())
        try:
            run_id = str(uuid.UUID(run_id))
        except ValueError as exc:
            raise ResourceCaptureError("backfill_run_id_invalid") from exc
        now = self.now_func()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO resource_backfill_runs(
                    run_id, mode, from_timestamp, selected_chat_digest,
                    state, created_at, expires_at, includes_contexts
                ) VALUES (?, ?, ?, ?, 'staging', ?, ?, ?)
                """,
                (
                    run_id, mode, max(0, int(from_timestamp)),
                    self._selected_chat_digest(chats), now,
                    now + BACKFILL_RUN_TTL_SECONDS,
                    int(mode == "contexts_only" or self._captures_contexts()),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return run_id

    def _stage_backfill_page(
        self,
        run_id,
        chat,
        page,
        *,
        include_links,
        include_files,
        include_contexts=False,
    ):
        inserted_links = 0
        inserted_files = 0
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for candidate in (self._context_candidates(chat, page) if include_contexts else ()):
                conn.execute("""
                    INSERT OR REPLACE INTO resource_backfill_staged_contexts(
                        run_id, chat_username, source_message_id, payload
                    ) VALUES (?, ?, ?, ?)
                """, (run_id, chat["username"], candidate["source_message_id"],
                      json.dumps(candidate, ensure_ascii=False, sort_keys=True)))
            for candidate in self._normalized_occurrence_candidates(
                chat,
                page,
                include_links=include_links,
                include_files=include_files,
            ):
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO resource_backfill_staged_occurrences(
                        run_id, chat_username, chat_key, chat_alias,
                        source_message_id, resource_index, kind,
                        source_timestamp, source_month, source_time,
                        source_sender, original_name, observed_url, url_sha256,
                        declared_size, declared_hash, status, candidate_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        candidate["chat_username"], candidate["chat_key"],
                        candidate["chat_alias"], candidate["source_message_id"],
                        candidate["resource_index"], candidate["kind"],
                        candidate["source_timestamp"], candidate["source_month"],
                        candidate["source_time"], candidate["source_sender"],
                        candidate["original_name"], candidate["observed_url"],
                        candidate["url_sha256"], candidate["declared_size"],
                        candidate["declared_hash"], candidate["status"],
                        self._candidate_sha256(candidate),
                    ),
                )
                if self._inserted(cursor):
                    if candidate["kind"] == "link":
                        inserted_links += 1
                    else:
                        inserted_files += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return inserted_links, inserted_files

    def _staged_candidate_digest(self, conn, run_id):
        digest = hashlib.sha256()
        for row in conn.execute(
            """
            SELECT *
            FROM resource_backfill_staged_occurrences
            WHERE run_id = ?
            ORDER BY chat_username, source_timestamp, source_message_id,
                     kind, resource_index
            """,
            (run_id,),
        ):
            candidate = {
                key: row[key]
                for key in (
                    "chat_username", "chat_key", "chat_alias",
                    "source_message_id", "resource_index", "kind",
                    "source_timestamp", "source_month", "source_time",
                    "source_sender", "original_name", "observed_url",
                    "url_sha256", "declared_size", "declared_hash", "status",
                )
            }
            candidate_hash = self._candidate_sha256(candidate)
            if candidate_hash != str(row["candidate_sha256"]):
                digest.update(b"tampered:")
            digest.update(candidate_hash.encode("ascii"))
            digest.update(b"\n")
        digest.update(b"visible-contexts-v1\n")
        for row in conn.execute(
            "SELECT payload FROM resource_backfill_staged_contexts WHERE run_id=? "
            "ORDER BY chat_username, source_message_id", (run_id,),
        ):
            digest.update(self._candidate_sha256(json.loads(row["payload"])).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    @staticmethod
    def _backfill_result(row, **overrides):
        value = dict(row or {})
        result = {
            "state": str(value.get("state") or "run_not_found"),
            "run_id": str(value.get("run_id") or ""),
            "candidate_digest": str(value.get("candidate_digest") or ""),
            "selected_chat_digest": str(value.get("selected_chat_digest") or ""),
            "inventory_digest": str(value.get("inventory_digest") or ""),
            "source_manifest_digest": str(value.get("source_manifest_digest") or ""),
            "scanned": int(value.get("scanned") or 0),
            "discovered_links": int(value.get("discovered_links") or 0),
            "discovered_files": int(value.get("discovered_files") or 0),
            "inserted_links": int(value.get("inserted_links") or 0),
            "inserted_files": int(value.get("inserted_files") or 0),
            "discovered_contexts": int(value.get("discovered_contexts") or 0),
            "inserted_contexts": int(value.get("inserted_contexts") or 0),
            "includes_contexts": bool(value.get("includes_contexts")),
            "error_code": str(value.get("error_code") or ""),
            "mode": str(value.get("mode") or ""),
            "from_timestamp": int(value.get("from_timestamp") or 0),
            "source_complete": bool(value.get("source_complete")),
        }
        result.update(overrides)
        return result

    def _finish_backfill_plan(
        self,
        run_id,
        *,
        state,
        source_complete,
        scanned,
        discovered_links,
        discovered_files,
        inventory_digest,
        source_manifest,
        error_code="",
    ):
        conn = self._connect()
        try:
            candidate_digest = self._staged_candidate_digest(conn, run_id)
            conn.execute(
                "UPDATE resource_backfill_runs SET discovered_contexts=("
                "SELECT COUNT(*) FROM resource_backfill_staged_contexts WHERE run_id=?) "
                "WHERE run_id=?", (run_id, run_id),
            )
            conn.execute(
                """
                UPDATE resource_backfill_runs
                SET inventory_digest = ?, source_manifest_digest = ?, candidate_digest = ?,
                    source_complete = ?, state = ?, scanned = ?,
                    discovered_links = ?, discovered_files = ?, error_code = ?
                WHERE run_id = ?
                """,
                (
                    str(inventory_digest or ""), self._digest_json(source_manifest),
                    candidate_digest,
                    1 if source_complete else 0, state, scanned,
                    discovered_links, discovered_files, error_code, run_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM resource_backfill_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            conn.commit()
            return self._backfill_result(row)
        finally:
            conn.close()

    def _plan_backfill(
        self,
        from_timestamp,
        *,
        include_links,
        include_files,
        mode,
    ):
        from_timestamp = max(0, int(from_timestamp))
        chats = self.selected_chats()
        if not chats:
            return self._backfill_result({
                "state": "no_selected_chats",
                "mode": mode,
                "from_timestamp": from_timestamp,
            })
        if self.source is None:
            return self._backfill_result({
                "state": "source_unavailable",
                "mode": mode,
                "from_timestamp": from_timestamp,
                "error_code": "source_unavailable",
            })
        self.cleanup_backfill_runs()
        binding = bind_source_inventory(self.source, chats)
        run_id = self._create_backfill_run(mode, from_timestamp, chats)
        page_size = min(2_000, max(500, int(
            self.config.get("resource_backup_max_messages_per_scan", BACKFILL_PAGE_SIZE)
        )))
        scanned = 0
        discovered_links = 0
        discovered_files = 0
        degraded_shards = int(binding.get("degraded_shards") or 0)
        source_error_code = str(binding.get("error_code") or "")
        source_manifest = [
            {
                "chat_key": chat["chat_key"],
                "source_shards": list(
                    (binding.get("shards_by_username") or {}).get(chat["username"], [])
                ),
            }
            for chat in chats
        ]
        for chat in chats:
            source_shards = list(
                (binding.get("shards_by_username") or {}).get(chat["username"], [])
            )
            if not source_shards:
                continue
            for source_shard_id in source_shards:
                cursor_timestamp = from_timestamp
                seen_ids = set()
                cursor_token = ""
                while True:
                    try:
                        page, next_cursor_token, exhausted = read_source_page(
                            self.source,
                            chat["username"],
                            source_shard_id,
                            cursor_token=cursor_token,
                            cursor_timestamp=cursor_timestamp,
                            seen_ids=seen_ids,
                            limit=page_size,
                        )
                    except SourceUnavailableError as exc:
                        degraded_shards += 1
                        source_error_code = normalize_source_error(exc)
                        break
                    if not page and not exhausted:
                        degraded_shards += 1
                        source_error_code = "source_cursor_stalled"
                        break
                    if page and callable(getattr(self.source, "get_cursor_page_for_shard", None)) and next_cursor_token == cursor_token:
                        degraded_shards += 1
                        source_error_code = "source_cursor_stalled"
                        break
                    if not page:
                        break
                    scanned += len(page)
                    links, files = self._stage_backfill_page(
                        run_id,
                        chat,
                        page,
                        include_links=include_links,
                        include_files=include_files,
                        include_contexts=mode == "contexts_only" or self._captures_contexts(),
                    )
                    discovered_links += links
                    discovered_files += files
                    cursor_timestamp, seen_ids = self._cursor_after(
                        page, cursor_timestamp, seen_ids
                    )
                    if callable(getattr(self.source, "get_cursor_page_for_shard", None)):
                        seen_ids = set()
                    cursor_token = next_cursor_token
                    if exhausted:
                        break
        final_binding = bind_source_inventory(self.source, chats)
        if (not final_binding.get("complete") or
                final_binding.get("inventory_digest") != binding.get("inventory_digest")):
            degraded_shards += 1
            source_error_code = str(final_binding.get("error_code") or "source_generation_changed")
        self._record_source_inventory_evidence(
            binding,
            degraded_shards=degraded_shards,
            error_code=source_error_code,
        )
        return self._finish_backfill_plan(
            run_id,
            state="source_degraded" if degraded_shards else "planned",
            source_complete=bool(binding.get("complete")) and degraded_shards == 0,
            scanned=scanned,
            discovered_links=discovered_links,
            discovered_files=discovered_files,
            inventory_digest=str(binding.get("inventory_digest") or ""),
            source_manifest=source_manifest,
            error_code=source_error_code,
        )

    def _apply_backfill_run(self, run_id, *, mode, from_timestamp):
        if not run_id:
            return self._backfill_result({
                "state": "plan_required",
                "mode": mode,
                "from_timestamp": max(0, int(from_timestamp)),
                "error_code": "backfill_run_id_required",
            })
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM resource_backfill_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if row is None:
                return self._backfill_result({
                    "state": "run_not_found",
                    "run_id": str(run_id),
                    "mode": mode,
                    "from_timestamp": max(0, int(from_timestamp)),
                    "error_code": "backfill_run_not_found",
                })
            result = self._backfill_result(row)
            if float(row["expires_at"] or 0) <= self.now_func():
                return self._backfill_result(row, state="plan_expired", source_complete=False)
            if row["state"] == "applied":
                return result
            if (
                row["state"] != "planned"
                or not bool(row["source_complete"])
                or str(row["mode"]) != mode
                or int(row["from_timestamp"]) != max(0, int(from_timestamp))
            ):
                return self._backfill_result(
                    row,
                    state="plan_not_applicable",
                    source_complete=False,
                    error_code="backfill_plan_not_applicable",
                )
            if str(row["selected_chat_digest"]) != self._selected_chat_digest():
                return self._backfill_result(
                    row,
                    state="selection_changed",
                    source_complete=False,
                    error_code="selected_chat_digest_mismatch",
                )
            if self.source is None:
                return self._backfill_result(
                    row,
                    state="inventory_unavailable",
                    source_complete=False,
                    error_code="source_inventory_unavailable",
                )
            current_binding = bind_source_inventory(
                self.source, self.selected_chats()
            )
            self._record_source_inventory_evidence(current_binding)
            if not bool(current_binding.get("complete")):
                return self._backfill_result(
                    row,
                    state="source_degraded",
                    source_complete=False,
                    error_code=str(
                        current_binding.get("error_code")
                        or "source_inventory_incomplete"
                    ),
                )
            if str(row["inventory_digest"] or "") != str(
                current_binding.get("inventory_digest") or ""
            ):
                return self._backfill_result(
                    row,
                    state="inventory_changed",
                    source_complete=False,
                    error_code="inventory_digest_mismatch",
                )
            if str(row["candidate_digest"]) != self._staged_candidate_digest(conn, run_id):
                return self._backfill_result(
                    row,
                    state="candidate_mismatch",
                    source_complete=False,
                    error_code="candidate_digest_mismatch",
                )

            # Planning is staging-only. Canonical chat/cursor rows are created or
            # reconciled only after the user applies the identity-bound plan.
            self._initialize_selected_chat_cursors_locked()
            now = self.now_func()
            conn.execute("BEGIN IMMEDIATE")
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO resource_occurrences(
                    chat_username, chat_key, chat_alias, source_message_id,
                    resource_index, kind, source_timestamp, source_month,
                    source_time, source_sender, original_name, observed_url,
                    url_sha256, declared_size, declared_hash, status,
                    created_at, updated_at
                )
                SELECT chat_username, chat_key, chat_alias, source_message_id,
                       resource_index, kind, source_timestamp, source_month,
                       source_time, source_sender, original_name, observed_url,
                       url_sha256, declared_size, declared_hash, status, ?, ?
                FROM resource_backfill_staged_occurrences
                WHERE run_id = ? AND kind = 'link'
                """,
                (now, now, run_id),
            )
            inserted_links = conn.total_changes - before
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO resource_occurrences(
                    chat_username, chat_key, chat_alias, source_message_id,
                    resource_index, kind, source_timestamp, source_month,
                    source_time, source_sender, original_name, observed_url,
                    url_sha256, declared_size, declared_hash, status,
                    created_at, updated_at
                )
                SELECT chat_username, chat_key, chat_alias, source_message_id,
                       resource_index, kind, source_timestamp, source_month,
                       source_time, source_sender, original_name, observed_url,
                       url_sha256, declared_size, declared_hash, status, ?, ?
                FROM resource_backfill_staged_occurrences
                WHERE run_id = ? AND kind = 'file'
                """,
                (now, now, run_id),
            )
            inserted_files = conn.total_changes - before
            inserted_contexts = 0
            for staged in conn.execute(
                "SELECT payload FROM resource_backfill_staged_contexts WHERE run_id=? "
                "ORDER BY chat_username, source_message_id", (run_id,),
            ):
                inserted_contexts += self._inserted(
                    self._insert_context(conn, json.loads(staged["payload"]))
                )
            for chat in (self._selected_chats_locked() if row["includes_contexts"] else []):
                self._history_range(conn, chat, from_timestamp,
                                    row["inventory_digest"], "explicit_backfill")
                if int(from_timestamp) == 0:
                    conn.execute(
                        "UPDATE resource_chats SET context_history_gap=0 WHERE chat_username=?",
                        (chat["username"],),
                    )
            conn.execute(
                """
                UPDATE resource_backfill_runs
                SET state = 'applied', applied_at = ?, inserted_links = ?,
                    inserted_files = ?, inserted_contexts = ?
                WHERE run_id = ? AND state = 'planned'
                """,
                (now, inserted_links, inserted_files, inserted_contexts, run_id),
            )
            applied = conn.execute(
                "SELECT * FROM resource_backfill_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            conn.commit()
            return self._backfill_result(applied)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _run_backfill(
        self,
        from_timestamp,
        *,
        apply,
        run_id,
        include_links,
        include_files,
        mode,
    ):
        try:
            with self._operation_lock():
                return self._run_backfill_locked(
                    from_timestamp,
                    apply=apply,
                    run_id=run_id,
                    include_links=include_links,
                    include_files=include_files,
                    mode=mode,
                )
        except ResourceCaptureError as exc:
            if exc.code == "capture_worker_busy":
                return self._backfill_result({
                    "state": "worker_busy",
                    "mode": mode,
                    "from_timestamp": max(0, int(from_timestamp)),
                    "error_code": exc.code,
                })
            raise

    def _run_backfill_locked(
        self,
        from_timestamp,
        *,
        apply,
        run_id,
        include_links,
        include_files,
        mode,
    ):
        self._reload_canonical_config_locked()
        if apply:
            return self._apply_backfill_run(
                run_id,
                mode=mode,
                from_timestamp=from_timestamp,
            )
        with source_snapshot(self.source):
            return self._plan_backfill(
                from_timestamp,
                include_links=include_links,
                include_files=include_files,
                mode=mode,
            )

    def _retry_delay(self, attempt_count):
        base = max(1, int(self.config.get("attachment_archive_retry_base_seconds", 300)))
        maximum = max(base, int(self.config.get("attachment_archive_retry_max_seconds", 21600)))
        exponential = min(maximum, base * (2 ** max(0, attempt_count - 1)))
        return max(1, int(exponential * (0.75 + 0.5 * float(self.random_func()))))

    def _set_file_state(
        self,
        occurrence_id,
        status,
        *,
        method="",
        error_code="",
        values=None,
        expected_status=None,
        expected_updated_at=None,
    ):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT attempt_count, status, updated_at FROM resource_occurrences "
                "WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
            if row is None:
                return False
            if (
                expected_status is not None
                and str(row["status"] or "") != str(expected_status)
            ):
                return False
            if (
                expected_updated_at is not None
                and float(row["updated_at"] or 0) != float(expected_updated_at)
            ):
                return False
            previous = int(row["attempt_count"] or 0) if row else 0
            failure = status in {
                "waiting_cache", "insufficient_local_space", "retry_wait"
            }
            attempt = previous + 1 if failure else 0 if status == "ready_local" else previous
            next_retry = self.now_func() + self._retry_delay(attempt) if failure else 0
            assignments = [
                "status = ?", "resolution_method = ?", "last_error_code = ?",
                "attempt_count = ?", "next_retry_at = ?", "updated_at = ?",
            ]
            params = [status, method, error_code, attempt, next_retry, self.now_func()]
            for key, value in (values or {}).items():
                if key not in {"object_sha256", "object_size", "object_relpath"}:
                    continue
                assignments.append(f"{key} = ?")
                params.append(value)
            params.extend((occurrence_id, str(row["status"]), float(row["updated_at"])))
            cursor = conn.execute(
                "UPDATE resource_occurrences SET " + ", ".join(assignments)
                + " WHERE occurrence_id = ? AND status = ? AND updated_at = ?",
                params,
            )
            conn.commit()
            return int(cursor.rowcount or 0) == 1
        finally:
            conn.close()

    def resolve_pending_files(self, limit=50, *, consent_check=None):
        try:
            with self._operation_lock():
                self._reload_canonical_config_locked()
                return self._resolve_pending_files_locked(
                    limit=limit,
                    consent_check=consent_check,
                )
        except ResourceCaptureError as exc:
            if exc.code == "capture_worker_busy":
                return {
                    "state": "worker_busy",
                    "processed": 0,
                    "ready_local": 0,
                    "failed": 0,
                    "error_code": exc.code,
                }
            raise

    def _resolve_pending_files_locked(self, limit=50, *, consent_check=None):
        chat_keys = self.selected_chat_keys()
        if not chat_keys:
            return {"state": "no_selected_chats", "processed": 0, "ready_local": 0, "failed": 0}
        placeholders = ",".join("?" for _ in chat_keys)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT * FROM resource_occurrences
                WHERE chat_key IN ({placeholders})
                  AND kind = 'file'
                  AND (
                      status = 'queued'
                      OR (status IN ('waiting_cache', 'insufficient_local_space', 'retry_wait')
                          AND next_retry_at <= ?)
                  )
                ORDER BY CASE WHEN status = 'queued' THEN 0 ELSE 1 END,
                         next_retry_at, occurrence_id
                LIMIT ?
                """,
                (*chat_keys, self.now_func(), max(1, int(limit))),
            ).fetchall()
        finally:
            conn.close()

        processed = 0
        ready_local = 0
        failed = 0
        superseded = 0
        for row in rows:
            if consent_check is not None and not bool(consent_check()):
                return {
                    "state": "consent_revoked",
                    "processed": processed,
                    "ready_local": ready_local,
                    "failed": failed,
                    "error_code": "attachment_consent_revoked",
                }
            processed += 1
            result = self.archive.preserve_file_mention(dict(row))
            status = str(result.get("status") or "retry_wait")
            method = str(result.get("resolution_method") or "")
            if status == "ready_local":
                changed = self._set_file_state(
                    row["occurrence_id"],
                    "ready_local",
                    method=method,
                    values={
                        "object_sha256": result["sha256"],
                        "object_size": int(result["size"]),
                        "object_relpath": result["object_relpath"],
                    },
                    expected_status=row["status"],
                    expected_updated_at=row["updated_at"],
                )
                if changed:
                    ready_local += 1
                else:
                    superseded += 1
            elif status == "missing_retryable":
                changed = self._set_file_state(
                    row["occurrence_id"], "waiting_cache",
                    method=method, error_code=status,
                    expected_status=row["status"],
                    expected_updated_at=row["updated_at"],
                )
                if changed:
                    failed += 1
                else:
                    superseded += 1
            elif status in {"ambiguous", "object_too_large", "source_rejected"}:
                changed = self._set_file_state(
                    row["occurrence_id"], status,
                    method=method, error_code=status,
                    expected_status=row["status"],
                    expected_updated_at=row["updated_at"],
                )
                if changed:
                    failed += 1
                else:
                    superseded += 1
            elif status == "insufficient_local_space":
                changed = self._set_file_state(
                    row["occurrence_id"], status,
                    method=method, error_code=status,
                    expected_status=row["status"],
                    expected_updated_at=row["updated_at"],
                )
                if changed:
                    failed += 1
                else:
                    superseded += 1
            else:
                changed = self._set_file_state(
                    row["occurrence_id"], "retry_wait",
                    method=method,
                    error_code=str(result.get("error_code") or status),
                    expected_status=row["status"],
                    expected_updated_at=row["updated_at"],
                )
                if changed:
                    failed += 1
                else:
                    superseded += 1
        return {
            "state": "healthy" if failed == 0 and superseded == 0 else "degraded",
            "processed": processed,
            "ready_local": ready_local,
            "failed": failed,
            "superseded": superseded,
        }

    def occurrences(self, *, selected_only=True):
        if not self._owns_operation():
            with self._operation_lock():
                return self.occurrences(selected_only=selected_only)
        params = []
        where = ""
        if selected_only:
            chat_keys = self.selected_chat_keys()
            if not chat_keys:
                return []
            where = "WHERE chat_key IN ({})".format(",".join("?" for _ in chat_keys))
            params.extend(chat_keys)
        conn = self._connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT * FROM resource_occurrences
                    {where}
                    ORDER BY chat_alias, source_timestamp, source_message_id,
                             kind, resource_index, occurrence_id
                    """,
                    params,
                )
            ]
        finally:
            conn.close()

    def status(self):
        if not self._owns_operation():
            try:
                with self._operation_lock():
                    return self.status()
            except ResourceCaptureError as exc:
                if exc.code == "capture_worker_busy":
                    return {
                        "state": "worker_busy",
                        "selected_chats": 0,
                        "counts": {},
                        "pending_files": 0,
                        "last_scan_at": "",
                        "error_code": exc.code,
                    }
                raise
        chats = self._selected_chats_locked()
        keys = [chat["chat_key"] for chat in chats]
        counts = {}
        pending_files = 0
        conn = self._connect()
        try:
            if keys:
                placeholders = ",".join("?" for _ in keys)
                rows = conn.execute(
                    f"""
                    SELECT kind, status, COUNT(*) AS count
                    FROM resource_occurrences
                    WHERE chat_key IN ({placeholders})
                    GROUP BY kind, status
                    ORDER BY kind, status
                    """,
                    keys,
                ).fetchall()
                counts = {
                    f"{row['kind']}:{row['status']}": int(row["count"])
                    for row in rows
                }
                pending_files = int(conn.execute(
                    f"""
                    SELECT COUNT(*) FROM resource_occurrences
                    WHERE chat_key IN ({placeholders})
                      AND kind = 'file' AND status <> 'ready_local'
                    """,
                    keys,
                ).fetchone()[0])
        finally:
            conn.close()
        return {
            "state": self._meta_get("source_state", "not_started"),
            "selected_chats": len(chats),
            "counts": counts,
            "pending_files": pending_files,
            "last_scan_at": self._meta_get("last_scan_at", ""),
            "error_code": self._meta_get("source_error_code", ""),
        }

    def run(
        self,
        resolve_limit=50,
        *,
        resolve_files=False,
        consent_check=None,
    ):
        try:
            with self._operation_lock():
                self._reload_canonical_config_locked()
                with source_snapshot(self.source):
                    scan = self._scan_locked()
                if str(scan.get("state") or "") == "worker_busy":
                    raise ResourceCaptureError("capture_worker_busy")
                resolve = (
                    self._resolve_pending_files_locked(
                        limit=resolve_limit,
                        consent_check=consent_check,
                    )
                    if resolve_files
                    else {
                        "state": "skipped",
                        "reason": "file_resolution_disabled",
                        "processed": 0,
                        "ready_local": 0,
                        "failed": 0,
                    }
                )
        except ResourceCaptureError as exc:
            if exc.code != "capture_worker_busy":
                raise
            scan = {
                "state": "worker_busy",
                "scanned": 0,
                "captured_links": 0,
                "captured_files": 0,
                "error_code": exc.code,
            }
            resolve = {
                "state": "not_run_worker_busy",
                "processed": 0,
                "ready_local": 0,
                "failed": 0,
            }
            return {"state": "worker_busy", "scan": scan, "resolve": resolve}
        scan_state = str(scan.get("state") or "unknown")
        resolve_state = str(resolve.get("state") or "unknown")
        if scan_state != "healthy":
            state = "degraded" if scan_state == "source_degraded" else scan_state
        elif resolve_state in {"healthy", "skipped"}:
            state = "healthy"
        elif resolve_state == "degraded":
            state = "degraded"
        else:
            state = resolve_state
        return {"state": state, "scan": scan, "resolve": resolve}


def update_resource_backup_selection(config, selected_chats):
    """Patch selection and initialize its epoch under one capture operation lock."""
    del config  # Caller snapshots are never lock or write authority.
    for _attempt in range(2):
        canonical = load_config()
        locked_db_path = _capture_db_path(canonical)
        try:
            with resource_capture_operation_lock(canonical):
                def patch_current(current):
                    if _capture_db_path(current) != locked_db_path:
                        raise ResourceCaptureError("capture_config_changed")
                    current["resource_backup_selected_chats"] = list(
                        selected_chats or []
                    )
                    return current

                updated = update_config(mutator=patch_current)
                if _capture_db_path(updated) != locked_db_path:
                    raise ResourceCaptureError("capture_config_changed")
                capture = SelectedResourceCapture.from_config(updated)
                capture._operation_depth = 1
                capture._operation_owner = threading.get_ident()
                try:
                    capture._reload_canonical_config_locked()
                    capture._ensure_initialized_locked()
                    initialized = capture._initialize_selected_chat_cursors_locked()
                finally:
                    capture._operation_depth = 0
                    capture._operation_owner = None
                return updated, initialized
        except ResourceCaptureError as exc:
            if exc.code != "capture_config_changed":
                raise
    raise ResourceCaptureError("capture_config_changed")
