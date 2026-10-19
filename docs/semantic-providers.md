# Semantic provider and vector contracts

This document owns the synchronous provider boundary. SQLite remains the
canonical owner of semantic objects and job progress. These adapters neither
read WeChat nor write source checkpoints. The implementation is source-only;
synthetic tests do not establish a configured Cloudflare account, local model
service or live index. Both local embeddings plus local cosine search and
Cloudflare embeddings plus Vectorize are supported routes. Embedding and index
selection are independent; choosing a local index alone does not make a
Cloudflare embedding request local.

## Public API

All methods are synchronous. Inputs are complete: an adapter rejects oversized
input instead of silently truncating it. Errors are `ProviderError(code,
retryable=False, outcome_unknown=False)` and contain no provider response,
prompt, endpoint or credentials.
The lower-level usage store raises `ProviderUsageError(code)`; Cloudflare
adapters translate it to `ProviderError` without adding sensitive details.

Two exact endpoints receive a gateway-specific explicit-off value when the
caller sets `thinking=False` with the DeepSeek thinking dialect:
`https://api.commandcode.ai/provider/v1` sends `reasoning_effort="off"`, and
`https://amr-link.open-design.ai/v1` sends `reasoning_effort="none"`. The
output-token ceiling is unchanged. Semantic role composition also enables
`response_format={"type":"json_object"}` at these two endpoints so every
semantic role receives JSON-object mode. Ordinary prose summaries retain their
normal response format. The match is exact after a trailing slash is removed:
other endpoints, paths and lookalike hosts keep their explicitly selected
dialect, with no discovery, fallback or inferred gateway behavior.

Custom OpenAI-compatible text calls may explicitly set config
`custom_streaming: true` (strict boolean), which `ai.factory.create_provider`
passes to `OpenAIProvider(..., streaming=True)`. The SDK receives the same
model, token cap, thinking dialect and response-format arguments plus
`stream=True`. The adapter collects content deltas and closes the response;
only `finish_reason="stop"` permits a complete result. `length` is a known
truncated output; an interrupted, malformed or otherwise unfinished stream
returns no partial result. Semantic jobs retain their existing unknown-outcome
handling and the SDK makes no automatic retries. Streaming does not prove
that a gateway honors the selected model or removes every gateway timeout.
The key is absent from defaults, so inspecting older configs does not change
frozen fingerprints. Changing it is a material provider binding change;
relocation can adopt it only for a generation with zero jobs.

`ai.semantic_provider` exports:

- `ProviderRoute(provider, model, task, fallback_from=None)`; every result carries
  the actual route. Tasks are `text`, `decision` and `embedding`.
- `TextResult(text, route)` and `TextProvider.generate(prompt, *, request_id,
  generation_id="")`. `LegacyTextProvider` wraps an already configured
  `AIProvider.summarize`; it never discovers another provider.
  Its constructor takes `provider_name` and `model`; the configured route's
  existing authorization and vendor/gateway limits remain applicable. It
  does not invent a Cloudflare-style daily reset for another vendor. Optional
  accounting is enabled only when the caller explicitly supplies all of
  `usage_store`, `account_key`, `upper_bound_units`. The legacy method returns
  no usage; in that optional mode successful calls retain the configured
  conservative bound and unknown failures retain the reservation. No
  price/unit for another vendor is inferred.
- `DecisionQuestion(id, type, instructions="", criteria=None)`, where type is
  `noul`, `choice` or `score`; choice criteria are an option-to-description map,
  score criteria are ordered descriptions. `DecisionRequest(state, questions)`
  contains a string or JSON state and a tuple of questions.
- `DecisionAnswer(type, noul=None, choice=None, score=None, confidence=None,
  probabilities=None)` and `DecisionResult(answers, route, input_tokens=None)`.
  `DecisionProvider.decide(request, *, request_id, generation_id="")` never
  returns generated body text or invented groups. Invalid output raises an
  error; it is never converted to a negative match.
- `TextDecisionProvider(text_provider)` is an explicit fallback using the same
  typed contract. Its result route records `fallback_from="decision"` and the
  actual configured text model. Provider errors do not trigger fallback.
