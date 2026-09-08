# WGO recovery hardening acceptance

This document records the source contract added on top of public baseline
`c9e5fdaf342674661ce40a5eea3a3ded1321e6d7`. It is not an installed-app or live
WeChat attestation. The exact protected profile remains macOS WeChat
`4.1.11 (269136)` arm64; a version string from another distribution channel is
not interchangeable evidence.

## Source behavior

### Key recovery

`core/key_extractor.py::recover_keys` returns a structured observed-key result:

| State | Meaning |
| --- | --- |
| `fresh_verified` | This scan produced page-one HMAC-verified candidates and every currently observed required encrypted DB has a verified key. |
| `partial` | Verified discoveries were retained, but at least one current required DB remains unreadable or lacks a verified key. |
| `cache_only` | A known-profile scan produced no fresh verified key; old cache bytes were preserved but are not refresh success. |
| `unsupported_build` | No exact protected profile and no page-verified legacy candidate. |
| `failed` | Setup, target identity, scanner, source race, cache parse, or publication failed. |

The C scanner staging JSON is never cache authority. Every admitted candidate
is re-matched against a freshly read encrypted page one, and every writer uses
the same locked fresh-read/reverify/merge/fsync/replace path. Unknown protected
profiles are not guessed. Key freshness does not prove expected source-shard
completeness or successful row decoding.

The scanner executable comes only from an immutable build directory. Its
receipt binds the C source bytes, compiler path/binary/version/target, explicit
target architecture and flags, and output digest/size. Source, binary, and
receipt are complete and fsynced before one atomic `scanner-current.json`
pointer publishes the build. Fixed legacy binaries, partial directories,
tampered receipts/binaries, and unattributed mock-style success values are not
execution authority.

### Exact re-sign target

`core/wechat_resign.py` owns the high-impact operation; the launcher and
`scripts/resign_wechat.py` are thin explicit-consent entrypoints. One canonical
bundle path is inspected before privilege acquisition. When running, its PID,
launch time, executable path/inode, version, and build are bound and rechecked
after privilege acquisition. Only that application receives a normal terminate
request. Timeout or identity change stops before signing. Signing, independent
strict verification, exact-path reopen, and new-process identity verification
must all succeed.

There is no name-based `killall`, default-app rediscovery after mutation, or
automatic SIP/permission broadening. Multiple running copies fail closed unless
the operator supplied `--wechat-app=/exact/path/WeChat.app`. Ordinary startup
still never re-signs without `--allow-wechat-resign`.

### Generation admission

First enablement keeps the established from-now policy. A legacy monitor state
with no per-shard cursors may perform its one compatibility migration. Once any
logical-shard binding exists, a replacement physical generation or a newly
discovered logical shard stops before every page read and provider call with
`source_generation_admission_required`. The checkpoint file is unchanged.

The returned content-free v1 plan binds exact inventory digest/revision,
logical shard, previous/current generation, prior cursor, reason, and the
bounded reconciliation-row limit. The plan is deliberately non-executable:
the old cursor alone cannot prove that the replacement contains the same
prefix. Ordinary monitoring never copies it or substitutes the global
timestamp. An operator migration must later supply a stable-prefix proof or an
explicit bounded replay/reconciliation decision.

Durable `source_message_id` is separated from physical cursor generation. It
uses stable source namespace and logical cache basename, while the envelope
carries `source_generation_id` separately. This preserves identity for the
same logical row across an explicitly reconciled rebuild and prevents physical
inode churn alone from manufacturing duplicate message identities.

### Canonical event and projection crash recovery

Compressed-row decode errors raise `source_message_decode_failed`. They cannot
become presentation-filtered empty rows, consume catch-up page budget, invoke
the provider, or advance a cursor.

Daily Digest output uses a same-directory private temporary file, file fsync,
atomic replace, and parent-directory fsync. The last-known-good page remains on
publication failure. Every new canonical event inserts a
`daily_digest_changes` row in the same SQLite transaction. Existing affected
pages are rebuilt from canonical SQLite; the exact invalidation prefix is ACKed
only after every page that existed at inspection time was republished. The
event path drains immediately, and the menu timer drains every 60 seconds even
when the scheduled Digest is not due.

