# Legacy conservation and independent rebuild verification

This is a source implementation contract. Planning and importing do not read
WeChat, call a provider, enable a subscription, publish a page, change the old
database, activate a generation or reload the app. Explicit navigation publication
uses the canonical reading consumer after verified backups. Native/live acceptance is a
separate operator action. Every inventory and staged manifest is **private**:
it may contain exact historical messages, URLs, paths and full personal notes.
Public status contains bounded error codes and counts only.

## Inventory and capture

`core/semantic/legacy.py` exposes:

```python
inventory = plan_legacy(config)  # already supplied config, never load_config()
# or: plan_legacy(db_path=..., managed_root=..., managed_subdir=...)
inventory = include_review_queue(inventory, explicit_review_queue_root)
private_dict = inventory.to_dict()
inventory = LegacyInventory.from_dict(private_dict)
for bounded_rows in iter_legacy_inputs(inventory, batch_size=100):
    ...
result = import_legacy(store, inventory, generation_id=store.generation_id)
record_legacy_backups(store, inventory, absolute_backup_paths)
topic_item_map = derive_topic_item_map(store, inventory)
# item_paths map canonical Item IDs to publisher-relative paths, e.g. Items/...
publication = record_legacy_resolution(store, inventory, topic_item_map, item_paths)
result = publish_legacy_navigation(store, inventory, publication, publisher)
```

All three input locations must be explicit. `managed_root` is the old vault
root; `managed_subdir` scopes recursive discovery. Only database-recorded topic
paths outside that subdirectory are additionally inspected. No arbitrary
filename or topic title proves ownership. A recorded topic path, the canonical
leading `source_app`/`source_kind` header, or a known generated date-index marker
does. Other files remain unowned and in place; their bytes/hash are inventoried,
but their bodies are not imported as source knowledge.

The old SQLite connection uses URI `mode=ro`, `query_only` and one read
transaction. The importer does not construct `KnowledgeStore`, initialize a
schema or migrate the old database. It inspects available known tables and
their identity columns before selecting rows. All columns of topics, events,
relations, attachment references/objects/attempts/worker state and digest change
records survive in the private manifest, including extension columns and BLOBs.
FTS is a derived index, not another fact source.

`messages_excerpt` has `excerpt_only` provenance. Topic/event summaries without
raw evidence have `derived_summary`; a full preserved Markdown page has
`manual_annotation`. `raw` requires an explicitly stored `raw_text` and an
exact `source_message_id`. An old batch ID or message hash cannot substitute
for an occurrence ID. Absent IDs receive stable opaque legacy references.
Repeated exact occurrence IDs share one source identity; A→B→A is captured in
order as three transitions, while repetition of the current payload adopts its
current version. The complete old row remains separate mapping evidence, so an
old row ID cannot manufacture a new independent witness.

Owned pages retain **all original bytes**, including CRLF, aliases, unknown
frontmatter and prose outside generated regions. Legacy pages have no trusted
machine baseline: no heuristic attempts to find the personal section. Links,
headings and block anchors carry their original syntax and byte spans. Symlinks,
root escape, nonregular files, mutation during a file read and configured file
size limits fail closed. Missing pages stay explicit; loss of a page never
retracts a source fact.

`import_legacy` writes through the canonical `SemanticStore.capture_batch` and
the store transaction API. `legacy_mappings` retains exact row-to-source-version
lineage, file inventory and the prior selection/alias snapshot.
`annotation_preservation` starts at `captured_pending_backup`; source capture
alone does not certify a recovery copy. Old selected chats are historical
metadata and create zero subscriptions. Its result is deliberately
`captured_semantic_pending`, with exact batch IDs, capture receipt IDs and
legacy-reference-to-version mappings. Normal accepted extraction and resolver
jobs must still decide Observations and Items and produce processing receipts.
`include_review_queue` reads the explicitly supplied legacy `pending.jsonl`
append log without initializing the queue. Every line, including later status
transitions for the same queue identity, keeps its exact private bytes and
canonical legacy source mapping. The whole file needs a verified backup; the
separate redacted `Legacy/Review Queue.md` is history, not a fabricated action
against the new reader controls. It does not reactivate delivery.

