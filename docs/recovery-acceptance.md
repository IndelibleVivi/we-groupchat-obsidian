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

## Still separate from source acceptance

Source tests do not attest the installed `.app`, LaunchAgent checkout, current
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