If a canonical event commits before the corresponding monitor-state CAS, the
next run checks its stable `source_batch_id` before any provider call. It repairs
missing topic/date projections, reuses the event, and commits the original
source cursor once. A failed canonical lookup leaves the cursor unchanged.
Thus projection repair does not create another event or ask the provider to
reinterpret the committed batch.

Catch-up output uses the actual `rebuild_projections()` `notes` and `actions`
fields. It does not invent event/topic counters or hide a producer/consumer
schema mismatch behind default zeroes.

## Verification boundary

Use a temporary `HOME` before Python imports so default runtime paths remain
syntactically real while no live state is reachable:

```bash
TEST_HOME="$(mktemp -d)"
HOME="$TEST_HOME" .venv/bin/python -m unittest \
  tests.test_key_extractor tests.test_refresh_data_source \
  tests.test_key_recovery_hardening tests.test_recovery_pipeline_hardening \
  tests.test_catch_up_monitor tests.test_monitor_source \
  tests.test_wechat_resign
HOME="$TEST_HOME" .venv/bin/python -m unittest discover -s tests -t . -p 'test_*.py'
HOME="$TEST_HOME" .venv/bin/python -m compileall -q \
  app.py mcp_server.py setup.py ai core ui scripts tests
for launcher in 启动.command launchers/*.command; do bash -n "$launcher"; done
```

The native C warning gate compiles `c_src/find_keys_macos.c` with
`-Wall -Wextra -O2 -arch <target> -framework Foundation` into a temporary
output and never executes it. Synthetic tests cover the real repository
producer/consumer chain for key publication, Digest→catch-up apply→receipt,
decode failure→cursor unchanged, exact re-sign binding, scanner identity,
generation pre-admission, and event-commit→projection-repair without provider
replay.

## `v0.1.0-alpha.1` bounded native canary

On 2026-09-06, public candidate `2353931a22e58ff1d1496d0224bfcc00c38924bf`
was exercised on macOS arm64 with explicit local authorization. The exact
WeChat `4.1.11 (269136)` target binding and running-process identity were valid,
so no re-sign mutation was needed. A fresh recovery scan returned
`fresh_verified`; the admitted scanner's source/compiler/architecture/flags
and executable bytes matched its durable receipt.

The catch-up canary deliberately allowed only one page per selected chat. It
therefore finalized honestly as `partial / resume_required`, not as terminal
success. The bounded run migrated legacy state to generation-bound shard
cursors, committed exactly one new canonical event, produced its non-empty
regular-file topic projection, reported zero duplicate message-hash groups,
and passed SQLite quick/integrity plus topics/FTS parity checks. Its provisional
and final receipt proved that the previously loaded LaunchAgent was restored;
readback found the job loaded and running. This verifies the write/recovery
path without claiming that the deliberately capped historical backlog reached
EOF.

The canary ran from the exact candidate checkout while preserving the existing
installed/runtime checkout. It is live-path evidence for the candidate, not a
claim that the candidate had already replaced the installed application.

## Still separate from source acceptance

Source tests alone do not attest the installed `.app`, LaunchAgent checkout, current
process, real AppKit metadata, actual App Store bundle, task-memory access, or
real chat source. A separately authorized canary must verify:

- the actual bundle/executable identity and exact re-sign/reopen behavior;
- the real compiler/scanner receipt and process CPU match;
- foreground/background and sleep/wake behavior;
- DB/WAL mutation, temporary source loss, a newly created shard, and overlap;
- one consented source message producing exactly one canonical event and an
  openable projection; and
- provider/read failure preserving checkpoint bytes and recovered inventory
  reaching verified EOF.

Generation plans whose prefix cannot be proven remain supervised migration
work; the source gate blocks them safely but does not silently choose between
loss and duplicate replay. No source-only result authorizes re-signing WeChat,
clearing cache, resetting checkpoints, reloading a LaunchAgent, running live
historical catch-up, or publishing a release tag.


## Monitor acceptance and frozen batches

The monitor now admits an explicit, typed decision before normalization or
progress. `match` must be a JSON boolean and `score` an integer from 0 to 100;
a positive decision must score at least 70 and contain a non-empty `digest`,
`summary` or supported `items` body. Known optional fields are type checked.
Duplicate JSON keys and non-finite JSON constants are rejected. A standalone
JSON code fence remains accepted; arbitrary prose containing an object does not.
An invalid response gets the content-free code `ai_invalid_response`, bounded
short retries and the existing durable backoff. It never becomes `no_match`.
A legitimate negative still advances normally. Dry-run writes no intent or
failure metadata.

