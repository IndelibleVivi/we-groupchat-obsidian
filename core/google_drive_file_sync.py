"""Selected-chat file discovery, shared-CAS preservation, and Google Drive projection."""
from __future__ import annotations

import fcntl
import hashlib
import json
import mimetypes
import os
import random
import re
import sqlite3
import stat
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime

from .attachment_archive import AttachmentArchive
from .config import DATA_DIR, selected_drive_sync_chats
from .google_drive_auth import GoogleDriveAuthRequired
from .google_drive_client import (
    FOLDER_MIME,
    SHORTCUT_MIME,
    GoogleDriveError,
    GoogleDriveRetryableError,
)
from .source_adapter import (
    SourceUnavailableError,
    bind_source_inventory,
    normalize_source_error,
    read_source_page,
    source_snapshot,
    source_state_evidence,
)


BACKFILL_PAGE_SIZE = 50_000


SCHEMA_VERSION = 1
FRESH_RESOLVE_STATES = ("queued",)
RETRY_RESOLVE_STATES = ("waiting_cache", "insufficient_local_space")
REMOTE_STATES = (
    "ready_local",
    "upload_pending",
    "uploading",
    "uploaded_verified",
    "shortcut_pending",
    "retry_wait",
    "auth_required",
    "remote_degraded",
)


class DriveSyncError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class RemoteDegraded(DriveSyncError):
    pass


class RunBudgetExhausted(DriveSyncError):
    pass


def _safe_name(value, fallback="attachment"):
    text = "".join(" " if ord(char) < 32 else char for char in str(value or ""))
    text = text.replace("/", "／").replace(":", "：").strip().strip(".")
    text = re.sub(r"\s+", " ", text)
    return (text[:180] or fallback)


def _shortcut_conflict_name(name, digest):
    safe = _safe_name(name)
    stem, extension = os.path.splitext(safe)
    return f"{stem}--{digest[:8]}{extension}"


def _month(timestamp):
    return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m")


def _hash_path(path):
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise DriveSyncError("local_object_not_regular")
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            sha256.update(chunk)
            md5.update(chunk)
        after = os.fstat(fd)
        if any(
            getattr(before, field) != getattr(after, field)
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        ):
            raise DriveSyncError("local_object_changed")
        return size, sha256.hexdigest(), md5.hexdigest()
    finally:
        os.close(fd)


