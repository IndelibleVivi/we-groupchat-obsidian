# Windows portability map (shared foundation and source seam)

The full Windows programme contract, `WGO-WIN-SPEC-2`, remains external
owner-review candidate material and is not distributed with this repository.
This document is the in-repository authority for the completed storage
foundation, W0.3 API/OAuth credentials, W1.1 shared source seam and living
portability classification. Contributor sequencing and exact-build acceptance
are in [Windows development](WINDOWS-DEVELOPMENT.md). It does not claim
completion or approval of the full external Windows programme.

W0.2A supplied native shared/exclusive file locks. W0.2B.1 supplied concrete
path identities. **W0.2B.2** adds private storage and atomic publication and
migrates only `ConfigStore`, `MonitorStateStore`, and `SourceInventoryStore`.
The storage tranche was integrated in `fc0f301`; the credential tranche in
`7f0bd9c`; the W1.1 source seam in `4dcf07d`. These are source baselines,
not installed/live or Windows feature-support claims.

Windows identity remains local-NTFS-only. UNC is a syntax fixture; reparse
points and unsupported namespaces/filesystems remain rejected. The native secret service now supports API/OAuth credentials. No Windows source,
attachment, backup, monitor job, tray, startup or packaging capability is enabled. MCP remains optional legacy read-only compatibility with a
separate SDK installation and explicit stdio entrypoint. Ordinary app startup
does not manage or probe it; the configured MCP client owns its lifecycle.

## W0.2B.2 storage contract

- `core/state_storage.py` binds the three state owners to the existing path,
  lock, private-storage, and atomic-publication services. The owners retain
  their schemas, JSON representation, revision/CAS rules and error vocabulary.
  Constructors do not create storage. Read-only monitor/inventory inspection
  neither creates a directory/lock nor changes permissions.
  Lock preparation leaves existing state-file bytes and permissions intact,
  including corrupt recovery evidence. A validated write publishes a private
  replacement rather than re-permissioning the old file before parsing it.
- Path admission uses the existing platform path provider. The final state
  file cannot be a symlink/non-regular node. Native Windows input is not passed
  through POSIX shell unescaping. File identities are not used as lock names:
  atomic replacement changes the file ID, while the admitted filename and its
  sibling lock must continue serializing writers.
- Private storage is enforced before publishing state. macOS uses current-user
  ownership and 0700 directories / 0600 regular files. Windows uses a protected
  NTFS DACL admitting the current user and SYSTEM. Windows mode bits are not
  evidence of privacy. Securing a directory must not recursively rewrite ACLs
  or permissions on existing ancestors or unrelated descendants.
- Atomic publication creates a private temporary file in the same directory,
  writes and flushes its complete payload, then replaces the destination once.
  A failure before replacement preserves the previous canonical bytes and
  revision. There is no delete-destination fallback. Cleanup owns only the
  operation's temporary file. Atomic visibility does not promise power-loss
  durability on every filesystem; Windows has no claimed directory-fsync
  barrier here.
- Existing `core.config.ensure_private_dir/file` remain for the explicitly
  unmigrated archive, knowledge, projection, and other Mac consumers. They are
  not used by the three migrated stores. Those callers are later work, not a
  second publication path for these stores. API/OAuth credentials use the separate W0.3 native secret service below.

Acceptance covers actual private storage and first/replacement publication,
pre-replace failure preservation, native path inputs, read-only inspection,
concurrent config patches, checkpoint revision conflicts, and inventory union.
Windows ACL and reparse tests run on Windows; macOS skips do not prove them.

## W0.3 protected credential contract

- `core.platform.create_secret_store()` selects macOS Keychain or Windows
  Credential Manager. Import and construction perform no credential reads.
  Missing loads return `None`; native access/write/delete failures raise
  content-free `SecretStoreError` codes. There is no plaintext sidecar fallback.
- `core/keychain.py` retains its historical name and bool/optional-value API for
  existing app, AI factory, health and configuration callers. It contains no
  native implementation; `secret_status` distinguishes missing from unavailable.
  This facade stays while those callers use that API. Native adapters own all
  credential operations. OAuth uses the strict service directly, reports
  unavailable storage distinctly and never reports a failed delete as success.
- Mac writes use service `we-groupchat-obsidian`. Reads try `wechat-summary`
  only when the current item is missing, not when access failed. Explicit delete
  removes both readable identities so a legacy token cannot reappear.
