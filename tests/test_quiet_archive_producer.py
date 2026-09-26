import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, redirect_stdout
from unittest.mock import patch

from core.quiet_archive_producer import PROFILE_SCHEMA, ProducerProfile, ProducerError
from core.quiet_archive_handoff import QuietArchiveHandoff
from core.resource_capture import ResourceCaptureError, resource_capture_operation_lock
from core.source_adapter import SourceUnavailableError
from scripts import quiet_archive_handoff as cli
from tests.test_quiet_archive_handoff import message
from tests.test_resource_backup import KeysetSource


def _synthetic_profile_value(root):
    return {
        "schema": PROFILE_SCHEMA,
        "source": {"db_dir": str(root / "wechat/db_storage"),
                   "keys_file": str(root / "keys.json")},
        "state_dir": str(root / "producer"), "target": str(root / "handoff"),
        "chats": [{"username": "synthetic@chatroom", "alias": "Synthetic chat",
                   "selection_id": "00000000-0000-0000-0000-000000000001", "selected_since": 1}],
        "budget": {"max_rounds": 1, "max_seconds": 30, "page_size": 2, "min_free_bytes": 0},
    }


def _write_synthetic_source(value):
    path = Path(value["source"]["db_dir"]) / "message/message_0.db"
    path.parent.mkdir(parents=True)
    table = "Msg_" + hashlib.md5(value["chats"][0]["username"].encode()).hexdigest()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f"CREATE TABLE [{table}] (local_type INTEGER, create_time INTEGER, "
                     "message_content TEXT, WCDB_CT_message_content INTEGER, status INTEGER)")
        for index in range(3):
            conn.execute(f"INSERT INTO [{table}] VALUES (1, 100, ?, 0, 0)",
                         (f"sender:\nSynthetic message {index} "
                          f"https://example.org/synthetic/{index}?part=overview#details",))
    Path(value["source"]["keys_file"]).write_text("{}")
    Path(value["source"]["keys_file"]).chmod(0o600)


def write_synthetic_producer_fixture(root):
    """Create a new, caller-owned fixture directory and return its profile Path.

    Contains only three synthetic messages/bookmarks and an empty key map for a
    plaintext SQLite source. Call the real CLI's init and refresh separately.
    """
    root = Path(root).absolute()
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    value = _synthetic_profile_value(root)
    (root / "handoff").mkdir(mode=0o700)
    profile_file = root / "profile.json"
    profile_file.write_text(json.dumps(value), encoding="utf-8")
    profile_file.chmod(0o600)
    _write_synthetic_source(value)
    return profile_file


class StandaloneProducerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile_file = self.root / "profile.json"
        self.username = "synthetic@chatroom"
        self.value = _synthetic_profile_value(self.root)
        (self.root / "handoff").mkdir()
        self.write_profile()
        self.traps = ExitStack()
        self.addCleanup(self.traps.close)
        for target in ("scripts.quiet_archive_handoff.load_config", "scripts.quiet_archive_handoff.update_config",
                       "scripts.quiet_archive_handoff._source", "core.resource_capture.load_config",
                       "core.config.load_config", "core.key_extractor.get_cached_keys",
                       "core.attachment_archive.AttachmentArchive._connect",
                       "core.knowledge.KnowledgeStore.__init__"):
            self.traps.enter_context(patch(target, side_effect=AssertionError("default runtime used: " + target)))

    def write_profile(self):
        self.profile_file.write_text(json.dumps(self.value), encoding="utf-8")
        self.profile_file.chmod(0o600)
        self.profile = ProducerProfile.read(str(self.profile_file))

    def run_cli(self, *arguments, source=None):
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            if source is not None:
                stack.enter_context(patch.object(ProducerProfile, "source", return_value=source))
            code = cli.main(["--profile", str(self.profile_file), *arguments])
        return code, json.loads(output.getvalue())

    def initialize(self):
        code, result = self.run_cli("init")
        self.assertEqual((code, result["state"]), (0, "initialized"))
        return result

    def actual_source(self):
        _write_synthetic_source(self.value)

    def snapshot_contexts(self, result):
        path = Path(self.value["target"]) / "wgo-resource-backup/v3/snapshots" / result["snapshot_id"]
        coverage = json.loads((path / "coverage.json").read_text())
        self.assertEqual(coverage["capture_run_id"], result["coverage"]["capture_run_id"])
        return [json.loads(row) for row in (path / "contexts.jsonl").read_text().splitlines()]

    def test_plan_status_init_never_read_source_or_keys_and_refresh_needs_init(self):
        with (patch.object(ProducerProfile, "source", side_effect=AssertionError("source")),
              patch("core.key_extractor.read_keys_file", side_effect=AssertionError("keys"))):
            for command in ("plan", "status"):
                code, result = self.run_cli(command)
                self.assertEqual(code, 0)
                self.assertFalse(result["source_read"])
            code, result = self.run_cli("refresh", "--allow-transient-wechat-source-read")
            self.assertEqual((code, result["error_code"]), (2, "producer_not_initialized"))
            self.initialize()
            code, result = self.run_cli("status")
            self.assertEqual(result["coverage"]["source_scan"]["state"], "not_started")
        self.assertFalse(Path(self.value["source"]["db_dir"]).exists())
        self.assertFalse(Path(self.value["source"]["keys_file"]).exists())
        self.assertFalse((Path(self.profile.state_dir) / "unused-knowledge.db").exists())

    def test_ungranted_refresh_does_not_even_read_profile(self):
        with patch.object(ProducerProfile, "read", side_effect=AssertionError("profile")):
            code, result = self.run_cli("refresh")
        self.assertEqual((code, result["error_code"]), (2, "allow_transient_wechat_source_read_required"))

    def test_separate_cli_process_never_initializes_the_default_wgo_runtime(self):
        fixture_profile = write_synthetic_producer_fixture(self.root / "cli-fixture")
        environment = dict(os.environ)
        forbidden = self.root / "wgo-runtime-must-not-exist"
        environment["WE_GROUPCHAT_OBSIDIAN_DATA_DIR"] = str(forbidden)
        for arguments in (("plan",), ("init",),
                          ("refresh", "--allow-transient-wechat-source-read", "--max-rounds", "3")):
            result = subprocess.run(
                [sys.executable, str(Path(cli.__file__)), "--profile", str(fixture_profile), *arguments],
                env=environment, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIsInstance(json.loads(result.stdout), dict)
        self.assertFalse(forbidden.exists())

    def test_real_reader_headless_pending_restart_eof_and_stable_context_ids(self):
        self.actual_source()
        self.initialize()
        flag = "--allow-transient-wechat-source-read"
        code, first = self.run_cli("refresh", flag)
        self.assertEqual((code, first["state"]), (2, "pending"))
        self.assertEqual(len(self.snapshot_contexts(first)), 2)
        code, second = self.run_cli("refresh", flag)
        self.assertEqual((code, second["state"]), (0, "eof"))
        rows = self.snapshot_contexts(second)
        self.assertEqual(len(rows), 3)
        code, third = self.run_cli("refresh", flag)
        self.assertEqual((code, third["state"]), (0, "eof"))
        self.assertEqual([row["event_id"] for row in rows],
                         [row["event_id"] for row in self.snapshot_contexts(third)])
        self.assertTrue(Path(self.profile.paths()["inventory"]).is_file())
        self.assertTrue(Path(self.profile.paths()["cache"]).is_dir())
        self.assertFalse((Path(self.profile.state_dir) / "unused-knowledge.db").exists())

    def test_injected_cache_root_preserves_source_and_message_identity(self):
        from core.wechat_db import WeChatDB
        from core.source_inventory import SourceInventoryStore
        self.actual_source()
        self.initialize()
        original_cache_root = WeChatDB.CACHE_DIR
        with patch.object(WeChatDB, "CACHE_DIR", str(self.root / "synthetic-default-cache")):
            legacy = WeChatDB(self.value["source"]["db_dir"], {},
                              source_inventory_store=SourceInventoryStore(path=None))
            independent = self.profile.source()
            self.assertNotEqual(legacy.cache_dir, independent.cache_dir)
            self.assertEqual(legacy.cache_namespace, independent.cache_namespace)
            self.assertEqual(legacy.source_namespace, independent.source_namespace)
            self.assertEqual(legacy._source_message_shard_identity("message/message_0.db"),
                             independent._source_message_shard_identity("message/message_0.db"))
        self.assertEqual(WeChatDB.CACHE_DIR, original_cache_root)

    def test_interruption_commits_only_completed_pages_then_resumes(self):
        self.initialize()
        source = KeysetSource({"shard": [message(index) for index in range(3)]})
        original = source.get_cursor_page_for_shard
        calls = []
        def interrupted(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt()
            return original(*args, **kwargs)
        with patch.object(source, "get_cursor_page_for_shard", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_cli("refresh", "--allow-transient-wechat-source-read", "--max-rounds", "3", source=source)
        self.assertEqual(len(self.profile.capture().contexts()), 2)
        code, result = self.run_cli("refresh", "--allow-transient-wechat-source-read", source=source)
        self.assertEqual((code, result["state"]), (0, "eof"))
        self.assertEqual(len(self.snapshot_contexts(result)), 3)

    def test_failed_source_and_busy_never_relabel_old_snapshot_as_eof(self):
        self.initialize()
        source = KeysetSource({"shard": [message(1)]})
        flag = "--allow-transient-wechat-source-read"
        self.assertEqual(self.run_cli("refresh", flag, source=source)[0], 0)
        with patch.object(source, "get_cursor_page_for_shard",
                          side_effect=SourceUnavailableError("source_snapshot_failed")):
            code, result = self.run_cli("refresh", flag, source=source)
        self.assertEqual((code, result["state"], result["completed"]), (2, "failed", False))
        self.assertIn("source_snapshot_failed", result["coverage"]["errors"])
        with resource_capture_operation_lock(self.profile.config()):
            code, busy = self.run_cli("refresh", flag, source=source)
        self.assertEqual((code, busy["completed"], busy["error_code"]), (2, False, "capture_worker_busy"))
        self.assertIsNone(busy["snapshot_id"])

    def test_profile_selection_change_uses_existing_epoch_contract(self):
        self.initialize()
        source = KeysetSource({"shard": [message(1)]})
        flag = "--allow-transient-wechat-source-read"
        self.assertEqual(self.run_cli("refresh", flag, source=source)[0], 0)
        self.value["chats"][0].update(selection_id="00000000-0000-0000-0000-000000000002", selected_since=200)
        self.write_profile()
        self.assertEqual(self.run_cli("refresh", flag, source=source)[0], 0)
        with closing(sqlite3.connect(self.profile.paths()["ledger"])) as conn, conn:
            self.assertEqual(conn.execute("SELECT selection_epoch FROM resource_chats").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT cursor_timestamp FROM resource_shards").fetchone()[0], 200)
        self.value["chats"] = []
        self.write_profile()
        code, result = self.run_cli("export")
        self.assertEqual(code, 0)
        self.assertEqual(result["coverage"]["selection"], [])

    def test_explicit_file_resolution_needs_no_app_or_knowledge_database(self):
        self.initialize()
        content = b"synthetic attachment"
        source = KeysetSource({"shard": [message(1, resources=[{
            "kind": "file", "original_name": "fixture.txt", "declared_size": len(content),
            "declared_hash": hashlib.sha256(content).hexdigest()}])]})
        flag = "--allow-transient-wechat-source-read"
        code, refreshed = self.run_cli("refresh", flag, source=source)
        self.assertEqual((code, refreshed["coverage"]["files"]["pending_occurrences"]), (0, 1))
        occurrence = self.profile.capture().occurrences()[0]
        file_path = Path(self.value["source"]["db_dir"]).parent / "msg/file" / occurrence["source_month"] / "fixture.txt"
        file_path.parent.mkdir(parents=True)
        file_path.write_bytes(content)
        code, denied = self.run_cli("resolve-files", flag)
        self.assertEqual((code, denied["state"]), (2, "attachment_read_not_authorized"))
        with patch.object(ProducerProfile, "source", side_effect=AssertionError("message source/keys")):
            code, result = self.run_cli("resolve-files", flag, "--allow-attachment-read")
        self.assertEqual((code, result["resolve"]["ready_local"]), (0, 1))
        self.assertEqual(self.run_cli("export")[1]["coverage"]["files"]["pending_occurrences"], 0)
        self.assertFalse((Path(self.profile.state_dir) / "unused-knowledge.db").exists())

    def adoption_fixture(self):
        self.actual_source()
        self.initialize()
        self.assertEqual(self.run_cli("refresh", "--allow-transient-wechat-source-read", "--max-rounds", "3")[0], 0)
        old = self.profile
        capture = old.capture()
        capture.archive.ensure_layout()
        digest, size, relative = capture.archive.store_bytes(b"adopted attachment", "adopted.txt")
        chat = capture.selected_chats()[0]
        with closing(sqlite3.connect(old.paths()["ledger"])) as conn, conn:
            conn.execute("UPDATE resource_chats SET context_history_gap=1")
            # Preserve a ready file and a queued file, including their original row IDs.
            capture._insert_occurrences(conn, chat, [message(100, resources=[
                {"kind": "file", "original_name": "adopted.txt"},
                {"kind": "file", "original_name": "pending.txt"}])], 200)
            conn.execute("UPDATE resource_occurrences SET status='ready_local', object_sha256=?, "
                         "object_size=?, object_relpath=? WHERE resource_index=0", (digest, size, relative))
        self.value["state_dir"] = str(self.root / "adopted-producer")
        self.profile_file = self.root / "adopted-profile.json"
        self.write_profile()
        return old

    @staticmethod
    def dump(path):
        with closing(sqlite3.connect(path)) as conn:
            return list(conn.iterdump())

    def make_plan(self, old):
        plan = str(self.root / "adoption.json")
        code, result = self.run_cli("adopt-plan", "--from-ledger", old.paths()["ledger"],
                                    "--from-objects", old.paths()["objects"],
                                    "--from-inventory", old.paths()["inventory"], "--output", plan)
        self.assertEqual((code, result.get("state")), (0, "planned"), result)
        return plan, result

    def test_adoption_preserves_all_ledger_rows_objects_and_identity_without_following_source(self):
        old = self.adoption_fixture()
        before = self.dump(old.paths()["ledger"])
        old_contexts = QuietArchiveHandoff(old.config(), capture=old.capture())._context_records(old.capture().occurrences())
        plan, result = self.make_plan(old)
        self.assertEqual(self.dump(old.paths()["ledger"]), before)
        with closing(sqlite3.connect(old.paths()["ledger"])) as conn, conn:
            conn.execute("UPDATE resource_contexts SET text='Later synthetic source change'")
        later_source = self.dump(old.paths()["ledger"])
        code, applied = self.run_cli("adopt-apply", "--plan", plan)
        self.assertEqual((code, applied["archive_id"]), (0, old.capture().archive_id))
        self.assertEqual(self.dump(self.profile.paths()["ledger"]), before)
        adopted = self.profile.capture()
        self.assertEqual(QuietArchiveHandoff(self.profile.config(), capture=adopted)._context_records(adopted.occurrences()), old_contexts)
        self.assertTrue(adopted.context_coverage()["contexts"]["unmeasured_history_gap"])
        ready = next(row for row in adopted.occurrences() if row["status"] == "ready_local")
        self.assertEqual((Path(adopted.archive_root) / ready["object_relpath"]).read_bytes(), b"adopted attachment")
        self.assertEqual(self.run_cli("adopt-apply", "--plan", plan)[1]["state"], "already_applied")
        self.assertEqual(self.run_cli("refresh", "--allow-transient-wechat-source-read")[0], 0)
        self.assertEqual(self.dump(old.paths()["ledger"]), later_source)

    def test_adoption_respects_existing_capture_owner(self):
        old = self.adoption_fixture()
        plan = self.root / "adoption.json"
        with (resource_capture_operation_lock(old.config()),
              patch("core.resource_capture.time.sleep", side_effect=AssertionError("default must not wait"))):
            code, result = self.run_cli("adopt-plan", "--from-ledger", old.paths()["ledger"],
                                        "--from-objects", old.paths()["objects"],
                                        "--from-inventory", old.paths()["inventory"], "--output", str(plan))
        self.assertEqual((code, result["error_code"]), (2, "capture_worker_busy"))
        self.assertFalse(plan.exists())
        self.assertFalse(Path(str(plan) + ".payload").exists())

    def test_adoption_waits_and_freezes_under_the_same_capture_lock(self):
        from core import quiet_archive_producer as producer
        old = self.adoption_fixture()
        plan = self.root / "adoption.json"
        owned, release = threading.Event(), threading.Event()
        def hold_lock():
            with resource_capture_operation_lock(old.config()):
                owned.set()
                self.assertTrue(release.wait(5))
        sleep = time.sleep
        review = producer._ledger_review
        def release_while_waiting(seconds):
            release.set()
            sleep(seconds)
        def review_while_owned(*args):
            with self.assertRaisesRegex(ResourceCaptureError, "capture_worker_busy"):
                with resource_capture_operation_lock(old.config()):
                    self.fail("freeze released capture ownership")
            return review(*args)
        with ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(hold_lock)
            try:
                self.assertTrue(owned.wait(5))
                with (patch("core.resource_capture.time.sleep", side_effect=release_while_waiting) as waiting,
                      patch.object(producer, "_ledger_review", side_effect=review_while_owned),
                      patch.object(ProducerProfile, "source", side_effect=AssertionError("source read"))):
                    code, result = self.run_cli("adopt-plan", "--from-ledger", old.paths()["ledger"],
                                               "--from-objects", old.paths()["objects"],
                                               "--from-inventory", old.paths()["inventory"],
                                               "--output", str(plan), "--wait-seconds", "2")
                self.assertGreater(waiting.call_count, 0)
            finally:
                release.set()
            holder.result(timeout=5)
        self.assertEqual((code, result["state"]), (0, "planned"))
        self.assertTrue(plan.is_file())
        self.assertTrue(Path(str(plan) + ".payload/capture.db").is_file())
        self.assertFalse(Path(self.profile.state_dir).exists())
        with resource_capture_operation_lock(old.config()):
            pass

    def test_adoption_wait_timeout_creates_no_candidate_or_state(self):
        old = self.adoption_fixture()
        plan = self.root / "timeout/adoption.json"
        with resource_capture_operation_lock(old.config()):
            started = time.monotonic()
            code, result = self.run_cli("adopt-plan", "--from-ledger", old.paths()["ledger"],
                                       "--from-objects", old.paths()["objects"],
                                       "--from-inventory", old.paths()["inventory"],
                                       "--output", str(plan), "--wait-seconds", "0.08")
            self.assertGreaterEqual(time.monotonic() - started, 0.08)
        self.assertEqual((code, result["error_code"]), (2, "capture_worker_busy"))
        self.assertFalse(plan.parent.exists())
        self.assertFalse(Path(self.profile.state_dir).exists())

    def test_adoption_wait_budget_is_finite_nonnegative_and_bounded(self):
        old = self.adoption_fixture()
        plan = self.root / "invalid/adoption.json"
        for budget in ("-1", "nan", "inf", "301"):
            with self.subTest(budget=budget):
                code, result = self.run_cli("adopt-plan", "--from-ledger", old.paths()["ledger"],
                                           "--from-objects", old.paths()["objects"],
                                           "--output", str(plan), "--wait-seconds", budget)
                self.assertEqual((code, result["error_code"]), (2, "invalid_capture_lock_timeout"))
                self.assertFalse(plan.parent.exists())
                self.assertFalse(Path(self.profile.state_dir).exists())

    def test_adoption_requires_exact_selection_and_durable_inventory(self):
        old = self.adoption_fixture()
        args = ("adopt-plan", "--from-ledger", old.paths()["ledger"],
                "--from-objects", old.paths()["objects"], "--output", str(self.root / "adoption.json"))
        code, result = self.run_cli(*args)
        self.assertEqual((code, result["error_code"]), (2, "adoption_inventory_required"))
        self.value["chats"][0]["selected_since"] = 10
        self.write_profile()
        code, result = self.run_cli(*args)
        self.assertEqual((code, result["error_code"]), (2, "adoption_selection_mismatch"))
        self.assertEqual(result["required_chats"][0]["selected_since"], 1)
        self.assertFalse(Path(self.profile.state_dir).exists())

    def test_adoption_tamper_and_interruption_leave_target_uninitialized_and_plan_retryable(self):
        old = self.adoption_fixture()
        plan, _ = self.make_plan(old)
        staged = Path(plan + ".payload/capture.db")
        original = staged.read_bytes()
        with closing(sqlite3.connect(staged)) as conn, conn:
            conn.execute("UPDATE resource_chats SET context_history_gap=0")
        code, result = self.run_cli("adopt-apply", "--plan", plan)
        self.assertEqual((code, result["error_code"]), (2, "adoption_payload_changed"))
        self.assertFalse(Path(self.profile.state_dir).exists())
        staged.write_bytes(original)
        from core import quiet_archive_producer as producer
        copy = producer._copy_private
        calls = []
        def interrupted(*args):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt()
            return copy(*args)
        with patch.object(producer, "_copy_private", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_cli("adopt-apply", "--plan", plan)
        self.assertFalse(Path(self.profile.state_dir).exists())
        self.assertEqual(self.run_cli("adopt-apply", "--plan", plan)[1]["state"], "applied")


if __name__ == "__main__":
    unittest.main()