- `EmbeddingProfile(provider, model, dimensions, pooling="provider-default",
  normalization="l2", document_template="{text}", query_template="{text}",
  chunker="semantic-v1", metric="cosine")`. `profile_id` is a digest of every
  field above. Templates must have exactly one `{text}` placeholder. Profiles
  are immutable; any changed field creates a distinct space.
- `EmbeddingResult(vectors, profile_id, route)` and
  `EmbeddingProvider.embed(texts, *, purpose, request_id, generation_id="")`.
  `purpose` is `document` or `query`; output order equals input order. Vectors
  have the declared dimension, finite components and nonzero norm.

`ai.cloudflare_provider` exports `HTTPResponse(status, body)`,
`HTTPTransport.request(method, url, *, headers, body, timeout)`, the standard
library `UrllibTransport`, and `CloudflareClient(account_id, api_token,
usage_store, *, transport=None, timeout=45)`. Credentials are explicitly
injected, never loaded from config, files or Keychain. `CloudflareDecisionProvider(client)`
implements Clef Flash. `CloudflareEmbeddingProvider(client, profile=None)`
implements BGE-M3. `default_bge_m3_profile()` supplies the full default profile.
Every network operation requires an application-wide usage reservation. There
are no automatic retries or external fallbacks. This mandatory Cloudflare
gate is not applied implicitly to an existing configured Text route.

`ai.local_embedding_provider` exports:

- `LocalEmbeddingProfile`, which extends `EmbeddingProfile` with `endpoint`,
  `api`, `context_window_tokens` and `max_input_bytes`. Its identity binds these
  fields as well as every ordinary embedding-space field. Existing Cloudflare
  profile identities are unchanged. A changed endpoint, model, normalization,
  template or chunker cannot reuse another profile's vectors, even when their
  dimensions match.
- `LocalEmbeddingProvider.from_config(local_embedding, *, transport=None)` and
  `LocalEmbeddingProvider(profile, *, transport=None, timeout=45,
  max_batch_size=32, max_batch_bytes=32000)` implement the same `embed` protocol.
  Construction and validation perform no service probe, model discovery,
  download, secret lookup or network request.
- `LoopbackTransport` implements the existing `HTTPTransport` contract using
  direct standard-library HTTP connections. It ignores environment proxies,
  does not follow redirects and sends no credentials. HTTP and HTTPS URLs must
  use an explicit port and numeric loopback address; `localhost` is pinned to
  `127.0.0.1` without DNS. IPv6 `::1` is supported. Credentials, escaped hosts,
  IPv4-mapped IPv6 addresses, other hostnames, query strings and fragments are
  rejected before a request. Injected transports are trusted implementations of
  this same boundary; tests use synthetic responses.

The local route has two explicitly selected wire formats; failures never switch
between them. `api: "openai"` uses the complete `/v1/embeddings` endpoint and
sends `model`, a string-array `input`, and `encoding_format: "float"`. It checks
the exact returned model, list/embedding object types, unique integer indices
covering the whole batch, dimensions and finite nonzero vectors. Out-of-order
rows are restored by index. `api: "ollama"` uses the complete `/api/embed`
endpoint and sends `truncate: false`; it validates the positional `embeddings`
array and exact model. Neither path slices vector dimensions or invents a
default model. L2 normalization follows the selected profile.

Every input is prepared through the canonical URL redactor and validated before
the first request. The declared `context_window_tokens` must be the deployed
model's verified effective capacity, including any server configuration. The
per-input UTF-8 byte limit leaves 64 tokens of special-token headroom and defaults
to the smaller of 8,000 bytes and that capacity minus 64. An operator can lower
the byte limit. Complete inputs are sorted into batches bounded by count and
padded byte size, and results return in original order. Oversized inputs fail
before sending any batch; the adapter does not crop a document or invent source
chunks. Canonical source/chunker owners retain that responsibility.

