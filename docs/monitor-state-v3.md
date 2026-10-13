# Monitor state v3

Status: source contract; this document does not record an installed or live
migration. `MonitorStateStore` remains the only production checkpoint writer.
The generation database owns the capture, canonical processing and outbox
transactions. Its caller verifies durable receipts before passing their exact
bindings to this store; structural validation here is not independent proof
that a database transaction exists.

## State and ordering

The on-disk schema is `we-groupchat-obsidian.monitor-state.v3`. `revision` is a
positive integer owned by the store. `MonitorStateSnapshot.schema` exposes the
parsed schema; `snapshot.data` excludes `schema` and `revision` as before.
Unversioned, v1 and v2 files remain readable until explicit installation. The
previous v1/v2 reader rejects v3. There is no automatic downgrade or migration.

The required data fields are:

```json
{
  "semantic_generation_id": "generation-example",
  "source_namespace": "source-example",
  "chat_id": "chat-example",
  "source_inventory_digest": "<64 lowercase hexadecimal characters>",
  "source_inventory_revision": 1,
  "captured_cursors": {
    "logical-shard-example": {
      "generation_id": "physical-generation-example",
      "cursor_token": "[120,7]",
      "start_cursor": "[100,0]"
    }
  },
  "source_cursors": {
    "logical-shard-example": {
      "generation_id": "physical-generation-example",
      "cursor_token": "[110,4]",
      "start_cursor": "[100,0]"
    }
  },
  "last_checked_ts": 110,
  "capture_receipt": null,
  "processing_receipt": null,
  "migration_receipt": "<the complete migration binding below>"
}
```

`source_cursors` is the semantic applied continuous prefix. `captured_cursors`
is the durable captured prefix. The state is per chat; namespace and chat are
explicit immutable receipt bindings, not source/attachment permission. Both
vectors must have the same nonempty logical-shard key set, physical generations
and optional `start_cursor` fields. `start_cursor` keeps its existing from-now
boundary and cannot change during ordinary commits. Tokens are JSON arrays of
exactly two nonnegative integers, ordered using
`core.source_adapter.decode_cursor_token`; booleans, floats and coercible
strings are invalid. Each applied position must be at or below captured, and
each present start must be at or below its position.

`last_checked_ts` is the maximum timestamp in the applied vector. It is only
compatibility evidence; neither capture nor processing may use it to skip
source rows. Capture commits leave it unchanged. It is an exact nonnegative
integer in v3. State and receipt objects reject missing required fields,
unknown fields, unknown schemas, nonfinite numbers and wrong scalar types.

## Python entrypoints

```python
old = store.read()
old_fingerprint = monitor_state_fingerprint(old)
installed = store.install_v3(old.revision, new_state, migration_receipt)
captured = store.commit_capture(installed.revision, captured_cursors, capture_receipt)
applied = store.commit_applied(captured.revision, source_cursors, processing_receipt)
```

All writes hold the existing checkpoint lock, reread and validate the current
state, check the exact revision, validate the binding, and publish through
`StateStorage`. An expected revision must be a nonnegative integer, not a bool.
The execution lock still serializes a complete monitor run independently from
the checkpoint lock. `inspect()` never prepares storage, writes or changes
permissions. Corrupt, symlink and nonregular files fail closed before migration.

`new_state` supplies all required data fields except `migration_receipt`, which
`install_v3` adds. It can also supply that field if it exactly equals the
argument. Its initial `capture_receipt` and `processing_receipt` are null: the
migration binding is the initial authority for both output vectors. Subsequent
commits replace only the appropriate receipt and vector. A migration of an
already-v3 state requires a different `semantic_generation_id`; ordinary calls
cannot change generation, scoped inventory digest, namespace, chat or shard membership.
The inventory ledger revision is a global observation counter, not the scoped
identity: capture may advance it monotonically while the digest stays exact.
Processing an earlier durable capture may cite its earlier observed revision;
it keeps the newest checkpoint inventory revision. State revision CAS still
binds the entire input checkpoint, and capture/processing receipts retain their
actual observation revisions.

The caller must resolve/read the immutable durable receipt under the generation
DB's own authority and provide the exact dict below. No `success: true`,
projection status or arbitrary new-state dict substitutes for these bindings.
The store does not import or query the generation DB and does not make an
external service call.

## Capture receipt

Every field is required and no extra fields are accepted:

```json
{
  "schema": "we-groupchat-obsidian.monitor-capture-receipt.v1",
  "receipt_id": "durable-capture-receipt-id",
  "batch_id": "durable-capture-batch-id",
  "semantic_generation_id": "generation-example",
  "source_namespace": "source-example",
  "chat_id": "chat-example",
  "source_inventory_digest": "<inventory digest>",
  "source_inventory_revision": 1,
  "state_revision": 2,
  "input_cursors": "<exact current captured vector>",
  "output_cursors": "<exact proposed captured vector>"
}
```

