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
Observation boundaries follow a specific experience, question or connected
conversation, not individual messages or speaker changes. A scene can cite many
messages and preserve each person's different position. Distinctive quotations
may have their own units; copying every acknowledgment into a quote unit is not
required for preservation. Exact source spans remain available independently.
Resolution can assign several such Observations to one grounded Item; an
Observation is not an obligation to create another page. A shared broad topic,
entity or timestamp still cannot merge independent cases.
For a quote unit, the model selects `span_indices` and omits `text`. The program
copies those complete, contiguous source spans from one message verbatim before
persisting the accepted artifact; canonical units still always have `text`.
Noncontiguous selections or joined messages fail closed. Explicit `text` from
an older response remains supported only when it already matches the cited
source exactly: the program never silently rewrites a rejected quotation.
Self-added speaker labels, quotation wrappers and Markdown markers are not
source words. Attribution stays in captured speaker metadata and scene/fact
units, while the reading projection supplies quotation formatting.

If a page also returns a scene whose every source/span pair is a verified
non-core context reference, the page artifact retains it under
`excluded_context_results` as untrusted audit evidence. It never enters the
canonical Observations or becomes a duplicate of earlier work. Unknown,
mispaired or malformed references stay in strict validation. Omitting a core
member still leaves it `unclassified` and blocks processing ACK; scope admission
does not turn missing work into a negative result.

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
`semantic-extraction-pages-v2-attribution`. Fresh page and coverage jobs also
bind `unit_policy="retained-knowledge-v5"`. Earlier None/v1/v2/v3/v4 unit
policies are frozen candidate variants: their exact input,
version and accepted/unknown outcome survive resume without being reinterpreted.
The v5 units describe retained knowledge and complete scenes, not a provenance
audit or message directory. Source evidence, resources and exact source spans
remain separately preserved; a display name never supplies gender or identity.
Pure communication actions or isolated invitations without identifiable content
may receive reasoned `context_only`; length, keywords and message type do not
mechanically decide which daily experiences, stories or meaningful words survive.
URL slugs and filenames establish a reference, not an unseen page title, author
or conclusion. Expected and erroneous values are not competing reports unless
they actually assert contrary facts in the same scope.
An existing accepted v1 candidate
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
page context is marked non-core and cannot independently produce another
Observation. New pages retain the entire preceding bounded page, in source
order, so a pronoun or reply can still see a subject more than two messages
back. Core page limits remain unchanged; overlap grants no identity proof.
An existing page job keeps its exact frozen context and accepted/unknown
outcome on resume, including earlier two-message overlap and its original
`unit_policy`. A fresh page receives the full preceding bounded page as
ordered non-core context and binds `retained-knowledge-v4`; the change does not
fork, rewrite or resend an older unknown job. The parent extraction artifact
aggregates validated pages before
canonical commit. An unclassified fragment keeps its whole source unread even
when other fragments produced Observations. Only explicit supported terminal
dispositions can finish a source with zero Observations. Empty
`legacy_resource` and `legacy_metadata` rows receive the program disposition
`unsupported_type` because their complete private metadata remains captured;
empty pages and messages do not receive that assumption.
The reader's already filtered, empty system envelopes (exact integer types
`10000` and `10002`) retain their captured metadata as `unsupported_type` without
inventing text spans. This does not classify an ordinary empty message or a
readable system message, and does not bypass unknown child jobs. Existing page
artifacts remain unchanged when their parent applies this metadata disposition.

