# Legacy → semantic 首次迁移与恢复

`scripts/rebuild_knowledge.py` 是首次迁移的 operator 入口，实际流程由
`core/semantic/rebuild.py::RebuildCoordinator` 拥有。本文描述当前候选源码；
通过 synthetic tests 不代表已安装、已读取真实来源、已使用账户额度或已激活。
当前支持旧 topic/event 安装迁入**第一个 semantic generation**，以及同一
`run_id` 的恢复和 rollback。已有 active semantic generation 时，新 plan
会明确拒绝；这里没有按标题推测跨 generation reader preferences 的迁移器。

新 generation 的 SQLite 是 canonical owner。原知识库、原笔记、已有
Review Queue、resource ledger 和本地附件均先保全；Markdown 与 index 是
derived consumers。协议细节分别见 [store](semantic-store-contract.md)、
[legacy conservation](semantic-legacy.md)、[provider](semantic-providers.md)、
[reading](semantic-reading.md) 和 [monitor v3](monitor-state-v3.md)。

## 操作前提

使用实际 managed app checkout 的 Python 环境。配置必须已有明确的
`monitor_knowledge_db`、`monitor_obsidian_root`、旧 managed subdirectory 和
当前选中群聊。新阅读根固定为该 vault 下的 `WGO/`。
`semantic_runtime_root` 未设置时使用应用 DATA_DIR 下的 `semantic/`；
传入 `--runtime-root` 时必须与既有配置一致。配置只能通过 ConfigStore
或应用已有设置界面修改，不能在执行中替换配置快照。
显式 root 必须存在且可用；外部卷未挂载时 app、monitor、health 与旧命令都
失败关闭为 `semantic_runtime_root_unavailable`，不会自动创建替代目录或回落到旧版。
初次生成 legacy inventory 时，单文件 metadata 在读取期间变化可重新读取一次；
每次都重新验证路径、fd/live identity、大小与完整稳定读取。持续变化仍停止。
已经冻结的 inventory、备份和验收校验保持严格，不采用新的变化替换封存证据。

managed LaunchAgent 必须唯一、精确绑定实际 app bundle executable、plist
及 working directory。旧 `/bin/bash 启动.command` wrapper 不能证明 app
child 的身份，执行会报 `runtime_app_bundle_binding_required`。先按安装流程
准备并验证稳定 app bundle，再在独立授权的安装操作中配置：

```bash
.venv/bin/python scripts/autostart.py install --app-bundle /ABSOLUTE/PATH/WeGroupchatObsidian.app
```

该命令会改变 LaunchAgent，不是只读检查。需要即时加载时，既有安装入口
另有 `--load-now`；rebuild 不猜测或安装另一个 runtime。若从独立工作树运行
rebuild，使用全局参数 `--project-dir /ABSOLUTE/PATH/MANAGED-CHECKOUT` 精确
指定既有绑定。执行和恢复会检查 launchctl PID、启动时间、executable 以及
同 PID 的 AppInstanceLock owner；只看到 plist 已加载不能完成 live 验收。

所有运行 manifest、journal、backup 和 generation DB 都是私有材料，可能
包含完整原文、账号、群标识、精确 URL、本地路径及个人笔记。不要提交到 Git、
贴入 issue 或当作公共诊断输出。CLI 默认只输出状态、数量和 content-free codes。

`plan` 和 `status` 的 `configured_admission` 显示该冻结计划的并行上限及
RPM/TPM 配置；`unset` 明确列出未设置的速率。它只读取计划配置，不构造
provider，也不代表已探明服务商限额或当前运行中的请求数。实际进程的
admission 观察来自所用 `ProviderRoles.preflight()`；已冻结计划不能靠后来
修改配置来悄悄改换执行参数。

## Provider profile 与预算

在应用私有目录保存 profile JSON。此例中的占位符必须替换为操作员已确认
的账户及 Keychain secret 名称；文件只存 secret reference，不存 token：

