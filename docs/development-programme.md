# WGO 的成熟目标与连续建设主线

WGO 要让群聊里发生的事情、形成的经验与提出的方案，在一个可查证、会更新、
尊重个人编辑的知识空间里延续。一个人可以读懂事情本身、找到支持与反证、带着问题
找回材料，并继续关注尚未结束的事情。摘要、简报、检索与提醒共同服务这个使用过程。

本文维护产品方向、能力之间的连接和阶段验收。当前实现状态由
[current-state](current-state.md) 说明；具体使用见[阅读工作流](semantic-workflow.md)，
机器契约与操作权限仍由各自 owner 和 [AGENTS](../AGENTS.md) 管理。
后面的建设目标不表示这些能力已经实现、安装或激活，也不授予来源、模型或部署权限。

## 已经成立的方向

从最初公开的群聊摘要与 Obsidian 输出，到 canonical semantic Items，再到 source-first
重建和连贯正文，几次关键调整保持同一方向：

- **保存值得延续的内容。** 技术讨论、经验、生活、故事和原话都有留存资格。
  兴趣用于安排阅读与简报，不是丢弃其它知识的门槛。
- **围绕同一件事继续。** 一个 Item 可以跨群、跨天增补、修正与重新打开。
  时间接近、同名或共享链接不足以证明身份相同；条件、案例、主体和时间不能被拼平。
- **正文讲清事情，来源随时可核对。** 正文可以综合和改写，显式逐字义务、来源片段、
  归属和资源保持精确。旧摘要独立保全，不冒充原始聊天进入新语义知识。
- **与人长期共同编辑。** 稳定 identity 和段落 home、重命名接续、用户属性、手写保护、
  明确冲突解决，是知识库的正常行为。
- **不把中间结果当完成。** 捕获、语义处理、正文、索引、备份、远端确认、通知、
  实际打开与明确已知，分别保存证据。一个环节失败时，其它已成立的事实仍然成立。
- **由用户掌握数据与选择。** SQLite 和可携带的 Markdown 是本地资产；云模型和远端
  索引有明确的数据边界，附件读取有单独同意。自动化不能偷偷扩大来源或消费范围。

源码历史中，`f9e452c` 建立 semantic Items，`b0d05a3` 回到 raw source-first，
`732df6e` 分离派生措辞与逐字义务，`4cb8c7c` 恢复完整可编辑内容责任，
`a7beb7e` 强调具体问题、并行选项和有证据限定的结果。这些是理解设计的线索，
实际操作应使用当前契约，而不是照旧提交恢复流程。

## 当前能力与尚未闭合的连接

| 使用任务 | 已接入的源码能力 | 成熟使用仍需补齐的部分 |
| --- | --- | --- |
| 不丢失群聊材料 | 完整 inventory、分 shard cursor、不可变 source、captured/applied、durable artifacts 与首次迁移 conservation | 完整私有迁移、真实增量和长期故障恢复的独立运行验收 |
| 读一件持续发展的事 | Item、稳定正文 home、条件与反证、原文嵌入、人工冲突与重命名保护 | 实际模型的连贯、完整、自然表达，以及 native Obsidian 阅读验收 |
| 持续收到可读产物 | canonical outbox、独立 index/projection 维护、局部发布隔离、维护状态与统一首页 | 真实长期运行与 provider/native 阅读验收仍单独完成 |
| 按主题探索 | 重建与日常 View 归纳、每轮有界依赖维护、主题说明与现有成员 pin/exclude、稳定投影 | 当前自动归纳只补充归属；复杂重新归类、完整目录超出 60 KB 后的分层方案和真实归纳质量仍需推进 |
| 带着问题找依据 | lexical/dense/structural hybrid search、canonical tail、覆盖说明与持久 query | 当前输出是检索片段；跨 Item 的有证据综合回答尚未实现 |
| 管理阅读与后续 | 首页连接当前/打开/已知版本、以明确已知为基线的具体变化页；follow/mute/wait、typed Watch/Episode、简报与独立 delivery receipt | 有界差异预览还需真实阅读检验；打开记录不推定理解或已知 |
| 保留和交付资源 | 独立 occurrence/selection、附件同意、CAS、mounted handoff 与独立 Direct Drive 确认 | 实际目标的交付与远端确认分别验收；不能从本地 COMPLETE 推出云端已同步 |
| 长期演进 | 唯一 active generation、冻结 policy/artifacts、首次迁移与 rollback | 已 active generation 的后续升级尚不支持；reader 意图、identity 与历史不能通过清库重建丢弃 |

这里的“已接入”表示源码与调用路径存在。合成测试可以拒绝结构、状态、证据和恢复错误，
不能证明自然语言质量、实际用户理解、安装、真实来源与账户侧结果。

当前可重复的贯通场景见
[`test_semantic_programme_integration.py`](../tests/test_semantic_programme_integration.py)：
真实临时 capture/engine/store/publisher 进入 reader poll，覆盖日常目录、用户批量设置、
后续新增、更正到具体变化页、unknown 与手写冲突隔离，以及重启后的空轮询。
TEXT/CHOICE/embedding 使用合成 provider，不能据此声称真实模型或 native Obsidian 已验收。

