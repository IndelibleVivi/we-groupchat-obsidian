# Source reliability

This guide covers five deliberately separate responsibilities:

1. the optional WeChat source guard, which may request a normal macOS
   application launch;
2. the attachment catalog and private local content-addressed archive;
3. the default no-OAuth selected-resource handoff to an existing mounted target;
4. optional advanced direct selected-chat sync to the Drive API; and
5. a provider-neutral filesystem snapshot target for the broader attachment archive.

They remain separate domain responsibilities even though protected-data timers
share one long-lived menu-app process. `TopicMonitor` reads messages and owns
checkpoints; it does not import or invoke the source guard. The Knowledge
transaction owns attachment mentions; a post-commit worker owns byte copying.
Each selected-chat scanner owns an independent cursor/selection and may reuse
the same local CAS without a Knowledge hit. Mounted backup reads immutable local
archive objects and writes only to its configured filesystem target.

## 1. Optional WeChat source guard

The source guard is disabled by default. Its timer runs inside the long-lived
menu-bar app; enabling the policy is the only activation step:

```bash
.venv/bin/python scripts/wechat_source_guard.py status
.venv/bin/python scripts/wechat_source_guard.py enable
.venv/bin/python scripts/wechat_source_guard.py uninstall-agent  # legacy cleanup
```

Earlier versions scheduled a separate one-shot `StartInterval` job. macOS App
Data consent is process-lifetime access: after that helper exited, its next wake
could prompt again. New installs therefore refuse `install-agent`; the legacy
`--source-guard-run` mode returns `long_lived_app_required` before reading
protected data, while `uninstall-agent` remains idempotent cleanup. The ordinary
autostart LaunchAgent owns one long-lived menu app, and the source guard uses an
in-process timer plus a non-blocking lock.

Process-list availability is proven by inspecting the guard process itself; it
does not depend on whether a normal user session can see the system `launchd`.
Source freshness uses exact stats for cached known `message/` shard paths and
their WAL/SHM siblings. It never recursively walks the DB root or WeChat
hardlink/cache trees, so each in-process check remains bounded.

When process lookup confirms that WeChat is absent, the guard first enters a
grace period. After grace, and only while restart budget and exponential
backoff allow it, the sole launch request is equivalent to:

```text
open -g -a WeChat
```

The guard never kills WeChat, edits or re-signs the app, extracts keys, drives
UI, handles login, steals focus, or advances a monitor checkpoint. If macOS
process lookup is unavailable, the state is `process_lookup_unknown` and the
guard does not launch anything. A running but stale local database produces one
warning per stale episode; a fresh observation closes that episode. Staleness
is not treated as permission to restart.

Effective states are:

| State | Meaning |
| --- | --- |
| `disabled` | Policy is off; no launch can be requested. |
| `healthy` | WeChat was confirmed running. Source freshness is reported separately. |
| `missing_grace` | Absence was confirmed, but the grace period has not elapsed. |
| `restart_backoff` | A normal launch was requested or a retry is waiting for backoff. |
| `paused` | A timed or indefinite user pause blocks launches. |
| `degraded` | Restart budget was exhausted or the launch command failed repeatedly. |
| `process_lookup_unknown` | The process list could not be read; absence is unknown and fail-closed. |

Pause, resume, one-shot check, disable, and removal are explicit:

```bash
.venv/bin/python scripts/wechat_source_guard.py pause --hours 8
.venv/bin/python scripts/wechat_source_guard.py pause --indefinite
.venv/bin/python scripts/wechat_source_guard.py resume
.venv/bin/python scripts/wechat_source_guard.py check
.venv/bin/python scripts/wechat_source_guard.py disable
.venv/bin/python scripts/wechat_source_guard.py uninstall-agent
```

State and receipts live under the project runtime data directory with `0600`
file permissions inside `0700` directories. Receipts contain state transitions,
budget/backoff values, and error codes, not chat names, usernames, message
content, or attachment paths. Degraded/launch notifications remain keyed and
cooldown-throttled; stale-source notifications are episode-scoped.

## 2. Stable message and resource identity

Message reads inspect each actual WeChat table schema before building the
query. When present, the source envelope records `local_id`, `server_id`,
`sort_seq`, SQLite `rowid`, shard identity, `create_time`, and `local_type`.
Missing optional columns remain explicit nulls. A deterministic
`source_message_id` is derived from those values and a hash of the chat
username; it does not expose the username or `wxid`.

An explicitly configured `db_dir` remains authoritative even when its mount or
container is temporarily unavailable; auto-detection can fill only a still-empty
canonical value. Cursor-shard identity includes the source namespace, key
fingerprint, and database-generation evidence (file identity plus the
encrypted-page salt/header prefix). Replacing or rekeying a database at the
same relative path therefore starts a new physical generation. Durable
`source_message_id` instead uses the stable source namespace plus logical cache
basename, so the same logical row can retain its identity across an explicitly
reconciled cache/DB rebuild. The envelope carries the physical generation
separately for cursor admission. A decrypted cache with no corresponding live
source is marked `source_cache_only` and cannot produce an applicable backfill
plan.

### Re-sign and scanner identity