Before an unaccepted parent aggregates, every member still `unclassified` gets
at most one bounded adjacent-context coverage re-read (`extract_coverage`, phase
`bounded-adjacent-context-v2`). Fresh jobs bind `planning="pending-fragments-v1"`
and exact `pending_span_ids`; terminal classified fragments and already-cited
spans do not reenter core because another fragment of their source is pending. Up to four
neighbouring messages from the same frozen batch supply their original first
span as non-core context, with an 8,000-character total limit. Context can be
cited alongside a pending core member, using its exact source/span pair; it
cannot support a new Observation alone. No context text is reassigned to a core
source. The coverage pass reuses the shared page generation
and known-output correction path, binds exact source/span/context inputs, reuses
accepted page artifacts byte-for-byte and never reopens the protected WeChat source. It
contributes retained content only when each unit cites that page's pending core
spans. A core reference only on the Observation cannot ground units supported
entirely by old context. Joint core/context units, richer non-core evidence and
separately grounded typed claim lineage remain valid. It replaces a pending
disposition only when every still-unclassified
fragment of that member reaches a terminal state. A still-unclassified or unknown
coverage result keeps the member unread with no processing ACK; a distinct
coverage job is never re-sent under a new prompt, and no uniform `noise` or
forced `context_only` disposition may bypass the gap. An already accepted parent
keeps its exact artifact and receipt. An unknown coverage job blocks even a
prompt-version upgrade. Frozen None/v1-v4 unit-policy children using the same
coverage prompt retain their exact scope and artifacts. An accepted artifact
from a different historical coverage prompt version still requires explicit
reconciliation rather than a new parallel interpretation.

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
attempt. Eligibility comes from the exact exception raised while validating that
received response, rather than an error-name prefix. Provider, prompt-building,
claim and acceptance failures
do not enter this path. Each correction claims a new durable attempt and uses a
distinct physical request ID with
the error and rejected JSON as untrusted feedback for the same source spans.
Accepted pages are reused; unknown outcomes, unsent work and provider failures
cannot enter this correction loop. Exhaustion still stops extraction without
canonical acceptance or ACK. Waiting for configured
rate capacity renews the owned job lease without holding a database transaction.
The role defaults, explicit rate ceilings and process scope are documented in
[semantic-providers.md](semantic-providers.md#bounded-provider-admission).

Resolver `propose` jobs retain the exact `semantic-proposal-v1` initial prompt,
frozen state and original `<job_id>:text` request ID. After a complete response
matches the job's bound text route, a model-output validation failure may receive
at most two corrections inside the same claim, worker and durable attempt
(three text calls total). Follow-up IDs are `<job_id>:text:correction-1` and
`<job_id>:text:correction-2`. Their
`semantic-proposal-known-output-correction-v1` feedback carries the error code,
rejected JSON as untrusted data and the complete required-key contract: root,
novelty, every action including shared `type`/`action_key`, CREATE's `item_id`,
CREATE-shaped split outputs and claim relations. Every follow-up retains the
exact frozen state; the program does not fill fields, trim actions or relax
validation. The original first prompt remains unchanged for frozen identity.

Only received model-output failures enter proposal correction. Provider errors,
unknown outcomes, wrong routes, empty/non-text responses and program-owned
read-set or other non-model failures stop the claim. A correction call with an
unknown outcome makes the original job unknown and blocks resume without another
call; no repair child, new proposal version or second ledger is created. Three
known-invalid responses leave one failed attempt without an accepted proposal
or canonical action commit. Existing pending/failed v1 jobs keep their original
inputs, versions and identity when the existing JobStore claim rules permit a
normal invocation; earlier failed attempts remain recorded, and an old error
code alone does not authorize correction. No row is reset. Accepted artifacts
continue to replay without provider calls or renewed validation. Decision,
read-set/CAS and canonical commit contracts remain unchanged.

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

An accepted `defer` is revisited when the frozen Item catalog or View context
changes. Existing `proposal_context` jobs retain the planning epochs across
restart; old accepted artifacts remain immutable. A raw capture/empty EOF or
unchanged poll alone does not trigger another provider call. Unknown provider
outcomes still block their original invocation. Rebuild continues later batches
before revisiting deferred ones, and stops if a full pass makes no progress.
Live monitoring also lets later knowledge commit; processing receipts and the
applied checkpoint still require the complete continuous prefix.

Resolver pages contain at most 24 Observations and 30,000 JSON bytes before
candidate context. Hybrid recall is followed by a complete paginated scan of
all Item identity scopes. Possible omitted candidates prohibit `CREATE_ITEM`.
When more than 12 candidates remain possible, bounded six-Item pages examine
their full or accepted summarized identity context. Uncertain summaries cannot
establish absence. A still-ambiguous result remains deferred, with its accepted
pages available for replay. This does not remove the global-scan cost: synthetic
24/240/4,000-Item catalogs require 1/10/167 coarse decision calls respectively
when byte limits do not force additional pages. These are actual fake-provider
call counts, not real-provider latency or a constant-cost live guarantee.
Selected long Items get accepted, bounded identity-context summaries covering
all detail parts; these summaries never replace canonical body units. The final
proposal uses content, View definition, View override and global revisions.
The final decision receives the same assignment semantics: new identities are
an available pool, not one required Item per Observation. Each action names its
exact Observation membership, and several observations may share a grounded
thread. Complete catalog coverage is distinct from a top-k miss. These prompt
clarifications do not replace accepted decisions or replay unknown requests;
existing deferrals still follow the dependency-bound replanning path above.
Titles and relation reasons are reader-visible prose; the proposer must describe
their content without internal identity strings or abbreviated IDs. Identities
belong in the typed fields. Existing accepted reasons remain immutable evidence.
Its existing-candidate novelty list contains exactly the supplied existing Item
IDs, separately from unused new identities. Identity scope is an object;
memberships require an existing View and a model/rule origin. Pending attachment
bytes, an unresolved assignment flag or an absent View alone do not prevent a
source-grounded textual experience from establishing an Item. Validators still
reject invented identities, Views and malformed action fields.
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
An Item relation alone never retracts a claim. A withdrawn predicate propagates
only to the same typed value and exact qualifier scope within a material, or
through explicit claim-level `restates`/`replay_same_source` lineage. It does not
withdraw unrelated propositions from that report. A combined claim can retain
its unaffected predicates during evaluation; canonical claim bytes are preserved.
Forwarding speakers are distinct from claim subjects. Material-family and
restatement dependencies are evaluated across Items before watch selection.
Evidence-only attachments remain available as intermediate dependency nodes,
while never supplying an independent condition or temporal-boundary witness.

For `current_state`, incomplete qualifiers still cannot join partial facts into
a trial. Separately, a same-time or later contrary report with potentially
overlapping scope makes the old positive witness contested. Explicitly different
people/cases remain independent, and `occurrence` keeps historical success.

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
