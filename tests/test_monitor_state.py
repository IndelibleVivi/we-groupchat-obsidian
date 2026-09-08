import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from core.monitor import reset_state_to_now
from core.monitor_state import (
    MONITOR_STATE_SCHEMA,
    MONITOR_PENDING_STATE_SCHEMA,
    MonitorStateError,
    MonitorStateStore,
)


def _race_commit(path, value, ready_queue, start_event, result_queue):
    store = MonitorStateStore(path)
    snapshot = store.read()
    ready_queue.put(snapshot.revision)
    start_event.wait(10)
    try:
        committed = store.commit(snapshot.revision, {"last_checked_ts": value})
        result_queue.put(("committed", committed.revision))
    except MonitorStateError as exc:
        result_queue.put((exc.code, None))


def _hold_execution_lock(path, ready, release):
    from core.platform import PlatformName, create_file_lock
    selected = PlatformName.WINDOWS if os.name == "nt" else PlatformName.MACOS
    store = MonitorStateStore(path, file_lock=create_file_lock(selected))
    with store.execution_lock():
        ready.set()
        release.wait(10)
        os._exit(0)  # Deliberate synthetic crash: no context-manager cleanup.


class MonitorStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.store = MonitorStateStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_absent_state_is_distinct_and_initializes_once(self):
        missing = self.store.read()
        self.assertFalse(missing.existed)
        self.assertEqual(missing.revision, 0)
        self.assertEqual(missing.data, {})

        initialized = self.store.initialize_if_absent({"last_checked_ts": 100})
        self.assertFalse(initialized.existed)
        self.assertEqual(initialized.revision, 1)
        self.assertEqual(initialized.data["last_checked_ts"], 100)

        retained = self.store.initialize_if_absent({"last_checked_ts": 999})
        self.assertTrue(retained.existed)
        self.assertEqual(retained.revision, 1)
        self.assertEqual(retained.data["last_checked_ts"], 100)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(str(self.path) + ".lock").stat().st_mode), 0o600)

    def test_legacy_state_migrates_without_losing_fields(self):
        legacy = {
            "last_checked_ts": 10,
            "last_topic_key": "fixture-topic",
            "ai_failure_count": 2,
        }
        self.path.write_text(json.dumps(legacy), encoding="utf-8")

        snapshot = self.store.read()
        self.assertTrue(snapshot.existed)
        self.assertEqual(snapshot.revision, 0)
        self.assertEqual(snapshot.data, legacy)

        committed = self.store.commit(snapshot.revision, snapshot.data)
        self.assertEqual(committed.revision, 1)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], MONITOR_STATE_SCHEMA)
        self.assertEqual(payload["revision"], 1)
        for key, value in legacy.items():
            self.assertEqual(payload[key], value)

    def test_corrupt_json_never_initializes_to_now(self):
        original = b'{"last_checked_ts":'
        self.path.write_bytes(original)

        with self.assertRaises(MonitorStateError) as caught:
            self.store.initialize_if_absent({"last_checked_ts": 999})

        self.assertEqual(caught.exception.code, "monitor_state_corrupt")
        self.assertEqual(self.path.read_bytes(), original)

    def test_symlink_state_is_rejected(self):
        target = Path(self.tmp.name) / "target.json"
        target.write_text('{"last_checked_ts": 10}', encoding="utf-8")
        self.path.symlink_to(target)

        with self.assertRaises(MonitorStateError) as caught:
            self.store.read()

        self.assertEqual(caught.exception.code, "monitor_state_not_regular")

    def test_pending_schema_blocks_old_readers_until_acknowledged(self):
        snapshot = self.store.initialize_if_absent({
            "last_checked_ts": 10, "pending_source_batch": {"fixture": True},
        })
        payload = json.loads(self.path.read_text())
        self.assertEqual(payload["schema"], MONITOR_PENDING_STATE_SCHEMA)
        self.assertNotEqual(payload["schema"], MONITOR_STATE_SCHEMA)
        # The pre-change reader accepts exactly MONITOR_STATE_SCHEMA.
        self.assertTrue(snapshot.data["pending_source_batch"])
        self.store.update(lambda state: {key: value for key, value in state.items()
                                         if key != "pending_source_batch"})
        self.assertEqual(json.loads(self.path.read_text())["schema"], MONITOR_STATE_SCHEMA)

    def test_v1_or_unversioned_state_cannot_hide_pending_work(self):
        for schema in (None, MONITOR_STATE_SCHEMA):
            with self.subTest(schema=schema):
                payload = {"last_checked_ts": 10, "pending_source_batch": {"fixture": True}}
                if schema:
                    payload.update(schema=schema, revision=1)
                self.path.write_text(json.dumps(payload))
                with self.assertRaises(MonitorStateError):
                    self.store.read()

    def test_stale_revision_commit_is_rejected(self):
        first = self.store.initialize_if_absent({"last_checked_ts": 10})
        second = self.store.commit(first.revision, {"last_checked_ts": 11})

        with self.assertRaises(MonitorStateError) as caught:
            self.store.commit(first.revision, {"last_checked_ts": 12})

        self.assertEqual(caught.exception.code, "monitor_state_conflict")
        self.assertEqual(self.store.read().revision, second.revision)
        self.assertEqual(self.store.read().data["last_checked_ts"], 11)

    def test_interrupted_temporary_write_preserves_canonical_state(self):
        first = self.store.initialize_if_absent({"last_checked_ts": 10})

        with patch("core.monitor_state.os.replace", side_effect=OSError("interrupted")):
            with self.assertRaises(MonitorStateError) as caught:
                self.store.commit(first.revision, {"last_checked_ts": 11})

        self.assertEqual(caught.exception.code, "monitor_state_write_failed")
        retained = self.store.read()
        self.assertEqual(retained.revision, first.revision)
        self.assertEqual(retained.data["last_checked_ts"], 10)
        self.assertEqual(list(Path(self.tmp.name).glob(".state.json.*.tmp")), [])

    def test_explicit_reset_to_now_is_atomic_and_preserves_other_fields(self):
        self.store.initialize_if_absent({
            "last_checked_ts": 10,
            "last_topic_key": "old-topic",
            "last_notified_ts": 11,
            "future_cursor": {"opaque": "value"},
            "pending_source_batch": {"fixture": "explicitly-reset"},
        })

        reset_state_to_now(self.path, now_func=lambda: 500)

        snapshot = self.store.read()
        self.assertEqual(snapshot.revision, 2)
        self.assertEqual(snapshot.data["last_checked_ts"], 500)
        self.assertNotIn("pending_source_batch", snapshot.data)
        self.assertNotIn("last_topic_key", snapshot.data)
        self.assertNotIn("last_notified_ts", snapshot.data)
        self.assertEqual(snapshot.data["future_cursor"], {"opaque": "value"})

    def test_execution_lock_excludes_another_process_and_survives_owner_exit(self):
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        process = context.Process(target=_hold_execution_lock, args=(str(self.path), ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(10))
            with self.assertRaises(MonitorStateError) as caught:
                with self.store.execution_lock():
                    self.fail("overlapping ownership")
            self.assertEqual(caught.exception.code, "monitor_worker_busy")
        finally:
            release.set()
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertEqual(process.exitcode, 0)
        with self.store.execution_lock():
            pass

    def test_two_processes_cannot_replace_the_same_revision(self):
        self.store.initialize_if_absent({"last_checked_ts": 10})
        context = multiprocessing.get_context("spawn")
        ready_queue = context.Queue()
        result_queue = context.Queue()
        start_event = context.Event()
        processes = [
            context.Process(
                target=_race_commit,
                args=(str(self.path), value, ready_queue, start_event, result_queue),
            )
            for value in (11, 12)
        ]
        for process in processes:
            process.start()
        self.assertEqual([ready_queue.get(timeout=10) for _ in processes], [1, 1])
        start_event.set()
        results = [result_queue.get(timeout=10)[0] for _ in processes]
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)

        self.assertCountEqual(results, ["committed", "monitor_state_conflict"])
        final = self.store.read()
        self.assertEqual(final.revision, 2)
        self.assertIn(final.data["last_checked_ts"], {11, 12})


if __name__ == "__main__":
    unittest.main()
