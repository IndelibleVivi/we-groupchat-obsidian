# Resource Capture & Mounted Backup Specification

- Status: draft 0.4
- Target release: resource backup v3
- Applies to: selected-chat links, selected-chat file occurrences, shared local CAS objects, Obsidian resource indexes, and mounted-filesystem handoff

## 1. Decision summary

The default Google Drive path is the existing Google Drive for Desktop mount in Finder. It does not require a Google Cloud project, OAuth client, access token, browser automation, or direct Drive API calls.

The system captures link and file occurrences only from chats that satisfy both conditions:

1. the chat is currently active in monitor configuration; and
2. the chat is explicitly present in the resource-backup selection.

The eligible set is therefore:

```text
active monitor chats ∩ explicitly selected backup chats
```

A local CAS object may be shared by eligible and ineligible occurrences. The object bytes may be handed off when at least one eligible occurrence references them, but exported catalogs, paths, indexes, and metadata must contain only eligible occurrences. No group name, message identity, sender, timestamp, or other provenance from an unselected chat may cross the handoff boundary.

## 2. Problem

The repository already has four useful but separate capabilities:

- monitor-selected knowledge notes with specialized link/file rendering;
- stable WeChat source message identities and structured file metadata;
- a shared local content-addressed attachment archive;
- filesystem snapshots and a direct Google Drive API adapter.

The missing contract is a deterministic selected-chat resource lane. Existing topic `links_json` is curated, AI-dependent, and limited to monitor hits. It cannot serve as the complete source of link backup. Existing generic attachment snapshots can see more local CAS objects than the user intends to disclose. Existing direct Drive sync mixes source capture with one transport.

Resource backup v3 separates these responsibilities:

```text
WeChat source
  -> selected-chat deterministic resource capture
  -> local occurrence ledger
       -> file resolver -> shared local CAS
       -> Obsidian resource index
       -> mounted-filesystem handoff
       -> optional future transports
```

## 3. Goals

The implementation must:

- capture every exact HTTP(S) URL observed in eligible source messages without relying on AI output;
- capture structured file occurrences from the same eligible messages;
- preserve occurrence provenance independently of content deduplication;
- resolve file bytes into the existing shared SHA-256 CAS;
- keep source capture durable and independent from backup availability;
- write a per-chat, per-month Obsidian resource index even when the mounted
  destination is absent or unavailable;
- hand eligible CAS objects and eligible occurrence metadata to a mounted filesystem target;
- avoid re-reading streamed cloud placeholders during ordinary scheduled runs;
- expose honest `sync_delegated` semantics rather than claiming remote verification;
- run bounded source-guard and mounted-resource timer work inside the long-lived
  menu app so process-lifetime App Data consent is not repeatedly lost;
- retain the existing direct Google Drive API implementation as an optional advanced lane.

## 4. Non-goals

The first release does not:

- infer semantic relationships between a link and a file merely because they appeared together;
- crawl, download, summarize, or validate every link target;
- expand short links or access authenticated webpages;
- provide Google Drive server-side file IDs, checksums, native shortcuts, or remote receipts;
- perform historical backfill automatically;
- delete local CAS objects after handoff;
- remove files from the mounted target;
- make Google Drive Desktop start, restart, or take focus;
- treat Obsidian as the canonical resource database.

## 5. Authority model

### 5.1 Source occurrence authority

A resource occurrence is identified by:

```text
chat identity
source_message_id
kind
resource_index
```

The occurrence ledger is authoritative for the fact that a resource was observed in a selected chat message.

### 5.2 File content authority

A resolved file points to a shared CAS object identified by:

```text
SHA-256(file bytes)
```

The CAS object is authoritative for preserved file bytes. File names and chat paths are projections, not content identity.

### 5.3 Link identity

A link occurrence stores the exact observed URL string. Its stable index key is:

```text
SHA-256("we-groupchat-resource-link-v1\0" || UTF-8(observed_url))
```

This hash identifies the exact URL string only. It does not identify or authenticate the webpage content.

No canonical operation may lowercase the entire URL, reorder query parameters, delete tracking parameters, follow redirects, or otherwise rewrite the observed value.