The explicit re-sign path accepts one canonical WeChat bundle. If that bundle
is running, its PID, launch time, executable path/inode and version/build remain
bound through privilege acquisition, graceful termination, signing,
independent `codesign` verification and exact-path reopen. It never terminates
by process name and never rediscovers a default app after mutation. Multiple
running copies require an explicit `--wechat-app=...` selection; cancellation,
identity change, quit timeout, signing failure, hardened-runtime verification
failure or reopen mismatch stops the operation.

The read-only C scanner is only a candidate producer. A scanner build receipt
binds the exact source digest/size, compiler path and binary digest,
compiler/target identity, explicit `arm64|x86_64` target and flags, plus the
output digest/size. Source, executable and receipt are completed in an
immutable private build directory before one fsynced atomic current pointer is
published. A missing/tampered receipt, pointer, binary or source/compiler input
forces a rebuild; failed compilation or pointer publication preserves the old
current pointer but does not certify it for the changed input.

### Authoritative shard inventory

`core/source_inventory.py` persists the expected union of current message DB
files, key-inventory paths, and every previously observed non-retired logical
shard. Logical identity is an opaque source namespace plus normalized relative
path; a file replacement or key rotation changes the separate generation ID
without erasing the logical shard from history. Each reconciliation publishes a
path-free `inventory_revision`, `inventory_digest`, completeness flag, state
counts, content-free error codes, and the generation IDs that are currently
readable.

`missing_file`, `key_missing`, `cache_only`, and `unreadable` make the inventory
incomplete. They are never interpreted as an empty source. Topic Monitor and
catch-up refuse to advance while the inventory is incomplete. Selected-resource
and Direct Drive scanners may continue consuming the listed present generations,
but their result remains `source_degraded`; when a missing shard returns, its own
cursor resumes and occurrence deduplication prevents duplicates.

### Monitor raw-row cursor authority

`core/monitor_source.py` turns that complete inventory into one bounded monitor
batch. Durable `source_cursors` are keyed by logical shard and bind the current
generation ID plus its opaque `(create_time, rowid)` token and initial boundary.
First enablement binds every then-present shard from now. A legacy state with no
per-shard cursor may perform its one compatibility migration. After any shard
binding exists, a replacement generation or new logical shard is rejected
before page reads with `source_generation_admission_required`. The returned
content-free plan binds the exact inventory digest/revision, old/new generation
IDs, prior cursor and bounded row limit. It is evidence for a later explicit
continuity proof or bounded replay/reconciliation, not permission to copy a
cursor or borrow the global timestamp.

The reader keeps a bounded page for each present shard, performs a k-way merge
by `create_time` and stable `source_message_id`, and stops at the configured raw
row budget. It derives each committed token from the last row actually consumed,
not from a fetched page end. Rows removed by presentation cleaning still consume
that budget and advance their shard cursor, but they never enter the AI prompt.
Filtered-only progress returns `source_advanced_no_visible`; `no_messages` is
reserved for verified raw EOF on every shard under the same complete inventory.

Visible rows may use separately queried, read-only overlap context. Context never
changes tentative cursor positions. AI/provider failure commits no cursor, and a
generation change is rechecked after AI/Knowledge work and before the monitor
state CAS. A retryable provider failure may CAS-write only content-free failure
count/code/timestamps and a bounded backoff deadline against the observed state
revision. It preserves source cursors, inventory bindings and checkpoints
exactly; a run before the deadline returns `ai_backoff` without calling the
provider. A successful retry clears both current and legacy failure metadata.

Knowledge writes use a source-ID-derived `source_batch_id`; if the event commits
but the state revision loses its CAS, the next run checks canonical SQLite
before calling the provider, adopts that exact event and advances the same
source batch without inserting another event or reinterpreting the messages.
If that lookup fails, the cursor remains unchanged. Missing managed topic
Markdown and date indexes are rebuilt from canonical SQLite.

Every new canonical event also inserts a `daily_digest_changes` row in the same
SQLite transaction. Existing affected Digest pages are rebuilt from canonical
events and the exact journal prefix is acknowledged only after every page that
existed at inspection time was atomically republished. Digest publication uses
a same-directory private temporary file, file fsync, atomic replace and parent
directory fsync, preserving the last-known-good file on failure. The event path
drains immediately; the 60-second menu timer also drains while the scheduled
Digest itself is not due, so a restart retains an independent repair trigger.

### Catch-up receipt finalization

Write-mode catch-up stops the managed LaunchAgent, acquires the menu app's
singleton lock, and holds it through backup, bounded drain, projection rebuild,
canonical validation, one capture of the post-drain checkpoints, and a durable
provisional receipt. Receipt publication fsyncs the file and parent directory
around atomic replacement. Catch-up then releases the lock and attempts to
restore a LaunchAgent that was previously loaded. It atomically overwrites the
same `run_id` receipt with the observed restore result; no second checkpoint
sample is taken after the app can resume.

The provisional state is `partial / drain_complete_restore_pending`, never
success. The finalized receipt is `complete / drained` only when canonical work
passed and restoration succeeded or was unnecessary. A failed restoration is
durably `partial / launch_agent_restore_failed`, disables `resume_supported`,
and returns nonzero. Receipts keep content-free error codes rather than provider
or chat content.

