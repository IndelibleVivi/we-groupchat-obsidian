# Current source state

This checkout contains the semantic-generation rebuild candidate on top of the
October Ops source line. Package metadata remains `v0.1.0-alpha.1`; no new release
is implied by this document.

- New installations and unmigrated state keep the legacy topic/event path.
  An accepted active-generation manifest selects the shared full-rebuild/live
  semantic engine; a half-written cutover blocks startup.
- The candidate adds immutable source versions, Observations/Items, claim and
  identity operations, hybrid search, explicit local or Cloudflare embedding and
  local or remote vector indexing, captured member attribution, Markdown bodies
  with tables/lists/quotations, Obsidian reading controls, briefing editions,
  delivery evidence and v3 checkpoints. Provider preparation is bounded and
  parallel; canonical identity decisions and publication remain serialized.
- Product-specific private adapters are outside this public repository. Generic
  resource capture, context retention by explicit injection, CAS and v3 snapshot
  transport remain supported building blocks.
- Source tests, complete migration verification, application build, installation,
  activation, actual incremental acceptance and any remote publication are
  separate evidence. This file makes no claim about a particular live runtime.
- Windows support remains the shared foundation described in
  [WINDOWS-PORT-MAP.md](WINDOWS-PORT-MAP.md). New semantic source modules do not
  imply Windows source, app, migration or notification support.

Use [semantic-workflow.md](semantic-workflow.md) for reading behavior and
[semantic-rebuild.md](semantic-rebuild.md) for migration, resume and rollback.
Private run receipts and local operator continuity are not tracked here.

Run-journal storage now uses compact v2 metadata and immutable private payloads.
The locked `compact-journal` command upgrades interrupted inline v1 runs without
starting source/model processing; original bytes remain available for recovery.
This is source capability, not evidence that any private run has been converted
or activated. See the migration runbook for limits and recovery.

An unpinned, pre-activation run whose plan expired or whose runtime root moved can
be relocated to another already-mounted private local root by the explicit
`relocate` recovery. It copies run/generation state (SQLite via Online Backup
including WAL), rebinds only the operational plan, adopts `semantic_runtime_root`
through the locked ConfigStore patch and leaves a retirement record for the
retained original root, preserving the run/generation, raw captures, jobs and
immutable evidence. It never fabricates a missing destination root and admits a
changed TEXT/DECISION binding only while the generation has zero jobs. This is
source capability, not evidence that any private run was relocated.
An explicitly configured unavailable runtime root blocks app, monitor, health
and legacy dispatch with `semantic_runtime_root_unavailable`; it is not treated
as an unmigrated installation. Relocation verification streams copied files,
and SQLite retries publish a fresh WAL-aware Online Backup atomically.

Local cosine now keeps vectors in a private disposable SQLite disk cache, and
retrieval reads bounded vector payload pages instead of retaining all vectors
and receipt payloads in Python. This candidate change does not establish a
whole-migration RAM bound or real private-data acceptance.
Full-rebuild extraction preparation also releases each durable artifact rather
than collecting every batch's observations in the scheduler's result list.
Resolution reads the accepted result for one batch at a time.

Independent verification now uses a private temporary disk snapshot and scoped
SQL reads instead of an in-memory database and whole-corpus payload dictionaries.
Computed vectors and receipt anchors use disposable disk scratch. This preserves
complete checks and WAL consistency; caller manifest metadata, identity sets and
the largest individual record still contribute memory. Temporary disk capacity
and full private-run acceptance remain separate operational requirements.

Correction attention considers earlier semantic changes even when the same Item
has a later detail or provenance-only revision before polling. Existing accepted
or unknown device/channel receipts remain authoritative across later revisions;
already-known corrections do not become new attention.
