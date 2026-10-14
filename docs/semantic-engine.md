# Semantic processing, search and temporal watches

`core/semantic/engine.py`, `retrieval.py` and `watches.py` consume already
captured canonical data. They do not open WeChat, discover credentials, enable
providers or activate a generation. Their source implementation and synthetic
tests are separate from installation and live acceptance.

The extraction contract treats independent technical discussion, life scenes,
stories, gossip and quotations equally. Every Observation has a core source
member; overlapping context alone cannot produce another Observation. Text
providers select supplied source and span IDs. The program assigns contiguous
2,000-character spans in the exact redacted input; providers cannot invent IDs,
offsets, or excerpts. The engine assigns stable unit, claim and Observation IDs.
Facts, steps, quotations, scenes,
counterevidence and resources retain provenance. Unknown claim qualifiers stay
unknown; matching names do not establish entity identity.
A quote unit's `text` is only the contiguous verbatim words of one cited message.
Neither the prompt nor the correction pass allows a self-added speaker label or
name, quotation-mark wrapping, a Markdown `>` blockquote marker, or words joined
from separate messages; the strict grounding check rejects them. Attribution
stays in captured speaker metadata and in scene/fact units, never inside the
quote text.

The engine passes captured **source version IDs**, not message occurrence IDs.
`core/semantic/attribution.py::source_attribution(namespace, chat_id, payload)`
is the shared, source-free attribution owner for extraction and reading. It
returns `speaker_id` and `speaker_name`, each nullable. Captured `raw_sender_id`
produces an opaque identity scoped to the source namespace, stable across
dates and chats. A local `sender_id` is usable only with a captured
`source_envelope.db_shard_id` and remains scoped to that namespace, chat and
shard. No stable ID means an unknown speaker identity, even when an observed
name is available. Matching names and adjacent messages never establish identity.
The name is that message's captured `sender` label, with canonical URL
redaction; an account-ID fallback is not exported as a name. The helper makes
no contact lookup and does not reconstruct past name changes. Exact account
IDs remain in the private captured payload.

Speaker means the message writer or forwarder, distinct from an assertion's
subject, a quoted author and a mentioned third party. Explicit `subject_anchor`
and case anchors remain scoped to namespace and chat; material IDs remain
namespace scoped. A claim may select a cited source's speaker ID for a clearly
first-person self-report, or its explicit subject anchor. Unknown reported
subjects stay null. Neither the engine nor store auto-fills a claim subject
from its speaker. Units retain readable attribution and reported/quoted
boundaries. Both validators reject a claim that borrows an ID from an uncited
source. Claim `observed_at` must equal one cited source's captured time.
Reported historical phases remain explicit; a model-generated timestamp
cannot manufacture a later recovery test.

New extraction jobs bind these fields under
`semantic-extraction-v2-attribution`; bounded pages use
`semantic-extraction-pages-v2-attribution`. An existing accepted v1 candidate
can replay its exact frozen input and artifact after a commit interruption;
its former subject mapping is not reinterpreted as new attribution. This
limited branch exists for the current source-candidate recovery path, not a
deployed-generation migration, and can be removed when candidates containing
v1 jobs have been retired. An unaccepted v1 parent remains
`semantic_extraction_upgrade_pending`; its pages and input are retained for
explicit reconciliation or a fresh candidate. Unknown parent/page outcomes
remain unknown and block upgrade. There is no silent provider replay under
a new prompt version, and unversioned extraction fixtures receive no exemption
from the canonical input binding.

Long captures use accepted `extract_page` artifacts, with input span packets
bounded to 22,000 JSON bytes per page. A source may span several pages. Previous
message context is marked non-core and cannot independently produce another
Observation. The parent extraction artifact aggregates validated pages before
canonical commit. An unclassified fragment keeps its whole source unread even
when other fragments produced Observations. Only explicit supported terminal
dispositions can finish a source with zero Observations. Empty
`legacy_resource` and `legacy_metadata` rows receive the program disposition
`unsupported_type` because their complete private metadata remains captured;
empty pages and messages do not receive that assumption.

Before an unaccepted parent aggregates, every member still `unclassified` gets
at most one bounded adjacent-context coverage re-read (`extract_coverage`, phase
`bounded-adjacent-context-v1`). Only the pending members are core and citable;
the nearest neighbouring messages of the same frozen batch are attached as
non-citable, character-bounded context text without span identities (at most four
message sources and 8,000 characters per page), so no earlier terminal
disposition is overwritten. The coverage pass reuses the shared page generation
and known-output correction path, binds exact source/span/context inputs, reuses
accepted page artifacts byte-for-byte and never reopens the protected WeChat source. It
contributes a new Observation only when that Observation cites a pending core
member, and it replaces a pending disposition only when every still-unclassified
fragment of that member reaches a terminal state. A still-unclassified or unknown
coverage result keeps the member unread with no processing ACK; a distinct
coverage job is never re-sent under a new prompt, and no uniform `noise` or
forced `context_only` disposition may bypass the gap. An already accepted parent
keeps its exact artifact and receipt.

