# Windows portability map (W0.2B.1)

The full Windows programme contract, `WGO-WIN-SPEC-2`, remains external
owner-review candidate material and is not distributed with this repository.
`PR-W0.2B.1` is authorized as a bounded path-identity foundation; this
document is its sole in-repository executable authority and the living
portability classification.

```text
repository: IndelibleVivi/we-groupchat-obsidian
requested_ref: feat/windows-port-w0-2b1-path-identity-main
resolved_sha: see_pr_head
default_branch_sha: 7a318c0f2a85c587f05f5013dfd301d67efb4702
merge_base: 7a318c0f2a85c587f05f5013dfd301d67efb4702
w0_1_spec_baseline_sha: a25af75468588cad6f32fef1a3358b40b9036917
w0_1_implementation_sha: 27c46226d21e540518a868c0ff55498b9f55bb3e
last_reconciled_main_sha: 7a318c0f2a85c587f05f5013dfd301d67efb4702
w0_2a_squash_merge_sha: 7a318c0f2a85c587f05f5013dfd301d67efb4702
classification_scope: living module/import status
applies_to: feat/windows-port-w0-2b1-path-identity-main
release_tier_affected: W0 only
```

W0.2B.1 adds concrete macOS and Windows path-identity providers behind the
existing platform contract. It distinguishes human display paths, absolute
operational paths, filesystem identity keys, and slash-normalized
source-relative paths. Windows support is limited to local NTFS, rejects
reparse points and unsupported namespaces/filesystems, and treats UNC only as
a syntax fixture. No existing storage, resource, source, monitor, or UI owner
is migrated in this phase. It does not enable Windows source discovery, key
acquisition, database reads, monitor writes, attachments, backup, tray UI,
autostart, packaging, or message sending. Existing macOS entrypoints and
behavior remain authoritative.
MCP message sending is now retired across platforms; `mcp_server.py` remains
only as an optional legacy read-only compatibility surface.