class GoogleDriveFileSync:
    def __init__(
        self,
        config: dict,
        *,
        source=None,
        drive_client=None,
        oauth=None,
        notifier=None,
        now_func=time.time,
        random_func=random.random,
        archive_id_factory=None,
        control_state_func=None,
        after_remote_object=None,
        after_remote_shortcut=None,
    ):
        self.config = dict(config or {})
        self.source = source
        self.drive_client = drive_client
        self.oauth = oauth
        self.notifier = notifier or (lambda _title, _message: None)
        self.now_func = now_func
        self.random_func = random_func
        self.archive_id_factory = archive_id_factory or (lambda: str(uuid.uuid4()))
        self.control_state_func = control_state_func or (lambda: self.config)
        self.after_remote_object = after_remote_object
        self.after_remote_shortcut = after_remote_shortcut
        self.db_path = os.path.abspath(os.path.expanduser(
            self.config.get("google_drive_file_sync_db")
            or os.path.join(DATA_DIR, "google_drive_file_sync.db")
        ))
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
        self._ensure_schema()
        self.archive_id = self._ensure_archive_id()

    @classmethod
    def from_config(cls, config, **kwargs):
        return cls(config, **kwargs)

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_schema(self):
        os.makedirs(os.path.dirname(self.db_path), mode=0o700, exist_ok=True)
        try:
            os.chmod(os.path.dirname(self.db_path), 0o700)
        except OSError:
            pass
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS drive_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS drive_scan_state (
                    chat_username TEXT PRIMARY KEY,
                    cursor_timestamp INTEGER NOT NULL,
                    cursor_message_ids_json TEXT NOT NULL DEFAULT '[]',
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS drive_scan_shards (
                    chat_username TEXT NOT NULL,
                    source_shard_id TEXT NOT NULL,
                    cursor_timestamp INTEGER NOT NULL,
                    cursor_message_ids_json TEXT NOT NULL DEFAULT '[]',
                    source_cursor_token TEXT NOT NULL DEFAULT '',
                    source_state TEXT NOT NULL DEFAULT 'healthy',
                    last_error_code TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(chat_username, source_shard_id)
                );

                CREATE TABLE IF NOT EXISTS drive_sync_items (
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_message_id TEXT NOT NULL,
                    resource_index INTEGER NOT NULL,
                    chat_username TEXT NOT NULL,
                    chat_key TEXT NOT NULL,
                    chat_alias TEXT NOT NULL,
                    source_timestamp INTEGER NOT NULL,
                    source_month TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'file',
                    declared_size INTEGER,
                    declared_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    resolution_method TEXT NOT NULL DEFAULT '',
                    object_sha256 TEXT NOT NULL DEFAULT '',
                    object_size INTEGER,
                    object_relpath TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(source_message_id, resource_index)
                );
                CREATE INDEX IF NOT EXISTS idx_drive_sync_items_status
                    ON drive_sync_items(status, next_retry_at, item_id);

                CREATE TABLE IF NOT EXISTS drive_objects (
                    sha256 TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    md5 TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    drive_file_id TEXT NOT NULL DEFAULT '',
                    verification_state TEXT NOT NULL DEFAULT '',
                    web_view_link TEXT NOT NULL DEFAULT '',
                    last_error_code TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS drive_placements (
                    placement_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_key TEXT NOT NULL,
                    source_month TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    shortcut_id TEXT NOT NULL DEFAULT '',
                    chat_folder_id TEXT NOT NULL,
                    month_folder_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    verification_state TEXT NOT NULL DEFAULT '',
                    last_error_code TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    UNIQUE(chat_key, source_month, sha256)
                );

                CREATE TABLE IF NOT EXISTS drive_folders (
                    folder_key TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    chat_key TEXT NOT NULL DEFAULT '',
                    source_month TEXT NOT NULL DEFAULT '',
                    shard TEXT NOT NULL DEFAULT '',
                    drive_file_id TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS drive_sync_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    state TEXT NOT NULL,
                    scanned INTEGER NOT NULL DEFAULT 0,
                    queued INTEGER NOT NULL DEFAULT 0,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    uploaded INTEGER NOT NULL DEFAULT 0,
                    shortcuts INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                """
            )
            # The keyset source cursor is opaque and must survive restarts, so
            # an existing ledger gains the column instead of replaying a
            # same-second bucket from the timestamp floor on every scan.  The
            # check and conditional ALTER must share one write transaction:
            # menu and operator processes may construct the service together.
            conn.execute("BEGIN IMMEDIATE")
            try:
                shard_columns = {
                    str(row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(drive_scan_shards)"
                    )
                }
                if "source_cursor_token" not in shard_columns:
                    conn.execute(
                        "ALTER TABLE drive_scan_shards "
                        "ADD COLUMN source_cursor_token TEXT NOT NULL DEFAULT ''"
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _meta_get(self, key, default=""):
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM drive_meta WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default
        finally:
            conn.close()

    def _meta_set(self, key, value):
        self._meta_set_many({key: value})

    def _meta_set_many(self, values):
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT INTO drive_meta(key, value) VALUES (?, ?) "
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
            "last_scan_at": self.now_func(),
            **source_state_evidence(
                binding,
                degraded_shards=degraded_shards,
                error_code=error_code,
            ),
        })

    def _ensure_archive_id(self):
        archive_id = self._meta_get("archive_id")
        if archive_id:
            return archive_id
        archive_id = str(self.archive_id_factory())
        try:
            archive_id = str(uuid.UUID(archive_id))
        except ValueError as exc:
            raise DriveSyncError("archive_id_invalid") from exc
        self._meta_set("archive_id", archive_id)
        return archive_id

    def _chat_key(self, username):
        return hashlib.sha256(
            f"we-groupchat-drive-chat-v1\0{self.archive_id}\0{username}".encode("utf-8")
        ).hexdigest()

    def selected_chats(self):
        result = []
        for chat in selected_drive_sync_chats(self.config):
            chat_key = self._chat_key(chat["username"])
            result.append({
                "username": chat["username"],
                "alias": _safe_name(chat.get("alias"), f"群聊-{chat_key[:8]}"),
                "chat_key": chat_key,
            })
        return result

    def _active_chat_keys(self):
        return [chat["chat_key"] for chat in self.selected_chats()]

    def initialize_selected_chat_cursors(self, start_timestamp=None):
        start = int(self.now_func() if start_timestamp is None else start_timestamp)
        conn = self._connect()
        try:
            for chat in self.selected_chats():
                conn.execute(
                    "INSERT OR IGNORE INTO drive_scan_state("
                    "chat_username, cursor_timestamp, cursor_message_ids_json, updated_at"
                    ") VALUES (?, ?, '[]', ?)",
                    (chat["username"], start, self.now_func()),
                )
            conn.commit()
        finally:
            conn.close()

    def _control_state(self):
        state = self.control_state_func() or {}
        return bool(state.get("google_drive_file_sync_enabled", False)), bool(
            state.get("google_drive_file_sync_paused", False)
        )

    def _shard_state(self, chat_username, source_shard_id, seed):
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO drive_scan_shards(
                    chat_username, source_shard_id, cursor_timestamp,
                    cursor_message_ids_json, source_state, last_error_code, updated_at
                ) VALUES (?, ?, ?, ?, 'healthy', '', ?)
                """,
                (
                    chat_username,
                    source_shard_id,
                    int(seed["cursor_timestamp"]),
                    str(seed["cursor_message_ids_json"] or "[]"),
                    self.now_func(),
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM drive_scan_shards
                WHERE chat_username = ? AND source_shard_id = ?
                """,
                (chat_username, source_shard_id),
            ).fetchone()
            conn.commit()
            return row
        finally:
            conn.close()

    def _mark_shard_degraded(self, chat_username, source_shard_id, error_code):
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE drive_scan_shards
                SET source_state = 'source_degraded', last_error_code = ?, updated_at = ?
                WHERE chat_username = ? AND source_shard_id = ?
                """,
                (error_code, self.now_func(), chat_username, source_shard_id),
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

    def _insert_file_items(self, conn, chat, messages, now):
        inserted = 0
        for message in messages:
            source_message_id = str(message.get("source_message_id") or "")
            timestamp = int(message.get("timestamp") or 0)
            if not source_message_id or not timestamp:
                continue
            for resource in message.get("resources") or []:
                if not isinstance(resource, dict) or resource.get("kind") != "file":
                    continue
                original_name = _safe_name(resource.get("original_name"), "attachment")
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO drive_sync_items(
                        source_message_id, resource_index, chat_username, chat_key,
                        chat_alias, source_timestamp, source_month, original_name,
                        kind, declared_size, declared_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'file', ?, ?, ?, ?)
                    """,
                    (
                        source_message_id,
                        int(resource.get("resource_index") or 0),
                        chat["username"],
                        chat["chat_key"],
                        chat["alias"],
                        timestamp,
                        _month(timestamp),
                        original_name,
                        resource.get("declared_size"),
                        str(resource.get("declared_hash") or "").lower(),
                        now,
                        now,
                    ),
                )
                inserted += max(0, int(cursor.rowcount or 0))
        return inserted

    def scan(self):
        with source_snapshot(self.source):
            return self._scan_source()

    def _scan_source(self):
        enabled, paused = self._control_state()
        if not enabled:
            return {"state": "disabled", "scanned": 0, "queued": 0}
        if paused:
            return {"state": "paused", "scanned": 0, "queued": 0}
        chats = self.selected_chats()
        if not chats:
            return {"state": "no_selected_chats", "scanned": 0, "queued": 0}
        binding = bind_source_inventory(self.source, chats)
        max_messages = max(1, int(self.config.get("google_drive_file_sync_max_messages_per_scan", 500)))
        scanned = 0
        queued = 0
        initialized = 0
        degraded_shards = int(binding.get("degraded_shards") or 0)
        source_error_code = str(binding.get("error_code") or "")
        for chat in chats:
            conn = self._connect()
            try:
                state = conn.execute(
                    "SELECT * FROM drive_scan_state WHERE chat_username = ?",
                    (chat["username"],),
                ).fetchone()
            finally:
                conn.close()
            if state is None:
                self.initialize_selected_chat_cursors()
                initialized += 1
                continue
            source_shards = list(
                (binding.get("shards_by_username") or {}).get(chat["username"], [])
            )
            if not source_shards:
                continue
            for source_shard_id in source_shards:
                shard_state = self._shard_state(chat["username"], source_shard_id, state)
                cursor_timestamp = int(shard_state["cursor_timestamp"])
                cursor_token = str(shard_state["source_cursor_token"] or "")
                try:
                    seen_ids = set(json.loads(shard_state["cursor_message_ids_json"] or "[]"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    seen_ids = set()
                try:
                    messages, next_cursor_token, _exhausted = read_source_page(
                        self.source,
                        chat["username"],
                        source_shard_id,
                        cursor_token=cursor_token,
                        cursor_timestamp=cursor_timestamp,
                        seen_ids=seen_ids,
                        limit=max_messages,
                    )
                except SourceUnavailableError as exc:
                    code = normalize_source_error(exc)
                    self._mark_shard_degraded(chat["username"], source_shard_id, code)
                    degraded_shards += 1
                    source_error_code = code
                    continue
                new_timestamp, new_ids = self._cursor_after(
                    messages, cursor_timestamp, seen_ids
                )
                if callable(
                    getattr(self.source, "get_cursor_page_for_shard", None)
                ):
                    new_ids = set()
                now = self.now_func()
                conn = self._connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    queued += self._insert_file_items(conn, chat, messages, now)
                    updated = conn.execute(
                        """
                        UPDATE drive_scan_shards
                        SET cursor_timestamp = ?, cursor_message_ids_json = ?,
                            source_cursor_token = ?, source_state = 'healthy',
                            last_error_code = '', updated_at = ?
                        WHERE chat_username = ? AND source_shard_id = ?
                          AND cursor_timestamp = ? AND source_cursor_token = ?
                        """,
                        (
                            new_timestamp,
                            json.dumps(sorted(new_ids)),
                            next_cursor_token,
                            now,
                            chat["username"],
                            source_shard_id,
                            cursor_timestamp,
                            cursor_token,
                        ),
                    )
                    if int(updated.rowcount or 0) != 1:
                        raise DriveSyncError("source_cursor_changed")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
                scanned += len(messages)
        self._record_source_inventory_evidence(
            binding,
            degraded_shards=degraded_shards,
            error_code=source_error_code,
        )
        return {
            "state": "source_degraded" if degraded_shards else "healthy",
            "scanned": scanned,
            "queued": queued,
            "initialized_chats": initialized,
            "degraded_shards": degraded_shards,
            "error_code": source_error_code,
            "source_complete": bool(binding.get("complete")),
            "inventory_digest": str(binding.get("inventory_digest") or ""),
            "inventory_revision": int(binding.get("inventory_revision") or 0),
            "source_counts": dict(binding.get("counts") or {}),
            "source_error_codes": list(binding.get("error_codes") or []),
        }

    def backfill(self, from_timestamp, *, apply=False):
        with source_snapshot(self.source):
            return self._backfill(from_timestamp, apply=apply)

    def _backfill(self, from_timestamp, *, apply=False):
        chats = self.selected_chats()
        binding = bind_source_inventory(self.source, chats)
        max_messages = max(1, int(self.config.get("google_drive_file_sync_max_messages_per_scan", 500)))
        max_messages = max(max_messages, BACKFILL_PAGE_SIZE)
        scanned = 0
        discovered = 0
        inserted = 0
        degraded_shards = int(binding.get("degraded_shards") or 0)
        source_error_code = str(binding.get("error_code") or "")
        for chat in chats:
            source_shards = list(
                (binding.get("shards_by_username") or {}).get(chat["username"], [])
            )
            if not source_shards:
                continue
            for source_shard_id in source_shards:
                cursor_timestamp = int(from_timestamp)
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
                            limit=max_messages,
                        )
                    except SourceUnavailableError as exc:
                        degraded_shards += 1
                        source_error_code = normalize_source_error(exc)
                        break
                    if not page:
                        break
                    scanned += len(page)
                    file_count = sum(
                        1
                        for message in page
                        for resource in (message.get("resources") or [])
                        if isinstance(resource, dict) and resource.get("kind") == "file"
                    )
                    discovered += file_count
                    if apply and file_count:
                        conn = self._connect()
                        try:
                            conn.execute("BEGIN IMMEDIATE")
                            inserted += self._insert_file_items(conn, chat, page, self.now_func())
                            conn.commit()
                        except Exception:
                            conn.rollback()
                            raise
                        finally:
                            conn.close()
                    cursor_timestamp, seen_ids = self._cursor_after(
                        page, cursor_timestamp, seen_ids
                    )
                    cursor_token = next_cursor_token
                    if exhausted:
                        break
        self._record_source_inventory_evidence(
            binding,
            degraded_shards=degraded_shards,
            error_code=source_error_code,
        )
        return {
            "state": (
                "source_degraded"
                if degraded_shards
                else "applied" if apply else "planned"
            ),
            "scanned": scanned,
            "discovered_files": discovered,
            "inserted": inserted,
            "degraded_shards": degraded_shards,
            "error_code": source_error_code,
            "source_complete": bool(binding.get("complete")) and degraded_shards == 0,
            "inventory_digest": str(binding.get("inventory_digest") or ""),
            "inventory_revision": int(binding.get("inventory_revision") or 0),
            "source_counts": dict(binding.get("counts") or {}),
            "source_error_codes": list(binding.get("error_codes") or []),
        }

    def _retry_delay(self, attempt_count, retry_after=0):
        base = max(1, int(self.config.get("google_drive_file_sync_retry_base_seconds", 300)))
        maximum = max(base, int(self.config.get("google_drive_file_sync_retry_max_seconds", 21600)))
        exponential = min(maximum, base * (2 ** max(0, attempt_count - 1)))
        jittered = int(exponential * (0.75 + 0.5 * float(self.random_func())))
        return max(int(retry_after or 0), max(1, jittered))

    def _set_item_state(self, item_id, status, *, method="", error_code="", retry_after=0, values=None):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT attempt_count FROM drive_sync_items WHERE item_id = ?", (item_id,)
            ).fetchone()
            previous_attempts = int(row["attempt_count"] or 0) if row else 0
            retry_failure_states = {
                "waiting_cache",
                "retry_wait",
                "insufficient_local_space",
                "auth_required",
                "remote_degraded",
            }
            retry_phase_reset_states = {"upload_pending", "shortcut_pending", "complete"}
            if status in retry_failure_states:
                attempt = previous_attempts + 1
            elif status in retry_phase_reset_states:
                attempt = 0
            else:
                attempt = previous_attempts
            next_retry = 0
            if status in {"waiting_cache", "retry_wait", "insufficient_local_space"}:
                next_retry = self.now_func() + self._retry_delay(attempt, retry_after)
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
            params.append(item_id)
            conn.execute(
                "UPDATE drive_sync_items SET " + ", ".join(assignments) + " WHERE item_id = ?",
                params,
            )
            conn.commit()
        finally:
            conn.close()

    def _resolve_due(self):
        chat_keys = self._active_chat_keys()
        if not chat_keys:
            return 0
        chat_placeholders = ",".join("?" for _ in chat_keys)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT * FROM drive_sync_items
                WHERE chat_key IN ({chat_placeholders})
                  AND (status = 'queued'
                       OR (status IN ('waiting_cache', 'insufficient_local_space') AND next_retry_at <= ?))
                ORDER BY CASE WHEN status = 'queued' THEN 0 ELSE 1 END,
                         next_retry_at, item_id
                """,
                (*chat_keys, self.now_func()),
            ).fetchall()
        finally:
            conn.close()
        resolved = 0
        for item in rows:
            result = self.archive.preserve_file_mention(item)
            status = result.get("status") or "retry_wait"
            method = result.get("resolution_method") or ""
            if status == "ready_local":
                self._set_item_state(
                    item["item_id"],
                    "upload_pending",
                    method=method,
                    values={
                        "object_sha256": result["sha256"],
                        "object_size": int(result["size"]),
                        "object_relpath": result["object_relpath"],
                    },
                )
                resolved += 1
            elif status == "missing_retryable":
                self._set_item_state(item["item_id"], "waiting_cache", method=method, error_code=status)
            elif status == "ambiguous":
                self._set_item_state(item["item_id"], "ambiguous", method=method, error_code=status)
            elif status == "object_too_large":
                self._set_item_state(item["item_id"], "object_too_large", method=method, error_code=status)
            elif status == "insufficient_local_space":
                self._set_item_state(item["item_id"], status, method=method, error_code=status)
            else:
                self._set_item_state(item["item_id"], "retry_wait", method=method, error_code=result.get("error_code") or status)
        return resolved

    @contextmanager
    def _run_lock(self):
        lock_path = self.db_path + ".lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(lock_path, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _properties(self, role, **extra):
        properties = {
            "wgo_schema": str(SCHEMA_VERSION),
            "wgo_archive_id": self.archive_id,
            "wgo_role": role,
        }
        for key, value in extra.items():
            if value not in {None, ""}:
                properties[f"wgo_{key}"] = str(value)
        return properties

    @staticmethod
    def _folder_key(role, chat_key="", source_month="", shard=""):
        return "|".join((role, chat_key, source_month, shard))

    def _folder_row(self, role, chat_key="", source_month="", shard=""):
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT * FROM drive_folders WHERE folder_key = ?",
                (self._folder_key(role, chat_key, source_month, shard),),
            ).fetchone()
        finally:
            conn.close()

    def _record_folder(self, role, file_id, chat_key="", source_month="", shard=""):
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO drive_folders(
                    folder_key, role, chat_key, source_month, shard, drive_file_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(folder_key) DO UPDATE SET
                    drive_file_id = excluded.drive_file_id,
                    updated_at = excluded.updated_at
                """,
                (
                    self._folder_key(role, chat_key, source_month, shard), role,
                    chat_key, source_month, shard, file_id, self.now_func(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _valid_item(self, item, role):
        properties = item.get("appProperties") or {}
        return (
            not item.get("trashed", False)
            and properties.get("wgo_schema") == str(SCHEMA_VERSION)
            and properties.get("wgo_archive_id") == self.archive_id
            and properties.get("wgo_role") == role
        )

    def _ensure_root(self):
        row = self._folder_row("root")
        if row:
            try:
                item = self.drive_client.get_file(row["drive_file_id"])
            except GoogleDriveRetryableError:
                raise
            except GoogleDriveError as exc:
                self._meta_set("root_state", "missing")
                raise RemoteDegraded("root_missing_or_inaccessible") from exc
            if not self._valid_item(item, "root"):
                self._meta_set("root_state", "trashed" if item.get("trashed") else "invalid")
                raise RemoteDegraded("root_missing_or_inaccessible")
            self._meta_set("root_state", "known")
            self._meta_set("root_web_view_link", item.get("webViewLink") or "")
            return item

        props = self._properties("root")
        matches = sorted(self.drive_client.find_by_properties(props, mime_type=FOLDER_MIME), key=lambda value: value["id"])
        if len(matches) > 1:
            self._meta_set("root_state", "duplicate")
            raise RemoteDegraded("remote_duplicate_root")
        item = matches[0] if matches else self.drive_client.create_folder(
            _safe_name(self.config.get("google_drive_file_sync_root_name"), "微信群文件归档"),
            "",
            props,
        )
        if not self._valid_item(item, "root"):
            raise RemoteDegraded("root_create_unverified")
        self._record_folder("root", item["id"])
        self._meta_set("root_state", "known")
        self._meta_set("root_web_view_link", item.get("webViewLink") or "")
        return item

    def _ensure_folder(self, role, name, parent_id, *, chat_key="", source_month="", shard=""):
        row = self._folder_row(role, chat_key, source_month, shard)
        if row:
            try:
                item = self.drive_client.get_file(row["drive_file_id"])
                if self._valid_item(item, role):
                    return item
            except GoogleDriveError:
                pass
        extras = {}
        if chat_key:
            extras["chat_key"] = chat_key
        if source_month:
            extras["month"] = source_month
        if shard:
            extras["shard"] = shard
        props = self._properties(role, **extras)
        matches = sorted(
            self.drive_client.find_by_properties(props, parent_id=parent_id, mime_type=FOLDER_MIME),
            key=lambda value: value["id"],
        )
        item = matches[0] if matches else self.drive_client.create_folder(
            _safe_name(name, role), parent_id, props
        )
        if not self._valid_item(item, role):
            raise RemoteDegraded("folder_unverified")
        self._record_folder(role, item["id"], chat_key, source_month, shard)
        return item

    def _ensure_layout(self, item):
        root = self._ensure_root()
        chats_root = self._ensure_folder("chats_root", "群聊", root["id"])
        system_root = self._ensure_folder("system_root", "_系统", root["id"])
        objects_root = self._ensure_folder("objects_root", "objects", system_root["id"])
        shard = self._ensure_folder(
            "shard", item["object_sha256"][:2], objects_root["id"], shard=item["object_sha256"][:2]
        )
        chat_folder = self._ensure_folder(
            "chat", item["chat_alias"], chats_root["id"], chat_key=item["chat_key"]
        )
        month_folder = self._ensure_folder(
            "month", item["source_month"], chat_folder["id"],
            chat_key=item["chat_key"], source_month=item["source_month"],
        )
        return shard, chat_folder, month_folder

    def _local_object(self, item):
        path = os.path.realpath(os.path.join(self.archive_root, item["object_relpath"]))
        archive_root = os.path.realpath(self.archive_root)
        if os.path.commonpath((path, archive_root)) != archive_root:
            raise DriveSyncError("local_object_outside_archive")
        size, sha256, md5 = _hash_path(path)
        if sha256 != item["object_sha256"] or size != int(item["object_size"]):
            raise DriveSyncError("local_object_mismatch")
        return path, size, sha256, md5

    @staticmethod
    def _verify_remote_object(remote, size, sha256, md5):
        try:
            remote_size = int(remote.get("size"))
        except (TypeError, ValueError):
            return False, "remote_size_missing"
        if remote_size != int(size):
            return False, "remote_size_mismatch"
        remote_sha = str(remote.get("sha256Checksum") or "").lower()
        if remote_sha:
            return (remote_sha == sha256, "" if remote_sha == sha256 else "remote_sha256_mismatch")
        remote_md5 = str(remote.get("md5Checksum") or "").lower()
        if remote_md5:
            return (remote_md5 == md5, "" if remote_md5 == md5 else "remote_md5_mismatch")
        return False, "remote_checksum_unavailable"

    def _record_object(self, sha256, size, md5, mime_type, remote, state, error_code=""):
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO drive_objects(
                    sha256, size, md5, mime_type, drive_file_id,
                    verification_state, web_view_link, last_error_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sha256) DO UPDATE SET
                    size = excluded.size, md5 = excluded.md5, mime_type = excluded.mime_type,
                    drive_file_id = excluded.drive_file_id,
                    verification_state = excluded.verification_state,
                    web_view_link = excluded.web_view_link,
                    last_error_code = excluded.last_error_code,
                    updated_at = excluded.updated_at
                """,
                (
                    sha256, size, md5, mime_type, remote.get("id") or "", state,
                    remote.get("webViewLink") or "", error_code, self.now_func(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _object_row(self, sha256):
        conn = self._connect()
        try:
            return conn.execute("SELECT * FROM drive_objects WHERE sha256 = ?", (sha256,)).fetchone()
        finally:
            conn.close()

    def _ensure_object(self, item, shard, budget):
        path, size, sha256, md5 = self._local_object(item)
        mime_type = mimetypes.guess_type(item["original_name"])[0] or "application/octet-stream"
        row = self._object_row(sha256)
        candidates = []
        if row and row["drive_file_id"]:
            try:
                candidates = [self.drive_client.get_file(row["drive_file_id"])]
            except GoogleDriveError:
                candidates = []
        props = self._properties("object", sha256=sha256)
        if not candidates:
            candidates = sorted(
                self.drive_client.find_by_properties(props), key=lambda value: value["id"]
            )
        duplicate = len(candidates) > 1
        if candidates:
            remote = candidates[0]
            if not self._valid_item(remote, "object"):
                raise RemoteDegraded("remote_object_invalid")
            verified, error = self._verify_remote_object(remote, size, sha256, md5)
            self._record_object(
                sha256, size, md5, mime_type, remote,
                "uploaded_verified" if verified else "verification_failed",
                "remote_duplicate_detected" if verified and duplicate else error,
            )
            if not verified:
                raise RemoteDegraded(error)
            return remote, False, 0

        if budget["uploads"] >= budget["max_uploads"] or budget["bytes"] + size > budget["max_bytes"]:
            raise RunBudgetExhausted("run_budget_exhausted")
        self._set_item_state(item["item_id"], "uploading")
        remote = self.drive_client.upload_file(
            path,
            f"{sha256}--{_safe_name(item['original_name'])}",
            shard["id"],
            props,
            mime_type=mime_type,
        )
        if self.after_remote_object:
            self.after_remote_object(remote, item)
        if not self._valid_item(remote, "object"):
            raise RemoteDegraded("uploaded_object_invalid")
        verified, error = self._verify_remote_object(remote, size, sha256, md5)
        self._record_object(
            sha256, size, md5, mime_type, remote,
            "uploaded_verified" if verified else "verification_failed", error,
        )
        if not verified:
            raise RemoteDegraded(error)
        budget["uploads"] += 1
        budget["bytes"] += size
        return remote, True, size

    def _placement_row(self, item):
        conn = self._connect()
        try:
            return conn.execute(
                """
                SELECT * FROM drive_placements
                WHERE chat_key = ? AND source_month = ? AND sha256 = ?
                """,
                (item["chat_key"], item["source_month"], item["object_sha256"]),
            ).fetchone()
        finally:
            conn.close()

    def _placement_name(self, item, month_folder):
        preferred = _safe_name(item["original_name"])
        used = {}
        conn = self._connect()
        try:
            for row in conn.execute(
                "SELECT sha256, display_name FROM drive_placements WHERE chat_key = ? AND source_month = ?",
                (item["chat_key"], item["source_month"]),
            ):
                used[row["display_name"]] = row["sha256"]
        finally:
            conn.close()
        for child in self.drive_client.list_children(month_folder["id"]):
            props = child.get("appProperties") or {}
            if props.get("wgo_archive_id") == self.archive_id and props.get("wgo_role") == "placement":
                used[str(child.get("name") or "")] = str(props.get("wgo_sha256") or "")
        if preferred not in used or used[preferred] == item["object_sha256"]:
            return preferred
        return _shortcut_conflict_name(preferred, item["object_sha256"])

    def _record_placement(self, item, shortcut, chat_folder, month_folder, display_name, error_code=""):
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO drive_placements(
                    chat_key, source_month, sha256, shortcut_id, chat_folder_id,
                    month_folder_id, display_name, verification_state, last_error_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'complete', ?, ?)
                ON CONFLICT(chat_key, source_month, sha256) DO UPDATE SET
                    shortcut_id = excluded.shortcut_id,
                    chat_folder_id = excluded.chat_folder_id,
                    month_folder_id = excluded.month_folder_id,
                    display_name = excluded.display_name,
                    verification_state = 'complete',
                    last_error_code = excluded.last_error_code,
                    updated_at = excluded.updated_at
                """,
                (
                    item["chat_key"], item["source_month"], item["object_sha256"],
                    shortcut["id"], chat_folder["id"], month_folder["id"],
                    display_name, error_code, self.now_func(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _ensure_placement(self, item, remote_object, chat_folder, month_folder):
        row = self._placement_row(item)
        candidates = []
        if row and row["shortcut_id"]:
            try:
                candidates = [self.drive_client.get_file(row["shortcut_id"])]
            except GoogleDriveError:
                candidates = []
        props = self._properties(
            "placement",
            sha256=item["object_sha256"],
            chat_key=item["chat_key"],
            month=item["source_month"],
        )
        if not candidates:
            candidates = sorted(
                self.drive_client.find_by_properties(
                    props, parent_id=month_folder["id"], mime_type=SHORTCUT_MIME
                ),
                key=lambda value: value["id"],
            )
        duplicate = len(candidates) > 1
        if candidates:
            shortcut = candidates[0]
            details = shortcut.get("shortcutDetails") or {}
            if not self._valid_item(shortcut, "placement") or details.get("targetId") != remote_object["id"]:
                raise RemoteDegraded("remote_shortcut_invalid")
            display_name = str(
                shortcut.get("name") or (row["display_name"] if row else "")
            )
            self._record_placement(
                item, shortcut, chat_folder, month_folder, display_name,
                "remote_duplicate_detected" if duplicate else "",
            )
            return shortcut, False
        display_name = self._placement_name(item, month_folder)
        shortcut = self.drive_client.create_shortcut(
            display_name, remote_object["id"], month_folder["id"], props
        )
        if self.after_remote_shortcut:
            self.after_remote_shortcut(shortcut, item)
        details = shortcut.get("shortcutDetails") or {}
        if not self._valid_item(shortcut, "placement") or details.get("targetId") != remote_object["id"]:
            raise RemoteDegraded("created_shortcut_invalid")
        self._record_placement(item, shortcut, chat_folder, month_folder, display_name)
        return shortcut, True

    def _due_remote_items(self, reconcile=False):
        chat_keys = self._active_chat_keys()
        if not chat_keys:
            return []
        statuses = list(REMOTE_STATES)
        if reconcile:
            statuses.append("complete")
        placeholders = ",".join("?" for _ in statuses)
        chat_placeholders = ",".join("?" for _ in chat_keys)
        conn = self._connect()
        try:
            return conn.execute(
                f"""
                SELECT * FROM drive_sync_items
                WHERE status IN ({placeholders})
                  AND chat_key IN ({chat_placeholders})
                  AND (next_retry_at <= ? OR status IN ('ready_local', 'upload_pending',
                                                        'uploading', 'uploaded_verified', 'shortcut_pending',
                                                        'auth_required', 'remote_degraded', 'complete'))
                ORDER BY CASE WHEN status IN ('ready_local', 'upload_pending') THEN 0 ELSE 1 END,
                         next_retry_at, item_id
                """,
                (*statuses, *chat_keys, self.now_func()),
            ).fetchall()
        finally:
            conn.close()

    def _notify_once(self, key, title, message):
        if self._meta_get("notification_episode") == key:
            return
        try:
            self.notifier(title, message)
        except Exception:
            return
        self._meta_set("notification_episode", key)

    def _clear_notification(self):
        self._meta_set("notification_episode", "")

    def _record_run(self, action, result):
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO drive_sync_runs(
                    action, state, scanned, queued, resolved, uploaded,
                    shortcuts, completed, error_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action, result.get("state", "unknown"), int(result.get("scanned", 0)),
                    int(result.get("queued", 0)), int(result.get("resolved", 0)),
                    int(result.get("uploaded", 0)), int(result.get("shortcuts", 0)),
                    int(result.get("completed", 0)), str(result.get("error_code") or ""),
                    self.now_func(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def run(self, *, scan_first=True, reconcile=False):
        enabled, paused = self._control_state()
        if not enabled:
            return {"state": "disabled", "scanned": 0, "queued": 0, "uploaded": 0}
        if paused:
            return {"state": "paused", "scanned": 0, "queued": 0, "uploaded": 0}
        with self._run_lock() as acquired:
            if not acquired:
                return {"state": "worker_busy", "scanned": 0, "queued": 0, "uploaded": 0}
            scan_result = self.scan() if scan_first else {"state": "healthy", "scanned": 0, "queued": 0}
            resolved = self._resolve_due()
            result = {
                "state": (
                    "source_degraded"
                    if scan_result.get("state") == "source_degraded"
                    else "healthy"
                ),
                "scanned": int(scan_result.get("scanned", 0)),
                "queued": int(scan_result.get("queued", 0)),
                "resolved": resolved,
                "uploaded": 0,
                "upload_bytes": 0,
                "shortcuts": 0,
                "completed": 0,
                "error_code": str(scan_result.get("error_code") or ""),
            }
            if self.drive_client is None:
                result.update(state="auth_required", error_code="drive_client_unavailable")
                self._record_run("reconcile" if reconcile else "run", result)
                return result
            budget = {
                "uploads": 0,
                "bytes": 0,
                "max_uploads": max(1, int(self.config.get("google_drive_file_sync_max_uploads_per_run", 20))),
                "max_bytes": max(1, int(self.config.get("google_drive_file_sync_max_bytes_per_run", 512 * 1024 * 1024))),
            }
            for item in self._due_remote_items(reconcile=reconcile):
                enabled, paused = self._control_state()
                if not enabled or paused:
                    result["state"] = "stopped_after_current_item"
                    break
                try:
                    shard, chat_folder, month_folder = self._ensure_layout(item)
                    remote_object, uploaded, uploaded_bytes = self._ensure_object(item, shard, budget)
                    self._set_item_state(item["item_id"], "shortcut_pending")
                    refreshed = self._item(item["item_id"])
                    _shortcut, created = self._ensure_placement(
                        refreshed, remote_object, chat_folder, month_folder
                    )
                    self._set_item_state(item["item_id"], "complete")
                    result["uploaded"] += int(uploaded)
                    result["upload_bytes"] += int(uploaded_bytes)
                    result["shortcuts"] += int(created)
                    result["completed"] += 1
                    self._meta_set("last_verified_upload_at", self.now_func())
                    self._clear_notification()
                except RunBudgetExhausted:
                    result["state"] = "budget_exhausted"
                    result["error_code"] = "run_budget_exhausted"
                    break
                except GoogleDriveAuthRequired as exc:
                    self._set_item_state(item["item_id"], "auth_required", error_code=exc.code)
                    result.update(state="auth_required", error_code=exc.code)
                    self._notify_once(
                        "auth_required",
                        "Google Drive 需要重新授权",
                        "群文件队列已保留；重新授权后会继续同步。",
                    )
                    break
                except GoogleDriveRetryableError as exc:
                    self._set_item_state(
                        item["item_id"], "retry_wait", error_code=exc.code,
                        retry_after=exc.retry_after,
                    )
                    result.update(state="retry_wait", error_code=exc.code)
                except (RemoteDegraded, GoogleDriveError) as exc:
                    code = exc.code if hasattr(exc, "code") else "remote_degraded"
                    self._set_item_state(item["item_id"], "remote_degraded", error_code=code)
                    result.update(state="remote_degraded", error_code=code)
                    self._notify_once(
                        "remote_degraded",
                        "Google Drive 群文件归档需要检查",
                        "远端 root 或应用创建的对象不可用；程序没有创建第二个 root。",
                    )
                    break
                except (DriveSyncError, OSError, sqlite3.Error) as exc:
                    code = exc.code if hasattr(exc, "code") else "sync_failed"
                    self._set_item_state(item["item_id"], "retry_wait", error_code=code)
                    result.update(state="retry_wait", error_code=code)
            self._record_run("reconcile" if reconcile else "run", result)
            return result

    def _item(self, item_id):
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT * FROM drive_sync_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        finally:
            conn.close()

    def reconcile(self):
        return self.run(scan_first=False, reconcile=True)

    @classmethod
    def inspect_status(cls, config, *, oauth=None):
        """Read privacy-safe local status without creating or migrating the ledger."""
        config = dict(config or {})
        auth = {"state": "auth_required", "connected": False}
        if oauth is not None:
            try:
                auth = oauth.status()
            except Exception:
                pass
        result = {
            "state": "paused" if config.get("google_drive_file_sync_paused") else (
                "enabled" if config.get("google_drive_file_sync_enabled") else "disabled"
            ),
            "auth": auth.get("state", "auth_required"),
            "selected_chat_count": len(selected_drive_sync_chats(config)),
            "queue_counts": {},
            "last_scan_at": 0.0,
            "last_verified_upload_at": 0.0,
            "next_retry_at": 0.0,
            "root_state": "unknown",
            "root_web_view_link": "",
            "source_state": "unknown",
            "source_degraded_shards": 0,
            "uploaded_unique_objects": 0,
            "shortcut_placements": 0,
            "last_error_code": "",
        }
        db_path = os.path.abspath(os.path.expanduser(
            config.get("google_drive_file_sync_db")
            or os.path.join(DATA_DIR, "google_drive_file_sync.db")
        ))
        if not os.path.isfile(db_path):
            return result
        uri = "file:" + urllib.parse.quote(db_path, safe="/") + "?mode=ro"
        conn = None
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            result["queue_counts"] = {
                row["status"]: int(row["count"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM drive_sync_items GROUP BY status"
                )
            }
            objects = conn.execute(
                "SELECT COUNT(*) AS count FROM drive_objects WHERE verification_state = 'uploaded_verified'"
            ).fetchone()
            placements = conn.execute(
                "SELECT COUNT(*) AS count FROM drive_placements WHERE verification_state = 'complete'"
            ).fetchone()
            retry = conn.execute(
                "SELECT MIN(next_retry_at) AS next_retry FROM drive_sync_items WHERE next_retry_at > 0"
            ).fetchone()
            has_shard_state = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'drive_scan_shards'"
            ).fetchone()
            degraded = (
                conn.execute(
                    "SELECT COUNT(*) AS count FROM drive_scan_shards WHERE source_state = 'source_degraded'"
                ).fetchone()
                if has_shard_state
                else {"count": 0}
            )
            meta = {
                row["key"]: row["value"]
                for row in conn.execute(
                    "SELECT key, value FROM drive_meta WHERE key IN ("
                    "'last_scan_at', 'last_verified_upload_at', 'root_state', "
                    "'root_web_view_link', 'source_state', 'source_error_code')"
                )
            }
            last_run = conn.execute(
                "SELECT error_code FROM drive_sync_runs ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
            result.update({
                "last_scan_at": float(meta.get("last_scan_at") or 0),
                "last_verified_upload_at": float(
                    meta.get("last_verified_upload_at") or 0
                ),
                "next_retry_at": float(retry["next_retry"] or 0),
                "root_state": str(meta.get("root_state") or "unknown"),
                "root_web_view_link": str(meta.get("root_web_view_link") or ""),
                "source_state": str(meta.get("source_state") or "unknown"),
                "source_degraded_shards": int(degraded["count"]),
                "uploaded_unique_objects": int(objects["count"]),
                "shortcut_placements": int(placements["count"]),
                "last_error_code": (
                    str(last_run["error_code"] or "")
                    if last_run and last_run["error_code"]
                    else str(meta.get("source_error_code") or "")
                ),
            })
        except (OSError, sqlite3.Error, TypeError, ValueError):
            result["last_error_code"] = "local_ledger_unreadable"
        finally:
            if conn is not None:
                conn.close()
        return result

    def status(self):
        return self.inspect_status(self.config, oauth=self.oauth)

    def _last_error_code(self):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT error_code FROM drive_sync_runs ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
            return str(row["error_code"] or "") if row else ""
        finally:
            conn.close()