```json
{
  "schema": "we-groupchat-obsidian.semantic-providers.v1",
  "decision": "text",
  "embedding": "cloudflare",
  "vector_index": "local",
  "account_id": "CLOUDFLARE_ACCOUNT_ID",
  "vectorize_index": "",
  "secret_name": "EXISTING_KEYCHAIN_SECRET_NAME"
}
```

Text 使用当前配置的 monitor AI provider；`decision: text` 是显式的 typed
text route，`decision: cloudflare` 使用 Clef。`embedding: cloudflare` 使用
BGE-M3。上述 profile 的 `vector_index: local` 只使向量检索留在本地；
embedding 仍有远端调用。
使用 `vector_index: cloudflare` 时同时提供已准备好的专用 `vectorize_index`。
不得把未知远端 mutation 当作已确认的 index。

也可显式选择已有 loopback OpenAI-compatible embedding 服务。以下 model、
dimensions、context_window_tokens 与端口必须对应操作员已准备并确认容量的服务；
1024 与 8192 仅为配置示例，CLI 不探测容量、下载或启动模型：

```json
{
  "schema": "we-groupchat-obsidian.semantic-providers.v1",
  "decision": "text",
  "embedding": "local",
  "vector_index": "local",
  "local_embedding": {
    "api": "openai",
    "endpoint": "http://127.0.0.1:8080/v1/embeddings",
    "model": "LOCAL_MODEL_NAME",
    "dimensions": 1024,
    "context_window_tokens": 8192
  }
}
```

Ollama native 服务应显式使用 `api: ollama` 与 `/api/embed` endpoint；该路径
发送 `truncate: false`。OpenAI-compatible 路径不发送此字段，因此必须核对实际
服务的输入容量。地址仅允许受约束的 loopback endpoint，不将任意内网服务称作本地。三种角色均
未选择 Cloudflare 时，不要求 Cloudflare account、Keychain reference 或 budget
ledger；Text 仍按当前 monitor provider 配置执行。若其中任一角色使用 Cloudflare，
需提供该账户字段及相应 AI/Vectorize budget evidence。local embedding 不是
Cloudflare 失败后的隐式 fallback。

Loopback endpoint 也可以是操作员维护的 SSH tunnel，连接已明确授权的自托管
embedding 服务。此时文本会离开 Mac 到该主机，`embedding: local` 不证明同机计算；
本地索引仍留在 Mac。两端只绑定 loopback，先用合成文本核对维度、pooling、query
instruction、容量拒绝行为和实际 adapter，再冻结 profile。WGO 不负责安装服务或
tunnel；自托管 embedding 也不能代替独立 Text 套餐的额度确认。

Cloudflare 预算属于应用级 `DATA_DIR/provider-usage.sqlite`，跨 generation
共用。外部账户用量必须先由操作员实际核对，缺失不能填作零。
现有 Text vendor 保留自己的配置与限额，不套用 Cloudflare 免费额度。
当前预算确认入口是 Python API。以下命令不读 WeChat、Keychain 或模型；
它会明确写入本地 budget evidence。依次传入私有 profile、`ai` 或
`vectorize`、允许的日总量、已核对的外部用量，以及本次证据标识：

```bash
.venv/bin/python - /PRIVATE/provider-profile.json ai DAILY_LIMIT EXTERNAL_USAGE EVIDENCE_ID <<'PY'
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from core.config import DATA_DIR
from core.provider_usage import ProviderUsageStore

profile_path, service, limit, external, evidence_id = sys.argv[1:]
if service not in {"ai", "vectorize"}:
    raise SystemExit("service must be ai or vectorize")
profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
account = hashlib.sha256(profile["account_id"].lower().encode("ascii")).hexdigest()
account_key = ("cf-ai:" if service == "ai" else "cf-vectorize:") + account
now = datetime.now(timezone.utc)
end_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
usage = ProviderUsageStore(str(Path(DATA_DIR) / "provider-usage.sqlite"))
usage.confirm_budget(account_key, utc_day=now.date().isoformat(),
    daily_limit_units=int(limit), external_usage_units=int(external),
    confirmation_id=evidence_id,
    valid_until=min(now + timedelta(minutes=30), end_of_day).timestamp())
print(json.dumps(usage.budget_status(account_key), sort_keys=True))
PY
```

