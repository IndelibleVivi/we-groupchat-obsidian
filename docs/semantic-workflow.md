# WGO 语义整理与 Obsidian 阅读

这是新语义 generation 的使用说明。它描述本 checkout 的候选源码；安装、全量重建、激活和真实增量验收是独立步骤。没有 active generation 的已有安装继续使用原来的 topic/event monitor。完整操作见 [重建 runbook](semantic-rebuild.md)，当前交付状态见 [current state](current-state.md)。

## 内容如何成为笔记

选中群聊的消息先保存为不可变的 SourceRecord 版本，再抽取零到多个 Observations。一个聊天窗口可以包含技术讨论、生活片段和玩笑；它们可以形成不同 Item，也可以为跨天、跨群的已有 Item 增补细节、证据或更正。阅读偏好不再决定哪些内容有资格留存，也没有旧版相关性分数门槛。不能确定的输入留在待处理状态，不能伪装成“无内容”。

SQLite 保存 canonical source、语义、关系、任务和提交证据。Markdown、全文和向量索引是可重建的阅读/检索结果。消息的发生时间、首次捕获时间和后来获知更正的时间分别保存；重新观察同一版本不会刷新内容，A→B→A 则保留三次变化。链接展示和模型输入仍使用统一 URL 脱敏规则；精确 URL 只留在私有 ledger。

整理保留成员归属：使用该条消息捕获时已有的名字，并用可验证的来源身份连接同一人的后续。同名、相邻发言或相似语气都不能证明同一人；缺少稳定身份时保持未知。发言人、转述者和被谈论的人物各有含义，不能把转发内容自动算作发言人的亲身经历，也不根据名字补写人物关系或动机。原始账户标识不作为展示名传给模型或写入阅读页。

新增消息的两条进度分别是 `captured_cursors` 与 `source_cursors`：前者在 raw rows 持久化后推进，后者必须等连续输入完成语义处理、canonical transaction 和 outbox 一起提交后才能推进。笔记冲突或远端向量尚未确认会保留待办，不会让成功的语义处理重跑模型。来源缺 shard 或 generation 改变仍失败关闭。

## 在 Obsidian 中阅读和操作

新 generation 使用固定的 `WGO/` 阅读根目录。Item 保持稳定 `wgo_id`，Views 通过链接组织多个归属，简报保留当时的正文与证据。旧笔记有完整的冻结备份和可见的 Legacy 导航，不把旧摘要冒充原始聊天。

正文按内容选择 Markdown 形式：比较不同条件时可用表格，操作顺序用有序列表，平行要点用列表，准确原话用带归属和来源的引用。故事可以保留连续叙述，不强制插入表格。格式变化不能丢失条件、步骤或反证，也不能把不同成员各自的测试拼成一个人的完整成功案例。

菜单中的“打开知识库目录”“打开检索”“打开阅读管理”进入这些页面：

- **检索.md**：在已注册的“查询”属性填写问题，源无关 reader worker 调用同一套 lexical、dense 与结构检索，返回可跳转的结果。远端 index 未确认时显示覆盖限制，不能把 TopK 没命中当成全库不存在。
- **管理.md**：勾选页面生成的操作。隐藏 action identity 绑定对象、内容版本和当前文件；手写一个相似 checkbox 不会得到执行权限。刷新会保留尚未消费的有效勾选，过期操作会明确报告。
- **Item properties**：默认使用“阅读关注”“阅读静音”“阅读等待”。字段首次注册时检查冲突，不接管已有同名用户数据。`semantic_reader_properties` 可以分别映射 `follow`、`mute`、`wait`、`query`；属性名必须唯一且不能使用保留的 `wgo_*` 前缀。

关注没有自动到期日。等待描述由模型提出可核对条件，typed decision 决定是否采用；缺少确切的 subject/case/time 证据会保持 `unknown`。等待支持一次发生与当前状态两种含义；旧测试不能证明“恢复后的完整实测”，有效替代证据尚在时也不会错误撤回已满足状态。Review 时间只是提醒重新审视。

“已知”只对应明确确认的内容版本。可选 [Obsidian visible-open bridge](../integrations/obsidian/README.md) 为实际可见页面生成本机事件；app 核对 generation、Item、内容版本、文件和路径后记录阅读。发出打开命令、mtime 变化或系统接受通知都不等于读过。没有插件时仍可显式标记已知。

WGO 只维护 `wgo_*` 属性和命名的 managed regions。用户自己的属性与正文区域保持原样。编辑了受管理正文时，WGO 保存 exact conflict backup 和待接受的 draft，在管理页提供解决动作；不会默默覆盖。文件重命名按稳定 identity 重新定位；identity 碰撞、symlink 或越出阅读根目录均拒绝。