- Windows stores UTF-8 generic credentials under `we-groupchat-obsidian/<account>`
  for the current user on this computer (`CRED_PERSIST_LOCAL_MACHINE`, not
  enterprise roaming). Values exceeding the native 2560-byte blob limit fail
  before writing. API/OAuth calls do not invoke macOS `security` on Windows.
- AI construction has no config-key fallback. Ollama does not open the secret
  store. This tranche does not migrate the existing Mac `all_keys.json` cache
  or `image_aes_key` compatibility input. They have active Mac source/attachment
  callers; Windows must not adopt them as plaintext storage. Versioned imported
  database/image key validation belongs with the exact-build W1.3 key provider,
  after source evidence establishes what can actually be verified.
- Tests cover failed writes, missing vs inaccessible, legacy deletion, consumer
  errors, and synthetic native Windows save/update/new-process reload/delete.
  Native Windows test credentials have unique test-only identities; they never
  query real app accounts. macOS adapter regressions use an injected runner.

Native API authority: Microsoft [CredWriteW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew),
[CredReadW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreadw),
[CredDeleteW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-creddeletew),
and [CREDENTIALW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw).

## W1.1 source seam

`core/source_adapter.py` owns the shared `WeChatSource` protocol and stateless
source capability, cursor, inventory-binding, page and error helpers. The current
Mac `WeChatDB` implements the reader; monitor, resource capture and Direct Drive
reuse its shared rules. Resource and Direct reads prefer bounded keyset pages,
including large same-second buckets. Timestamp-only legacy adapters retain their
existing completeness behavior and growing same-second request; they do not
claim the keyset bound. Direct ledgers add an empty-default `source_cursor_token`
column while preserving existing rows; queue insert and token advancement share
one transaction. This is a local cursor addition, with no remote Drive schema
change. Decode/generation/inventory failures retain
their bounded source codes instead of being collapsed into an unrelated error.

The seam never writes checkpoints or owns the expected shard set. Monitor still
requires complete inventory; resource and Direct may consume present shards only
while reporting degraded source state. `core/source_contract.py` continues to own
Markdown provenance. Existing reader method signatures, message/generation
identities and monitor/inventory state formats remain unchanged. Mac schema/query/cache
implementation is not a Windows reader; actual Windows source/cache work begins
with the exact-build evidence in the [development guide](WINDOWS-DEVELOPMENT.md).

## Classification

Phase labels below retain their original names. `deferred-w0.2` means that
module's storage callers have not migrated; it does not mean the completed
shared foundation is still waiting to be implemented. Classification changes
require the owning phase's evidence, not a new label for an old import.

- `windows-import-safe`: imports in a fresh Windows Python 3.11 process. This
  is an import claim only, not a Windows behavior or release-tier claim.
- `deferred-w0.2`: blocked by direct or transitive POSIX lock, path migration,
  atomic publication, or private-storage ownership. W0.2B.2 owns private
  storage, atomic-publication completion, and bounded caller migration after
  the W0.2B.1 path-identity foundation.
- `deferred-w1+`: import work is possible, but source/product activation is
  owned by W1 or a later phase.
- `macos-only`: current macOS shell, packaging, or adapter. It is intentionally
  excluded from the Windows import gate until its owning platform PR.
- `operator-deferred`: thin operator entrypoint whose dependencies or behavior
  are not authorized on Windows in the current phase.

`tests.windows.test_portability_inventory` enforces exact coverage of every
root, `ai/`, `core/`, `ui/`, and `scripts/` Python module and imports every
`windows-import-safe` module in a fresh process on Windows.

## Module inventory