Workers AI 单位为 integer micro-neurons；Vectorize 使用独立确认的保守
dimension-operation units。后者不是完整月账单或长期 storage spending cap。
只确认实际 Cloudflare roles 对应的 service：decision 或 embedding 使用
Cloudflare 时确认 `ai`；vector index 使用 Cloudflare 时确认 `vectorize`。
local embedding + Text decision + Vectorize 仅需后者。上述 30 分钟只是这次证据有效期，
不是自动续期；跨 UTC 日必须重新核对。reservation、unknown 请求和既有超界
状态不会被重新确认抹掉。实际 pricing/account 边界见 [provider contract](semantic-providers.md)。

## Plan、execute 与 status

所有全局参数放在子命令前。省略 `--config` 时使用应用默认配置文件：

```bash
.venv/bin/python scripts/rebuild_knowledge.py --config /PRIVATE/config.json \
  plan --provider-profile /PRIVATE/provider-profile.json --run-id FIRST_MIGRATION
.venv/bin/python scripts/rebuild_knowledge.py --config /PRIVATE/config.json \
  status FIRST_MIGRATION
```

`plan` 使用 ConfigStore.inspect，只读旧 SQLite、已存 checkpoint、明确的
managed note roots、Review Queue 和 resource/archive 路径，写入私有 run
journal。它不构造 message source 或 provider、不读取 key cache、不发现或
stat WeChat source，也不停止 app。私有路径与 `db_dir` 有包含关系、symlink、
root escape、非 regular 文件或既有 cutover 不一致都会失败关闭。

Plan 包含当前选中群聊范围、全部旧 canonical records、旧页面/个人内容、
Review Queue append history、已有 prior selections、storage 估计和当时预算
状态。prior selections 只保留为历史，不重新订阅旧群。尚未观察 source 的
message 数量为 `null`；plan counts 不是 H1，也不是费用报价。新原文的实际
体积未知，执行仍须通过 disk 和逐次 provider reservation gates。

执行必须同时给出两项本次授权，检查发生在 config/key/source 访问之前：

```bash
.venv/bin/python scripts/rebuild_knowledge.py --config /PRIVATE/config.json \
  execute FIRST_MIGRATION \
  --allow-transient-wechat-source-read --allow-model-processing
```

前一 flag 只授权本次短命 CLI 读取来源，macOS 仍可能为该进程显示独立 App
Data consent。后一 flag 授权将此次 captured/legacy 文本经统一 URL 脱敏后
交给已配置的 Text、Decision 和 Embedding providers。没有隐式 attachment-byte
consent、link crawler 或换模型 fallback。该操作会停止并恢复 exact managed
app、生成新 DB、发布 `WGO/` 并 CAS 更新 monitor checkpoint。

执行首先检查配置、精确 runtime binding、可用空间、candidate 路径和现有预算，
随后才停止 LaunchAgent。AppInstanceLock、resource capture-operation lock、
resource DB backup lock、real-path output lock、checkpoint execution locks 和
新阅读根的 projection lock 覆盖冻结与切换。所有锁释放后才恢复 LaunchAgent。

## Journal 存储升级与内存边界

Run journal v2 只保存小型状态与不可变 payload references。旧 inventory、冻结
receipt、source capture receipt 和 verification manifest 保存在同一私有 run
目录的 `payloads/`，以内容 digest、byte length 和 run ID 绑定。状态推进不再
解析、复制或重新序列化全部旧正文。同一 journal reader 对已验证且文件 identity
未改变的 payload 复用验证；首次读取与变化后的读取仍核对完整 bytes，实际消费
正文时再 decode。此优化不降低冻结、来源完整性或 canonical conservation 验证。
Legacy inventory 的身份校验也按 canonical JSON chunks 计算，保持既有 digest；
单次 backup/navigation 操作复用其已算身份，不在每个旧页面上重算全库。
本地 cosine 的向量保存在私有临时 SQLite 磁盘缓存；索引准备只保留 metadata，
发布／未索引尾部检索分批读取至多 100 个向量，查询只保留 top-k。
Canonical embedding 与确认 receipt 仍是恢复依据；普通关闭清理临时缓存，
强制终止可能留下私有临时目录。需为缓存预留磁盘，不能把减少 RAM 理解为零磁盘成本。

