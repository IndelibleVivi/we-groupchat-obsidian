# 来源可靠性：source guard、本地 CAS、mounted backup、可选 Drive API 与 filesystem snapshot

这一层故意拆成五项互不冒充的责任：

1. 可选 WeChat source guard：只负责在安全状态下请求 macOS 正常打开微信；
2. Attachment catalog 与本机私有 content-addressed archive；
3. 默认 no-OAuth selected-resource mounted handoff；
4. 可选 advanced selected-chat Drive API sync；
5. 面向更广 attachment archive 的 provider-neutral filesystem snapshot target。

它们仍是彼此独立的 domain responsibility，只是需要 protected-data access 的 timer 共享一个长驻菜单 app
进程。`TopicMonitor` 负责读消息与 checkpoint，不 import、也不调用 source guard。Knowledge transaction 负责登记 attachment mention；
commit 之后的 worker 才负责找 bytes 和复制。Mounted backup 只读本机 immutable archive object，
并且只写自己的 configured filesystem target。两条 selected-chat scanner 各有独立 cursor/selection；
即使消息没有 Knowledge hit，也可以复用同一个本地 CAS。

## 1. 可选 WeChat source guard

Source guard 默认关闭。它的 timer 位于长驻菜单 app 内；开启 policy 就是唯一 activation：

```bash
.venv/bin/python scripts/wechat_source_guard.py status
.venv/bin/python scripts/wechat_source_guard.py enable
.venv/bin/python scripts/wechat_source_guard.py uninstall-agent  # 清理旧 agent
```

旧版本会调度独立的 one-shot `StartInterval` job。macOS App Data consent 绑定正在运行的 process；helper
退出后，下一次 wake 可能再次弹窗。新版本因此拒绝 `install-agent`；历史
`--source-guard-run` 会在读取 protected data 之前返回 `long_lived_app_required`，而
`uninstall-agent` 保持幂等清理。普通 autostart LaunchAgent 只负责一只长驻菜单 app；source guard 在其中
使用 timer 与 non-blocking lock。

Process-list availability 通过读取 guard 当前进程自身来证明，不依赖普通用户 session
能否看见系统级 `launchd`。Source freshness 只对 cached key inventory 中已知的
`message/` shard 路径及其 WAL/SHM siblings 做 exact stat；它不会递归遍历 DB root 或
微信 hardlink/cache tree，因此每轮 in-process check 仍然有界。

只有 process lookup 明确确认微信不在运行时，guard 才会先进入 grace。Grace 结束后，
还必须同时满足 restart budget 与 exponential backoff，唯一可能发出的启动请求等价于：

```text
open -g -a WeChat
```

Guard 不会 kill 微信，不修改或 re-sign app，不提取 key，不操作 UI，不负责登录，不抢 focus，
也不推进 monitor checkpoint。如果当前进程看不到 macOS process list，状态是
`process_lookup_unknown`，guard fail-closed，不会启动任何东西。微信仍在运行但本地数据库 stale 时，
每个 stale episode 只通知一次；一次 fresh observation 会结束该 episode。Stale 不是 restart 授权。

有效状态如下：

| 状态 | 含义 |
| --- | --- |
| `disabled` | Policy 关闭，不可能请求启动。 |
| `healthy` | 已明确确认微信在运行；source freshness 单独报告。 |
| `missing_grace` | 已确认进程不存在，但 grace 还没有结束。 |
| `restart_backoff` | 已请求一次正常启动，或正在等待下一次 backoff。 |
| `paused` | 用户设置的定时/无限 pause 阻止启动。 |
| `degraded` | Restart budget 耗尽，或 launch command 多次失败。 |
| `process_lookup_unknown` | 无法读取 process list；不能把 unknown 当 absent。 |

Pause、resume、单次 check、disable 与卸载都要显式执行：

```bash
.venv/bin/python scripts/wechat_source_guard.py pause --hours 8
.venv/bin/python scripts/wechat_source_guard.py pause --indefinite
.venv/bin/python scripts/wechat_source_guard.py resume
.venv/bin/python scripts/wechat_source_guard.py check
.venv/bin/python scripts/wechat_source_guard.py disable
.venv/bin/python scripts/wechat_source_guard.py uninstall-agent
```

State 与 receipts 位于项目 runtime data directory：目录权限 `0700`，文件权限 `0600`。
Receipt 只含状态跳转、budget/backoff 数值与 error code，不含群名、username、消息内容或附件路径。
Degraded/launch 通知仍按 key 去重并有 cooldown；stale-source 通知按 episode 去重。

## 2. 稳定 message/resource identity

每次读取消息时，代码先 introspect 实际 WeChat table schema，再构建 query。实际存在时，
source envelope 会保留 `local_id`、`server_id`、`sort_seq`、SQLite `rowid`、shard identity、
`create_time` 与 `local_type`；缺失的 optional column 显式保持 null。稳定的
`source_message_id` 由这些值和 chat username 的 hash 派生，不暴露 username 或 `wxid`。

显式配置的 `db_dir` 即使 mount/container 暂时不可用也仍是 authority；auto-detect 只能填入锁内复核后
仍为空的 canonical 值。Cursor shard identity 绑定 source namespace、key fingerprint 与 physical
database-generation evidence（file identity 加 encrypted-page salt/header prefix），所以同一 relative path
上的 DB 被替换或 rekey 后会得到新 generation。Durable `source_message_id` 则使用稳定的 source
namespace 与 logical cache basename；经过明确 reconciliation 后，同一逻辑 row 不会仅因 DB/cache 重建
而制造第二个 identity。Envelope 另带 physical generation，专门用于 cursor admission。只有 decrypted
cache、没有对应 live source 时仍明确报告 `source_cache_only`，不能生成 applicable backfill plan。

### Re-sign 与 scanner identity

