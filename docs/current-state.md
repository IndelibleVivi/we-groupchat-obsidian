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