旧内联 journal 必须在 run lock 内逐块转换。可先独立执行以下操作；它只升级
本地 journal 存储，不读微信、不调用 provider、不停止或激活 app：

```bash
.venv/bin/python scripts/rebuild_knowledge.py --config /PRIVATE/config.json \
  compact-journal FIRST_MIGRATION
```

`execute` / `resume` 在同一个锁内也会自动转换。只读 `status` / `verify` 不暗中
升级旧文件，返回 `rebuild_journal_upgrade_required` 后使用上述命令。转换验证旧
plan fingerprint，保留 run/generation/阶段/错误和全部证据；原 journal 原字节
保存在 `inline-v1.json`，新 payload durable 后才原子替换 `run.json`。中断可沿同一
run 重试，不能删除原件或手改 phase。旧 writer 生成的 canonical key order 必须
保持；乱序、损坏、错 run、坏 hash、缺文件及 symlink 均失败关闭。旧版本程序
不能读取 v2 journal，应使用包含该修复的 coordinator 恢复。

转换按 64 KiB 读取，单个 JSON scalar 上限为 64 MiB，mutable metadata 上限为
16 MiB；越限保留旧 journal 并明确停止。完整 inventory 的实际导入和独立 verifier
仍可能按其数据量占用内存，这里不宣称整个重建具有固定 RAM 上限。
收到正常 Ctrl-C / SIGINT 后，coordinator 记录中断并释放锁；切换状态允许安全
恢复时再恢复精确 app，不能把中断当完成。强制终止仍按 durable journal 恢复。

## 激活前迁移到另一个本地 runtime root

已 plan、尚未 activation 的重建若计划过期或需要换到另一个已挂载的本地卷，可用
同一 `run_id` 的显式 recovery 把该 run/generation 复制到新的私有本地 root 后继续。
先看 plan，再显式 apply（apply 必须同时给出两个 flag）：

```bash
.venv/bin/python scripts/rebuild_knowledge.py --config /PRIVATE/config.json \
  --runtime-root /EXISTING/OLD/runtime relocate FIRST_MIGRATION \
  --destination-root /EXISTING/NEW/runtime
.venv/bin/python scripts/rebuild_knowledge.py --config /PRIVATE/config.json \
  --runtime-root /EXISTING/OLD/runtime relocate FIRST_MIGRATION \
  --destination-root /EXISTING/NEW/runtime --apply --allow-runtime-changes
```

`relocate` 只处理 activation 之前的中断 run：已有 active pin、进行中的 cutover、
`run_complete` 或已绑定到另一个 destination 都失败关闭。目标 root 必须由操作员
在确认卷已挂载后事先创建为空目录；缺失、symlink、非目录、非空且不属于该 run 的
目标，以及任何与 source/vault/legacy/state 路径重叠的位置都会被拒绝。`relocate`
绝不代替创建 mount 目录，避免在 boot volume 上生成错误的替代卷树。准备阶段只读，
不读 WeChat 来源、不构造 provider、不停止 app。

apply 先停止并精确绑定 managed app（AppInstanceLock）与既有 rebuild/capture/state
owners，再用 SQLite Online Backup（含 WAL）复制该 run 的 journal/payloads/backups/
staging 与 generation DB，逐文件校验 digest。inventory、verification 等不可变
大 payload 只按引用复制，不展开或重写。中断的复制可对同一 destination 重试：重试
重新校验并补齐，绝不删除、改写或替换旧 root。复制、destination journal 绑定与校验
全部成功并写入 relocation 与 retirement evidence 后，才用 latest-locked
`ConfigStore` patch 写入新的 `semantic_runtime_root` 并 finalize relocation receipt；
所有锁释放后才恢复精确 app，返回值单独记录 `restored`。