显式重签路径只接受一个 canonical WeChat bundle。该 bundle 正在运行时，PID、launch time、executable
path/inode 与 version/build 会贯穿 privilege acquisition、正常退出、签名、独立 `codesign` 验证及
exact-path reopen。它不按进程名退出，也不会在 mutation 后重新发现“默认微信”。多个运行副本必须用
`--wechat-app=...` 显式选择；取消、identity 变化、退出超时、签名失败、hardened runtime 验证失败或
reopen mismatch 都会停止。

Read-only C scanner 只产生 candidate。Scanner build receipt 绑定 exact source digest/size、compiler
path/binary digest、compiler/target identity、显式 `arm64|x86_64` target、flags 与产物 digest/size。
Source、binary、receipt 先在 private immutable build directory 内完整落盘，再 fsync 并 atomic 发布一个
current pointer。Receipt/pointer/binary 被改，或 source/compiler input 变化，都会要求重建；compile 或
pointer publish 失败只保留旧 pointer，不把它冒充当前 input 的合格 build。

### Authoritative shard inventory

`core/source_inventory.py` 持久保存 expected shard union：当前 message DB 文件、key inventory
中的路径，以及此前见过且未显式 retired 的全部 logical shard。Logical identity 由 opaque source
namespace 加 normalized relative path 构成；文件替换或 key rotation 只改变独立的 generation ID，
不会把原 logical shard 从历史里抹掉。每次 reconcile 都发布 path-free 的
`inventory_revision`、`inventory_digest`、completeness、state counts、content-free error codes，
以及当前可读的 generation IDs。

`missing_file`、`key_missing`、`cache_only`、`unreadable` 都会让 inventory incomplete，绝不能被
解释成“source 为空”。Topic Monitor 与 catch-up 在 inventory incomplete 时拒绝推进；selected-resource
与 Direct Drive scanner 可以继续消费明确列出的 present generation，但整轮仍报告
`source_degraded`。缺失 shard 回来后从自己的 cursor 继续，occurrence dedup 会阻止重复写入。

### Monitor raw-row cursor authority

`core/monitor_source.py` 把 complete inventory 变成一批 bounded monitor work。Durable
`source_cursors` 以 logical shard 为 key，绑定当前 generation ID 与 opaque
`(create_time, rowid)` token 和 initial boundary。第一次 enable 会把当时存在的全部 shard 从 now
绑定；完全没有 per-shard cursor 的 legacy state 可做一次 compatibility migration。一旦已有任一
shard binding，replacement generation 或新 logical shard 会在 page read 前返回
`source_generation_admission_required`。Content-free plan 绑定 exact inventory digest/revision、旧/新
generation ID、prior cursor 与 bounded row limit；它只给后续显式 continuity proof 或 bounded
replay/reconciliation 提供 exact input，不授权复制旧 cursor 或借用 global timestamp。

Reader 为每个 present shard 保留 bounded page，再按 `create_time` 与稳定 `source_message_id`
做 k-way merge，并在 configured raw-row budget 停止。可提交 token 只从最后一条真正消费的 row
派生，不能拿 fetched page end 冒充。被 presentation cleaning 过滤的 row 同样消耗 budget、推进
自己的 shard cursor，但绝不进入 AI prompt。只有 filtered row 的批次返回
`source_advanced_no_visible`；只有同一份 complete inventory 下所有 shard 的 verified raw EOF
才能返回 `no_messages`。

Visible row 可以搭配另行读取的 read-only overlap context；context 永远不改变 tentative cursor。
AI/provider 失败不提交 cursor；AI/Knowledge 工作结束后、state CAS 之前还会复核 generation。
Retryable provider failure 只能针对读取到的 state revision CAS 写入 content-free failure
count/code/timestamps 与 bounded backoff deadline，并精确保留 source cursors、inventory binding 和
checkpoint。Deadline 之前的 run 返回 `ai_backoff`，不会调用 provider；成功 retry 会同时清理当前与
legacy failure metadata。

Knowledge write 使用由 source IDs 派生的 `source_batch_id`：若 event 已 commit、state revision CAS
却失败，下一轮会在 provider 之前检查 canonical SQLite，adopt 同一 event 并推进同一 source batch，
既不插入第二条 event，也不再次让模型解释消息。若 canonical lookup 自己失败，cursor 保持不动。
缺失的 managed topic Markdown 与 date indexes 从 canonical SQLite 重建。

每条新 canonical event 还会在同一个 SQLite transaction 插入 `daily_digest_changes`。已有 affected
Digest 从 canonical events 重建；inspection 时存在的每个 page 都 atomic republish 成功后，才 ACK
exact journal prefix。Digest 写入使用同目录 private temp、file fsync、atomic replace 与 parent fsync，
失败时保住 last-known-good。Event path 会立即 drain；60 秒 menu timer 即使当天 scheduled Digest
尚未 due，也会 drain journal，所以进程重启后仍有独立 repair trigger。

### Catch-up receipt finalization

Write-mode catch-up 会停止 managed LaunchAgent、取得 menu app 的 singleton lock，并持锁完成 backup、
bounded drain、projection rebuild、canonical validation、一次 post-drain checkpoint capture 与 durable
provisional receipt。Receipt publication 通过 atomic replace，并对 file 和 parent directory 执行
`fsync`。随后释放 lock，再恢复原本 loaded 的 LaunchAgent；最终以观测到的 restore result atomic
覆盖同一个 `run_id` receipt，app 恢复运行后不会再次采样 checkpoint。

Provisional receipt 只能是 `partial / drain_complete_restore_pending`，绝不是 success。Canonical work
通过且 LaunchAgent 恢复成功（或原本无需恢复）后，final receipt 才是 `complete / drained`；恢复失败会
durably 写成 `partial / launch_agent_restore_failed`、关闭 `resume_supported` 并返回 nonzero。Receipt
只保存 content-free error code，不保存 provider 或 chat content。