`state_revision` binds the CAS input. The input vector must exactly equal the
current captured vector; output must exactly equal the API argument and must
not move any shard backwards. A verified empty/EOF batch can leave the vector
unchanged, but still needs its own durable receipt and batch identity. Receipt
IDs must not reuse the current capture or processing identity.

## Processing receipt

```json
{
  "schema": "we-groupchat-obsidian.monitor-processing-receipt.v1",
  "receipt_id": "durable-processing-receipt-id",
  "processing_id": "durable-processing-transaction-id",
  "capture_receipt_ids": ["durable-capture-receipt-id"],
  "canonical_receipt_id": "durable-canonical-transaction-id",
  "outbox_receipt_id": "durable-projection-outbox-transaction-id",
  "semantic_generation_id": "generation-example",
  "source_namespace": "source-example",
  "chat_id": "chat-example",
  "source_inventory_digest": "<inventory digest>",
  "source_inventory_revision": 1,
  "state_revision": 3,
  "input_cursors": "<exact current applied vector>",
  "output_cursors": "<exact proposed applied vector>"
}
```

The nonempty, duplicate-free `capture_receipt_ids` identify the durable capture
coverage consumed by this processing transaction. Canonical, processing and
outbox identities are all required. The store validates exact input/output and
`current applied <= new applied <= captured`; the generation DB must separately
verify that the named receipts cover the continuous prefix without a gap.
Human projection conflicts or an undelivered outbox do not block applied
progress once these durable transactions exist. No projection-delivery boolean
is part of this schema. A prior capture receipt can be consumed after later
capture commits; requiring only the most recent receipt would incorrectly
exclude an existing captured backlog.

## Explicit installation/migration receipt

```json
{
  "schema": "we-groupchat-obsidian.monitor-migration-receipt.v1",
  "receipt_id": "durable-migration-receipt-id",
  "migration_id": "durable-migration-transaction-id",
  "semantic_generation_id": "generation-example",
  "source_namespace": "source-example",
  "chat_id": "chat-example",
  "source_inventory_digest": "<inventory digest>",
  "source_inventory_revision": 1,
  "old_state_revision": 1,
  "old_state_fingerprint": "<monitor_state_fingerprint(old)>",
  "input_source_cursors": "<exact old source_cursors, or {} when absent>",
  "input_captured_cursors": "<exact old captured_cursors, else old source_cursors, else {}>",
  "output_source_cursors": "<exact installed applied vector>",
  "output_captured_cursors": "<exact installed captured vector>",
  "capture_receipt_ids": ["durable-rebuild-capture-receipt-id"],
  "processing_receipt_ids": ["durable-rebuild-processing-receipt-id"],
  "canonical_receipt_id": "durable-rebuild-canonical-transaction-id",
  "outbox_receipt_id": "durable-rebuild-outbox-transaction-id",
  "pending_resolution": null
}
```

The fingerprint is SHA-256 over canonical JSON of `schema`, `revision`,
`existed` and the complete parsed `data`. For a snapshot returned by a write,
positive revision means the resulting file exists even when `snapshot.existed`
reports that the file was absent before that write. It is an identity comparison, not a
signature or proof of durability. It therefore binds unrelated state metadata
and the complete old pending descriptor too. Receipt arrays are nonempty and
duplicate-free; installation has an explicit durable generation transaction
even for an empty semantic result.

If v2 contains `pending_source_batch`, `pending_resolution` must instead be:

```json
{
  "disposition": "superseded_by_rebuild",
  "lineage_id": "durable-rebuild-or-adoption-lineage-id",
  "pending_descriptor": "<complete exact old pending_source_batch object>",
  "member_coverage": [
    {
      "source_message_id": "<one exact pending member, in original order>",
      "capture_receipt_id": "<one of capture_receipt_ids>",
      "processing_receipt_id": "<one of processing_receipt_ids>"
    }
  ]
}
```

`disposition` is exactly `adopted` or `superseded_by_rebuild`. Existing canonical
pending-descriptor validation runs before installation, including checksum and
checkpoint binding. The exact original member sequence must be covered once;
missing, reordered, duplicate or invented members, mismatched descriptors,
unknown receipt references or a missing lineage fail closed. The generation DB
owner must verify each member really belongs to the named durable capture and
processing receipts. Pending intent cannot be erased based on an unverified
claim that rebuilding succeeded. The migration receipt retains the complete
content-free descriptor after successful installation.

## Restricted metadata and reset