OpenAI's compatibility schema has no truncation switch. In the inspected Ollama
implementation, the compatibility layer does not pass a truncation setting to
its native embedding handler. Use the explicit native option for Ollama when
server-enforced rejection of overlong input is required. Other compatible
services must honor the declared capacity and reject excess input. WGO never
assumes that an arbitrary service silently truncating its input is safe. The
operator must configure the service to run the selected multilingual model
locally; a loopback address alone cannot prove what a separately managed service
does internally. There is no cloud fallback or automatic model installation.

Local calls have no Cloudflare reservation or invented daily allowance. The
canonical job owner still saves accepted artifacts and marks a timeout unknown.
A later process must not blindly resend an unknown job. The adapter makes no
automatic retries; a failure in a later batch never returns a partial successful
`EmbeddingResult`.

## Runtime profile selection

`core.semantic.runtime.validate_profile` retains the flat
`we-groupchat-obsidian.semantic-providers.v1` shape. `decision` is `text` or
`cloudflare`; `embedding` and `vector_index` are each `local` or `cloudflare`.
The configured Text adapter remains separate from these selections.

For local embeddings, `local_embedding` requires `endpoint`, `model`,
`dimensions` and `context_window_tokens`. Optional fields are `api` (default
`openai`), `pooling`, `normalization`, `document_template`, `query_template`,
`chunker`, `metric`, `max_input_bytes`, `max_batch_size`, `max_batch_bytes` and
`timeout`. Ordinary profile defaults are those listed above. Batch size is
1–128; timeout is at most 300 seconds. The padded batch byte bound must be at
least the per-input byte limit. There is no API-key field on this local route.

This example is a configuration template for an already running local Ollama
embedding model. Replace the model name, dimension and capacity with verified
values for that deployment; these numbers are not model discovery or defaults:

```json
{
  "schema": "we-groupchat-obsidian.semantic-providers.v1",
  "decision": "text",
  "embedding": "local",
  "vector_index": "local",
  "local_embedding": {
    "api": "ollama",
    "endpoint": "http://127.0.0.1:11434/api/embed",
    "model": "your-installed-multilingual-model",
    "dimensions": 1024,
    "context_window_tokens": 8192,
    "max_input_bytes": 8000
  }
}
```

An OpenAI-compatible local service instead uses `"api": "openai"` and its
explicit `http://127.0.0.1:<port>/v1/embeddings` endpoint. Selecting
`embedding: local`, `vector_index: local`, `decision: text` requires no
Cloudflare account, Keychain entry or usage database. `ProviderRoles.preflight`
returns a ready result with no Cloudflare budgets; this is not a claim that the
local service is running. The separately configured Text route still uses its
own authorization, credentials and limits. To keep all model calls local, that
Text route must also be configured for a local service.

A loopback endpoint may also be an operator-managed SSH forward to a self-hosted
embedding service. In that deployment, selected text leaves the Mac for that
explicitly authorized host; `embedding: local` describes the adapter, not proof
of same-machine processing. Keep both tunnel ends bound to loopback, provision
and maintain the service separately, and verify its dimension, pooling, query
instruction and rejection of oversized input before creating the generation.
The local index remains on the Mac. WGO does not install the service or tunnel,
and an existing server does not establish the separate Text provider's quota.

Selecting **any** Cloudflare role requires the existing `account_id` and
`secret_name`; Vectorize also requires `vectorize_index`. Workers AI evidence is
required only when Decision or Embedding uses Cloudflare. Vectorize evidence is
required only when the index uses Cloudflare. Legacy Cloudflare profiles remain
valid, and Cloudflare BGE-M3 plus Vectorize remains the maintained remote
deployment choice. Missing evidence still blocks the relevant role. Profile
changes require the existing explicit generation workflow; they do not mutate
an active generation's vector space in place.

## Bounded provider admission

The candidate semantic runtime uses `core/semantic/scheduling.py` for bounded
parallel work. The main-config field `semantic_provider_limits` accepts these
role settings; omitted roles and fields use the defaults shown here:

```json
{
  "semantic_provider_limits": {
    "text": {"max_inflight": 8, "requests_per_minute": null, "tokens_per_minute": null},
    "decision": {"max_inflight": 4, "requests_per_minute": null, "tokens_per_minute": null},
    "embedding": {"max_inflight": 4, "requests_per_minute": null, "tokens_per_minute": null}
  }
}
```