Encrypted WAL reconstruction 会验证 SQLite WAL header 与 cumulative frame checksum，只回放到最后一个
valid commit marker，按 committed database-page count 截断，并丢弃 uncommitted tail。发布 decrypted
cache 前还会重新核对 main/WAL identity；并发 checkpoint、reset 或 replacement 只做一次 bounded retry，
再失败就报告 `source_snapshot_failed`，不会发布 mixed snapshot。

File XML 与 image packed metadata 在 message text cleaning 之前解析。Resource envelope 可以保存
原文件名、declared size、declared MD5/SHA-256、attach id、extension 或 image hash。
AI formatter 只接收 sender 与 cleaned message text；source envelope 和 internal IDs 不会进入 AI prompt。

## 3. Attachment catalog 与本地 archive

一次 Knowledge 命中里，`attachment_mentions` 与对应 `events` row 在同一个 SQLite transaction
里插入。`attachment_objects` 是 canonical object catalog，`attachment_attempts` 是 content-free
attempt history。Event commit 后，app 可以启动 daemon thread 消费 pending mentions；
process-level archive lock 防止两个 worker 重复复制。Fresh `pending` 永远先于 retry；可自动重试的
失败会写入 `attempt_count` 与 `next_retry_at`，使用有上限的 exponential backoff，并且只有 due row
才会再被选中。每个 trigger 在抢 lock 前持久化 wake generation；active worker 会持续 drain batch，
直到没有 due work 或未消费 wake。

这条分离边界保证：

- cache resolver 或 copy 失败不能回滚已提交 event；
- 失败不会再次调用 AI；
- 失败不会倒退或额外推进 monitor checkpoint；
- retry 只修改 catalog state。

本地 archive 默认关闭。需要显式 enable 并选择资源类型；图片仍是单独 opt-in：

```json
{
  "attachment_archive_enabled": true,
  "attachment_archive_kinds": ["file", "image"],
  "attachment_archive_max_object_bytes": 536870912,
  "attachment_archive_min_free_bytes": 1073741824,
  "attachment_archive_retry_base_seconds": 300,
  "attachment_archive_retry_max_seconds": 21600
}
```

较新的 WeChat V2 image object 还需要本机捕获的 `image_aes_key`；这个 key 不会写入 attachment
catalog、archive receipt、backup manifest 或文档。

### 文件 resolver

Resolver 只搜索消息声明月份下的 WeChat `msg/file` cache，候选仅限 exact name 和常见
`name (N).ext` duplicate variant。有 declared size 或 MD5/SHA-256 时，先用 metadata 筛选。

- 只有一个有效候选：选中；
- 多个候选 bytes 完全相同：视为 equivalent duplicates，可 deterministic 选中；
- 多个候选 bytes 不同：`ambiguous`；
- 没有有效候选：`missing_retryable`。

目录顺序与 modification time 从不承担 identity。Symlink、non-regular file、以及 cache root
之外的候选都会被拒绝。

### 图片 resolver

图片只按 structured image hash、hashed chat directory 和 source month 定位，优先 full/original
suffix，再看 thumbnail suffix；完全没有 mtime fallback。结果明确区分：

- `original_archived`；
- `thumbnail_only`；
- `decode_unavailable`；
- `missing_retryable`；
- `ambiguous` 或对应 source rejection/failure 状态。

### Content-addressed storage

Object 存在：

```text
~/.we-groupchat-obsidian/attachment_archive/
  objects/sha256/<first-two-hex>/<sha256>--<safe-original-name>
  tmp/
```

Root 与 object directory 是 `0700`，final object 和 worker lock 是 `0600`。Copy 先写私有 partial，
边写边算 SHA-256，读取前后检查 source identity/size/mtime，随后 `fsync` 并 atomic rename。
Crash 后如果 final object 已存在但 catalog row 还没提交，下次可按 digest 复用；worker-owned partial
不会被当作 object。Partial recovery 只会在进程真正取得 archive lock 后开始，因此 losing worker
返回 `worker_busy` 前不能删除 active writer 的 temp file。

SHA-256 是 identity，所以不同文件名引用同一份 bytes 时只保留一个 immutable object。
Markdown 的资源区会显示 catalog status、可用时的本地 object link，以及既有月份目录 hint。

复制前，worker 会拒绝大于 `attachment_archive_max_object_bytes` 的 object，并保证拟写入之后仍
保留至少 `attachment_archive_min_free_bytes`。Oversized object 需要显式调整 policy 后 manual retry；
low-space failure 进入正常的 due-only retry schedule。

一个很重要的 storage boundary：archive **不会删除或 prune 微信自己的 cache**。它避免在 archive
里重复存同一份 bytes，却不会自动回收 source cache 占用。破坏性的 cache retention/pruning 不属于
这一 tranche，必须另行设计并审查。Backup target 也会再占用一份 target storage。

### Archive CLI

```bash
.venv/bin/python scripts/attachment_archive.py status
.venv/bin/python scripts/attachment_archive.py run --limit 50
.venv/bin/python scripts/attachment_archive.py retry --mention-id <id>
.venv/bin/python scripts/attachment_archive.py retry --mention-id <id> --run
```

历史回填严格 plan/apply 分离：

```bash
.venv/bin/python scripts/attachment_archive.py backfill
.venv/bin/python scripts/attachment_archive.py backfill --apply
.venv/bin/python scripts/attachment_archive.py backfill --apply --run
```

第一条只读历史 `events.files_json` 并报告 counts，不插入 mention，也不复制 bytes。
`--apply` 才显式插入缺失的历史 catalog row；再加 `--run` 才继续消费 pending rows。
`--limit` 是 batch size，不是本次总上限；worker 一旦获得 lock，会在退出前 drain 所有 fresh 与
当前已 due 的 rows。