同一 run/generation、原始 plan fingerprint、raw captures、jobs/artifacts、
checkpoints 与旧 journal provenance 全部保留；不新建会丢弃进度的 plan、不 reset
cursor、不伪造完成 receipt、也不自动重放 provider。operational plan 只重绑运行位置
（`database`/`staging_root`）、config revision/fingerprint、预算与限额快照，以及新的
`expires_at`（这是显式 recovery，不是手改过期时间）；原 plan 存为 `original_plan`
payload 供历史恢复。旧 root 保留，另有 `runs/<run_id>/relocation-retired.json`
记录 destination、receipt 与 `reclaim: operator_review_required`，最终空间回收
留给 operator/coordinator review。

Provider 绑定同时来自 profile 与 live config。generation 已有任何 jobs（含 accepted/
unknown）时，profile 或 live config 的 TEXT/DECISION route（provider/model/base URL/
thinking/timeout）变化一律失败关闭；只有零 job 的 generation 才能用
`--provider-profile` 刷新，或采用已变更的 config endpoint/model。无变化则保留既有
jobs。config 只经 `ConfigStore.update` 以最新 revision patch，只改
`semantic_runtime_root`，无关的并发 config 更新会被保留。完成迁移后用新的
`--runtime-root /EXISTING/NEW/runtime` 正常 resume：

```bash
.venv/bin/python scripts/rebuild_knowledge.py --config /PRIVATE/config.json \
  --runtime-root /EXISTING/NEW/runtime resume FIRST_MIGRATION \
  --allow-transient-wechat-source-read --allow-model-processing
```

relocation receipt、retirement record 与复制的 payload 仍是私有材料，可能包含本地
路径；CLI 默认只输出状态与数量。若 adoption 被中断，同一 run/同一 destination
再跑一次 `relocate --apply --allow-runtime-changes` 完成 finalize；此时 plain
`execute`/`resume` 会失败关闭为 `rebuild_relocation_recovery_required`。

## 冻结、完整来源与 publication

每次真正重新冻结都会生成新的 `freeze-N`：fresh 读取旧 DB/state/config 与
完整笔记；SQLite Online Backup 保留 WAL commits；config、checkpoint、全部
managed bytes、Review Queue 文件、resource ledger 和本地 archive bytes 各有
exact recovery copy。数据库和原笔记不会被重写为新 schema。未知 baseline 的
整页一并保留，不猜测哪一段属于用户。

执行顺序固定为：完整 pinned C0 traversal、legacy import、全部 extraction、
全局真实 embedding/index、resolver、consolidation、Views；然后从零进行完整
C1 traversal 并处理其新版本。C1 捕获旧时间戳晚到行及 A→B→A，不以最后时间戳
代替全库证明。最后生成本日 Brief，保留来源时间与获知时间，避免补写历史日期。
accepted jobs 和 artifacts 可复用，unknown provider outcome 不会自动重发。

旧 `pending_source_batch` 在任何 provider 工作前以原 policy、raw limit、精确
成员、内容和完整 inventory 重新验证。失败保留 intent；不能清空 pending 或
挪动 cursor 来让迁移通过。最后一轮 full pass 独立证明每个 selected chat 的
H1。migration receipt 只引用该轮的连续批次，C0/C1 不混成一条 checkpoint proof。
捕获途中 crash 的合法 raw prefix 明确记录为 abandoned/incomplete pass；仍需
逐成员处理并保全，随后新的完整 pass 才能提供 H1。

独立 verifier 从 canonical SQLite snapshot 和 frozen manifest 重建证据：每个
member identity、terminal disposition、Observation/span、accepted action、
transaction/outbox、embedding/index confirmation、当前 Item/来源/Views
projection，以及旧 rows/pages/notes/links/anchors 和映射均须有去向。Brief
必须有本日 inputs、accepted plan/disposition 和引用；success 还须有 history
及当前磁盘正文。明确 `silent` 可以通过，未生成、deferred、unresolved review
或丢失正文不能通过。报告区分 source、semantic、index、projection pending；
数量相等不证明资料相同。