Encrypted WAL reconstruction validates the SQLite WAL header and cumulative
frame checksums, applies frames only through the last valid commit marker,
honors the committed database-page count, and discards an uncommitted tail.
Main/WAL identities are checked again before publishing the decrypted cache;
a concurrent checkpoint, reset, or replacement triggers one bounded retry and
then `source_snapshot_failed` rather than publishing a mixed snapshot.

File XML and image packed metadata are parsed before message text is cleaned.
The resulting resource envelope can contain the original file name, declared
size, declared MD5/SHA-256, attach id, extension, or image hash. Only sender and
cleaned message text go to the AI formatter; source envelopes and internal IDs
do not.

## 3. Attachment catalog and local archive

For a Knowledge hit, `attachment_mentions` rows are inserted in the same SQLite
transaction as the corresponding `events` row. `attachment_objects` is the
canonical object catalog and `attachment_attempts` is a content-free attempt
history. After the event commits, the app may start a daemon thread that
consumes pending mentions. A process-level archive lock prevents duplicate
workers. Fresh `pending` rows are selected before retries. Failed retryable rows
record `attempt_count` and `next_retry_at`, use bounded exponential backoff, and
are eligible only when due. Each trigger persists a wake generation before it
tries the lock; the active worker drains successive batches until no due work
or unconsumed wake remains.

This separation is intentional:

- cache resolution or copying cannot roll back a committed event;
- failures do not call the AI again;
- failures do not rewind or advance a monitor checkpoint; and
- retry changes catalog state only.

The local archive is disabled by default. Enable it explicitly and choose the
accepted resource kinds; images remain a separate opt-in:

```json
{
  "attachment_archive_enabled": true,
  "attachment_archive_kinds": ["file", "image"],
  "attachment_archive_max_object_bytes": 536870912,
  "attachment_archive_min_free_bytes": 1073741824,
  "attachment_archive_retry_base_seconds": 300,
  "attachment_archive_retry_max_seconds": 21600
}
```

Newer WeChat V2 image objects also require the locally captured
`image_aes_key`; the key is not stored in the attachment catalog, archive
receipts, backup manifest, or documentation.

### File resolution

The resolver searches only the declared month in WeChat's `msg/file` cache. It
accepts the exact name and conventional `name (N).ext` variants. Declared size
and MD5/SHA-256, when available, filter candidates before selection.

- one valid candidate is selected;
- several candidates with identical bytes are equivalent duplicates and are
  safe to select deterministically;
- several candidates with different bytes are `ambiguous`; and
- no valid candidate is `missing_retryable`.

Directory order and modification time are never used as identity. Symlinks,
non-regular files, and candidates outside the expected WeChat cache root are
rejected.

### Image resolution

Images are located only by the structured image hash, hashed chat directory,
and source month. Full/original suffixes are preferred over thumbnail suffixes.
There is no modification-time fallback. Outcomes distinguish:

- `original_archived`;
- `thumbnail_only`;
- `decode_unavailable`;
- `missing_retryable`; and
- `ambiguous` or a source-rejection/failure state where applicable.

### Content-addressed storage

Objects live under:

```text
~/.we-groupchat-obsidian/attachment_archive/
  objects/sha256/<first-two-hex>/<sha256>--<safe-original-name>
  tmp/
```

The root and object directories are `0700`; final objects and the worker lock
are `0600`. Copying uses a private partial file, calculates SHA-256 while
writing, checks source identity/size/mtime before and after the read, `fsync`s,
and atomically renames the completed object. A final object without a catalog
row is reusable after a crash; worker-owned partial files are recoverable and
never treated as objects. Partial recovery starts only after the process owns
the archive lock, so a losing worker cannot delete the active writer's temp
file before returning `worker_busy`.

SHA-256 is the identity, so identical bytes referenced under different names
use one immutable object. Markdown resource sections show the catalog status,
local object link when available, and the existing month-folder hint.

Before copying, the worker rejects objects larger than
`attachment_archive_max_object_bytes` and preserves at least
`attachment_archive_min_free_bytes` after the proposed write. An oversized
object requires an explicit policy change/manual retry; low-space failures use
the normal due-only retry schedule.

Important storage boundary: the archive does **not** delete or prune WeChat's
own cache. It avoids creating multiple archive copies of identical bytes, but
it does not reclaim source-cache disk space. Destructive cache retention or
pruning is outside this tranche and would require a separately reviewed policy.

### Archive CLI

```bash
.venv/bin/python scripts/attachment_archive.py status
.venv/bin/python scripts/attachment_archive.py run --limit 50
.venv/bin/python scripts/attachment_archive.py retry --mention-id <id>
.venv/bin/python scripts/attachment_archive.py retry --mention-id <id> --run
```

Historical backfill is plan/apply separated:

```bash
.venv/bin/python scripts/attachment_archive.py backfill
.venv/bin/python scripts/attachment_archive.py backfill --apply
.venv/bin/python scripts/attachment_archive.py backfill --apply --run
```

The first command reads historical `events.files_json` and reports counts. It
does not insert mentions or copy bytes. `--apply` explicitly inserts missing
historical catalog rows; `--run` then consumes pending rows.
`--limit` is the worker batch size, not a total cap: an acquired worker keeps
draining fresh and currently due rows before it exits.

## 4. Default selected-resource mounted backup