`max_inflight` is an integer from 1 through 64. Configured RPM and TPM are
positive integer operator ceilings; `null` means unset. WGO does not infer a
vendor allowance from a model name or claim that an unset rate is safe for an
account. `ProviderRoles.preflight().admission` reports effective ceilings,
inflight counters and unset rate fields without account identifiers or routes.
Set explicit rates from the selected service's actual allowance and the
operator's desired lower ceiling.

Admission uses a rolling 60-second window. Text reserves a conservative UTF-8
input-byte bound, the configured output-token ceiling and template headroom;
Decision reserves its supplied-state bound plus headroom; Embedding reserves
complete prepared input bytes plus per-input headroom. These are conservative
admission reservations, not observed usage or billing reports. A request larger
than a configured TPM ceiling stays unsent and pending instead of waiting
forever or truncating input. The Cloudflare usage ledger remains the separate
atomic authority for confirmed budget and charges.

Independent extraction batches, Item Body preparation and distinct-text
embedding preparation run concurrently. Canonical identity decisions remain
serial, and Vectorize mutation/confirmation retains its shared native lock.
Each distinct complete document text uses one embedding job and provider
dispatch; canonical source/Observation/Body records with equal text reuse it.
No provider call holds a canonical database transaction or the Body publication
root lock. Semantic SDK clients disable automatic SDK retries so a hidden retry
cannot bypass admission.

The app's monitor, reader and brief timers share gates by actual
route/account/credential/profile in one process, including rate reservations
when a timer constructs a new runtime. Different credentials or profiles do
not silently share identity. If overlapping runtime instances request different
limits for the same route, the stricter limits remain effective for that
process; restart applies a newly raised ceiling. This in-memory scheduler is
not a cross-process vendor quota service. Rebuild and the app retain their
existing exclusive process/cutover ownership.

A failed or unknown provider call stops further admission in affected active
phases, drains calls already dispatched and keeps all validated accepted
artifacts. Work not dispatched is neither successful nor failed. JobStore and
reader artifact records return to pending only on the scheduler's own pre-send
proof; arbitrary provider errors cannot use that transition. Restart reuses
accepted results, and unknown sent outcomes retain their reconciliation
requirement. There is no automatic fallback or same-phase provider retry.
The extraction consumer separately permits at most two corrections of a
received JSON response rejected by its validator; it records each failed
attempt and uses distinct request IDs. This cannot replay an unknown outcome
or reinterpret an already accepted artifact.

## Index and usage APIs

`ai.vector_index` exports:

- `VectorRecord(id, values)` and `VectorMatch(id, score)`; `id` must be an opaque
  UUID. Local object/source/chat IDs must be mapped to opaque IDs in canonical
  SQLite before calling the index.
- `IndexReceipt(backend, profile_id, generation_id, mutation_id, records,
  indexed=False, index_identity="")`. The receipt contains vectors and opaque
  IDs, never text. `index_identity` binds the exact account/index using a digest.
  `to_dict()` / `from_dict()` preserve receipts through canonical JSON storage.
- `VectorIndex.upsert(records, *, profile, generation_id, request_id)`;
  `confirm(receipt, *, profile, request_id)` returns a new receipt;
  `query(vector, *, profile, generation_id, top_k=10, request_id)` returns
  matches. Receipts must be durably saved by the caller before confirmation.
- `LocalCosineIndex()` is a standard-library, rebuildable disk cache in a
  platform-verified private temporary directory. It stores exact float64
  values, caps each upsert at 1,000 records, and scans one vector at a time
  with a bounded top-k heap and a 2 MiB SQLite page cache. Upsert is immediately
  indexed. It isolates generation and profile and does not claim durability;
  hydrate it from canonical SQLite receipts after restart. `close()` and
  ordinary object cleanup remove the owned cache; forced process termination
  may leave a private disposable temporary directory. Its disk use grows with
  vector count and dimension; vectors are not retained as a full Python graph.
  The retriever retains embedding metadata, reads vector payloads in pages of
  at most 100, and hydrates each confirmed receipt once per retriever lifetime.
  Provider calls never hold those read connections. Metadata/document memory
  still grows with corpus size; this is not a fixed RAM cap for the whole app.
