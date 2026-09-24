import fcntl
import hashlib
import io
import json
import multiprocessing
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from urllib.parse import unquote
from unittest.mock import Mock, patch

import scripts.resource_backup as resource_backup_cli
import core.resource_backup as resource_backup
from core.resource_backup import (
    BACKUP_SCHEMA,
    DESTINATION_MARKER_NAME,
    DESTINATION_MARKER_SCHEMA,
    FILE_SURFACE_STATES,
    LOCAL_INDEX_MANIFEST_SCHEMA,
    MountedResourceBackup,
    TARGET_INDEX_MANIFEST_SCHEMA,
    _markdown_relative_url,
    _redact_url,
    _safe_month,
    classify_file_occurrence,
    evaluate_resource_backup_outcome,
    inspect_mounted_resource_backup,
    load_resource_backup_settings,
    save_resource_backup_settings,
    summarize_file_coverage,
)
from core.resource_capture import (
    SCHEMA_VERSION,
    ResourceCaptureError,
    SelectedResourceCapture,
    _exact_links,
    _url_sha256,
    resource_capture_operation_lock,
    update_resource_backup_selection,
)
from core.wechat_db import WeChatSourceDegraded


def _settings_patch_worker(path, patch_value, start_event):
    start_event.wait(5)
    save_resource_backup_settings(patch_value, path=path)


def _render_projection_worker(
    config,
    archive_id,
    settings_path,
    start_event,
    result_queue,
):
    start_event.wait(10)
    try:
        capture = SelectedResourceCapture(
            config,
            source=None,
            archive_id_factory=lambda: archive_id,
        )
        backup = MountedResourceBackup(
            config,
            capture=capture,
            settings_path=settings_path,
        )
        result_queue.put(backup.render_obsidian_indexes())
    except Exception as exc:  # pragma: no cover - returned to the parent process
        result_queue.put({"state": type(exc).__name__, "error": str(exc)})


def _hold_backup_capture_operation(
    config,
    archive_id,
    settings_path,
    started,
    release,
    result_queue,
):
    capture = SelectedResourceCapture(
        config,
        source=None,
        archive_id_factory=lambda: archive_id,
    )
    backup = MountedResourceBackup(
        config,
        capture=capture,
        settings_path=settings_path,
    )

    def paused_run():
        started.set()
        release.wait(10)
        return {
            "state": "idle",
            "copied": 0,
            "failed": 0,
            "obsidian": {"state": "written"},
        }

    backup._run_owned = paused_run
    result_queue.put(backup.run())


class FakeSource:
    def __init__(self, messages_by_chat=None, *, degraded=False):
        self.messages_by_chat = dict(messages_by_chat or {})
        self.degraded = degraded

    def get_message_shards(self, _username):
        if self.degraded:
            raise WeChatSourceDegraded("source_shard_unavailable")
        return ["shard-1"]

    def get_messages_for_shard(
        self,
        username,
        _source_shard_id,
        since_ts=0,
        limit=500,
        page_forward=False,
        since_inclusive=False,
    ):
        if self.degraded:
            raise WeChatSourceDegraded("source_shard_unavailable")
        comparison = (
            (lambda value: value >= since_ts)
            if since_inclusive
            else (lambda value: value > since_ts)
        )
        rows = [
            dict(row)
            for row in self.messages_by_chat.get(username, [])
            if comparison(int(row.get("timestamp") or 0))
        ]
        rows.sort(key=lambda row: int(row.get("timestamp") or 0))
        if not page_forward:
            rows = rows[-limit:]
        return rows[:limit]


class InventoryAwareSource:
    def __init__(self, messages_by_shard, *, complete, digest, missing=0):
        self.messages_by_shard = dict(messages_by_shard)
        self.complete = bool(complete)
        self.digest = str(digest)
        self.missing = int(missing)
        self.present = list(messages_by_shard)

    def get_source_inventory(self, *, update=True, sensitive=False):
        del update, sensitive
        return {
            "schema": "we-groupchat-obsidian.source-inventory.v1",
            "source_namespace": "opaque-source",
            "inventory_revision": 1,
            "inventory_digest": self.digest,
            "complete": self.complete,
            "counts": {
                "present": len(self.present),
                "missing_file": self.missing,
            },
            "error_codes": [] if self.complete else ["source_missing_file"],
            "present_generation_ids": list(self.present),
            "shards": [],
        }

    def get_messages_for_shard(
        self,
        _username,
        source_shard_id,
        *,
        since_ts=0,
        limit=500,
        page_forward=False,
        since_inclusive=False,
    ):
        rows = [
            dict(row)
            for row in self.messages_by_shard.get(source_shard_id, [])
            if int(row.get("timestamp") or 0) > int(since_ts)
            or (
                since_inclusive
                and int(row.get("timestamp") or 0) == int(since_ts)
            )
        ]
        rows.sort(key=lambda row: (
            int(row.get("timestamp") or 0),
            str(row.get("source_message_id") or ""),
        ))
        return rows[:limit] if page_forward else rows[-limit:]


class KeysetSource:
    """A production-shaped source exposing the opaque keyset page reader."""

    def __init__(self, messages_by_shard):
        self.messages_by_shard = {
            shard: [dict(message) for message in messages]
            for shard, messages in dict(messages_by_shard or {}).items()
        }

    def get_message_shards(self, _username):
        return list(self.messages_by_shard)

    def get_cursor_page_for_shard(
        self,
        _username,
        source_shard_id,
        *,
        cursor_token="",
        since_ts=0,
        limit=500,
    ):
        page_limit = max(1, int(limit))
        positioned = sorted(
            (
                (int(message["timestamp"]), position, message)
                for position, message in enumerate(
                    self.messages_by_shard.get(source_shard_id, []), start=1
                )
            ),
            key=lambda item: (item[0], item[1]),
        )
        after = (
            tuple(json.loads(cursor_token))
            if cursor_token
            else (max(0, int(since_ts)), 0)
        )
        pending = [entry for entry in positioned if (entry[0], entry[1]) > after]
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


