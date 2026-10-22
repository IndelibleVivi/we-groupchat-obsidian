"""AI summary interface - abstract base class and prompt templates."""
from abc import ABC, abstractmethod


class OutputTruncatedError(RuntimeError):
    """The provider explicitly reported an incomplete, truncated response."""
    def __init__(self):
        super().__init__("text_output_truncated")


SUMMARY_PROMPT = """你是一个专业的群聊消息筛选与知识整理助手。请把以下群聊消息整理成适合快速回看的知识笔记。

## 群聊信息
- 群名：{group_name}
- 时间范围：{start_time} ~ {end_time}
- 消息数：{msg_count} 条

## 输出格式要求（严格遵循）

第一行：一句话说明这个时间段整体有没有值得回看的内容；不要写成“大家聊得很热闹”这种空话。

然后先输出以下栏目：

## 值得先看

只列真正值得用户回看的内容。按价值类型分组；没有内容的分组不要出现。

### 技术讨论
- **[具体标题]**：关键问题/观点是什么，重要补充是谁给的，最后有什么结论或仍未解决的点。

### 资源分享
- **[资源名或链接主题]**：资源/链接/关键词是什么，谁分享的只在有助于追溯时写，为什么值得打开。

### 实用技巧
- **[技巧标题]**：具体做法、步骤、命令、配置、prompt、workflow 或排错方法是什么。

### 难点与坑
- **[问题标题]**：遇到的坑是什么，表现是什么，可能原因/解决方向是什么。

### 结论与行动项
- **[结论标题]**：已经形成的共识、决定、待办、提醒或后续可追踪事项。

然后输出：

## 快速索引

用 3-8 条 bullet 列出最值得搜索/回看的关键词、模型、工具、链接、项目名或人名。每条说明一句为什么重要。

然后输出：

## 背景脉络

只在有必要时，用 2-5 条补充这段聊天的大致上下文。不要按时间线复述整段聊天，不要把普通闲聊写得像连续剧 recap。

最后，**仅当**消息中出现以下明确的通知信号时，才添加「相关提醒」栏目：
- @所有人 / @全体成员
- 明确标注为"群公告"、"通知"、"注意"、"重要"的内容
- 明确的时间约定（如集合时间、截止日期、会议安排）

如果没有上述信号，不要添加「相关提醒」。大家复制同一句话刷屏、接龙玩梗等不算提醒。

**相关提醒**
· [提醒内容，包含谁说的、什么事]

## 要求
1. 价值优先，不要按时间顺序机械复述。
2. 每个值得先看的条目必须说明“为什么值得看”。
3. 不要堆群友名字。只在以下情况点名：某人分享了关键资源、提出关键问题、给出解决方案、做出决定、或名字有助于用户回到群里搜索。
4. 如果需要点名，使用消息记录中的原始发言人名字；不要写"其他成员"、"其他人"、"等人"这种模糊称呼。
5. 对很花哨、很长、带装饰符号的昵称，可以只在第一次出现时保留原名；后续用“分享者”“提问者”“补充者”等角色称呼，减轻阅读负担。
6. 忽略纯表情包、拍一拍、复读、无意义水消息；可以在背景脉络里一句话说明“闲聊较多”，但不要展开。
7. 如果消息里出现链接、工具名、模型名、命令、配置、prompt、repo、论文、教程、debug 线索，优先提取。
8. 如果一个话题只是情绪互动或玩笑，但没有方法、资源、结论、关系动态或可复用信息，可以跳过。
9. 如果消息中有 [图片]、[视频]、[链接] 等非文字内容，简要提及即可；不要臆造看不到的图片/网页内容。
10. 不需要加"该总结由AI生成"等声明。

## 消息记录
{messages}"""


BATCH_SUMMARY_PROMPT = """你是一个专业的群聊消息总结助手。现在需要你对 **多个群聊** 的消息进行精简总结。

## 分组名称：{group_category}
## 包含群聊：{group_list}
## 总时间范围：{start_time} ~ {end_time}

## 输出格式要求（严格遵循）

第一行：一句话概述这组群聊在本时间段内的整体活跃情况。

然后 **按群聊分别列出** 每个群的精简总结。格式如下：

---
### 📌 {{群名}}（{{消息数}}条 · {{时间范围}}）

**1. [话题标题]**
· 群成员：[参与成员]
· 总结：[精简总结，保留关键信息和发言人名字，省略冗长的讨论过程]

**2. [话题标题]**
...

---
### 📌 {{下一个群名}}（...）
...

最后，**仅当**消息中出现 @所有人/@全体成员、群公告、通知、注意、重要 等明确通知信号，或有具体的时间约定时，才添加以下栏目（复制刷屏、接龙玩梗不算）：

**⚠️ 需要关注**
· [提醒内容]

## 要求
1. 每个群聊单独一个区块，不要把不同群的消息混在一起
2. 比单群总结更精简：省略讨论过程中的反复讨论和细节，只保留结论和关键信息
3. 但仍然要提到具体发言人的名字
4. **禁止**将发言人归纳为"其他成员"、"其他人"、"等人"等模糊称呼。始终使用消息记录中的原始发言人名字，即使名字看起来像 ID 也照常使用
5. 忽略纯表情包、拍一拍、无意义的水消息
6. 如果某个群在该时间段没有新消息，直接写"暂无新消息"
7. 话题标题要具体，不要太笼统
8. 不需要加"该总结由AI生成"等声明

## 各群消息记录

{messages}"""