Relations describe evidence (`replay_same_source`, `same_material`, `restates`,
`independent_support`, `refines_scope`, `changes_over_time`, `disputes`,
`corrects_or_retracts`, `unresolved`). Actions describe data changes. They are
separate vocabularies. A merge can combine append-detail, additional-evidence,
correction, scope-refinement, continuation and resource-attachment modes.
Same material forwarding is not another independent witness. Replay is not a
semantic update. Similar vectors do not establish a transitive Item identity.

The synchronous provider boundary is documented in
[semantic-providers.md](semantic-providers.md). TextProvider produces text and
proposed structured actions; DecisionProvider chooses only among supplied
typed options. Explicit TextDecisionProvider configuration is the only text
fallback. Invalid or failed output is an error, never an automatic negative
match. Accepted artifacts are persisted before canonical commit and reused on
resume. Network calls run outside database transactions.

`SemanticRuntime` composes the provider roles with the bounded admission owner
in `core/semantic/scheduling.py`. Full rebuild extraction runs independent
captured batches concurrently; batches sharing a core source version run in
ordered waves so later batches adopt the first owner's exact result. Any
extraction failure stops the pass before resolution or a later processing ACK.
The global Item resolver, consolidation, View actions and canonical commits
retain their serial revision checks. Decision concurrency is a ceiling across
callers, not permission to race identity-changing proposals.

Each parallel extraction reads core ownership only for the current batch's
members, in bounded key lookups, rather than copying the whole ownership table.
Observation citations and explicit disposition keys are disjoint. Unclear
conversational fragments can retain a reasoned `context_only` disposition and
their exact raw source without inventing a claim or an Item. This differs from
`unclassified`, which means processing is unfinished and still blocks ACK.
Meaningful life scenes, stories and quotations cannot become noise merely
because they are nontechnical. The prompt asks for the evidence's dominant
language while retaining original quotations and technical terms.

Body preparation runs concurrently in pages of at most 100 Items. Each validated
provider artifact is durable before publication; `prepare_canonical_item` holds
no projection-root lock while calling the provider. `publish_prepared_item`
rechecks the current Item revision and uses the existing serial publication,
manual-note protection and outbox ACK path. Active targets publish before old
identity redirects. A publication interruption reuses already accepted Body
artifacts on the next invocation.