### 5.4 Projection authority

Obsidian Markdown, mounted target views, manifests, and catalogs are regenerable projections. They must not become independent mutable authorities.

## 6. Selected-chat disclosure boundary

Only the current intersection of active monitor chats and explicit backup selection is eligible for index or handoff output.

Mounted resource backup owns the private `resource_backup_selected_chats`
selection. It does not reuse `google_drive_file_sync_selected_chats`, so enabling
or selecting the optional direct OAuth/API lane cannot silently change the
mounted-backup disclosure scope, and vice versa.

A shared object follows these rules:

```text
selected occurrence -> object bytes may be handed off
unselected occurrence -> occurrence metadata remains local
selected + unselected occurrences -> bytes may be handed off once;
                                   only selected provenance may be exported
only unselected occurrences -> object remains local
```

Removing a chat from selection stops future capture and future inclusion in newly generated views/snapshots. Existing backup bytes are not automatically deleted. Each selection entry carries a private UUID `selection_id` plus a display/source lower-bound `selected_since`. Re-selecting a previously removed chat creates a new UUID epoch and new from-now cursors even within the same second; the unselected gap is never captured implicitly.

## 7. Source capture and cursor correctness

Each source shard has an independent cursor consisting of:

- cursor timestamp;
- an opaque source-owned keyset token binding `(create_time, rowid)`;
- the set of source message IDs already consumed at that timestamp for legacy adapter migration;
- source health state and last privacy-safe error code.

For each shard page:

1. read a bounded complete page from the source adapter;
2. deterministically extract all link and file occurrences;
3. insert occurrences and advance that shard cursor in one SQLite transaction;
4. commit;
5. resolve file bytes later, outside the source transaction.

If a shard is unavailable, unknown, incomplete, or raises a source-degraded error, that shard cursor must not advance. Healthy shards may continue independently.

The first initialization and every unselected-to-selected transition use `from now` semantics. Historical capture is an identity-bound staged plan/apply action:

```bash
python scripts/resource_backup.py backfill --from YYYY-MM-DD
python scripts/resource_backup.py backfill --from YYYY-MM-DD --apply --run-id <run-id-from-plan>
```

Planning freezes the selection/source manifest and writes 500-2,000-row keyset
pages to SQLite staging without mutating canonical chat, cursor, or occurrence
rows. Apply verifies the unexpired run ID, selection digest, and staged candidate
digest, then inserts idempotent occurrences without rescanning source or moving
the live per-shard cursors.

## 8. Resource extraction

### 8.1 Links

Links are extracted from deterministic cleaned source message text with the repository HTTP(S) URL matcher.

Within one source message:

- source order is preserved;
- the exact matcher span is stored without stripping terminal URL characters;
- exact duplicate URL strings are collapsed;
- case-distinct path or query strings remain distinct;
- WeChat's deterministic `[链接] title URL` representation may provide a display title;
- plain-text links may have no title.

The local ledger stores the full exact URL.

### 8.2 Files

Files use structured `message.resources` entries with `kind=file`. The occurrence stores declared name, size, and hash when available. Resolution reuses `AttachmentArchive.preserve_file_mention`, including existing cache disambiguation, source-stability checks, size/hash verification, CAS publication, retry behavior, and disk-space policy.

### 8.3 Co-occurrence

Links and files in the same message share `source_message_id`. This is sufficient to query and display co-occurrence.

No direct link-to-file semantic edge is created in v1. The durable catalog may
expose co-occurrence for audit or later confirmation, but the lightweight
resource-finding index does not need to display sender or a co-occurrence
disclaimer.

A future explicit relation requires independent evidence such as user confirmation, an evidence span, or byte-identical materialization.

## 9. Obsidian resource index

The existing topic notes continue to show curated resources relevant to that topic. The new index is a complete resource-finding surface for eligible chats, including resources that did not trigger a knowledge notification.

Recommended layout:

```text
<monitor_obsidian_subdir>/
  00-资源索引.md
  <chat>/
    00-资源索引.md
    资源索引/
      2026-08.md
      2026-09.md
```