## 4. 默认 selected-resource mounted backup

这是默认 Google Drive 路径：把 selected-chat links、selected file occurrences 与共享 CAS bytes
交给已经存在的 mounted filesystem，例如 Google Drive for Desktop。它不创建 Google Cloud project、
不索取 OAuth credentials、不调用 Drive API，也不做 browser automation。

```text
active monitor chats
  intersect resource_backup_selected_chats
  -> 每群 × message-shard occurrence capture
  -> exact URL metadata + 共享本地 SHA-256 CAS
  -> 本地 Obsidian resource index
  -> mounted target objects / catalog snapshots / views
```

Mounted lane 与可选 direct API lane 的 disclosure selection 完全独立：
`resource_backup_selected_chats` 只控制 mounted lane；
`google_drive_file_sync_selected_chats` 只控制可选 API lane。选择一边不会启用另一边。

### Private selection 与本地 policy

Mounted-backup defaults 全部 private、opt-in：

```json
{
  "resource_backup_selected_chats": [],
  "resource_backup_interval_seconds": 300,
  "resource_backup_max_messages_per_scan": 500,
  "resource_backup_min_free_bytes": 1073741824
}
```

先列出 active monitor chats；输出不会打印 raw `@chatroom` identifier。再用列表编号替换 mounted
backup selection：

```bash
.venv/bin/python scripts/resource_backup.py list-chats
.venv/bin/python scripts/resource_backup.py set-selected-chats 1
.venv/bin/python scripts/resource_backup.py clear-selected-chats
```

Target 与 link-export policy 存在独立 private `resource_backup.json` 中。目标目录必须已经存在；
worker 不会在 mount 缺失时把那个路径重新创建成普通本地目录。

Exact observed URL 及其 digest-bound identity 只保留在 private local ledger。所有人类可读
Markdown projection、snapshot/export value、Review Queue、Daily Digest、AI prompt 与对外 error
统一以 `core/url_safety.py` 作为 canonical display-redaction authority；query 与 fragment 中的
credential-bearing values 都会脱敏，包括常见 AWS、GCS 与 Azure signed-URL fields。支持的 export
mode 只有默认 `redacted` 与 `off`；旧配置里的 `full` 会迁移为 `redacted`，不再允许把 exact
credential-bearing URL 导出。内置远程链接预览保持 inert，即使旧 main config 写有
`monitor_fetch_links: true` 也产生零网络请求。本项目不声称提供 hardened crawler。

```bash
.venv/bin/python scripts/resource_backup.py set-target "<已经存在的挂载目录>"
.venv/bin/python scripts/resource_backup.py set-link-export-mode redacted
.venv/bin/python scripts/resource_backup.py init
.venv/bin/python scripts/resource_backup.py backfill-links --all
.venv/bin/python scripts/resource_backup.py backfill-links --all --apply --run-id <plan 返回的 run-id>
.venv/bin/python scripts/resource_backup.py backfill-links --from YYYY-MM-DD
.venv/bin/python scripts/resource_backup.py backfill-links --from YYYY-MM-DD --apply --run-id <plan 返回的 run-id>
.venv/bin/python scripts/resource_backup.py backfill --all
.venv/bin/python scripts/resource_backup.py backfill --all --apply --run-id <plan 返回的 run-id>
.venv/bin/python scripts/resource_backup.py backfill --from YYYY-MM-DD
.venv/bin/python scripts/resource_backup.py backfill --from YYYY-MM-DD --apply --run-id <plan 返回的 run-id>
.venv/bin/python scripts/resource_backup.py status
.venv/bin/python scripts/resource_backup.py plan
.venv/bin/python scripts/resource_backup.py run
.venv/bin/python scripts/resource_backup.py run --resolve-files --resolve-limit 10
.venv/bin/python scripts/resource_backup.py verify
```

`init` 只初始化 from-now cursors。每次 selection 都有 UUID epoch，因此同一秒内停选/重选也不会复用
旧身份或吞回停选 gap。Schema upgrade 后，unchanged legacy selection 第一次补 UUID 时只 adopt identity，
不推进 epoch，也不清空 shard cursor。历史 backfill 是 identity-bound staged plan/apply，不移动 live cursor。Plan 冻结
selection/source manifest，以 500-2,000 rows 的 bounded keyset page 写 staging，且不改 canonical
chat、cursor 或 occurrence；apply 必须使用 exact unexpired `run-id`，复核 candidate digest 与 current
selection digest，再重新打开 source 并要求 inventory digest 与 plan 记录完全一致；它不会重新扫描
message row。Inventory unavailable、incomplete 或 changed 时，会在 staging 进入 canonical occurrence
catalog 之前 fail closed。`backfill --all` 表示 known local WeChat shards 中仍然可读的
全部历史。`backfill-links` 不读取附件 bytes；任一 known shard degraded 时 canonical occurrence 写入 0 rows。普通 `run`
捕获 deterministic metadata occurrence、默认跳过 file-byte resolution、即使 target 不可用也继续刷新
本地 Obsidian index，然后才尝试 mounted handoff。`--resolve-files` 是显式授权。

```text
<target>/wgo-resource-backup/
  00-打开微信资源备份.md
  .wgo-destination.json
  v3/
    objects/sha256/...
    snapshots/<snapshot-id>/{manifest.json,resources.jsonl,COMPLETE}
    views/
      00-文件备份.md
      00-待补齐附件.md
      00-资源索引.md
      <chat>/{00-文件备份.md,文件备份/<month>.md,00-待补齐附件.md,待补齐附件/<month>.md,00-资源索引.md,资源索引/<month>.md}
```

