import hashlib
import json
import multiprocessing
import os
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from core.attachment_archive import (
    ArchiveError,
    AttachmentArchive,
    Resolution,
    process_pending_from_config,
)
from core.image_decoder import V2_MAGIC
from core.knowledge import KnowledgeStore


def _hold_archive_partial(
    knowledge_db,
    archive_root,
    db_dir,
    ready_event,
    release_event,
    result_queue,
):
    archive = AttachmentArchive(
        knowledge_db,
        archive_root,
        db_dir=db_dir,
        archive_kinds=("file",),
        min_free_bytes=0,
    )
    try:
        with archive._worker_lock():
            partial = os.path.join(
                archive_root,
                "tmp",
                ".partial-live-writer",
            )
            with open(partial, "wb") as handle:
                handle.write(b"live private attachment bytes")
                handle.flush()
                os.fsync(handle.fileno())
            ready_event.set()
            release_event.wait(10)
            result_queue.put({"state": "released", "partial_exists": os.path.exists(partial)})
    except Exception as exc:  # pragma: no cover - returned to the parent process
        ready_event.set()
        result_queue.put({"state": type(exc).__name__, "error": str(exc)})


def _contend_archive_lock(
    knowledge_db,
    archive_root,
    db_dir,
    ready_event,
    result_queue,
):
    ready_event.wait(10)
    archive = AttachmentArchive(
        knowledge_db,
        archive_root,
        db_dir=db_dir,
        archive_kinds=("file",),
        min_free_bytes=0,
    )
    try:
        with archive._worker_lock():
            state = "acquired"
    except ArchiveError as exc:
        state = exc.code
    result_queue.put({
        "state": state,
        "partial_exists": os.path.exists(
            os.path.join(archive_root, "tmp", ".partial-live-writer")
        ),
    })


def candidate(title="Attachment fixture", topic_key="attachment-fixture"):
    return {
        "title": title,
        "summary": "Synthetic attachment archive fixture.",
        "topic_key": topic_key,
        "category": "技术方法",
        "entities": [],
        "key_facts": ["Synthetic fixture"],
        "links": [],
        "event_type": "resource",
        "status_hint": "tracking",
    }


class AttachmentArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "knowledge.db")
        self.obsidian_root = os.path.join(self.tmp.name, "obsidian")
        self.archive_root = os.path.join(self.tmp.name, "archive")
        self.db_dir = os.path.join(
            self.tmp.name,
            "xwechat_files",
            "wxid_fixture",
            "db_storage",
        )
        os.makedirs(self.db_dir)
        self.file_root = os.path.join(os.path.dirname(self.db_dir), "msg", "file")
        self.image_root = os.path.join(os.path.dirname(self.db_dir), "msg", "attach")
        self.config = {
            "monitor_chat_display_name": "Fixture chat",
            "monitor_chat_username": "room@chatroom",
            "monitor_obsidian_root": self.obsidian_root,
            "monitor_obsidian_subdir": "关注推送",
            "monitor_knowledge_db": self.db_path,
            "db_dir": self.db_dir,
            "attachment_archive_root": self.archive_root,
            "attachment_archive_kinds": ["file"],
        }
        self.store = KnowledgeStore(
            self.db_path,
            self.obsidian_root,
            "关注推送",
            now_func=lambda: 1000,
            attachment_archive_root=self.archive_root,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def archive(self, kinds=("file",), image_aes_key="", now_func=None, **kwargs):
        # Resolver/CAS tests must not depend on the host's current free space.
        # The dedicated object-policy test passes its own threshold explicitly.
        kwargs.setdefault("min_free_bytes", 0)
        return AttachmentArchive(
            self.db_path,
            self.archive_root,
            db_dir=self.db_dir,
            archive_kinds=kinds,
            image_aes_key=image_aes_key,
            obsidian_root=self.obsidian_root,
            obsidian_subdir="关注推送",
            now_func=now_func or (lambda: 2000),
            **kwargs,
        )

    def add_file_mention(
        self,
        name,
        *,
        content_text=None,
        declared_size=None,
        declared_hash="",
        topic_key=None,
    ):
        resource = {
            "kind": "file",
            "resource_index": 0,
            "original_name": name,
            "extension": os.path.splitext(name)[1].lstrip("."),
        }
        if declared_size is not None:
            resource["declared_size"] = declared_size
        if declared_hash:
            resource["declared_hash"] = declared_hash
        message = {
            "timestamp": 1_780_000_000,
            "time_str": "2026-05-29 03:16",
            "sender": "Fixture sender",
            "text": content_text or f"[文件] {name}",
            "source_message_id": "wgmsg_" + hashlib.sha256(
                f"{topic_key or name}\0{name}".encode()
            ).hexdigest()[:32],
            "resources": [resource],
        }
        return self.store.apply_event(
            candidate(name, topic_key or "file-" + hashlib.md5(name.encode()).hexdigest()),
            [message],
            self.config,
            {"relation": "new"},
        )

    def add_image_mention(self, image_hash, *, topic_key="image-fixture"):
        message = {
            "timestamp": 1_780_000_000,
            "time_str": "2026-05-29 03:16",
            "sender": "Fixture sender",
            "text": "[图片]",
            "source_message_id": "wgmsg_" + topic_key,
            "resources": [
                {
                    "kind": "image",
                    "resource_index": 0,
                    "declared_hash": image_hash,
                }
            ],
        }
        return self.store.apply_event(
            candidate("Image fixture", topic_key),
            [message],
            self.config,
            {"relation": "new"},
        )

    def write_cache_file(self, name, data, month="2026-05"):
        directory = os.path.join(self.file_root, month)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "wb") as file:
            file.write(data)
        return path

    def image_dir(self, month="2026-05"):
        chat_hash = hashlib.md5(b"room@chatroom").hexdigest()
        directory = os.path.join(self.image_root, chat_hash, month, "Img")
        os.makedirs(directory, exist_ok=True)
        return directory

    def rows(self, query, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(query, params)]
        finally:
            conn.close()

    def resource_body(self, markdown):
        return markdown.split("## 资源", 1)[-1]

    def test_exact_file_archives_private_object_and_updates_markdown(self):
        data = b"canonical attachment bytes"
        digest = hashlib.sha256(data).hexdigest()
        result = self.add_file_mention(
            "source reliability.txt",
            declared_size=len(data),
            declared_hash=digest,
        )
        self.write_cache_file("source reliability.txt", data)

        outcome = self.archive().process_pending()

        self.assertEqual(outcome, {"state": "healthy", "processed": 1, "archived": 1, "failed": 0})
        mention = self.rows("SELECT * FROM attachment_mentions")[0]
        obj = self.rows("SELECT * FROM attachment_objects")[0]
        self.assertEqual(mention["status"], "original_archived")
        self.assertEqual(mention["resolution_method"], "unique_candidate")
        self.assertEqual(mention["object_sha256"], digest)
        object_path = os.path.join(self.archive_root, obj["object_relpath"])
        self.assertEqual(stat.S_IMODE(os.stat(self.archive_root).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(object_path).st_mode), 0o600)
        with open(result["knowledge_path"], encoding="utf-8") as note:
            markdown = note.read()
        archived_line = next(
            line
            for line in markdown.splitlines()
            if line.startswith("- ") and "source reliability.txt" in line
        )
        self.assertTrue(archived_line.startswith("- "))
        self.assertIn("归档状态 original_archived", archived_line)
        self.assertIn("[本地归档](file://", archived_line)
        self.assertFalse(
            any(
                line.startswith("  - ")
                for line in self.resource_body(markdown).splitlines()
            )
        )
        self.assertNotIn("  - 归档状态", markdown)
        self.assertNotIn("  - 本地归档", markdown)

    def test_cjk_object_name_stays_within_filesystem_byte_limit(self):
        name = "猫" * 80 + ".pdf"
        data = b"cjk filename fixture"
        self.add_file_mention(name, declared_size=len(data))
        self.write_cache_file(name, data)

        outcome = self.archive().process_pending()

        self.assertEqual(outcome["archived"], 1)
        obj = self.rows("SELECT * FROM attachment_objects")[0]
        leaf = os.path.basename(obj["object_relpath"])
        self.assertLessEqual(len(leaf.encode("utf-8")), 255)
        self.assertTrue(leaf.endswith(".pdf"))
        self.assertTrue(os.path.isfile(os.path.join(self.archive_root, obj["object_relpath"])))

    def test_identical_bytes_across_names_are_deduplicated(self):
        data = b"same bytes, different cache names"
        self.add_file_mention("alpha.txt", topic_key="dedup-alpha")
        self.add_file_mention("beta.txt", topic_key="dedup-beta")
        self.write_cache_file("alpha.txt", data)
        self.write_cache_file("beta.txt", data)

        outcome = self.archive().process_pending()

        self.assertEqual(outcome["archived"], 2)
        self.assertEqual(len(self.rows("SELECT * FROM attachment_objects")), 1)
        digests = {
            row["object_sha256"]
            for row in self.rows("SELECT object_sha256 FROM attachment_mentions")
        }
        self.assertEqual(digests, {hashlib.sha256(data).hexdigest()})

    def test_equivalent_duplicate_variants_are_safe_but_distinct_variants_are_ambiguous(self):
        same = b"same duplicate"
        self.add_file_mention("same.pdf", topic_key="same-duplicates")
        self.write_cache_file("same.pdf", same)
        self.write_cache_file("same (1).pdf", same)
        outcome = self.archive().process_pending()
        self.assertEqual(outcome["archived"], 1)
        mention = self.rows("SELECT * FROM attachment_mentions")[0]
        self.assertEqual(mention["resolution_method"], "equivalent_duplicates")

        self.add_file_mention("different.pdf", topic_key="different-duplicates")
        self.write_cache_file("different.pdf", b"version A")
        self.write_cache_file("different (1).pdf", b"version B")
        outcome = self.archive().process_pending()
        self.assertEqual(outcome["failed"], 1)
        mention = self.rows(
            "SELECT * FROM attachment_mentions WHERE original_name = 'different.pdf'"
        )[0]
        self.assertEqual(mention["status"], "ambiguous")
        self.assertEqual(mention["object_sha256"], "")

    def test_metadata_filter_and_missing_are_retryable_without_guessing(self):
        self.add_file_mention(
            "wrong.txt",
            declared_size=4,
            declared_hash=hashlib.sha256(b"good").hexdigest(),
        )
        self.write_cache_file("wrong.txt", b"bad!")
        outcome = self.archive().process_pending()
        self.assertEqual(outcome["failed"], 1)
        mention = self.rows("SELECT * FROM attachment_mentions")[0]
        self.assertEqual(mention["status"], "missing_retryable")
        self.assertEqual(mention["resolution_method"], "file_metadata_mismatch")

    def test_symlink_candidate_is_rejected(self):
        self.add_file_mention("linked.txt")
        directory = os.path.join(self.file_root, "2026-05")
        os.makedirs(directory, exist_ok=True)
        outside = os.path.join(self.tmp.name, "outside.txt")
        with open(outside, "wb") as file:
            file.write(b"outside")
        os.symlink(outside, os.path.join(directory, "linked.txt"))

        self.archive().process_pending()

        mention = self.rows("SELECT * FROM attachment_mentions")[0]
        self.assertEqual(mention["status"], "source_rejected")
        self.assertEqual(self.rows("SELECT * FROM attachment_objects"), [])

    def test_copy_failure_does_not_rollback_event_or_requeue_ai_work(self):
        result = self.add_file_mention("durable-event.txt")
        self.write_cache_file("durable-event.txt", b"fixture")
        archive = self.archive()

        with patch.object(archive, "store_source", side_effect=ArchiveError("source_changed")):
            outcome = archive.process_pending()

        self.assertEqual(outcome["failed"], 1)
        self.assertEqual(len(self.rows("SELECT * FROM events WHERE event_id = ?", (result["event_id"],))), 1)
        mention = self.rows("SELECT * FROM attachment_mentions")[0]
        self.assertEqual(mention["status"], "source_changed")

    def test_original_and_thumbnail_image_statuses_are_explicit(self):
        image_hash = "a" * 32
        original_result = self.add_image_mention(image_hash, topic_key="image-original")
        with open(os.path.join(self.image_dir(), image_hash + "_h.dat"), "wb") as image:
            image.write(b"\xff\xd8" + b"original image bytes")

        outcome = self.archive(("image",)).process_pending()
        self.assertEqual(outcome["archived"], 1)
        mention = self.rows("SELECT * FROM attachment_mentions")[0]
        self.assertEqual(mention["status"], "original_archived")
        with open(original_result["knowledge_path"], encoding="utf-8") as note:
            markdown = note.read()
        self.assertIn("### 附件归档", markdown)
        self.assertIn("图片附件", markdown)
        image_line = next(
            line
            for line in markdown.splitlines()
            if line.startswith("- ") and "图片附件" in line
        )
        self.assertTrue(image_line.startswith("- "))
        self.assertIn("归档状态 original_archived", image_line)
        self.assertIn("[本地归档](file://", image_line)
        self.assertIn("来源月份 2026-05", image_line)
        self.assertFalse(
            any(
                line.startswith("  - ")
                for line in self.resource_body(markdown).splitlines()
            )
        )
        self.assertNotIn("  - 归档状态", markdown)
        self.assertNotIn("  - 来源月份", markdown)

        thumb_hash = "b" * 32
        self.add_image_mention(thumb_hash, topic_key="image-thumb")
        with open(os.path.join(self.image_dir(), thumb_hash + "_t.dat"), "wb") as image:
            image.write(b"\x89PNG" + b"thumbnail bytes")
        outcome = self.archive(("image",)).process_pending()
        self.assertEqual(outcome["archived"], 1)
        thumb = self.rows(
            "SELECT * FROM attachment_mentions WHERE declared_hash = ?",
            (thumb_hash,),
        )[0]
        self.assertEqual(thumb["status"], "thumbnail_only")

    def test_image_decode_unavailable_and_no_mtime_fallback(self):
        image_hash = "c" * 32
        self.add_image_mention(image_hash, topic_key="image-v2-no-key")
        with open(os.path.join(self.image_dir(), image_hash + "_h.dat"), "wb") as image:
            image.write(V2_MAGIC + b"\x00" * 32)
        outcome = self.archive(("image",)).process_pending()
        self.assertEqual(outcome["failed"], 1)
        mention = self.rows("SELECT * FROM attachment_mentions")[0]
        self.assertEqual(mention["status"], "decode_unavailable")

        missing_hash = "d" * 32
        self.add_image_mention(missing_hash, topic_key="image-no-mtime-fallback")
        unrelated = "e" * 32
        with open(os.path.join(self.image_dir(), unrelated + "_h.dat"), "wb") as image:
            image.write(b"\xff\xd8unrelated")
        self.archive(("image",)).process_pending()
        missing = self.rows(
            "SELECT * FROM attachment_mentions WHERE declared_hash = ?",
            (missing_hash,),
        )[0]
        self.assertEqual(missing["status"], "missing_retryable")
        self.assertEqual(missing["object_sha256"], "")

    def test_backfill_plan_is_read_only_and_apply_is_explicit(self):
        result = self.add_file_mention("historical.txt")
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM attachment_mentions WHERE event_id = ?", (result["event_id"],))
        conn.commit()
        conn.close()
        archive = self.archive()

        plan = archive.plan_backfill()

        self.assertEqual(plan["new_mentions"], 1)
        self.assertEqual(self.rows("SELECT * FROM attachment_mentions"), [])
        self.assertEqual(archive.apply_backfill(), 1)
        mention = self.rows("SELECT * FROM attachment_mentions")[0]
        self.assertTrue(mention["source_message_id"].startswith("wgbackfill_"))
        self.assertEqual(mention["status"], "pending")

    def test_retry_requires_explicit_selection_of_failed_rows(self):
        self.add_file_mention("missing.txt")
        archive = self.archive()
        archive.process_pending()
        mention = self.rows("SELECT * FROM attachment_mentions")[0]
        self.assertEqual(mention["status"], "missing_retryable")

        self.assertEqual(archive.retry(mention_ids=[mention["mention_id"]]), 1)
        retried = self.rows("SELECT * FROM attachment_mentions")[0]
        self.assertEqual(retried["status"], "pending")
        self.assertEqual(retried["attempt_count"], 0)
        self.assertEqual(retried["next_retry_at"], 0)

    def test_retry_is_due_only_and_uses_exponential_backoff(self):
        clock = [1000.0]
        self.add_file_mention("eventually-present.txt")
        archive = self.archive(
            now_func=lambda: clock[0],
            retry_base_seconds=10,
            retry_max_seconds=40,
        )

        first = archive.process_pending()
        mention = self.rows("SELECT * FROM attachment_mentions")[0]
        self.assertEqual(first["processed"], 1)
        self.assertEqual(mention["attempt_count"], 1)
        self.assertEqual(mention["next_retry_at"], 1010)

        clock[0] = 1009
        self.assertEqual(archive.process_pending()["processed"], 0)
        clock[0] = 1010
        self.assertEqual(archive.process_pending()["processed"], 1)
        mention = self.rows("SELECT * FROM attachment_mentions")[0]
        self.assertEqual(mention["attempt_count"], 2)
        self.assertEqual(mention["next_retry_at"], 1030)

    def test_fresh_pending_rows_are_not_starved_by_due_retries(self):
        for index in range(4):
            self.add_file_mention(f"old-retry-{index}.txt")
        self.add_file_mention("fresh-pending.txt")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            UPDATE attachment_mentions
            SET status = 'missing_retryable', attempt_count = 3, next_retry_at = 1
            WHERE original_name LIKE 'old-retry-%'
            """
        )
        conn.commit()
        conn.close()
        archive = self.archive(retry_base_seconds=10, retry_max_seconds=40)
        order = []

        def unresolved(mention):
            order.append(mention["original_name"])
            return Resolution("missing_retryable", "fixture")

        with patch.object(archive, "resolve", side_effect=unresolved):
            outcome = archive.process_pending(limit=1)

        self.assertEqual(outcome["processed"], 5)
        self.assertEqual(order[0], "fresh-pending.txt")
        self.assertEqual(set(order[1:]), {f"old-retry-{index}.txt" for index in range(4)})

    def test_active_worker_coalesces_wake_and_drains_inserted_rows(self):
        self.add_file_mention("first.txt")
        archive = self.archive(retry_base_seconds=10, retry_max_seconds=40)
        nested_results = []
        seen = []

        def insert_during_first(mention):
            seen.append(mention["original_name"])
            if mention["original_name"] == "first.txt":
                self.add_file_mention("inserted-while-active.txt")
                nested_results.append(archive.process_pending(limit=1))
            return Resolution("missing_retryable", "fixture")

        with patch.object(archive, "resolve", side_effect=insert_during_first):
            outcome = archive.process_pending(limit=1)

        self.assertEqual(outcome["processed"], 2)
        self.assertEqual(seen, ["first.txt", "inserted-while-active.txt"])
        self.assertEqual(nested_results[0]["state"], "worker_busy")
        worker = self.rows(
            "SELECT wake_generation, drained_generation FROM attachment_worker_state"
        )[0]
        self.assertEqual(worker["wake_generation"], worker["drained_generation"])

    def test_final_wake_after_drain_recompetes_and_processes_inserted_row(self):
        self.add_file_mention("first.txt")
        archive = self.archive(retry_base_seconds=10, retry_max_seconds=40)
        original_mark_drained = archive._mark_drained
        nested_results = []
        fired = [False]

        def wake_after_drain(conn):
            drained = original_mark_drained(conn)
            if drained and not fired[0]:
                fired[0] = True
                self.add_file_mention("final-wake.txt")
                nested_results.append(archive.process_pending(limit=1))
            return drained

        with patch.object(archive, "_mark_drained", side_effect=wake_after_drain):
            outcome = archive.process_pending(limit=1)

        self.assertEqual(outcome["processed"], 2)
        self.assertEqual(nested_results[0]["state"], "worker_busy")
        mentions = self.rows("SELECT original_name, attempt_count FROM attachment_mentions")
        self.assertEqual({row["original_name"] for row in mentions}, {"first.txt", "final-wake.txt"})
        self.assertTrue(all(row["attempt_count"] == 1 for row in mentions))
        worker = self.rows(
            "SELECT wake_generation, drained_generation FROM attachment_worker_state"
        )[0]
        self.assertEqual(worker["wake_generation"], worker["drained_generation"])

    def test_object_size_and_free_space_policy_are_explicit(self):
        self.add_file_mention("too-large.bin", declared_size=5)
        too_large = self.archive(max_object_bytes=4, min_free_bytes=0).process_pending()
        self.assertEqual(too_large["failed"], 1)
        mention = self.rows(
            "SELECT * FROM attachment_mentions WHERE original_name = 'too-large.bin'"
        )[0]
        self.assertEqual(mention["status"], "object_too_large")
        self.assertEqual(mention["next_retry_at"], 0)

        self.add_file_mention("needs-space.bin")
        self.write_cache_file("needs-space.bin", b"four")
        archive = self.archive(max_object_bytes=100, min_free_bytes=100)
        usage = type("DiskUsage", (), {"free": 102})()
        with patch("core.attachment_archive.shutil.disk_usage", return_value=usage):
            low_space = archive.process_pending()
        self.assertEqual(low_space["failed"], 1)
        mention = self.rows(
            "SELECT * FROM attachment_mentions WHERE original_name = 'needs-space.bin'"
        )[0]
        self.assertEqual(mention["status"], "insufficient_archive_space")
        self.assertGreater(mention["next_retry_at"], 2000)

    def test_worker_lock_and_partial_recovery_are_local_and_content_free(self):
        self.add_file_mention("busy.txt")
        archive = self.archive()
        archive.ensure_layout()
        partial = os.path.join(self.archive_root, "tmp", ".partial-crash-fixture")
        with open(partial, "wb") as file:
            file.write(b"incomplete private bytes")
        with archive._worker_lock():
            pass
        self.assertFalse(os.path.exists(partial))

        with archive._worker_lock():
            outcome = archive.process_pending()
        self.assertEqual(outcome["state"], "worker_busy")

        with open(os.path.join(self.archive_root, ".archive.lock"), encoding="utf-8") as lock:
            self.assertEqual(lock.read(), "")

    def test_losing_process_cannot_recover_the_active_writers_partial(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        holder_results = context.Queue()
        contender_results = context.Queue()
        holder = context.Process(
            target=_hold_archive_partial,
            args=(
                self.db_path,
                self.archive_root,
                self.db_dir,
                ready,
                release,
                holder_results,
            ),
        )
        contender = context.Process(
            target=_contend_archive_lock,
            args=(
                self.db_path,
                self.archive_root,
                self.db_dir,
                ready,
                contender_results,
            ),
        )
        holder.start()
        try:
            self.assertTrue(ready.wait(10))
            contender.start()
            contender.join(10)
            self.assertEqual(contender.exitcode, 0)
            contender_result = contender_results.get(timeout=2)
            self.assertEqual(contender_result["state"], "worker_busy")
            self.assertTrue(contender_result["partial_exists"])
        finally:
            release.set()
            if contender.pid is not None:
                contender.join(5)
            holder.join(10)

        self.assertEqual(holder.exitcode, 0)
        holder_result = holder_results.get(timeout=2)
        self.assertEqual(holder_result["state"], "released")
        self.assertTrue(holder_result["partial_exists"])

    def test_config_consumer_bootstraps_catalog_for_crash_recovery(self):
        fresh_db = os.path.join(self.tmp.name, "fresh", "knowledge.db")
        fresh_archive = os.path.join(self.tmp.name, "fresh", "archive")
        config = {
            **self.config,
            "monitor_knowledge_db": fresh_db,
            "monitor_obsidian_root": os.path.join(self.tmp.name, "fresh", "obsidian"),
            "attachment_archive_root": fresh_archive,
            "attachment_archive_enabled": True,
        }

        result = process_pending_from_config(config)

        self.assertEqual(result["state"], "healthy")
        conn = sqlite3.connect(fresh_db)
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        finally:
            conn.close()
        self.assertIn("attachment_mentions", tables)
        self.assertIn("attachment_objects", tables)


if __name__ == "__main__":
    unittest.main()
