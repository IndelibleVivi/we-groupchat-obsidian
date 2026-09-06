import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.app_runtime import AppAlreadyRunning
from core.knowledge import KnowledgeStore, build_message_hash
from core.monitor import save_state
from scripts.catch_up_monitor import (
    _pending_messages,
    backup_runtime_state,
    build_reconciliation_receipt,
    drain_monitors,
    apply_catch_up,
    rebuild_projections,
    validate_knowledge_db,
    write_reconciliation_receipt,
    main,
)


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def get_messages(self, _username, **_kwargs):
        return list(self.rows)


class FakeMonitor:
    def __init__(self, results):
        self.results = iter(results)

    def check_once(self):
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


class CatchUpMonitorTests(unittest.TestCase):
    def setUp(self):
        self.instance_lock = MagicMock()
        self.instance_lock.acquire.return_value = self.instance_lock
        self.instance_lock_patcher = patch(
            "scripts.catch_up_monitor.AppInstanceLock",
            return_value=self.instance_lock,
        )
        self.instance_lock_patcher.start()

    def tearDown(self):
        self.instance_lock_patcher.stop()

    def test_cli_requires_explicit_transient_source_consent_before_loading_runtime(self):
        stderr = StringIO()
        with (
            patch("scripts.catch_up_monitor._load_runtime") as load_runtime,
            redirect_stderr(stderr),
        ):
            code = main([])

        self.assertEqual(code, 2)
        load_runtime.assert_not_called()
        self.assertIn("--allow-transient-wechat-source-read", stderr.getvalue())

    def test_cli_explicit_transient_source_consent_allows_audit(self):
        with (
            patch(
                "scripts.catch_up_monitor._load_runtime",
                return_value=({}, [{"username": "chat", "name": "Chat"}], object()),
            ) as load_runtime,
            patch("scripts.catch_up_monitor.audit_pending", return_value=[]) as audit,
            patch("scripts.catch_up_monitor._print_audit"),
        ):
            code = main(["--allow-transient-wechat-source-read"])

        self.assertEqual(code, 0)
        load_runtime.assert_called_once_with()
        audit.assert_called_once()

    def test_pending_messages_preserves_unprocessed_same_timestamp_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            first = {"timestamp": 100.0, "text": "first", "sender": "a"}
            second = {"timestamp": 100.0, "text": "second", "sender": "b"}
            later = {"timestamp": 101.0, "text": "later", "sender": "c"}
            save_state({
                "last_checked_ts": 100.0,
                "last_checked_message_hash_ts": 100.0,
                "last_checked_message_hashes": [build_message_hash([first])],
            }, state_path)

            report = _pending_messages(FakeDB([first, second, later]), "chat", state_path, 10)

            self.assertEqual(report["count"], 2)
            self.assertFalse(report["capped"])

    def test_drain_isolates_chat_error_and_finishes_other_chat(self):
        rows = [
            ({"username": "broken", "name": "Broken"}, FakeMonitor([RuntimeError("boom")])),
            ({"username": "ok", "name": "OK"}, FakeMonitor([
                {
                    "status": "notified",
                    "knowledge_event_written": True,
                    "affected_dates": ["2026-08-02", "2026-08-03"],
                },
                {
                    "status": "duplicate",
                    "knowledge_event_id": 9,
                    "affected_dates": ["2026-08-03"],
                },
                {"status": "no_messages", "source_eof": True},
            ])),
        ]

        result = drain_monitors(rows, max_pages_per_chat=5, max_minutes=5)

        self.assertEqual(result["complete"], ["ok"])
        self.assertIn("RuntimeError", result["blocked"]["broken"])
        self.assertEqual(result["affected_dates"], ["2026-08-02", "2026-08-03"])
        self.assertEqual(result["statuses"], {"notified": 1, "duplicate": 1, "no_messages": 1})
        self.assertEqual(result["per_chat"]["ok"]["event_ids"], [9])
        self.assertEqual(result["per_chat"]["ok"]["outcome"], "complete")
        self.assertEqual(result["per_chat"]["broken"]["outcome"], "blocked")

    def test_partial_receipt_is_content_free_and_resume_supported(self):
        chats = [
            {"username": "first@chatroom", "name": "Private First"},
            {"username": "second@chatroom", "name": "Private Second"},
        ]
        audit = [
            {"username": "first@chatroom", "checkpoint": 10, "count": 20, "capped": False},
            {"username": "second@chatroom", "checkpoint": 30, "count": 40, "capped": False},
        ]
        result = {
            "complete": ["first@chatroom"],
            "blocked": {"second@chatroom": "RuntimeError: provider timeout"},
            "pages": {"first@chatroom": 2, "second@chatroom": 1},
            "statuses": {"notified": 2, "no_match": 1, "no_messages": 1},
            "affected_dates": ["2026-08-02", "2026-08-03"],
            "per_chat": {
                "first@chatroom": {
                    "pages": 2,
                    "statuses": {"notified": 2, "no_messages": 1},
                    "event_ids": [101, 102],
                    "affected_dates": ["2026-08-02"],
                    "outcome": "complete",
                    "blocked_reason": "",
                },
                "second@chatroom": {
                    "pages": 1,
                    "statuses": {"no_match": 1},
                    "event_ids": [],
                    "affected_dates": [],
                    "outcome": "blocked",
                    "blocked_reason": "RuntimeError: provider timeout",
                },
            },
        }
        receipt = build_reconciliation_receipt(
            run_id="run-1",
            started_at="2026-08-07T10:00:00+08:00",
            chats=chats,
            audit=audit,
            checkpoints_after={"first@chatroom": 20, "second@chatroom": 31},
            result=result,
            projections={"indexes": {"written_count": 2}, "digests": [{"date": "2026-08-02", "notes": 2, "actions": 1, "path": "/private/vault.md"}]},
            validation={"ok": True, "quick_check": "ok", "integrity_check": "ok"},
            backup_path=Path("/private/backup"),
            launch_agent={"was_loaded": True, "restore_attempted": True, "restored": True, "error": ""},
            transaction_error="",
        )

        self.assertEqual(receipt["state"], "partial")
        self.assertEqual(receipt["outcome"], "resume_required")
        self.assertTrue(receipt["resume_supported"])
        self.assertEqual(receipt["canonical"]["event_ids"], [101, 102])
        self.assertEqual(receipt["chats"][0]["checkpoint_before"], 10)
        self.assertEqual(receipt["chats"][0]["checkpoint_after"], 20)
        serialized = str(receipt)
        self.assertNotIn("Private First", serialized)
        self.assertNotIn("first@chatroom", serialized)
        self.assertNotIn("/private/vault.md", serialized)
        self.assertNotIn("provider timeout", serialized)

    def test_complete_drain_is_provisional_while_agent_restore_is_pending(self):
        receipt = build_reconciliation_receipt(
            run_id="run-pending",
            started_at="2026-08-30T10:00:00+08:00",
            chats=[{"username": "chat", "name": "Chat"}],
            audit=[{
                "username": "chat",
                "checkpoint": 10,
                "count": 1,
                "capped": False,
            }],
            checkpoints_after={"chat": 11},
            result={
                "complete": ["chat"],
                "blocked": {},
                "affected_dates": [],
                "per_chat": {
                    "chat": {
                        "pages": 1,
                        "statuses": {"no_match": 1},
                        "event_ids": [],
                        "affected_dates": [],
                        "outcome": "complete",
                        "blocked_reason": "",
                    },
                },
            },
            projections={"indexes": {}, "digests": []},
            validation={"ok": True},
            backup_path=Path("/private/backup"),
            launch_agent={
                "was_loaded": True,
                "restore_attempted": False,
                "restored": None,
                "error": "",
            },
            transaction_error="",
        )

        self.assertEqual(receipt["state"], "partial")
        self.assertEqual(receipt["outcome"], "drain_complete_restore_pending")
        self.assertFalse(receipt["resume_supported"])

    def test_receipt_writer_is_private_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch(
                    "scripts.catch_up_monitor.os.fsync", wraps=os.fsync
                ) as fsync,
                patch(
                    "scripts.catch_up_monitor.os.replace", wraps=os.replace
                ) as replace,
            ):
                path = write_reconciliation_receipt(
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "state": "complete",
                    },
                    receipts_dir=tmp,
                )

            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["state"], "complete")
            self.assertGreaterEqual(fsync.call_count, 2)
            replace.assert_called_once()
            self.assertEqual(list(Path(tmp).glob(".run-1.*.tmp")), [])

    def test_rebuild_projections_uses_exact_affected_dates(self):
        store = MagicMock()
        store.write_date_indexes.return_value = {"written_count": 2}
        digest = {
            "new_notes_count": 1,
            "today_action_count": 0,
            "path": "/tmp/digest.md",
        }
        with (
            patch("scripts.catch_up_monitor.KnowledgeStore.from_config", return_value=store),
            patch("scripts.catch_up_monitor.digest_output_path", return_value=("/tmp/existing.md", "")),
            patch("scripts.catch_up_monitor.os.path.isfile", return_value=True),
            patch("scripts.catch_up_monitor.write_daily_digest", return_value=digest) as write,
        ):
            result = rebuild_projections({}, ["2026-08-02", "2026-08-03", "2026-08-03"])

        self.assertEqual([item["date"] for item in result["digests"]], ["2026-08-02", "2026-08-03"])
        self.assertEqual(
            [call.kwargs["target_date"] for call in write.call_args_list],
            ["2026-08-02", "2026-08-03"],
        )

    def test_backup_and_validation_cover_canonical_db_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "knowledge.db"
            vault = root / "vault"
            store = KnowledgeStore(str(db_path), str(vault))
            conn = store.connect()
            conn.close()
            state_path = root / "chat-state.json"
            state_path.write_text('{"last_checked_ts": 1}\n', encoding="utf-8")

            import scripts.catch_up_monitor as module
            old_state_file_for_chat = module.state_file_for_chat
            module.state_file_for_chat = lambda _username: str(state_path)
            try:
                backup = backup_runtime_state(
                    {"monitor_knowledge_db": str(db_path)},
                    [{"username": "chat", "name": "Chat"}],
                    backup_base=root / "backups",
                )
            finally:
                module.state_file_for_chat = old_state_file_for_chat

            self.assertTrue((backup / "monitor_knowledge.db").is_file())
            self.assertTrue((backup / "monitor_state" / "chat-state.json").is_file())
            report = validate_knowledge_db({"monitor_knowledge_db": str(db_path)})
            self.assertTrue(report["ok"])
            self.assertEqual(report["integrity_check"], "ok")
            self.assertEqual(report["topics"], report["fts"])

    def test_apply_restores_previously_loaded_agent_after_transaction_error(self):
        args = SimpleNamespace(audit_limit=100, max_pages_per_chat=5, max_minutes=5)
        record = MagicMock(label="test.agent", plist_path=Path("/tmp/test.plist"))
        status = MagicMock(loaded=True)
        audit = [{"name": "Chat", "username": "chat", "count": 1, "capped": False}]

        with (
            patch("scripts.catch_up_monitor.audit_pending", return_value=audit),
            patch("scripts.catch_up_monitor.launch_agent_report", return_value=(record, status)),
            patch("scripts.catch_up_monitor._stop_launch_agent") as stop,
            patch("scripts.catch_up_monitor.backup_runtime_state", return_value=Path("/tmp/backup")),
            patch("scripts.catch_up_monitor.TopicMonitor"),
            patch("scripts.catch_up_monitor.drain_monitors", side_effect=RuntimeError("boom")),
            patch("scripts.catch_up_monitor._restore_launch_agent") as restore,
            patch("scripts.catch_up_monitor.write_reconciliation_receipt") as write_receipt,
        ):
            result = apply_catch_up(
                {},
                [{"username": "chat", "name": "Chat"}],
                FakeDB([]),
                args,
            )

        self.assertEqual(result, 1)
        stop.assert_called_once_with(record)
        self.instance_lock.acquire.assert_called_once_with()
        self.instance_lock.release.assert_called_once_with()
        restore.assert_called_once_with(record)
        self.assertEqual(write_receipt.call_args.args[0]["state"], "failed")

    def test_apply_passes_all_affected_dates_to_projection_rebuild(self):
        args = SimpleNamespace(audit_limit=100, max_pages_per_chat=5, max_minutes=5)
        record = MagicMock(label="test.agent", plist_path=Path("/tmp/test.plist"))
        status = MagicMock(loaded=False)
        audit = [{"name": "Chat", "username": "chat", "count": 1, "capped": False}]
        drain_result = {
            "complete": ["chat"],
            "blocked": {},
            "pages": {"chat": 1},
            "statuses": {"notified": 1},
            "affected_dates": ["2026-08-02", "2026-08-03"],
        }
        projection_result = {
            "indexes": {"written_count": 2},
            "digests": [{
                "date": "2026-08-02",
                "notes": 1,
                "actions": 4,
                "path": "/tmp/digest.md",
            }],
        }
        validation = {"ok": True, "quick_check": "ok", "integrity_check": "ok"}

        with (
            patch("scripts.catch_up_monitor.audit_pending", return_value=audit),
            patch("scripts.catch_up_monitor.launch_agent_report", return_value=(record, status)),
            patch("scripts.catch_up_monitor.backup_runtime_state", return_value=Path("/tmp/backup")),
            patch("scripts.catch_up_monitor.TopicMonitor"),
            patch("scripts.catch_up_monitor.drain_monitors", return_value=drain_result),
            patch("scripts.catch_up_monitor.rebuild_projections", return_value=projection_result) as rebuild,
            patch("scripts.catch_up_monitor.validate_knowledge_db", return_value=validation),
            patch("scripts.catch_up_monitor.write_reconciliation_receipt") as write_receipt,
        ):
            result = apply_catch_up(
                {},
                [{"username": "chat", "name": "Chat"}],
                FakeDB([]),
                args,
            )

        self.assertEqual(result, 0)
        self.instance_lock.release.assert_called_once_with()
        rebuild.assert_called_once_with({}, ["2026-08-02", "2026-08-03"])
        self.assertEqual(write_receipt.call_args.args[0]["state"], "complete")

    def test_apply_records_resumable_partial_commit_and_restores_agent(self):
        args = SimpleNamespace(audit_limit=100, max_pages_per_chat=5, max_minutes=5)
        record = MagicMock(label="test.agent", plist_path=Path("/tmp/test.plist"))
        status = MagicMock(loaded=True)
        chats = [
            {"username": "done", "name": "Done"},
            {"username": "blocked", "name": "Blocked"},
        ]
        audit = [
            {"name": "Done", "username": "done", "checkpoint": 10, "count": 2, "capped": False},
            {"name": "Blocked", "username": "blocked", "checkpoint": 20, "count": 2, "capped": False},
        ]
        drain_result = {
            "complete": ["done"],
            "blocked": {"blocked": "ai_backoff"},
            "pages": {"done": 1},
            "statuses": {"notified": 1, "ai_backoff": 1},
            "affected_dates": ["2026-08-03"],
            "per_chat": {
                "done": {
                    "pages": 1,
                    "statuses": {"notified": 1, "no_messages": 1},
                    "event_ids": [101],
                    "affected_dates": ["2026-08-03"],
                    "outcome": "complete",
                    "blocked_reason": "",
                },
                "blocked": {
                    "pages": 0,
                    "statuses": {"ai_backoff": 1},
                    "event_ids": [],
                    "affected_dates": [],
                    "outcome": "blocked",
                    "blocked_reason": "ai_backoff",
                },
            },
        }
        validation = {"ok": True, "quick_check": "ok", "integrity_check": "ok"}

        with (
            patch("scripts.catch_up_monitor.audit_pending", return_value=audit),
            patch("scripts.catch_up_monitor.launch_agent_report", return_value=(record, status)),
            patch("scripts.catch_up_monitor._stop_launch_agent"),
            patch("scripts.catch_up_monitor._restore_launch_agent") as restore,
            patch("scripts.catch_up_monitor.backup_runtime_state", return_value=Path("/tmp/backup")),
            patch("scripts.catch_up_monitor.TopicMonitor"),
            patch("scripts.catch_up_monitor.drain_monitors", return_value=drain_result),
            patch("scripts.catch_up_monitor.rebuild_projections", return_value={"indexes": {}, "digests": []}),
            patch("scripts.catch_up_monitor.validate_knowledge_db", return_value=validation),
            patch("scripts.catch_up_monitor.write_reconciliation_receipt") as write_receipt,
        ):
            result = apply_catch_up({}, chats, FakeDB([]), args)

        self.assertEqual(result, 1)
        self.instance_lock.release.assert_called_once_with()
        restore.assert_called_once_with(record)
        receipt = write_receipt.call_args.args[0]
        self.assertEqual(receipt["state"], "partial")
        self.assertEqual(receipt["outcome"], "resume_required")
        self.assertTrue(receipt["resume_supported"])
        self.assertEqual(receipt["canonical"]["event_ids"], [101])

    def test_zero_backlog_writes_complete_noop_receipt_without_launch_switch(self):
        args = SimpleNamespace(audit_limit=100, max_pages_per_chat=5, max_minutes=5)
        audit = [{"name": "Chat", "username": "chat", "checkpoint": 10, "count": 0, "capped": False}]

        with (
            patch("scripts.catch_up_monitor.audit_pending", return_value=audit),
            patch("scripts.catch_up_monitor.launch_agent_report") as launch_report,
            patch("scripts.catch_up_monitor.write_reconciliation_receipt") as write_receipt,
        ):
            result = apply_catch_up(
                {},
                [{"username": "chat", "name": "Chat"}],
                FakeDB([]),
                args,
            )

        self.assertEqual(result, 0)
        launch_report.assert_not_called()
        receipt = write_receipt.call_args.args[0]
        self.assertEqual(receipt["state"], "complete")
        self.assertEqual(receipt["outcome"], "no_op")

    def test_apply_with_active_menu_app_performs_no_canonical_writes(self):
        args = SimpleNamespace(audit_limit=100, max_pages_per_chat=5, max_minutes=5)
        record = MagicMock(label="test.agent", plist_path=Path("/tmp/test.plist"))
        status = MagicMock(loaded=True)
        chats = [{"username": "private-chat", "name": "Private Chat"}]
        audit = [{
            "name": "Private Chat",
            "username": "private-chat",
            "checkpoint": 10,
            "count": 1,
            "capped": False,
        }]
        self.instance_lock.acquire.side_effect = AppAlreadyRunning("menu_app_already_running")

        with (
            patch("scripts.catch_up_monitor.audit_pending", return_value=audit),
            patch("scripts.catch_up_monitor.launch_agent_report", return_value=(record, status)),
            patch("scripts.catch_up_monitor._stop_launch_agent"),
            patch("scripts.catch_up_monitor._restore_launch_agent") as restore,
            patch("scripts.catch_up_monitor.backup_runtime_state") as backup,
            patch("scripts.catch_up_monitor.TopicMonitor") as topic_monitor,
            patch("scripts.catch_up_monitor.drain_monitors") as drain,
            patch("scripts.catch_up_monitor.rebuild_projections") as rebuild,
            patch("scripts.catch_up_monitor.validate_knowledge_db") as validate,
            patch("scripts.catch_up_monitor.write_reconciliation_receipt") as write_receipt,
        ):
            result = apply_catch_up({}, chats, FakeDB([]), args)

        self.assertEqual(result, 1)
        backup.assert_not_called()
        topic_monitor.assert_not_called()
        drain.assert_not_called()
        rebuild.assert_not_called()
        validate.assert_not_called()
        self.instance_lock.release.assert_not_called()
        restore.assert_called_once_with(record)
        receipt = write_receipt.call_args.args[0]
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["outcome"], "menu_app_active")
        self.assertNotIn("Private Chat", str(receipt))
        self.assertNotIn("private-chat", str(receipt))

    def test_apply_holds_singleton_through_receipt_then_restores_agent(self):
        args = SimpleNamespace(audit_limit=100, max_pages_per_chat=5, max_minutes=5)
        record = MagicMock(label="test.agent", plist_path=Path("/tmp/test.plist"))
        status = MagicMock(loaded=True)
        chats = [{"username": "chat", "name": "Chat"}]
        audit = [{
            "name": "Chat",
            "username": "chat",
            "checkpoint": 10,
            "count": 1,
            "capped": False,
        }]
        drain_result = {
            "complete": ["chat"],
            "blocked": {},
            "pages": {"chat": 1},
            "statuses": {"no_match": 1},
            "affected_dates": [],
        }
        events = []

        class RecordingLock:
            def acquire(self):
                events.append("lock_acquired")
                return self

            def release(self):
                events.append("lock_released")

        with (
            patch("scripts.catch_up_monitor.AppInstanceLock", return_value=RecordingLock()),
            patch("scripts.catch_up_monitor.audit_pending", return_value=audit),
            patch("scripts.catch_up_monitor.launch_agent_report", return_value=(record, status)),
            patch("scripts.catch_up_monitor._stop_launch_agent", side_effect=lambda _record: events.append("agent_stopped")),
            patch("scripts.catch_up_monitor.backup_runtime_state", side_effect=lambda *_args: events.append("backup") or Path("/tmp/backup")),
            patch("scripts.catch_up_monitor.TopicMonitor"),
            patch("scripts.catch_up_monitor.drain_monitors", side_effect=lambda *_args, **_kwargs: events.append("drain") or drain_result),
            patch("scripts.catch_up_monitor.rebuild_projections", side_effect=lambda *_args: events.append("projections") or {"indexes": {}, "digests": []}),
            patch("scripts.catch_up_monitor.validate_knowledge_db", side_effect=lambda *_args: events.append("validation") or {"ok": True}),
            patch(
                "scripts.catch_up_monitor._checkpoint_for_chat",
                return_value=11,
            ) as checkpoint,
            patch(
                "scripts.catch_up_monitor.write_reconciliation_receipt",
                side_effect=lambda receipt: events.append(
                    f"receipt:{receipt['outcome']}"
                ) or Path("/tmp/receipt.json"),
            ),
            patch("scripts.catch_up_monitor._restore_launch_agent", side_effect=lambda _record: events.append("agent_restored")),
        ):
            result = apply_catch_up({}, chats, FakeDB([]), args)

        self.assertEqual(result, 0)
        checkpoint.assert_called_once_with("chat")
        self.assertEqual(events, [
            "agent_stopped",
            "lock_acquired",
            "backup",
            "drain",
            "projections",
            "validation",
            "receipt:drain_complete_restore_pending",
            "lock_released",
            "agent_restored",
            "receipt:drained",
        ])

    def test_restore_failure_finalizes_durable_receipt_as_partial(self):
        args = SimpleNamespace(audit_limit=100, max_pages_per_chat=5, max_minutes=5)
        record = MagicMock(label="test.agent", plist_path=Path("/tmp/test.plist"))
        status = MagicMock(loaded=True)
        chats = [{"username": "chat", "name": "Chat"}]
        audit = [{
            "name": "Chat",
            "username": "chat",
            "checkpoint": 10,
            "count": 1,
            "capped": False,
        }]
        drain_result = {
            "complete": ["chat"],
            "blocked": {},
            "pages": {"chat": 1},
            "statuses": {"no_match": 1},
            "affected_dates": [],
            "per_chat": {
                "chat": {
                    "pages": 1,
                    "statuses": {"no_match": 1},
                    "event_ids": [],
                    "affected_dates": [],
                    "outcome": "complete",
                    "blocked_reason": "",
                },
            },
        }
        validation = {"ok": True, "quick_check": "ok", "integrity_check": "ok"}

        with tempfile.TemporaryDirectory() as tmp:
            written = []

            def durable_write(receipt):
                written.append(json.loads(json.dumps(receipt)))
                return write_reconciliation_receipt(receipt, receipts_dir=tmp)

            with (
                patch("scripts.catch_up_monitor.audit_pending", return_value=audit),
                patch(
                    "scripts.catch_up_monitor.launch_agent_report",
                    return_value=(record, status),
                ),
                patch("scripts.catch_up_monitor._stop_launch_agent"),
                patch(
                    "scripts.catch_up_monitor._restore_launch_agent",
                    side_effect=RuntimeError("bootstrap failed"),
                ),
                patch(
                    "scripts.catch_up_monitor._checkpoint_for_chat",
                    return_value=11,
                ),
                patch(
                    "scripts.catch_up_monitor.backup_runtime_state",
                    return_value=Path("/tmp/backup"),
                ),
                patch("scripts.catch_up_monitor.TopicMonitor"),
                patch(
                    "scripts.catch_up_monitor.drain_monitors",
                    return_value=drain_result,
                ),
                patch(
                    "scripts.catch_up_monitor.rebuild_projections",
                    return_value={"indexes": {}, "digests": []},
                ),
                patch(
                    "scripts.catch_up_monitor.validate_knowledge_db",
                    return_value=validation,
                ),
                patch(
                    "scripts.catch_up_monitor.write_reconciliation_receipt",
                    side_effect=durable_write,
                ),
            ):
                result = apply_catch_up({}, chats, FakeDB([]), args)

            self.assertEqual(result, 1)
            self.assertEqual(
                [item["outcome"] for item in written],
                ["drain_complete_restore_pending", "launch_agent_restore_failed"],
            )
            receipt_files = list(Path(tmp).glob("*.json"))
            self.assertEqual(len(receipt_files), 1)
            receipt = json.loads(receipt_files[0].read_text(encoding="utf-8"))
            self.assertEqual(receipt["state"], "partial")
            self.assertEqual(receipt["outcome"], "launch_agent_restore_failed")
            self.assertTrue(receipt["launch_agent"]["restore_attempted"])
            self.assertFalse(receipt["launch_agent"]["restored"])
            self.assertEqual(receipt["launch_agent"]["error"], "RuntimeError")
            self.assertEqual(receipt["canonical"]["affected_dates"], [])


if __name__ == "__main__":
    unittest.main()