## Classification

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
| `app.py` | `macos-only` | rumps/AppKit/objc menu shell; reusable controllers are extracted in later staged PRs. |
| `mcp_server.py` | `deferred-w1+` | Optional legacy read-only compatibility surface; Windows source factory activation belongs to W1.1/W2. |
| `setup.py` | `macos-only` | py2app packaging entrypoint; Windows packaging is W6. |
| `ai/__init__.py` | `windows-import-safe` | Empty shared provider package boundary. |
| `ai/base.py` | `windows-import-safe` | Platform-neutral provider interface. |
| `ai/claude_provider.py` | `windows-import-safe` | Provider adapter imports cleanly; credentials remain behind the W0.3 secret boundary. |
| `ai/factory.py` | `windows-import-safe` | Imports cleanly on Windows; provider creation still reaches the macOS keychain and is not activated before W0.3. |
| `ai/ollama_provider.py` | `windows-import-safe` | Platform-neutral HTTP provider adapter; runtime behavior is not a Windows product-support claim. |
| `ai/openai_provider.py` | `windows-import-safe` | Provider adapter imports cleanly; credentials remain behind the W0.3 secret boundary. |
| `core/__init__.py` | `windows-import-safe` | Empty shared package boundary. |
| `core/api_errors.py` | `windows-import-safe` | Provider-independent error normalization. |
| `core/app_runtime.py` | `windows-import-safe` | W0.2A singleton now uses the portable exclusive non-blocking lock; Windows app activation remains W6. |
| `core/attachment_archive.py` | `deferred-w0.2` | Direct `fcntl` plus path/private-storage semantics; Windows bytes remain W5. |
| `core/attachment_backup.py` | `deferred-w0.2` | Transitively imports attachment/config storage. |
| `core/background_jobs.py` | `windows-import-safe` | Shared process-lifetime job coordination; behavior activation remains gated. |
| `core/bookmark.py` | `deferred-w0.2` | Transitively imports ConfigStore/private storage. |
| `core/chat_groups.py` | `deferred-w0.2` | Transitively imports ConfigStore/private storage. |
| `core/config.py` | `windows-import-safe` | W0.2A config locking is portable; W0.2B.1 does not migrate config paths, while atomic publication and private-storage hardening remain W0.2B.2. |
| `core/daily_digest.py` | `deferred-w0.2` | Transitively imports config/knowledge storage; Windows activation is W3. |
| `core/decryptor.py` | `windows-import-safe` | Shared crypto/WAL implementation; synthetic behavior fixtures expand in W1. |
| `core/google_drive_auth.py` | `deferred-w0.2` | Config/private storage plus macOS Keychain; secret adapter is W0.3. |
| `core/google_drive_client.py` | `deferred-w0.2` | Transitively imports the current auth adapter. |
| `core/google_drive_file_sync.py` | `deferred-w0.2` | Direct `fcntl` and attachment/config dependencies; Windows activation is later. |
| `core/image_decoder.py` | `windows-import-safe` | Platform-neutral byte decoding. |
| `core/key_extractor.py` | `macos-only` | macOS process scanner, codesign, sudo, and osascript source adapter. |
| `core/keychain.py` | `macos-only` | macOS `security` adapter; shared secret contract is defined for W0.3 wiring. |
| `core/knowledge.py` | `deferred-w0.2` | Transitively imports ConfigStore/path/private storage; Windows activation is W3. |
| `core/launch_agent.py` | `macos-only` | macOS LaunchAgent adapter; Windows autostart is W6. |
| `core/link_preview.py` | `windows-import-safe` | Platform-neutral exact URL extraction plus inert zero-network compatibility receipts; remote preview is retired. |
| `core/mcp_config.py` | `windows-import-safe` | Pure configuration rendering; Windows command emission is activated later. |
| `core/monitor.py` | `deferred-w0.2` | Transitively imports config/knowledge/review storage; Windows activation is W3. |
| `core/monitor_source.py` | `windows-import-safe` | Pure bounded source-cursor merge helper; platform storage remains owned by callers. |
| `core/monitor_state.py` | `windows-import-safe` | W0.2A shared/exclusive locking and revision CAS are portable; Windows monitor activation remains W3. |
| `core/notification_identity.py` | `macos-only` | Foundation/app-bundle notification identity diagnostics. |
| `core/notification_target.py` | `macos-only` | Emits the macOS `open` command; target-opening adapter is W6. |
| `core/platform/__init__.py` | `windows-import-safe` | Exposes contracts, stable path errors, and active-platform lock/path selectors. |
| `core/platform/contracts.py` | `windows-import-safe` | Shared lock/path/storage/secret/process/notification/open/autostart protocols; W0.2B.1 defines stable `path_identity_unknown` and `reparse_point_conflict` failures. |
| `core/platform/factory.py` | `windows-import-safe` | Fail-closed service registry with lazy macOS and Windows lock/path providers; all later capabilities remain absent. |
| `core/platform/macos_locks.py` | `macos-only` | Native `fcntl.flock` shared/exclusive backend retained for current macOS behavior. |
| `core/platform/macos_paths.py` | `macos-only` | Concrete inode identity; valid-UTF-8 missing-child identity is limited to APFS and uses volume-reported case semantics. HFS+ and unknown normalization rules fail closed. |
| `core/platform/windows_locks.py` | `windows-import-safe` | Native `LockFileEx` shared/exclusive backend with retained handles and stable `worker_busy` conflicts. |
| `core/platform/windows_paths.py` | `windows-import-safe` | Local-NTFS identity via handle-relative NT traversal, retained ancestry handles, volume/file IDs, extended operational paths, and fail-closed reparse/case-sensitive/unsupported-filesystem checks. |
| `core/project_identity.py` | `windows-import-safe` | Shared public project identifiers. |
| `core/relation_audit.py` | `windows-import-safe` | Imports without platform services; filesystem behavior remains unclaimed. |
| `core/relation_markdown_cleanup.py` | `deferred-w0.2` | Transitively imports knowledge/config storage. |
| `core/resource_backup_launch_agent.py` | `macos-only` | Retired/current LaunchAgent compatibility surface. |
| `core/resource_backup.py` | `deferred-w0.2` | Direct `fcntl`, path identity, target lock, and atomic semantics; Windows is W5. |
| `core/resource_capture.py` | `deferred-w0.2` | Direct `fcntl` and source/config dependencies; Windows is W4. |
| `core/review_queue.py` | `deferred-w0.2` | Transitively imports ConfigStore/private storage; Windows activation is W3. |
| `core/source_contract.py` | `windows-import-safe` | Existing shared source-metadata helpers; canonical WeChatSource extraction is W1.1. |
| `core/source_inventory.py` | `windows-import-safe` | W0.2A inventory serialization is portable; W0.2B.1 does not migrate inventory paths, while private/atomic completion and source activation remain W0.2B.2/W1+. |
| `core/source_metadata_plan.py` | `deferred-w0.2` | Transitively imports digest/knowledge/config storage. |
| `core/taxonomy_assignment.py` | `windows-import-safe` | Platform-neutral taxonomy resolution. |
| `core/taxonomy_migration.py` | `deferred-w0.2` | Direct `fcntl` and knowledge storage. |
| `core/url_safety.py` | `windows-import-safe` | Stdlib-only canonical URL display/export/prompt redaction. |
| `core/wechat_db.py` | `windows-import-safe` | Existing shared crypto/query import surface; schema/source adapters are W1.1+. |
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
| `scripts/health_check.py` | `macos-only` | Privacy-safe reliability matrix plus LaunchAgent/notification/macOS source diagnostics; its Windows line reports the W0.2B.1 lock/path-source-only boundary. |
| `scripts/migrate_taxonomy.py` | `operator-deferred` | Depends on W0.2 config/knowledge storage. |
| `scripts/organize_obsidian.py` | `operator-deferred` | Depends on W0.2 path/storage and W3 projection activation. |
| `scripts/refresh_data_source.py` | `macos-only` | Invokes the current macOS key/process adapter. |
| `scripts/repair_relation_markdown.py` | `operator-deferred` | Depends on knowledge/config storage. |
| `scripts/repair_relations.py` | `operator-deferred` | Depends on configured private relation state. |
| `scripts/resource_backup.py` | `macos-only` | Current LaunchAgent/source/backup operator surface; Windows backup is W5. |
| `scripts/review_queue.py` | `operator-deferred` | Depends on W0.2 storage and W3 activation. |
| `scripts/wechat_source_guard.py` | `macos-only` | Current LaunchAgent and macOS source-guard operator. |