The scope-root `00-资源索引.md` links to every selected chat that currently has
captured occurrences and shows link/file/month counts. Each chat-level
`00-资源索引.md` contains month links and counts. Monthly notes are a lightweight
reading surface grouped by day and show:

- time;
- a clickable credential-redacted observed title when present;
- otherwise the canonical credential-redacted URL as the visible clickable label;
- a clickable file name when the CAS or mounted object is available;
- one short unavailable state only when a file cannot be opened.

Sender, source-message identity, URL identity, hashes, resolution state,
co-occurrence, and mounted handoff details remain in the private durable
catalogs and receipts rather than the reading projection.

The index contains references only. It does not copy file bytes into the Obsidian vault.

The mounted target additionally exposes three parallel human views:

```text
<target>/wgo-resource-backup/
  00-打开微信资源备份.md
  v3/views/
    00-文件备份.md
    00-待补齐附件.md
    00-资源索引.md
    <chat>/
      00-文件备份.md
      文件备份/<month>.md
      00-待补齐附件.md
      待补齐附件/<month>.md
      00-资源索引.md
      资源索引/<month>.md
```

The namespace-root portal links to the delivered-file view, the pending-attachment
view, and the combined resource view. Delivered pages include only occurrences
with `ready_local` capture state plus a valid `sync_delegated` delivery. Monthly
delivered pages are object-centric: one clickable row per digest, a deterministic
primary filename, optional alternate names, and the number of source occurrences.
Pending-only chats do not appear in the delivered root. Pending pages contain no
target links and group every non-delivered occurrence by the shared surface state.
`waiting_cache` says that resolution was attempted and the local cache was not
found, with the concrete instruction to reopen or download the attachment in
WeChat before retrying. Terminal states are described as needing attention, not
as waiting. The views must not duplicate payload bytes or use
symlinks, hardlinks, Finder aliases, or provider-specific shortcuts as a second
storage representation. The menu-bar app provides a direct Finder reveal action
for the managed portal. Counts label file occurrences separately from distinct
delivered digests. These are portable relative Markdown links on
the mounted filesystem; the lane does not promise provider-native Drive-web
rendering or shortcuts.

If two selected chats have the same human alias, their generated directories must remain distinct by adding a stable short chat-key suffix.

Every generated index contains an app ownership marker. A managed file normally
uses the clean preferred name without a `.generated` suffix. If the preferred
scope-root, chat-root, or monthly path already contains a user-authored file
without that marker, the worker preserves it and writes a sibling such as
`00-资源索引.generated.md`, `00-文件备份.generated.md`,
`00-待补齐附件.generated.md`, or
`2026-08.generated.md`. Parent navigation must
point to the actual generated filename. Two unmanaged collisions fail closed
rather than overwriting either file.

Each Obsidian and mounted-view surface has its own hidden ownership manifest
containing `archive_id` and the exact managed path set. Every render maintains an
explicit scope root even when the selection is empty. Unchanged index pages and
ownership manifests are reused without publication; new or changed manifests,
including schema migrations, are still published atomically. Missing managed
pages are rebuilt even when their manifest is unchanged. Stale paths are removed
only when the prior manifest or ownership marker proves they are app-generated;
unmanaged files are preserved. Rendering, managed GC, and mounted handoff hold
the capture operation lock so their selected-chat scope remains canonical for
the complete output phase, then take the backup-DB lock. Local projection
additionally takes a private root-identity lock keyed by the output root's real
path; mounted handoff takes a target-side lock. These root locks serialize
distinct capture databases and path aliases that target the same projection or
mount. Local generated directories reject symlink/non-directory descendants
before any managed write.

Local Obsidian projection manifests remain v1. Mounted target-view manifests
are v2 and own the combined, delivered, and pending path sets; the renderer reads a
legacy target v1 once and rewrites it as v2 under the existing target lock.
Older renderers therefore fail closed on the unknown target v2 schema instead
of treating file-only pages as stale v1 paths. The namespace-root portal is a
separate marker-owned singleton: only its preferred and `.generated.md`
candidates are reconciled, and arbitrary namespace files are never scanned or
collected. The portal is written last, after the combined target views and v2
manifest succeed.

