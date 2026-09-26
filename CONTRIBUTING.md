# Contributing / 参与贡献

Thank you for helping improve WGO. This repository handles local chat-derived
data, credentials, durable state, and platform-specific filesystem behavior, so
every contribution must keep its scope and evidence explicit.

感谢你参与 WGO。这个项目会处理本地 chat-derived data、凭据、持久状态和平台专属
filesystem 行为，因此每项贡献都需要明确 scope、验证证据与隐私边界。

## Before starting / 开工前

1. Choose a maintainer-marked ready issue, or open a bounded proposal with the
   relevant issue template. One issue has one accountable human owner; agents
   may assist but do not replace that ownership.
2. Record the full base commit with `git rev-parse HEAD`. Use a short-lived topic
   branch in a fork unless a maintainer has granted another workflow.
3. Use the [documentation and history index](docs/README.md) to locate the
   current contract, then read `AGENTS.md` and the affected guide. For
   Windows work, begin with [`docs/WINDOWS-DEVELOPMENT.md`](docs/WINDOWS-DEVELOPMENT.md)
   and [`docs/WINDOWS-PORT-MAP.md`](docs/WINDOWS-PORT-MAP.md).
4. Confirm the real caller, allowed paths, invariants, verification, and stop
   conditions before changing code. A new adapter or class is not complete
   until the task's actual consumer and failure path are covered.

## Pull requests / Pull Request

- Open a Draft PR early and link the accepted issue. One PR should deliver one
  independently testable behavior change, including required callers, error
  handling, tests, and documentation.
- Keep existing state ownership and privacy boundaries. Do not introduce a
  parallel config, cursor, inventory, credential, or migration authority for
  convenience.
- Report the exact tested commit, environment, commands, results, and anything
  not run. CI success, source merge, native verification, an installable
  candidate, and a supported release are separate facts.
- Review applies to an exact head commit. New relevant commits require the
  affected review and evidence to be refreshed. Unanswered review, quota
  failure, or review of an older head is not approval.
- Scope expansion involving schema/state identity, permissions, source access,
  credentials, external services, or production/runtime behavior must return
  to the linked issue for a maintainer decision.

## Private data and real machines / 私人数据与真机

Hosted CI and public fixtures must be synthetic. Never upload real WeChat
DB/WAL files, keys, account or chat identifiers, messages, attachments, local
absolute paths, credentials, screenshots containing private content, raw logs,
or full agent transcripts to an issue, PR, CI artifact, or repository file.

真实 Windows/macOS source 验收必须绑定已审阅的 commit，并由机器 owner 在执行前明确
同意范围。登录着微信的日常电脑不能作为自动执行公共 PR 的 self-hosted runner。公开
evidence 只保留 content-free 的版本、环境、profile、测试名、状态、有限错误码和未覆盖项。

## Verification and completion / 验证与完成

Use focused tests while iterating, then run the risk-matched repository checks
required by `AGENTS.md`. Preserve old data and retry state when a migration or
recovery path is involved. In the PR, distinguish:

- `source merged`: implementation, actual consumer, failure path, tests, and
  documentation are merged;
- `native verified`: the stated commit ran on the stated real environment;
- `release candidate verified`: the built candidate passed its install and
  lifecycle acceptance;
- `released/supported`: maintainers deliberately published a bounded support
  claim.

Maintainers make the final merge, release, ruleset, and support decisions.