For v3, existing `commit(expected_revision, new_state)` and `update(mutator)`
may change only `ai_failure_count`, `ai_last_error_code`, `ai_last_error_ts`,
`ai_next_retry_after`, `last_topic_key` and `last_notified_ts`. Counts are
nonnegative integers, timestamps are finite nonnegative numbers, error codes
are bounded lowercase identifiers and topic keys are nonempty strings. These
calls cannot replace/delete either vector, any binding or any receipt. `update`
uses a deep copy so nested mutations cannot bypass the comparison.

The legacy reset-to-now remains destructive for legacy v1/v2. It fails closed
on v3 because it removes protected fields. A v3 reset must be an explicit
new-generation installation with durable migration receipts and exact pending
coverage, using `install_v3`; this store adds no implicit reset or source read.

## Explicit checkpoint rollback

`restore_checkpoint(expected_revision, backup_bytes, rollback_receipt)` is the
only explicit schema-downgrade exception. It accepts a currently-v3 checkpoint,
parses the exact UTF-8 backup bytes with the same state parser, and restores all
backup data with a new `current revision + 1`. The original backup revision must
be older than the current one. v1/v2/v3 schemas are preserved; an unversioned
legacy backup is restored as v1. A v2 backup must also pass the canonical pending
descriptor/checkpoint validation. The complete pending descriptor remains
unchanged. A v3 backup must belong to the same namespace and chat.

The required rollback receipt is:

```json
{
  "schema": "we-groupchat-obsidian.monitor-rollback-receipt.v1",
  "receipt_id": "durable-rollback-receipt-id",
  "rollback_id": "durable-rollback-transaction-id",
  "run_id": "rebuild-run-id",
  "journal_id": "durable-rebuild-journal-id",
  "semantic_generation_id": "<currently installed generation>",
  "source_namespace": "<current namespace>",
  "chat_id": "<current chat>",
  "source_inventory_digest": "<current inventory digest>",
  "source_inventory_revision": 1,
  "state_revision": 7,
  "current_state_fingerprint": "<monitor_state_fingerprint(current)>",
  "backup_bytes_digest": "<SHA-256 of exact backup bytes>",
  "backup_state_fingerprint": "<monitor_state_fingerprint(parsed backup)>",
  "retained_raw_store_id": "retained-generation-database-id",
  "retained_raw_receipt_id": "durable-raw-retention-receipt-id",
  "retained_capture_receipt_ids": ["durable-capture-receipt-id"],
  "retained_captured_cursors": "<exact current captured vector>",
  "replay_receipt_id": "durable-replay-plan-receipt-id",
  "replay_destination_id": "durable-replay-destination-id",
  "replay_from_cursors": "<exact backup applied vector, or {} when absent>",
  "replay_through_cursors": "<exact current captured vector>"
}
```

The retained list must contain the current capture receipt, or all installation
capture receipts when there is no later capture. Both through/captured vectors
bind the current durable capture boundary, including work after the rebuild
handoff boundary H1. The durable journal/generation DB owner must verify the
complete retained capture set and the named replay destination; this store
cannot infer that proof merely from a list of identifiers. Backup bytes do not
authorize deleting raw capture data. Restore performs no raw deletion, source
read, projection write or generation-DB mutation.

The caller persists the receipt and outcome in its existing journal. This store
adds no checkpoint sidecar and does not insert rollback metadata into the old
checkpoint. The exact current revision/fingerprint binding prevents receipt
replay even after restoring another v3 checkpoint. After restoring v1/v2, a
second restore attempt also fails the current-v3 precondition. Ordinary
`commit`, `update` and initialization cannot use this exception. Symlink,
nonregular, corrupt, unknown-schema and nonfinite current states fail before
backup replacement; an invalid backup or any receipt mismatch leaves the
current bytes intact. Publication still uses `StateStorage` atomically.

## Verification boundary

Synthetic tests in `tests/test_monitor_state_v3.py` exercise capture/applied
separation, receipt bindings, pending migration, schema downgrade rejection,
parse-before-migrate, atomic failure, nonmutating inspection, stale revisions
and concurrent CAS. Rollback tests cover legacy/v2/v3 restoration, retained
raw/replay bindings, stale or reused receipts, invalid backups and atomic
publication failure. A subprocess executes the immutable pre-v3 reader against
a synthetic v3 file to demonstrate the old-reader failure boundary. These tests
are source evidence only and do not claim a real migration, database receipt
validation, native Windows activation or live macOS acceptance.

来源中的 `resources` 是 WeChat 文件/图片元数据，source bridge 显式转换成
semantic resource references。引用身份绑定 source namespace、message ID 与资源位置，
status 为 `ready_metadata`、location 为空；声明的 hash 不用于跨消息合并或证明
附件已下载。原始 resource metadata 仍原样保存在 source payload，这条路径不读取附件 bytes。