When a pre-manifest projection is encountered, one adoption pass examines only
the exact app-owned root/chat page names and `YYYY-MM(.generated).md` files one
level below the known `资源索引`, `文件备份`, or `待补齐附件` directories. It reads
at most a 16 KiB prefix, rejects symlinks, non-regular/binary files and unknown
path shapes, and requires the exact ownership marker. If frontmatter exists it
must bind `source_app: we-groupchat-obsidian` and an allowed generated
`source_kind`; marker-only legacy text remains accepted at a known shape. A
successful render immediately returns the surface to manifest-bound GC.

Capture construction performs no SQLite write. Capture schema/archive identity
are initialized only inside the capture operation lock, while backup delivery
tables are initialized only inside the backup-DB lock. Attachment occurrence
state updates consume their status-plus-revision compare-and-set result; a
rejected claim is counted as superseded/degraded rather than as a successful or
failed stale transition.

## 10. Mounted-filesystem handoff

The target may be a Google Drive for Desktop Stream files mount or another writable filesystem directory. It is stored in a dedicated private `resource_backup.json` settings file rather than reusing the generic attachment-backup v2 target. This prevents the older all-CAS snapshot command from accidentally treating the selected-chat Drive destination as its own unrestricted target.

The user-selected target directory must already exist and be writable. The worker never recreates a missing File Provider mount path; a missing target is `destination_unavailable`.

The mounted lane writes under an app-owned namespace:

```text
<target>/wgo-resource-backup/
  00-打开微信资源备份.md
  v3/
    objects/sha256/<prefix>/<sha256>--<safe-original-name>
    snapshots/<snapshot-id>/
      manifest.json
      resources.jsonl
      COMPLETE
    views/
      00-文件备份.md
      00-待补齐附件.md
      00-资源索引.md
      <chat>/
        00-文件备份.md
        文件备份/<month>.md
        00-待补齐附件.md
        待补齐附件/<month>.md
        00-资源索引.md
        资源索引/<month>.md
```

The lane must:

- copy, never move, CAS bytes;
- reject targets that overlap the local archive, resource DB, knowledge DB, Obsidian vault, or filesystem root;
- reject a symlink configured target and any symlink/non-directory component in
  the app-owned `wgo-resource-backup/v3` subtree during both plan and run;
- reject symlink/non-regular destination conflicts;
- compute SHA-256 while copying;
- publish through a worker-owned temporary file and replace;
- perform one immediate destination hash readback;
- write a local durable delivery receipt only after successful readback;
- maintain the root portal and zero-copy file-only views as generated,
  collision-safe navigation surfaces;
- on later scheduled runs, validate only the target entry type and logical size
  before trusting a valid local `sync_delegated` receipt; do not hash or hydrate
  a streamed placeholder;
- apply the same metadata-only receipt validation in `plan` and `status`;
- reserve full target rehash for an explicit `verify` action.

Before creating a new target object, the worker checks available target-volume
space and retains a configurable free-space reserve. Insufficient space yields
`insufficient_target_space`; the occurrence and local CAS remain intact and no
complete snapshot is published.

`fsync` and permission changes are attempted but may degrade on File Provider mounts when the filesystem reports that the operation is unsupported. Copy and readback correctness may not be skipped.

### 10.1 Background scheduling

The ordinary autostart LaunchAgent owns one long-lived menu-bar app. Within that
process, separate bounded timers trigger source-guard checks and mounted-resource
capture/projection; non-blocking locks coalesce overlaps.

macOS App Data consent is process-lifetime access. The former short-lived
`StartInterval` helpers could therefore prompt again after every exit/relaunch.
New source/resource `install-agent` paths are retired and refuse installation;
their old command modes return `long_lived_app_required` before touching
protected data. Status/uninstall remain only to detect and remove legacy plists.

Resource background work captures occurrences, refreshes indexes, attempts
mounted handoff, and writes receipts/snapshots when appropriate. File-byte
resolution is disabled by default and requires a separate explicit menu
confirmation or `--resolve-files` on the current CLI run. Menu consent is
in-memory only and resets on process exit. Link-only backfill never reads
attachment bytes.