根部 portal 是人类入口，菜单栏的 `📂 在 Finder 打开文件备份` 也会直接 reveal 它。
`文件备份` 只列已交付文件：每个 digest 一行，另列 occurrence 次数，并只链接 mounted CAS 中
那一份 bytes。`待补齐附件` 不生成 target link，会按尚未尝试、cache unavailable、retry、
本地空间、需处理、awaiting handoff 与 unknown 分组。Target views 使用一个合并的 v2
ownership manifest 管理三套 family，本地 Obsidian 仍是 v1；根部 portal 是独立 marker-owned singleton，只在
views 与 manifest 成功后最后写入。这些 portable relative Markdown links 不承诺 provider-native
Drive-web rendering。

从 pre-manifest projection format 升级时，系统只会在一次 reconciliation 中 adopt 带 exact
app-generated index marker 的历史生成文件，而且只检查已知 root/chat 页面与三种 generated directory
下一层的合法月份文件。每个 candidate 最多读取 16 KiB text prefix；symlink、binary、未知 shape 或
不合法 generated frontmatter 都被拒绝。其他人类文件不会被扫描，成功后立即写入正常 ownership
manifest；之后的 GC 仍完全由 manifest 约束。

Plan 与 run 都拒绝 filesystem root、与本地 source 相同/嵌套/祖先关系的 target、configured-target
symlink，以及 planned object、snapshot、view、chat-index directory chain 中的 symlink/non-directory
component。Snapshot/view 冲突返回 structured `target_failed`。第一次复制会边写边 hash，并立即
readback target bytes。Regular non-symlink destination marker 保存一个绑定 owning archive 的随机 UUID；
target-side lock 会串行化指向同一 mount 的不同本地 ledger。Projection manifest 在任何 write/managed GC
前同时验证 archive 与 destination identity。后续 scheduled run 对有效 local receipt 只用 `lstat`
确认 target 仍是 regular、non-symlink 且 logical size 匹配，不重新 hash，也不 hydrate streamed
placeholder；`plan` 与 `status` 也使用同一 metadata-only check。显式 `verify` 才重新 hash
所选 snapshot 的全部 objects，并检测 same-size corruption。

`sync_delegated` 只表示 resolved bytes 已写入 mounted filesystem 并立即验证；它绝不表示 provider-side
upload 或 remote checksum verification。如果仍有 eligible file unresolved，系统可以发布 hash-bound
`COMPLETE` catalog snapshot，但 run state 必须是 `pending_resources`，manifest 记录
`snapshot_completeness=catalog_complete`，CLI 非零退出。`COMPLETE` 只绑定 durable catalog，不会凭空
补出缺失 bytes，也不证明 expected WeChat shards 已被完整观察。独立的 path-free
`source_observation` 会记录 inventory digest、revision、state counts、error codes 与 `complete`；即使
catalog 本身没变，只要这份 evidence 改变，也会生成新的 snapshot。

所有 surface 共用一套 classifier：`ready_local` 加有效 mounted receipt 才是 delivered；`queued`、
`waiting_cache`、`retry_wait`、`insufficient_local_space` 分别映射到对应 backlog；`ambiguous`、
`object_too_large`、`source_rejected` 是需要处理；有合法本地 object 但没有有效 delivery 是 awaiting
handoff；未知或结构损坏的 row 保持 unknown。Receipt validation 绑定 digest、logical size 与 backup root
内 relative path，普通轮次只做 metadata `lstat`（regular、non-symlink、size 匹配），不 hash target bytes，
也不打开 source CAS；显式 `verify` 继续完整 rehash。

兼容用的 `completed` 与 CLI exit code 保持严格；新增 `operational_success`、`coverage_complete` 与
`coverage`，分别说明 capture/projection/handoff 本轮是否健康，以及附件 occurrence 是否已经全部 delivered。
因此健康的 `pending_resources` 表示 catalog/index 已更新但 attachment-byte coverage 尚未完整，不再被通知
文案误写成普通 operational failure。

Ordinary status 只有在 current catalog、target objects、有效 latest `COMPLETE`、link mode 与 manifest
全部一致时才报告 `sync_delegated`。Snapshot 缺失或 object pending 时报告 `pending`，file unresolved 时报告
`pending_resources`。WeChat source unavailable 时，显式 CLI 仍可投影并 handoff 已有 ledger/CAS state，
输出 structured JSON，并因 source outage 返回非零。

后台捕获与 projection 在长驻菜单 app 中开启：

```bash
.venv/bin/python scripts/resource_backup.py enable
.venv/bin/python scripts/resource_backup.py disable
.venv/bin/python scripts/resource_backup.py agent-status
.venv/bin/python scripts/resource_backup.py uninstall-agent  # 清理旧 agent
```

菜单 app 在 source 初始化前取得 process-lifetime singleton。Main config writer 会在 lock 内重读最新
revision、只 patch 所需字段，再用 same-directory atomic replace 发布；app 监听 revision 并在不重启的
情况下 reconcile timers。显式 operator CLI source run 仍保留，但和 app 共用跨进程 capture lock；
projection render、managed GC 与 mounted handoff 会重新取得这把 capture lock、reload canonical
selection，并在整个 output phase 保持 authority，再取得 DB-scoped backup lock。本地 projection
另外按 output root real path 取得私有 `0600` root-identity lock，mounted handoff 则取得 target-side
lock；因此不同 capture DB 或 path alias 也不能并发管理同一个 projection / target。Managed
descendant 若为 symlink 或非目录，会在 write / GC 前 fail closed。

Capture constructor 本身不写 SQLite：schema / archive identity 只在取得 capture operation lock 后
初始化，backup delivery tables 也只在 backup-DB lock 内初始化。Attachment occurrence transition
会消费 status + revision compare-and-set 的真实结果；claim 被拒绝时报告 `superseded` / degraded，
不会被 stale worker 虚增为 ready 或 failed。