This is the default Google Drive path. It hands selected-chat links, selected
file occurrences, and shared-CAS bytes to an existing mounted filesystem such
as Google Drive for Desktop. It does not create a Google Cloud project, request
OAuth credentials, call the Drive API, or automate a browser.

```text
active monitor chats
  intersect resource_backup_selected_chats
  -> per-chat x message-shard occurrence capture
  -> exact URL metadata + shared local SHA-256 CAS
  -> local Obsidian resource index
  -> mounted target objects / catalog snapshots / views
```

The mounted lane and the optional direct API lane have separate disclosure
selections. `resource_backup_selected_chats` controls only this lane;
`google_drive_file_sync_selected_chats` controls only the optional API lane.
Neither selection enables the other transport.

### Private selection and local policy

Mounted-backup defaults are private and opt-in:

```json
{
  "resource_backup_selected_chats": [],
  "resource_backup_interval_seconds": 300,
  "resource_backup_max_messages_per_scan": 500,
  "resource_backup_min_free_bytes": 1073741824
}
```

List active monitor chats without printing raw `@chatroom` identifiers, then
replace the mounted-backup selection with the chosen list indexes:

```bash
.venv/bin/python scripts/resource_backup.py list-chats
.venv/bin/python scripts/resource_backup.py set-selected-chats 1
.venv/bin/python scripts/resource_backup.py clear-selected-chats
```

The target and link-export policy live in the separate private
`resource_backup.json` settings file. The chosen directory must already exist;
the worker never recreates a missing mount path.

The exact observed URL and its digest-bound identity stay in the private local
ledger. Every human-readable Markdown projection, snapshot/export value,
Review Queue entry, Daily Digest entry, AI prompt, and surfaced error uses
`core/url_safety.py` as the canonical display-redaction authority. It redacts
credential-bearing values in both query strings and fragments, including
common AWS, GCS, and Azure signed-URL fields. The supported export choices are
`redacted` (default) and `off`; a legacy stored `full` value is migrated to
`redacted` and can no longer export exact credential-bearing URLs. Built-in
remote link preview is inert and makes zero network requests even if an old
main config contains `monitor_fetch_links: true`. This is not a hardened
crawler claim.

```bash
.venv/bin/python scripts/resource_backup.py set-target "<existing-mounted-directory>"
.venv/bin/python scripts/resource_backup.py set-link-export-mode redacted
.venv/bin/python scripts/resource_backup.py init
.venv/bin/python scripts/resource_backup.py backfill-links --all
.venv/bin/python scripts/resource_backup.py backfill-links --all --apply --run-id <run-id-from-plan>
.venv/bin/python scripts/resource_backup.py backfill-links --from YYYY-MM-DD
.venv/bin/python scripts/resource_backup.py backfill-links --from YYYY-MM-DD --apply --run-id <run-id-from-plan>
.venv/bin/python scripts/resource_backup.py backfill --all
.venv/bin/python scripts/resource_backup.py backfill --all --apply --run-id <run-id-from-plan>
.venv/bin/python scripts/resource_backup.py backfill --from YYYY-MM-DD
.venv/bin/python scripts/resource_backup.py backfill --from YYYY-MM-DD --apply --run-id <run-id-from-plan>
.venv/bin/python scripts/resource_backup.py status
.venv/bin/python scripts/resource_backup.py plan
.venv/bin/python scripts/resource_backup.py run
.venv/bin/python scripts/resource_backup.py run --resolve-files --resolve-limit 10
.venv/bin/python scripts/resource_backup.py verify
```

`init` seeds from-now cursors only. Each selection has a UUID epoch, so a rapid
deselect/reselect cannot reuse a one-second timestamp identity or consume the
unselected gap. On schema upgrade, the first UUID assigned to an unchanged
legacy selection is adopted without advancing its epoch or resetting shard
cursors. Historical backfill is an identity-bound staged plan/apply and
does not move live cursors. Planning freezes the selection/source manifest and
writes bounded 500-2,000-row keyset pages; it does not mutate canonical chat,
cursor, or occurrence rows. Apply requires the exact unexpired `run_id`, checks
the staged candidate and current selection digests, reopens the source, and
requires the exact `inventory_digest` recorded by the plan. It never rescans
message rows. An unavailable, incomplete, or changed inventory fails closed
before staged rows enter the canonical occurrence catalog.
`backfill --all` means history still locally readable from known shards.
`backfill-links` never reads attachment bytes and leaves canonical occurrence
count unchanged if any known shard is degraded.
Ordinary `run` captures deterministic metadata occurrences, skips file-byte
resolution, refreshes local Obsidian indexes even when the target is
unavailable, and then attempts mounted handoff. `--resolve-files` is explicit.

The mounted namespace is:

```text
<target>/wgo-resource-backup/
  00-打开微信资源备份.md
  .wgo-destination.json
  v3/
    objects/sha256/...
    snapshots/<snapshot-id>/{manifest.json,resources.jsonl,COMPLETE}
    views/
      00-文件备份.md
      00-待补齐附件.md
      00-资源索引.md
      <chat>/{00-文件备份.md,文件备份/<month>.md,00-待补齐附件.md,待补齐附件/<month>.md,00-资源索引.md,资源索引/<month>.md}
```