## 11. Status vocabulary

Capture states include:

```text
ready_metadata
queued
waiting_cache
ready_local
ambiguous
object_too_large
insufficient_local_space
retry_wait
source_rejected
```

Mounted delivery states include:

```text
pending
pending_resources
sync_delegated
target_failed
```

`pending_resources` means the catalog is current but at least one eligible file
occurrence is not delivered under the shared surface classifier, so the worker
must not claim that all intended bytes were handed off.

`sync_delegated` means:

> The complete intended bytes were written to and immediately verified on the configured mounted destination. Subsequent cloud transport is delegated to the filesystem provider.

It does not mean:

```text
uploaded
remote_verified
remote_checksum_verified
```

Every file occurrence is classified centrally as one of:

```text
delivered             = ready_local + valid mounted delivery
awaiting_resolution   = queued
cache_unavailable     = waiting_cache
retry_scheduled       = retry_wait
local_space_blocked   = insufficient_local_space
needs_attention       = ambiguous / object_too_large / source_rejected
awaiting_handoff      = structurally valid ready_local without valid delivery
unknown_state         = unknown or structurally invalid state
```

A valid mounted delivery binds digest and logical size to a relative path inside
the backup root and passes metadata-only `lstat`: regular, non-symlink, matching
size. Ordinary classification, rendering, `plan`, and `status` do not hash target
bytes or open the source CAS. Explicit `verify` retains full target hashing.

The general redacted health check uses a stricter read-only inspection path: it
does not create/migrate the capture ledger, does not open CAS payload objects,
and reports only destination/snapshot control evidence plus delivery counts.
Its mounted line always says `provider_side_sync=unknown` and
`remote_verified=False`. Direct Drive API verification is a separate health
line and never upgrades mounted handoff evidence.

App/CLI reporting keeps the existing strict `completed` and CLI exit contract for
compatibility, and adds `operational_success`, `coverage_complete`, and `coverage`.
`operational_success` accepts a healthy capture/scan/resolution/projection cycle
whose handoff is `idle`, `sync_delegated`, or `pending_resources`.
`coverage_complete` is true only when no file occurrence remains outside
`delivered`. The additive coverage object reports delivered occurrence/object
counts and a count for every non-delivered surface state. Thus
`pending_resources` truthfully means “catalog/index updated; attachment-byte
coverage incomplete,” without relabeling a normal backlog as an operational error.

## 12. Snapshot contract

A snapshot contains only currently eligible occurrences. It must not include raw internal chat usernames. It may include the stable hashed chat key and the user-facing alias.

`resources.jsonl` is canonical JSON Lines sorted by occurrence order. Each record includes occurrence identity, selected-chat identity, source time/sender, resource metadata, local capture state, and mounted handoff state.

`manifest.json` records `archive_id`, counts, object identities, link export
mode, the SHA-256 of `resources.jsonl`, and a separate path-free
`source_observation` copied from the authoritative shard inventory.

`COMPLETE` is written last and binds the manifest and resource catalog hashes. An incomplete directory without a valid `COMPLETE` marker is not a complete snapshot.

`COMPLETE` means `catalog_complete`: the exported occurrence catalog and its
listed objects are internally bound and independently readable. It does not turn
an unresolved file occurrence into a completed byte handoff. Such a manifest
records `handoff_semantics=pending_resources` and a non-zero
`unresolved_file_count`; the run remains non-successful until those bytes are
resolved and delivered. It also does not claim a complete WeChat source
observation. Only `source_observation.complete=true` proves that the current
expected logical-shard set was present/readable when captured; the same object
records `inventory_revision`, `inventory_digest`, state counts, and
content-free error codes.

A new snapshot is skipped only when the canonical resource catalog hash is
unchanged, the recorded target snapshot still has a valid hash-bound `COMPLETE`,
and its `link_export_mode`, handoff semantics, and `source_observation` match the
current run. A missing/invalid snapshot or changed source evidence is rebuilt
even when local SQLite still holds the old catalog hash.

## 13. Link privacy modes