One private checkpoint can contain one pending descriptor. It records only
IDs, fingerprints and cursor metadata, and is written by MonitorStateStore
before the provider call. Source progress is unchanged by this preparation
transition. AI failure then updates only failure metadata while retaining that
intent. On restart, production source reads use the frozen raw member count,
not the current configured page budget; the member IDs, content/resource
fingerprint, next cursors and inventory must agree. A newly appended message
stays for the next batch. A same-ID in-place edit or an inserted row that changes
the frozen prefix causes `monitor_pending_batch_changed`, not silent adoption.
A changed generation is blocked before source-page reads. The intent is removed
only in the successful source-progress CAS. Existing checkpoints without an
intent keep their compatibility path.

Pending checkpoints use monitor-state v2, which older readers reject instead
of silently ignoring frozen work. The new reader accepts v1 and v2; after a
successful acknowledgment the file returns to v1. Old code must not process a
v2 checkpoint. An unversioned/v1 file containing pending intent is corrupt,
not legacy-compatible progress. This guard does not make a destructive reset
or a rollback of unrelated knowledge migrations safe.

The checkpoint also binds the chat, interest and canonical knowledge destination.
Changing the provider/model to recover from a provider outage is allowed. Changing
the interest or destination pauses with `monitor_pending_policy_changed` until
the original policy is restored or a supervised migration is chosen. An explicit
reset-to-now abandons the interval and clears intent; it is not a lossless repair
command. Never suggest deleting a checkpoint to bypass these guards.

A separate per-checkpoint execution lock is held across a non-dry monitor run.
It is nonblocking, uses the existing platform lock provider, and is released by
process exit, including a crash. It does not hold the checkpoint-file lock across
network work, so configuration/maintenance writers can still produce a detectable
revision conflict. `monitor_worker_busy` never invokes AI or advances progress.

After an event commit/state-CAS failure, an identical frozen batch reuses its
canonical event before context lookup or another provider call. The new tests
change the source between attempts rather than replaying an unchanged fixture.
One test connects the repository's actual SQLite WeChat source adapter,
TopicMonitor, KnowledgeStore transaction and Markdown projection using a wholly
synthetic database. No test authorizes reading live chats or invoking a model.

### Limits retained deliberately

- A pending descriptor is not a raw-message archive. If required source rows
  disappear or change, recovery stops for source reconciliation; it cannot
  reconstruct lost raw bodies from a hash.
- This fixes batches prepared by the new path. An older already-committed event
  with no pending descriptor has only its existing exact-batch recovery evidence.
- A crash before canonical commit (including an in-flight/lost provider response,
  or a negative response before checkpoint publication) can repeat a provider
  request for the same frozen batch. This is not exactly-once external execution.
- Context is a fresh bounded read when uncommitted work needs evaluation; it is
  not a persisted prompt snapshot. Canonical-event recovery needs no context.
- Arbitrarily late old timestamps, source-generation admission, notification/
  review-queue replay and full-application restore remain separate work.
- Frozen batches apply to the production per-shard cursor path. Legacy adapters
  get strict response validation, but cannot promise durable raw membership.

### Verification

Run the native focused regression with a temporary HOME:

```bash
TEST_HOME="$(mktemp -d)"
HOME="$TEST_HOME" .venv/bin/python -m unittest \
  tests.test_monitor_acceptance tests.test_monitor tests.test_monitor_source \
  tests.test_monitor_resilience tests.test_monitor_state \
  tests.test_recovery_pipeline_hardening tests.test_app_monitor_resilience \
  tests.test_health_check
```

Then run the full native portability workflow. Linux tests using an explicitly
injected POSIX lock adapter are supplemental; they cannot waive macOS acceptance.
Tests cover malformed/schema-invalid responses, legitimate negatives, bounded
retry/backoff, dry-run and legacy boundaries, failed intent publication,
concurrent execution, owner crash, event commit followed by newly arriving rows,
changed run budgets, filtered rows, content/generation/policy drift, corrupted
intent, menu error reporting and identity-free health inspection.
