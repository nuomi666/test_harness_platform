---
name: meeting-minutes                      # 技能唯一标识，全小写，用连字符
description: |                              # 技能描述，是Agent决定是否调用的关键
  将对话或会议内容整理成结构化的会议纪要。
  当用户说"总结这段对话"、"生成会议纪要"或"提取行动项"时使用。[reference:19]

# 可选字段：提供更丰富的上下文
version: 1.0.0                             # 技能版本号
license: MIT                               # 开源协议
author: Your-Organization                  # 作者或组织
allowed-tools: [read_file, Bash]           # 允许Agent调用的工具[reference:24]
---
# ==================== Markdown 指令部分 ====================
# 这里是给AI看的"工作手册"，详细说明如何执行任务[reference:26][reference:27]

# Meeting Minutes Skill

你是一个专业的会议纪要生成专家。请严格遵循以下步骤将对话内容整理成会议纪要。

## 工作流程
1.  **提取关键信息**：仔细分析对话，提取每位发言人的核心观点。
2.  **提炼行动项**：明确列出所有行动项（Action Items），并标注**负责人**、**具体事项**和**截止日期**。[reference:28]
3.  **应用模板**：使用 `templates/meeting_template.md` 中的格式来组织最终输出。[reference:29]
4.  **执行脚本（条件触发）**：如果用户明确要求“上传纪要”或“保存纪要”，则调用 `scripts/upload_meeting.py` 脚本进行上传。[reference:30]

## 输出格式规范
-   必须严格遵循 `templates/meeting_template.md` 的格式。
-   确保所有行动项清晰、可执行。

## 参考资料（按需加载）
-   如果会议内容涉及**费用、预算或报销**，你必须参考 `resources/company_financial_manual.md` 中的公司财务政策，并在纪要的“注意事项”部分添加相关提醒。[reference:31]