The root portal is the human entrypoint and is also revealed by the menu-bar
action `📂 在 Finder 打开文件备份`. Delivered-file pages link to the one mounted
CAS copy, one row per digest with a separate occurrence count. Pending pages
contain no target links and group the real backlog as awaiting resolution,
cache unavailable, retry scheduled, local-space blocked, needs attention,
awaiting handoff, or unknown. Target views use one combined v2 ownership
manifest for all three families; local Obsidian remains v1. The
root portal is a separately marked singleton written only after the target
views and manifest succeed. These portable relative Markdown links do not
promise provider-native Drive-web rendering.

For upgrades from the pre-manifest projection format, one reconciliation pass
examines only known root/chat pages and valid month filenames one level below
the three generated directories. It reads at most a 16 KiB text prefix, rejects
symlinks, binary/unknown-shaped files, and invalid generated frontmatter, and
requires the exact app marker. It preserves every other user file and immediately
writes the normal ownership manifest. All later GC remains manifest-bound.

Plan and run reject filesystem-root, same/nested/ancestor local-source targets,
a symlink configured target, and a symlink or non-directory in the app-owned
subtree, including planned object, snapshot, view, and chat-index directories.
Snapshot/view conflicts return structured `target_failed` results. A successful first copy hashes while writing and immediately reads the
target bytes back. The regular, non-symlink destination marker contains a
random UUID bound to the owning archive, and a target-side lock serializes
different local ledgers that point at the same mount. Projection manifests bind
both archive and destination identities before any write or managed GC. Later
scheduled runs trust a valid local receipt after `lstat` confirms a regular,
non-symlink target with matching logical size; they do not rehash or hydrate a
streamed placeholder. `plan` and `status` use that same metadata-only check.
Explicit `verify` rehashes every object in the selected snapshot and detects
same-size corruption.

`sync_delegated` means the resolved file bytes were written and immediately
verified on the mounted filesystem. It never means provider-side upload or
remote checksum verification. If any eligible file remains unresolved, the
catalog may still publish a hash-bound `COMPLETE` snapshot, but the run state is
`pending_resources`, the manifest says `snapshot_completeness=catalog_complete`,
and the CLI exits non-zero. `COMPLETE` binds the durable catalog; it does not
fabricate missing bytes or prove that every expected WeChat shard was observed.
The separate path-free `source_observation` object records the inventory digest,
revision, state counts, error codes, and `complete` truth for that run. A change
to this evidence creates a new snapshot even when the durable catalog is
unchanged.

All surfaces consume one classifier: `ready_local` plus a valid mounted receipt
is delivered; `queued`, `waiting_cache`, `retry_wait`, and
`insufficient_local_space` map to their corresponding backlog states;
`ambiguous`, `object_too_large`, and `source_rejected` need attention; a valid
local object without valid delivery awaits handoff; unknown/structurally invalid
rows remain unknown. Receipt validation binds digest/size/path and uses only
metadata `lstat` (regular, non-symlink, matching size). It does not hash target
bytes or open source CAS; explicit `verify` still performs full hashing.

The compatibility `completed` flag and CLI exit code remain strict. Additive
`operational_success`, `coverage_complete`, and `coverage` fields report whether
the capture/projection/handoff cycle was healthy separately from whether every
attachment occurrence was delivered. Thus a healthy `pending_resources` cycle
means the catalog/index was updated while attachment-byte coverage remains
incomplete, not that the operation itself failed.

Ordinary status reports `sync_delegated` only when the current catalog, target
objects, valid latest `COMPLETE`, link mode, and manifest all agree. A missing
snapshot or pending object reports `pending`; unresolved files report
`pending_resources`. If WeChat source is unavailable, an explicit CLI run can
still project and hand off existing ledger/CAS state, emits structured JSON,
and returns non-zero for the source outage.

Background capture and projection are enabled in the long-lived menu app:

```bash
.venv/bin/python scripts/resource_backup.py enable
.venv/bin/python scripts/resource_backup.py disable
.venv/bin/python scripts/resource_backup.py agent-status
.venv/bin/python scripts/resource_backup.py uninstall-agent  # legacy cleanup
```

The menu app acquires a process-lifetime singleton before source
initialization. Main config writers patch the latest locked revision and publish
by same-directory atomic replace; the app watches revisions and reconciles
timers without restart. Explicit operator CLI source runs remain available but
share a cross-process capture lock with the app. Projection rendering, managed
GC, and mounted handoff reacquire that capture lock and reload canonical
selection before output, then take the DB-scoped backup lock. Local Obsidian
projection additionally takes a private `0600` root-identity lock keyed by the
real output path, while mounted handoff takes a target-side lock; distinct
capture databases and path aliases therefore cannot concurrently manage the
same projection or target. Local generated descendants are checked without
following symlinks before write or managed GC.

Capture construction is side-effect free: capture schema/archive identity are
initialized only after the capture operation lock is held, and backup delivery
tables are initialized only under the backup-DB lock. Attachment occurrence
transitions consume the status-plus-revision compare-and-set result; a rejected
claim is reported as `superseded`/degraded rather than counted as ready or
failed by the stale worker.