- `CloudflareVectorizeIndex(client, index_name, *, pending_receipt_check=None)`
  uses Vectorize v2. Upsert requires a callback
  `pending_receipt_check(index_identity) -> bool`: `True` means a durable
  unconfirmed receipt or unknown write exists, and blocks another mutation.
  Missing or failed checks block writes. The canonical caller must hold its
  cross-process index lock while checking, submitting and durably saving the
  new receipt. The adapter's additional in-process pending guard does not
  replace this owner. Only one mutation may be outstanding per index; use a
  dedicated index rather than an independently written shared index.
  It sends
  only opaque IDs, float32 vectors and a fixed metadata whitelist:
  `profile_id`, `generation_id` (the latter is also an opaque digest).
  Generation namespaces and derived remote IDs
  isolate reused object IDs. Query results must match both bindings and be
  known to this index's confirmed receipts before entering downstream work.
  `restore_receipt(receipt)` restores that verification map or pending guard
  after restart. The confirmed map retains opaque IDs and exact-value digests,
  rather than copies of every confirmed vector.
  Each confirmation poll or repeated query is a new HTTP operation and needs
  a fresh `request_id`; persist the upsert's original request/receipt identity.
  The adapter never polls or resends automatically.
  Upsert alone is pending. Confirmation checks the exact mutation marker and
  retrieves the corresponding IDs, metadata and float32 values; UUID order
  is never used to infer mutation order. A pending receipt stays pending.
  If an unrelated writer advances the watermark past the saved mutation,
  confirmation remains pending. Do not submit a second write to force a new
  marker or declare the earlier receipt complete without reconciliation.
  The adapter checks actual index dimensions and cosine metric before each
  operation. It limits each receipt to 1,000 records so readback is bounded.

`core.provider_usage` exports `ProviderUsageStore(path, *, clock=None)`:

- `confirm_budget(account_key, *, utc_day, daily_limit_units,
  external_usage_units, confirmation_id, valid_until)` records explicit,
  bounded account evidence. Units are integer micro-neurons for Workers AI;
  Vectorize uses the separately confirmed `client.vectorize_account_key` with
  conservative dimension-operation units: upsert reserves twice the number of
  vector dimensions (write/query-accounting plus new storage), a query reserves
  its dimension count, readback reserves returned dimensions, and metadata
  checks reserve one unit. This is local admission accounting, not a model of
  the complete monthly Vectorize bill or a storage-lifetime spending cap.
- `reserve(account_key, *, request_id, route, upper_bound_units,
  request_fingerprint, generation_id="") -> UsageReservation` atomically
  reserves by account and UTC day, across all generations and processes.
- `mark_sent(request_id)` rechecks day and budget evidence immediately before
  transport, and rejects reuse of sent, unknown or settled IDs.
- `settle(request_id, *, actual_units, receipt_id)` is idempotent only for the
  same unique receipt and amount. `mark_unknown(request_id)` retains the full
  reservation; `cancel(request_id)` releases only an unsent reservation.
- `get(request_id)` and `day_usage(account_key, utc_day)` expose content-free
  local accounting. An unknown outcome never earns a refund or permission to
  resend. A caller must retain the request ID with its durable job intent.

The local ledger constrains WGO only. It cannot guarantee that a shared Paid
account remains within a free allowance. Missing, expired or unconfirmed
external usage/budget evidence blocks calls. Crossing midnight requires new
day evidence and a new unsent reservation; a reservation from yesterday
cannot authorize today's request.

Workers AI shares `client.account_key` across decision and embedding models.
Clef reserves its full 24,576-token input ceiling at 3,455 micro-neurons per
token, then settles the observed `usage.input_tokens`; an observed bound
violation blocks further calls for that account/day. BGE reserves a UTF-8 byte
upper bound including per-row special-token headroom and padded batch length
at 1,075 micro-neurons per token. Its synchronous schema supplies no usage, so
the reservation is retained as a conservative settled charge. These are
explicit dated model rates, not automatically refreshed billing facts.

