# Repository agent contract

## Source ownership

- `app.py` is the macOS menu-bar and py2app application entrypoint.
- `mcp_server.py` is the direct FastMCP entrypoint.
- MCP is an optional legacy read-only compatibility surface. It may query and
  summarize, but must not mutate bookmarks/groups or touch WeChat UI. The old
  send tool names are inert `mcp_send_retired` stubs; legacy send config keys
  remain loadable but inactive. Do not restore sender/policy/confirmation
  modules or a send-side effect path.
- `core/monitor_state.py::MonitorStateStore` is the sole monitor-checkpoint
  write authority. Existing state migrates only after a valid parse; corrupt,
  symlink or non-regular state fails closed. TopicMonitor commits one expected
  revision after successful work and never advances source state after an AI
  failure. A retryable AI failure may CAS-patch only content-free failure
  count/code/timestamps and the bounded backoff deadline against the revision
  that was read; source cursors, inventory bindings and checkpoints must remain
  byte-for-byte unchanged, and a run inside that backoff makes no provider call.
  `core/monitor_source.py` owns the bounded raw-row reader. Canonical progress
  is per chat x logical-shard generation in `source_cursors`; `last_checked_ts`
  is derived compatibility evidence, not source authority. Raw pages merge by
  `create_time` then `source_message_id`, and only actually consumed rows may
  advance tentative cursors. Filtered rows advance without entering an AI
  prompt; `no_messages` requires verified raw EOF under the same complete
  inventory. A generation change or state-revision conflict commits no cursor.
  Knowledge events created before a state conflict reuse their stable
  `source_batch_id` on retry and must not create a second canonical event. If
  that committed event's managed topic Markdown is missing, reuse repairs the
  projection and date indexes without changing canonical event identity.
  Catch-up apply must stop the managed LaunchAgent, acquire the same
  `AppInstanceLock` as the menu app, hold it through backup/drain/projection/
  validation and a durable provisional receipt, release it, and only then
  restore the LaunchAgent. The same `run_id` receipt is atomically finalized
  with the observed restore result; it must not report `complete` while restore
  is pending or after restore failed.
- `core/source_inventory.py::SourceInventoryStore` owns the durable expected
  message-shard set. Logical shard identity is source namespace plus normalized
  relative path; database generation remains separate. Missing, keyless,
  cache-only, unreadable, and changed-generation states must remain explicit.
  Monitor/catch-up may read only a complete inventory. Resource and Direct
  Drive scanners may consume present generations while reporting
  `source_degraded`; they must never relabel that partial observation complete.
- `scripts/health_check.py` is the canonical operator health matrix. Its
  default output is path-free and content-free: no chat identity/body,
  source-relative path, local absolute path, API endpoint, or token. It must
  distinguish monitor healthy/missing/corrupt/conflict, source inventory
  completeness/counts, raw cursor progress, mounted handoff with provider sync
  unknown, separate Direct Drive verification, disabled link preview, legacy
  read-only/send-retired MCP, and the Windows W0.2B.1 lock-and-path-only boundary.
  Health inspection must not scan source, initialize/migrate the source
  inventory, open CAS payload objects, or promote mounted evidence to remote
  verification. Local details require explicit `--sensitive`.
- `setup.py` is the py2app packaging entrypoint. These are the only Python
  files that belong at repository root.
- `core/` owns domain behavior, durable state, privacy boundaries, recovery,
  backup and projection contracts.
- `core/config.py::ConfigStore` is the sole main-config write authority. Writers
  patch the latest locked revision; do not reintroduce whole-snapshot UI/CLI
  saves or non-atomic config writes. `core/app_runtime.py` owns the menu-app
  process singleton.
- `core/url_safety.py` is the sole URL display/export/prompt redaction
  authority. Exact observed URLs and their stable hashes remain unchanged only
  in private durable ledgers. Human-readable projections, snapshots/exports,
  Review Queue, Daily Digest, AI prompts and surfaced errors must use the
  canonical redacted form. The in-process remote link preview path is retired
  and inert: legacy `monitor_fetch_links: true` must still produce
  `link_preview_disabled` with zero network requests. Legacy mounted
  `link_export_mode=full` migrates to `redacted`; only `redacted|off` may be
  selected. Do not add a crawler or another local redaction implementation.
- `ai/` owns provider adapters; `ui/` owns reusable UI components.
- `scripts/` contains thin operator entrypoints and compatibility cleanup
  commands. Put
  reusable behavior in the owning package rather than duplicating it in a CLI.
- Source-guard and mounted-resource timers run inside the long-lived py2app
  menu-bar process. macOS App Data consent is process-lifetime access, so their
  retired short-lived LaunchAgent modes must remain no-op cleanup surfaces and
  must not be reintroduced as Python or app-bundle interval workers.
- Explicit resource CLI source operations remain operator entrypoints, but app
  and CLI capture/backfill runs share the resource capture operation lock.
  Historical backfill is staged: plan writes bounded keyset pages and apply
  requires the exact unexpired `run_id`; never restore a confirm-then-rescan
  path. The plan binds `inventory_digest`; apply must reopen the source,
  re-read the exact inventory digest, and fail closed before consuming staged
  rows if the inventory is unavailable, incomplete, or changed.
- `launchers/` owns the canonical Finder-friendly `.command` entrypoints. The
  root `启动.command` is a compatibility stub for deployed source-mode
  LaunchAgents and must not grow a second implementation.
- `tests/` is an importable unittest package. New tests belong there and use
  `tests.<module>` for focused invocation.

## Windows port staging

- The full Windows programme contract, `WGO-WIN-SPEC-2`, remains external
  owner-review candidate material. `PR-W0.2B.1` is authorized as a bounded
  path-identity foundation; `docs/WINDOWS-PORT-MAP.md` is its sole
  in-repository executable authority and the living module/import
  classification.