| Path | Classification | Current boundary and next owner |
|---|---|---|
| `app.py` | `macos-only` | rumps/AppKit/objc shell and long-lived runtime host; background jobs do not depend on menu visibility. Reusable controllers are extracted in later staged PRs. |
| `mcp_server.py` | `deferred-w1+` | Optional legacy read-only compatibility surface; Windows source factory activation belongs to W1.1/W2. |
| `setup.py` | `macos-only` | py2app packaging entrypoint; Windows packaging is W6. |
| `ai/__init__.py` | `windows-import-safe` | Empty shared provider package boundary. |
| `ai/base.py` | `windows-import-safe` | Platform-neutral provider interface. |
| `ai/claude_provider.py` | `windows-import-safe` | Provider adapter imports cleanly; API credentials use the native W0.3 secret boundary. |
| `ai/factory.py` | `windows-import-safe` | API keys use the active native secret store; Ollama bypasses credentials; no config-key fallback. |
| `ai/ollama_provider.py` | `windows-import-safe` | Platform-neutral HTTP provider adapter; runtime behavior is not a Windows product-support claim. |
| `ai/openai_provider.py` | `windows-import-safe` | Provider adapter imports cleanly; API credentials use the native W0.3 secret boundary. |
| `core/__init__.py` | `windows-import-safe` | Empty shared package boundary. |
| `core/api_errors.py` | `windows-import-safe` | Provider-independent error normalization. |
| `core/app_runtime.py` | `windows-import-safe` | W0.2A singleton now uses the portable exclusive non-blocking lock; Windows app activation remains W6. |
| `core/attachment_archive.py` | `deferred-w0.2` | Direct `fcntl` plus path/private-storage semantics; Windows bytes remain W5. |
| `core/attachment_backup.py` | `deferred-w0.2` | Transitively imports attachment/config storage. |
| `core/background_jobs.py` | `windows-import-safe` | Shared process-lifetime job coordination; behavior activation remains gated. |
| `core/bookmark.py` | `deferred-w0.2` | Bookmark JSON writes retain the existing config permission helpers; this storage caller has not migrated. |
| `core/chat_groups.py` | `deferred-w0.2` | Group JSON writes retain the existing config permission helpers; this storage caller has not migrated. |
| `core/config.py` | `windows-import-safe` | W0.2B.2 uses native private storage, path admission and atomic publication; config revision/sole-writer rules are unchanged. |
| `core/daily_digest.py` | `deferred-w0.2` | Transitively imports config/knowledge storage; Windows activation is W3. |
| `core/decryptor.py` | `windows-import-safe` | Shared crypto/WAL implementation; synthetic behavior fixtures expand in W1. |
| `core/google_drive_auth.py` | `deferred-w0.2` | Native protected refresh tokens; OAuth client-config file storage remains Mac-owned and Windows Direct Drive is deferred. |
| `core/google_drive_client.py` | `deferred-w0.2` | Transitively imports the current auth adapter. |
| `core/google_drive_file_sync.py` | `deferred-w0.2` | Direct `fcntl` and attachment/config dependencies; Windows activation is later. |
| `core/image_decoder.py` | `windows-import-safe` | Platform-neutral byte decoding. |
| `core/key_extractor.py` | `macos-only` | macOS process scanner, codesign, sudo, and osascript source adapter. |
| `core/wechat_signature.py` | `macos-only` | Shared read-only macOS codesign predicate (strict, no runtime flag, legacy ad-hoc or managed stable identity); injectable runner only, no Windows signature or key support. |
| `core/keychain.py` | `windows-import-safe` | Compatibility facade for current credential callers; native operations live in platform adapters. |
| `core/knowledge.py` | `deferred-w0.2` | Transitively imports ConfigStore/path/private storage; Windows activation is W3. |
| `core/launch_agent.py` | `macos-only` | macOS LaunchAgent adapter; Windows autostart is W6. |
| `core/link_preview.py` | `windows-import-safe` | Platform-neutral exact URL extraction plus inert zero-network compatibility receipts; remote preview is retired. |
| `core/monitor.py` | `deferred-w0.2` | Transitively imports config/knowledge/review storage; Windows activation is W3. |
| `core/monitor_result.py` | `windows-import-safe` | Pure bounded outcome interpretation; unknown results block catch-up. No platform or product activation. |
| `core/monitor_source.py` | `windows-import-safe` | Pure source-cursor merge and pending-batch metadata validation; platform storage remains owned by callers. |
| `core/monitor_state.py` | `windows-import-safe` | W0.2B.2 private/atomic storage and admitted paths preserve checkpoint CAS and read-only inspection; Windows monitor activation remains W3. |
| `core/notification_identity.py` | `macos-only` | Foundation/app-bundle notification identity diagnostics. |
| `core/notification_target.py` | `macos-only` | Emits the macOS `open` command; target-opening adapter is W6. |
| `core/platform/__init__.py` | `windows-import-safe` | Exposes contracts and active-platform lock/path/private-storage/publication selectors. |
| `core/platform/contracts.py` | `windows-import-safe` | Shared platform protocols including private storage and atomic publication; path/storage errors are content-free. |
| `core/platform/factory.py` | `windows-import-safe` | Lazy macOS/Windows lock, path, private-storage, atomic-publication and secret providers; later capabilities remain absent. |
| `core/platform/macos_secrets.py` | `macos-only` | Native Keychain operations, bounded failures and legacy identity compatibility. |
| `core/platform/windows_secrets.py` | `windows-import-safe` | Native current-user Credential Manager; synthetic Windows behavior gate. |
| `core/platform/macos_locks.py` | `macos-only` | Native `fcntl.flock` shared/exclusive backend retained for current macOS behavior. |
| `core/platform/macos_private_storage.py` | `macos-only` | Current-UID 0700/0600 enforcement and same-directory atomic publication; no ancestor or unrelated child permission changes. |
| `core/platform/macos_paths.py` | `macos-only` | Concrete inode identity; valid-UTF-8 missing-child identity is limited to APFS and uses volume-reported case semantics. HFS+ and unknown normalization rules fail closed. |
| `core/platform/windows_locks.py` | `windows-import-safe` | Native `LockFileEx` shared/exclusive backend with retained handles and stable `worker_busy` conflicts. |
| `core/platform/windows_private_storage.py` | `windows-import-safe` | Handle-based protected NTFS DACL enforcement/verification and atomic publication; path admission remains in windows_paths. |
| `core/platform/windows_paths.py` | `windows-import-safe` | Local-NTFS identity via handle-relative NT traversal, retained ancestry handles, volume/file IDs, extended operational paths, and fail-closed reparse/case-sensitive/unsupported-filesystem checks. |
| `core/project_identity.py` | `windows-import-safe` | Shared public project identifiers. |
| `core/quiet_archive_handoff.py` | `deferred-w0.2` | Private local v3 context handoff using existing capture/backup POSIX storage and locks; not a Windows feature. |
| `core/quiet_archive_producer.py` | `deferred-w0.2` | Explicit standalone profile and local ledger/CAS adoption; reuses POSIX capture locks and current Mac source reader. |
| `core/relation_audit.py` | `windows-import-safe` | Imports without platform services; filesystem behavior remains unclaimed. |
| `core/relation_markdown_cleanup.py` | `deferred-w0.2` | Transitively imports knowledge/config storage. |
| `core/resource_backup_launch_agent.py` | `macos-only` | Retired short-lived job inspection/removal; installation returns `long_lived_app_required`. |
| `core/resource_backup.py` | `deferred-w0.2` | Direct `fcntl`, path identity, target lock, and atomic semantics; Windows is W5. |
| `core/resource_capture.py` | `deferred-w0.2` | Direct `fcntl` and source/config dependencies; Windows is W4. |
| `core/review_queue.py` | `deferred-w0.2` | Transitively imports ConfigStore/private storage; Windows activation is W3. |
| `core/source_adapter.py` | `windows-import-safe` | Canonical read-source protocol and shared capability, cursor, inventory, page and error helpers; no durable-state ownership. |
| `core/source_contract.py` | `windows-import-safe` | Generated Markdown provenance helpers; unrelated to WeChat source reading. |
| `core/state_storage.py` | `windows-import-safe` | Platform IO binding for the three migrated JSON state owners; no schema or revision authority. |
| `core/source_inventory.py` | `windows-import-safe` | W0.2B.2 private/atomic storage and admitted paths preserve completeness, revisions, and read-only inspection; source activation remains W1+. |
| `core/source_metadata_plan.py` | `deferred-w0.2` | Transitively imports digest/knowledge/config storage. |
| `core/taxonomy_assignment.py` | `windows-import-safe` | Platform-neutral taxonomy resolution. |
| `core/taxonomy_migration.py` | `deferred-w0.2` | Direct `fcntl` and knowledge storage. |
| `core/url_safety.py` | `windows-import-safe` | Stdlib-only canonical URL display/export/prompt redaction. |
| `core/wechat_db.py` | `windows-import-safe` | Current Mac WeChatSource implementation; import-safe only. Windows schema, key provider and private decrypted cache remain W1.2+. |
| `core/wechat_resign.py` | `macos-only` | Exact-target AppKit/codesign/sudo re-sign orchestration; Windows key-provider authorization belongs to W1+. |
| `core/wechat_signing_identity.py` | `macos-only` | macOS `security`/openssl adapter owning the persistent self-signed re-sign identity keychain; no Windows signing support. |
| `core/wechat_source_guard.py` | `macos-only` | `fcntl`, macOS key/process adapter, and osascript notification behavior. |
| `ui/__init__.py` | `windows-import-safe` | Empty reusable UI package boundary; Windows tray is W6. |
| `scripts/__init__.py` | `windows-import-safe` | Empty operator package boundary. |
| `scripts/attachment_archive.py` | `operator-deferred` | Depends on W0.2 storage and W5 attachment authorization. |
| `scripts/attachment_backup.py` | `operator-deferred` | Depends on W0.2 storage and later backup activation. |
| `scripts/autostart.py` | `macos-only` | macOS LaunchAgent installer. |
| `scripts/backfill_history.py` | `operator-deferred` | Source/knowledge operation; Windows historical behavior is not authorized in the current phase. |
| `scripts/build_share_package.py` | `operator-deferred` | POSIX mode/symlink publication semantics require a later filesystem audit. |
| `scripts/catch_up_monitor.py` | `macos-only` | LaunchAgent control plus monitor/source operations. |
| `scripts/configure_monitor.py` | `operator-deferred` | Depends on config, source, keychain, and knowledge activation. |
| `scripts/daily_digest.py` | `operator-deferred` | Depends on W0.2 storage and W3 activation. |
| `scripts/google_drive_file_sync.py` | `operator-deferred` | Depends on current auth/config/source adapters. |
| `scripts/health_check.py` | `macos-only` | Privacy-safe reliability matrix plus LaunchAgent/notification/macOS source diagnostics; its Windows line reports the shared storage/credential foundation without claiming product support. |
| `scripts/migrate_taxonomy.py` | `operator-deferred` | Depends on W0.2 config/knowledge storage. |
| `scripts/organize_obsidian.py` | `operator-deferred` | Depends on W0.2 path/storage and W3 projection activation. |
| `scripts/quiet_archive_handoff.py` | `macos-only` | Explicit protected-source capture/drain/backfill and local JSON handoff; reuses current Mac capture/backup owners. |
| `scripts/refresh_data_source.py` | `macos-only` | Invokes the current macOS key/process adapter. |
| `scripts/resign_wechat.py` | `macos-only` | Explicit-consent thin CLI for the exact-target macOS re-sign operation. |
| `scripts/repair_relation_markdown.py` | `operator-deferred` | Depends on knowledge/config storage. |
| `scripts/repair_relations.py` | `operator-deferred` | Depends on configured private relation state. |
| `scripts/resource_backup.py` | `macos-only` | Current LaunchAgent/source/backup operator surface; Windows backup is W5. |
| `scripts/review_queue.py` | `operator-deferred` | Depends on W0.2 storage and W3 activation. |
| `scripts/wechat_source_guard.py` | `macos-only` | Current LaunchAgent and macOS source-guard operator. |

