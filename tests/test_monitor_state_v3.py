"""Synthetic MonitorStateStore v3 contracts; no live source or generation DB."""
from copy import deepcopy
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from core.monitor_source import MonitorSourceBatch, freeze_monitor_source_batch
from core.monitor_state import (
    MONITOR_CAPTURE_RECEIPT_SCHEMA,
    MONITOR_MIGRATION_RECEIPT_SCHEMA,
    MONITOR_PENDING_STATE_SCHEMA,
    MONITOR_PROCESSING_RECEIPT_SCHEMA,
    MONITOR_ROLLBACK_RECEIPT_SCHEMA,
    MONITOR_STATE_SCHEMA,
    MONITOR_V3_STATE_SCHEMA,
    MonitorStateError,
    MonitorStateStore,
    monitor_state_fingerprint,
)
from core.source_adapter import encode_cursor_token


_BASELINE = "8efbdb5d82315b59c2362211715348a16fa14d03"
_BINDING_FIELDS = (
    "semantic_generation_id", "source_namespace", "chat_id",
    "source_inventory_digest", "source_inventory_revision",
)


def _vector(timestamp=100, rowid=0):
    return {
        "logical-a": {
            "generation_id": "physical-a",
            "cursor_token": encode_cursor_token(timestamp, rowid),
            "start_cursor": encode_cursor_token(100, 0),
        },
        "logical-b": {
            "generation_id": "physical-b",
            "cursor_token": encode_cursor_token(100, 0),
            "start_cursor": encode_cursor_token(100, 0),
        },
    }


def _state(*, applied=100, captured=100, generation="semantic-one"):
    return {
        "semantic_generation_id": generation,
        "source_namespace": "synthetic-source",
        "chat_id": "synthetic-chat",
        "source_inventory_digest": "a" * 64,
        "source_inventory_revision": 1,
        "source_cursors": _vector(applied),
        "captured_cursors": _vector(captured),
        "last_checked_ts": applied,
        "capture_receipt": None,
        "processing_receipt": None,
    }


def _migration(old, new, *, resolution=None):
    return {
        "schema": MONITOR_MIGRATION_RECEIPT_SCHEMA,
        "receipt_id": "migration-receipt",
        "migration_id": "migration-transaction",
        **{key: new[key] for key in _BINDING_FIELDS},
        "old_state_revision": old.revision,
        "old_state_fingerprint": monitor_state_fingerprint(old),
        "input_source_cursors": deepcopy(old.data.get("source_cursors", {})),
        "input_captured_cursors": deepcopy(old.data.get("captured_cursors", old.data.get("source_cursors", {}))),
        "output_source_cursors": deepcopy(new["source_cursors"]),
        "output_captured_cursors": deepcopy(new["captured_cursors"]),
        "capture_receipt_ids": ["rebuild-capture"],
        "processing_receipt_ids": ["rebuild-processing"],
        "canonical_receipt_id": "rebuild-canonical",
        "outbox_receipt_id": "rebuild-outbox",
        "pending_resolution": resolution,
    }


def _capture(snapshot, output, identity="capture-one"):
    return {
        "schema": MONITOR_CAPTURE_RECEIPT_SCHEMA,
        "receipt_id": identity,
        "batch_id": "batch-" + identity,
        **{key: snapshot.data[key] for key in _BINDING_FIELDS},
        "state_revision": snapshot.revision,
        "input_cursors": deepcopy(snapshot.data["captured_cursors"]),
        "output_cursors": deepcopy(output),
    }


def _processing(snapshot, output, identity="processing-one"):
    return {
        "schema": MONITOR_PROCESSING_RECEIPT_SCHEMA,
        "receipt_id": identity,
        "processing_id": "transaction-" + identity,
        "capture_receipt_ids": ["capture-one"],
        "canonical_receipt_id": "canonical-" + identity,
        "outbox_receipt_id": "outbox-" + identity,
        **{key: snapshot.data[key] for key in _BINDING_FIELDS},
        "state_revision": snapshot.revision,
        "input_cursors": deepcopy(snapshot.data["source_cursors"]),
        "output_cursors": deepcopy(output),
    }