先在私有 staging/WGO 达到完整验收，再以冻结 file hashes CAS 发布到固定
vault/WGO。已知机器区由 canonical publisher 管理；未知文件、个人编辑、重名
identity、symlink 和越界路径不会被覆盖。旧页面仍留原址，另建 redacted
`Legacy/` history/redirect/hub navigation。旧 Review Queue 每次 append 的状态
都进入同一 canonical DB 的 legacy evidence，并有可读历史；不会翻译成新
reader controls 的一次虚构操作。

## Cutover、resume 与 live completion

外置 cutover journal 按顺序保存 intent、acceptance、projections、checkpoints、
active_manifest、consistency、activation_committed 和 provisional。active manifest
一次 pin DB、embedding profile、index identity、projection root、config revision
与 receipts。半切换启动失败关闭；只有 activation_committed 以后新 runtime
才可读取这组绑定。activation metadata 记录 H1、时间和 delivery floor，历史
change events 不会补发通知。

```bash
.venv/bin/python scripts/rebuild_knowledge.py --config /PRIVATE/config.json \
  verify FIRST_MIGRATION
.venv/bin/python scripts/rebuild_knowledge.py --config /PRIVATE/config.json \
  resume FIRST_MIGRATION \
  --allow-transient-wechat-source-read --allow-model-processing
```

`status` 和 `verify` 都 source-free，也不修复 schema、调用 provider 或构造 source。
`verify` 检查保存的验收范围；激活后已有更多 live captures 时，旧 frozen manifest
与当前 DB 不再相同，不能把它当作实时健康矩阵。此时使用应用 health；原验收
receipt 保留其 H1 范围。

切换前旧 runtime 已恢复的 retry 会重新停止、fresh backup，并迁入间隔期间
新增的旧 canonical records、完整用户笔记和 Queue history，重新 C0/C1。
原备份和先前 raw versions 保留；不会只换一次 state CAS。配置或 runtime binding
改变必须先处理对应冲突，不能将旧 plan 静默套用到新范围。
fresh freeze 使旧 accepted proposal 的 read set 失效时，会建立新的 resolver
计划。只有同一 batch/Observation 输入的替代计划已提交，并有完整来源和实际
resolution receipts，旧 proposal/decision 才能以 explicit supersession proof
终结；原 artifact 保留。缺少替代、部分 resolution 或 unknown outcome 仍阻断。

同一 run 已 activation_committed 的 `resume` 只执行待完成的 provisional、
observed restore 与 live 验收，不再读取 source 或调用 model。CLI 仍要求同一组
显式 flags，以免操作者在阶段未确定时隐式授权一条更广的恢复路径。

`run_complete` 同时要求 durable provisional、所有锁已释放、实际 restore 被
观察、exact 新进程身份，以及每个原 selected chat 在 restore 后由真实新
runtime 写入的 `phase=applied`/processing receipt/checkpoint revision。
只有 `captured`、旧进程或生产者 success counter 都不够。当前没有新 applied
证据时返回 `rebuild_live_increment_pending`；让 managed app 完成一次正常增量
后再以同一 run resume。restore failed 不得 complete；provisional receipt
写失败对整个 run 是 sticky，即使后来补写成功也不能抹去该失效记录。

## Rollback 与故障处理

先看 rollback plan；只有显式 apply 才改变 runtime 与 checkpoint：

```bash
.venv/bin/python scripts/rebuild_knowledge.py --config /PRIVATE/config.json \
  rollback FIRST_MIGRATION
.venv/bin/python scripts/rebuild_knowledge.py --config /PRIVATE/config.json \
  rollback FIRST_MIGRATION --apply --allow-runtime-changes
```

Rollback 冻结 exact owner，在任何 downgrade 前持久保存 replay manifest，保留
完整 candidate generation、H1 以后 raw/capture receipts、reader/delivery state
和 current/backup checkpoint。只由 MonitorStateStore.restore_checkpoint 执行
revision CAS；原 pending intent 也随严格验证的旧 checkpoint 恢复。它不会删除
generation，亦不执行 source/model replay，所以此命令不需 source/model flags。
replay plan 保存明确的 retained DB、capture receipt 集合、from/through cursors
及每次 CAS proof；保留的语义/阅读内容仍可审查。旧 runtime 恢复后的正常扫描
遵循恢复的旧 checkpoint。这里不宣称自动把新 semantic reader state 转译回
旧 topic schema。

