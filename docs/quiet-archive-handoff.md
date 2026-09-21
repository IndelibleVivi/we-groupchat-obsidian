# Quiet Archive private local handoff

This opt-in producer hands selected resources and their original visible message
context to a local Quiet Archive consumer. It reads the same selected-resource
ledger, preserves the existing backups, and needs no AI note, Obsidian vault,
Drive projection, OAuth or network service. This document describes source
behavior; installing/rebuilding the app and accepting it on real source data are
separate operator actions.

## Selection and retention

Selection remains the intersection of `monitor_chats` and the explicit
`resource_backup_selected_chats`. The AI monitor may be disabled; its model and
success state do not gate capture. New selection starts from now. Removing and
reselecting a chat retains the existing selection-epoch behavior and does not
silently collect the unselected interval.

`quiet_archive_handoff_enabled` defaults to `false`. Ordinary resource scans then
retain only their existing resource metadata, not all message text. When enabled,
the same bounded raw pages retain every selected non-empty visible message in
`resource_contexts`, including pure text without a URL or file. A dedicated
`capture`, `drain` or `refresh` invocation also explicitly enables context capture
for that invocation; it does not change the persistent switch. Text has no new
length truncation. It is the existing WeChat decoder's visible text: ordinary text
is preserved, cards/media use existing readable representations, and unsupported
XML is filtered. This is not an XML or media-byte archive.

Contexts, occurrences, cursor advancement and page coverage commit in one
resource-DB transaction. The private ledger may contain original observed URLs;
exports use `core/url_safety.py` redaction. Context rows export no sender identity,
raw chat username, source envelope, rowid or local path. The v3 resource
`source_sender` field is empty in this dedicated export. Treat the handoff as
private source material; it is not a public publication or a permission grant to
send messages to an AI provider.

Schema migration preserves resources, CAS objects, selection and cursors and
creates empty context tables. Existing consumed history is explicitly unknown.
Consuming visible text while context capture is disabled marks a history gap.
Turning the switch back on does not erase it. A context-only staged backfill can
recover available source history without changing live cursors or resolving
attachments. Only an applied `--all` context backfill clears the unmeasured legacy
gap; bounded historical ranges remain explicit evidence. No claim includes
unavailable/deleted WeChat history or future arrivals.

## Configure and operate

Use a pre-existing private local directory, separate from mounted/cloud backup
destinations. A destination purpose marker binds it to this archive and refuses
reuse of an existing mounted-backup namespace. The operator is responsible for
choosing a directory that is not synchronized to an external service.

```bash
# Reuse the explicit selected-resource scope; these commands inspect config.
.venv/bin/python scripts/resource_backup.py list-chats
.venv/bin/python scripts/resource_backup.py set-selected-chats 1

.venv/bin/python scripts/quiet_archive_handoff.py configure --target "<existing-private-local-directory>"
.venv/bin/python scripts/quiet_archive_handoff.py enable
.venv/bin/python scripts/quiet_archive_handoff.py status

# Export only the already committed local ledger and CAS: no WeChat reads.
.venv/bin/python scripts/quiet_archive_handoff.py export

# One explicitly authorized bounded source refresh, then local JSON export.
.venv/bin/python scripts/quiet_archive_handoff.py refresh \
  --allow-transient-wechat-source-read --max-rounds 20 --max-seconds 480

# Capture only; drain calls the same scanner repeatedly within its budget.
.venv/bin/python scripts/quiet_archive_handoff.py capture --allow-transient-wechat-source-read
.venv/bin/python scripts/quiet_archive_handoff.py drain \
  --allow-transient-wechat-source-read --max-rounds 20 --max-seconds 480

# Stage first, inspect its counts, then apply that exact unexpired run.
.venv/bin/python scripts/quiet_archive_handoff.py backfill --all --allow-transient-wechat-source-read
.venv/bin/python scripts/quiet_archive_handoff.py backfill --all --apply \
  --run-id <run-id-from-plan> --allow-transient-wechat-source-read
# --from YYYY-MM-DD can replace --all on both plan and apply.

.venv/bin/python scripts/quiet_archive_handoff.py disable
```

The switch controls the existing long-lived app timer; it does not start/reload
an app. The resource cycle scans once, then runs each enabled downstream consumer
independently. A mounted/Markdown failure does not block the dedicated handoff,
and a handoff failure does not block the old backup. Disabling preserves stored
contexts and completed snapshots. No new short-lived scheduled source worker is
installed. Explicit source CLI commands require the shown flag and may receive
their own macOS App Data prompt.

All commands above keep attachment resolution off. Existing per-run/session
attachment consent is unchanged; ready local CAS files can be copied, while
missing files remain resource records with pending coverage. Neither history
backfill nor machine export invokes an AI provider or manufactures AI notes.

## Files and machine contract

The consumer root is `<target>/wgo-resource-backup/v3`, containing:

```text
objects/sha256/<prefix>/<sha256>--<safe-name>
snapshots/<snapshot-id>/
  resources.jsonl
  contexts.jsonl
  coverage.json
  manifest.json
  COMPLETE
```

The existing v3 resource/object/manifest fields remain readable. `manifest.json`
adds this extension; hashes bind the exact file bytes:

```json
{
  "machine_handoff": {
    "schema": "we-groupchat-obsidian.quiet-archive.v1",
    "contexts_file": "contexts.jsonl",
    "contexts_sha256": "<sha256>",
    "context_count": 1,
    "coverage_file": "coverage.json",
    "coverage_sha256": "<sha256>",
    "text_projection": "wechat-visible-text-redacted-v1"
  }
}
```

`contexts.jsonl` has one record per visible message:

```json
{"event_id":"wgo_context_<stable-id>","chat_key":"<hashed-chat-key>","chat_alias":"Synthetic chat","source_message_id":"wgmsg_<stable-id>","source_timestamp":100,"source_time":"1970-01-01 00:01","message_type":1,"text":"Complete visible message, with canonical URL redaction.","resource_occurrence_ids":[]}
```

`event_id` hashes the fixed `we-groupchat-context-v1` domain, archive ID, chat key
and source message ID, separated by NUL. The exported ID is `wgo_context_` plus
the first 32 hex characters of SHA-256. It does not depend on text or snapshot.
Resource associations use the existing v3 stable occurrence ID. Rows sort by
chat key, source timestamp and source message ID; consumers can combine adjacent
messages without inferring that time adjacency alone proves semantic relation.

`coverage.json` uses `we-groupchat-obsidian.quiet-archive.coverage.v1`:

| Field | Meaning |
| --- | --- |
| `capture_run_id`, `observed_at` | Identity and UTC time of the recorded scan observation; never snapshot freshness by itself. |
| `selection[]` | Exact current `chat_key`, `selection_id`, `selected_since`; a changed selection invalidates the previous scan receipt. |
| `inventory` | `revision`, `digest`, `complete`, `counts`, `error_codes` for the initial expected shard observation. |
| `source_scan` | `state` (`not_started`, `pending`, `eof`, `degraded`, `failed`), `raw_eof`, `raw_rows_scanned`, `visible_messages_captured`, `pending_shards`, `failed_shards`, `shards[]`. |
| `source_scan.shards[]` | `chat_key`, `source_generation_id`, opaque `cursor_before`/`cursor_after`, committed `raw_rows_scanned`, `raw_eof`, `state`, content-free `error_code`. |
| `contexts` | `total_messages`, `history_gap_chat_count`, `legacy_resource_messages_without_context`, `unmeasured_history_gap`, `history_ranges[]`. |
| `contexts.history_ranges[]` | Completed observations: `chat_key`, `selection_id`, `from_timestamp`, `observed_at`, `inventory_digest`, `origin` (`incremental` or `explicit_backfill`). |
| `files` | `total_occurrences`, `ready_local`, `delivered_occurrences`, `pending_occurrences`, `by_capture_status`. |
| `errors[]` | Content-free scan failure codes; no source bodies or paths. |

Inventory complete is not raw EOF. Raw EOF requires every selected shard to be
exhausted, no failed/pending shard, and a matching final complete inventory.
It describes the bounded source observation, not arrivals after it. The default
scan consumes at most one configured page per chat/shard; a drain stops at EOF,
a failure, its round budget or its elapsed-time budget. The time budget is checked
between rounds; an individual page may still take longer. Completed pages survive
process interruption, while an interrupted page cannot advance its cursor.

The legacy missing-context count measures distinct resource messages only; pure
text that old versions never saved cannot be counted. Keep the unmeasured gap
separate, even if the measurable count is zero. Empty selection can export an
explicit empty snapshot. Attachment coverage, historical context gaps, and raw EOF
are independent. The existing `COMPLETE` means internally complete payload files,
not complete history or successful cloud delivery.

Snapshot publication writes payloads and manifest before `COMPLETE`. A context-only
or coverage-only change produces a new snapshot; unchanged reuse also rechecks
the exact machine payload files. An incomplete directory is never accepted.
Legacy v3 snapshots without `machine_handoff` have no context coverage evidence.

## Refresh response and recovery

`refresh` writes one JSON object to stdout. Diagnostics go to stderr. Its stable
outer fields are `schema: we-groupchat-obsidian.quiet-archive.refresh.v1`,
`state: eof|pending|failed`, `completed`, and `snapshot_id` (null without a valid
snapshot from this invocation), plus `capture`, `handoff`, and `coverage` when
available. Exit 0 requires raw EOF and a successfully written/confirmed machine
snapshot. Exit 2 means pending or failed; history/file gaps remain separate from
the raw-EOF exit decision.

A budget-limited pending run can export committed partial progress. A consumer
must use the returned exact snapshot, compare `coverage.capture_run_id` to that
snapshot, and label remaining work. On failure, timeout, invalid JSON or absent
snapshot it must not silently select an older snapshot as a successful refresh.
Retry continues the committed scanner cursor. A caller may cap the whole process
at 600 seconds; that timeout does not roll back already committed pages. A failed
source read never authorizes restarting WeChat, starting the app, accessing
attachment bytes or invoking a model.

Backfill apply reopens the source to verify the exact staged inventory, selection,
payload digest and expiry, but does not rescan staged messages. Failed/degraded
plans cannot apply. Fix the reported source/selection condition and create a new
explicit plan; never relabel incomplete history as complete. Export can be retried
from the private ledger without reopening WeChat.
