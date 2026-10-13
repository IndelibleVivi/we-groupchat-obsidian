"""One source owner, durable captures and exact MonitorState v3 receipts."""
from __future__ import annotations

from contextlib import closing
import json
import uuid

from core.monitor_source import _read_inventory, read_monitor_source_batch, verify_monitor_source_inventory
from core.source_adapter import encode_cursor_token, decode_cursor_token, source_snapshot
from core.monitor_state import monitor_state_fingerprint
from .contracts import SemanticStoreError, digest


class SemanticCapture:
    def __init__(self, store, source, *, page_size=200):
        if type(page_size) is not int or not 1 <= page_size <= 2000:
            raise SemanticStoreError("semantic_capture_limit_invalid")
        self.store, self.source, self.page_size = store, source, page_size

    def _records(self, batch, inventory):
        generations = {s["generation_id"]: s["logical_shard_id"] for s in inventory["shards"]
                       if s.get("state") != "explicitly_retired"}
        records = []
        for row in batch.raw_messages:
            envelope = row.get("source_envelope") or {}
            generation = envelope.get("source_generation_id") or envelope.get("db_shard_id")
            if generation not in generations:
                raise SemanticStoreError("source_generation_changed")
            records.append({"source_id": row["source_message_id"],
                            "logical_shard_id": generations[generation], "physical_generation": generation,
                            "source_cursor_token": encode_cursor_token(envelope["create_time"], envelope["rowid"]),
                            "source_sent_at": row["timestamp"], "text": row.get("text", ""),
                            "payload": row, "kind": "message", "provenance_grade": "raw_exact", "core": True})
        return records

    def page(self, chat_id, cursors, *, snapshot_id, purpose="live"):
        inventory, _ = _read_inventory(self.source)
        state = {"source_cursors": cursors, "last_checked_ts": 0,
                 "source_inventory_digest": inventory["inventory_digest"],
                 "source_inventory_revision": inventory["inventory_revision"]}
        batch = read_monitor_source_batch(self.source, chat_id, state, raw_limit=self.page_size)
        records = self._records(batch, inventory)
        batch_id = digest([snapshot_id, chat_id, cursors, batch.source_cursors, records])
        return self.store.capture_batch(batch_id, records,
            source_namespace=inventory["source_namespace"], chat_id=chat_id,
            inventory_digest=batch.inventory_digest, inventory_revision=batch.inventory_revision,
            input_cursors=cursors, output_cursors=batch.source_cursors,
            source_eof=batch.source_eof, purpose=purpose)

    def snapshot(self, chat_ids, *, snapshot_id=None, purpose="rebuild"):
        """A fresh complete pinned traversal, including late old-time rows/revisions."""
        snapshot_id = snapshot_id or str(uuid.uuid4())
        if not chat_ids or len(set(chat_ids)) != len(chat_ids):
            raise SemanticStoreError("semantic_capture_scope_invalid")
        batches, boundaries = [], []
        with source_snapshot(self.source):
            inventory, shards = _read_inventory(self.source)
            for chat_id in chat_ids:
                cursors = {row["logical_shard_id"]: {"generation_id": row["generation_id"],
                           "cursor_token": encode_cursor_token(0, 0), "start_cursor": encode_cursor_token(0, 0)}
                           for row in shards}
                while True:
                    receipt = self.page(chat_id, cursors, snapshot_id=snapshot_id,
                                        purpose="snapshot:" + snapshot_id)
                    batches.append(receipt)
                    if receipt["source_eof"]:
                        cursors = receipt["output_cursors"]
                        break
                    if cursors == receipt["output_cursors"]:
                        raise SemanticStoreError("semantic_capture_no_progress")
                    cursors = receipt["output_cursors"]
                boundaries.append({"source_namespace": inventory["source_namespace"], "chat_id": chat_id,
                                   "inventory_digest": inventory["inventory_digest"],
                                   "inventory_revision": inventory["inventory_revision"], "cursors": cursors})
            verify_monitor_source_inventory(self.source, inventory["inventory_digest"],
                                            inventory["inventory_revision"])
        with closing(self.store.connect()) as conn:
            seq = conn.execute("SELECT COALESCE(MAX(capture_seq),0) FROM source_records").fetchone()[0]
        return {"receipt_id": snapshot_id, "generation_id": self.store.generation_id,
                "batches": batches, "h1": boundaries, "capture_seq": seq,
                "source_inventory": {"complete": True, "digest": inventory["inventory_digest"],
                                     "revision": inventory["inventory_revision"]}}

    def start_boundary(self, chat_id, checkpoint, old_fingerprint):
        """Retain a new selection's from-now boundary without reading raw rows."""
        from core.monitor_source import initialize_monitor_source_state
        initialized = initialize_monitor_source_state(self.source, checkpoint)
        inventory, _ = _read_inventory(self.source)
        if inventory["inventory_digest"] != initialized["source_inventory_digest"]:
            raise SemanticStoreError("source_generation_changed")
        cursors = initialized["source_cursors"]
        purpose = "from-now:" + old_fingerprint
        return self.store.capture_batch(digest([self.store.generation_id, chat_id, purpose, cursors]), [],
            source_namespace=inventory["source_namespace"], chat_id=chat_id,
            inventory_digest=inventory["inventory_digest"], inventory_revision=inventory["inventory_revision"],
            input_cursors=cursors, output_cursors=cursors, source_eof=False, purpose=purpose)