Before-cutover resume uses a fresh frozen inventory. The importer durably records
the exact changed-row set before capture, adopts unchanged source mappings and
reuses accepted work. It does not replay old A→B→A transitions as new changes
when only a note or queue row changed. `allow_preserved_refresh=True` admits a
new page baseline only after verifying the earlier original backup; prior bytes
remain in annotation history. A backup alone is not an import of intervening
canonical or reader history. [The rebuild coordinator](semantic-rebuild.md)
owns freezing, import, source passes and activation.
Known attachment, URL and file references use the canonical capture
`resources` envelope. Their exact metadata survives with `pending` byte status;
no old path is promoted to an available attachment. The store associates a
reference with an Observation/Item only through cited source evidence.
Attachment/worker/digest metadata keeps empty text and its exact private payload;
it is not converted into message prose or an invented Observation.

## Mapping and publication

`build_publication_plan(inventory, topic_item_map, item_paths)` accepts only
explicit legacy topic reference IDs and canonical Item IDs/paths. Titles never
resolve identity. One Item produces a redirect navigation page, several Items
produce a hub, and an unmapped topic stays pending. A historical page with no
topic binding stays history. An explicit empty Item list requires terminal
non-observed source dispositions for the entire topic family; omission means
unresolved. `derive_topic_item_map` obtains active Items through canonical
Observation/span/source-version joins and never compares titles. Every old heading/block anchor keeps its original
history home and the complete outgoing-reference inventory survives.

The plan writes nothing. It requires a verified backup and preserves the full
original page at its old path. New redacted navigation is a separate `Legacy/`
page. Replacement of an unknown-baseline old page, if requested later, must use
the reading consumer's explicit conflict proposal and backup mechanism. File
deletion is never a semantic delete signal. Exact private URLs are conserved;
new human-readable navigation uses `core.url_safety`.
`record_legacy_backups` acknowledges only exact coordinator-created regular
copies that still match the preserved original page. `record_legacy_resolution`
verifies every target Item actually cites a captured version from that old
topic/event/page family. It stores topic, path and anchor mappings in the
canonical database. Superseded mappings remain explicit history.

`publish_legacy_navigation` requires the exact frozen plan, verified recovery
copies and a publisher using the same canonical database and generation.
It writes exact whole-page recovery bytes to private `.wgo/legacy-history/*.bin`
objects with atomic collision checks. Those bytes are never new readable
projections. The old page stays untouched; the `Legacy/` navigation page links
to that original vault path and to `WGO/` Item paths. Navigation uses
`ProjectionPublisher.publish_document`, so an unknown file at the target path
becomes a preserved conflict proposal instead of being overwritten. Replay
adopts identical copies and existing managed navigation. The result reports
`published`, `semantic_pending` or `projection_pending`, plus actual projection
receipts and history-copy identities. This does not activate a generation.

## Independent verifier input

`RebuildVerifier(store_path, staged_manifest, projection_root=...).verify()`
reads a SQLite snapshot and the exact staged membership manifest, not producer
success counters. It does not initialize the canonical schema. The manifest
binds `generation_id`, ordered `expected_batches` (`batch_id` and
`member_version_ids`), `source_inventory` (`complete`, `digest`, `revision`),
the per-chat closed cutoff `h1` (`source_namespace`, `chat_id`,
`inventory_digest`, `inventory_revision`, `cursors`),
`embedding_profile_id` and `index_generation_id`. Optional `legacy_inventory`
and `publication_plan` carry the private conservation inputs above.