File-byte resolution 默认关闭，必须经菜单确认，或在当前 CLI run 显式给出 `--resolve-files`。菜单授权
只存在内存中，不写入 durable config，并在 app process 退出时归零；每次 attachment-byte operation 前
还会重新检查，关闭后下一份文件立即停止。
旧 resource LaunchAgent 可被检测和移除，但新安装会因同一 process-lifetime consent 原因被拒绝。

手动通知会区分三种健康结果：coverage 完整时写“更新完成”；本轮健康但仍有 backlog 时写“索引已更新，
附件仍待补齐”；新解析或复制出 bytes 时报告进展。Source、resolution、projection 或 target 任一 phase
degraded、`worker_busy` 或 unknown 仍会报告未完成，不会压扁成 success。菜单同时显示 delivered object 数、
delivered occurrence 数、backlog 数和本次 session 的附件解析授权状态。

## 5. 可选 advanced selected-chat Google Drive API 直传

这是为明确需要 Drive-native object/shortcut 的用户保留的可选 advanced transport。它独立于
`TopicMonitor`、Knowledge selection、source guard 与 filesystem snapshot：

```text
用户选定的微信群聊
  -> 每群 × message shard scan cursor + durable file-message queue
  -> 既有精确 resolver + 共享本地 SHA-256 CAS
  -> 每份唯一 bytes 一个 Drive object
  -> 群聊 / source month 的 Drive shortcut projection
```

第一版只接受 source envelope 中 `kind=file` 的真实文件。图片、语音、视频与表情不会进入这条 queue。
Scanner 会跨过缺失文件与非 file 消息继续推进，因此一个暂时没有自动下载到微信 cache 的文件不会堵住
后面的消息。Cursor 是 per-chat × privacy-safe message-shard cursor：某 shard 失败时只把该 shard 记为
`source_degraded` 并保持 cursor 不动，健康 shard 仍可推进；恢复后漏读文件只会入队一次。普通
`WeChatDB.get_messages()` 也使用 strict all-known-shards contract，任何已知 shard 失败都不会返回伪完整的
剩余 rows。Receipt 和 health 只记录 content-free error code 与 degraded shard count，不记录 raw path、
chat ID 或 database name。

`missing_retryable` 只在 due 时按有上限的 exponential backoff 重试；`ambiguous` 不猜测、不上传。

第一次 enable 会把当前已选择群聊的 cursor 初始化到“现在”，不会静默上传全部历史。历史发现严格是另一条
plan/apply action。把某个群聊从 selected set 移除时，其 cursor、queue 与 retry state 都保留，但该群 pending
item 会停止 resolve/upload；重新选中后再继续。

### 本地 config 与独立 ledger

Public 默认全部关闭：

```json
{
  "google_drive_file_sync_enabled": false,
  "google_drive_file_sync_paused": false,
  "google_drive_file_sync_selected_chats": [],
  "google_drive_file_sync_interval_seconds": 300,
  "google_drive_file_sync_max_messages_per_scan": 500,
  "google_drive_file_sync_max_uploads_per_run": 20,
  "google_drive_file_sync_max_bytes_per_run": 536870912,
  "google_drive_file_sync_root_name": "微信群文件归档",
  "google_drive_file_sync_keep_local_objects": true
}
```

Selected chat username 只存在 private local config 和独立本机 ledger。Remote chat identity 是由随机本地
`archive_id` 加盐派生的 SHA-256 key；Drive metadata 永远不写 raw `@chatroom` username。每个群聊 × shard
cursor 保存 timestamp 和该 timestamp 已见过的完整 message identity set，因此同秒分页、restart 都不会漏或重。
`(source_message_id, resource_index)` 全局幂等。这套 DB 与 run receipt 不保存 raw chat body。

`drive_scan_state` 保存 enable-time chat seed；canonical 增量位置保存在 `drive_scan_shards`。其余主要表是
`drive_sync_items`、`drive_objects`、`drive_placements`、`drive_folders` 和 content-free
`drive_sync_runs`。Menu timer、CLI 与 app startup recovery 都调用同一个
带 non-blocking process lock 的 bounded worker，不新增 infinite loop。Disable/pause 后不开始新的 scan/upload；
如果开关在一个文件处理中改变，当前单文件安全结束，worker 不再取下一项。

`attempt_count` 只计算 retry failure；`uploading -> shortcut_pending -> complete` 等成功状态转换不再增加
exponential backoff。成功跨过 resolve、object 或 shortcut phase 时会重置该 phase 的连续失败计数。

### OAuth 与 credential boundary

Auth 使用 Installed desktop app OAuth 2.0、system browser、loopback callback 和 PKCE。唯一 scope 是：

```text
https://www.googleapis.com/auth/drive.file
```

用户自己的 OAuth client JSON 会被 normalize 到 private runtime directory，权限 `0600`；它不进入普通
config 或 tracked source。Refresh token 只存 macOS Keychain，access token 只在内存里；请求 offline access。
不支持 service account。`auth-status` 会实际验证 refresh token，并区分 `token_present` 与 `connected`；
`invalid_grant` 会清除已经无效的 Keychain token，之后不会继续报告 connected。Item 进入
`auth_required`，该 episode 只通知一次，queue 不丢。`disconnect` 只删除 Keychain refresh token，不删除
config、queue、CAS 或 Drive 文件。

### Drive identity 与 projection

第一次成功 remote run 会创建或 adopt 一个 app-owned root，默认可见名称是 `微信群文件归档`。Drive file ID
才是 authority：用户改名或移动不影响。已知 root 被 trashed、丢失、invalid 或权限不可读时进入
`remote_degraded`，不会偷偷建第二个 root。