File-byte resolution remains off by default and requires the menu confirmation
or `--resolve-files` on the current explicit CLI run. Menu consent exists only
in memory, is never persisted, resets when the app process exits, and is checked
again before each attachment-byte operation so turning it off stops the next
file immediately. Old resource
LaunchAgent installs are detected and removable, but new installation is
refused for the same process-lifetime consent reason.

Manual notifications distinguish three healthy results: full coverage says
“update complete”; a healthy backlog says the index was updated and attachments
remain to be completed; newly resolved/copied bytes report progress. Any
degraded source, resolution, projection, or target phase, `worker_busy`, or
unknown state is still reported as incomplete rather than flattened into
success. The menu shows delivered object count, delivered occurrence count,
backlog count, and the current session-only attachment parsing state.

## 5. Optional advanced direct selected-chat Google Drive API sync

This is an optional advanced transport retained for users who explicitly want
Drive-native objects and shortcuts. It is independent of
`TopicMonitor`, Knowledge selection, the source guard, and the filesystem
snapshot backend:

```text
selected WeChat chats
  -> per-chat x message-shard cursor + durable file-message queue
  -> exact file resolver + shared local SHA-256 CAS
  -> one Drive object per unique byte digest
  -> chat / source-month Drive shortcuts
```

Only resources whose source envelope says `kind=file` are eligible in this
release. Images, voice messages, videos, and stickers do not enter this queue.
The scanner advances over missing and non-file messages, so a file that has not
yet appeared in WeChat's cache cannot block later messages. Its bookmark is a
per-chat x privacy-safe message-shard cursor: a failed shard is persisted as
`source_degraded` without moving that shard cursor, while healthy shards may
advance. Recovery queues the previously unseen file exactly once. Ordinary
`WeChatDB.get_messages()` also uses a strict all-known-shards contract and never
returns remaining rows as a falsely complete page. Receipts and health expose
only content-free error codes and degraded-shard counts, never raw paths, chat
IDs, or database names.

`missing_retryable` uses due-only bounded exponential retry; `ambiguous` is
terminal until a human changes the local evidence and explicitly retries
through a future workflow.

The first enable initializes each currently selected chat at the current time.
It does not silently upload history. Historical discovery is a separate
plan/apply operation. Removing a chat from the selected set retains its cursor,
queue, and retry state but stops resolving or uploading its pending items until
the chat is selected again.

### Local configuration and ledger

Public defaults are off:

```json
{
  "google_drive_file_sync_enabled": false,
  "google_drive_file_sync_paused": false,
  "google_drive_file_sync_selected_chats": [],
  "google_drive_file_sync_interval_seconds": 300,
  "google_drive_file_sync_max_messages_per_scan": 500,
  "google_drive_file_sync_max_uploads_per_run": 20,
  "google_drive_file_sync_max_bytes_per_run": 536870912,
  "google_drive_file_sync_root_name": "微信群文件归档",
  "google_drive_file_sync_keep_local_objects": true
}
```

Selected chat usernames live only in private local config and the independent
local ledger. Each remote chat identity is a salted SHA-256 key derived from a
random local `archive_id`; raw `@chatroom` usernames are never Drive metadata.
The ledger stores a timestamp and complete same-timestamp message identity set
for each chat x shard cursor, making same-second pagination and restart
idempotent. `(source_message_id, resource_index)` is globally unique.
Raw chat bodies are not stored in this database or its run receipts.

`drive_scan_state` retains the enable-time chat seed; canonical incremental
positions live in `drive_scan_shards`. The other principal tables are
`drive_sync_items`, `drive_objects`, `drive_placements`, `drive_folders`, and
content-free `drive_sync_runs`. A non-blocking process lock makes menu timer, CLI, and app
startup recovery safe callers of the same bounded worker; there is no new
infinite loop. Disabling or pausing prevents a new scan/upload. If the setting
changes during one file, that file finishes safely and the worker stops before
taking the next item.

`attempt_count` counts retry failures only. Successful transitions such as
`uploading -> shortcut_pending -> complete` no longer inflate exponential
backoff, and progress across a resolve, object, or shortcut phase resets that
phase's consecutive failure count.

### OAuth and credential boundary

Authentication is Installed desktop app OAuth 2.0 with a system browser,
loopback callback, and PKCE. The only requested scope is:

```text
https://www.googleapis.com/auth/drive.file
```

The user's own OAuth client JSON is normalized into the private runtime data
directory with mode `0600`; it is never ordinary config or tracked source. The
refresh token is stored only in macOS Keychain. Access tokens remain in memory.
Offline access is requested. Service accounts are unsupported. `auth-status`
validates the refresh token and distinguishes `token_present` from `connected`.
`invalid_grant` removes the already-invalid Keychain token so later status does
not keep reporting a connection. Queued work becomes `auth_required`, emits one
notification for that episode, and remains durable. `disconnect` removes the
Keychain refresh token but does not delete config, queue, CAS objects, or Drive
files.

### Drive identity and projection

The first successful remote run creates or adopts one app-owned root folder,
whose default visible name is `微信群文件归档`. Its Drive file ID is authority:
rename or move does not matter. If that known root is trashed, missing, invalid,
or inaccessible, the worker enters `remote_degraded` and does not create a
second root.

```text
微信群文件归档/
  群聊/<stable-local-alias>/<source YYYY-MM>/<Drive shortcut>
  _系统/objects/<sha256-prefix>/<full-sha256>--<safe-original-name>
```