def _rollback(current, backup, backup_bytes):
    last_capture = current.data["capture_receipt"]
    return {
        "schema": MONITOR_ROLLBACK_RECEIPT_SCHEMA,
        "receipt_id": "rollback-receipt", "rollback_id": "rollback-transaction",
        "run_id": "synthetic-rebuild-run", "journal_id": "synthetic-journal",
        **{key: current.data[key] for key in _BINDING_FIELDS},
        "state_revision": current.revision,
        "current_state_fingerprint": monitor_state_fingerprint(current),
        "backup_bytes_digest": hashlib.sha256(backup_bytes).hexdigest(),
        "backup_state_fingerprint": monitor_state_fingerprint(backup),
        "retained_raw_store_id": "retained-generation-db",
        "retained_raw_receipt_id": "durable-raw-retention-receipt",
        "retained_capture_receipt_ids": (
            [last_capture["receipt_id"]] if last_capture
            else list(current.data["migration_receipt"]["capture_receipt_ids"])
        ),
        "retained_captured_cursors": deepcopy(current.data["captured_cursors"]),
        "replay_receipt_id": "durable-replay-receipt",
        "replay_destination_id": "durable-replay-destination",
        "replay_from_cursors": deepcopy(backup.data.get("source_cursors", {})),
        "replay_through_cursors": deepcopy(current.data["captured_cursors"]),
    }


def _race_capture(path, timestamp, ready, start, results):
    store = MonitorStateStore(path)
    old = store.read()
    ready.put(old.revision)
    start.wait(10)
    try:
        output = _vector(timestamp)
        result = store.commit_capture(old.revision, output, _capture(old, output, str(timestamp)))
        results.put(("committed", result.revision))
    except MonitorStateError as exc:
        results.put((exc.code, None))