```text
微信群文件归档/
  群聊/<stable-local-alias>/<source YYYY-MM>/<Drive shortcut>
  _系统/objects/<sha256-prefix>/<full-sha256>--<safe-original-name>
```

本地 SHA-256 是 canonical byte identity。上传前先查 local object ledger；ledger 不确定时再按
`appProperties` 搜索。因此 remote create 成功、local commit 前 crash，下次会 adopt 已存在 object/shortcut，
不会重复。多个 remote object 命中同 digest 时选择一个 canonical Drive ID，记录
`remote_duplicate_detected`，不上传第三份。Verification 优先比较 `sha256Checksum`；字段缺失时必须同时
匹配 size 与 `md5Checksum`，才写 `uploaded_verified`。

超过 5 MiB 的 object 使用真正的 resumable session：每个 `308` 都按 response `Range` 的 server-confirmed
offset 续传；network/429/5xx 后先用空 PUT 与 `Content-Range: bytes */<total>` 查询 session。Probe 若表明
lost final response 实际已完成，会直接采用完成 response；404 expired session 在同一 one-shot run 中最多
重建一次。Malformed/missing/regressing Range 明确失败，不会把整 chunk 猜成已接收。Session URI 不跨进程
持久化：进程中断后的下一 run 重新建 session，并仍通过 remote `appProperties` adopt 已完成 object；这是
明确的 bounded restart policy，不是 blind chunk restart。

同一 object 在全局只上传一次。Placement identity 是
`(hashed_chat_key, source_month, sha256)`：同 bytes 出现在另一群或另一月份只加 shortcut。同名不同 hash
使用 `stem--<hash8>.ext`。普通 child folder 或 shortcut 被删除后，`reconcile` 可重建；程序永不自动删除或
trash Drive item。

Remote `appProperties` 只含 schema version、随机 archive ID、role，以及相应 SHA-256、hashed chat key 和
source month。完整 source-message provenance 只留在本机。Google Drive 会收到被选群聊的 configured
alias、原始/安全文件名与文件 bytes；不会通过这条 lane 收到 raw chat username、raw XML/body、
`source_message_id`、`wxid` 或本机 WeChat cache path。

### CLI 与菜单控制

Auth、选群、enable、历史 backfill 与真实 upload 是五个分开的动作：

```bash
.venv/bin/python scripts/google_drive_file_sync.py auth --client-secrets "<installed-desktop-client.json>"
.venv/bin/python scripts/google_drive_file_sync.py auth-status
.venv/bin/python scripts/google_drive_file_sync.py status
.venv/bin/python scripts/google_drive_file_sync.py enable
.venv/bin/python scripts/google_drive_file_sync.py disable
.venv/bin/python scripts/google_drive_file_sync.py pause
.venv/bin/python scripts/google_drive_file_sync.py resume
.venv/bin/python scripts/google_drive_file_sync.py scan
.venv/bin/python scripts/google_drive_file_sync.py run
.venv/bin/python scripts/google_drive_file_sync.py reconcile
.venv/bin/python scripts/google_drive_file_sync.py backfill --from YYYY-MM-DD
.venv/bin/python scripts/google_drive_file_sync.py backfill --from YYYY-MM-DD --apply
.venv/bin/python scripts/google_drive_file_sync.py disconnect
```

`backfill` 不带 `--apply` 时完全只读。`enable` 只初始化当前 selected chat cursor seed，不会 auth、选群、运行
backfill 或上传。菜单栏的 **Google Drive 群文件备份** submenu 提供 status、enable/disable、pause/resume、
立即同步、选择群聊、打开 root 与重新授权；选择群聊本身也不会 enable 或 upload。

CLI 的 `auth-status` 会验证 refresh token；`auth_required`、`retry_wait`、`remote_degraded`、
`source_degraded` 和 failed one-shot result 返回非零 exit status，避免 shell 或 scheduler 把错误 JSON 当成功。

## 6. 可选 filesystem backup target

Backup layer 只认识 filesystem path。它没有 Google Drive、Dropbox、iCloud 或其他 provider API，
不保存 OAuth token/provider credential。Target 可以位于 provider desktop sync folder 内，
但真正 provider upload 在本项目 authority 之外。

显式设置或清除 target：

```bash
.venv/bin/python scripts/attachment_backup.py set-target "<filesystem-target>"
.venv/bin/python scripts/attachment_backup.py clear-target
```

Target layout 完全 provider-neutral：

```text
<target>/v2/
  objects/sha256/<first-two-hex>/<sha256>
  snapshots/<snapshot-id>/manifest.json
  snapshots/<snapshot-id>/catalog.json
  snapshots/<snapshot-id>/COMPLETE
  receipts/<snapshot-id>.json
```

Archive root 现在持有 provider-neutral `cas_catalog.db`，记录唯一 SHA-256 object identity 与
content-bounded source binding；Knowledge attachment lane 与 selected-chat Drive lane 都写同一个 catalog，
不复制 bytes、不创建第二种 object identity。Filesystem snapshot 对该 catalog 与 Knowledge attachment
catalog 取 authoritative union，因此从未命中 KnowledgeStore 的 Drive-only selected-chat object 也会进入
plan/run/verify 和 DB-free restore plan，同时保留原有 topic/event binding 与 privacy-bounded export。

`plan` 对 provider-neutral CAS objects 与 privacy-bounded attachment catalog 取 stable read view，检查
target object 是 missing、`target_verified` 还是 `target_failed`，不写 target。`run` 用 partial file
复制缺失 immutable object、验证 source hash 并 atomic publish target object。只有全部 object 成功，
才写 `manifest.json` 与 `catalog.json`，最后发布 `COMPLETE`。Catalog 包含 object SHA-256/size、原始
名称/类型、`source_message_id`、numeric topic/event binding、status 与 resolution method；明确不含
raw chat body、`wxid` 或 WeChat cache/archive path。失败时仍会写 content-free receipt 供
reconciliation。发现 target 上同 digest path 的 bytes 冲突时不会覆盖，unmanaged target files
也永不 prune。