Local SHA-256 is the canonical byte identity. The worker first checks the local
object ledger and then searches `appProperties` before uploading, so a crash
after a successful remote write but before local commit adopts the existing
object or shortcut. Duplicate remote object matches select one canonical Drive
ID, record `remote_duplicate_detected`, and never upload a third copy. Remote
verification prefers `sha256Checksum`; when unavailable it requires matching
size plus `md5Checksum` before recording `uploaded_verified`.

Objects larger than 5 MiB use a real resumable session. Every `308` advances
only to the server-confirmed `Range` offset. After network/429/5xx interruption,
an empty PUT with `Content-Range: bytes */<total>` probes the session. A completed
lost final response is adopted, while an expired 404 session is recreated at
most once in the same one-shot run. Missing, malformed, or regressing ranges
fail explicitly. Session URIs are not persisted across processes: a later run
starts a fresh session and still adopts any completed remote object through
`appProperties`. This is a documented bounded restart policy, not a blind
chunk restart.

One object is shared globally. The placement identity is
`(hashed_chat_key, source_month, sha256)`, so the same bytes in another chat or
month produce another shortcut, not another upload. A same-name/different-hash
collision uses `stem--<hash8>.ext`. A deleted ordinary shortcut or child folder
can be rebuilt by `reconcile`; the project never automatically deletes or
trashes any Drive item.

Remote `appProperties` contain only schema version, random archive ID, role,
SHA-256 where relevant, hashed chat key, and source month. Full source-message
provenance stays local. Google Drive receives the selected chat's configured
alias, original/safe file name, and attachment bytes. It does **not** receive
raw chat usernames, raw message XML/body, `source_message_id`, `wxid`, or local
WeChat cache paths through this lane.

### CLI and menu controls

Authentication, selected-chat choice, enablement, historical backfill, and a
real upload are separate actions:

```bash
.venv/bin/python scripts/google_drive_file_sync.py auth --client-secrets "<installed-desktop-client.json>"
.venv/bin/python scripts/google_drive_file_sync.py auth-status
.venv/bin/python scripts/google_drive_file_sync.py status
.venv/bin/python scripts/google_drive_file_sync.py enable
.venv/bin/python scripts/google_drive_file_sync.py disable
.venv/bin/python scripts/google_drive_file_sync.py pause
.venv/bin/python scripts/google_drive_file_sync.py resume
.venv/bin/python scripts/google_drive_file_sync.py scan
.venv/bin/python scripts/google_drive_file_sync.py run
.venv/bin/python scripts/google_drive_file_sync.py reconcile
.venv/bin/python scripts/google_drive_file_sync.py backfill --from YYYY-MM-DD
.venv/bin/python scripts/google_drive_file_sync.py backfill --from YYYY-MM-DD --apply
.venv/bin/python scripts/google_drive_file_sync.py disconnect
```

`backfill` without `--apply` is read-only. `enable` initializes current selected
chat cursor seeds but does not authenticate, select chats, run backfill, or upload.
The menu bar's **Google Drive group-file backup** submenu provides status,
enable/disable, pause/resume, sync now, chat selection, open root, and
reauthorize. Selecting chats also does not enable or upload.

CLI `auth-status` validates the refresh token. `auth_required`, `retry_wait`,
`remote_degraded`, `source_degraded`, and failed one-shot results return a
non-zero exit status so operators and schedulers cannot mistake printed error
JSON for success.

## 6. Optional filesystem backup target

The backup layer knows only filesystem paths. It has no Google Drive, Dropbox,
iCloud, or other provider API; it stores no OAuth token or provider credential.
A target may be located inside a provider's desktop sync folder, but provider
upload happens outside this project's authority.

Configure or clear the path explicitly:

```bash
.venv/bin/python scripts/attachment_backup.py set-target "<filesystem-target>"
.venv/bin/python scripts/attachment_backup.py clear-target
```

The target layout is provider-neutral:

```text
<target>/v2/
  objects/sha256/<first-two-hex>/<sha256>
  snapshots/<snapshot-id>/manifest.json
  snapshots/<snapshot-id>/catalog.json
  snapshots/<snapshot-id>/COMPLETE
  receipts/<snapshot-id>.json
```

The archive root now owns a provider-neutral `cas_catalog.db` containing one
SHA-256 object identity and content-bounded source bindings. Both the Knowledge
attachment lane and the selected-chat Drive lane write that catalog without
duplicating bytes or creating a second object identity. Filesystem snapshots
take an authoritative union with the Knowledge attachment catalog, so a
Drive-only selected-chat object that never hit KnowledgeStore participates in
plan/run/verify and DB-free restore planning while existing topic/event bindings
and privacy-bounded exports remain intact.

`plan` takes a stable read view of provider-neutral CAS objects and the privacy-bounded
attachment catalog, then checks which target objects are missing,
`target_verified`, or `target_failed` without writing the target.
`run` copies missing immutable objects through partial files, verifies source
hashes, and atomically publishes target objects. It writes `manifest.json` plus
`catalog.json`, then publishes `COMPLETE` last only when every object succeeded.
The catalog contains object SHA-256/size, original name/type,
`source_message_id`, numeric topic/event bindings, status, and resolution
method. It deliberately excludes raw chat bodies, `wxid` values, and WeChat
cache/archive paths. A content-free failed receipt is still written for
reconciliation. Existing target objects are never overwritten when their bytes
conflict, and unmanaged target files are never pruned.

