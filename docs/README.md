# Documentation and development history

WGO grew from a macOS menu-bar summary tool into a local knowledge, resource and
recovery workflow. Older names, compatibility commands and dated acceptance
records remain for different reasons. Start with the task below; a historical
plan or successful canary does not establish the state of a current installation.

## Choose a reading path

| Task | Start here | What owns the detail |
| --- | --- | --- |
| Install and use the Mac app | [English README](../README.md#quick-start) / [中文 README](../README.zh-CN.md#快速开始) | The READMEs own supported features, setup, ordinary commands and privacy boundaries. |
| Understand source access, archive or backup | [Source reliability](source-reliability.md) / [中文版](source-reliability.zh-CN.md) | Operator procedures and source/attachment/backup failure behavior. |
| Change selected-resource capture or mounted backup | [Resource specification](resource-capture-and-mounted-backup-spec.md) | Occurrence identity, selection, cursors, projections, handoff and coverage semantics. |
| Recover monitor progress safely | [Recovery contract](recovery-acceptance.md#source-behavior), then [frozen batches](recovery-acceptance.md#monitor-acceptance-and-frozen-batches) | Generation admission, checkpoint intent, canonical-event reuse and remaining migration limits. |
| Understand signature, redaction or catch-up result handling | [Reliability closure](reliability-closure.md) | Shared predicates and failure contracts; the original candidate's evidence is separately labelled. |
| Feed a private local archive without AI or Markdown | [Quiet Archive handoff](quiet-archive-handoff.md) | Opt-in text retention, machine schema, source coverage and bounded CLI commands. |
| Contribute or resume development | [CONTRIBUTING](../CONTRIBUTING.md), then [AGENTS](../AGENTS.md) | Contribution/review evidence, canonical source owners and verification commands. |
| Work on Windows | [Development handoff](WINDOWS-DEVELOPMENT.md), then [port map](WINDOWS-PORT-MAP.md) | Phase order and exact-build evidence; the map owns module classification. Foundation support is not a Windows app. |
| Use the offline source-package fallback | [Chinese offline-guide template](share-package-guide.zh-CN.md) | The exact-commit builder generates `群友使用说明.md` from this one source. |

For component relationships, use the [README architecture](../README.md#architecture).
Editable diagram sources are in [architecture/](architecture/); the SVGs in
[assets/architecture/](assets/architecture/) are generated exports. They are
an overview, not an exhaustive CLI or support matrix.

## Development changes that explain the current boundaries

These are source-history landmarks, not a release log or a new implementation
plan. Use `git show <commit>` in a Git checkout to inspect the corresponding
change; use the current guides above to operate the app.

| Source lineage | Why it still matters |
| --- | --- |
| Upstream menu-bar summaries and MCP; attribution in [NOTICE](../NOTICE.md) | The Mac shell remains the process host. Shared domain and durable state live in `core/`; menu visibility does not own background work. |
| Resource capture and mounted backup, `704aa36`; process-lifetime scheduling, `148752e` (August 2026) | Resource capture is independent of an AI Knowledge hit. Mounted folder delivery and remote cloud verification are different evidence. Short-lived scheduled source workers were retired. |
| Inventory, per-shard cursors and privacy hardening, `1c478d4` through `1e24953` (August 2026) | Missing shards cannot become “no messages.” Source, checkpoint and projection have separate owners. MCP mutations and remote link preview were retired. |
| Recovery and reliability integration, `9ab1eea` and `2353931`; stable app/signing identities, `d394deb` and `7a492ec` (September 2026) | Source tests, exact-target re-signing, a local alias bundle and actual runtime acceptance remain distinct. |
| Frozen monitor batches, `72a5d7b`; optional MCP, `c238ee2` (September 2026) | A retry must preserve prepared work. Ordinary app installation no longer includes or manages an MCP server. |
| Portable storage, credentials and source seam, `fc0f301`, `7f0bd9c`, `4dcf07d` (September 2026) | Shared foundations are implemented. Windows WeChat source support still needs exact-build evidence and its own adapter. |

Later commits in a topic branch can extend these foundations without being
merged or released. Documentation in a checkout describes that checkout's
source contract; record `git rev-parse HEAD` before attaching test evidence.
Package version strings, tags and dated canaries do not identify a running app.

## Retained, optional and retired surfaces

| Surface encountered in old material | Present role and reason |
| --- | --- |
| Root `启动.command` | Retained forwarding entrypoint for existing source installs. [launchers/启动.command](../launchers/启动.command) owns startup and the local alias bundle. |
| `wechat-summary` / `mac-wechat-summary` names | Compatibility names in old runtime paths, credential services, generated markers or managed LaunchAgents; see [migration](../README.md#runtime-data-migration). They are not alternative development products. |
| Source-guard or resource `install-agent`, `--source-guard-run`, `--resource-backup-run` | Retired short-lived scheduling. Inspection/removal commands support upgrades; [the long-lived app](source-reliability.md) owns timers. |
| MCP | Explicit opt-in read-only stdio compatibility. The client owns its process. Old send names return `mcp_send_retired`; they do not reactivate sending. |
| Remote link preview and `link_export_mode=full` | Preview is inert; legacy full export migrates to redacted. Exact URL identity stays in private ledgers. |
| Mounted backup and Direct Drive | Intentional separate backends, not old/new replacements. Mounted backup uses a filesystem; Direct Drive has separate OAuth, selection and remote verification. |
| Exact relation Markdown repair | [Retained incident-specific recovery](legacy-relation-repair.md), bound to one verified provenance profile. It is not normal setup or a general vault cleaner. |
| Source-package ZIP | Exact-commit offline/archive fallback. The public Git repository remains the ordinary distribution/update path. |

## Read acceptance history at its original scope

- [Recovery acceptance](recovery-acceptance.md) maintains the recovery contract
  and preserves the dated `2353931` canary. That canary does not cover later
  frozen-batch changes or attest today's installed app.
- [Reliability closure](reliability-closure.md) preserves the original authoring
  environment's limits. Its former candidate wording is not an outstanding
  instruction to implement already-integrated modules again.
- The [resource specification's rollout](resource-capture-and-mounted-backup-spec.md#16-rollout)
  explains implementation sequencing; acceptance criteria do not establish
  that a particular machine or backup target passed them.
- The external `WGO-WIN-SPEC-2` remains owner-review candidate material. The
  [Windows development guide](WINDOWS-DEVELOPMENT.md#与旧-spec-的关系) records
  its retained constraints and the changes made for current source.

Local deployment observations, private incident artifacts and working handoffs
stay outside the public repository. A current operational claim needs a fresh
observation of the relevant installation; this index supplies no deployment,
source-access or migration authorization.