## 连续 programme

### 1. 持续可用的知识交付

先保证已经保存并完成语义处理的知识，能够在无新消息、暂停监控、部分 provider 或文件
操作失败时继续变成可读页面。Reader controls、索引、正文和目录分别报告状态；失败对象
保留原任务与产物，独立可完成对象继续前进。严格重建与激活仍要求完整证据。

验收覆盖实际 app worker、monitor downstream 与同一 core owner：index 故障不能挡住
独立正文；局部页面失败不能吞掉其它可发布产物；View/control 冲突不能报告 complete；
恢复不能重新抽取已有知识、重发 unknown 或覆盖手写内容。

### 2. 让已有知识每天都能生长成可使用的目录

源码已经将重建阶段已有的 View 归纳与日常增量连接起来，以真实 Item/View 依赖变化驱动有界维护，
保留用户定义、pin/exclusion 和稳定正文位置。不能在每个空 poll 全库调用模型，也不能
为创建目录擅自合并 Item。全 Item、链接和段落嵌入根据阅读目的选择。

验收从日常新增主题、晚到材料、更正和用户组织出发：无需再次 rebuild 就有合理去向；
不重复建目录；无变化不重复 provider；失败后复用原产物；手写和用户 override 继续有效。

当前 live v2 每轮处理最多 16 个 Item，仅在 provider 工作完成后进入短 canonical 事务，
fresh CAS、确定性 program proposal、正常 membership apply 与 receipt 一起提交。
用户说明与现有成员设置使用独立注册动作，同事务消费。旧 frozen v1 保持原行为，历史
stale action 不自动修复。自动归纳现在只补充 membership；改变说明不会删除原成员。
下一步需验证真实目录质量，并在相同完整性标准下设计大目录分层和明确可恢复的重新归类。

这一阶段还要验证增长成本。目前 resolver 为避免把未检查的歧义当作不存在，会逐页检查
整个 Item catalog；现有合成证据在 4,000 个 Item 上需要 167 次 coarse scan 调用，
不含后续 refinement，也不是实际服务延迟。应在相同身份完整性标准下比较有界检索、
分层或增量候选证明；不能简单换成 TopK 并删掉未决候选。相关现状见
[semantic engine](semantic-engine.md)。

### 3. 把问题、阅读与变化接成连续体验

在现有 query、Item、Brief 与 reader ledger 上继续建设。首先让人看清找到哪些依据、
覆盖有什么限制、读过哪个版本、明确已知哪个版本、后来改变了什么以及正在等待什么。
打开、理解、已知、关注和静音继续是不同事实。

源码已有以明确已知版本为基线的内容、claim 状态/条件与关系比较，连接变化索引和首页。
它使用 canonical snapshots，不让比较页产生新事实；尚未确认已知时展示当前保留内容。
接下来应把有证据的问题综合与具体变化接入同一阅读过程，而不是增加独立问答数据库。

进一步建设综合回答时，复用原 canonical revision、检索、条件/反证、source anchors 和
durable model artifacts。回答保留案例差异与未知范围；TopK 未命中不能变成不存在。
回答是有版本和依据的阅读产物，不成为另一套事实库。

### 4. 用真实使用收敛成熟度

以上各阶段都要把 representative scenarios 贯穿 source → Item → Markdown → 检索 →
简报 → 跟进，而不只验收一个函数。既有技术方案、生活故事、跨天补充、同名不同人、
转述、更正与待补资源 fixtures 可以作为可重复结构证据。

在独立授予来源/模型/runtime 权限后，再完成真实 provider 与 native 阅读、通知、迁移和
增量验收。读者应能说明事情本身、区分建议与实测、找到原话和反证、理解更新后的整篇，
并保留自己的编辑。根据这些观察修复普通安装、状态说明、性能与恢复路径。

首次激活稳定后，再建设显式、可恢复的后续 generation 升级，保留原始证据、稳定身份、
用户编辑、阅读/已知/关注/等待与交付历史。现有首次迁移器明确拒绝已有不同 active generation，
应继续保留这个边界，直到独立升级合同与 conservation 验收成立。更好的新 prompt 不能
通过改写旧 accepted artifact 或静默切换 populated generation policy 来安装。

Windows 继续遵守[现有分阶段边界](WINDOWS-DEVELOPMENT.md)。共享基础可以服务未来适配，
但当前 Mac 阅读主线的成熟不能由 import-safe 模块数量或新的 UI 框架代理。

## 判断下一步的依据

优先选择能连接已有能力、改善实际阅读或恢复的工作。结构化 schema、更多测试和详细契约
都有价值，但它们的价值在于保障上述使用结果。完整性既包括知识保全，也包括读者最终
读到、找到、修正和继续使用它；每阶段应同时给出源码、集成与实际验收各自达到的边界。