class ResourceBackupTests(unittest.TestCase):
    def setUp(self):
        self.settings_patcher = patch(
            "core.resource_backup.load_resource_backup_settings",
            return_value={"target": "", "link_export_mode": "redacted"},
        )
        self.settings_patcher.start()
        self.addCleanup(self.settings_patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.db_dir = os.path.join(
            self.root, "xwechat_files", "account", "db_storage"
        )
        self.file_month = os.path.join(
            os.path.dirname(self.db_dir), "msg", "file", "2026-08"
        )
        os.makedirs(self.file_month, exist_ok=True)
        self.archive_root = os.path.join(self.root, "archive")
        self.capture_db = os.path.join(self.root, "resource_capture.db")
        self.knowledge_db = os.path.join(self.root, "knowledge.db")
        self.obsidian_root = os.path.join(self.root, "obsidian")
        self.target = os.path.join(self.root, "Google Drive", "WeChat Archive")
        os.makedirs(self.target, exist_ok=True)
        self.selected = "selected@chatroom"
        self.unselected = "unselected@chatroom"
        self.ghost = "not-monitored@chatroom"
        self.config = {
            "db_dir": self.db_dir,
            "resource_capture_db": self.capture_db,
            "attachment_archive_root": self.archive_root,
            "attachment_archive_min_free_bytes": 0,
            "monitor_knowledge_db": self.knowledge_db,
            "monitor_obsidian_root": self.obsidian_root,
            "monitor_obsidian_subdir": "微信群聊/关注推送",
            "resource_projection_lock_dir": os.path.join(
                self.root,
                "projection-locks",
            ),
            "resource_backup_target": self.target,
            "monitor_chats": [
                {"username": self.selected, "name": "Selected Chat"},
                {"username": self.unselected, "name": "Unselected Chat"},
            ],
            "monitor_chat_aliases": {
                self.selected: "猫猫研究群",
                self.unselected: "不外发群",
            },
            "resource_backup_selected_chats": [
                {"username": self.selected, "alias": "猫猫研究群"},
                {"username": self.ghost, "alias": "stale selection"},
            ],
            "resource_backup_max_messages_per_scan": 50,
        }

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _digest(data):
        return hashlib.sha256(data).hexdigest()

    def test_read_only_mounted_health_inspector_reports_verified_handoff(self):
        archive_id = "archive-health-fixture"
        destination_id = str(uuid.uuid4())
        snapshot_id = "20260830T120000Z-health"
        marker_dir = Path(self.target) / "wgo-resource-backup"
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / DESTINATION_MARKER_NAME).write_text(json.dumps({
            "schema": DESTINATION_MARKER_SCHEMA,
            "archive_id": archive_id,
            "destination_uuid": destination_id,
        }), encoding="utf-8")

        conn = sqlite3.connect(self.capture_db)
        conn.executescript(
            """
            CREATE TABLE resource_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE resource_deliveries (
                destination_id TEXT NOT NULL,
                object_sha256 TEXT NOT NULL,
                object_size INTEGER NOT NULL,
                target_relpath TEXT NOT NULL,
                status TEXT NOT NULL,
                last_error_code TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                PRIMARY KEY(destination_id, object_sha256)
            );
            CREATE TABLE resource_backup_state (
                destination_id TEXT PRIMARY KEY,
                catalog_sha256 TEXT NOT NULL DEFAULT '',
                snapshot_id TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO resource_meta(key, value) VALUES ('archive_id', ?)",
            (archive_id,),
        )
        conn.execute(
            "INSERT INTO resource_deliveries VALUES (?, ?, ?, ?, ?, '', ?)",
            (destination_id, "a" * 64, 3, "objects/sha256/aa/object", "sync_delegated", 1),
        )
        conn.execute(
            "INSERT INTO resource_backup_state VALUES (?, ?, ?, ?)",
            (destination_id, "catalog", snapshot_id, 1),
        )
        conn.commit()
        conn.close()

        snapshot_dir = (
            marker_dir / "v3" / "snapshots" / snapshot_id
        )
        snapshot_dir.mkdir(parents=True)
        resources_bytes = b""
        resources_sha256 = hashlib.sha256(resources_bytes).hexdigest()
        manifest = {
            "schema": BACKUP_SCHEMA,
            "archive_id": archive_id,
            "snapshot_id": snapshot_id,
            "handoff_semantics": "sync_delegated",
            "source_observation": {"complete": True},
            "resources_sha256": resources_sha256,
        }
        manifest_bytes = (
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
        ).encode("utf-8")
        complete = {
            "schema": BACKUP_SCHEMA,
            "snapshot_id": snapshot_id,
            "state": "complete",
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "resources_sha256": resources_sha256,
        }
        (snapshot_dir / "manifest.json").write_bytes(manifest_bytes)
        (snapshot_dir / "COMPLETE").write_text(
            json.dumps(complete), encoding="utf-8"
        )

        report = inspect_mounted_resource_backup({
            **self.config,
            "resource_backup_enabled": True,
        }, settings_path=os.path.join(self.root, "missing-settings.json"))

        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["handoff_semantics"], "sync_delegated")
        self.assertEqual(report["delivery_counts"], {"sync_delegated": 1})
        self.assertTrue(report["latest_snapshot"])
        self.assertTrue(report["source_complete"])
        self.assertEqual(report["provider_side_sync"], "unknown")
        self.assertFalse(report["remote_verified"])
        self.assertNotIn(self.target, str(report))
        self.assertNotIn(destination_id, str(report))
        self.assertFalse((snapshot_dir / "resources.jsonl").exists())

    def test_read_only_mounted_health_inspector_does_not_create_settings(self):
        settings_path = os.path.join(self.root, "absent", "settings.json")
        report = inspect_mounted_resource_backup(
            {"resource_backup_enabled": False},
            settings_path=settings_path,
        )

        self.assertEqual(report["state"], "disabled")
        self.assertEqual(report["handoff_semantics"], "target_not_configured")
        self.assertEqual(report["provider_side_sync"], "unknown")
        self.assertFalse(report["remote_verified"])
        self.assertFalse(os.path.exists(settings_path))
        self.assertFalse(os.path.exists(settings_path + ".lock"))

    def test_read_only_mounted_health_inspector_reports_marker_read_timeout(self):
        marker_dir = Path(self.target) / "wgo-resource-backup"
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / DESTINATION_MARKER_NAME).write_text(
            "marker bytes are present",
            encoding="utf-8",
        )

        with patch.object(
            MountedResourceBackup,
            "_read_regular_bytes",
            side_effect=TimeoutError(60, "Operation timed out"),
        ):
            report = inspect_mounted_resource_backup(
                {
                    **self.config,
                    "resource_backup_enabled": True,
                },
                settings_path=os.path.join(self.root, "missing-settings.json"),
            )

        self.assertEqual(report["state"], "destination_unavailable")
        self.assertEqual(report["handoff_semantics"], "destination_unavailable")
        self.assertEqual(report["last_error_code"], "destination_unavailable")
        self.assertFalse(report["remote_verified"])

    def _write_candidate_files(self):
        first = b"first file bytes"
        second = b"second file bytes"
        with open(os.path.join(self.file_month, "report.pdf"), "wb") as handle:
            handle.write(first)
        with open(os.path.join(self.file_month, "report (1).pdf"), "wb") as handle:
            handle.write(second)
        return first, second

    def _selected_message(self):
        first, second = self._write_candidate_files()
        first_url = "https://example.com/A?token=secret-value"
        second_url = "https://example.com/a?x=1"
        return {
            "timestamp": 1_787_481_060,
            "time_str": "2026-08-23 10:31",
            "sender": "Faye",
            "text": f"资源 {first_url} {second_url} {first_url}",
            "source_message_id": "wgmsg_selected_resource_fixture",
            "resources": [
                {
                    "kind": "file",
                    "resource_index": 0,
                    "original_name": "report.pdf",
                    "declared_size": len(first),
                    "declared_hash": self._digest(first),
                },
                {
                    "kind": "file",
                    "resource_index": 1,
                    "original_name": "report.pdf",
                    "declared_size": len(second),
                    "declared_hash": self._digest(second),
                },
            ],
        }

    def _unselected_message(self):
        return {
            "timestamp": 1_787_481_061,
            "time_str": "2026-08-23 10:32",
            "sender": "Someone",
            "text": "https://private.example/unselected",
            "source_message_id": "wgmsg_unselected_fixture",
            "resources": [],
        }

    def _capture(self, *, degraded=False):
        source = FakeSource(
            {
                self.selected: [self._selected_message()],
                self.unselected: [self._unselected_message()],
            },
            degraded=degraded,
        )
        return SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 1_787_500_000,
            random_func=lambda: 0.5,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )

    def _ready_capture(self):
        capture = self._capture()
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        scanned = capture.scan()
        self.assertEqual(scanned["state"], "healthy")
        self.assertEqual(scanned["captured_links"], 2)
        self.assertEqual(scanned["captured_files"], 2)
        resolved = capture.resolve_pending_files(limit=10)
        self.assertEqual(resolved["ready_local"], 2)
        return capture

    def test_keyset_source_pages_large_same_second_bucket_across_restarts(self):
        timestamp = 1_787_481_000
        rows = [
            {
                "timestamp": timestamp,
                "time_str": "2026-08-23 10:30",
                "sender": "Someone",
                "text": f"https://example.test/item-{index}",
                "source_message_id": f"wgmsg_same_{index}",
                "resources": [],
            }
            for index in range(1_205)
        ]
        capture = SelectedResourceCapture(
            self.config,
            source=KeysetSource({"fixture-shard": rows}),
            now_func=lambda: 1_787_500_000,
            random_func=lambda: 0.5,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.config["resource_backup_max_messages_per_scan"] = 500
        capture.initialize_selected_chat_cursors(start_timestamp=0)

        captured = []
        for _ in range(6):
            capture = SelectedResourceCapture(
                dict(capture.config),
                source=KeysetSource({"fixture-shard": rows}),
                now_func=lambda: 1_787_500_000,
                random_func=lambda: 0.5,
                archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
            )
            result = capture.scan()
            self.assertEqual(result["state"], "healthy")
            captured.append(result["captured_links"])
            if result["captured_links"] == 0:
                break

        self.assertEqual(captured, [500, 500, 205, 0])
        conn = sqlite3.connect(self.capture_db)
        conn.row_factory = sqlite3.Row
        try:
            shards = [dict(row) for row in conn.execute("SELECT * FROM resource_shards")]
            occurrences = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM resource_occurrences ORDER BY occurrence_id"
                )
            ]
        finally:
            conn.close()
        self.assertEqual(len(shards), 1)
        self.assertEqual(shards[0]["source_cursor_token"], f"[{timestamp},1205]")
        self.assertEqual(shards[0]["cursor_message_ids_json"], "[]")
        self.assertEqual(shards[0]["source_state"], "healthy")
        self.assertEqual(
            sorted(row["source_message_id"] for row in occurrences),
            sorted(f"wgmsg_same_{index}" for index in range(1_205)),
        )
        self.assertEqual(len(occurrences), 1_205)

    def _backup(self, capture, *, mode="redacted"):
        return MountedResourceBackup(
            self.config,
            capture=capture,
            now_func=lambda: 1_787_600_000,
            id_factory=lambda: "fixture",
            link_export_mode=mode,
        )

    def test_file_surface_classifier_maps_capture_and_delivery_states(self):
        rows = []
        capture_states = (
            ("ready_local", "delivered", "a"),
            ("ready_local", "awaiting_handoff", "b"),
            ("queued", "awaiting_resolution", ""),
            ("waiting_cache", "cache_unavailable", ""),
            ("retry_wait", "retry_scheduled", ""),
            ("insufficient_local_space", "local_space_blocked", ""),
            ("ambiguous", "needs_attention", ""),
            ("object_too_large", "needs_attention", ""),
            ("source_rejected", "needs_attention", ""),
            ("future_state", "unknown_state", ""),
            ("ready_local", "unknown_state", ""),
        )
        for index, (capture_state, _expected, digest_prefix) in enumerate(
            capture_states
        ):
            rows.append({
                "kind": "file",
                "status": capture_state,
                "object_sha256": digest_prefix * 64 if digest_prefix else "",
                "object_size": (
                    3 if capture_state == "ready_local" and digest_prefix else None
                ),
                "occurrence_id": f"occurrence-{index}",
            })
        deliveries = {"a" * 64: {"status": "sync_delegated"}}

        actual = [
            classify_file_occurrence(
                row,
                deliveries.get(str(row.get("object_sha256") or "")),
            )
            for row in rows
        ]
        coverage = summarize_file_coverage(rows, deliveries)

        self.assertEqual(actual, [item[1] for item in capture_states])
        self.assertEqual(coverage["delivered_occurrences"], 1)
        self.assertEqual(coverage["delivered_objects"], 1)
        self.assertEqual(coverage["needs_attention"], 3)
        self.assertEqual(coverage["non_delivered_occurrences"], 10)
        self.assertEqual(
            set(FILE_SURFACE_STATES),
            {key for key in coverage if key in FILE_SURFACE_STATES},
        )

    def test_pending_month_uses_truthful_waiting_cache_and_terminal_wording(self):
        backup = self._backup(self._capture())
        rows = [
            {
                "kind": "file",
                "status": "waiting_cache",
                "original_name": "cache.pdf",
                "source_time": "2026-08-23 10:31",
            },
            {
                "kind": "file",
                "status": "source_rejected",
                "original_name": "rejected.pdf",
                "source_time": "2026-08-23 10:32",
            },
        ]

        text = backup._render_pending_file_month(
            "猫猫研究群", "2026-08", rows, {}
        )

        self.assertIn("本地缓存暂未找到（1）", text)
        self.assertIn("已经尝试解析", text)
        self.assertIn("在微信里重新打开或下载", text)
        self.assertIn("需要处理（1）", text)
        self.assertNotIn("等待本地附件解析", text)
        self.assertNotIn("](<", text)

    def test_scope_is_active_monitor_intersection_explicit_selection(self):
        capture = self._capture()
        chats = capture.selected_chats()
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["username"], self.selected)
        self.assertEqual(chats[0]["alias"], "猫猫研究群")

        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        rows = capture.occurrences(selected_only=False)
        self.assertEqual({row["chat_username"] for row in rows}, {self.selected})
        self.assertNotIn("unselected", json.dumps(rows))

    def test_exact_links_and_same_name_distinct_file_bytes_are_first_class_occurrences(self):
        capture = self._ready_capture()
        rows = capture.occurrences()
        self.assertEqual(len(rows), 4)
        links = [row for row in rows if row["kind"] == "link"]
        files = [row for row in rows if row["kind"] == "file"]
        self.assertEqual(
            [row["observed_url"] for row in links],
            [
                "https://example.com/A?token=secret-value",
                "https://example.com/a?x=1",
            ],
        )
        self.assertEqual(
            [row["url_sha256"] for row in links],
            [_url_sha256(row["observed_url"]) for row in links],
        )
        self.assertEqual(len({row["object_sha256"] for row in files}), 2)
        self.assertEqual({row["original_name"] for row in files}, {"report.pdf"})
        self.assertEqual(
            {row["source_message_id"] for row in rows},
            {"wgmsg_selected_resource_fixture"},
        )

    def test_exact_link_identity_preserves_terminal_url_characters(self):
        urls = [
            "https://example.com/report!",
            "https://example.com/path?x=1;",
            "https://example.com/a:b",
        ]

        self.assertEqual(_exact_links(" ".join(urls)), urls)

    def test_mounted_handoff_exports_only_selected_occurrences_and_redacts_tokens(self):
        capture = self._ready_capture()
        # Add an unrelated local CAS object. It exists locally but has no selected
        # occurrence, so it must never be copied by this lane.
        capture.archive.ensure_layout()
        unselected_digest, _size, _relpath = capture.archive.store_bytes(
            b"unselected local object", "unselected.bin"
        )

        backup = self._backup(capture)
        result = backup.run()

        self.assertEqual(result["state"], "sync_delegated")
        self.assertEqual(result["copied"], 2)
        snapshot_id = result["snapshot"]["snapshot_id"]
        snapshot_dir = os.path.join(
            backup.backup_root, "snapshots", snapshot_id
        )
        with open(os.path.join(snapshot_dir, "resources.jsonl"), encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(records), 4)
        serialized = json.dumps(records, ensure_ascii=False)
        self.assertNotIn(self.unselected, serialized)
        self.assertNotIn(self.ghost, serialized)
        self.assertNotIn(unselected_digest, serialized)
        self.assertNotIn("secret-value", serialized)
        self.assertIn("REDACTED", serialized)
        self.assertTrue(all(record["chat_alias"] == "猫猫研究群" for record in records))
        self.assertTrue(all(
            re.fullmatch(r"wgo_resource_[0-9a-f]{32}", record["occurrence_id"])
            for record in records
        ))

        objects_root = os.path.join(backup.backup_root, "objects", "sha256")
        copied_files = [
            os.path.join(dirpath, filename)
            for dirpath, _dirs, filenames in os.walk(objects_root)
            for filename in filenames
        ]
        self.assertEqual(len(copied_files), 2)
        self.assertFalse(any(unselected_digest in path for path in copied_files))

    def test_mounted_target_has_a_human_file_portal_without_duplicate_bytes(self):
        capture = self._ready_capture()
        backup = self._backup(capture)

        result = backup.run()

        self.assertEqual(result["state"], "sync_delegated")
        portal_path = os.path.join(
            self.target,
            "wgo-resource-backup",
            "00-打开微信资源备份.md",
        )
        with open(portal_path, encoding="utf-8") as handle:
            portal = handle.read()
        self.assertIn("# 微信资源备份 / WeChat Resource Backup", portal)
        self.assertIn("v3/views/00-文件备份.md", unquote(portal))
        self.assertIn("v3/views/00-待补齐附件.md", unquote(portal))
        self.assertIn("2 个文件已备份", portal)
        self.assertIn("出现 2 次", portal)
        self.assertIn("0 条附件记录待补齐", portal)
        self.assertIn("系统目录", portal)

        target_views = os.path.join(backup.backup_root, "views")
        file_scope = os.path.join(target_views, "00-文件备份.md")
        with open(file_scope, encoding="utf-8") as handle:
            scope_text = handle.read()
        self.assertIn("# 文件备份", scope_text)
        self.assertIn("猫猫研究群", scope_text)
        self.assertIn("2 个去重文件 · 2 次出现", scope_text)

        chat_root = os.path.join(target_views, "猫猫研究群")
        with open(
            os.path.join(chat_root, "00-文件备份.md"),
            encoding="utf-8",
        ) as handle:
            chat_text = handle.read()
        self.assertIn("文件备份/2026-08.md", unquote(chat_text))
        self.assertIn("2 个去重文件 · 2 次出现", chat_text)

        month_path = os.path.join(chat_root, "文件备份", "2026-08.md")
        with open(month_path, encoding="utf-8") as handle:
            month_text = handle.read()
        self.assertIn("2 个去重文件 · 2 次出现", month_text)
        self.assertIn("[report.pdf]", month_text)
        self.assertIn("../../../objects/sha256/", month_text)
        self.assertNotIn("待补齐", month_text)
        self.assertNotIn("🔗", month_text)

        objects_root = os.path.join(backup.backup_root, "objects", "sha256")
        payloads = [
            os.path.join(root, name)
            for root, _dirs, names in os.walk(objects_root)
            for name in names
        ]
        self.assertEqual(len(payloads), 2)

        with open(
            os.path.join(target_views, ".resource-index-manifest.json"),
            encoding="utf-8",
        ) as handle:
            target_manifest = json.load(handle)
        self.assertEqual(target_manifest["schema"], TARGET_INDEX_MANIFEST_SCHEMA)
        self.assertTrue(any("00-资源索引.md" in path for path in target_manifest["paths"]))
        self.assertTrue(any("00-文件备份.md" in path for path in target_manifest["paths"]))
        self.assertTrue(any("00-待补齐附件.md" in path for path in target_manifest["paths"]))

        local_manifest_path = os.path.join(
            backup.obsidian_projection_root,
            ".resource-index-manifest.json",
        )
        with open(local_manifest_path, encoding="utf-8") as handle:
            local_manifest = json.load(handle)
        self.assertEqual(local_manifest["schema"], LOCAL_INDEX_MANIFEST_SCHEMA)

    def test_file_portal_separates_unresolved_files_from_backed_up_files(self):
        capture = self._capture()
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        scanned = capture.scan()
        self.assertEqual(scanned["captured_files"], 2)
        backup = self._backup(capture)

        result = backup.run()

        self.assertEqual(result["state"], "pending_resources")
        portal_path = os.path.join(
            self.target,
            "wgo-resource-backup",
            "00-打开微信资源备份.md",
        )
        with open(portal_path, encoding="utf-8") as handle:
            portal = handle.read()
        self.assertIn("0 个文件已备份", portal)
        self.assertIn("2 条附件记录待补齐", portal)
        self.assertIn("尚未尝试解析：2 条", portal)

        month_path = os.path.join(
            backup.backup_root,
            "views",
            "猫猫研究群",
            "待补齐附件",
            "2026-08.md",
        )
        with open(month_path, encoding="utf-8") as handle:
            month_text = handle.read()
        self.assertIn("## 尚未尝试解析（2）", month_text)
        self.assertNotIn("../../../objects/sha256/", month_text)
        self.assertNotIn("](<", month_text)
        self.assertFalse(os.path.exists(os.path.join(
            backup.backup_root,
            "views",
            "猫猫研究群",
            "00-文件备份.md",
        )))

    def test_mixed_delivery_writes_disjoint_delivered_and_pending_families(self):
        capture = self._capture()
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        resolved = capture.resolve_pending_files(limit=1)
        self.assertEqual(resolved["ready_local"], 1)
        backup = self._backup(capture)

        result = backup.run()

        self.assertEqual(result["state"], "pending_resources")
        self.assertEqual(result["coverage"]["delivered_occurrences"], 1)
        self.assertEqual(result["coverage"]["awaiting_resolution"], 1)
        chat_root = os.path.join(backup.backup_root, "views", "猫猫研究群")
        delivered_path = os.path.join(chat_root, "文件备份", "2026-08.md")
        pending_path = os.path.join(chat_root, "待补齐附件", "2026-08.md")
        with open(delivered_path, encoding="utf-8") as handle:
            delivered = handle.read()
        with open(pending_path, encoding="utf-8") as handle:
            pending = handle.read()
        self.assertIn("1 个去重文件 · 1 次出现", delivered)
        self.assertNotIn("尚未尝试解析", delivered)
        self.assertIn("尚未尝试解析（1）", pending)
        self.assertNotIn("../../../objects/sha256/", pending)

    def test_link_only_chat_does_not_get_an_empty_file_page(self):
        message = self._selected_message()
        message["resources"] = []
        capture = SelectedResourceCapture(
            self.config,
            source=FakeSource({self.selected: [message]}),
            now_func=lambda: 1_787_500_000,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        scanned = capture.scan()
        self.assertEqual(scanned["captured_links"], 2)
        self.assertEqual(scanned["captured_files"], 0)
        backup = self._backup(capture)

        result = backup.run()

        self.assertEqual(result["state"], "sync_delegated")
        views = os.path.join(backup.backup_root, "views")
        self.assertTrue(os.path.isfile(os.path.join(views, "00-文件备份.md")))
        self.assertFalse(os.path.exists(os.path.join(
            views,
            "猫猫研究群",
            "00-文件备份.md",
        )))

    def test_file_portal_preserves_a_user_collision_with_generated_fallback(self):
        capture = self._ready_capture()
        namespace_root = os.path.join(self.target, "wgo-resource-backup")
        os.makedirs(namespace_root, exist_ok=True)
        preferred = os.path.join(namespace_root, "00-打开微信资源备份.md")
        with open(preferred, "w", encoding="utf-8") as handle:
            handle.write("user-owned\n")
        backup = self._backup(capture)

        result = backup.run()

        self.assertEqual(result["state"], "sync_delegated")
        with open(preferred, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "user-owned\n")
        generated = os.path.join(
            namespace_root,
            "00-打开微信资源备份.generated.md",
        )
        with open(generated, encoding="utf-8") as handle:
            generated_text = handle.read()
        self.assertIn("resource-backup-portal v1", generated_text)
        self.assertIn("# 微信资源备份", generated_text)
        self.assertEqual(backup.existing_target_portal_path(), generated)

    def test_target_manifest_v1_migrates_to_v2_without_gc_between_view_families(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        first = backup.run()
        self.assertEqual(first["state"], "sync_delegated")
        manifest_path = os.path.join(
            backup.backup_root,
            "views",
            ".resource-index-manifest.json",
        )
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest["schema"] = LOCAL_INDEX_MANIFEST_SCHEMA
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)

        second = backup.run()

        self.assertEqual(second["state"], "idle")
        with open(manifest_path, encoding="utf-8") as handle:
            migrated = json.load(handle)
        self.assertEqual(migrated["schema"], TARGET_INDEX_MANIFEST_SCHEMA)
        self.assertTrue(any("00-资源索引.md" in path for path in migrated["paths"]))
        self.assertTrue(any("00-文件备份.md" in path for path in migrated["paths"]))
        self.assertTrue(any("00-待补齐附件.md" in path for path in migrated["paths"]))

    def test_manifestless_legacy_indexes_are_adopted_without_touching_user_files(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        first = backup.run()
        self.assertEqual(first["state"], "sync_delegated")

        local_root = backup.obsidian_projection_root
        target_root = os.path.join(backup.backup_root, "views")
        for root in (local_root, target_root):
            os.unlink(os.path.join(root, ".resource-index-manifest.json"))
            with open(
                os.path.join(root, "human-note.md"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("user-owned\n")

        second = backup.run()

        self.assertEqual(second["state"], "idle")
        for root, schema in (
            (local_root, LOCAL_INDEX_MANIFEST_SCHEMA),
            (target_root, TARGET_INDEX_MANIFEST_SCHEMA),
        ):
            with open(
                os.path.join(root, ".resource-index-manifest.json"),
                encoding="utf-8",
            ) as handle:
                self.assertEqual(json.load(handle)["schema"], schema)
            with open(
                os.path.join(root, "human-note.md"),
                encoding="utf-8",
            ) as handle:
                self.assertEqual(handle.read(), "user-owned\n")

    def test_legacy_adoption_does_not_open_marker_bearing_files_at_unknown_shapes(self):
        backup = self._backup(self._capture())
        root = os.path.join(backup.obsidian_projection_root, "legacy-projection")
        os.makedirs(root)
        known = os.path.join(root, "00-资源索引.md")
        unknown = os.path.join(root, "human-marker.md")
        with open(known, "w", encoding="utf-8") as handle:
            handle.write("<!-- we-groupchat-obsidian:resource-index v1 -->\n")
        with open(unknown, "w", encoding="utf-8") as handle:
            handle.write("<!-- we-groupchat-obsidian:resource-index v1 -->\n")
            handle.write("human-owned\n" * 10000)

        with patch.object(
            backup,
            "_read_legacy_projection_prefix",
            wraps=backup._read_legacy_projection_prefix,
        ) as read_prefix:
            paths = backup._managed_projection_paths(root, target_view=False)

        opened = {call.args[0] for call in read_prefix.call_args_list}
        self.assertEqual(paths, {"00-资源索引.md"})
        self.assertIn(known, opened)
        self.assertNotIn(unknown, opened)

    def test_legacy_adoption_reads_at_most_16_kib_and_rejects_binary(self):
        backup = self._backup(self._capture())
        root = os.path.join(backup.obsidian_projection_root, "legacy-binary")
        os.makedirs(root)
        path = os.path.join(root, "00-文件备份.md")
        with open(path, "wb") as handle:
            handle.write(b"\0<!-- we-groupchat-obsidian:resource-index v1 -->")
            handle.write(b"x" * 100000)
        original_read = os.read

        with patch(
            "core.resource_backup.os.read",
            side_effect=lambda fd, size: original_read(fd, size),
        ) as read_call:
            paths = backup._managed_projection_paths(root, target_view=False)

        self.assertEqual(paths, set())
        self.assertEqual(read_call.call_args.args[1], 16 * 1024)

    def test_legacy_frontmatter_must_bind_app_and_allowed_source_kind(self):
        backup = self._backup(self._capture())
        root = os.path.join(backup.obsidian_projection_root, "legacy-frontmatter")
        os.makedirs(root)
        path = os.path.join(root, "00-待补齐附件.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "---\nsource_app: another-app\n"
                "source_kind: pending_attachment_index\n---\n"
                "<!-- we-groupchat-obsidian:resource-index v1 -->\n"
            )

        self.assertEqual(
            backup._managed_projection_paths(root, target_view=False),
            set(),
        )

    def test_legacy_adoption_rejects_known_shape_symlinks(self):
        backup = self._backup(self._capture())
        root = os.path.join(backup.obsidian_projection_root, "legacy-symlink")
        os.makedirs(root)
        outside = os.path.join(self.root, "outside-index.md")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("<!-- we-groupchat-obsidian:resource-index v1 -->\n")
        os.symlink(outside, os.path.join(root, "00-资源索引.md"))

        self.assertEqual(
            backup._managed_projection_paths(root, target_view=False),
            set(),
        )

    def test_deselecting_all_chats_reconciles_target_views_but_keeps_objects(self):
        capture = self._ready_capture()
        canonical = dict(self.config)
        capture.config_loader = lambda: dict(canonical)
        backup = self._backup(capture)
        first = backup.run()
        self.assertEqual(first["state"], "sync_delegated")
        chat_root = os.path.join(backup.backup_root, "views", "猫猫研究群")
        objects_root = os.path.join(backup.backup_root, "objects", "sha256")
        object_files = [
            os.path.join(root, name)
            for root, _dirs, names in os.walk(objects_root)
            for name in names
        ]
        self.assertEqual(len(object_files), 2)

        canonical["resource_backup_selected_chats"] = []
        second = backup.run()

        self.assertEqual(second["state"], "no_selected_chats")
        self.assertFalse(os.path.exists(chat_root))
        self.assertTrue(all(os.path.isfile(path) for path in object_files))
        with open(backup.existing_target_portal_path(), encoding="utf-8") as handle:
            portal = handle.read()
        self.assertIn("0 个文件已备份", portal)
        self.assertIn("0 条附件记录待补齐", portal)

    def test_file_view_escapes_labels_encodes_hrefs_and_rejects_bad_months(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        item = {
            "kind": "file",
            "status": "ready_local",
            "object_sha256": "a" * 64,
            "object_size": 3,
            "original_name": "<img src=x>#%.pdf",
            "source_time": "2026-08-23 10:31",
        }
        delivery = {
            "status": "sync_delegated",
            "object_size": 3,
            "target_relpath": "objects/sha256/aa/file #%.pdf",
        }

        text = backup._render_file_month(
            "<b>猫猫</b>",
            "2026-08",
            [item],
            {"a" * 64: delivery},
        )

        self.assertIn("&lt;b&gt;猫猫&lt;/b&gt;", text)
        self.assertIn("&lt;img src=x&gt;#%.pdf", text)
        self.assertIn("file%20%23%25.pdf", text)
        self.assertEqual(
            _markdown_relative_url("../objects/a # % ?.pdf"),
            "../objects/a%20%23%20%25%20%3F.pdf",
        )
        self.assertEqual(_safe_month("../../outside"), "未标月份")
        reserved = backup._chat_path_parts([{
            "chat_key": "reserved-chat-key",
            "chat_alias": "00-文件备份.md",
        }])
        self.assertEqual(
            reserved[("reserved-chat-key", "00-文件备份.md")],
            "00-文件备份.md--reserved",
        )

    def test_delivered_month_deduplicates_object_rows_and_preserves_occurrence_count(self):
        backup = self._backup(self._capture())
        digest = "d" * 64
        rows = [
            {
                "kind": "file",
                "status": "ready_local",
                "object_sha256": digest,
                "original_name": "report.pdf",
            },
            {
                "kind": "file",
                "status": "ready_local",
                "object_sha256": digest,
                "original_name": "报告.pdf",
            },
        ]
        delivery = {
            "status": "sync_delegated",
            "object_sha256": digest,
            "object_size": 3,
            "target_relpath": "objects/sha256/dd/report.pdf",
        }

        text = backup._render_file_month(
            "猫猫研究群", "2026-08", rows, {digest: delivery}
        )

        self.assertEqual(text.count("../../../objects/sha256/dd/report.pdf"), 1)
        self.assertIn("1 个去重文件 · 2 次出现", text)
        self.assertIn("出现 2 次", text)
        self.assertIn("其他名称", text)

    def test_resource_index_is_a_light_resource_list_without_sender_or_ledger_details(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        backup.run()

        month_note = os.path.join(
            self.obsidian_root,
            "微信群聊",
            "关注推送",
            "猫猫研究群",
            "资源索引",
            "2026-08.md",
        )
        # Find the generated month rather than binding the test to one timezone.
        index_dir = os.path.dirname(month_note)
        month_files = [
            os.path.join(index_dir, name)
            for name in os.listdir(index_dir)
            if name.endswith(".md")
        ]
        self.assertEqual(len(month_files), 1)
        with open(month_files[0], encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn(
            "[https://example.com/A?token=REDACTED]"
            "(<https://example.com/A?token=REDACTED>)",
            text,
        )
        self.assertNotIn("secret-value", text)
        self.assertIn("📎 [report.pdf]", text)
        self.assertNotIn("Faye", text)
        self.assertNotIn("URL identity", text)
        self.assertNotIn("SHA-256", text)
        self.assertNotIn("Drive handoff", text)
        self.assertNotIn("同条消息共同出现", text)
        self.assertNotIn("source_message_id", text)
        vault_files = [
            os.path.join(dirpath, filename)
            for dirpath, _dirs, filenames in os.walk(self.obsidian_root)
            for filename in filenames
        ]
        self.assertTrue(all(
            path.endswith(".md")
            or os.path.basename(path) == ".resource-index-manifest.json"
            for path in vault_files
        ))
        lock_files = list(Path(self.config["resource_projection_lock_dir"]).glob("*.lock"))
        self.assertEqual(len(lock_files), 1)
        self.assertEqual(lock_files[0].read_bytes(), b"")
        self.assertEqual(lock_files[0].stat().st_mode & 0o777, 0o600)

    def test_resource_index_prefers_observed_title_and_uses_exact_url_as_fallback(self):
        capture = self._ready_capture()
        rows = capture.occurrences()
        links = [row for row in rows if row["kind"] == "link"]
        links[0]["original_name"] = "Observed title"
        links[1]["original_name"] = ""
        backup = self._backup(capture)

        text = backup._render_month(
            "猫猫研究群",
            "2026-08",
            links,
            {},
            target_view=False,
        )

        self.assertIn(
            "[Observed title](<https://example.com/A?token=REDACTED>)",
            text,
        )
        self.assertIn(
            "[https://example.com/a?x=1](<https://example.com/a?x=1>)",
            text,
        )
        self.assertNotIn("[链接]", text)

    def test_signed_cloud_urls_are_redacted_in_catalog_and_markdown(self):
        capture = self._ready_capture()
        links = [
            row for row in capture.occurrences() if row["kind"] == "link"
        ]
        links[0]["observed_url"] = (
            "https://bucket.example.com/object?X-Amz-Credential=export-secret"
            "&X-Amz-Signature=signature-secret&view=1"
            "#access_token=fragment-secret"
        )
        links[1]["observed_url"] = (
            "https://account.blob.example.com/object?sv=2026-01-01&sp=r"
            "&se=2026-09-01&sr=b&sig=azure-secret&filename=report.pdf"
        )
        backup = self._backup(capture)

        catalog = json.dumps(
            backup._catalog_records(links, {}),
            ensure_ascii=False,
        )
        markdown = backup._render_month(
            "猫猫研究群",
            "2026-08",
            links,
            {},
            target_view=False,
        )
        combined = catalog + markdown

        for secret in (
            "export-secret",
            "signature-secret",
            "fragment-secret",
            "azure-secret",
            "2026-01-01",
            "2026-09-01",
        ):
            self.assertNotIn(secret, combined)
        self.assertIn("X-Amz-Credential=REDACTED", combined)
        self.assertIn("access_token=REDACTED", combined)
        self.assertIn("filename=report.pdf", combined)

    def test_legacy_full_link_export_mode_migrates_to_redacted(self):
        capture = self._ready_capture()
        backup = MountedResourceBackup(
            self.config,
            capture=capture,
            link_export_mode="full",
            now_func=lambda: 300,
        )

        self.assertEqual(backup.link_export_mode, "redacted")
        self.assertNotIn(
            "legacy-secret",
            backup._export_url(
                "https://example.com/object?token=legacy-secret&view=1"
            ),
        )

    def test_resource_indexes_have_a_discoverable_scope_root(self):
        capture = self._ready_capture()
        backup = self._backup(capture)

        result = backup.render_obsidian_indexes()

        self.assertEqual(result["state"], "written")
        scope_root = os.path.join(
            self.obsidian_root,
            "微信群聊",
            "关注推送",
            "00-资源索引.md",
        )
        with open(scope_root, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("<!-- we-groupchat-obsidian:resource-index v1 -->", text)
        self.assertIn("[[猫猫研究群/00-资源索引|猫猫研究群]]", text)
        self.assertIn("2 个链接 · 2 个文件", text)

    def test_ordinary_receipt_backed_rerun_does_not_read_target_or_source_bytes(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        first = backup.run()
        self.assertEqual(first["copied"], 2)

        with patch(
            "core.resource_backup._hash_path",
            side_effect=AssertionError("receipt reuse hashed target bytes"),
        ) as hash_path, patch.object(
            backup,
            "_source_path",
            side_effect=AssertionError("receipt reuse opened the source CAS"),
        ) as source_path:
            second = backup.run()
        self.assertEqual(second["state"], "idle")
        self.assertEqual(second["copied"], 0)
        self.assertEqual(second["reused"], 2)
        hash_path.assert_not_called()
        source_path.assert_not_called()

    def test_idle_rerun_preserves_projection_files_without_atomic_writes(self):
        backup = self._backup(self._ready_capture())
        first = backup.run()
        self.assertEqual(first["state"], "sync_delegated")
        self.assertGreater(
            first["obsidian"]["projection_files_written"],
            0,
        )
        self.assertGreater(
            first["obsidian"]["projection_bytes_written"],
            0,
        )
        manifests = [
            Path(backup.obsidian_projection_root) / ".resource-index-manifest.json",
            Path(backup.backup_root) / "views" / ".resource-index-manifest.json",
        ]
        before = [(p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in manifests]

        with patch(
            "core.resource_backup._atomic_bytes",
            wraps=resource_backup._atomic_bytes,
        ) as publish:
            result = backup.run()

        self.assertEqual(result["state"], "idle")
        self.assertEqual(result["snapshot"]["state"], "unchanged")
        self.assertEqual(result["target_index_files_written"], 0)
        self.assertEqual(result["obsidian"]["projection_files_written"], 0)
        self.assertEqual(result["obsidian"]["projection_bytes_written"], 0)
        publish.assert_not_called()
        self.assertEqual(
            [(p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in manifests],
            before,
        )

    def test_idle_rerun_repairs_missing_indexes_with_unchanged_manifests(self):
        backup = self._backup(self._ready_capture())
        self.assertEqual(backup.run()["state"], "sync_delegated")
        roots = [
            Path(backup.obsidian_projection_root),
            Path(backup.backup_root) / "views",
        ]
        manifests = [root / ".resource-index-manifest.json" for root in roots]
        before = [p.read_bytes() for p in manifests]
        pages = [root / "00-资源索引.md" for root in roots]
        expected = [p.read_bytes() for p in pages]
        for page in pages:
            page.unlink()

        result = backup.run()

        self.assertEqual(result["state"], "idle")
        self.assertEqual([p.read_bytes() for p in pages], expected)
        self.assertEqual([p.read_bytes() for p in manifests], before)
        self.assertGreater(result["obsidian"]["files_written"], 0)
        self.assertGreater(result["target_index_files_written"], 0)

    def test_receipt_backed_plan_and_status_do_not_hash_target_bytes(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        first = backup.run()
        self.assertEqual(first["copied"], 2)

        with patch(
            "core.resource_backup._hash_path",
            side_effect=AssertionError("metadata status hashed target bytes"),
        ) as hash_path:
            plan = backup.plan()
            status = backup.status()

        self.assertEqual(plan["state"], "ready")
        self.assertEqual(status["handoff_semantics"], "sync_delegated")
        hash_path.assert_not_called()

    def test_explicit_verify_rehashes_and_detects_target_corruption(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        completed = backup.run()
        snapshot_id = completed["snapshot"]["snapshot_id"]
        delivery = next(iter(backup._delivery_map().values()))
        object_path = os.path.join(backup.backup_root, delivery["target_relpath"])
        with open(object_path, "wb") as handle:
            handle.write(b"corrupt")

        verified = backup.verify(snapshot_id)
        self.assertEqual(verified["state"], "target_failed")
        self.assertEqual(verified["failed"], 1)
        self.assertFalse(verified["remote_verified"])

    def test_degraded_shard_does_not_advance_cursor(self):
        capture = self._capture(degraded=True)
        capture.initialize_selected_chat_cursors(start_timestamp=123)
        result = capture.scan()
        self.assertEqual(result["state"], "source_degraded")
        conn = sqlite3.connect(self.capture_db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM resource_occurrences").fetchone()[0]
            shard_count = conn.execute("SELECT COUNT(*) FROM resource_shards").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)
        self.assertEqual(shard_count, 0)

    def test_generation_change_page_never_advances_resource_cursor(self):
        class EmptyShardSource:
            @staticmethod
            def get_message_shards(_username):
                return ["generation-a"]

            @staticmethod
            def get_cursor_page_for_shard(
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

        class GenerationChangedSource(EmptyShardSource):
            @staticmethod
            def get_cursor_page_for_shard(
                _username,
                _source_shard_id,
                *,
                cursor_token="",
                since_ts=0,
                limit=500,
            ):
                del cursor_token, since_ts, limit
                raise WeChatSourceDegraded("source_generation_changed")

        capture = SelectedResourceCapture(
            self.config,
            source=EmptyShardSource(),
            now_func=lambda: 1_787_500_000,
            random_func=lambda: 0.5,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=123)
        capture.scan()
        conn = sqlite3.connect(self.capture_db)
        conn.row_factory = sqlite3.Row
        try:
            before = dict(conn.execute(
                "SELECT * FROM resource_shards WHERE chat_username = ?",
                (self.selected,),
            ).fetchone())
        finally:
            conn.close()

        capture = SelectedResourceCapture(
            self.config,
            source=GenerationChangedSource(),
            now_func=lambda: 1_787_500_000,
            random_func=lambda: 0.5,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        result = capture.scan()

        conn = sqlite3.connect(self.capture_db)
        conn.row_factory = sqlite3.Row
        try:
            after = dict(conn.execute(
                "SELECT * FROM resource_shards WHERE chat_username = ?",
                (self.selected,),
            ).fetchone())
            occurrences = conn.execute(
                "SELECT COUNT(*) FROM resource_occurrences"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(result["state"], "source_degraded")
        self.assertEqual(result["error_code"], "source_generation_changed")
        self.assertEqual(after["cursor_timestamp"], before["cursor_timestamp"])
        self.assertEqual(
            after["source_cursor_token"], before["source_cursor_token"]
        )
        self.assertEqual(after["source_state"], "source_degraded")
        self.assertEqual(after["last_error_code"], "source_generation_changed")
        self.assertEqual(occurrences, 0)

    def test_failed_shard_recovers_after_healthy_shard_advances_without_losing_file(self):
        class TwoShardSource:
            fail_a = True

            @staticmethod
            def get_message_shards(_username):
                return ["shard-a", "shard-b"]

            def get_messages_for_shard(self, _username, source_shard_id, **_kwargs):
                if source_shard_id == "shard-a":
                    if self.fail_a:
                        raise WeChatSourceDegraded("source_shard_unavailable")
                    return [{
                        "timestamp": 100,
                        "source_message_id": "message-a-file",
                        "text": "",
                        "resources": [{
                            "kind": "file",
                            "resource_index": 0,
                            "original_name": "a.pdf",
                        }],
                    }]
                return [{
                    "timestamp": 200,
                    "source_message_id": "message-b-link",
                    "text": "https://example.com/b",
                    "resources": [],
                }]

        source = TwoShardSource()
        capture = SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 1_000,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)

        first = capture.scan()
        source.fail_a = False
        second = capture.scan()
        third = capture.scan()

        self.assertEqual(first["state"], "source_degraded")
        self.assertEqual(second["state"], "healthy")
        self.assertEqual(third["state"], "healthy")
        rows = capture.occurrences()
        self.assertEqual(
            [(row["source_message_id"], row["kind"]) for row in rows],
            [("message-a-file", "file"), ("message-b-link", "link")],
        )
        conn = sqlite3.connect(self.capture_db)
        try:
            cursors = dict(conn.execute(
                "SELECT source_shard_id, cursor_timestamp FROM resource_shards"
            ))
        finally:
            conn.close()
        self.assertEqual(cursors, {"shard-a": 100, "shard-b": 200})

    def test_incomplete_inventory_processes_present_shard_then_recovers_missing_once(self):
        source = InventoryAwareSource(
            {
                "generation-a": [{
                    "timestamp": 100,
                    "source_message_id": "message-a",
                    "text": "https://example.com/a",
                    "resources": [],
                }],
                "generation-b": [{
                    "timestamp": 200,
                    "source_message_id": "message-b",
                    "text": "https://example.com/b",
                    "resources": [],
                }],
            },
            complete=False,
            digest="inventory-missing-b",
            missing=1,
        )
        source.present = ["generation-a"]
        capture = SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 1_000,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)

        first = capture.scan()
        source.complete = True
        source.digest = "inventory-recovered"
        source.missing = 0
        source.present = ["generation-a", "generation-b"]
        second = capture.scan()
        third = capture.scan()

        self.assertEqual(first["state"], "source_degraded")
        self.assertFalse(first["source_complete"])
        self.assertEqual(first["captured_links"], 1)
        self.assertEqual(second["state"], "healthy")
        self.assertTrue(second["source_complete"])
        self.assertEqual(second["captured_links"], 1)
        self.assertEqual(third["captured_links"], 0)
        self.assertEqual(
            [row["source_message_id"] for row in capture.occurrences()],
            ["message-a", "message-b"],
        )

    def test_reselect_starts_new_selection_epoch_without_gap_backfill(self):
        config = dict(self.config)
        config["resource_backup_selected_chats"] = [{
            "username": self.selected,
            "alias": "猫猫研究群",
            "selected_since": 10,
        }]
        source = FakeSource({
            self.selected: [{
                "timestamp": 100,
                "source_message_id": "before-deselect",
                "text": "https://example.com/before",
                "resources": [],
            }],
        })
        capture = SelectedResourceCapture(
            config,
            source=source,
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()

        source.messages_by_chat[self.selected].extend([
            {
                "timestamp": 150,
                "source_message_id": "selection-gap",
                "text": "https://example.com/gap",
                "resources": [],
            },
            {
                "timestamp": 250,
                "source_message_id": "after-reselect",
                "text": "https://example.com/after",
                "resources": [],
            },
        ])
        capture.config["resource_backup_selected_chats"] = [{
            "username": self.selected,
            "alias": "猫猫研究群",
            "selected_since": 200,
        }]

        capture.scan()

        self.assertEqual(
            [row["source_message_id"] for row in capture.occurrences()],
            ["before-deselect", "after-reselect"],
        )
        conn = sqlite3.connect(self.capture_db)
        try:
            chat = conn.execute(
                "SELECT selected_since, selection_epoch FROM resource_chats"
            ).fetchone()
            cursor = conn.execute(
                "SELECT cursor_timestamp FROM resource_shards"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(chat, (200, 2))
        self.assertEqual(cursor, 250)

    def test_selection_uuid_changes_epoch_with_same_selected_timestamp(self):
        config = dict(self.config)
        config["resource_backup_selected_chats"] = [{
            "username": self.selected,
            "alias": "猫猫研究群",
            "selected_since": 100,
            "selection_id": "00000000-0000-0000-0000-000000000001",
        }]
        first = SelectedResourceCapture(
            config,
            source=FakeSource({self.selected: []}),
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000099",
        )
        first.initialize_selected_chat_cursors()

        config["resource_backup_selected_chats"][0]["selection_id"] = (
            "00000000-0000-0000-0000-000000000002"
        )
        second = SelectedResourceCapture(
            config,
            source=FakeSource({self.selected: []}),
            now_func=lambda: 200,
        )
        result = second.initialize_selected_chat_cursors()

        self.assertEqual(result["reselected_chats"], 1)
        conn = sqlite3.connect(self.capture_db)
        try:
            row = conn.execute(
                "SELECT selection_id, selection_epoch FROM resource_chats"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "00000000-0000-0000-0000-000000000002")
        self.assertEqual(row[1], 2)

    def test_legacy_empty_selection_id_is_adopted_without_resetting_cursors(self):
        config = dict(self.config)
        config["resource_backup_selected_chats"] = [{
            "username": self.selected,
            "alias": "猫猫研究群",
            "selected_since": 100,
        }]
        first = SelectedResourceCapture(
            config,
            source=FakeSource({self.selected: []}),
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000099",
        )
        first.initialize_selected_chat_cursors()
        first.scan()
        conn = sqlite3.connect(self.capture_db)
        try:
            before_shards = conn.execute(
                "SELECT source_shard_id, cursor_timestamp "
                "FROM resource_shards ORDER BY source_shard_id"
            ).fetchall()
        finally:
            conn.close()
        self.assertTrue(before_shards)

        config["resource_backup_selected_chats"][0]["selection_id"] = (
            "00000000-0000-0000-0000-000000000123"
        )
        upgraded = SelectedResourceCapture(
            config,
            source=FakeSource({self.selected: []}),
            now_func=lambda: 300,
        )
        result = upgraded.initialize_selected_chat_cursors()

        self.assertEqual(result["reselected_chats"], 0)
        conn = sqlite3.connect(self.capture_db)
        try:
            row = conn.execute(
                "SELECT selection_id, selection_epoch FROM resource_chats"
            ).fetchone()
            after_shards = conn.execute(
                "SELECT source_shard_id, cursor_timestamp "
                "FROM resource_shards ORDER BY source_shard_id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(row, (
            "00000000-0000-0000-0000-000000000123",
            1,
        ))
        self.assertEqual(after_shards, before_shards)

    def test_explicit_backfill_applies_history_without_moving_live_cursor(self):
        source = FakeSource({
            self.selected: [{
                "timestamp": 100,
                "source_message_id": "historical-resource",
                "text": "https://example.com/history",
                "resources": [{
                    "kind": "file",
                    "resource_index": 0,
                    "original_name": "history.pdf",
                }],
            }],
        })
        capture = SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=200)
        capture.scan()

        planned = capture.backfill(0, apply=False)
        applied = capture.backfill(
            0, apply=True, run_id=planned["run_id"]
        )

        self.assertEqual(planned["state"], "planned")
        self.assertEqual(planned["discovered_links"], 1)
        self.assertEqual(planned["discovered_files"], 1)
        self.assertEqual(
            capture.backfill(
                0, apply=True, run_id=planned["run_id"]
            )["inserted_links"],
            1,
        )
        self.assertEqual(applied["inserted_links"], 1)
        self.assertEqual(applied["inserted_files"], 1)
        conn = sqlite3.connect(self.capture_db)
        try:
            cursor = conn.execute(
                "SELECT cursor_timestamp FROM resource_shards"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(cursor, 200)

    def test_backfill_uses_cursor_complete_pages_across_filtered_rows(self):
        messages = []
        for timestamp in range(1, 1201):
            text = ""
            if timestamp in {10, 600, 1100}:
                text = f"https://example.com/history/{timestamp}"
            messages.append({
                "timestamp": timestamp,
                "source_message_id": f"source-{timestamp}",
                "text": text,
                "resources": [],
            })

        class CursorCompleteSource(FakeSource):
            def get_messages_for_shard(self, *args, **kwargs):
                return [
                    row
                    for row in super().get_messages_for_shard(*args, **kwargs)
                    if row.get("text")
                ]

            def get_cursor_messages_for_shard(self, *args, **kwargs):
                return super().get_messages_for_shard(*args, **kwargs)

        capture = SelectedResourceCapture(
            self.config,
            source=CursorCompleteSource({self.selected: messages}),
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )

        planned = capture.backfill(0, apply=False)

        self.assertEqual(planned["state"], "planned")
        self.assertEqual(planned["scanned"], 1200)
        self.assertEqual(planned["discovered_links"], 3)

    def test_backfill_plan_does_not_mutate_live_chat_or_cursor_state(self):
        capture = self._capture()

        planned = capture.backfill_links(0, apply=False)

        self.assertEqual(planned["state"], "planned")
        conn = sqlite3.connect(self.capture_db)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM resource_chats").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM resource_shards").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM resource_occurrences").fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_links_only_backfill_never_queues_attachment_files(self):
        source = FakeSource({
            self.selected: [{
                "timestamp": 100,
                "source_message_id": "historical-resource",
                "text": "https://example.com/history",
                "resources": [{
                    "kind": "file",
                    "resource_index": 0,
                    "original_name": "history.pdf",
                }],
            }],
        })
        capture = SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )

        planned = capture.backfill_links(0, apply=False)
        result = capture.backfill_links(
            0, apply=True, run_id=planned["run_id"]
        )

        self.assertEqual(result["state"], "applied")
        self.assertEqual(result["mode"], "links_only")
        self.assertTrue(result["source_complete"])
        self.assertEqual(result["discovered_links"], 1)
        self.assertEqual(result["discovered_files"], 0)
        self.assertEqual(result["inserted_links"], 1)
        self.assertEqual(result["inserted_files"], 0)
        self.assertEqual(
            [(row["kind"], row["observed_url"]) for row in capture.occurrences()],
            [("link", "https://example.com/history")],
        )

    def test_links_only_backfill_writes_nothing_when_a_later_shard_degrades(self):
        class PartialSource(FakeSource):
            def get_message_shards(self, _username):
                return ["healthy", "failed"]

            def get_messages_for_shard(
                self,
                username,
                source_shard_id,
                since_ts=0,
                limit=500,
                page_forward=False,
                since_inclusive=False,
            ):
                if source_shard_id == "failed":
                    raise WeChatSourceDegraded("source_shard_unavailable")
                return super().get_messages_for_shard(
                    username,
                    source_shard_id,
                    since_ts=since_ts,
                    limit=limit,
                    page_forward=page_forward,
                    since_inclusive=since_inclusive,
                )

        capture = SelectedResourceCapture(
            self.config,
            source=PartialSource({
                self.selected: [{
                    "timestamp": 100,
                    "source_message_id": "visible-link",
                    "text": "https://example.com/must-not-partially-commit",
                    "resources": [],
                }],
            }),
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )

        result = capture.backfill_links(0, apply=False)

        self.assertEqual(result["state"], "source_degraded")
        self.assertFalse(result["source_complete"])
        self.assertEqual(result["discovered_links"], 1)
        self.assertEqual(result["inserted_links"], 0)
        self.assertEqual(capture.occurrences(), [])

    def test_backfill_apply_rechecks_inventory_without_rescanning_messages(self):
        source = FakeSource({
            self.selected: [{
                "timestamp": 100,
                "source_message_id": "planned-link",
                "text": "https://example.com/planned",
                "resources": [],
            }],
        })
        capture = SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        planned = capture.backfill_links(0)
        source.messages_by_chat[self.selected].append({
            "timestamp": 101,
            "source_message_id": "later-link",
            "text": "https://example.com/later",
            "resources": [],
        })
        applied = capture.backfill_links(
            0, apply=True, run_id=planned["run_id"]
        )

        self.assertEqual(applied["state"], "applied")
        self.assertEqual(
            [row["source_message_id"] for row in capture.occurrences()],
            ["planned-link"],
        )

    def test_backfill_apply_fails_closed_after_selection_change(self):
        capture = self._capture()
        planned = capture.backfill_links(0)
        capture.config["resource_backup_selected_chats"] = []

        applied = capture.backfill_links(
            0, apply=True, run_id=planned["run_id"]
        )

        self.assertEqual(applied["state"], "selection_changed")
        self.assertFalse(applied["source_complete"])
        self.assertEqual(capture.occurrences(selected_only=False), [])

    def test_backfill_apply_fails_closed_when_inventory_digest_changes(self):
        source = InventoryAwareSource(
            {
                "generation-a": [{
                    "timestamp": 100,
                    "source_message_id": "planned-link",
                    "text": "https://example.com/planned",
                    "resources": [],
                }],
            },
            complete=True,
            digest="inventory-before",
        )
        capture = SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        planned = capture.backfill_links(0)
        source.digest = "inventory-after"

        applied = capture.backfill_links(
            0,
            apply=True,
            run_id=planned["run_id"],
        )

        self.assertEqual(planned["inventory_digest"], "inventory-before")
        self.assertEqual(applied["state"], "inventory_changed")
        self.assertEqual(applied["error_code"], "inventory_digest_mismatch")
        self.assertFalse(applied["source_complete"])
        self.assertEqual(capture.occurrences(selected_only=False), [])

    def test_backfill_apply_reloads_canonical_selection_after_service_construction(self):
        canonical = dict(self.config)
        source = FakeSource({self.selected: [self._selected_message()]})
        with patch(
            "core.resource_capture.load_config",
            side_effect=lambda: dict(canonical),
        ):
            planner = SelectedResourceCapture.from_config(
                self.config,
                source=source,
                now_func=lambda: 1_787_500_000,
                archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
            )
            planned = planner.backfill_links(0)
            stale_applier = SelectedResourceCapture.from_config(
                self.config,
                source=None,
                now_func=lambda: 1_787_500_000,
                archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
            )
            canonical["resource_backup_selected_chats"] = []
            applied = stale_applier.backfill_links(
                0,
                apply=True,
                run_id=planned["run_id"],
            )

        self.assertEqual(applied["state"], "selection_changed")
        conn = sqlite3.connect(self.capture_db)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM resource_occurrences").fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_backfill_candidate_digest_tamper_fails_closed(self):
        capture = self._capture()
        planned = capture.backfill_links(0)
        conn = sqlite3.connect(self.capture_db)
        try:
            conn.execute(
                """
                UPDATE resource_backfill_staged_occurrences
                SET observed_url = 'https://example.com/tampered'
                WHERE run_id = ?
                """,
                (planned["run_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        applied = capture.backfill_links(
            0, apply=True, run_id=planned["run_id"]
        )

        self.assertEqual(applied["state"], "candidate_mismatch")
        self.assertEqual(capture.occurrences(selected_only=False), [])

    def test_expired_incomplete_backfill_run_is_cleaned_with_staging(self):
        capture = self._capture()
        planned = capture.backfill_links(0)
        conn = sqlite3.connect(self.capture_db)
        try:
            conn.execute(
                "UPDATE resource_backfill_runs SET expires_at = 0 WHERE run_id = ?",
                (planned["run_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(capture.cleanup_backfill_runs(), 1)
        conn = sqlite3.connect(self.capture_db)
        try:
            staged = conn.execute(
                "SELECT COUNT(*) FROM resource_backfill_staged_occurrences"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(staged, 0)

    def test_archive_id_first_writer_is_compare_and_set(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []
        candidates = iter([
            "00000000-0000-0000-0000-000000000011",
            "00000000-0000-0000-0000-000000000022",
        ])
        candidates_lock = threading.Lock()

        def factory():
            with candidates_lock:
                return next(candidates)

        def construct():
            try:
                barrier.wait(5)
                results.append(SelectedResourceCapture(
                    self.config,
                    source=None,
                    archive_id_factory=factory,
                ).archive_id)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=construct) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        self.assertEqual(len(results), 1)
        self.assertEqual(
            [str(getattr(exc, "code", "")) for exc in errors],
            ["capture_worker_busy"],
        )
        retry = SelectedResourceCapture(
            self.config,
            source=None,
            archive_id_factory=factory,
        ).archive_id
        self.assertEqual(retry, results[0])

    def test_capture_constructor_does_not_write_before_operation_lock(self):
        config = dict(self.config)
        config["resource_capture_db"] = os.path.join(
            self.root,
            "constructor-isolation.db",
        )
        with resource_capture_operation_lock(config):
            capture = SelectedResourceCapture(
                config,
                source=None,
                archive_id_factory=(
                    lambda: "00000000-0000-0000-0000-000000000099"
                ),
            )
            self.assertFalse(os.path.exists(config["resource_capture_db"]))
            self.assertEqual(capture.status()["state"], "worker_busy")
            self.assertFalse(os.path.exists(config["resource_capture_db"]))

    def test_capture_reentrancy_is_owned_by_one_thread(self):
        capture = self._capture()
        observed = []

        with capture.canonical_operation():
            thread = threading.Thread(
                target=lambda: observed.append(capture.status()["state"])
            )
            thread.start()
            thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(observed, ["worker_busy"])

    def test_backup_holds_capture_authority_against_selection_mutation(self):
        capture = self._ready_capture()
        context = multiprocessing.get_context("spawn")
        started = context.Event()
        release = context.Event()
        results = context.Queue()
        process = context.Process(
            target=_hold_backup_capture_operation,
            args=(
                self.config,
                capture.archive_id,
                os.path.join(self.root, "spawned-backup-settings.json"),
                started,
                release,
                results,
            ),
        )
        try:
            process.start()
            self.assertTrue(started.wait(10))
            with self.assertRaisesRegex(
                ResourceCaptureError,
                "capture_worker_busy",
            ):
                with resource_capture_operation_lock(self.config):
                    pass
            release.set()
            process.join(10)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(results.get(timeout=2)["state"], "idle")
        finally:
            release.set()
            if process.pid is not None:
                process.join(5)

    def test_selection_writer_ignores_stale_caller_lock_identity(self):
        stale = dict(self.config)
        stale["resource_capture_db"] = os.path.join(self.root, "stale.db")
        canonical = dict(self.config)
        canonical["resource_capture_db"] = os.path.join(
            self.root,
            "canonical.db",
        )
        with (
            resource_capture_operation_lock(canonical),
            patch(
                "core.resource_capture.load_config",
                return_value=canonical,
            ),
            patch("core.resource_capture.update_config") as update,
        ):
            with self.assertRaisesRegex(
                ResourceCaptureError,
                "capture_worker_busy",
            ):
                update_resource_backup_selection(stale, [])

        update.assert_not_called()
        self.assertFalse(os.path.exists(stale["resource_capture_db"]))
        self.assertFalse(os.path.exists(canonical["resource_capture_db"]))

    def test_schema_too_new_is_rejected_without_relabeling(self):
        conn = sqlite3.connect(self.capture_db)
        try:
            conn.execute("CREATE TABLE future_owner(value TEXT)")
            conn.execute("INSERT INTO future_owner VALUES ('preserve')")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            conn.commit()
        finally:
            conn.close()

        with self.assertRaisesRegex(ResourceCaptureError, "schema_too_new"):
            SelectedResourceCapture(
                self.config,
                source=None,
                archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
            ).archive_id

        conn = sqlite3.connect(self.capture_db)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION + 1)
            self.assertEqual(
                conn.execute("SELECT value FROM future_owner").fetchone()[0],
                "preserve",
            )
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'resource_meta'"
            ).fetchone())
        finally:
            conn.close()

    def test_schema_v2_migrates_inventory_digest_without_replacing_ledger(self):
        capture = self._capture()
        archive_id = capture.archive_id
        conn = sqlite3.connect(self.capture_db)
        try:
            conn.execute(
                "ALTER TABLE resource_backfill_runs DROP COLUMN inventory_digest"
            )
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        finally:
            conn.close()

        reopened = self._capture()

        self.assertEqual(reopened.archive_id, archive_id)
        conn = sqlite3.connect(self.capture_db)
        try:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(resource_backfill_runs)")
            }
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        finally:
            conn.close()
        self.assertIn("inventory_digest", columns)

    def test_attachment_consent_revocation_stops_before_next_byte_operation(self):
        capture = self._capture()
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        consent = {"enabled": True}
        calls = []

        def preserve(row):
            calls.append(int(row["occurrence_id"]))
            consent["enabled"] = False
            return {"status": "missing_retryable", "resolution_method": "fixture"}

        with patch.object(capture.archive, "preserve_file_mention", side_effect=preserve):
            result = capture.resolve_pending_files(
                limit=10,
                consent_check=lambda: consent["enabled"],
            )

        self.assertEqual(result["state"], "consent_revoked")
        self.assertEqual(result["processed"], 1)
        self.assertEqual(len(calls), 1)

    def test_index_projection_respects_resource_backup_operation_lock(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        lock_path = self.capture_db + ".resource-backup.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = backup.render_obsidian_indexes()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        self.assertEqual(result["state"], "worker_busy")

    def test_projection_root_lock_serializes_distinct_capture_databases(self):
        first_capture = self._ready_capture()
        self._backup(first_capture)
        projection_root = os.path.join(
            self.obsidian_root,
            "微信群聊",
            "关注推送",
        )
        os.makedirs(projection_root, exist_ok=True)
        first_backup = self._backup(first_capture)

        def projection_files():
            result = {}
            for root, _dirs, files in os.walk(projection_root):
                for name in files:
                    path = os.path.join(root, name)
                    with open(path, "rb") as handle:
                        result[os.path.relpath(path, projection_root)] = handle.read()
            return result

        before = projection_files()
        second_config = dict(self.config)
        second_config["resource_capture_db"] = os.path.join(
            self.root,
            "second-projection-capture.db",
        )
        second_config["attachment_archive_root"] = os.path.join(
            self.root,
            "second-projection-archive",
        )
        settings_path = os.path.join(self.root, "second-resource-backup.json")
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        contender = context.Process(
            target=_render_projection_worker,
            args=(
                second_config,
                first_capture.archive_id,
                settings_path,
                start,
                results,
            ),
        )
        try:
            with first_backup._projection_worker_lock():
                contender.start()
                start.set()
                contender.join(10)
                self.assertEqual(contender.exitcode, 0)
                result = results.get(timeout=2)
        finally:
            if contender.pid is not None:
                contender.join(5)

        self.assertEqual(result["state"], "worker_busy")
        self.assertEqual(projection_files(), before)

    def test_explicit_verify_detects_same_size_target_corruption(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        first = backup.run()
        self.assertEqual(first["state"], "sync_delegated")
        delivery = next(iter(backup._delivery_map().values()))
        path = os.path.join(backup.backup_root, delivery["target_relpath"])
        size = os.path.getsize(path)
        with open(path, "wb") as handle:
            handle.write(b"x" * size)

        verified = backup.verify(first["snapshot"]["snapshot_id"])

        self.assertEqual(verified["state"], "target_failed")
        self.assertEqual(verified["failed"], 1)
        self.assertFalse(verified["remote_verified"])

    def test_delivery_receipt_rejects_target_size_mismatch_without_reading_bytes(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        first = backup.run()
        self.assertEqual(first["state"], "sync_delegated")
        delivery = next(iter(backup._delivery_map().values()))
        path = os.path.join(backup.backup_root, delivery["target_relpath"])
        with open(path, "ab") as handle:
            handle.write(b"x")

        with patch(
            "core.resource_backup._hash_path",
            side_effect=AssertionError("size mismatch hashed target bytes"),
        ) as hash_path, patch.object(
            backup,
            "_source_path",
            side_effect=AssertionError("size mismatch opened the source CAS"),
        ) as source_path:
            second = backup.run()

        self.assertEqual(second["state"], "target_failed")
        self.assertIn("target_object_conflict", second["error_codes"])
        hash_path.assert_not_called()
        source_path.assert_not_called()

    def test_target_marker_rejects_a_different_archive_at_same_mount_root(self):
        first_capture = self._ready_capture()
        first_backup = self._backup(first_capture)
        self.assertEqual(first_backup.run()["state"], "sync_delegated")

        second_config = dict(self.config)
        second_config["resource_capture_db"] = os.path.join(
            self.root,
            "second-capture.db",
        )
        second_config["attachment_archive_root"] = os.path.join(
            self.root,
            "second-archive",
        )
        second_config["monitor_obsidian_root"] = os.path.join(
            self.root,
            "second-obsidian",
        )
        second_capture = SelectedResourceCapture(
            second_config,
            source=None,
            now_func=lambda: 1_787_500_000,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000002",
        )
        second_capture.initialize_selected_chat_cursors(start_timestamp=0)
        second_backup = MountedResourceBackup(
            second_config,
            capture=second_capture,
            now_func=lambda: 1_787_600_000,
        )

        result = second_backup.run()

        self.assertEqual(result["state"], "target_failed")
        self.assertIn("destination_archive_mismatch", result["error_codes"])

    def test_target_side_lock_serializes_distinct_capture_databases(self):
        first_capture = self._ready_capture()
        first_backup = self._backup(first_capture)
        self.assertEqual(first_backup.run()["state"], "sync_delegated")
        second_config = dict(self.config)
        second_config["resource_capture_db"] = os.path.join(
            self.root,
            "parallel-capture.db",
        )
        second_capture = SelectedResourceCapture(
            second_config,
            source=None,
            now_func=lambda: 1_787_500_000,
            archive_id_factory=lambda: first_capture.archive_id,
        )
        second_capture.initialize_selected_chat_cursors(start_timestamp=0)
        second_backup = MountedResourceBackup(
            second_config,
            capture=second_capture,
            now_func=lambda: 1_787_600_000,
        )

        with first_backup._target_worker_lock():
            result = second_backup.run()

        self.assertEqual(result["state"], "worker_busy")

    def test_missing_archive_id_with_identity_bound_rows_fails_closed(self):
        capture = self._capture()
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        conn = sqlite3.connect(self.capture_db)
        try:
            conn.execute("DELETE FROM resource_meta WHERE key = 'archive_id'")
            conn.commit()
        finally:
            conn.close()

        with self.assertRaisesRegex(Exception, "archive_identity_missing"):
            self._capture().archive_id

    def test_capture_run_reports_source_unavailable_instead_of_healthy(self):
        capture = SelectedResourceCapture(
            self.config,
            source=None,
            now_func=lambda: 1_787_500_000,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)

        result = capture.run(resolve_limit=10)

        self.assertEqual(result["state"], "source_unavailable")
        self.assertEqual(result["scan"]["state"], "source_unavailable")
        evidence = capture.source_inventory_evidence()
        self.assertFalse(evidence["complete"])
        self.assertEqual(evidence["error_codes"], ["source_unavailable"])

    def test_capture_run_reports_worker_busy_and_never_resolves_after_failed_lock(self):
        capture = self._capture()
        lock_path = self.capture_db + ".capture.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(
                capture.archive,
                "preserve_file_mention",
                side_effect=AssertionError("resolver escaped capture lock"),
            ):
                result = capture.run(resolve_files=True)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        self.assertEqual(result["state"], "worker_busy")
        self.assertEqual(result["scan"]["state"], "worker_busy")
        self.assertEqual(result["resolve"]["state"], "not_run_worker_busy")

    def test_capture_run_never_promotes_an_unknown_scan_state_to_healthy(self):
        capture = self._capture()
        with patch.object(
            capture,
            "_scan_locked",
            return_value={"state": "future_scan_state"},
        ):
            result = capture.run(resolve_files=False)

        self.assertEqual(result["state"], "future_scan_state")
        self.assertEqual(result["scan"]["state"], "future_scan_state")

    def test_capture_run_releases_source_snapshot_before_file_resolution(self):
        capture = self._capture()
        active = {"source": False}
        events = []

        @contextmanager
        def snapshot():
            active["source"] = True
            events.append("enter")
            try:
                yield
            finally:
                active["source"] = False
                events.append("exit")

        def scan():
            events.append(f"scan:{active['source']}")
            return {"state": "healthy"}

        def resolve(*, limit, consent_check=None):
            del limit, consent_check
            events.append(f"resolve:{active['source']}")
            return {
                "state": "healthy",
                "processed": 0,
                "ready_local": 0,
                "failed": 0,
            }

        capture.source.source_snapshot = snapshot
        with (
            patch.object(capture, "_scan_locked", side_effect=scan),
            patch.object(
                capture,
                "_resolve_pending_files_locked",
                side_effect=resolve,
            ),
        ):
            result = capture.run(resolve_files=True)

        self.assertEqual(result["state"], "healthy")
        self.assertEqual(events, ["enter", "scan:True", "exit", "resolve:False"])

    def test_shared_outcome_requires_nested_scan_success(self):
        outcome = evaluate_resource_backup_outcome(
            {
                "state": "healthy",
                "scan": {"state": "worker_busy"},
                "resolve": {"state": "skipped"},
            },
            {
                "state": "sync_delegated",
                "obsidian": {"state": "written"},
            },
        )

        self.assertFalse(outcome["completed"])
        self.assertEqual(outcome["state"], "worker_busy")
        self.assertEqual(outcome["scan_state"], "worker_busy")

    def test_shared_outcome_separates_operational_success_from_coverage(self):
        coverage = {
            "delivered_occurrences": 3,
            "delivered_objects": 2,
            "non_delivered_occurrences": 4,
            "awaiting_resolution": 4,
        }
        outcome = evaluate_resource_backup_outcome(
            {
                "state": "healthy",
                "scan": {"state": "healthy"},
                "resolve": {"state": "skipped"},
            },
            {
                "state": "pending_resources",
                "obsidian": {"state": "written"},
                "coverage": coverage,
                "coverage_complete": False,
            },
        )

        self.assertFalse(outcome["completed"])
        self.assertTrue(outcome["operational_success"])
        self.assertFalse(outcome["coverage_complete"])
        self.assertEqual(outcome["coverage"], coverage)
        self.assertEqual(outcome["state"], "pending_resources")

    def test_file_state_compare_and_set_cannot_overwrite_newer_success(self):
        capture = self._capture()
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        row = next(item for item in capture.occurrences() if item["kind"] == "file")
        conn = sqlite3.connect(self.capture_db)
        try:
            conn.execute(
                "UPDATE resource_occurrences SET status = 'ready_local', updated_at = ? "
                "WHERE occurrence_id = ?",
                (float(row["updated_at"]) + 1, row["occurrence_id"]),
            )
            conn.commit()
        finally:
            conn.close()

        changed = capture._set_file_state(
            row["occurrence_id"],
            "retry_wait",
            expected_status=row["status"],
            expected_updated_at=row["updated_at"],
        )

        self.assertFalse(changed)
        conn = sqlite3.connect(self.capture_db)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM resource_occurrences WHERE occurrence_id = ?",
                    (row["occurrence_id"],),
                ).fetchone()[0],
                "ready_local",
            )
        finally:
            conn.close()

    def test_rejected_file_state_cas_is_not_counted_as_ready_or_failed(self):
        capture = self._capture()
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        with (
            patch.object(
                capture.archive,
                "preserve_file_mention",
                return_value={
                    "status": "ready_local",
                    "resolution_method": "fixture",
                    "sha256": "a" * 64,
                    "size": 1,
                    "object_relpath": "objects/aa/fixture",
                },
            ),
            patch.object(capture, "_set_file_state", return_value=False),
        ):
            result = capture.resolve_pending_files(limit=1)

        self.assertEqual(result["state"], "degraded")
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["ready_local"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["superseded"], 1)

    def test_capture_run_can_skip_attachment_cache_without_resolver_access(self):
        capture = self._capture()
        capture.initialize_selected_chat_cursors(start_timestamp=0)

        with patch.object(
            capture,
            "resolve_pending_files",
            side_effect=AssertionError("must not touch attachment cache"),
        ):
            result = capture.run(resolve_files=False)

        self.assertEqual(result["resolve"]["state"], "skipped")
        self.assertEqual(result["resolve"]["reason"], "file_resolution_disabled")

    def test_capture_run_without_arguments_fails_closed_for_attachment_bytes(self):
        capture = self._capture()
        capture.initialize_selected_chat_cursors(start_timestamp=0)

        with patch.object(
            capture,
            "resolve_pending_files",
            side_effect=AssertionError("default run must not read attachment cache"),
        ):
            result = capture.run()

        self.assertEqual(result["resolve"]["state"], "skipped")

    def test_occurrence_insert_and_cursor_advance_are_one_transaction(self):
        capture = self._capture()
        capture.initialize_selected_chat_cursors(start_timestamp=0)

        with patch.object(capture, "_insert_occurrences", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                capture.scan()

        conn = sqlite3.connect(self.capture_db)
        conn.row_factory = sqlite3.Row
        try:
            shard = conn.execute("SELECT * FROM resource_shards").fetchone()
            occurrence_count = conn.execute(
                "SELECT COUNT(*) FROM resource_occurrences"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNotNone(shard)
        self.assertEqual(int(shard["cursor_timestamp"]), 0)
        self.assertEqual(occurrence_count, 0)


    def test_missing_mounted_target_is_not_recreated(self):
        capture = self._ready_capture()
        missing = os.path.join(self.root, "CloudStorage", "missing-mount")
        config = dict(self.config)
        config["resource_backup_target"] = missing
        backup = MountedResourceBackup(
            config,
            capture=capture,
            now_func=lambda: 1_787_600_000,
            id_factory=lambda: "fixture",
        )

        result = backup.run()

        self.assertEqual(result["state"], "destination_unavailable")
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(result["obsidian"]["state"], "written")
        chat_root = os.path.join(
            self.obsidian_root,
            "微信群聊",
            "关注推送",
            "猫猫研究群",
        )
        self.assertTrue(os.path.isfile(os.path.join(chat_root, "00-资源索引.md")))

    def test_unresolved_files_keep_catalog_snapshot_but_do_not_claim_sync_delegated(self):
        capture = self._capture()
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        for filename in os.listdir(self.file_month):
            os.unlink(os.path.join(self.file_month, filename))
        resolved = capture.resolve_pending_files(limit=10)
        self.assertEqual(resolved["ready_local"], 0)
        self.assertEqual(resolved["failed"], 2)
        backup = self._backup(capture)

        result = backup.run()

        self.assertEqual(result["state"], "pending_resources")
        self.assertEqual(result["copied"], 0)
        self.assertEqual(result["unresolved_files"], 2)
        self.assertEqual(result["snapshot"]["state"], "written")
        snapshot_dir = os.path.join(
            backup.backup_root, "snapshots", result["snapshot"]["snapshot_id"]
        )
        with open(os.path.join(snapshot_dir, "manifest.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["snapshot_completeness"], "catalog_complete")
        self.assertEqual(manifest["handoff_semantics"], "pending_resources")
        self.assertEqual(manifest["unresolved_file_count"], 2)
        self.assertTrue(os.path.isfile(os.path.join(snapshot_dir, "COMPLETE")))

    def test_snapshot_separates_durable_catalog_from_incomplete_source_observation(self):
        source = InventoryAwareSource(
            {
                "generation-a": [{
                    "timestamp": 100,
                    "source_message_id": "durable-link",
                    "text": "https://example.com/durable",
                    "resources": [],
                }],
            },
            complete=False,
            digest="inventory-incomplete",
            missing=1,
        )
        capture = SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        scanned = capture.scan()
        backup = self._backup(capture)

        result = backup.run()
        snapshot_dir = os.path.join(
            backup.backup_root,
            "snapshots",
            result["snapshot"]["snapshot_id"],
        )
        with open(os.path.join(snapshot_dir, "manifest.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)

        self.assertEqual(scanned["state"], "source_degraded")
        self.assertEqual(manifest["snapshot_completeness"], "catalog_complete")
        self.assertFalse(manifest["source_observation"]["complete"])
        self.assertEqual(
            manifest["source_observation"]["inventory_digest"],
            "inventory-incomplete",
        )
        self.assertFalse(result["snapshot"]["source_complete"])

    def test_source_observation_change_rebuilds_unchanged_catalog_snapshot(self):
        source = InventoryAwareSource(
            {
                "generation-a": [{
                    "timestamp": 100,
                    "source_message_id": "stable-link",
                    "text": "https://example.com/stable",
                    "resources": [],
                }],
            },
            complete=False,
            digest="inventory-before",
            missing=1,
        )
        capture = SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        identifiers = iter(("first", "second"))
        backup = MountedResourceBackup(
            self.config,
            capture=capture,
            now_func=lambda: 1_787_600_000,
            id_factory=lambda: next(identifiers),
        )
        first = backup.run()
        source.complete = True
        source.digest = "inventory-after"
        source.missing = 0
        capture.scan()

        second = backup.run()

        self.assertEqual(first["snapshot"]["state"], "written")
        self.assertEqual(second["snapshot"]["state"], "written")
        self.assertNotEqual(
            first["snapshot"]["snapshot_id"],
            second["snapshot"]["snapshot_id"],
        )
        self.assertTrue(second["snapshot"]["source_complete"])

    def test_unchanged_catalog_rebuilds_deleted_snapshot(self):
        capture = self._ready_capture()
        identifiers = iter(("first", "second"))
        backup = MountedResourceBackup(
            self.config,
            capture=capture,
            now_func=lambda: 1_787_600_000,
            id_factory=lambda: next(identifiers),
        )
        first = backup.run()
        shutil.rmtree(os.path.join(
            backup.backup_root, "snapshots", first["snapshot"]["snapshot_id"]
        ))

        second = backup.run()

        self.assertEqual(second["snapshot"]["state"], "written")
        self.assertNotEqual(
            second["snapshot"]["snapshot_id"], first["snapshot"]["snapshot_id"]
        )
        self.assertIsNotNone(backup._load_snapshot())

    def test_unchanged_catalog_rebuilds_snapshot_without_valid_complete(self):
        capture = self._ready_capture()
        identifiers = iter(("first", "second"))
        backup = MountedResourceBackup(
            self.config,
            capture=capture,
            now_func=lambda: 1_787_600_000,
            id_factory=lambda: next(identifiers),
        )
        first = backup.run()
        complete = os.path.join(
            backup.backup_root, "snapshots", first["snapshot"]["snapshot_id"],
            "COMPLETE",
        )
        os.unlink(complete)

        second = backup.run()

        self.assertEqual(second["snapshot"]["state"], "written")
        self.assertNotEqual(
            second["snapshot"]["snapshot_id"], first["snapshot"]["snapshot_id"]
        )
        self.assertIsNotNone(backup._load_snapshot())

    def test_snapshot_fingerprint_includes_link_export_mode(self):
        source = FakeSource({
            self.selected: [{
                "timestamp": 100,
                "source_message_id": "plain-link",
                "text": "https://example.com/plain?topic=one",
                "resources": [],
            }],
        })
        capture = SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        identifiers = iter(("redacted", "off"))
        redacted = MountedResourceBackup(
            self.config, capture=capture, link_export_mode="redacted",
            now_func=lambda: 300, id_factory=lambda: next(identifiers),
        )
        first = redacted.run()
        off = MountedResourceBackup(
            self.config, capture=capture, link_export_mode="off",
            now_func=lambda: 300, id_factory=lambda: next(identifiers),
        )

        second = off.run()

        self.assertEqual(second["snapshot"]["state"], "written")
        self.assertNotEqual(
            second["snapshot"]["snapshot_id"], first["snapshot"]["snapshot_id"]
        )
        self.assertEqual(
            off._load_snapshot()["manifest"]["link_export_mode"], "off"
        )

    def test_redacted_mode_covers_authorization_and_credential_variants(self):
        url = (
            "https://example.com/file?authorization=Bearer-secret"
            "&credential=credential-secret&credentials=plural-secret"
            "&jwt=jwt-secret&x-amz-credential=amz-secret"
            "&x-amz-signature=signed-secret&safe=visible"
        )

        redacted = _redact_url(url)

        for secret in (
            "Bearer-secret", "credential-secret", "plural-secret",
            "jwt-secret", "amz-secret", "signed-secret",
        ):
            self.assertNotIn(secret, redacted)
        self.assertIn("safe=visible", redacted)
        self.assertGreaterEqual(redacted.count("REDACTED"), 6)

    def test_malformed_matched_url_fails_closed_without_crashing(self):
        malformed = "https://example.com／evil?token=secret-value"

        self.assertEqual(_redact_url(malformed), "REDACTED_INVALID_URL")
        source = FakeSource({
            self.selected: [{
                "timestamp": 100,
                "source_message_id": "malformed-link",
                "text": malformed,
                "resources": [],
            }],
        })
        capture = SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        backup = self._backup(capture)

        result = backup.run()

        self.assertEqual(result["state"], "sync_delegated")
        snapshot = backup._load_snapshot()
        with open(
            os.path.join(snapshot["directory"], "resources.jsonl"),
            encoding="utf-8",
        ) as handle:
            exported = handle.read()
        self.assertIn("REDACTED_INVALID_URL", exported)
        self.assertNotIn("secret-value", exported)

    def test_unresolved_and_unconfigured_runs_have_nonzero_cli_exit_status(self):
        for state in (
            "pending_resources",
            "target_not_configured",
            "no_selected_chats",
        ):
            with self.subTest(state=state):
                self.assertEqual(resource_backup_cli._exit_code({"state": state}), 2)

    def test_unmanaged_obsidian_indexes_are_preserved_with_generated_fallbacks(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        month = capture.occurrences()[0]["source_month"]
        chat_root = os.path.join(
            self.obsidian_root,
            "微信群聊",
            "关注推送",
            "猫猫研究群",
        )
        month_root = os.path.join(chat_root, "资源索引")
        os.makedirs(month_root, exist_ok=True)
        root_path = os.path.join(chat_root, "00-资源索引.md")
        month_path = os.path.join(month_root, month + ".md")
        scope_root = os.path.join(
            self.obsidian_root,
            "微信群聊",
            "关注推送",
            "00-资源索引.md",
        )
        with open(root_path, "w", encoding="utf-8") as handle:
            handle.write("# user-owned root\n")
        with open(month_path, "w", encoding="utf-8") as handle:
            handle.write("# user-owned month\n")
        with open(scope_root, "w", encoding="utf-8") as handle:
            handle.write("# user-owned scope root\n")

        result = backup.render_obsidian_indexes()

        self.assertEqual(result["state"], "written")
        with open(root_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "# user-owned root\n")
        with open(month_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "# user-owned month\n")
        with open(scope_root, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "# user-owned scope root\n")
        generated_root = os.path.join(chat_root, "00-资源索引.generated.md")
        generated_month = os.path.join(month_root, month + ".generated.md")
        generated_scope_root = os.path.join(
            self.obsidian_root,
            "微信群聊",
            "关注推送",
            "00-资源索引.generated.md",
        )
        self.assertTrue(os.path.isfile(generated_root))
        self.assertTrue(os.path.isfile(generated_month))
        self.assertTrue(os.path.isfile(generated_scope_root))
        with open(generated_root, encoding="utf-8") as handle:
            root_text = handle.read()
        self.assertIn(f"[[资源索引/{month}.generated|{month}]]", root_text)
        with open(generated_scope_root, encoding="utf-8") as handle:
            scope_text = handle.read()
        self.assertIn(
            "[[猫猫研究群/00-资源索引.generated|猫猫研究群]]",
            scope_text,
        )

    def test_deselecting_last_chat_writes_empty_root_and_collects_only_managed_indexes(self):
        capture = self._ready_capture()
        canonical = dict(self.config)
        capture.config_loader = lambda: dict(canonical)
        backup = self._backup(capture)
        first = backup.render_obsidian_indexes()
        self.assertGreater(first["occurrences"], 0)
        chat_root = os.path.join(
            self.obsidian_root,
            "微信群聊",
            "关注推送",
            "猫猫研究群",
        )
        user_file = os.path.join(chat_root, "我的笔记.md")
        with open(user_file, "w", encoding="utf-8") as handle:
            handle.write("user-owned\n")

        canonical["resource_backup_selected_chats"] = []
        second = backup.render_obsidian_indexes()
        scope_root = os.path.join(
            self.obsidian_root,
            "微信群聊",
            "关注推送",
            "00-资源索引.md",
        )

        self.assertEqual(second["occurrences"], 0)
        with open(scope_root, encoding="utf-8") as handle:
            self.assertIn("当前没有已选群聊资源", handle.read())
        self.assertTrue(os.path.isfile(user_file))
        self.assertFalse(os.path.exists(os.path.join(chat_root, "00-资源索引.md")))
        self.assertFalse(os.path.exists(os.path.join(chat_root, "资源索引")))

    def test_local_projection_rejects_child_symlink_without_outside_writes(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        projection_root = backup.obsidian_projection_root
        outside = os.path.join(self.root, "outside-projection")
        os.makedirs(projection_root, exist_ok=True)
        os.makedirs(outside, exist_ok=True)
        os.symlink(outside, os.path.join(projection_root, "猫猫研究群"))

        result = backup.run()

        self.assertEqual(result["state"], "projection_failed")
        self.assertEqual(
            result["obsidian"]["error_code"],
            "projection_directory_conflict",
        )
        self.assertEqual(os.listdir(outside), [])
        self.assertFalse(os.path.exists(backup.backup_root))

    def test_utf8_component_budget_preserves_extension_and_stable_hash_suffix(self):
        capture = self._capture()
        backup = self._backup(capture)
        long_name = "猫" * 200 + ".pdf"
        relative = backup._target_relpath({
            "object_sha256": "a" * 64,
            "original_name": long_name,
        })
        component = os.path.basename(relative)

        self.assertLessEqual(len(component.encode("utf-8")), 255)
        self.assertTrue(component.endswith(".pdf"))
        self.assertRegex(component, r"--[0-9a-f]{8}\.pdf$")

    def test_resource_settings_preserve_concurrent_disjoint_process_updates(self):
        path = os.path.join(self.root, "resource_backup.json")
        save_resource_backup_settings(
            {"target": "", "link_export_mode": "redacted"},
            path=path,
        )
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        target = context.Process(
            target=_settings_patch_worker,
            args=(path, {"target": self.target}, start),
        )
        mode = context.Process(
            target=_settings_patch_worker,
            args=(path, {"link_export_mode": "off"}, start),
        )
        target.start()
        mode.start()
        start.set()
        target.join(10)
        mode.join(10)

        self.assertEqual(target.exitcode, 0)
        self.assertEqual(mode.exitcode, 0)
        settings = load_resource_backup_settings(path)
        self.assertEqual(settings["target"], self.target)
        self.assertEqual(settings["link_export_mode"], "off")

    def test_delivery_receipt_does_not_trust_a_symlink_replacement(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        first = backup.run()
        self.assertEqual(first["state"], "sync_delegated")
        delivery = next(iter(backup._delivery_map().values()))
        target_path = os.path.join(backup.backup_root, delivery["target_relpath"])
        replacement = os.path.join(self.root, "replacement.bin")
        with open(replacement, "wb") as handle:
            handle.write(b"replacement")
        os.unlink(target_path)
        os.symlink(replacement, target_path)

        second = backup.run()

        self.assertEqual(second["state"], "target_failed")
        self.assertTrue(
            {"target_object_conflict", "target_outside_backup_root"}
            & set(second["error_codes"])
        )
        self.assertTrue(os.path.islink(target_path))

    def test_insufficient_target_space_keeps_local_state_and_skips_snapshot(self):
        capture = self._ready_capture()
        config = dict(self.config)
        config["resource_backup_min_free_bytes"] = 1
        backup = MountedResourceBackup(
            config,
            capture=capture,
            now_func=lambda: 1_787_600_000,
            id_factory=lambda: "fixture",
        )
        usage = type("Usage", (), {"free": 0})()

        with patch("core.resource_backup.shutil.disk_usage", return_value=usage):
            result = backup.run()

        self.assertEqual(result["state"], "target_failed")
        self.assertIn("insufficient_target_space", result["error_codes"])
        self.assertEqual(result["obsidian"]["state"], "written")
        self.assertFalse(os.path.isdir(os.path.join(backup.backup_root, "snapshots")))

    def test_duplicate_chat_aliases_get_stable_distinct_index_directories(self):
        other = "other-selected@chatroom"
        config = dict(self.config)
        config["monitor_chats"] = [
            {"username": self.selected, "name": "Same Alias"},
            {"username": other, "name": "Same Alias"},
        ]
        config["resource_backup_selected_chats"] = [
            {"username": self.selected, "alias": "Same Alias"},
            {"username": other, "alias": "Same Alias"},
        ]
        second_message = dict(self._unselected_message())
        second_message["source_message_id"] = "wgmsg_other_selected"
        source = FakeSource({
            self.selected: [self._selected_message()],
            other: [second_message],
        })
        capture = SelectedResourceCapture(
            config,
            source=source,
            now_func=lambda: 1_787_500_000,
            random_func=lambda: 0.5,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        capture.resolve_pending_files(limit=10)
        backup = MountedResourceBackup(
            config,
            capture=capture,
            now_func=lambda: 1_787_600_000,
            id_factory=lambda: "fixture",
        )
        backup.render_obsidian_indexes()
        chat_root = os.path.join(
            self.obsidian_root, "微信群聊", "关注推送"
        )
        directories = sorted(
            name for name in os.listdir(chat_root)
            if os.path.isdir(os.path.join(chat_root, name))
        )
        self.assertEqual(len(directories), 2)
        self.assertTrue(all(name.startswith("Same Alias--") for name in directories))
        self.assertNotEqual(directories[0], directories[1])

    def test_concurrent_handoff_returns_worker_busy(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        lock_path = self.capture_db + ".resource-backup.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(
                backup,
                "_render_obsidian_indexes_safely",
                side_effect=AssertionError("projection escaped operation lock"),
            ):
                result = backup.run()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        self.assertEqual(result["state"], "worker_busy")
        self.assertEqual(result["copied"], 0)

    def test_target_month_index_uses_correct_relative_object_link(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        backup.run()
        target_index_root = os.path.join(
            backup.backup_root, "views", "猫猫研究群", "资源索引"
        )
        month_file = next(
            os.path.join(target_index_root, name)
            for name in os.listdir(target_index_root)
            if name.endswith(".md")
        )
        with open(month_file, encoding="utf-8") as handle:
            text = handle.read()

        self.assertIn("../../../objects/sha256/", text)

    def test_target_overlap_fails_closed(self):
        capture = self._ready_capture()
        unsafe = dict(self.config)
        unsafe["resource_backup_target"] = self.archive_root
        backup = MountedResourceBackup(
            unsafe,
            capture=capture,
            now_func=lambda: 1_787_600_000,
            id_factory=lambda: "fixture",
        )
        result = backup.run()
        self.assertEqual(result["state"], "invalid_target")
        self.assertEqual(result["error_code"], "target_overlaps_local_source")

    def test_target_subtree_symlink_escape_is_rejected_before_external_write(self):
        capture = self._ready_capture()
        outside = os.path.join(self.root, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.target, "wgo-resource-backup"))
        backup = self._backup(capture)

        plan = backup.plan()
        result = backup.run()

        self.assertEqual(plan["state"], "invalid_target")
        self.assertEqual(plan["error_code"], "target_directory_conflict")
        self.assertEqual(result["state"], "invalid_target")
        self.assertEqual(result["error_code"], "target_directory_conflict")
        self.assertFalse(os.path.exists(os.path.join(outside, "v3")))

    def test_plan_rejects_symlink_at_objects_snapshots_and_views(self):
        capture = self._ready_capture()
        for relative in ("objects", "snapshots", "views"):
            with self.subTest(relative=relative):
                branch_target = os.path.join(self.root, "target-" + relative)
                outside = os.path.join(self.root, "outside-" + relative)
                os.makedirs(os.path.join(branch_target, "wgo-resource-backup", "v3"))
                os.makedirs(outside)
                os.symlink(
                    outside,
                    os.path.join(branch_target, "wgo-resource-backup", "v3", relative),
                )
                config = dict(self.config)
                config["resource_backup_target"] = branch_target
                backup = MountedResourceBackup(config, capture=capture)

                plan = backup.plan()

                self.assertEqual(plan["state"], "invalid_target")
                self.assertEqual(plan["error_code"], "target_subtree_conflict")

    def test_file_backup_view_rejects_a_nested_symlink_before_external_write(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        chat_root = os.path.join(
            backup.backup_root,
            "views",
            "猫猫研究群",
        )
        outside = os.path.join(self.root, "outside-file-view")
        os.makedirs(chat_root, exist_ok=True)
        os.makedirs(outside)
        os.symlink(outside, os.path.join(chat_root, "文件备份"))

        plan = backup.plan()
        result = backup.run()

        self.assertEqual(plan["state"], "invalid_target")
        self.assertEqual(plan["error_code"], "target_subtree_conflict")
        self.assertEqual(result["state"], "target_failed")
        self.assertIn("target_subtree_conflict", result["error_codes"])
        self.assertEqual(os.listdir(outside), [])

    def test_run_returns_target_failed_for_snapshot_directory_conflict(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        backup._ensure_target_dir(backup.backup_root)
        with open(os.path.join(backup.backup_root, "snapshots"), "wb") as handle:
            handle.write(b"conflict")

        result = backup.run()

        self.assertEqual(result["state"], "target_failed")
        self.assertIn("target_subtree_conflict", result["error_codes"])
        self.assertIsNone(backup._state_row())

    def test_run_returns_target_failed_for_target_view_conflict(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        backup._ensure_target_dir(backup.backup_root)
        with open(os.path.join(backup.backup_root, "views"), "wb") as handle:
            handle.write(b"conflict")

        result = backup.run()

        self.assertEqual(result["state"], "target_failed")
        self.assertIn("target_subtree_conflict", result["error_codes"])
        self.assertIsNone(backup._state_row())

    def test_status_handoff_semantics_are_derived_from_current_evidence(self):
        capture = self._ready_capture()

        no_target_config = dict(self.config)
        no_target_config["resource_backup_target"] = ""
        no_target = MountedResourceBackup(no_target_config, capture=capture)
        self.assertEqual(
            no_target.status()["handoff_semantics"], "target_not_configured"
        )

        pending = self._backup(capture)
        self.assertEqual(pending.status()["handoff_semantics"], "pending")
        completed = pending.run()
        self.assertEqual(completed["state"], "sync_delegated")
        self.assertEqual(pending.status()["handoff_semantics"], "sync_delegated")

        snapshot_dir = pending._snapshot_dir()
        os.unlink(os.path.join(snapshot_dir, "COMPLETE"))
        self.assertEqual(pending.status()["handoff_semantics"], "pending")

        unresolved_config = dict(self.config)
        unresolved_config["resource_capture_db"] = os.path.join(
            self.root, "unresolved-resource-capture.db"
        )
        unresolved_config["attachment_archive_root"] = os.path.join(
            self.root, "unresolved-archive"
        )
        unresolved_capture = SelectedResourceCapture(
            unresolved_config,
            source=FakeSource({self.selected: [self._selected_message()]}),
            now_func=lambda: 1_787_500_000,
            random_func=lambda: 0.5,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000002",
        )
        unresolved_capture.initialize_selected_chat_cursors(start_timestamp=0)
        unresolved_capture.scan()
        for filename in os.listdir(self.file_month):
            os.unlink(os.path.join(self.file_month, filename))
        unresolved_capture.resolve_pending_files(limit=10)
        unresolved_target = os.path.join(self.root, "unresolved-target")
        os.makedirs(unresolved_target)
        unresolved_config["resource_backup_target"] = unresolved_target
        unresolved = MountedResourceBackup(
            unresolved_config, capture=unresolved_capture
        )
        self.assertEqual(
            unresolved.status()["handoff_semantics"], "pending_resources"
        )

    def test_raw_chat_username_never_crosses_export_boundary(self):
        config = dict(self.config)
        config["monitor_chats"] = [{
            "username": self.selected,
            "name": self.selected,
        }]
        config["monitor_chat_aliases"] = {}
        config["resource_backup_selected_chats"] = [{
            "username": self.selected,
            "alias": self.selected,
        }]
        capture = SelectedResourceCapture(
            config,
            source=FakeSource({self.selected: [self._selected_message()]}),
            now_func=lambda: 1_787_500_000,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        capture.resolve_pending_files(limit=10)
        backup = MountedResourceBackup(config, capture=capture)

        result = backup.run()

        self.assertNotIn("@chatroom", json.dumps(result, ensure_ascii=False))
        snapshot = backup._load_snapshot()
        with open(
            os.path.join(snapshot["directory"], "resources.jsonl"), encoding="utf-8"
        ) as handle:
            self.assertNotIn("@chatroom", handle.read())
        self.assertTrue(all(
            "@chatroom" not in path
            for path, _dirs, _files in os.walk(backup.backup_root)
        ))

    def test_casefold_equivalent_aliases_get_distinct_paths(self):
        other = "other-selected@chatroom"
        config = dict(self.config)
        config["monitor_chats"] = [
            {"username": self.selected, "name": "Research"},
            {"username": other, "name": "research"},
        ]
        config["resource_backup_selected_chats"] = [
            {"username": self.selected, "alias": "Research"},
            {"username": other, "alias": "research"},
        ]
        source = FakeSource({
            self.selected: [self._selected_message()],
            other: [{
                "timestamp": 100,
                "source_message_id": "casefold-other",
                "text": "https://example.com/other",
                "resources": [],
            }],
        })
        capture = SelectedResourceCapture(
            config,
            source=source,
            now_func=lambda: 1_787_500_000,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        backup = MountedResourceBackup(config, capture=capture)

        backup.render_obsidian_indexes()

        root = os.path.join(self.obsidian_root, "微信群聊", "关注推送")
        directories = sorted(
            name for name in os.listdir(root)
            if os.path.isdir(os.path.join(root, name))
        )
        self.assertEqual(len(directories), 2)
        self.assertTrue(all("--" in name for name in directories))

    def test_configured_target_symlink_is_rejected(self):
        capture = self._ready_capture()
        actual = os.path.join(self.root, "actual-target")
        target_link = os.path.join(self.root, "target-link")
        os.makedirs(actual)
        os.symlink(actual, target_link)
        config = dict(self.config)
        config["resource_backup_target"] = target_link
        backup = MountedResourceBackup(config, capture=capture)

        plan = backup.plan()
        result = backup.run()

        self.assertEqual(plan["state"], "invalid_target")
        self.assertEqual(plan["error_code"], "target_is_symlink")
        self.assertEqual(result["state"], "invalid_target")
        self.assertFalse(os.path.exists(os.path.join(actual, "wgo-resource-backup")))


class ResourceBackupCliTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "monitor_chats": [
                {"username": "first@chatroom", "name": "First Group"},
                {"username": "second@chatroom", "name": "Second Group"},
            ],
            "monitor_chat_aliases": {"second@chatroom": "猫猫研究群"},
            "resource_backup_selected_chats": [
                {"username": "second@chatroom", "alias": "猫猫研究群"},
            ],
            "google_drive_file_sync_selected_chats": [
                {"username": "legacy@chatroom", "alias": "Legacy OAuth Group"},
            ],
        }

    def test_list_chats_is_privacy_safe_and_marks_current_selection(self):
        output = io.StringIO()
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            redirect_stdout(output),
        ):
            result = resource_backup_cli.main(["list-chats"])

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(
            payload["chats"],
            [
                {"index": 1, "alias": "First Group", "selected": False},
                {"index": 2, "alias": "猫猫研究群", "selected": True},
            ],
        )
        self.assertNotIn("@chatroom", output.getvalue())

    def test_set_selected_chats_uses_active_list_indexes_without_oauth_state(self):
        output = io.StringIO()
        updated = dict(self.config)
        updated["resource_backup_selected_chats"] = [{
            "username": "first@chatroom",
            "alias": "First Group",
            "selected_since": 1_787_500_000,
        }]
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            patch.object(
                resource_backup_cli,
                "update_resource_backup_selection",
                return_value=(updated, {"state": "initialized"}),
            ) as update,
            patch.object(resource_backup_cli.time, "time", return_value=1_787_500_000),
            patch.object(
                resource_backup_cli.uuid,
                "uuid4",
                return_value="00000000-0000-0000-0000-000000000123",
            ),
            redirect_stdout(output),
        ):
            result = resource_backup_cli.main(["set-selected-chats", "1"])

        saved = update.call_args.args[1]
        self.assertEqual(result, 0)
        self.assertEqual(
            saved,
            [{
                "username": "first@chatroom",
                "alias": "First Group",
                "selected_since": 1_787_500_000,
                "selection_id": "00000000-0000-0000-0000-000000000123",
            }],
        )
        self.assertNotIn("google_drive_file_sync_selected_chats", saved)
        self.assertNotIn("@chatroom", output.getvalue())

    def test_cli_scan_source_unavailable_returns_structured_json(self):
        output = io.StringIO()
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            patch.object(resource_backup_cli, "_source", return_value=None),
            patch.object(resource_backup_cli, "_capture") as capture_factory,
            redirect_stdout(output),
        ):
            capture_factory.return_value.scan.return_value = {
                "state": "source_unavailable",
                "error_code": "source_unavailable",
            }
            result = resource_backup_cli.main(["scan"])

        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue())["state"], "source_unavailable")

    def test_source_unavailable_is_a_local_state_not_system_exit(self):
        with patch.object(resource_backup_cli, "get_cached_keys", return_value={}):
            self.assertIsNone(resource_backup_cli._source({"db_dir": ""}))

    def test_backfill_all_is_an_explicit_all_history_plan(self):
        output = io.StringIO()
        capture = unittest.mock.Mock()
        capture.backfill.return_value = {
            "state": "planned",
            "discovered_links": 3,
            "inserted_links": 0,
        }
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            patch.object(resource_backup_cli, "_capture", return_value=capture),
            redirect_stdout(output),
        ):
            result = resource_backup_cli.main(["backfill", "--all"])

        self.assertEqual(result, 0)
        capture.backfill.assert_called_once_with(0, apply=False, run_id="")
        self.assertEqual(json.loads(output.getvalue())["state"], "planned")

    def test_cli_backfill_links_is_an_explicit_links_only_plan(self):
        output = io.StringIO()
        capture = unittest.mock.Mock()
        capture.backfill_links.return_value = {
            "state": "planned",
            "mode": "links_only",
            "source_complete": True,
            "discovered_links": 3,
            "inserted_links": 0,
        }
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            patch.object(resource_backup_cli, "_capture", return_value=capture),
            redirect_stdout(output),
        ):
            result = resource_backup_cli.main(["backfill-links", "--all"])

        self.assertEqual(result, 0)
        capture.backfill_links.assert_called_once_with(0, apply=False, run_id="")
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["mode"], "links_only")
        self.assertTrue(payload["source_complete"])

    def test_cli_enables_long_lived_updates_without_enabling_file_access(self):
        output = io.StringIO()
        config = dict(self.config)
        config["resource_backup_enabled"] = False
        updated = dict(config, resource_backup_enabled=True)
        with (
            patch.object(resource_backup_cli, "load_config", return_value=config),
            patch.object(resource_backup_cli, "update_config", return_value=updated) as update,
            redirect_stdout(output),
        ):
            result = resource_backup_cli.main(["enable"])

        self.assertEqual(result, 0)
        self.assertEqual(update.call_args.kwargs["patch"], {
            "resource_backup_enabled": True,
        })
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["runtime"], "long_lived_app")
        self.assertEqual(
            payload["file_resolution_policy"],
            "explicit_per_run_or_app_session",
        )

    def test_cli_backfill_apply_requires_exact_plan_run_id(self):
        with self.assertRaisesRegex(SystemExit, "--apply requires"):
            resource_backup_cli.main(["backfill-links", "--all", "--apply"])

    def test_cli_backfill_apply_reopens_source_for_inventory_check(self):
        output = io.StringIO()
        capture = unittest.mock.Mock()
        capture.backfill_links.return_value = {
            "state": "applied",
            "source_complete": True,
        }
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            patch.object(resource_backup_cli, "_capture", return_value=capture) as factory,
            redirect_stdout(output),
        ):
            result = resource_backup_cli.main([
                "backfill-links", "--all", "--apply", "--run-id",
                "00000000-0000-0000-0000-000000000099",
            ])

        self.assertEqual(result, 0)
        factory.assert_called_once_with(self.config, source=True)
        capture.backfill_links.assert_called_once_with(
            0,
            apply=True,
            run_id="00000000-0000-0000-0000-000000000099",
        )

    def test_cli_run_with_source_unavailable_still_handoffs(self):
        output = io.StringIO()
        capture = type("Capture", (), {})()
        capture.run = lambda resolve_limit=50, resolve_files=True: {
            "state": "source_unavailable",
            "scan": {"state": "source_unavailable"},
            "resolve": {"state": "healthy", "ready_local": 1},
        }
        backup = type("Backup", (), {})()
        backup.run = lambda: {"state": "sync_delegated", "copied": 1}
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            patch.object(resource_backup_cli, "_source", return_value=None),
            patch.object(resource_backup_cli, "_capture", return_value=capture),
            patch.object(resource_backup_cli, "_backup", return_value=backup) as backup_factory,
            redirect_stdout(output),
        ):
            result = resource_backup_cli.main(["run"])

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 2)
        self.assertEqual(payload["state"], "source_unavailable")
        self.assertEqual(payload["backup"]["state"], "sync_delegated")
        backup_factory.assert_called_once()

    def test_cli_run_fails_when_nested_projection_failed(self):
        output = io.StringIO()
        capture = Mock()
        capture.run.return_value = {
            "state": "healthy",
            "scan": {"state": "healthy"},
            "resolve": {"state": "skipped"},
        }
        backup = Mock()
        backup.run.return_value = {
            "state": "sync_delegated",
            "obsidian": {"state": "projection_failed"},
        }
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            patch.object(resource_backup_cli, "_capture", return_value=capture),
            patch.object(resource_backup_cli, "_backup", return_value=backup),
            redirect_stdout(output),
        ):
            exit_code = resource_backup_cli.main(["run"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["state"], "projection_failed")

    def test_cli_run_skips_file_resolution_unless_explicitly_requested(self):
        output = io.StringIO()
        capture = unittest.mock.Mock()
        capture.run.return_value = {
            "state": "healthy",
            "scan": {"state": "healthy"},
            "resolve": {"state": "skipped"},
        }
        backup = unittest.mock.Mock()
        backup.run.return_value = {
            "state": "idle",
            "obsidian": {"state": "written"},
        }
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            patch.object(resource_backup_cli, "_capture", return_value=capture),
            patch.object(resource_backup_cli, "_backup", return_value=backup),
            redirect_stdout(output),
        ):
            result = resource_backup_cli.main(["run"])

        self.assertEqual(result, 0)
        capture.run.assert_called_once_with(resolve_limit=50, resolve_files=False)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["completed"])
        self.assertTrue(payload["operational_success"])
        self.assertTrue(payload["coverage_complete"])

    def test_cli_run_reports_healthy_pending_coverage_without_changing_exit_contract(self):
        output = io.StringIO()
        capture = Mock()
        capture.run.return_value = {
            "state": "healthy",
            "scan": {"state": "healthy"},
            "resolve": {"state": "skipped"},
        }
        coverage = {
            "delivered_objects": 2,
            "delivered_occurrences": 3,
            "non_delivered_occurrences": 4,
        }
        backup = Mock()
        backup.run.return_value = {
            "state": "pending_resources",
            "obsidian": {"state": "written"},
            "coverage": coverage,
            "coverage_complete": False,
        }
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            patch.object(resource_backup_cli, "_capture", return_value=capture),
            patch.object(resource_backup_cli, "_backup", return_value=backup),
            redirect_stdout(output),
        ):
            exit_code = resource_backup_cli.main(["run"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertFalse(payload["completed"])
        self.assertTrue(payload["operational_success"])
        self.assertFalse(payload["coverage_complete"])
        self.assertEqual(payload["coverage"], coverage)


if __name__ == "__main__":
    unittest.main()
