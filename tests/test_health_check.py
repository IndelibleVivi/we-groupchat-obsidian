import os
import plistlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import scripts.health_check as health_check
from scripts.health_check import (
    launch_agent_report,
    latest_monitor_runtime_result,
    latest_notification_backend_status,
    monitor_state_health,
    parse_launch_agent_status,
    review_queue_pending_count,
    source_inventory_health,
)
from core.monitor import state_file_for_chat
from core.monitor_state import MonitorStateStore
from core.review_queue import ReviewQueue
from core.source_inventory import SourceInventoryStore, source_namespaces_for_root


class HealthCheckTests(unittest.TestCase):
    def test_monitor_state_inspect_does_not_create_parent_or_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "absent" / "monitor-state.json"

            snapshot = MonitorStateStore(state_path).inspect()

            self.assertFalse(snapshot.existed)
            self.assertFalse(state_path.exists())
            self.assertFalse(state_path.parent.exists())
            self.assertFalse(Path(str(state_path) + ".lock").exists())

    def test_monitor_state_health_distinguishes_all_operator_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "states"
            runtime_log = root / "autostart.out.log"
            config = {
                "monitor_chats": [
                    {"username": "one@chatroom", "name": "Private One"},
                ]
            }
            state_path = state_file_for_chat(
                "one@chatroom", state_dir=str(state_dir)
            )
            MonitorStateStore(state_path).initialize_if_absent({
                "last_checked_ts": 100,
                "source_inventory_digest": "opaque-digest",
                "source_cursors": {
                    "opaque-shard": {
                        "generation_id": "opaque-generation",
                        "cursor_token": "100:9",
                    }
                },
            })
            runtime_log.write_text(
                "[monitor] no_messages: no_messages\n", encoding="utf-8"
            )

            report = monitor_state_health(
                config,
                state_dir=str(state_dir),
                runtime_log=runtime_log,
            )
            self.assertEqual(report["state"], "healthy")
            self.assertEqual(report["counts"], {
                "healthy": 1, "missing": 0, "corrupt": 0,
            })
            self.assertEqual(report["shard_cursors"], 1)
            self.assertEqual(report["generation_bound"], 1)
            self.assertEqual(report["inventory_bound_chats"], 1)
            self.assertNotIn("one@chatroom", str(report))
            self.assertNotIn(str(state_dir), str(report))

            runtime_log.write_text(
                "[monitor] monitor_state_conflict: monitor_state_conflict\n",
                encoding="utf-8",
            )
            self.assertEqual(
                monitor_state_health(
                    config,
                    state_dir=str(state_dir),
                    runtime_log=runtime_log,
                )["state"],
                "conflict",
            )

            missing = monitor_state_health(
                {"monitor_chats": [{"username": "missing@chatroom"}]},
                state_dir=str(state_dir),
                runtime_log=root / "missing.log",
            )
            self.assertEqual(missing["state"], "missing")

            corrupt_path = state_file_for_chat(
                "corrupt@chatroom", state_dir=str(state_dir)
            )
            Path(corrupt_path).write_text("{broken", encoding="utf-8")
            corrupt = monitor_state_health(
                {"monitor_chats": [{"username": "corrupt@chatroom"}]},
                state_dir=str(state_dir),
                runtime_log=root / "missing.log",
            )
            self.assertEqual(corrupt["state"], "corrupt")

    def test_latest_monitor_runtime_result_resets_old_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "out.log"
            log.write_text(
                "[monitor] monitor_state_conflict: monitor_state_conflict\n"
                "[monitor] 命中[notification-muted]: redacted\n",
                encoding="utf-8",
            )
            self.assertEqual(latest_monitor_runtime_result(log), "notified")

    def test_source_inventory_health_is_read_only_and_counts_degradation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "db_storage"
            source_root.mkdir()
            config = {"db_dir": str(source_root)}
            inventory_path = root / "source_inventory.json"
            store = SourceInventoryStore(path=inventory_path)
            _cache_namespace, namespace = source_namespaces_for_root(source_root)
            store.reconcile(namespace, [
                {
                    "relative_path": "message/message_0.db",
                    "generation_id": "generation-0",
                    "state": "present",
                },
                {
                    "relative_path": "message/message_1.db",
                    "generation_id": "generation-1",
                    "state": "present",
                },
            ])
            store.reconcile(namespace, [
                {
                    "relative_path": "message/message_0.db",
                    "generation_id": "generation-0",
                    "state": "present",
                },
                {
                    "relative_path": "message/message_2.db",
                    "generation_id": "generation-2",
                    "state": "cache_only",
                },
            ])

            report = source_inventory_health(config, store=store)
            self.assertEqual(report["state"], "degraded")
            self.assertEqual(report["counts"]["present"], 1)
            self.assertEqual(report["counts"]["missing_file"], 1)
            self.assertEqual(report["counts"]["cache_only"], 1)
            self.assertEqual(report["shards"], [])
            self.assertNotIn(str(source_root), str(report))
            self.assertNotIn("message/message_0.db", str(report))

            absent_path = root / "absent-inventory.json"
            absent = source_inventory_health(
                config,
                store=SourceInventoryStore(path=absent_path),
            )
            self.assertEqual(absent["state"], "uninitialized")
            self.assertFalse(absent_path.exists())
            self.assertFalse(Path(str(absent_path) + ".lock").exists())

    def test_health_check_reports_v1_singleton_as_one_active_chat(self):
        config = {
            "monitor_chat_username": "room@chatroom",
            "monitor_chat_display_name": "示例人机互动群",
            "monitor_chat_taxonomy_profiles": {
                "room@chatroom": "human_ai_intimacy_v1",
            },
            "monitor_enabled": True,
            "monitor_interval_minutes": 3,
            "ai_provider": "ollama",
            "attachment_archive_enabled": True,
        }
        record = type(
            "Record",
            (),
            {"plist_path": Path("/missing.plist"), "label": "label"},
        )()
        status = type(
            "Status",
            (),
            {
                "loaded": False,
                "running": False,
                "state": "",
                "job_state": "",
                "last_exit_code": "",
            },
        )()

        patchers = (
            patch("scripts.health_check.load_config", return_value=config),
            patch("scripts.health_check.get_cached_keys", return_value={}),
            patch("scripts.health_check.load_key", return_value=""),
            patch("scripts.health_check.recent_autostart_ai_success", return_value=False),
            patch("scripts.health_check.is_wechat_signed", return_value=False),
            patch("scripts.health_check.process_lookup_available", return_value=False),
            patch("scripts.health_check.launch_agent_report", return_value=(record, status)),
            patch("scripts.health_check.launch_agent_status", return_value=status),
            patch("scripts.health_check.autostart_log_status", return_value=("", "", False)),
            patch("scripts.health_check.latest_notification_backend_status", return_value=("", False)),
            patch(
                "scripts.health_check.notification_identity_status_for_launch_agent",
                return_value={"ok": True},
            ),
            patch("scripts.health_check.count_markdown", return_value=(0, "")),
            patch("scripts.health_check.recent_topics", return_value=(0, [])),
            patch(
                "scripts.health_check.relation_integrity_status",
                return_value=("unavailable", False),
            ),
            patch("scripts.health_check.review_queue_pending_count", return_value=0),
            patch("scripts.health_check._sensitive_log_status", return_value=("absent", False)),
            patch(
                "scripts.health_check.source_guard_status",
                return_value={
                    "state": "disabled",
                    "last_result": "disabled",
                    "restart_budget_remaining": 3,
                    "source_freshness": "unknown",
                },
            ),
            patch(
                "scripts.health_check.AttachmentArchive.from_config",
                return_value=type(
                    "ArchiveStatus",
                    (),
                    {"status": lambda self: {"state": "healthy", "counts": {"pending": 2}, "objects": 1}},
                )(),
            ),
            patch(
                "scripts.health_check.AttachmentBackup.from_config",
                return_value=type(
                    "BackupStatus",
                    (),
                    {"status": lambda self: {"state": "target_not_configured", "complete_snapshots": 0}},
                )(),
            ),
            patch(
                "scripts.health_check.GoogleDriveFileSync.inspect_status",
                return_value={
                    "state": "disabled",
                    "auth": "auth_required",
                    "selected_chat_count": 0,
                    "queue_counts": {"waiting_cache": 2},
                    "last_scan_at": 123,
                    "last_verified_upload_at": 0,
                    "next_retry_at": 456,
                    "root_state": "unknown",
                    "uploaded_unique_objects": 1,
                    "shortcut_placements": 3,
                    "last_error_code": "",
                },
            ),
        )
        with ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            output = StringIO()
            with redirect_stdout(output):
                health_check.main([])

        text = output.getvalue()
        self.assertIn("[OK] Monitor chats: 1 selected", text)
        self.assertIn(
            "[OK] Taxonomy presets: explicit 1 / legacy 0 / unknown 0 / free-form 0",
            text,
        )
        self.assertNotIn("Monitor chats: 未选择", text)
        self.assertIn("WeChat source guard: disabled / disabled", text)
        self.assertIn(
            "Source guard runtime: long_lived_app; "
            "legacy_agent_installed=False; legacy_agent_loaded=False",
            text,
        )
        self.assertIn("Attachment archive: enabled / healthy; objects=1; pending=2", text)
        self.assertIn("Attachment backup target: not configured (optional)", text)
        self.assertIn(
            "Google Drive selected-chat files: disabled / active; "
            "auth=auth_required; selected=0; queue=waiting_cache=2;",
            text,
        )
        self.assertIn("root=unknown; objects=1; shortcuts=3; last_error=-", text)

    def test_relation_integrity_status_is_counts_only(self):
        report = {
            "available": True,
            "error": "",
            "total_relations": 100,
            "known_broken_reason_count": 12,
            "broader_relation_failure_count": 14,
            "cross_chat_edge_count": 7,
            "cross_chat_risky_edge_count": 6,
            "self_loop_count": 2,
            "exact_replay_group_count": 3,
            "exact_replay_excess_event_count": 41,
            "orphan_event_count": 1,
            "orphan_relation_count": 1,
            "fts_matches_topics": False,
            "dominant_relation": "updates",
            "dominant_relation_ratio": 0.95,
            "warnings": ["known_broken_relation_reason"],
            "examples": [{"source_title": "private title"}],
        }

        with patch("scripts.health_check.audit_relations", return_value=report):
            text, failed = health_check.relation_integrity_status("knowledge.db")

        self.assertTrue(failed)
        self.assertIn("relations 100", text)
        self.assertIn("known broken 12", text)
        self.assertIn("relation failures 14", text)
        self.assertIn("cross-chat 7", text)
        self.assertIn("risky cross-chat 6", text)
        self.assertIn("replays 3 groups / 41 excess", text)
        self.assertIn("orphans 2", text)
        self.assertIn("FTS mismatch", text)
        self.assertNotIn("private title", text)

    def test_launch_agent_loaded_but_exited_is_not_running(self):
        output = """
gui/501/com.example.wechat-summary = {
    state = not running
    job state = exited
    last exit code = 0
}
"""

        status = parse_launch_agent_status(0, output)

        self.assertTrue(status.loaded)
        self.assertFalse(status.running)
        self.assertEqual(status.state, "not running")
        self.assertEqual(status.job_state, "exited")
        self.assertEqual(status.last_exit_code, "0")

    def test_launch_agent_running_state_is_running(self):
        output = """
gui/501/com.example.wechat-summary = {
    state = running
    job state = running
}
"""

        status = parse_launch_agent_status(0, output)

        self.assertTrue(status.loaded)
        self.assertTrue(status.running)

    def test_launch_agent_report_uses_discovered_legacy_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir = root / "repo"
            project_dir.mkdir()
            agents_dir = root / "LaunchAgents"
            agents_dir.mkdir()
            label = "com.example.local-wechat-summary"
            with (agents_dir / f"{label}.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": label,
                        "ProgramArguments": ["/bin/bash", str(project_dir / "启动.command"), "--autostart"],
                        "WorkingDirectory": str(project_dir),
                    },
                    handle,
                )

            def fake_runner(args, capture_output=True, text=True):
                class Result:
                    returncode = 0
                    stdout = "state = running\njob state = running\n"
                    stderr = ""

                return Result()

            record, status = launch_agent_report(
                project_dir=project_dir,
                launch_agents_dir=agents_dir,
                runner=fake_runner,
            )

            self.assertEqual(record.label, label)
            self.assertTrue(status.running)

    def test_launch_agent_report_can_find_same_named_runtime_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir = root / "Documents" / "we-groupchat-obsidian"
            project_dir.mkdir(parents=True)
            runtime_dir = root / "projects" / "we-groupchat-obsidian"
            runtime_dir.mkdir(parents=True)
            agents_dir = root / "LaunchAgents"
            agents_dir.mkdir()
            label = "com.example.local-wechat-summary"
            with (agents_dir / f"{label}.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": label,
                        "ProgramArguments": ["/bin/bash", str(runtime_dir / "启动.command"), "--autostart"],
                        "WorkingDirectory": str(runtime_dir),
                    },
                    handle,
                )

            def fake_runner(args, capture_output=True, text=True):
                class Result:
                    returncode = 0
                    stdout = "state = running\njob state = running\n"
                    stderr = ""

                return Result()

            record, status = launch_agent_report(
                project_dir=project_dir,
                launch_agents_dir=agents_dir,
                runner=fake_runner,
            )

            self.assertEqual(record.label, label)
            self.assertEqual(Path(record.working_directory).name, "we-groupchat-obsidian")
            self.assertTrue(status.running)

    def test_launch_agent_report_warns_when_no_managed_job_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir = root / "repo"
            project_dir.mkdir()
            agents_dir = root / "LaunchAgents"
            agents_dir.mkdir()

            def fake_runner(args, capture_output=True, text=True):
                class Result:
                    returncode = 113
                    stdout = ""
                    stderr = "Could not find service"

                return Result()

            record, status = launch_agent_report(
                project_dir=project_dir,
                launch_agents_dir=agents_dir,
                runner=fake_runner,
            )

            self.assertFalse(status.loaded)
            self.assertFalse(status.running)
            self.assertEqual(record.label, "io.github.indeliblevivi.we-groupchat-obsidian")

    def test_review_queue_pending_count_is_metadata_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ReviewQueue(os.path.join(tmp, "review_queue"))
            queue.create_or_reuse({
                "source_chat": "wxid_should_not_print",
                "title": "example-toolkit.zip",
                "summary": "只统计数量，不输出详情。",
                "message_hash": "one",
            })

            self.assertEqual(review_queue_pending_count(queue.queue_dir), 1)

    def test_latest_notification_backend_status_hides_notification_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "autostart.out.log")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("[notify] rumps ok: 关注推送 / 私密标题\n")

            self.assertEqual(latest_notification_backend_status(path), ("rumps ok", False))

    def test_health_check_redacts_paths_chats_topics_and_logs_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "xwechat_files" / "wxid_private" / "db_storage"
            db_dir.mkdir(parents=True)
            obsidian_root = root / "Obsidian Vault"
            notes_dir = obsidian_root / "微信群聊" / "关注推送"
            notes_dir.mkdir(parents=True)
            latest_note = notes_dir / "Sensitive Group" / "工具更新" / "secret-title.md"
            latest_note.parent.mkdir(parents=True)
            latest_note.write_text("secret", encoding="utf-8")
            knowledge_db = root / "monitor_knowledge.db"
            conn = sqlite3.connect(knowledge_db)
            conn.execute("CREATE TABLE topics (topic_id INTEGER PRIMARY KEY, title TEXT)")
            conn.execute("INSERT INTO topics(title) VALUES ('私密 topic title')")
            conn.commit()
            conn.close()
            key_log = root / "extract_keys.log"
            key_log.write_text("raw key material", encoding="utf-8")

            config = {
                "db_dir": str(db_dir),
                "ai_provider": "qwen",
                "ai_model": "model-private",
                "ai_base_url": "https://api.private.invalid/v1?token=TOPSECRET",
                "monitor_enabled": True,
                "monitor_interval_minutes": 3,
                "monitor_chats": [
                    {"username": "secret-one@chatroom", "name": "Secret One"},
                    {"username": "secret-two@chatroom", "name": "示例人机互动群"},
                ],
                "monitor_chat_taxonomy_profiles": {
                    "secret-one@chatroom": "human_ai_intimacy_v1",
                },
                "monitor_topic": "私密 topic",
                "monitor_obsidian_root": str(obsidian_root),
                "monitor_obsidian_subdir": "微信群聊/关注推送",
                "monitor_knowledge_db": str(knowledge_db),
                "daily_digest_enabled": True,
                "daily_digest_time": "21:30",
                "daily_digest_timezone": "Asia/Shanghai",
                "daily_digest_dir": "",
            }
            agent_record = type(
                "Record",
                (),
                {
                    "plist_path": root / "LaunchAgents" / "secret.plist",
                    "label": "com.private.label",
                },
            )()
            agent_status = type(
                "Status",
                (),
                {
                    "loaded": True,
                    "running": False,
                    "state": "not running",
                    "job_state": "exited",
                    "last_exit_code": "1",
                },
            )()

            with patch("scripts.health_check.load_config", return_value=config), \
                 patch("scripts.health_check.get_cached_keys", return_value={"message/message_0.db": {"enc_key": "x"}}), \
                 patch("scripts.health_check.load_key", return_value="api-key"), \
                 patch("scripts.health_check.is_wechat_signed", return_value=True), \
                 patch("scripts.health_check.process_lookup_available", return_value=True), \
                 patch("scripts.health_check.is_wechat_running", return_value=True), \
                 patch("scripts.health_check.launch_agent_report", return_value=(agent_record, agent_status)), \
                 patch("scripts.health_check.launch_agent_status", return_value=agent_status), \
                 patch("scripts.health_check.autostart_log_status", return_value=("Last autostart stderr", "/private/raw stderr", True)), \
                 patch("scripts.health_check.latest_notification_backend_status", return_value=("rumps ok", False)), \
                 patch(
                     "scripts.health_check.notification_identity_status_for_launch_agent",
                     return_value={
                         "ok": False,
                         "bundle_identifier": "org.python.python",
                         "bundle_name": "Python",
                         "bundle_path": "/private/runtime/Python.app",
                         "expected_bundle_identifier": "io.github.indeliblevivi.we-groupchat-obsidian",
                         "message": "running under Python notification identity",
                     },
                     create=True,
                 ), \
                 patch(
                     "scripts.health_check.monitor_state_health",
                     return_value={
                         "state": "conflict",
                         "chat_count": 2,
                         "counts": {"healthy": 2, "missing": 0, "corrupt": 0},
                         "runtime_result": "monitor_state_conflict",
                         "cursor_chats": 2,
                         "shard_cursors": 6,
                         "generation_bound": 6,
                         "inventory_bound_chats": 2,
                         "legacy_states": 0,
                     },
                 ), \
                 patch(
                     "scripts.health_check.source_inventory_health",
                     return_value={
                         "state": "degraded",
                         "complete": False,
                         "counts": {
                             "present": 2,
                             "generation_changed": 0,
                             "missing_file": 1,
                             "cache_only": 1,
                             "key_missing": 0,
                             "unreadable": 0,
                         },
                         "error_codes": ["source_missing_file", "source_cache_only"],
                         "shards": [{
                             "relative_path": "private/account/message_0.db",
                             "logical_shard_id": "opaque",
                             "generation_id": "opaque-generation",
                             "state": "present",
                         }],
                     },
                 ), \
                 patch(
                     "scripts.health_check.inspect_mounted_resource_backup",
                     return_value={
                         "state": "ready",
                         "enabled": True,
                         "target_configured": True,
                         "handoff_semantics": "sync_delegated",
                         "delivery_counts": {"sync_delegated": 7},
                         "provider_side_sync": "unknown",
                         "remote_verified": False,
                     },
                 ), \
                 patch(
                     "scripts.health_check.GoogleDriveFileSync.inspect_status",
                     return_value={
                         "state": "enabled",
                         "auth": "connected",
                         "selected_chat_count": 2,
                         "queue_counts": {},
                         "last_scan_at": 10,
                         "last_verified_upload_at": 9,
                         "next_retry_at": 0,
                         "root_state": "ready",
                         "root_web_view_link": "https://drive.invalid/?token=TOPSECRET",
                         "uploaded_unique_objects": 3,
                         "shortcut_placements": 4,
                         "last_error_code": "",
                     },
                 ), \
                 patch("scripts.health_check.check_new_databases", return_value=[]), \
                 patch("scripts.health_check.EXTRACT_LOG", str(key_log)):
                output = StringIO()
                with redirect_stdout(output):
                    health_check.main([])

            text = output.getvalue()
            self.assertIn("WeChat DB: configured", text)
            self.assertIn("Monitor chats: 2 selected", text)
            self.assertIn(
                "[WARN] Taxonomy presets: explicit 1 / legacy 1 / unknown 0 / free-form 0",
                text,
            )
            self.assertIn("Knowledge topics: 1", text)
            self.assertIn("Notification identity: Python / org.python.python", text)
            self.assertIn("expected io.github.indeliblevivi.we-groupchat-obsidian", text)
            self.assertNotIn("/private/runtime/Python.app", text)
            self.assertIn("Sensitive key extraction log: present", text)
            self.assertIn("Monitor state: conflict", text)
            self.assertIn(
                "Source inventory: degraded; present=2; generation_changed=0; "
                "missing=1; cache_only=1",
                text,
            )
            self.assertIn(
                "Mounted resource handoff: runtime=ready; "
                "handoff=sync_delegated; target=configured; delegated_objects=7; "
                "provider_side_sync=unknown; remote_verified=False",
                text,
            )
            self.assertIn(
                "Link preview: state=link_preview_disabled; network_requests=0",
                text,
            )
            self.assertIn(
                "MCP compatibility: legacy_read_only; send=mcp_send_retired",
                text,
            )
            self.assertIn(
                "Windows: W0.2B.1 portable lock/path source only; "
                "product_support=not_claimed",
                text,
            )
            self.assertIn(
                "Google Drive API remote verification: "
                "verified_ledger_objects; verified_objects=3",
                text,
            )
            self.assertNotIn(str(db_dir), text)
            self.assertNotIn(str(obsidian_root), text)
            self.assertNotIn("secret-one@chatroom", text)
            self.assertNotIn("Secret One", text)
            self.assertNotIn("secret-two@chatroom", text)
            self.assertNotIn("示例人机互动群", text)
            self.assertNotIn("私密 topic title", text)
            self.assertNotIn("secret-title.md", text)
            self.assertNotIn("/private/raw stderr", text)
            self.assertNotIn("com.private.label", text)
            self.assertNotIn("private/account/message_0.db", text)
            self.assertNotIn("api.private.invalid", text)
            self.assertNotIn("drive.invalid", text)
            self.assertNotIn("TOPSECRET", text)

    def test_health_check_sensitive_mode_reveals_paths_chats_topics_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "db_storage"
            db_dir.mkdir()
            obsidian_root = root / "vault"
            notes_dir = obsidian_root / "微信群聊" / "关注推送" / "Sensitive Group"
            notes_dir.mkdir(parents=True)
            latest_note = notes_dir / "secret-title.md"
            latest_note.write_text("secret", encoding="utf-8")
            knowledge_db = root / "monitor_knowledge.db"
            conn = sqlite3.connect(knowledge_db)
            conn.execute("CREATE TABLE topics (topic_id INTEGER PRIMARY KEY, title TEXT)")
            conn.execute("INSERT INTO topics(title) VALUES ('私密 topic title')")
            conn.commit()
            conn.close()
            key_log = root / "extract_keys.log"
            key_log.write_text("raw key material", encoding="utf-8")

            config = {
                "db_dir": str(db_dir),
                "ai_provider": "qwen",
                "ai_model": "model-private",
                "monitor_enabled": True,
                "monitor_interval_minutes": 3,
                "monitor_chats": [{"name": "Sensitive Group", "username": "room@chatroom"}],
                "monitor_topic": "私密 topic",
                "monitor_obsidian_root": str(obsidian_root),
                "monitor_obsidian_subdir": "微信群聊/关注推送",
                "monitor_knowledge_db": str(knowledge_db),
                "daily_digest_enabled": True,
                "daily_digest_time": "21:30",
                "daily_digest_timezone": "Asia/Shanghai",
                "daily_digest_dir": "",
            }
            agent_record = type("Record", (), {"plist_path": root / "secret.plist", "label": "com.private.label"})()
            agent_status = type(
                "Status",
                (),
                {"loaded": True, "running": True, "state": "running", "job_state": "running", "last_exit_code": ""},
            )()

            with patch("scripts.health_check.load_config", return_value=config), \
                 patch("scripts.health_check.get_cached_keys", return_value={}), \
                 patch("scripts.health_check.load_key", return_value="api-key"), \
                 patch("scripts.health_check.is_wechat_signed", return_value=True), \
                 patch("scripts.health_check.process_lookup_available", return_value=True), \
                 patch("scripts.health_check.is_wechat_running", return_value=True), \
                 patch("scripts.health_check.launch_agent_report", return_value=(agent_record, agent_status)), \
                 patch("scripts.health_check.launch_agent_status", return_value=agent_status), \
                 patch("scripts.health_check.autostart_log_status", return_value=("Last autostart stderr", "/private/raw stderr", True)), \
                 patch("scripts.health_check.latest_notification_backend_status", return_value=("rumps ok", False)), \
                 patch(
                     "scripts.health_check.notification_identity_status_for_launch_agent",
                     return_value={
                         "ok": True,
                         "bundle_identifier": "io.github.indeliblevivi.we-groupchat-obsidian",
                         "bundle_name": "微信总结",
                         "bundle_path": "/Applications/WeGroupchatObsidian.app",
                         "expected_bundle_identifier": "io.github.indeliblevivi.we-groupchat-obsidian",
                         "message": "notification identity is stable",
                     },
                     create=True,
                 ), \
                 patch(
                     "scripts.health_check.monitor_state_health",
                     return_value={
                         "state": "healthy",
                         "chat_count": 1,
                         "counts": {"healthy": 1, "missing": 0, "corrupt": 0},
                         "runtime_result": "no_messages",
                         "cursor_chats": 1,
                         "shard_cursors": 1,
                         "generation_bound": 1,
                         "inventory_bound_chats": 1,
                         "legacy_states": 0,
                     },
                 ), \
                 patch(
                     "scripts.health_check.source_inventory_health",
                     return_value={
                         "state": "complete",
                         "complete": True,
                         "counts": {"present": 1},
                         "error_codes": [],
                         "shards": [{
                             "relative_path": "message/message_0.db",
                             "logical_shard_id": "opaque-shard",
                             "generation_id": "opaque-generation",
                             "state": "present",
                         }],
                     },
                 ), \
                 patch(
                     "scripts.health_check.inspect_mounted_resource_backup",
                     return_value={
                         "state": "disabled",
                         "enabled": False,
                         "target_configured": False,
                         "handoff_semantics": "target_not_configured",
                         "delivery_counts": {},
                         "provider_side_sync": "unknown",
                         "remote_verified": False,
                     },
                 ), \
                 patch("scripts.health_check.EXTRACT_LOG", str(key_log)):
                output = StringIO()
                with redirect_stdout(output):
                    health_check.main(["--sensitive"])

            text = output.getvalue()
            self.assertIn(str(db_dir), text)
            self.assertIn(str(obsidian_root), text)
            self.assertIn("Sensitive Group", text)
            self.assertIn("私密 topic title", text)
            self.assertIn("secret-title.md", text)
            self.assertIn("/private/raw stderr", text)
            self.assertIn("/Applications/WeGroupchatObsidian.app", text)
            self.assertIn("com.private.label", text)
            self.assertIn("message/message_0.db", text)

    def test_delete_sensitive_key_log_requires_explicit_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_log = Path(tmp) / "extract_keys.log"
            key_log.write_text("raw key material", encoding="utf-8")

            with patch("scripts.health_check.load_config", return_value={}), \
                 patch("scripts.health_check.get_cached_keys", return_value={}), \
                 patch("scripts.health_check.load_key", return_value="api-key"), \
                 patch("scripts.health_check.is_wechat_signed", return_value=False), \
                 patch("scripts.health_check.process_lookup_available", return_value=False), \
                 patch("scripts.health_check.launch_agent_report") as report, \
                 patch("scripts.health_check.launch_agent_status", return_value=type(
                     "Status", (), {"loaded": False, "running": False, "state": "", "job_state": "", "last_exit_code": ""}
                 )()), \
                 patch("scripts.health_check.autostart_log_status", return_value=("", "", False)), \
                 patch("scripts.health_check.latest_notification_backend_status", return_value=("", False)), \
                 patch(
                     "scripts.health_check.notification_identity_status_for_launch_agent",
                     return_value={
                         "ok": True,
                         "bundle_identifier": "io.github.indeliblevivi.we-groupchat-obsidian",
                         "bundle_name": "微信总结",
                         "bundle_path": "",
                         "expected_bundle_identifier": "io.github.indeliblevivi.we-groupchat-obsidian",
                         "message": "notification identity is stable",
                     },
                     create=True,
                 ), \
                 patch("scripts.health_check.review_queue_pending_count", return_value=0), \
                 patch("scripts.health_check.EXTRACT_LOG", str(key_log)):
                record = type("Record", (), {"plist_path": Path(tmp) / "missing.plist", "label": "label"})()
                status = type(
                    "Status",
                    (),
                    {"loaded": False, "running": False, "state": "", "job_state": "", "last_exit_code": ""},
                )()
                report.return_value = (record, status)
                output = StringIO()
                with redirect_stdout(output):
                    health_check.main([])
                self.assertTrue(key_log.exists())
                self.assertIn("Sensitive key extraction log: present", output.getvalue())

                delete_output = StringIO()
                with redirect_stdout(delete_output):
                    health_check.main(["--delete-sensitive-key-log"])
                self.assertFalse(key_log.exists())
                self.assertIn("deleted", delete_output.getvalue())


if __name__ == "__main__":
    unittest.main()
