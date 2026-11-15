# MEMORY ANCHOR（防压缩失忆记忆锚点）

> 本文件位于会话的 Artifact 目录（`<appDataDir>/brain/<conversation-id>/MEMORY_ANCHOR.md`）。
> Antigravity 在触发有损压缩（`CHECKPOINT`）后会自动保留并列出 Artifacts 路径；`PreInvocation` 护盾也会在压缩后自动将本文件注入上下文，防止关键约束、精确数值与已排除路径丢失。

---

## 1. 铁律与负面约束 (Hard Constraints & Never-Do)
<!-- 记录用户明确禁止事项、格式红线、不可修改的文件/变量/字段、不可编造的数据边界 -->
- [ ] **禁止事项 / 不可违反约束**：
  - （例如：严禁修改/覆盖原文件 `xxx`；严禁改动数字、变量名、文献编号 `[N]`）
- [ ] **输出/交付格式要求**：
  - （例如：输出语言、编码、排版规范、验收阈值）

---

## 2. 精确事实与参数账本 (Exact Facts, Paths & Metrics)
<!-- 压缩摘要最容易把精确数字/路径泛化成“若干文件/某个目录”，务必在此原样固化 -->
- **关键文件与绝对路径**：
  - `[输入/目标文件]`：`...`
- **关键命令 / 环境 / 变量**：
  - `...`
- **精确数值 / 偏移量 / 段落索引 / 基线指标**：
  - `...`

---

## 3. 已排除死胡同 (Dead Ends & Failed Hypotheses)
<!-- 记录已经验证失败或被用户否决的方案，防止压缩失忆后再次重复试错 -->
| 尝试过的方案 / 假设 | 失败原因 / 现象 / 用户反馈 | 结论（禁止重试） |
|---|---|---|
| （例：某种改写脚本 / 某参数组合） | （例：导致公式丢失 / 检测率不降反升） | ❌ 勿重试 |

---

## 4. 里程碑与当前精确状态 (Progress & Current State)
- **已完成 (Done)**：
  - [x] ...
- **进行中 / 未完成 (Pending)**：
  - [ ] ...
- **下一步唯一精确动作 (Immediate Next Action)**：
  - `...`

---

## 5. 关键历史步骤索引 (Transcript Step Bookmarks)
<!-- 记录 transcript_full.jsonl 中不可压缩的原始长文本所在 step_index，便于用 context_guardian.py step / search 秒级无损调取 -->
| `step_index` | 类型 | 摘要说明（含被 >500B 截断的长用户输入 / 关键报错 / 关键输出） |
|---|---|---|
| `...` | `USER_INPUT` / `GENERIC` | `...` |
