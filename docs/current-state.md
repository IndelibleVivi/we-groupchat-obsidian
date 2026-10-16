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

Watch evaluation now limits a local correction to the withdrawn predicate and
its proven scope or explicit restatement lineage, preserving unrelated claims
from the same material. Incomplete scope cannot combine separate trials, and a
newer overlapping contrary report prevents an unconditional current-state match.
Incremental BodyPlans may rewrite automatic prose when new details join its
stable block; unchanged blocks, exact quotations and manual edits remain
protected. Current BodyPlan artifacts use v6, while accepted v2–v5 evidence
remains verifiable. These are candidate source behaviors, not evidence of a
private generation being repaired, re-rendered or activated.
The canonical reading adapter now separates quote provenance from mandatory
full-message body wording. Connected exchanges can be composed with selected
quotations while every source remains reachable; remaining citations use a
collapsible source callout. Extraction and resolution guidance retains whole
scenes without treating message or Observation counts as required page counts.
Structural checks do not establish editorial quality or owner acceptance.
New extraction pages include the preceding bounded page as ordered non-core
context instead of only its last two messages. Existing page jobs keep their
frozen inputs and outcomes; this change does not silently re-extract old work.

Known deferred decisions now admit dependency-bound replanning and later-batch
knowledge processing, while the applied prefix remains blocked until its gap
is proved complete. More than 12 possible candidates receive bounded detailed
refinement; remaining ambiguity stays explicit. The full catalog scan still
has linear model-call cost (167 coarse calls for a synthetic 4,000-Item catalog),
and real-provider latency has not been established by this source change.
The candidate also includes the exact 4.1.15/270102/arm64 protected-key profile
and managed self-signed leaf-anchor compatibility from the main source line;
the dated native evidence retains its original scope in
[recovery-acceptance.md](recovery-acceptance.md).