On a stopped admission phase, work already dispatched drains and keeps its
accepted results. A scheduler-proven unsent attempt returns to `pending`,
retaining its attempt record. `outcome_unknown=False` on an arbitrary provider
exception is not that proof. Sent failures and unknown outcomes retain their
existing JobStore state; unknown work requires explicit reconciliation. There
is no automatic provider retry or fallback. Extraction may correct a known
received JSON validation failure at most twice: each rejected result closes its
durable attempt, and the next claim uses a distinct physical request ID with
the error and rejected JSON as untrusted feedback for the same source spans.
Accepted pages are reused; unknown outcomes, unsent work and provider failures
cannot enter this correction loop. Exhaustion still stops extraction without
canonical acceptance or ACK. Waiting for configured
rate capacity renews the owned job lease without holding a database transaction.
The role defaults, explicit rate ceilings and process scope are documented in
[semantic-providers.md](semantic-providers.md#bounded-provider-admission).

`SemanticEngine(store, text_provider, decision_provider, *, retriever=None,
jobs=None, worker_id=None, view_ids=())` exposes these synchronous operations:

| Operation | Durable result |
| --- | --- |
| `extract_batch(batch_id)` | `job_id`, extraction `receipt`, and canonical-shaped `observations`; no Item decision yet. |
| `resolve_batch(batch_id, extracted=None, plan_revision=1)` | Bounded Observation proposals, exact typed choices, canonical receipts, and a processing receipt only after the whole batch is complete. |
| `process_batch(batch_id, plan_revision=1)` | Extract, then resolve through the same APIs. |
| `consolidate(run_id)` | Global identity review proposing explicit merge, split, relation, or no change; lineage is conserved. |
| `reconcile_views(run_id)` | Paginated View induction, reuse of existing definitions, and revision-bound membership writes. |

An offline rebuild can extract every captured batch, prepare their embeddings,
then resolve each batch against that globally visible evidence. After resolving,
call `consolidate` and `reconcile_views` with phase-specific run IDs. Reuse an
exact run ID to resume; use a new run ID for a new C0/C1 pass. `plan_revision`
selects an explicit new resolver attempt after a changed canonical read set;
an accepted stale plan is never silently regenerated under its old job ID.
After a replacement commits, the store atomically retains a supersession proof
for the old plan's exact batch/Observation scope and actual resolved memberships.
The old proposal and accepted choice become `done` with that proof receipt;
their artifacts remain unchanged. Replaying the old resolver context adopts
the verified replacement receipt without another text/decision call. A stale
read set by itself, an unknown choice, or a replacement that leaves any input
Observation unresolved cannot terminate the old job.

Resolver pages contain at most 24 Observations and 30,000 JSON bytes before
candidate context. Hybrid recall is followed by a complete paginated scan of
all Item identity scopes. Possible omitted candidates prohibit `CREATE_ITEM`.
Selected long Items get accepted, bounded identity-context summaries covering
all detail parts; these summaries never replace canonical body units. The final
proposal uses content, View definition, View override and global revisions.
View creation and membership are separate recoverable stages: the program
binds the already accepted View choice to its exact mechanical membership
actions after refreshing the read set. User definitions and overrides survive.

`status=deferred` and `status=unclassified` leave raw and extracted evidence
searchable without a processing ACK. Invalid shape, unprovided references,
missing core evidence, nonfinite or ungrounded timestamps, stale read sets and
unknown sent outcomes fail closed with content-free codes. An accepted artifact
is replayed rather than requesting new model output.

Search combines Chinese lexical/detail matching and profile-bound embeddings
with reciprocal rank fusion. Display limits are not evidence-completeness
limits. Structural reply/case/relation paths and directed correction
dependencies bypass ranked candidate limits. Unconfirmed asynchronous vector
mutations remain pending; their canonical tail still participates locally.
Query results report unread and unindexed coverage rather than asserting
absence from a top-k miss.

`HybridRetriever(store, embedding_provider=None, vector_index=None, *,
profile=None, decision_provider=None, jobs=None, worker_id=None,
shared_lock_root=None, scheduler=None)` uses the canonical connection for its lexical detail
index, complete embedding profile bindings, immutable vector artifacts and
mutation receipts. It supports the local and Cloudflare `VectorIndex` protocol;
only opaque vector IDs and the adapter's fixed metadata allowlist leave the
database. A changed model, dimensions, normalization, pooling, templates or
chunker belongs to another profile.

- `prepare_index()` reads one canonical revision's documents and stored
  embeddings; its result is temporary and cannot cross a canonical revision.
- `index_pending(limit=100, preparation=None)` prepares embeddings concurrently
  and submits bounded current documents. Equal text shares one embedding job;
  each complete distinct text is one admitted provider call. Mutation and
  confirmation retain the shared native index lock and pending-mutation guard.
- `SemanticRuntime.index_all()` reuses one preparation for its bounded pass,
  instead of reading every document and embedding again for each 100-row page.
  Every embedding-record write and index intent checks that revision. Pending
  counts include both missing embeddings and unconfirmed index mutations, so
  an indexed first page cannot hide later unembedded documents.
- `confirm_pending()` checks receipt completion. Index completion never implies
  semantic ACK.
- `search(query, lexical_limit=40, dense_limit=40, rerank_limit=10,
  display_limit=5, source_ids=(), case_ids=(), exact_ids=(), correction_ids=())`
  returns typed source, Observation, Item, body, claim, relation and change hits.
  Silent and unclassified sources participate. Reranking accepts 8–12 documents,
  persists validated typed scores, and resumes without another decision call.
- `relation_candidates(...)` returns structural candidates independently of
  display limits. Exact source/reply/case entrances traverse directed claim and
  Item dependencies; traversal never creates an identity union.
- Results expose canonical revision, embedding/index pending counts,
  unread/unclassified counts, source completeness and explicit unknown reasons.
  A source change during retrieval marks its snapshot unknown. `absence` is
  `unknown` for an empty ranked result, including a fully indexed database.

Only the contiguous confirmed index receipt prefix is trusted as remotely
indexed. Every current canonical embedding outside that prefix is scored
locally, so an asynchronous mutation cannot hide the new tail. Missing
embeddings retain lexical recall and explicit pending coverage.

The runtime should pass one app-level `shared_lock_root` for all generations
using the same index. The default recognizes the `generations/<id>/` layout.
A native lock and a private content-free pending pointer serialize the full
index mutation boundary across generations. The pointer references its owning
canonical receipt; it is not a second facts ledger. A crash or unknown sent
outcome blocks another mutation even after the process lock is released.
An unavailable owner receipt fails closed. Provider calls hold no database
transaction.

Temporal watches distinguish a follow without a deadline, an occurrence, and
current state. AND conditions must share a known subject/case/time phase;
unknown qualifiers do not allow anonymous cases to be combined. Explicit
recovery boundaries require later tests. Witness revocations retain history;
another independent valid witness can keep a watch met, while loss of all
witnesses produces a separate reopened change. `review_after` schedules review
and never expires a watch. Unresolved Episodes remain available for late
replies and resources. Reader overrides and preferences remain independently
owned by the reading consumer.

`WatchService(store, *, text_provider=None, decision_provider=None, jobs=None,
clock=None)` owns watch histories and Episodes in the canonical database.
`create_watch(WatchSpec(...))`, `evaluate(watch_id)`, `witness_history(watch_id)`
and `changes(watch_id)` are the typed APIs. Evaluation uses all eligible
canonical claims; an `evidence_only` attachment is retained provenance and
cannot become a new independent success witness. `same_material` claims share
one witness family while preserving every source reference.

`record_claim_relation(from_claim_id, to_claim_id, relation, reason=...,
evidence_ids=..., expected_revision=...)` delegates to the store's atomic claim
relation API. **From is the new evidence and to is the affected claim.** A
correction/retraction invalidates that exact witness and recomputes all watches
reached through directed dependencies, including older Items outside top-k.
An Item relation alone never retracts a claim.

`sync_reader_watch(item_id, preference_revision, wait_text, follow)` is the
reader bridge. It converts free Chinese waiting text through accepted Text
normalization pages and a typed Decision choice. Plans may select only supplied
predicate keys, scoped qualifier values and explicit temporal boundaries.
Boolean targets may select either truth value; other scalar values must be
supplied. Missing vocabulary or ambiguous mappings retain `unknown` and a
reason; new canonical claim vocabulary permits another mapping attempt. A
plain follow needs no provider and has no deadline. Clearing both controls
stops future evaluation/attention while preserving past witnesses and changes.
Reader preference revisions are checked again after provider work.

The bridge returns `status`, `watch_id` (nullable when unmapped or stopped),
`reason`, `evaluation`, `changes` and `counterevidence`.
`reader_watch_status(item_id)` reads its persisted result. Evaluation contains
exact witness claim/source IDs, revoked claims, completeness, review status and
`progress`/`met`/`reopened` changes. Durable changes include `change_id` and
`canonical_revision`; the delivery consumer owns mute and notification policy.
Counterevidence contains exact unmet/contested witnesses, revoked claim IDs, or
an explicit unmapped-condition reason.

`open_episode`, `attach_episode`, `close_episode` and `episodes` retain exact
source anchors and resource status. A late reply/resource reopens a resolved
Episode; time passage and `review_after` do not remove unresolved context.
The engine calls `sync_episodes(item_ids, receipt_id=...)` after accepted action
commit in both resolution and identity consolidation, before finishing its job
or acknowledging the batch. Each active Item gets a stable Episode. New
`supports` sources become exact continuation links; evidence-only forwarding
does not reopen it. Native reply references are scoped to namespace/chat, a
source correction follows the same occurrence's version history, and explicit
claim corrections follow the accepted receipt's exact affected claim spans.
These paths can reopen an older Episode across Item boundaries. Similar names,
nearby timestamps, and a shared vector do not establish links. Resource links
retain captured source/version and resource IDs; unknown or conflicting byte
availability stays pending. Replaying saved links does not refresh or reopen
the Episode. A crash after the canonical action commit resumes this sync from
the accepted receipt without a provider call. Retired Item Episodes keep their
history and receive no new automatic links.

Focused synthetic verification from the repository root:

```bash
.venv/bin/python -m unittest tests.test_semantic_engine tests.test_semantic_retrieval tests.test_semantic_watches
```

These tests use temporary canonical SQLite stores and deterministic fake
providers. They cover life scenes, malformed provenance, accepted-artifact
recovery, large capture paging, async index tails, profile isolation, directed
corrections, identity merge/split, View reuse, named-case temporal negatives,
independent witness revocation and the reader bridge. They do not establish
live model quality, remote account readiness, real-chat acceptance, installation
or generation activation. The canonical persistence and provider boundaries
remain in [semantic-store-contract.md](semantic-store-contract.md) and
[semantic-providers.md](semantic-providers.md).