## Staged cutover

The completed source tranches are retained here to explain the ownership cut;
they are not instructions to repeat that implementation:

1. **W0.2A — integrated:** macOS/Windows file-lock backends migrated only
   `core/config.py`, `core/app_runtime.py`, `core/monitor_state.py`, and
   `core/source_inventory.py`. This phase is source portability, not product
   activation.
2. **W0.2B.1 — integrated:** concrete macOS/Windows path identities cover path
   alias, Unicode, long-path, missing-final, UNC-syntax, reserved-name, and
   reparse and held-ancestor boundaries without migrating existing callers.
3. **W0.2B.2 — integrated:** private storage and atomic publication migrated only
   config, monitor checkpoints, and source inventory. Remaining storage owners
   retain their deferred classification and separate caller migrations.
4. **W0.3 — integrated:** native API/OAuth credential adapters and current consumers are wired.
   Imported source-key records follow exact-build validation in W1.3. Notifications, target opening,
   tray behavior, packaging, and logon startup remain W6.
5. **W1.1 — integrated:** current Mac reader and consumers use the shared source seam.

The next Windows source phases are **E1 / W1.2–W1.3**: collect real Windows
evidence, then implement an admitted exact-build probe/schema profile and a
protected verified key provider/private cache. **W2–W6** enable read-only source,
knowledge, resources, backup, then tray, packaging and logon startup only after
their separate gates. Follow [Windows development](WINDOWS-DEVELOPMENT.md) for
that sequence; the map does not authorize live source access.

Direct `fcntl` ownership removed in W0.2A: `core/config.py`,
`core/app_runtime.py`, `core/monitor_state.py`, and `core/source_inventory.py`.
Direct application-level `fcntl` remains intentionally deferred in attachment,
resource capture/backup, taxonomy migration, Google Drive file sync, and the
macOS-only source guard. The canonical macOS lock backend necessarily retains
its native `fcntl` implementation.

No module changes classification merely because it imports. The owning phase
must supply its behavioral tests and acceptance evidence first.