The optional [Quiet Archive local handoff](quiet-archive-handoff.md) reuses this
v3 resource/object structure and the canonical capture ledger. It adds hashed
`contexts.jsonl` and `coverage.json` through the explicit
`we-groupchat-obsidian.quiet-archive.v1` manifest extension, uses a distinct local
destination purpose, and runs no Markdown/Drive projection. Full visible-message
retention defaults off. Its raw EOF, selected source scope, legacy context gaps
and attachment coverage do not change the existing mounted snapshot semantics.

The mounted export supports:

```text
redacted  default; credential-bearing query and fragment values are replaced with REDACTED
 off      URL values are omitted; exact URL identity remains private
```

The local occurrence ledger always preserves the exact observed URL and its
stable hash identity. Human-readable Markdown projections, snapshots/exports,
Review Queue, Daily Digest, AI prompts, and surfaced error text never use that
exact value directly; they share `core/url_safety.py` as the canonical display
redaction authority. A legacy stored `full` mode migrates to `redacted` and is
not a supported output mode.

Keys such as token, access_token, api_key/apikey, secret, password, session,
sessionid, signature/sig, auth/authorization, credential, jwt, credential-like
code, and AWS/GCS/Azure signed-URL credential variants are sensitive. The same
rule applies in the query and fragment. A URL that cannot be parsed safely
exports `REDACTED_INVALID_URL`; parse failure never falls back to the exact URL
and never terminates the worker. Built-in remote link preview is disabled and
must report `link_preview_disabled` with `network_requests=0`; a legacy
`monitor_fetch_links: true` value must not activate network access.

## 14. Failure semantics

- Source failure: do not advance the failed shard cursor.
- Incomplete inventory: Topic Monitor/catch-up refuse all partial reads;
  resource/Drive scanners may consume listed present generations but keep the
  run `source_degraded` and never claim `source_complete`.
- Source unavailable at process start: return structured `source_unavailable`, then continue due local resolution, Obsidian projection, and mounted handoff from the existing ledger/CAS; the composite CLI exit remains non-zero.
- CAS resolution failure: retain the occurrence and retry according to local archive policy.
- Target unavailable: retain all local state; do not alter source cursors or CAS;
  continue refreshing the local Obsidian resource index.
- Obsidian projection conflict: preserve unmanaged files and use the managed
  `.generated.md` fallback; if no safe managed path exists, report
  `projection_failed` without rolling back captured occurrences.
- Target volume below the configured reserve: stop before copy with
  `insufficient_target_space`; do not publish a complete snapshot.
- Target conflict: fail closed; do not overwrite unknown bytes.
- Concurrent worker: DB-, projection-root-, and target-scoped non-blocking locks
  permit only one owner for each affected surface; a contender returns
  `worker_busy` without mutating that surface.
- Process crash during copy: leave only a worker-owned partial file. A later
  archive owner may clean it only after acquiring the archive lock.
- Process crash after target publication but before receipt: the next run verifies the existing target object once and reconstructs the receipt.
- Process crash before `COMPLETE`: the snapshot is incomplete and ignored.
- Removing selection: do not delete prior target data.

## 15. Compatibility

The direct Google Drive API implementation remains intact and optional. It owns
its existing OAuth-specific selection and may later consume the same
provider-neutral occurrence ledger instead of maintaining its own source scanner.

Attachment backup v2 snapshots retain their existing interpretation. Resource backup v3 uses a new schema and subtree and does not mutate v2 artifacts in place.

## 16. Rollout

The first live rollout is bounded:

1. create the resource capture DB;
2. initialize selected chats from now;
3. use one explicitly selected canary chat;
4. send one non-sensitive message containing two links and two small files;
5. capture four occurrences;
6. resolve two distinct CAS objects;
7. render the Obsidian month index;
8. hand off to a small canary target directory inside the mounted Drive root;
9. verify destination bytes immediately;
10. confirm the remote copy manually from another Drive surface;
11. run a second ordinary handoff and confirm it uses local receipts without
    rehashing streamed target objects;
12. only after the canary passes, enable the long-lived resource timer;
13. keep historical backfill as a separate explicit dry-plan/apply decision.