外部 cutover 的 `rollback_intent` 优先于旧 `run_complete` receipt。checkpoint
CAS 后中断时，`status` 显示 incomplete/rollback_intent，execute/resume 返回
`rebuild_rollback_recovery_required`；以同一 run 再执行显式 rollback apply
完成回滚和 exact runtime restore，不重新激活新 generation，也不复活旧完成状态。

Rollback 只在 current bytes 仍等于已发布 candidate 时恢复原 WGO bytes；用户
后来编辑的文件会阻断该恢复并保全。本次新增的 generated files 保留，不做
无条件删除。rollback_intent 未完成时启动失败关闭，继续同一个 rollback apply；
不能用 `resume` 重新激活一半撤回的状态。pointer/checkpoint 撤回完成后若 restore
失败，仍报 incomplete；再次 rollback apply 只补 exact restore，不重复 downgrade。

| 观察到的状态 | 下一步 |
| --- | --- |
| budget unconfirmed/expired、额度或 unknown reservation 阻断 | 核对账户和原 request evidence；不盲目创建新 request 绕过账本。 |
| source inventory/pending proof 不一致 | 保留旧 checkpoint/intent，按既有 source recovery 契约处理；不能 reset。 |
| semantic_database_busy / semantic_storage_full / semantic_database_io_error | 分别检查竞争连接、可用磁盘或存储 I/O；它们不代表数据库已损坏。保留同一 run 和已提交 captures，排除原因后恢复。 |
| semantic_database_invalid / semantic_database_unavailable | 前者是 SQLite 明确的 corrupt/not-a-database 分类，后者表示尚未分类的连接失败；先对精确 generation 做只读完整性检查，不能删除数据库重建来掩盖失败。旧版本将所有连接初始化错误都标为 invalid，历史记录本身不能证明损坏。 |
| semantic pending、unclassified 或 Brief review/deferred | 检查私有 accepted job/attempt 证据，在同一 run 恢复；不得改成 noise/silent。 |
| index confirmation pending | 保留 mutation receipt，下一次恢复确认精确 mutation；不重复 upsert。 |
| projection conflict 或个人编辑 | 使用 reader owner 的明确冲突处置；原 bytes、backup 和 draft 都保留。 |
| cutover recovery required | 保持 app 停止；同一 run resume 或显式 rollback，不手改 active.json。 |
| restore failed / live increment pending | 同一 run 复查 exact binding 与真实 app；不能仅修改 receipt 声称完成。 |
| relocation 中断、plan 过期或已换 root | 用同一 run 与同一 destination 再跑显式 `relocate --apply --allow-runtime-changes` 完成复制/adoption/finalize；不新建 run、不手改 plan 或 expires_at、不在 config 未 adopt 前继续。 |
| `rebuild_relocation_*` 拒绝（active/cutover/路径/目标非空/provider 变化） | 按 code 处理：先完成或 rollback 现有 cutover；在已挂载卷上创建空目标目录；jobs 存在时恢复原 provider route/model，或另走新 generation 而不是就地改绑。 |

不要删除状态目录、修改成功计数、清空 pending、换一个 run id 隐藏未解决
cutover，或以“文件已写出”代替 model/index/source proof。空 journal、FIFO、
corrupt JSON、symlink 及未知 schema 均失败关闭。

## Source verification

```bash
.venv/bin/python -m unittest tests.test_semantic_rebuild tests.test_rebuild_knowledge \
  tests.test_semantic_legacy tests.test_semantic_verification
```

这些 tests 只使用 temporary legacy SQLite、notes、source 与 provider fakes，
通过真实 capture/store/engine/retrieval/publisher/checkpoint API 验证结果。
测试路径与 DATA_DIR 绑定到临时目录并拒绝越界读取。fixture 中的 restore/live
proof 不构成 native LaunchAgent、macOS consent、真实账号/index、安装或部署验收。
真实操作必须由操作者另行执行，并保留其 exact run 与 live evidence。