`plan` 与 `run` 都会拒绝 filesystem root，以及与本地 archive/database 相同、位于其内部或作为其
ancestor 的 target。检查同时比较 lexical path 与 resolved path，因此 symlink escape 在任何 target
写入之前也会被拒绝。

```bash
.venv/bin/python scripts/attachment_backup.py status
.venv/bin/python scripts/attachment_backup.py plan
.venv/bin/python scripts/attachment_backup.py run
.venv/bin/python scripts/attachment_backup.py verify
.venv/bin/python scripts/attachment_backup.py verify --snapshot-id <id>
.venv/bin/python scripts/attachment_backup.py restore-plan
.venv/bin/python scripts/attachment_backup.py restore-plan --snapshot-id <id>
```

`verify` 重新 hash target 当前可见的 objects，只报告 `target_verified` 或 `target_failed`；不会报告或暗示
cloud-upload verification。`restore-plan` 是 read-only，只统计 target 上已验证、但本机缺失/损坏
的 object 数量与 bytes；它读取 snapshot catalog 并直接扫描本地 CAS，因此本地 Knowledge
DB/catalog 缺失时仍能工作。这一 tranche 故意没有 automatic restore 或删除功能。

## 7. Health 与安全 rollout

Redacted health check 现在提供一份 privacy-safe reliability matrix：明确区分 monitor state
`healthy|missing|corrupt|conflict`，统计 generation-bound raw cursors；对 configured source inventory
只读 inspect，不做 source scan、也不创建 ledger，并分别报告 complete/degraded、present、missing、
cache-only、key-missing 与 unreadable。只有 expected logical-shard inventory 完整、current generation set
保持稳定且 required reads 全部成功，才能叫 source complete；一次 enumeration 有 rows 不够。

同一个 health surface 会只读报告现有 mounted destination/snapshot handoff，且不打开 CAS payload
objects。Mounted `sync_delegated` 始终保留 `provider_side_sync=unknown` 与
`remote_verified=False`。独立 Direct Drive 行只统计通过 Drive API verification 的 ledger objects；
这份 remote evidence 与 source completeness 互不替代。Health 还直接写明三个固定产品边界：remote
link preview 是 `link_preview_disabled` 且零请求；MCP 是 legacy read-only、send retired；Windows
W0.2A 只提供可移植文件锁源码，不代表产品支持。

```bash
.venv/bin/python scripts/health_check.py
.venv/bin/python scripts/resource_backup.py status
```

默认输出不含 absolute path、chat name/username、message text、source-relative path、API endpoint
或 token material。只有显式 `--sensitive` 才会为本机 debug 披露 path、title 与 source-shard details。

### Upgrade 与 migration 行为

- 合法的 unversioned monitor state 仍可读取，只在之后一次成功的 locked write 中升级；corrupt 或
  non-regular state 绝不 migrate，也绝不 reset to now。
- Legacy timestamp checkpoint 只有在 complete inventory 下才用于 seed 缺失的 per-generation raw
  cursors；generation change 绝不继承旧 token。
- Source-inventory health inspection 不创建、不迁移 ledger；只有 actual source scan 才初始化。
- 旧 `monitor_fetch_links=true` 加载后仍为 disabled；旧 mounted `link_export_mode=full` 加载为
  `redacted`，两者都不能恢复已经退休的 network/export 行为。
- Legacy MCP send keys 仍可解析但完全 inert；所有旧 send tool 固定返回 `mcp_send_retired`。
- Windows W0.2A 为 config、app singleton、monitor state 与 source inventory 增加可移植文件锁，
  但不激活 source、monitor、backup、tray、autostart、packaging 或 sending；后续 Windows phase
  仍是独立 migration。

第一次安全启用 mounted backup 建议按下面顺序：

1. 保持 optional direct API lane disabled；
2. 用 `list-chats` 查看 active chats，按编号只选择一个无敏感 canary，再执行 `init`；
3. 在 mounted provider root 下选择一个已经存在的目录；
4. 先跑 `plan`，然后在 from-now cursor 之后发送一个无敏感 link 与一个 small file；
5. 显式运行一次 `run` 与 `verify`，检查 occurrence/CAS、Obsidian index、mounted object 与 catalog snapshot；
6. 从另一个 Drive surface 确认 provider-side arrival，因为 `sync_delegated` 不是 remote verification；
7. 最后才开启长驻 resource timer；除非明确需要附件 bytes，否则保持 file resolution 关闭。

可选 advanced Direct Drive API rollout 仍然独立：

1. 准备自己的 Google Cloud Installed desktop app OAuth client JSON；
2. 只执行 `auth`，用 `auth-status` 确认，不 enable；
3. 在菜单选择群聊，检查将显示到 Drive 的 stable alias；
4. 如果需要历史，只跑一次 `backfill --from ...` dry plan；
5. 执行 `enable`，再手动 `run` 一次，检查本地 `status` 与 Drive root；
6. 审查 counts 后才决定是否运行历史 `--apply`；
7. 激活后重新跑 redacted health check。

Source guard 与更广 attachment filesystem snapshot 各有自己的 rollout：先看 status/plan，未单独审查前
保持 disabled 或 unconfigured。

开启 source guard、选择 mounted-backup chats、写真实 mounted target、开启 resource timer 或附件 bytes 解析、为可选
API lane 做 Google auth/选群/enable、执行任何历史 backfill、清理微信 cache 与删除 Drive 文件，始终是
分开的 operational actions。Source availability、本机保存、mounted target-byte verification、File
Provider upload state 与已验证的 Drive API object/shortcut state 也是不同事实。