The intended operator sequence is:

```bash
python scripts/resource_backup.py list-chats
python scripts/resource_backup.py set-selected-chats 1
python scripts/resource_backup.py set-target "/path/chosen/in/Finder"
python scripts/resource_backup.py set-link-export-mode redacted
python scripts/resource_backup.py init
python scripts/resource_backup.py backfill-links --all
python scripts/resource_backup.py backfill-links --all --apply --run-id <run-id-from-plan>
python scripts/resource_backup.py backfill --all
python scripts/resource_backup.py backfill --all --apply --run-id <run-id-from-plan>
python scripts/resource_backup.py backfill --from YYYY-MM-DD
python scripts/resource_backup.py backfill --from YYYY-MM-DD --apply --run-id <run-id-from-plan>
python scripts/resource_backup.py run
python scripts/resource_backup.py run --resolve-files --resolve-limit 10
python scripts/resource_backup.py verify
python scripts/resource_backup.py enable
```

`init` is from-now only. Re-selection also starts a new from-now epoch. Explicit
`backfill --apply` does not move that live cursor. `backfill --all` means all
history still locally readable from known WeChat shards. `backfill-links`
requires a complete authoritative inventory and atomically writes only exact
link occurrences; the plan binds `inventory_digest`, and apply re-reads that
exact digest before consuming staging without rescanning message rows. It never
resolves files. Ordinary `run` remains safe before the
target is available and skips attachment bytes unless `--resolve-files` is
explicit: it captures eligible metadata occurrences and refreshes the local
Obsidian index, then reports the target state without fabricating a remote
success. The final `enable` step activates only the long-lived timer; file-byte
resolution remains a separate choice.

## 17. Acceptance tests

At minimum, automated tests must prove:

- a chat must be both active and explicitly selected;
- mounted selection remains independent from the direct OAuth/API selection;
- an unselected chat never appears in the exported catalog or views;
- a CAS object shared with an unselected occurrence exports only selected provenance;
- case-distinct URLs remain distinct;
- exact duplicate URLs in one message collapse;
- exact URL strings and hashes remain unchanged in the private ledger while
  credential-bearing query and fragment values are redacted from every
  human-readable, export, AI-prompt, and surfaced-error projection;
- legacy preview-enable and `full` export settings fail closed with zero remote
  preview requests and redacted output;
- two same-name files with different bytes become two CAS objects;
- source cursor and occurrence inserts commit atomically;
- a degraded shard does not advance;
- an expected shard missing from the current filesystem keeps inventory
  incomplete while present shards remain consumable by resource/Drive scans;
- a returned shard resumes its own cursor and is captured exactly once;
- backfill apply fails closed when its bound inventory digest changes;
- links-only backfill writes no rows unless every known shard completes, then
  commits all new link occurrences atomically;
- shard A may fail while shard B advances; after A recovers its unseen file is
  captured exactly once;
- mixed link/file occurrence groups do not create semantic edges;
- ordinary reruns trust a valid local delivery receipt and do not rehash target placeholders;
- explicit verify rehashes target objects and detects corruption;
- a missing configured target is `destination_unavailable` and is not recreated;
- a missing target does not block local Obsidian index generation;
- unmanaged Obsidian index paths are preserved and use `.generated.md` siblings;
- a previously delivered path replaced by a symlink is never trusted;
- insufficient target space preserves local state and publishes no complete snapshot;
- target overlap and symlink conflicts fail closed;
- a configured-target or app-subtree symlink is rejected in plan and run before
  any path outside the configured target is created;
- snapshots without a valid `COMPLETE` marker are rejected;
- unresolved files may appear in a catalog-complete snapshot but never produce a
  `sync_delegated` run state;
- catalog-complete snapshots separately preserve incomplete source-observation
  evidence and are rebuilt when that evidence changes;
- Obsidian indexes reference CAS files but do not duplicate bytes.
- long-lived app timers use non-blocking overlap guards; retired source/resource
  command modes do not touch protected data, installation is refused, and
  legacy-agent removal is idempotent under mocked `launchctl`.