SEARCH_SUMMARY_PROMPT = """你是一个专业的群聊消息分析助手。用户搜索了关键词「{keywords}」，以下是在多个群聊中搜索到的相关消息。请对这些消息进行归纳总结。

## 搜索信息
- 搜索关键词：{keywords}
- 时间范围：{start_time} ~ {end_time}
- 涉及群聊：{group_list}
- 命中消息数：{total_count} 条

## 输出格式要求（严格遵循）

按群聊分别列出每个群中与关键词相关的讨论：

---
### 📌 {{群名}}

**1. [相关话题标题]**
· 时间：[该话题讨论的时间段]
· 发言人：[参与讨论的人]
· 经过：[简述讨论过程，可以简写]
· 结果：[重点！最终结论、决定、结果是什么]

**2. [下一个话题]**
...

---
### 📌 {{下一个群名}}
...

最后，总结一段：

**📋 综合结论**
· 关于「{keywords}」，各群讨论的核心结果和要点

## 要求
1. 按群聊分别列出，每个群内按时间顺序
2. **重点突出结果**，讨论经过可以简写，但结果必须详细
3. 必须提到具体发言人的名字，说明谁提出了什么、谁做了什么决定
4. 如果同一个群中有多次讨论同一话题，按时间合并为一个条目
5. 忽略与搜索关键词明显无关的内容
6. 用自然流畅的中文书写
7. 不需要加"该总结由AI生成"等声明

## 搜索到的消息记录

{messages}"""


class AIProvider(ABC):
    """AI provider abstract base class."""

    @abstractmethod
    def summarize(self, prompt: str) -> str:
        """Send prompt to AI and return summary result."""
        pass

    def build_prompt(self, group_name, messages_text, start_time, end_time, msg_count):
        """Build single-chat summary prompt."""
        return SUMMARY_PROMPT.format(
            group_name=group_name,
            start_time=start_time,
            end_time=end_time,
            msg_count=msg_count,
            messages=messages_text,
        )

    def build_search_prompt(self, keywords_str, search_results, start_time, end_time):
        """Build search summary prompt.

        Args:
            keywords_str: Raw keyword string (e.g. "claude api").
            search_results: {username: [messages]} grouped by chat.
            start_time: Display string for search start time.
            end_time: Display string for search end time.
        """
        group_names = []
        parts = []
        total_count = 0

        for username, messages in search_results.items():
            if not messages:
                continue
            group_name = messages[0]["group_name"]
            group_names.append(group_name)
            count = len(messages)
            total_count += count

            lines = []
            for msg in messages:
                if msg["sender"]:
                    lines.append(f"[{msg['time_str']}] {msg['sender']}: {msg['text']}")
                else:
                    lines.append(f"[{msg['time_str']}] {msg['text']}")

            parts.append(
                f"======== {group_name}（{count}条命中）========\n"
                + "\n".join(lines)
            )

        messages_text = "\n\n".join(parts)
        group_list = "、".join(group_names)

        return SEARCH_SUMMARY_PROMPT.format(
            keywords=keywords_str,
            start_time=start_time,
            end_time=end_time,
            group_list=group_list,
            total_count=total_count,
            messages=messages_text,
        )

    def build_batch_prompt(self, group_category, groups_data):
        """Build batch summary prompt.

        Args:
            group_category: Group category name.
            groups_data: List of dicts with keys: name, messages_text,
                start_time, end_time, msg_count.
        """
        group_list = "、".join(g["name"] for g in groups_data)

        # Overall time range
        all_starts = [g["start_time"] for g in groups_data if g["msg_count"] > 0]
        all_ends = [g["end_time"] for g in groups_data if g["msg_count"] > 0]
        start_time = min(all_starts) if all_starts else "N/A"
        end_time = max(all_ends) if all_ends else "N/A"

        # Concatenate messages from all groups
        parts = []
        for g in groups_data:
            if g["msg_count"] > 0:
                parts.append(
                    f"======== {g['name']}（{g['msg_count']}条 · "
                    f"{g['start_time']} ~ {g['end_time']}）========\n"
                    f"{g['messages_text']}"
                )
            else:
                parts.append(f"======== {g['name']} ========\n（暂无新消息）")

        messages = "\n\n".join(parts)

        return BATCH_SUMMARY_PROMPT.format(
            group_category=group_category,
            group_list=group_list,
            start_time=start_time,
            end_time=end_time,
            messages=messages,
        )