## Staged cutover

1. **W0.2A:** implement macOS/Windows file-lock backends and migrate only
   `core/config.py`, `core/app_runtime.py`, `core/monitor_state.py`, and
   `core/source_inventory.py`. This phase is source portability, not product
   activation.
2. **W0.2B.1:** provide concrete macOS/Windows path identities and prove path
   alias, Unicode, long-path, missing-final, UNC-syntax, reserved-name, and
   reparse and held-ancestor boundaries without migrating existing callers.
3. **W0.2B.2:** add private-storage enforcement and atomic publication, then
   migrate only the explicitly authorized storage owners.
4. **W0.3:** adapt secrets behind the contract. Notifications, target opening,
   tray behavior, packaging, and logon startup remain W6.
5. **W1.1–W1.3:** extract the canonical WeChat source contract, add one
   exact-build Windows probe/schema profile, and add a verified key provider.
6. **W2–W6:** enable read-only source, knowledge, resources, backup, then tray,
   packaging, and logon startup only after their separate live gates.

Direct `fcntl` ownership removed in W0.2A: `core/config.py`,
`core/app_runtime.py`, `core/monitor_state.py`, and `core/source_inventory.py`.
Direct application-level `fcntl` remains intentionally deferred in attachment,
resource capture/backup, taxonomy migration, Google Drive file sync, and the
macOS-only source guard. The canonical macOS lock backend necessarily retains
its native `fcntl` implementation.

No module changes classification merely because it imports. The owning phase
must supply its behavioral tests and acceptance evidence first.