When the rebuild intentionally makes several complete source traversals,
`capture_passes` contains their full immutable snapshot receipts: unique
`receipt_id`, `generation_id`, complete `batches` receipt dictionaries, `h1`,
`capture_seq`, and `source_inventory`. A batch must carry the exact
`purpose="snapshot:<receipt_id>"`. Only a declared, complete pass can restart
its cursors at `[0,0]`; every pass must have contiguous per-chat pages, exact
member identities and terminal EOF. The final pass must exactly equal the
staged final H1/inventory. All earlier captured versions still require their
own valid semantic dispositions. An undeclared cursor reset remains a failure.
`abandoned_capture_passes` explicitly binds unique interrupted snapshot IDs,
`complete: false` and every durable batch receipt from that prefix. Such a
prefix starts from zero, has continuous cursors and unchanged inventory, but
cannot supply final H1. A later complete full pass is required. The verifier
still checks all retained members and processing evidence. Each frozen legacy
import has its own declared `legacy_capture_passes` entry containing its exact
batch IDs; it cannot authorize a reset in a live source stream.

Verification distinguishes invalid/missing evidence, source work remaining,
semantic work remaining, dense-index work remaining, and projection work
remaining. A missing current Item projection cannot be called complete. A
current, explicit user decision to retain personal text is a separate
conservation disposition, not an assertion that the old machine projection is
current. Failure reports never echo IDs, text, URLs or paths.
Processing proof is independently rebuilt from each ordered core source, its
accepted extraction disposition, and the cited Observation's historical Item
revision. Exact extraction/transaction/outbox sets and `proof_fingerprint` must
match the stored receipt. Later identity changes do not invalidate a previously
proved historical membership. Every transaction retains typed actions and real
outbox IDs; Item revisions retain their change events. A superseded projection
outbox requires a later completed revision with the same receipt; index outboxes
cannot use that shortcut.
Accepted resolver actions without their own receipts require an explicit
`proposal_supersessions` proof. The verifier independently checks the exact
batch/Observation scope, older read set, retained artifact hashes, source
extraction lineage, committed replacement and historical resolved memberships.
It does not infer supersession from newer Items or successful counts. Missing
proof, partial resolution and unknown decisions remain blocking conditions.
Migration aggregate receipts borrow the exact union of their referenced
processing outboxes. They must preserve the ordered processing/capture receipt
lists and their own canonical transaction; a transaction ID cannot substitute
for an actual outbox ID.
Captured resource metadata participates in source-version identity; every
resource cited by an Observation must survive both its Observation and Item
associations. Legacy file bytes, private history copies, mapping cardinality,
references and actual navigation receipts are verified independently.
Coordinator manifests additionally bind `rebuild_reading_required` and
`required_brief` (edition ID, local day, timezone, cutoff, content revision,
provider route and bounded context size).
Every canonical View requires its current actual projection. Brief acceptance
reads the durable original candidate inputs, item/preference revision bindings,
accepted attempt/provider artifacts, evidence and historical text; a successful edition also
needs its matching current file. Missing/deferred/review is pending. An explicit
silent disposition may pass only with preserved inputs and its reason, or
independently confirmed absence of eligible changes. Store-only tests without
an edition cutoff do not assert that a full reader rebuild is complete.
The input's `before_revision` and exact `contributing_change_ids` bind the
canonical revision interval, so a later navigation-only revision cannot hide
earlier content from that day. Compaction artifacts retain exact original-input
bindings and contiguous bounded-page coverage; loss of accepted writer evidence
cannot be repaired by an edition success flag.
Extraction attribution is independently recomputed from each captured payload;
even a rehashed job cannot substitute a name or speaker ID. New Brief v3 input
hashes bind the complete canonical attribution array, and v2 compaction page
coverage includes its non-null strings. Writer/selection/compaction versions
must match that input contract. The previous Brief v2 contract is checked as
its exact historical shape, never treated as attribution-aware input.
Full rebuild also requires an accepted Body artifact (`body-plan-v6` or
`body-paged-v6`; retained v2/v3/v4/v5 artifacts remain supported)
for each current Item body. An unchanged body can reuse an earlier accepted
revision under the same generation/provider route only when its blocks and
unit fingerprints are identical. Missing, unknown or conflicting versions
remain explicit pending evidence; a ready file alone does not supply a model
artifact.

Focused synthetic checks live in `tests.test_semantic_legacy` and
`tests.test_semantic_verification`. They use private temporary fixtures and do
not constitute live source, provider, installation or activation evidence.