## 简报与提醒

每日简报由一个 editor 综合跨 Item 的变化，生活故事可以独立成篇。没有固定的“技术/资源/风险”三栏。每个配置时区的日期只有一个 edition identity；到设定时间后，后续补充与更正修订同一期，不重复生成多个日期文件。旧期保留当时的冻结正文。编辑判断无值得读的变化是 `silent`；模型、证据或投影失败是 `deferred`，不能把失败当作安静的一天，上一版成功正文继续保留。

本地提醒在投递前重新核对最新静音/已知状态、canonical revision 和可打开的证据目标。事件、设备与 channel 共同防重；外部结果不明确时保持 unknown，不盲目重发。历史重建不会补发过去的提醒。通知点击使用 logical identity，文件重命名或 Item merge 后重新解析当前目标。

暂停监控停止新增来源采集；Obsidian 的轻量阅读控制仍独立运行。新选群聊从选择时间开始，保留同秒新行；再次调整列表不会重置已经迁移的群聊进度。旧 checkpoint 带 pending intent 时必须先由完整迁移证明其去向。

## 模型与向量

Text、typed decision、embedding 与 vector index 各有显式角色：

| 角色 | 当前实现 | 数据边界 |
| --- | --- | --- |
| Text | 现有配置的 AI provider | 抽取、候选动作、正文和简报；无隐式换服务 |
| Decision | Cloudflare Clef 或显式 text decision route | 只返回受约束的 choice/score/noul |
| Embedding | 本机 loopback 服务或 Cloudflare BGE-M3 | 脱敏后的检索文本交给所选服务；本机 adapter 不跟随重定向或改换服务 |
| Vector index | 本地 cosine 或 Cloudflare Vectorize | SQLite 仍为 canonical；远端只放向量及允许的 opaque metadata |

本机 embedding 支持显式 OpenAI-compatible `/v1/embeddings` 或 Ollama native `/api/embed`；必须配置实际模型、维度和经部署确认的 context 容量，超限拒绝而不裁剪。Ollama native 另发送 `truncate:false`。服务自身须使用本地模型。完整本地路线还要求 Text 与 Decision 使用本地服务。选择本地 index 不会把 Cloudflare embedding 变成本地模型。Vectorize 按 WGO generation/profile 隔离，不能复用另一个应用的索引身份。远端写入只有在 mutation 和精确记录均确认后才能标记 indexed；HTTP 接受不是完成证据。

仅所选 Cloudflare 角色要求账户凭据与预算。本机 embedding＋本地索引＋text decision 不加载 Cloudflare 凭据或使用量数据库。Cloudflare 的账户 UTC 日预算在 app 级 usage ledger 预留，跨 generation 共用。外部使用量必须有未过期的确认；缺证据、额度耗尽或未知请求占用时阻断调用。普通 text provider 不套用 Cloudflare 免费额度。Semantic text 默认允许最多 16384 output tokens（普通摘要仍为 4096）；`semantic_text_output_tokens` 可按已配置模型能力调整。返回被截断是明确失败，不能进入 canonical 内容。

账户、Keychain 引用、source 路径、原始输入、向量及完整 SPEC 均留在私有运行目录。公共 WGO 提供通用 capture、resource ledger 与 snapshot transport；产品专用导入、selection、发布和撤回由公共 checkout 外的 adapter 拥有。公共 app 不启动或配置这些私有服务。

## 诊断与恢复

```bash
.venv/bin/python scripts/health_check.py --semantic-json
.venv/bin/python scripts/rebuild_knowledge.py --help
```

健康检查只报告本机已提交的 generation、capture/applied、jobs、index/projection 与预算证据；不读取微信、调用模型或替代迁移验收。`candidate`、`pending`、`unknown`、`blocked` 各有不同含义。故障恢复必须沿 [重建 runbook](semantic-rebuild.md) 的 exact run id，不能删除数据库、清空 checkpoint 或用一次新扫描覆盖原有 pending intent。

预算状态按 generation 内完整 provider profile 的显式角色读取：Cloudflare Decision 或 Embedding 要求 Workers AI 证据，远端 Vectorize 只要求 Vectorize 证据。合法的本机 embedding＋本地 index＋text decision 显示 `provider_budget.state=not_required`、`ready=true`，不检查使用量数据库，也不代表本机模型服务已经在线。缺失或无效 profile 分别显示 `profile_missing`、`profile_invalid`，均不视为预算就绪；unknown job 仍保留为未知状态。

实现责任和机器契约分别见 [store](semantic-store-contract.md)、[engine](semantic-engine.md)、[provider](semantic-providers.md)、[reading](semantic-reading.md)、[legacy](semantic-legacy.md) 与 [monitor v3](monitor-state-v3.md)。
