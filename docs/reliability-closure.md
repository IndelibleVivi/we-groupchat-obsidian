# Reliability closure: contract and historical evidence

This document maintains the shared signature, redaction, catch-up outcome and
receipt-failure contracts below. It began as a bounded candidate on
`9ab1eeab25d1e7450e7c1fc9eef8b5fb55e60f3b` (recovery PR #18) and was integrated
through `2353931a22e58ff1d1496d0224bfcc00c38924bf` (PR #19).
The original authoring limits and excluded work are historical evidence, not a
current repository-wide backlog. Current source access and operator procedures
are in [source reliability](source-reliability.md); later monitor recovery is in
[recovery acceptance](recovery-acceptance.md). No section attests a current
installation or authorizes deployment.

## Changed behavior

The session binding `WE_GROUPCHAT_OBSIDIAN_WECHAT_APP_PATH` is resolved before
ordinary application discovery. The launcher forwards an inherited binding or
`--wechat-app=...` to Python, including its health entrypoint. A binding that is
empty or unavailable blocks; it never silently falls back to another copy.
This binding remains session-local. No LaunchAgent or persistent config is
changed, and a separately launched process does not inherit it automatically.

`core/wechat_signature.py` provides the common read-only predicate for the
launcher, `is_wechat_signed()`, and explicit re-sign verification. It invokes
`/usr/bin/codesign`, requires strict verification, reads the numeric
`CS_RUNTIME` bit from CodeDirectory flags, and accepts either one legacy
ad-hoc signature or a signature whose designated requirement anchors the
managed stable identity from `core/wechat_signing_identity.py`. Explicit
re-sign verification passes the expected certificate root, so ad-hoc output
and foreign certificates are rejected there. Paths containing the word
`runtime` do not cause a false rejection.
Missing or malformed verification evidence fails closed with a bounded code.
The caller still owns exact bundle/process binding around mutation. This
predicate is not an atomic filesystem identity or process-termination proof.

URL redaction now recognizes `token`, `secret`, and `jwt` word components in
snake_case, kebab-case, camelCase, dotted and bracketed names, plus compact
credential suffixes. Query duplicates and routed fragments use the same helper.
The app-level monitor error handler applies that helper before writing its
structured `monitor_runtime_error` line or sending a notification, and monitor
tracebacks redact URLs before reaching the error log.
Private canonical URL identity is unchanged. Arbitrary unknown parameter names,
path-embedded credentials, and credentials outside URLs are not made safe by
this finite name policy; do not advertise universal secret detection.

`core/monitor_result.py` interprets the existing TopicMonitor result contract.
Only known post-commit progress statuses consume catch-up page budget. All
other statuses stop that chat and preserve their bounded reason; a payload
with no valid status is not success. `no_messages` completes only with
`source_eof is True`. This is consumer classification, not a new commit ledger.
Preflight errors retain their source reason in each chat receipt rather than
being collapsed into missing-checkpoint errors. No source replay is authorized.

A failed provisional receipt remains a failure for the whole catch-up run.
The app lock is released and original LaunchAgent restoration is still
attempted. A later successful final write records the missing evidence and
cannot report `complete` or advertise safe automatic resume. If final
publication fails, console success and a zero exit status are withheld. A
failure after rename has an ambiguous durability outcome: inspect the recorded
receipt and command outcome before retrying; visible bytes alone are not proof
that the failed publication completed durably. The receipt schema stays v1.

Health treats the newest monitor event as authoritative. It preserves a bounded
result code, including unfamiliar stop codes, and reports an unstructured event
as `unknown` without substituting an older success. App-level failures use the
bounded `monitor_runtime_error` code. Its direct
`check_new_databases()` import/call is removed. Key coverage comes from the
existing durable inventory; current page-one verification belongs to explicit
refresh. Other health subsystems are unchanged by this patch.

## Regression and acceptance

Repository-native regression coverage:

- `tests.test_reliability_closure_modules`: redaction, shared signature predicate,
  positive outcome allowlist, malformed/unknown payloads and verified EOF.
- `tests.test_operator_reliability_closure`: session binding, actual catch-up
  orchestration and receipt writer with mocked external services, preflight
  reasons, sticky write failures, and health source-scan exclusion.

All test data is synthetic and temporary. Never run these by importing a live
operator environment. Set a temporary HOME and data directory before imports:

```bash
TEST_HOME="$(mktemp -d)"
export HOME="$TEST_HOME"
export WE_GROUPCHAT_OBSIDIAN_DATA_DIR="$TEST_HOME/wgo-data"
unset WE_GROUPCHAT_OBSIDIAN_WECHAT_APP_PATH
unset WE_GROUPCHAT_OBSIDIAN_ALLOW_RESIGN WECHAT_SUMMARY_ALLOW_RESIGN
# PYTHON must be an absolute path to an existing approved project interpreter.
"$PYTHON" -m unittest tests.test_reliability_closure_modules tests.test_operator_reliability_closure
"$PYTHON" -m unittest tests.test_wechat_resign tests.test_key_extractor tests.test_catch_up_monitor tests.test_health_check tests.test_startup_helpers
"$PYTHON" -m unittest discover -s tests -t . -p 'test_*.py'
"$PYTHON" -m compileall -q app.py mcp_server.py setup.py ai core ui scripts tests
for launcher in 启动.command launchers/*.command; do bash -n "$launcher" || exit; done
```

The complete suite requires the optional MCP test dependency as documented in
[AGENTS](../AGENTS.md#verification-and-deployment). These commands describe how
to obtain fresh evidence; their presence is not a record that they passed.

### Original candidate authoring limits

The original authoring environment could read pinned GitHub source but could
not obtain a complete checkout. Its evidence covered isolated unit checks on
captured production functions and the new modules, not complete-tree imports,
regression fixtures or the hosted Windows/macOS matrix. Subsequent source
integration does not turn those isolated results into full-suite or live
acceptance. Keep later results bound to their own tested commits and environments;
the dated native canary is recorded in [recovery acceptance](recovery-acceptance.md#v010-alpha1-bounded-native-canary).

## Original candidate exclusions

The following list preserves what the original bounded change did not settle.
It is not a current task list; consult the owning source/guide before deciding
whether a later change resolved a boundary:

Generation-admission execution and stable-prefix/bounded replay migration;
same-second cross-shard ordering; optional resource-backup completeness and
symlink inspection; Digest concurrent-publication behavior; scanner build and
re-sign race boundaries; durable custom-target autostart; installed bundle and
live AppKit/task-memory/sleep-wake acceptance; release artifacts, tags, and
upgrade/downgrade guarantees. None was implicitly approved by that patch.

No real chats, keys, config, checkpoints, source databases, mounted targets,
provider requests, permissions, app signatures or LaunchAgents are changed by
source-only acceptance. Keep the existing long-lived macOS process boundary.