Both `plan` and `run` reject a filesystem root or a target that is the same as,
inside, or an ancestor of the local archive/database. The checks compare both
lexical and resolved paths, so symlink escapes into a protected source are also
rejected before any target write.

```bash
.venv/bin/python scripts/attachment_backup.py status
.venv/bin/python scripts/attachment_backup.py plan
.venv/bin/python scripts/attachment_backup.py run
.venv/bin/python scripts/attachment_backup.py verify
.venv/bin/python scripts/attachment_backup.py verify --snapshot-id <id>
.venv/bin/python scripts/attachment_backup.py restore-plan
.venv/bin/python scripts/attachment_backup.py restore-plan --snapshot-id <id>
```

`verify` re-hashes objects visible at the target. It reports `target_verified`
or `target_failed`; it does not report or imply cloud-upload verification. `restore-plan`
is read-only and reports how many verified target objects are absent or invalid
locally. It reads the snapshot catalog and scans the local CAS directly, so the
plan still works when the local Knowledge database/catalog is absent. This
tranche deliberately provides no automatic restore or deletion.

## 7. Health and safe rollout

The redacted health check now gives one privacy-safe reliability matrix. It
distinguishes monitor state `healthy|missing|corrupt|conflict`, counts
generation-bound raw cursors, reads the configured source inventory without
scanning or creating it, and reports complete/degraded plus present, missing,
cache-only, key-missing, and unreadable counts. Source completeness means the
expected logical-shard inventory is complete, the current generation set stays
stable, and the required reads succeeded; it does not mean merely that one
enumeration returned rows.

The same health surface reports the existing mounted destination/snapshot
handoff without opening CAS payload objects. Mounted `sync_delegated` remains
`provider_side_sync=unknown` and `remote_verified=False`. A separate Direct
Drive line reports the number of ledger objects that passed Drive API
verification; that remote evidence is independent from source completeness.
It also states the fixed product boundaries: remote link preview is
`link_preview_disabled` with zero requests, MCP is legacy read-only with send
retired, and Windows W0.2A provides portable file-lock source only, not product
support.

```bash
.venv/bin/python scripts/health_check.py
.venv/bin/python scripts/resource_backup.py status
```

Default output contains no absolute paths, chat names/usernames, message text,
source-relative paths, API endpoints, or token material. `--sensitive` is an
explicit local-debug disclosure gate for paths, titles, and source-shard
details.

### Upgrade and migration behavior

- A valid unversioned monitor state remains readable and is upgraded only by a
  later successful locked write. Corrupt/non-regular state never migrates or
  resets to now.
- A legacy timestamp checkpoint seeds missing per-generation raw cursors only
  under one complete inventory. Generation changes never inherit an old token.
- Source-inventory health inspection never creates or migrates the ledger;
  initialization happens only during an actual source scan.
- Legacy `monitor_fetch_links=true` loads as disabled, and legacy mounted
  `link_export_mode=full` loads as `redacted`. Neither value restores the
  retired network/export behavior.
- Legacy MCP send keys remain parseable but inert; every old send tool returns
  `mcp_send_retired`.
- Windows W0.2A adds portable file locking for config, the app singleton,
  monitor state, and source inventory, but activates no source, monitor,
  backup, tray, autostart, packaging, or sending behavior. Later Windows phases
  remain separate migrations.

A safe first mounted-backup rollout is:

1. keep the optional direct API lane disabled;
2. run `list-chats`, select one non-sensitive canary by index, and run `init`;
3. choose one already existing directory inside the mounted provider root;
4. run `plan`, then send one non-sensitive link and one small file after the
   from-now cursor;
5. run one explicit `run` and `verify`, checking the local occurrence/CAS,
   Obsidian index, mounted object, and catalog snapshot;
6. confirm provider-side arrival from another Drive surface because
   `sync_delegated` is not remote verification; and
7. only then enable the long-lived resource timer; leave file resolution off
   unless attachment bytes are deliberately wanted.

The optional advanced direct-Drive API rollout remains separate:

1. obtain your own Google Cloud Installed desktop app OAuth client JSON;
2. run `auth`, then confirm `auth-status` without enabling sync;
3. choose chats in the menu and review the stable aliases that will be visible
   in Drive;
4. run historical `backfill --from ...` without `--apply` if history is wanted;
5. run `enable`, then one explicit `run`, and inspect `status` plus Drive root;
6. apply historical backfill only after reviewing its counts; and
7. run the redacted health check after activation.

The source guard and broader attachment filesystem snapshot have their own
rollouts: inspect their status/plan first and keep them disabled or unconfigured
until separately reviewed.

Enabling the source guard, selecting mounted-backup chats, writing a real
mounted target, enabling the resource timer or attachment-byte resolution,
authorizing Google for the
optional API lane, selecting API-lane chats, enabling direct sync, applying any
historical backfill, pruning WeChat cache data, and deleting Drive files are all
separate operational actions. Source availability, local preservation, mounted
target-byte verification, File Provider upload state, and verified Drive API
object/shortcut state are distinct facts.