class MonitorReceiptBridge:
    """Resolve durable DB evidence before producing a state-store CAS binding."""
    def __init__(self, store):
        self.store = store

    def _binding(self, snapshot):
        data = snapshot.data
        if data.get("semantic_generation_id") != self.store.generation_id:
            raise SemanticStoreError("semantic_generation_mismatch")
        return {key: data[key] for key in ("semantic_generation_id", "source_namespace", "chat_id",
                "source_inventory_digest", "source_inventory_revision")}

    def capture(self, snapshot, batch_id):
        batch = self.store.get_batch(batch_id)
        if batch is None:
            raise SemanticStoreError("capture_receipt_missing")
        binding = self._binding(snapshot)
        if (batch["source_namespace"] != binding["source_namespace"] or batch["chat_id"] != binding["chat_id"]
                or batch["inventory_digest"] != binding["source_inventory_digest"]
                or batch["inventory_revision"] < binding["source_inventory_revision"]
                or batch["input_cursors"] != snapshot.data["captured_cursors"]):
            raise SemanticStoreError("capture_receipt_binding_mismatch")
        receipt = {"schema": "we-groupchat-obsidian.monitor-capture-receipt.v1", **binding,
                   "source_inventory_revision": batch["inventory_revision"],
                   "receipt_id": batch["receipt_id"], "batch_id": batch_id, "state_revision": snapshot.revision,
                   "input_cursors": batch["input_cursors"], "output_cursors": batch["output_cursors"]}
        self._persist_cas_binding(receipt)
        return receipt

    def processing(self, snapshot, batch_id):
        batch = self.store.get_batch(batch_id)
        binding = self._binding(snapshot)
        if batch is None or batch["input_cursors"] != snapshot.data["source_cursors"]:
            raise SemanticStoreError("processing_prefix_gap")
        if (batch["source_namespace"] != binding["source_namespace"] or batch["chat_id"] != binding["chat_id"]
                or batch["inventory_digest"] != binding["source_inventory_digest"]
                or batch["inventory_revision"] > binding["source_inventory_revision"]):
            raise SemanticStoreError("processing_receipt_binding_mismatch")
        with closing(self.store.connect()) as conn:
            row = conn.execute("SELECT * FROM processing_receipts WHERE batch_id=?", (batch_id,)).fetchone()
            if row is None:
                raise SemanticStoreError("processing_receipt_missing")
            durable = json.loads(row["receipt_json"])
            canonical = conn.execute("SELECT 1 FROM canonical_transactions WHERE transaction_id=?", (durable["transaction_id"],)).fetchone()
            if not canonical:
                raise SemanticStoreError("processing_canonical_receipt_missing")
        self.store.verify_processing_receipt(durable)
        receipt = {"schema": "we-groupchat-obsidian.monitor-processing-receipt.v1", **binding,
                   "source_inventory_revision": batch["inventory_revision"],
                   "receipt_id": durable["receipt_id"], "processing_id": durable["transaction_id"],
                   "canonical_receipt_id": durable["transaction_id"], "outbox_receipt_id": durable["outbox_id"],
                   "capture_receipt_ids": durable["capture_receipt_ids"], "state_revision": snapshot.revision,
                   "input_cursors": batch["input_cursors"], "output_cursors": batch["output_cursors"]}
        self._persist_cas_binding(receipt)
        return receipt

    def migration(self, old_snapshot, boundary, batch_ids, *, pending_batch=None):
        """Prove a complete pass and old pending membership before checkpoint CAS.

        The caller validates the old pending policy and exact source replay first.
        This bridge checks its original row bytes against retained capture rows;
        a matching message identity alone is insufficient migration evidence.
        """
        batches = [self.store.get_batch(batch_id) for batch_id in batch_ids]
        if not batches or any(batch is None for batch in batches):
            raise SemanticStoreError("migration_capture_missing")
        relevant = [batch for batch in batches if batch["source_namespace"] == boundary["source_namespace"]
                    and batch["chat_id"] == boundary["chat_id"]]
        if not relevant or not relevant[-1]["source_eof"] or relevant[-1]["output_cursors"] != boundary["cursors"]:
            raise SemanticStoreError("migration_h1_unproven")
        for index, batch in enumerate(relevant):
            if (batch["inventory_digest"] != boundary["inventory_digest"] or
                    (index and batch["input_cursors"] != relevant[index - 1]["output_cursors"])):
                raise SemanticStoreError("migration_capture_prefix_gap")
        if any(decode_cursor_token(v["cursor_token"]) != (0, 0)
               for v in relevant[0]["input_cursors"].values()):
            raise SemanticStoreError("migration_full_pass_required")
        processing, sources = [], {}
        with closing(self.store.connect()) as conn:
            for batch in relevant:
                row = conn.execute("SELECT receipt_json FROM processing_receipts WHERE batch_id=?", (batch["batch_id"],)).fetchone()
                if row is None:
                    raise SemanticStoreError("migration_processing_missing")
                done = json.loads(row[0])
                self.store.verify_processing_receipt(done)
                processing.append(done)
                for member in batch["members"]:
                    sources.setdefault(member["source_id"], []).append((member, batch["receipt_id"], done["receipt_id"]))
        pending = old_snapshot.data.get("pending_source_batch")
        resolution = None
        migration_id = "migration:" + digest([self.store.generation_id, monitor_state_fingerprint(old_snapshot),
                                               boundary, batch_ids])
        if pending:
            from core.monitor_source import verify_pending_monitor_source_batch
            if pending_batch is None:
                raise SemanticStoreError("migration_pending_source_proof_required")
            verify_pending_monitor_source_batch(pending, pending_batch)
            coverage = []
            for raw in pending_batch.raw_messages:
                matches = [entry for entry in sources.get(raw["source_message_id"], [])
                           if entry[0]["payload"] == raw]
                if not matches:
                    raise SemanticStoreError("migration_pending_content_uncovered")
                _member, captured, applied = matches[-1]
                coverage.append({"source_message_id": raw["source_message_id"],
                                 "capture_receipt_id": captured, "processing_receipt_id": applied})
            resolution = {"disposition": "superseded_by_rebuild", "lineage_id": migration_id,
                          "pending_descriptor": pending, "member_coverage": coverage}
        return self._record_migration(old_snapshot, boundary, relevant, processing, migration_id, resolution)

    def initialize_from_now(self, old_snapshot, batch_id):
        """Install a new selection's explicit start receipt, never pretend EOF.

        This is an empty boundary capture before any source read, not a claim
        that historical messages have been consumed. A pending legacy batch or
        an existing v3 checkpoint can only use the full rebuild migration.
        """
        from core.monitor_state import MONITOR_V3_STATE_SCHEMA
        batch = self.store.get_batch(batch_id)
        if (old_snapshot.schema == MONITOR_V3_STATE_SCHEMA or old_snapshot.data.get("pending_source_batch")
                or batch is None or batch["members"] or batch["source_eof"]
                or batch["purpose"] != "from-now:" + monitor_state_fingerprint(old_snapshot)
                or batch["input_cursors"] != batch["output_cursors"]
                or any(v.get("start_cursor") != v["cursor_token"] for v in batch["input_cursors"].values())):
            raise SemanticStoreError("semantic_initial_boundary_invalid")
        done = self.store.complete_batch(batch_id)
        self.store.verify_processing_receipt(done)
        boundary = {"source_namespace": batch["source_namespace"], "chat_id": batch["chat_id"],
                    "inventory_digest": batch["inventory_digest"], "inventory_revision": batch["inventory_revision"],
                    "cursors": batch["output_cursors"]}
        migration_id = "from-now:" + digest([self.store.generation_id, monitor_state_fingerprint(old_snapshot), batch_id])
        return self._record_migration(old_snapshot, boundary, [batch], [done], migration_id, None)

    def _record_migration(self, old_snapshot, boundary, relevant, processing, migration_id, resolution):
        vectors = boundary["cursors"]
        state = {"semantic_generation_id": self.store.generation_id,
                 "source_namespace": boundary["source_namespace"], "chat_id": boundary["chat_id"],
                 "source_inventory_digest": boundary["inventory_digest"],
                 "source_inventory_revision": boundary["inventory_revision"],
                 "captured_cursors": vectors, "source_cursors": vectors,
                 "last_checked_ts": max(decode_cursor_token(v["cursor_token"])[0] for v in vectors.values()),
                 "capture_receipt": None, "processing_receipt": None}
        receipt = {"schema": "we-groupchat-obsidian.monitor-migration-receipt.v1",
                   "receipt_id": migration_id, "migration_id": migration_id,
                   **{key: state[key] for key in ("semantic_generation_id", "source_namespace", "chat_id",
                        "source_inventory_digest", "source_inventory_revision")},
                   "old_state_revision": old_snapshot.revision,
                   "old_state_fingerprint": monitor_state_fingerprint(old_snapshot),
                   "input_source_cursors": old_snapshot.data.get("source_cursors", {}),
                   "input_captured_cursors": old_snapshot.data.get("captured_cursors", old_snapshot.data.get("source_cursors", {})),
                   "output_source_cursors": vectors, "output_captured_cursors": vectors,
                   "capture_receipt_ids": [b["receipt_id"] for b in relevant],
                   "processing_receipt_ids": [r["receipt_id"] for r in processing],
                   "canonical_receipt_id": migration_id, "outbox_receipt_id": migration_id,
                   "pending_resolution": resolution}
        self.store.record_migration_receipt(receipt, processing)
        return state, receipt

    def _persist_cas_binding(self, receipt):
        # This table records evidence supplied to the sole checkpoint writer; it
        # does not claim the checkpoint CAS succeeded or own source progress.
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS monitor_receipt_bindings(binding_id TEXT PRIMARY KEY,receipt_json TEXT NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO monitor_receipt_bindings VALUES(?,?)", (digest(receipt), json.dumps(receipt, sort_keys=True)))