- `app.py` remains the macOS shell. W0.2B.1 must not add Windows source reads, key
  acquisition, monitor activation, attachment/backup behavior, tray UI,
  autostart, packaging or message sending.
- `core/platform/` owns platform contracts and fail-closed provider selection.
  W0.2A supplies concrete macOS/Windows file-lock adapters. W0.2B.1 adds only
  concrete path-identity providers. Atomic publication and private storage
  remain W0.2B.2; secrets remain W0.3;
  notifications/open/autostart/tray/packaging remain W6; source adapters begin
  in W1.
- W0.2A migrates direct lock ownership only in `core/config.py`,
  `core/app_runtime.py`, `core/monitor_state.py`, and
  `core/source_inventory.py`. Preserve ConfigStore sole-writer semantics,
  AppInstanceLock process singleton ownership, MonitorState revision CAS, and
  SourceInventory completeness/revision semantics.
- W0.2B.1 distinguishes `display_path`, `operational_path`, `identity_key`, and
  slash-normalized `source_relative_path`. Windows paths are native strings and
  must never pass through POSIX shell unescaping. Initial live identity support
  is local NTFS; UNC remains syntax-only, and reparse points, case-sensitive
  directories, unsupported filesystems/namespaces, root escape, reserved names,
  and trailing-dot/space components fail closed. Do not migrate existing
  storage/resource/source callers in this phase.
- `docs/WINDOWS-PORT-MAP.md` is the living module inventory. Every root,
  `ai/`, `core/`, `ui/` and `scripts/` Python module must remain classified,
  and only modules marked `windows-import-safe` enter the Windows import gate.
  Import success is not evidence of Windows feature support.

## Durable and generated boundaries

- SQLite/CAS ledgers are authoritative for durable derived state. Markdown,
  indexes, digests, target views and SVG exports are rebuildable projections.
- Attachment-byte consent is process/session-local and must never be persisted.
  WeChat decrypted caches and source shard/message identities are namespaced by
  source root; plaintext SQLite snapshots use Online Backup so WAL state is not
  lost.
- Source-inventory evidence is path-free and content-free. A mounted snapshot's
  `catalog_complete` marker binds the durable exported catalog only; its
  separate `source_observation.complete` field is the authority for whether the
  current WeChat source set was completely observed.
- Resource projection manifests own generated-path GC. Empty selections still
  render an explicit root; GC may remove only app-owned generated files and must
  hold canonical capture/selection authority, then the DB-scoped backup lock
  plus the real-path-keyed output-root lock. Mounted handoff also takes its
  target-side lock. Distinct capture databases/path aliases cannot concurrently
  own the same projection or mount, and local generated descendants must not
  follow symlinks.
- Editable architecture truth lives in `docs/architecture/*.excalidraw`; SVGs
  under `docs/assets/architecture/` are portable generated exports and must be
  regenerated and visually inspected after source changes.
- Runtime config, WeChat keys/cache, chat-derived databases, logs, Obsidian
  output, OAuth material, mounted-target receipts and attachment bytes never
  belong in Git.
- Public publication must preserve public-safe defaults and exclude raw chat
  identities/bodies, account identifiers, local paths, credentials, private
  continuity and live runtime data.
- `scripts/build_share_package.py` packages the exact Git commit tree. Its
  generated guide comes from an exact-commit tracked template and is mode/hash
  bound under manifest `controls`; the no-Git path is manifest-only and
  hash-verifies regular non-symlink payload/control entries. Do not reintroduce
  recursive fallback scanning or live-runtime control text.

## Verification and deployment

Run from repository root:

```bash
.venv/bin/python -m unittest discover -s tests -t . -p 'test_*.py'
.venv/bin/python -m compileall -q app.py mcp_server.py setup.py ai core ui scripts tests
for launcher in 启动.command launchers/*.command; do bash -n "$launcher"; done
```

Use focused `tests.<module>` runs while iterating, then the full suite for
shared code, packaging, public-boundary or runtime changes. Source completion,
private/public publication, app-bundle rebuild, LaunchAgent reload and live
acceptance are separate gates. Do not mutate live config/data or reload a live
agent merely because source tests pass.

For W0.2B.1 on Windows, also run:

```powershell
.\.venv\Scripts\python.exe -m unittest `
  tests.windows `
  tests.test_repository_layout `
  tests.test_config.ConfigTests.test_config_store_preserves_concurrent_disjoint_process_updates `
  tests.test_monitor_state.MonitorStateStoreTests.test_two_processes_cannot_replace_the_same_revision `
  tests.test_source_inventory.SourceInventoryStoreTests.test_concurrent_reconcile_preserves_inventory_union_and_revisions
.\.venv\Scripts\python.exe -m compileall -q mcp_server.py ai core ui scripts tests
```

The portability workflow is the macOS regression authority; a Windows host
cannot waive or simulate that gate.

## Review ref resolution

Before producing a code-review finding, resolve and print `repository`,
`requested_ref`, `resolved_sha`, `default_branch_sha`, `merge_base`, and
`applies_to`. When Faye supplies a named active branch, PR branch, or review
URL, inspect that exact immutable SHA. Never transfer a finding from the
default branch to the active branch, and never fall back to `main` when the
requested ref cannot be resolved; fail closed instead. Every finding title
must include `applies_to=<ref>@<sha>`. If a default-branch defect is already
closed on a verified active branch, label both facts explicitly.

## Documentation triggers

Update README EN/ZH when entrypoints, supported behavior, privacy boundaries,
installation, repository layout or ordinary commands change. Update operator
guides for changed procedures and this file when source ownership, required
verification or deployment gates change.
