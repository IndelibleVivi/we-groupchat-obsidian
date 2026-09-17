import hashlib
import json
import os
import sqlite3
import tempfile
import unittest

from core.google_drive_auth import GoogleDriveAuthRequired
from core.google_drive_client import (
    FOLDER_MIME,
    SHORTCUT_MIME,
    GoogleDriveError,
    GoogleDriveRetryableError,
)
from core.google_drive_file_sync import DriveSyncError, GoogleDriveFileSync, _month
from core.wechat_db import WeChatSourceDegraded


ARCHIVE_ID = "11111111-2222-4333-8444-555555555555"


def keyset_page(rows, *, cursor_token="", since_ts=0, limit=500):
    """Emulate the production ``(create_time, rowid)`` keyset page.

    The fixture position is the row's 1-based order in the shard fixture, so a
    bucket larger than the page size still pages completely, exactly like the
    real macOS reader. This keeps the fixture production-shaped instead of
    exercising the legacy timestamp/de-duplication fallback.
    """
    page_limit = max(1, int(limit))
    positioned = sorted(
        (
            (int(message["timestamp"]), position, message)
            for position, message in enumerate(rows, start=1)
        ),
        key=lambda item: (item[0], item[1]),
    )
    after = (
        tuple(json.loads(cursor_token))
        if cursor_token
        else (max(0, int(since_ts)), 0)
    )
    pending = [
        entry for entry in positioned if (entry[0], entry[1]) > after
    ]
    page = pending[:page_limit]
    next_cursor = cursor_token
    if page:
        next_cursor = json.dumps(
            [page[-1][0], page[-1][1]], separators=(",", ":")
        )
    return {
        "messages": [entry[2] for entry in page],
        "next_cursor": next_cursor,
        "exhausted": len(pending) <= page_limit,
    }


def file_message(identity, timestamp, name, data=None, *, declared_hash="", kind="file"):
    resource = {
        "kind": kind,
        "resource_index": 0,
        "original_name": name,
    }
    if data is not None:
        resource["declared_size"] = len(data)
    if declared_hash:
        resource["declared_hash"] = declared_hash
    return {
        "source_message_id": identity,
        "timestamp": timestamp,
        "resources": [resource],
        "text": "private body must not persist",
    }


class FakeSource:
    def __init__(self, messages_by_chat):
        self.messages_by_chat = messages_by_chat
        self.calls = []

    def get_messages(
        self,
        username,
        *,
        since_ts=0,
        limit=500,
        page_forward=False,
        since_inclusive=False,
    ):
        self.calls.append((username, since_ts, limit, page_forward, since_inclusive))
        comparison = (
            lambda message: message["timestamp"] >= since_ts
            if since_inclusive
            else message["timestamp"] > since_ts
        )
        rows = [dict(message) for message in self.messages_by_chat.get(username, []) if comparison(message)]
        rows.sort(key=lambda message: (message["timestamp"], message["source_message_id"]))
        return rows[:limit] if page_forward else rows[-limit:]

    def get_message_shards(self, _username):
        return ["fixture-shard"]

    def get_messages_for_shard(
        self,
        username,
        _source_shard_id,
        *,
        since_ts=0,
        limit=500,
        page_forward=False,
        since_inclusive=False,
    ):
        return self.get_messages(
            username,
            since_ts=since_ts,
            limit=limit,
            page_forward=page_forward,
            since_inclusive=since_inclusive,
        )

    def get_cursor_page_for_shard(
        self,
        username,
        _source_shard_id,
        *,
        cursor_token="",
        since_ts=0,
        limit=500,
    ):
        return keyset_page(
            self.messages_by_chat.get(username, []),
            cursor_token=cursor_token,
            since_ts=since_ts,
            limit=limit,
        )


class RecoveringShardSource:
    def __init__(self, chat, shard_messages):
        self.chat = chat
        self.shard_messages = shard_messages
        self.failed = {"shard-a"}

    def get_message_shards(self, username):
        return ["shard-a", "shard-b"] if username == self.chat else []

    def get_messages_for_shard(
        self,
        username,
        source_shard_id,
        *,
        since_ts=0,
        limit=500,
        page_forward=False,
        since_inclusive=False,
    ):
        if username != self.chat or source_shard_id in self.failed:
            raise WeChatSourceDegraded("source_shard_unavailable")
        rows = [
            dict(message)
            for message in self.shard_messages[source_shard_id]
            if message["timestamp"] >= since_ts
            and not (
                message["timestamp"] == since_ts
                and not since_inclusive
            )
        ]
        rows.sort(key=lambda message: (message["timestamp"], message["source_message_id"]))
        return rows[:limit] if page_forward else rows[-limit:]

    def get_cursor_page_for_shard(
        self,
        username,
        source_shard_id,
        *,
        cursor_token="",
        since_ts=0,
        limit=500,
    ):
        if username != self.chat or source_shard_id in self.failed:
            raise WeChatSourceDegraded("source_shard_unavailable")
        return keyset_page(
            self.shard_messages[source_shard_id],
            cursor_token=cursor_token,
            since_ts=since_ts,
            limit=limit,
        )