BGE validates every input before sending, sorts by UTF-8 byte length, uses
bounded padded batches and restores original order. Each batch has a stable
derived request ID. Admission reserves all batches before the first send.
There is no retry loop: after a later batch fails, completed batches remain
charged, unknown batches retain reservations, and unsent batches are cancelled.
The caller must retain the parent request ID and reconcile that attempt rather
than blindly submitting the whole operation under another ID. The native BGE
response is a positional array without item IDs; shape and count are validated,
but the adapter cannot detect an upstream semantic swap of two valid vectors.

## Privacy and validation

All prompt/state/embedding strings pass through
`core.url_safety.redact_urls_in_text`. Exact URLs remain only in their existing
private ledgers. HTTP bodies and raw errors are never logged. Cloudflare
HTTP 2xx alone is insufficient: `success is True` and a valid `result` are
required. Typed answers bind exactly to question IDs and supplied enums;
probabilities must be finite, within range and normalized. Profiles cannot
mix even when dimensions match. Remote metadata cannot accept arbitrary
caller keys. Network uncertainty remains explicit and reserves its budget.

## Official evidence

Schema and pricing references were inspected on 2026-10-13. Prices are not an
account allowance or a promise of free use.

- [Clef Flash model](https://developers.cloudflare.com/workers-ai/models/clef-flash/)
  documents the System One model/state/questions input and typed answers.
- [BGE-M3 model](https://developers.cloudflare.com/workers-ai/models/bge-m3/)
  identifies the multilingual model. The
  [official raw model schema](https://github.com/cloudflare/cloudflare-docs/blob/production/src/content/workers-ai-models/bge-m3.json)
  has multiple `oneOf` branches; the adapter uses synchronous
  `text` / `truncate_inputs: false` and validates `shape` / `data`, with the
  BGE-M3 dense CLS profile and 1,024 dimensions. Async `request_id` responses
  are rejected by this synchronous boundary.
  [BAAI's model card](https://huggingface.co/BAAI/bge-m3) independently documents
  1,024 dimensions and an 8,192-token sequence limit. The adapter's 8,000-byte
  per-item cap is intentionally below that bound, even though Cloudflare's
  page advertises a larger combined context window.
- [Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)
  publishes neuron rates and UTC daily free allocation, which must not be
  confused with an account spending cap.
- [Vectorize upsert](https://developers.cloudflare.com/api/resources/vectorize/subresources/indexes/methods/upsert/),
  [query](https://developers.cloudflare.com/api/resources/vectorize/subresources/indexes/methods/query/),
  and [index info](https://developers.cloudflare.com/api/resources/vectorize/subresources/indexes/methods/info/)
  distinguish mutation acceptance, query results and the processed watermark.
- [Wrangler upsert implementation](https://github.com/cloudflare/workers-sdk/blob/main/packages/wrangler/src/vectorize/upsert.ts)
  confirms the `vectors` multipart field and NDJSON file body;
  [Vectorize pricing](https://developers.cloudflare.com/vectorize/platform/pricing/)
  has distinct monthly queried and stored dimensions, requiring external
  confirmation beyond this application's daily admission ledger.
- [OpenAI embeddings schema](https://developers.openai.com/api/reference/resources/embeddings/methods/create)
  documents `input`, `model`, float encoding and indexed response rows. No
  OpenAI account is used by the local adapter.
- [Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility)
  lists the supported embedding request fields;
  [native embedding API](https://docs.ollama.com/api/embed) documents
  `truncate: false` and positional response vectors. The inspected
  [official compatibility middleware](https://github.com/ollama/ollama/blob/main/middleware/openai.go)
  forwards model/input/dimensions without a truncation setting. The native
  route is explicit, never a fallback after a compatibility request fails.

Integration owners must save receipts and use `indexed` only after confirmation;
this module does not activate candidates, advance cursors or mutate the main
configuration. Account provisioning, credential installation, external budget
confirmation and live acceptance remain separate operator actions.