class MonitorStateV3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.store = MonitorStateStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, *, applied=100, captured=100):
        old = self.store.inspect()
        new = _state(applied=applied, captured=captured)
        return self.store.install_v3(old.revision, new, _migration(old, new))

    def rejected_unchanged(self, callback, code=None):
        before = self.path.read_bytes() if self.path.exists() else None
        with self.assertRaises(MonitorStateError) as caught:
            callback()
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)

    def pending_old(self):
        state = {
            "last_checked_ts": 100,
            "source_cursors": _vector(),
            "source_inventory_digest": "a" * 64,
            "source_inventory_revision": 1,
        }
        batch = MonitorSourceBatch(
            inventory_digest="a" * 64, inventory_revision=1,
            source_cursors=_vector(110, 2), raw_count=2, source_eof=False,
            last_checked_ts=110, source_batch_id="wgbatch_" + "b" * 64,
            raw_messages=(
                {"source_message_id": "synthetic-member-1", "timestamp": 105, "text": "fixture one"},
                {"source_message_id": "synthetic-member-2", "timestamp": 110, "text": "fixture two"},
            ), visible_messages=(),
        )
        state["pending_source_batch"] = freeze_monitor_source_batch(batch, state, "c" * 64)
        return self.store.initialize_if_absent(state)

    def resolution(self, old, disposition="superseded_by_rebuild"):
        pending = old.data["pending_source_batch"]
        return {
            "disposition": disposition,
            "lineage_id": "synthetic-lineage",
            "pending_descriptor": deepcopy(pending),
            "member_coverage": [{
                "source_message_id": member,
                "capture_receipt_id": "rebuild-capture",
                "processing_receipt_id": "rebuild-processing",
            } for member in pending["source_message_ids"]],
        }

    def test_install_binds_old_snapshot_and_does_not_mutate_arguments(self):
        old = self.store.initialize_if_absent({"last_checked_ts": 100, "last_topic_key": "old-topic"})
        new = _state()
        receipt = _migration(old, new)
        original_new, original_receipt = deepcopy(new), deepcopy(receipt)
        installed = self.store.install_v3(old.revision, new, receipt)
        self.assertEqual(new, original_new)
        self.assertEqual(receipt, original_receipt)
        self.assertEqual(installed.schema, MONITOR_V3_STATE_SCHEMA)
        self.assertEqual(installed.revision, 2)
        self.assertEqual(installed.data["migration_receipt"]["old_state_fingerprint"], monitor_state_fingerprint(old))
        self.assertEqual(self.store.inspect().data, installed.data)
        self.assertEqual(self.store.inspect().revision, installed.revision)
        self.assertEqual(json.loads(self.path.read_text())["schema"], MONITOR_V3_STATE_SCHEMA)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_capture_advances_independently_from_applied(self):
        installed = self.install()
        output = _vector(120, 7)
        captured = self.store.commit_capture(installed.revision, output, _capture(installed, output))
        self.assertEqual(captured.data["captured_cursors"], output)
        self.assertEqual(captured.data["source_cursors"], installed.data["source_cursors"])
        self.assertEqual(captured.data["last_checked_ts"], 100)
        self.assertIsNone(captured.data["processing_receipt"])

    def test_applied_requires_durable_canonical_and_outbox_binding_not_delivery(self):
        installed = self.install()
        capture_output = _vector(120, 7)
        captured = self.store.commit_capture(installed.revision, capture_output, _capture(installed, capture_output))
        output = _vector(110, 4)
        receipt = _processing(captured, output)
        # Canonical and outbox are durable; projection may still be conflicted.
        self.assertNotIn("projection_delivered", receipt)
        applied = self.store.commit_applied(captured.revision, output, receipt)
        self.assertEqual(applied.data["source_cursors"], output)
        self.assertEqual(applied.data["captured_cursors"], capture_output)
        self.assertEqual(applied.data["last_checked_ts"], 110)
        self.assertEqual(applied.data["processing_receipt"], receipt)

    def test_applied_cannot_exceed_captured_even_on_one_shard(self):
        old = self.install(captured=120)
        for output in (_vector(121), {**_vector(110), "logical-b": {
            **_vector()["logical-b"], "cursor_token": "[100,1]",
        }}):
            with self.subTest(output=output):
                self.rejected_unchanged(lambda: self.store.commit_applied(old.revision, output, _processing(old, output)))

    def test_monotonicity_uses_keyset_rowid_not_token_string_order(self):
        installed = self.install()
        first = _vector(100, 9)
        captured = self.store.commit_capture(installed.revision, first, _capture(installed, first))
        second = _vector(100, 10)
        captured = self.store.commit_capture(captured.revision, second, _capture(captured, second, "capture-two"))
        self.assertEqual(captured.data["last_checked_ts"], 100)
        backward = _vector(100, 8)
        self.rejected_unchanged(lambda: self.store.commit_capture(captured.revision, backward, _capture(captured, backward, "backward")))

    def test_capture_and_processing_require_exact_receipts(self):
        old = self.install(captured=120)
        for processing in (False, True):
            factory = _processing if processing else _capture
            commit = self.store.commit_applied if processing else self.store.commit_capture
            output = _vector(110 if processing else 130)
            invalid_receipts = [{"success": True}]
            base = factory(old, output)
            for key, value in (
                ("schema", "unknown-schema"),
                ("semantic_generation_id", "other-generation"),
                ("source_namespace", "other-source"),
                ("chat_id", "other-chat"),
                ("source_inventory_digest", "d" * 64),
                ("source_inventory_revision", 2 if processing else 0),
                ("state_revision", True),
                ("state_revision", old.revision + 1),
                ("input_cursors", _vector(105)),
                ("output_cursors", _vector(115)),
                ("receipt_id", ""),
            ):
                invalid_receipts.append({**deepcopy(base), key: value})
            invalid_receipts.append({**base, "success": True})
            for key in base:
                invalid_receipts.append({k: v for k, v in base.items() if k != key})
            for receipt in invalid_receipts:
                with self.subTest(processing=processing, receipt=receipt):
                    self.rejected_unchanged(lambda: commit(old.revision, output, receipt))

    def test_unrelated_inventory_revision_does_not_block_same_scoped_digest(self):
        old = self.install()
        capture = _capture(old, _vector(110))
        capture["source_inventory_revision"] = 5
        captured = self.store.commit_capture(old.revision, _vector(110), capture)
        self.assertEqual(captured.data["source_inventory_revision"], 5)
        later = _capture(captured, _vector(120), "capture-later")
        later["source_inventory_revision"] = 8
        latest = self.store.commit_capture(captured.revision, _vector(120), later)
        processing = _processing(latest, _vector(110))
        processing["source_inventory_revision"] = 5
        applied = self.store.commit_applied(latest.revision, _vector(110), processing)
        self.assertEqual(applied.data["source_inventory_revision"], 8)
        self.assertEqual(self.store.inspect().data, applied.data)
        stale = _capture(applied, _vector(130), "capture-stale")
        stale["source_inventory_revision"] = 7
        self.rejected_unchanged(lambda: self.store.commit_capture(applied.revision, _vector(130), stale))

    def test_shard_generation_membership_and_start_are_immutable(self):
        old = self.install(captured=120)
        variants = []
        changed = _vector(130)
        changed["logical-a"]["generation_id"] = "changed-physical"
        variants.append(changed)
        changed = _vector(130)
        changed["logical-a"]["start_cursor"] = "[99,0]"
        variants.append(changed)
        changed = _vector(130)
        changed["logical-a"].pop("start_cursor")
        variants.append(changed)
        changed = _vector(130)
        changed.pop("logical-b")
        variants.append(changed)
        for output in variants:
            with self.subTest(output=output):
                self.rejected_unchanged(lambda: self.store.commit_capture(old.revision, output, _capture(old, output)))

    def test_cursor_tokens_reject_coercion_wrong_shape_negative_and_nonfinite(self):
        old = self.install()
        for token in ("", "[true,1]", "[100,false]", "[100.0,1]", '["100",1]',
                      "[100,1,2]", "[100]", "[-1,2]", "[100,-1]", "[NaN,1]", "[Infinity,1]", "{}"):
            output = _vector(110)
            output["logical-a"]["cursor_token"] = token
            with self.subTest(token=token):
                self.rejected_unchanged(lambda: self.store.commit_capture(old.revision, output, _capture(old, output)))

    def test_generic_metadata_commit_preserves_every_progress_binding(self):
        old = self.install(captured=120)
        metadata = {
            "ai_failure_count": 2, "ai_last_error_code": "ai_rate_limited",
            "ai_last_error_ts": 130.5, "ai_next_retry_after": 150,
            "last_topic_key": "topic-fixture", "last_notified_ts": 120,
        }
        updated = self.store.commit(old.revision, {**deepcopy(old.data), **metadata})
        self.assertEqual(updated.schema, MONITOR_V3_STATE_SCHEMA)
        for key, value in old.data.items():
            self.assertEqual(updated.data[key], value)
        self.assertEqual({key: updated.data[key] for key in metadata}, metadata)
        cleared = self.store.update(lambda data: {key: value for key, value in data.items() if key not in metadata})
        self.assertEqual(cleared.data, old.data)

    def test_generic_writes_cannot_bypass_receipts_by_nested_mutation_or_reset(self):
        old = self.install()
        def nested(data):
            data["captured_cursors"]["logical-a"]["cursor_token"] = "[130,1]"
        self.rejected_unchanged(lambda: self.store.update(nested), "monitor_state_v3_receipt_required")
        self.rejected_unchanged(lambda: self.store.commit(old.revision, {"last_checked_ts": 150}), "monitor_state_v3_receipt_required")
        self.rejected_unchanged(lambda: self.store.commit(old.revision, {**old.data, "schema": MONITOR_STATE_SCHEMA}), "monitor_state_v3_downgrade_forbidden")
        from core.monitor import reset_state_to_now
        self.rejected_unchanged(lambda: reset_state_to_now(self.path, now_func=lambda: 150), "monitor_state_v3_receipt_required")
        self.assertEqual(self.store.inspect().data, old.data)

    def test_generic_writes_cannot_initialize_or_upgrade_to_v3(self):
        new = _state()
        for call in (
            lambda: self.store.initialize_if_absent(new),
            lambda: self.store.commit(0, new),
            lambda: self.store.update(lambda _: new),
        ):
            self.rejected_unchanged(call, "monitor_state_v3_install_required")

    def test_strict_state_and_metadata_types_are_not_coerced(self):
        old = self.install()
        for key, value in (
            ("ai_failure_count", True), ("ai_failure_count", 1.5), ("ai_last_error_ts", True),
            ("ai_next_retry_after", float("nan")), ("last_notified_ts", float("inf")),
            ("ai_last_error_code", "unbounded message body"), ("ai_last_error", "private body"),
        ):
            with self.subTest(key=key, value=value):
                self.rejected_unchanged(lambda: self.store.commit(old.revision, {**old.data, key: value}))
        for revision in (True, 1.0, "1", -1, float("nan")):
            with self.subTest(revision=revision):
                self.rejected_unchanged(lambda: self.store.commit(revision, old.data))

    def test_stale_cas_and_reused_receipt_fail_closed(self):
        old = self.install()
        output = _vector(120)
        receipt = _capture(old, output)
        captured = self.store.commit_capture(old.revision, output, receipt)
        self.rejected_unchanged(lambda: self.store.commit_capture(old.revision, output, receipt), "monitor_state_conflict")
        reused = _capture(captured, _vector(130))
        self.rejected_unchanged(lambda: self.store.commit_capture(captured.revision, _vector(130), reused), "monitor_state_receipt_reused")

    def test_pending_migration_retains_exact_descriptor_and_lineage(self):
        old = self.pending_old()
        self.assertEqual(old.schema, MONITOR_PENDING_STATE_SCHEMA)
        new = _state(applied=110, captured=120)
        resolution = self.resolution(old)
        installed = self.store.install_v3(old.revision, new, _migration(old, new, resolution=resolution))
        self.assertNotIn("pending_source_batch", installed.data)
        self.assertEqual(installed.data["migration_receipt"]["pending_resolution"], resolution)
        self.assertEqual(self.store.inspect().data, installed.data)

    def test_pending_migration_rejects_missing_fake_or_incomplete_coverage(self):
        old = self.pending_old()
        new = _state(applied=110, captured=120)
        good = self.resolution(old, "adopted")
        bad = [None]
        for mutation in (
            lambda r: r["member_coverage"].pop(),
            lambda r: r["member_coverage"].reverse(),
            lambda r: r["member_coverage"][0].update(source_message_id="invented-member"),
            lambda r: r["member_coverage"][0].update(capture_receipt_id="unknown-capture"),
            lambda r: r["member_coverage"][0].update(processing_receipt_id="unknown-processing"),
            lambda r: r.update(lineage_id=""),
            lambda r: r.update(disposition="success"),
            lambda r: r["pending_descriptor"].update(content_fingerprint="d" * 64),
        ):
            resolution = deepcopy(good)
            mutation(resolution)
            bad.append(resolution)
        for resolution in bad:
            with self.subTest(resolution=resolution):
                self.rejected_unchanged(lambda: self.store.install_v3(old.revision, new, _migration(old, new, resolution=resolution)))
        adopted = self.store.install_v3(old.revision, new, _migration(old, new, resolution=good))
        self.assertEqual(adopted.data["migration_receipt"]["pending_resolution"]["disposition"], "adopted")

    def test_pending_checkpoint_validation_precedes_migration(self):
        old = self.pending_old()
        corrupted = {**old.data, "last_checked_ts": 101}
        old = self.store.commit(old.revision, corrupted)
        new = _state()
        self.rejected_unchanged(
            lambda: self.store.install_v3(old.revision, new, _migration(old, new, resolution=self.resolution(old))),
            "monitor_state_pending_migration_invalid",
        )

    def test_migration_rejects_forged_snapshot_or_progress_binding(self):
        old = self.store.initialize_if_absent({"last_checked_ts": 100})
        new = _state()
        receipt = _migration(old, new)
        for key, value in (
            ("old_state_revision", old.revision + 1), ("old_state_fingerprint", "0" * 64),
            ("input_source_cursors", _vector()), ("input_captured_cursors", _vector()),
            ("output_source_cursors", _vector(101)), ("output_captured_cursors", _vector(101)),
        ):
            with self.subTest(key=key):
                forged = {**deepcopy(receipt), key: value}
                self.rejected_unchanged(lambda: self.store.install_v3(old.revision, new, forged))

    def test_only_explicit_install_can_replace_semantic_generation(self):
        old = self.install(applied=110, captured=120)
        same_generation = _state()
        self.rejected_unchanged(lambda: self.store.install_v3(old.revision, same_generation, _migration(old, same_generation)), "monitor_state_generation_unchanged")
        new = _state(generation="semantic-two")
        new["source_inventory_digest"] = "d" * 64
        new["source_inventory_revision"] = 2
        new["source_cursors"]["logical-a"]["generation_id"] = "replacement-physical"
        new["captured_cursors"]["logical-a"]["generation_id"] = "replacement-physical"
        installed = self.store.install_v3(old.revision, new, _migration(old, new))
        self.assertEqual(installed.data["semantic_generation_id"], "semantic-two")
        self.assertEqual(installed.data["last_checked_ts"], 100)

    def test_v3_parse_rejects_unknown_schema_missing_fields_and_downgrade(self):
        self.install()
        valid = json.loads(self.path.read_text())
        invalid = []
        for key in valid:
            invalid.append({k: v for k, v in valid.items() if k != key})
        invalid.extend([
            {**valid, "schema": MONITOR_STATE_SCHEMA},
            {**valid, "schema": MONITOR_PENDING_STATE_SCHEMA},
            {**valid, "schema": "unknown"},
            {**valid, "schema": []},
            {**valid, "revision": True},
            {**valid, "revision": 1.5},
            {**valid, "source_inventory_revision": True},
            {**valid, "last_checked_ts": 100.0},
            {**valid, "last_checked_ts": 999},
            {**valid, "pending_source_batch": {}},
            {**valid, "success": True},
            {k: v for k, v in valid.items() if k not in {"schema", "revision"}},
        ])
        for payload in invalid:
            with self.subTest(payload=payload):
                self.path.write_text(json.dumps(payload))
                self.rejected_unchanged(lambda: self.store.inspect(), "monitor_state_corrupt")

    def test_corrupt_symlink_nonregular_and_nonfinite_never_migrate(self):
        for raw in (b'{"broken":', b'{"last_checked_ts":NaN}', b'{"last_checked_ts":1e400}',
                    b'{"last_checked_ts":1,"last_checked_ts":2}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                os.chmod(self.path, 0o644)
                before = self.path.stat().st_mode
                self.rejected_unchanged(lambda: self.store.install_v3(0, _state(), {}), "monitor_state_corrupt")
                self.assertEqual(self.path.stat().st_mode, before)
        self.path.unlink()
        target = Path(self.tmp.name) / "target.json"
        target.write_text('{"last_checked_ts":100}')
        self.path.symlink_to(target)
        self.rejected_unchanged(lambda: self.store.install_v3(0, _state(), {}), "monitor_state_not_regular")
        self.assertEqual(target.read_text(), '{"last_checked_ts":100}')
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaisesRegex(MonitorStateError, "monitor_state_not_regular"):
            self.store.install_v3(0, _state(), {})

    def test_inspect_creates_no_files_and_does_not_change_permissions(self):
        absent = Path(self.tmp.name) / "absent" / "state.json"
        with patch("core.state_storage.StateStorage.prepare", side_effect=AssertionError("unexpected mutation")):
            self.assertFalse(MonitorStateStore(absent).inspect().existed)
        self.assertFalse(absent.parent.exists())
        self.install()
        os.chmod(self.path, 0o644)
        before = (self.path.read_bytes(), self.path.stat().st_mode, self.path.stat().st_mtime_ns,
                  set(Path(self.tmp.name).iterdir()))
        with patch("core.state_storage.StateStorage.prepare", side_effect=AssertionError("unexpected mutation")):
            self.assertEqual(self.store.inspect().schema, MONITOR_V3_STATE_SCHEMA)
        self.assertEqual(before, (self.path.read_bytes(), self.path.stat().st_mode, self.path.stat().st_mtime_ns,
                                 set(Path(self.tmp.name).iterdir())))

    def test_atomic_publish_failure_preserves_both_progress_vectors(self):
        old = self.install()
        output = _vector(120)
        with patch.object(self.store._storage.services.atomic_publisher, "_replace", side_effect=OSError("synthetic failure")):
            self.rejected_unchanged(lambda: self.store.commit_capture(old.revision, output, _capture(old, output)), "monitor_state_write_failed")
        self.assertEqual(self.store.inspect().data, old.data)

    def test_original_immutable_reader_rejects_synthetic_v3(self):
        self.install()
        root = Path(__file__).resolve().parents[1]
        source = subprocess.run(
            ["git", "show", _BASELINE + ":core/monitor_state.py"], cwd=root,
            capture_output=True, text=True, check=False,
        )
        if source.returncode:
            self.skipTest("immutable pre-v3 Git object is not available in this checkout")
        program = '''import sys, types
old = types.ModuleType("core._pre_v3_reader")
old.__package__ = "core"
sys.modules[old.__name__] = old
exec(compile(sys.stdin.read(), "immutable-pre-v3-monitor-state.py", "exec"), old.__dict__)
try:
    old.MonitorStateStore(sys.argv[1]).inspect()
except old.MonitorStateError as exc:
    if exc.code != "monitor_state_corrupt":
        raise
else:
    raise AssertionError("old reader admitted v3")
'''
        before = self.path.read_bytes()
        result = subprocess.run([sys.executable, "-c", program, str(self.path)], cwd=root,
                                input=source.stdout, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.path.read_bytes(), before)

    def test_restore_v2_retains_complete_pending_and_raw_replay_binding(self):
        backup = self.pending_old()
        backup_bytes = self.path.read_bytes()
        new = _state(applied=110, captured=120)
        installed = self.store.install_v3(backup.revision, new, _migration(backup, new, resolution=self.resolution(backup)))
        output = _vector(130)
        captured = self.store.commit_capture(installed.revision, output, _capture(installed, output))
        receipt = _rollback(captured, backup, backup_bytes)
        restored = self.store.restore_checkpoint(captured.revision, backup_bytes, receipt)
        self.assertEqual(restored.schema, MONITOR_PENDING_STATE_SCHEMA)
        self.assertEqual(restored.revision, captured.revision + 1)
        self.assertEqual(restored.data, backup.data)
        self.assertEqual(self.store.inspect().data["pending_source_batch"], backup.data["pending_source_batch"])
        self.assertEqual(receipt["retained_captured_cursors"], output)
        self.assertEqual(receipt["replay_from_cursors"], backup.data["source_cursors"])

    def test_restore_v1_and_unversioned_backup_keep_data_and_use_new_cas_revision(self):
        for schema in (None, MONITOR_STATE_SCHEMA):
            with self.subTest(schema=schema):
                payload = {"last_checked_ts": 100, "last_topic_key": "before-rebuild"}
                if schema:
                    payload.update(schema=schema, revision=3)
                self.path.write_text(json.dumps(payload))
                backup = self.store.inspect()
                backup_bytes = self.path.read_bytes()
                new = _state()
                installed = self.store.install_v3(backup.revision, new, _migration(backup, new))
                restored = self.store.restore_checkpoint(installed.revision, backup_bytes, _rollback(installed, backup, backup_bytes))
                self.assertEqual(restored.schema, MONITOR_STATE_SCHEMA)
                self.assertEqual(restored.data, backup.data)
                self.assertEqual(restored.revision, installed.revision + 1)

    def test_restore_v3_and_reject_reused_rollback_receipt(self):
        self.install(captured=120)
        backup = self.store.inspect()
        backup_bytes = self.path.read_bytes()
        new = _state(generation="semantic-two")
        installed = self.store.install_v3(backup.revision, new, _migration(backup, new))
        receipt = _rollback(installed, backup, backup_bytes)
        self.rejected_unchanged(lambda: self.store.restore_checkpoint(installed.revision - 1, backup_bytes, receipt), "monitor_state_conflict")
        restored = self.store.restore_checkpoint(installed.revision, backup_bytes, receipt)
        self.assertEqual(restored.schema, MONITOR_V3_STATE_SCHEMA)
        self.assertEqual(restored.data, backup.data)
        self.rejected_unchanged(lambda: self.store.restore_checkpoint(restored.revision, backup_bytes, receipt), "monitor_state_receipt_binding_mismatch")

    def test_restore_requires_all_exact_generation_journal_backup_and_replay_fields(self):
        backup = self.store.initialize_if_absent({"last_checked_ts": 100})
        backup_bytes = self.path.read_bytes()
        new = _state()
        installed = self.store.install_v3(backup.revision, new, _migration(backup, new))
        valid = _rollback(installed, backup, backup_bytes)
        invalid = [{"success": True}]
        for key in valid:
            invalid.append({k: v for k, v in valid.items() if k != key})
        for key, value in (
            ("schema", "unknown"), ("semantic_generation_id", "wrong-generation"),
            ("chat_id", "wrong-chat"), ("source_inventory_digest", "e" * 64),
            ("state_revision", True), ("current_state_fingerprint", "e" * 64),
            ("backup_bytes_digest", "e" * 64), ("backup_state_fingerprint", "e" * 64),
            ("run_id", ""), ("journal_id", ""), ("retained_raw_receipt_id", ""),
            ("retained_capture_receipt_ids", ["unknown-capture"]),
            ("retained_captured_cursors", _vector(110)),
            ("replay_from_cursors", _vector()), ("replay_through_cursors", _vector(110)),
            ("replay_destination_id", ""), ("replay_receipt_id", ""),
        ):
            invalid.append({**deepcopy(valid), key: value})
        for receipt in invalid:
            with self.subTest(receipt=receipt):
                self.rejected_unchanged(lambda: self.store.restore_checkpoint(installed.revision, backup_bytes, receipt))
        self.rejected_unchanged(lambda: self.store.restore_checkpoint(installed.revision, backup_bytes + b" ", valid), "monitor_state_rollback_binding_mismatch")

    def test_restore_rejects_invalid_and_invalid_pending_backups_without_writing(self):
        current = self.install()
        invalid = [
            b"{bad", b"[]", b'{"schema":"unknown","revision":1}',
            b'{"schema":"we-groupchat-obsidian.monitor-state.v1","revision":true}',
            b'{"last_checked_ts":NaN}', b'{"last_checked_ts":1,"last_checked_ts":2}',
            b'{"pending_source_batch":{}}',
            b'{"schema":"we-groupchat-obsidian.monitor-state.v2","revision":1,"pending_source_batch":{}}',
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                self.rejected_unchanged(lambda: self.store.restore_checkpoint(current.revision, raw, {}), "monitor_state_backup_invalid")

    def test_restore_requires_latest_capture_in_retained_coverage(self):
        backup = self.store.initialize_if_absent({"last_checked_ts": 100})
        backup_bytes = self.path.read_bytes()
        new = _state()
        installed = self.store.install_v3(backup.revision, new, _migration(backup, new))
        output = _vector(130)
        captured = self.store.commit_capture(installed.revision, output, _capture(installed, output))
        receipt = _rollback(captured, backup, backup_bytes)
        receipt["retained_capture_receipt_ids"] = ["rebuild-capture"]
        self.rejected_unchanged(lambda: self.store.restore_checkpoint(captured.revision, backup_bytes, receipt), "monitor_state_rollback_binding_mismatch")

    def test_restore_atomic_failure_preserves_installed_generation(self):
        backup = self.store.initialize_if_absent({"last_checked_ts": 100})
        backup_bytes = self.path.read_bytes()
        new = _state()
        installed = self.store.install_v3(backup.revision, new, _migration(backup, new))
        receipt = _rollback(installed, backup, backup_bytes)
        with patch.object(self.store._storage.services.atomic_publisher, "_replace", side_effect=OSError("synthetic failure")):
            self.rejected_unchanged(lambda: self.store.restore_checkpoint(installed.revision, backup_bytes, receipt), "monitor_state_write_failed")
        self.assertEqual(self.store.inspect().data, installed.data)

    def test_two_processes_only_one_capture_cas_succeeds(self):
        installed = self.install()
        context = multiprocessing.get_context("spawn")
        ready, results, start = context.Queue(), context.Queue(), context.Event()
        processes = [context.Process(target=_race_capture, args=(str(self.path), timestamp, ready, start, results))
                     for timestamp in (110, 120)]
        for process in processes:
            process.start()
        try:
            self.assertEqual([ready.get(timeout=10) for _ in processes], [installed.revision] * 2)
            start.set()
            outcomes = [results.get(timeout=10)[0] for _ in processes]
            self.assertCountEqual(outcomes, ["committed", "monitor_state_conflict"])
        finally:
            start.set()
            for process in processes:
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
        for process in processes:
            self.assertEqual(process.exitcode, 0)
        final = self.store.inspect()
        self.assertEqual(final.revision, installed.revision + 1)
        self.assertEqual(final.data["source_cursors"], installed.data["source_cursors"])


if __name__ == "__main__":
    unittest.main()