class InventoryDriveSource(RecoveringShardSource):
    def __init__(self, chat, shard_messages):
        super().__init__(chat, shard_messages)
        self.failed = set()
        self.complete = False
        self.digest = "inventory-missing-b"
        self.present = ["shard-a"]

    def get_source_inventory(self, *, update=True, sensitive=False):
        del update, sensitive
        missing = 0 if self.complete else 1
        return {
            "schema": "we-groupchat-obsidian.source-inventory.v1",
            "source_namespace": "opaque-source",
            "inventory_revision": 1,
            "inventory_digest": self.digest,
            "complete": self.complete,
            "counts": {
                "present": len(self.present),
                "missing_file": missing,
            },
            "error_codes": [] if self.complete else ["source_missing_file"],
            "present_generation_ids": list(self.present),
            "shards": [],
        }


class FakeDrive:
    def __init__(self):
        self.items = {}
        self.next_id = 1
        self.upload_calls = 0
        self.shortcut_calls = 0
        self.fail_mode = ""
        self.bad_checksum = False

    def _id(self):
        value = f"drive-{self.next_id:04d}"
        self.next_id += 1
        return value

    def _maybe_fail(self):
        if self.fail_mode == "auth":
            raise GoogleDriveAuthRequired("invalid_grant")
        if self.fail_mode == "retry":
            raise GoogleDriveRetryableError("drive_http_429", status_code=429, retry_after=17)

    def get_file(self, file_id):
        self._maybe_fail()
        if file_id not in self.items:
            raise GoogleDriveError("drive_http_404", status_code=404)
        return dict(self.items[file_id])

    def find_by_properties(self, properties, *, parent_id="", mime_type=""):
        self._maybe_fail()
        rows = []
        for item in self.items.values():
            if item.get("trashed"):
                continue
            if mime_type and item.get("mimeType") != mime_type:
                continue
            if parent_id and parent_id not in item.get("parents", []):
                continue
            actual = item.get("appProperties") or {}
            if all(actual.get(key) == value for key, value in properties.items()):
                rows.append(dict(item))
        return rows

    def list_children(self, parent_id):
        self._maybe_fail()
        return [
            dict(item)
            for item in self.items.values()
            if not item.get("trashed") and parent_id in item.get("parents", [])
        ]

    def create_folder(self, name, parent_id, app_properties):
        self._maybe_fail()
        file_id = self._id()
        item = {
            "id": file_id,
            "name": name,
            "mimeType": FOLDER_MIME,
            "parents": [parent_id] if parent_id else [],
            "appProperties": dict(app_properties),
            "trashed": False,
            "webViewLink": f"https://drive.invalid/{file_id}",
        }
        self.items[file_id] = item
        return dict(item)

    def upload_file(self, path, name, parent_id, app_properties, *, mime_type=""):
        self._maybe_fail()
        self.upload_calls += 1
        with open(path, "rb") as handle:
            data = handle.read()
        file_id = self._id()
        sha256 = hashlib.sha256(data).hexdigest()
        item = {
            "id": file_id,
            "name": name,
            "size": str(len(data)),
            "sha256Checksum": "0" * 64 if self.bad_checksum else sha256,
            "md5Checksum": hashlib.md5(data).hexdigest(),
            "mimeType": mime_type,
            "parents": [parent_id],
            "appProperties": dict(app_properties),
            "trashed": False,
            "webViewLink": f"https://drive.invalid/{file_id}",
        }
        self.items[file_id] = item
        return dict(item)

    def create_shortcut(self, name, target_id, parent_id, app_properties):
        self._maybe_fail()
        self.shortcut_calls += 1
        file_id = self._id()
        item = {
            "id": file_id,
            "name": name,
            "mimeType": SHORTCUT_MIME,
            "parents": [parent_id],
            "shortcutDetails": {"targetId": target_id},
            "appProperties": dict(app_properties),
            "trashed": False,
            "webViewLink": f"https://drive.invalid/{file_id}",
        }
        self.items[file_id] = item
        return dict(item)


class FakeOAuth:
    def __init__(self, connected=True):
        self.connected = connected

    def status(self):
        return {"state": "connected" if self.connected else "auth_required", "connected": self.connected}


class GoogleDriveFileSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_dir = os.path.join(self.tmp.name, "xwechat_files", "wxid_fixture", "db_storage")
        os.makedirs(self.db_dir)
        self.archive_root = os.path.join(self.tmp.name, "archive")
        self.ledger = os.path.join(self.tmp.name, "drive-sync.db")
        self.timestamp = 1_780_000_000
        self.chat_a = "alpha-room@chatroom"
        self.chat_b = "beta-room@chatroom"
        self.config = {
            "db_dir": self.db_dir,
            "monitor_knowledge_db": os.path.join(self.tmp.name, "knowledge.db"),
            "attachment_archive_root": self.archive_root,
            "attachment_archive_max_object_bytes": 1024 * 1024,
            "attachment_archive_min_free_bytes": 0,
            "google_drive_file_sync_db": self.ledger,
            "google_drive_file_sync_enabled": True,
            "google_drive_file_sync_paused": False,
            "google_drive_file_sync_selected_chats": [
                {"username": self.chat_a, "alias": "Alpha group"},
            ],
            "google_drive_file_sync_max_messages_per_scan": 2,
            "google_drive_file_sync_max_uploads_per_run": 20,
            "google_drive_file_sync_max_bytes_per_run": 1024 * 1024,
            "google_drive_file_sync_root_name": "微信群文件归档",
            "google_drive_file_sync_retry_base_seconds": 10,
            "google_drive_file_sync_retry_max_seconds": 40,
        }
        self.drive = FakeDrive()

    def tearDown(self):
        self.tmp.cleanup()

    @property
    def file_root(self):
        return os.path.join(os.path.dirname(self.db_dir), "msg", "file")

    def cache(self, name, data, timestamp=None):
        directory = os.path.join(self.file_root, _month(timestamp or self.timestamp))
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def service(self, source, **kwargs):
        return GoogleDriveFileSync(
            self.config,
            source=source,
            drive_client=self.drive,
            oauth=FakeOAuth(),
            now_func=lambda: self.timestamp + 100,
            random_func=lambda: 0.5,
            archive_id_factory=lambda: ARCHIVE_ID,
            **kwargs,
        )

    def rows(self, table):
        conn = sqlite3.connect(self.ledger)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        finally:
            conn.close()

    def initialize(self, service, timestamp=None):
        service.initialize_selected_chat_cursors(
            self.timestamp - 1 if timestamp is None else timestamp
        )

    def test_scans_only_selected_chat_without_knowledge_gate_and_cursor_survives_restart(self):
        data = b"selected bytes"
        source = FakeSource({
            self.chat_a: [
                file_message("wgmsg_a1", self.timestamp, "one.txt", data),
                file_message("wgmsg_a2", self.timestamp, "two.txt", data),
                file_message("wgmsg_a3", self.timestamp, "three.txt", data),
            ],
            self.chat_b: [file_message("wgmsg_b1", self.timestamp, "private.txt", data)],
        })
        service = self.service(source)
        self.initialize(service)

        first = service.scan()
        restarted = self.service(source)
        second = restarted.scan()

        self.assertEqual(first["queued"], 2)
        self.assertEqual(second["queued"], 1)
        items = self.rows("drive_sync_items")
        self.assertEqual({row["source_message_id"] for row in items}, {"wgmsg_a1", "wgmsg_a2", "wgmsg_a3"})
        self.assertTrue(all(row["chat_username"] == self.chat_a for row in items))
        self.assertFalse(os.path.exists(self.config["monitor_knowledge_db"]))
        self.assertNotIn(self.chat_b, {call[0] for call in source.calls})

    def test_large_same_second_bucket_pages_completely_across_restarts(self):
        data = b"same-second bytes"
        source = FakeSource({
            self.chat_a: [
                file_message(f"wgmsg_same_{index}", self.timestamp, f"{index}.txt", data)
                for index in range(5)
            ],
        })
        service = self.service(source)
        self.initialize(service)

        queued = []
        for _ in range(6):
            result = self.service(source).scan()
            queued.append(result["queued"])
            if result["queued"] == 0:
                break

        self.assertEqual(queued, [2, 2, 1, 0])
        items = self.rows("drive_sync_items")
        self.assertEqual(
            sorted(row["source_message_id"] for row in items),
            [f"wgmsg_same_{index}" for index in range(5)],
        )
        self.assertEqual(len(items), 5)
        shard = self.rows("drive_scan_shards")[0]
        self.assertEqual(
            shard["source_cursor_token"], f"[{self.timestamp},5]"
        )
        self.assertEqual(shard["source_state"], "healthy")

    def test_decode_failure_reports_exact_code_and_never_advances(self):
        class EmptyShardSource:
            def get_message_shards(self, _username):
                return ["shard-a"]

            def get_cursor_page_for_shard(
                self,
                _username,
                _source_shard_id,
                *,
                cursor_token="",
                since_ts=0,
                limit=500,
            ):
                del since_ts, limit
                return {
                    "messages": [],
                    "next_cursor": cursor_token,
                    "exhausted": True,
                }

        class DecodeFailingSource:
            def get_message_shards(self, _username):
                return ["shard-a"]

            def get_cursor_page_for_shard(
                self,
                _username,
                _source_shard_id,
                *,
                cursor_token="",
                since_ts=0,
                limit=500,
            ):
                del cursor_token, since_ts, limit
                raise WeChatSourceDegraded("source_message_decode_failed")

        service = self.service(EmptyShardSource())
        self.initialize(service)
        service.scan()
        before = self.rows("drive_scan_shards")
        self.assertEqual(len(before), 1)

        degraded = self.service(DecodeFailingSource()).scan()

        self.assertEqual(degraded["state"], "source_degraded")
        self.assertEqual(degraded["error_code"], "source_message_decode_failed")
        self.assertEqual(degraded["queued"], 0)
        self.assertEqual(self.rows("drive_sync_items"), [])
        after = self.rows("drive_scan_shards")
        self.assertEqual(len(after), 1)
        self.assertEqual(
            after[0]["cursor_timestamp"], before[0]["cursor_timestamp"]
        )
        self.assertEqual(
            after[0]["source_cursor_token"], before[0]["source_cursor_token"]
        )
        self.assertEqual(after[0]["source_state"], "source_degraded")
        self.assertEqual(
            after[0]["last_error_code"], "source_message_decode_failed"
        )
        status = self.service(DecodeFailingSource()).status()
        self.assertEqual(status["source_state"], "source_degraded")
        self.assertNotIn(self.chat_a, json.dumps(degraded))

    def test_old_ledger_gains_the_cursor_token_column_without_losing_state(self):
        class LegacyTimestampSource:
            """Pre-keyset reader: timestamp pages only, no opaque cursor."""

            def __init__(self, rows):
                self.rows = [dict(row) for row in rows]

            def get_message_shards(self, _username):
                return ["shard-a"]

            def get_messages_for_shard(
                self,
                _username,
                _source_shard_id,
                *,
                since_ts=0,
                limit=500,
                page_forward=False,
                since_inclusive=False,
            ):
                rows = [
                    dict(row)
                    for row in self.rows
                    if (
                        int(row["timestamp"]) >= since_ts
                        if since_inclusive
                        else int(row["timestamp"]) > since_ts
                    )
                ]
                rows.sort(
                    key=lambda row: (row["timestamp"], row["source_message_id"])
                )
                return rows[:limit]

        def legacy_rows():
            return [
                file_message("wgmsg_old_0", self.timestamp, "0.txt", b"x"),
                file_message("wgmsg_old_1", self.timestamp, "1.txt", b"x"),
                file_message("wgmsg_old_2", self.timestamp, "2.txt", b"x"),
            ]

        self.service(FakeSource({}))
        conn = sqlite3.connect(self.ledger)
        try:
            conn.execute(
                """
                INSERT INTO drive_scan_state(
                    chat_username, cursor_timestamp, cursor_message_ids_json, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (self.chat_a, self.timestamp, '["wgmsg_old_0","wgmsg_old_1"]', 1.0),
            )
            conn.execute(
                """
                INSERT INTO drive_scan_shards(
                    chat_username, source_shard_id, cursor_timestamp,
                    cursor_message_ids_json, source_state, last_error_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.chat_a,
                    "shard-a",
                    self.timestamp,
                    '["wgmsg_old_0","wgmsg_old_1"]',
                    "source_degraded",
                    "source_shard_unavailable",
                    42.0,
                ),
            )
            conn.execute(
                """
                INSERT INTO drive_sync_items(
                    source_message_id, resource_index, chat_username, chat_key,
                    chat_alias, source_timestamp, source_month, original_name,
                    status, attempt_count, next_retry_at, last_error_code,
                    created_at, updated_at
                ) VALUES ('wgmsg_legacy', 0, ?, 'ck', 'Alpha group', ?, ?, 'legacy.txt',
                          'waiting_cache', 3, 1780009999.0, 'local_file_missing',
                          1780000000.0, 1780000000.0)
                """,
                (self.chat_a, self.timestamp, _month(self.timestamp)),
            )
            conn.execute(
                "INSERT INTO drive_meta(key, value) VALUES ('legacy_marker', 'keep')"
            )
            # Recreate the pre-W1.1 shard table: no source_cursor_token column.
            conn.execute(
                "ALTER TABLE drive_scan_shards DROP COLUMN source_cursor_token"
            )
            conn.commit()
        finally:
            conn.close()

        self.service(LegacyTimestampSource(legacy_rows()))

        conn = sqlite3.connect(self.ledger)
        try:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(drive_scan_shards)")
            }
        finally:
            conn.close()
        self.assertIn("source_cursor_token", columns)

        shard = self.rows("drive_scan_shards")[0]
        self.assertEqual(shard["cursor_timestamp"], self.timestamp)
        self.assertEqual(
            shard["cursor_message_ids_json"], '["wgmsg_old_0","wgmsg_old_1"]'
        )
        self.assertEqual(shard["source_cursor_token"], "")
        self.assertEqual(shard["source_state"], "source_degraded")
        self.assertEqual(shard["last_error_code"], "source_shard_unavailable")
        self.assertEqual(shard["updated_at"], 42.0)

        item = self.rows("drive_sync_items")[0]
        self.assertEqual(item["source_message_id"], "wgmsg_legacy")
        self.assertEqual(item["status"], "waiting_cache")
        self.assertEqual(item["attempt_count"], 3)
        self.assertEqual(item["next_retry_at"], 1780009999.0)
        self.assertEqual(item["last_error_code"], "local_file_missing")

        result = self.service(LegacyTimestampSource(legacy_rows())).scan()

        self.assertEqual(result["state"], "healthy")
        self.assertEqual(result["queued"], 1)
        items = {
            row["source_message_id"]: row for row in self.rows("drive_sync_items")
        }
        self.assertEqual(sorted(items), ["wgmsg_legacy", "wgmsg_old_2"])
        self.assertEqual(items["wgmsg_legacy"]["status"], "waiting_cache")
        self.assertEqual(items["wgmsg_legacy"]["attempt_count"], 3)
        self.assertEqual(items["wgmsg_legacy"]["next_retry_at"], 1780009999.0)
        self.assertEqual(
            items["wgmsg_legacy"]["last_error_code"], "local_file_missing"
        )
        shard = self.rows("drive_scan_shards")[0]
        self.assertEqual(shard["source_cursor_token"], "")
        self.assertEqual(shard["source_state"], "healthy")

    def test_cursor_cas_failure_rolls_back_queue_rows_and_token(self):
        source = FakeSource({
            self.chat_a: [
                file_message("wgmsg_cas", self.timestamp, "cas.txt", b"x")
            ],
        })
        service = self.service(source)
        self.initialize(service)
        original_shard_state = service._shard_state

        def stale_read(chat_username, source_shard_id, seed):
            row = original_shard_state(chat_username, source_shard_id, seed)
            conn = service._connect()
            try:
                conn.execute(
                    """
                    UPDATE drive_scan_shards
                    SET cursor_timestamp = ?, source_cursor_token = ?
                    WHERE chat_username = ? AND source_shard_id = ?
                    """,
                    (
                        int(row["cursor_timestamp"]) + 5,
                        "[7,7]",
                        chat_username,
                        source_shard_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            return row

        service._shard_state = stale_read

        with self.assertRaises(DriveSyncError) as raised:
            service.scan()

        self.assertEqual(raised.exception.code, "source_cursor_changed")
        self.assertEqual(self.rows("drive_sync_items"), [])
        shard = self.rows("drive_scan_shards")[0]
        self.assertEqual(shard["cursor_timestamp"], self.timestamp - 1 + 5)
        self.assertEqual(shard["source_cursor_token"], "[7,7]")

    def test_queue_insert_failure_rolls_back_the_shard_cursor(self):
        source = FakeSource({
            self.chat_a: [
                file_message("wgmsg_insert", self.timestamp, "i.txt", b"x")
            ],
        })
        service = self.service(source)
        self.initialize(service)

        def failing_insert(conn, chat, messages, now):
            conn.execute(
                """
                INSERT INTO drive_sync_items(
                    source_message_id, resource_index, chat_username, chat_key,
                    chat_alias, source_timestamp, source_month, original_name,
                    created_at, updated_at
                ) VALUES ('wgmsg_partial', 0, ?, 'ck', 'Alpha group', ?, ?, 'p.txt', ?, ?)
                """,
                (
                    chat["username"],
                    self.timestamp,
                    _month(self.timestamp),
                    now,
                    now,
                ),
            )
            raise RuntimeError("fixture queue insert failure")

        service._insert_file_items = failing_insert

        with self.assertRaises(RuntimeError):
            service.scan()

        # ``_shard_state`` commits the shard's first observation before the
        # scan's own transaction, so the row must exist but still hold the
        # initial cursor: neither the partial queue row nor the cursor advance
        # may survive the failed page.
        self.assertEqual(self.rows("drive_sync_items"), [])
        shard = self.rows("drive_scan_shards")[0]
        self.assertEqual(shard["cursor_timestamp"], self.timestamp - 1)
        self.assertEqual(shard["cursor_message_ids_json"], "[]")
        self.assertEqual(shard["source_cursor_token"], "")
        self.assertEqual(shard["source_state"], "healthy")

    def test_failed_shard_never_advances_past_unseen_file_and_recovers_exactly_once(self):
        source = RecoveringShardSource(
            self.chat_a,
            {
                "shard-a": [file_message("wgmsg_unseen_a", 100, "unseen.txt")],
                "shard-b": [file_message("wgmsg_seen_b", 200, "seen.txt", kind="image")],
            },
        )
        service = self.service(source)
        service.initialize_selected_chat_cursors(99)

        degraded = service.scan()

        self.assertEqual(degraded["state"], "source_degraded")
        shard_rows = {
            row["source_shard_id"]: row
            for row in self.rows("drive_scan_shards")
        }
        self.assertEqual(shard_rows["shard-a"]["cursor_timestamp"], 99)
        self.assertEqual(shard_rows["shard-a"]["source_state"], "source_degraded")
        self.assertEqual(shard_rows["shard-b"]["cursor_timestamp"], 200)
        self.assertEqual(self.rows("drive_sync_items"), [])
        run_result = service.run()
        self.assertEqual(run_result["state"], "source_degraded")
        receipt = self.rows("drive_sync_runs")[-1]
        receipt_text = json.dumps(receipt)
        self.assertEqual(receipt["error_code"], "source_shard_unavailable")
        self.assertNotIn(self.chat_a, receipt_text)
        self.assertNotIn("shard-a", receipt_text)
        self.assertNotIn("message_", receipt_text)
        status = service.status()
        self.assertEqual(status["source_state"], "source_degraded")
        self.assertEqual(status["source_degraded_shards"], 1)

        source.failed.clear()
        recovered = service.scan()
        repeated = service.scan()

        self.assertEqual(recovered["state"], "healthy")
        self.assertEqual(recovered["queued"], 1)
        self.assertEqual(repeated["queued"], 0)
        self.assertEqual(
            [row["source_message_id"] for row in self.rows("drive_sync_items")],
            ["wgmsg_unseen_a"],
        )

    def test_incomplete_inventory_queues_present_shard_then_recovers_missing_once(self):
        source = InventoryDriveSource(
            self.chat_a,
            {
                "shard-a": [file_message("wgmsg_a", 100, "a.txt")],
                "shard-b": [file_message("wgmsg_b", 200, "b.txt")],
            },
        )
        service = self.service(source)
        service.initialize_selected_chat_cursors(0)

        first = service.scan()
        source.complete = True
        source.digest = "inventory-recovered"
        source.present = ["shard-a", "shard-b"]
        second = service.scan()
        third = service.scan()

        self.assertEqual(first["state"], "source_degraded")
        self.assertFalse(first["source_complete"])
        self.assertEqual(first["queued"], 1)
        self.assertEqual(second["state"], "healthy")
        self.assertTrue(second["source_complete"])
        self.assertEqual(second["queued"], 1)
        self.assertEqual(third["queued"], 0)
        self.assertEqual(
            [row["source_message_id"] for row in self.rows("drive_sync_items")],
            ["wgmsg_a", "wgmsg_b"],
        )

    def test_default_cursor_skips_history_and_explicit_backfill_is_dry_then_apply(self):
        data = b"history"
        source = FakeSource({
            self.chat_a: [file_message("wgmsg_history", self.timestamp - 100, "old.txt", data)]
        })
        service = self.service(source)

        initialized = service.scan()
        plan = service.backfill(self.timestamp - 200, apply=False)
        self.assertEqual(self.rows("drive_sync_items"), [])
        applied = service.backfill(self.timestamp - 200, apply=True)

        self.assertEqual(initialized["initialized_chats"], 1)
        self.assertEqual(plan["discovered_files"], 1)
        self.assertEqual(plan["inserted"], 0)
        self.assertEqual(applied["inserted"], 1)

    def test_same_hash_deduplicates_object_and_placement_but_projects_across_months(self):
        data = b"same global object"
        later = self.timestamp + 40 * 86400
        source = FakeSource({
            self.chat_a: [
                file_message("wgmsg_same1", self.timestamp, "same-a.txt", data),
                file_message("wgmsg_same2", self.timestamp, "same-b.txt", data),
                file_message("wgmsg_same3", later, "same-c.txt", data),
            ]
        })
        for name, ts in (("same-a.txt", self.timestamp), ("same-b.txt", self.timestamp), ("same-c.txt", later)):
            self.cache(name, data, ts)
        service = self.service(source)
        self.config["google_drive_file_sync_max_messages_per_scan"] = 10
        service.config["google_drive_file_sync_max_messages_per_scan"] = 10
        self.initialize(service)

        result = service.run()

        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(self.drive.upload_calls, 1)
        self.assertEqual(self.drive.shortcut_calls, 2)
        self.assertEqual(len(self.rows("drive_objects")), 1)
        self.assertEqual(len(self.rows("drive_placements")), 2)
        self.assertTrue(all(row["status"] == "complete" for row in self.rows("drive_sync_items")))

    def test_successful_state_transitions_do_not_inflate_retry_backoff(self):
        source = FakeSource({
            self.chat_a: [file_message("wgmsg_attempts", self.timestamp, "attempts.txt")]
        })
        service = self.service(source)
        self.initialize(service)
        service.scan()
        item_id = self.rows("drive_sync_items")[0]["item_id"]

        for state in ("upload_pending", "uploading", "shortcut_pending", "complete"):
            service._set_item_state(item_id, state)

        self.assertEqual(self.rows("drive_sync_items")[0]["attempt_count"], 0)
        service._set_item_state(item_id, "retry_wait", error_code="drive_http_503")
        first = self.rows("drive_sync_items")[0]
        service._set_item_state(item_id, "retry_wait", error_code="drive_http_503")
        second = self.rows("drive_sync_items")[0]
        self.assertEqual(first["attempt_count"], 1)
        self.assertEqual(second["attempt_count"], 2)
        self.assertEqual(first["next_retry_at"], self.timestamp + 110)
        self.assertEqual(second["next_retry_at"], self.timestamp + 120)

    def test_same_hash_in_two_chats_uses_one_object_and_two_shortcuts(self):
        data = b"cross chat"
        self.config["google_drive_file_sync_selected_chats"].append(
            {"username": self.chat_b, "alias": "Beta group"}
        )
        source = FakeSource({
            self.chat_a: [file_message("wgmsg_cross_a", self.timestamp, "cross.txt", data)],
            self.chat_b: [file_message("wgmsg_cross_b", self.timestamp, "cross.txt", data)],
        })
        self.cache("cross.txt", data)
        service = self.service(source)
        self.initialize(service)

        service.run()

        self.assertEqual(self.drive.upload_calls, 1)
        self.assertEqual(self.drive.shortcut_calls, 2)
        self.assertEqual(len(self.rows("drive_placements")), 2)

    def test_same_name_different_hash_gets_hash_suffix_without_overwrite(self):
        first = b"first version"
        second = b"second version"
        first_hash = hashlib.sha256(first).hexdigest()
        second_hash = hashlib.sha256(second).hexdigest()
        source = FakeSource({
            self.chat_a: [
                file_message("wgmsg_name1", self.timestamp, "report.pdf", first, declared_hash=first_hash),
                file_message("wgmsg_name2", self.timestamp, "report.pdf", second, declared_hash=second_hash),
            ]
        })
        self.cache("report.pdf", first)
        self.cache("report (1).pdf", second)
        service = self.service(source)
        self.initialize(service)

        service.run()

        names = {row["display_name"] for row in self.rows("drive_placements")}
        self.assertIn("report.pdf", names)
        self.assertIn(f"report--{second_hash[:8]}.pdf", names)
        self.assertEqual(self.drive.upload_calls, 2)

    def test_missing_cache_retries_without_blocking_and_ambiguous_never_uploads(self):
        ready = b"ready"
        source = FakeSource({
            self.chat_a: [
                file_message("wgmsg_missing", self.timestamp, "missing.txt"),
                file_message("wgmsg_ready", self.timestamp + 1, "ready.txt", ready),
                file_message("wgmsg_ambiguous", self.timestamp + 2, "ambiguous.txt"),
            ]
        })
        self.cache("ready.txt", ready, self.timestamp + 1)
        self.cache("ambiguous.txt", b"A", self.timestamp + 2)
        self.cache("ambiguous (1).txt", b"B", self.timestamp + 2)
        self.config["google_drive_file_sync_max_messages_per_scan"] = 10
        service = self.service(source)
        service.config["google_drive_file_sync_max_messages_per_scan"] = 10
        self.initialize(service)

        service.run()

        states = {row["original_name"]: row["status"] for row in self.rows("drive_sync_items")}
        self.assertEqual(states["missing.txt"], "waiting_cache")
        self.assertEqual(states["ambiguous.txt"], "ambiguous")
        self.assertEqual(states["ready.txt"], "complete")
        self.assertEqual(self.drive.upload_calls, 1)

    def test_disabled_and_paused_never_write_remote_and_resume_continues_queue(self):
        data = b"resume"
        source = FakeSource({self.chat_a: [file_message("wgmsg_resume", self.timestamp, "resume.txt", data)]})
        self.cache("resume.txt", data)
        service = self.service(source)
        self.initialize(service)
        service.scan()

        service.config["google_drive_file_sync_enabled"] = False
        disabled = service.run()
        service.config["google_drive_file_sync_enabled"] = True
        service.config["google_drive_file_sync_paused"] = True
        paused = service.run()
        service.config["google_drive_file_sync_paused"] = False
        service.control_state_func = lambda: service.config
        resumed = service.run()

        self.assertEqual(disabled["state"], "disabled")
        self.assertEqual(paused["state"], "paused")
        self.assertEqual(self.drive.upload_calls, 1)
        self.assertEqual(resumed["completed"], 1)

    def test_images_are_ignored_but_do_not_hold_the_scan_cursor(self):
        source = FakeSource({
            self.chat_a: [
                file_message(
                    "wgmsg_image",
                    self.timestamp,
                    "image.png",
                    b"image bytes",
                    kind="image",
                )
            ]
        })
        service = self.service(source)
        self.initialize(service)

        first = service.scan()
        second = service.scan()

        self.assertEqual(first["scanned"], 1)
        self.assertEqual(first["queued"], 0)
        self.assertEqual(second["scanned"], 0)
        self.assertEqual(self.rows("drive_sync_items"), [])

    def test_per_run_budget_stops_before_a_second_object_upload(self):
        source = FakeSource({
            self.chat_a: [
                file_message("wgmsg_budget_a", self.timestamp, "a.txt", b"a"),
                file_message("wgmsg_budget_b", self.timestamp + 1, "b.txt", b"b"),
            ]
        })
        self.cache("a.txt", b"a")
        self.cache("b.txt", b"b", self.timestamp + 1)
        self.config["google_drive_file_sync_max_uploads_per_run"] = 1
        service = self.service(source)
        self.initialize(service)

        result = service.run()

        self.assertEqual(result["state"], "budget_exhausted")
        self.assertEqual(self.drive.upload_calls, 1)
        self.assertEqual(
            [row["status"] for row in self.rows("drive_sync_items")],
            ["complete", "upload_pending"],
        )

    def test_disable_during_an_item_finishes_it_then_stops_before_the_next(self):
        source = FakeSource({
            self.chat_a: [
                file_message("wgmsg_stop_a", self.timestamp, "stop-a.txt", b"a"),
                file_message("wgmsg_stop_b", self.timestamp + 1, "stop-b.txt", b"b"),
            ]
        })
        self.cache("stop-a.txt", b"a")
        self.cache("stop-b.txt", b"b", self.timestamp + 1)
        service = self.service(source)
        self.initialize(service)

        def disable_after_shortcut(_remote, _item):
            service.config["google_drive_file_sync_enabled"] = False

        service.after_remote_shortcut = disable_after_shortcut
        result = service.run()

        self.assertEqual(result["state"], "stopped_after_current_item")
        self.assertEqual(self.drive.upload_calls, 1)
        self.assertEqual(self.drive.shortcut_calls, 1)
        self.assertEqual(
            [row["status"] for row in self.rows("drive_sync_items")],
            ["complete", "upload_pending"],
        )

    def test_reconcile_rebuilds_a_deleted_shortcut_without_uploading_bytes_again(self):
        data = b"reconcile shortcut"
        source = FakeSource({
            self.chat_a: [file_message("wgmsg_reconcile", self.timestamp, "reconcile.txt", data)]
        })
        self.cache("reconcile.txt", data)
        service = self.service(source)
        self.initialize(service)
        service.run()
        shortcut_id = next(
            item_id
            for item_id, item in self.drive.items.items()
            if item.get("appProperties", {}).get("wgo_role") == "placement"
        )
        del self.drive.items[shortcut_id]

        result = service.reconcile()

        self.assertEqual(result["completed"], 1)
        self.assertEqual(self.drive.upload_calls, 1)
        self.assertEqual(self.drive.shortcut_calls, 2)

    def test_root_lookup_preserves_retryable_failure(self):
        service = self.service(FakeSource({}))
        service._record_folder("root", "drive-root")
        self.drive.fail_mode = "retry"

        with self.assertRaises(GoogleDriveRetryableError):
            service._ensure_root()

    def test_deselected_chat_keeps_its_queue_but_stops_resolve_and_remote_work(self):
        data = b"scope retained"
        source = FakeSource({
            self.chat_a: [file_message("wgmsg_deselected", self.timestamp, "scope.txt", data)]
        })
        self.cache("scope.txt", data)
        service = self.service(source)
        self.initialize(service)
        service.scan()
        service.config["google_drive_file_sync_selected_chats"] = []

        stopped = service.run(scan_first=False)

        self.assertEqual(stopped["completed"], 0)
        self.assertEqual(self.drive.upload_calls, 0)
        self.assertEqual(self.rows("drive_sync_items")[0]["status"], "queued")

        service.config["google_drive_file_sync_selected_chats"] = [
            {"username": self.chat_a, "alias": "Alpha group"}
        ]
        resumed = service.run(scan_first=False)
        self.assertEqual(resumed["completed"], 1)

    def test_status_inspection_does_not_create_a_disabled_ledger(self):
        missing = os.path.join(self.tmp.name, "never-created.db")
        config = dict(self.config)
        config["google_drive_file_sync_db"] = missing
        config["google_drive_file_sync_enabled"] = False

        status = GoogleDriveFileSync.inspect_status(
            config,
            oauth=FakeOAuth(connected=False),
        )

        self.assertEqual(status["state"], "disabled")
        self.assertEqual(status["auth"], "auth_required")
        self.assertFalse(os.path.exists(missing))

    def test_object_and_shortcut_crash_recovery_adopt_remote_ids(self):
        data = b"crash recovery"
        source = FakeSource({self.chat_a: [file_message("wgmsg_crash", self.timestamp, "crash.txt", data)]})
        self.cache("crash.txt", data)
        crash = [True]

        def crash_after_object(_remote, _item):
            if crash[0]:
                crash[0] = False
                raise RuntimeError("simulated object commit crash")

        first = self.service(source, after_remote_object=crash_after_object)
        self.initialize(first)
        with self.assertRaisesRegex(RuntimeError, "object commit crash"):
            first.run()
        self.assertEqual(self.drive.upload_calls, 1)

        shortcut_crash = [True]

        def crash_after_shortcut(_remote, _item):
            if shortcut_crash[0]:
                shortcut_crash[0] = False
                raise RuntimeError("simulated shortcut commit crash")

        second = self.service(source, after_remote_shortcut=crash_after_shortcut)
        with self.assertRaisesRegex(RuntimeError, "shortcut commit crash"):
            second.run(scan_first=False)
        self.assertEqual(self.drive.upload_calls, 1)
        self.assertEqual(self.drive.shortcut_calls, 1)

        recovered = self.service(source).run(scan_first=False)
        self.assertEqual(recovered["completed"], 1)
        self.assertEqual(self.drive.upload_calls, 1)
        self.assertEqual(self.drive.shortcut_calls, 1)

    def test_checksum_auth_retry_and_root_degraded_states_are_fail_closed(self):
        data = b"verification"
        source = FakeSource({self.chat_a: [file_message("wgmsg_verify", self.timestamp, "verify.bin", data)]})
        self.cache("verify.bin", data)
        service = self.service(source)
        self.initialize(service)
        self.drive.bad_checksum = True

        mismatch = service.run()

        self.assertEqual(mismatch["state"], "remote_degraded")
        self.assertNotEqual(self.rows("drive_objects")[0]["verification_state"], "uploaded_verified")

        self.drive = FakeDrive()
        self.ledger = os.path.join(self.tmp.name, "auth-sync.db")
        self.config["google_drive_file_sync_db"] = self.ledger
        service = self.service(source)
        self.initialize(service)
        service.scan()
        self.drive.fail_mode = "auth"
        auth = service.run(scan_first=False)
        self.assertEqual(auth["state"], "auth_required")
        self.assertEqual(self.rows("drive_sync_items")[0]["status"], "auth_required")

        self.drive.fail_mode = "retry"
        conn = sqlite3.connect(self.ledger)
        conn.execute("UPDATE drive_sync_items SET status='upload_pending'")
        conn.commit()
        conn.close()
        retry = service.run(scan_first=False)
        self.assertEqual(retry["state"], "retry_wait")
        self.assertGreaterEqual(self.rows("drive_sync_items")[0]["next_retry_at"], self.timestamp + 117)

    def test_root_id_survives_rename_move_and_trashed_root_does_not_recreate(self):
        data = b"root identity"
        source = FakeSource({self.chat_a: [file_message("wgmsg_root", self.timestamp, "root.txt", data)]})
        self.cache("root.txt", data)
        service = self.service(source)
        self.initialize(service)
        service.run()
        root_row = next(row for row in self.rows("drive_folders") if row["role"] == "root")
        root_id = root_row["drive_file_id"]
        created_before = self.drive.next_id
        self.drive.items[root_id]["name"] = "User renamed root"
        self.drive.items[root_id]["parents"] = ["user-moved-parent"]

        renamed = service.reconcile()

        self.assertNotEqual(renamed["state"], "remote_degraded")
        self.assertEqual(self.drive.next_id, created_before)
        self.drive.items[root_id]["trashed"] = True
        trashed = service.reconcile()
        self.assertEqual(trashed["state"], "remote_degraded")
        self.assertEqual(self.drive.next_id, created_before)

    def test_remote_metadata_receipts_and_public_contract_exclude_private_source_identity(self):
        data = b"privacy"
        source = FakeSource({self.chat_a: [file_message("wgmsg_private_source", self.timestamp, "公开文件名.txt", data)]})
        self.cache("公开文件名.txt", data)
        service = self.service(source)
        self.initialize(service)
        service.run()

        remote_text = json.dumps(self.drive.items, ensure_ascii=False)
        self.assertNotIn("@chatroom", remote_text)
        self.assertNotIn("wxid", remote_text.lower())
        self.assertNotIn("wgmsg_private_source", remote_text)
        self.assertNotIn(self.file_root, remote_text)
        receipt_text = json.dumps(self.rows("drive_sync_runs"), ensure_ascii=False)
        self.assertNotIn("@chatroom", receipt_text)
        self.assertNotIn("wgmsg", receipt_text)
        roles = {item.get("appProperties", {}).get("wgo_role") for item in self.drive.items.values()}
        self.assertTrue({"root", "chats_root", "system_root", "objects_root", "shard", "chat", "month", "object", "placement"}.issubset(roles))


if __name__ == "__main__":
    unittest.main()
