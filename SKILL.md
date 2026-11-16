---
name: anti-compaction-amnesia
description: >-
  防止 Antigravity 在上下文满（~512k tokens）触发有损压缩（CHECKPOINT / # Resuming from a compaction）后出现失忆、违背早期约束、重复已排除死胡同或丢失压缩前长输入。使用场景：长会话/多轮调试中需要防爆仓与持久化核心记忆、用户执行 /compact 前、收到 PreInvocation 高水位预警（≥380k tokens）、用户要求“记住/别忘了/防压缩失忆/保存进度”，或上下文中刚出现 `# Resuming from a compaction` 时立即触发。
---

# Anti-Compaction Amnesia（防有损压缩失忆协议）

## When to use

满足以下任一条件时**立即激活**本技能：
1. **恢复触发（最高优先级）**：上下文中出现 `# Resuming from a compaction`（即 Antigravity 触发了 `CHECKPOINT` 有损压缩），或收到 `[ANTI-COMPACTION GUARD]` 自动注入提示。
2. **预警触发**：收到 `[CONTEXT WATERMARK WARNING / CRITICAL]`（上下文达到 ≥380k / ≥450k tokens），或自感对话已很长、准备运行 `/compact`。
3. **主动触发**：用户要求“防止失忆”“保存当前记忆锚点”“总结别丢约束”“查一下前面贴过的完整内容”，或启动复杂长程任务（跨文件重构、多轮逆向、长文档批量修订、持续排错）需要防爆仓时。

---

## 核心认知：Antigravity 为什么会在有损压缩后“失忆”

通过审计本地会话日志（`transcript.jsonl` / `transcript_full.jsonl`），失忆源于 4 个明确机制：

1. **512k 阈值 `<summary>` 泛化丢失精确度**：当活跃上下文（`input_tokens + cache_read_tokens`）逼近 ~512,000 tokens 时，系统插入 `CHECKPOINT` 步骤，把历史压成 7 段式摘要。摘要倾向概括成“修改了若干段落”“尝试过几个脚本”，导致**绝对路径、字节偏移、精确指标、具体报错栈、段落编号**失真。
2. **`# User Requests` 约 1000 字节硬截断**：`CHECKPOINT` 里的历史用户请求每条在 ~1000 字节（实测 1,021–1,392 UTF-8 字节）处被强行截断（`<truncated N bytes>`）。用户若在压缩前贴了长日志、检测报告、需求文档或代码片段，压缩后模型完全看不到后半段，极易“刚发完就忘”。
3. **多代重压缩劣化（雪球效应）**：超长会话会连续触发多次 `CHECKPOINT`（摘要再总结旧摘要），早期确立的**负面约束（不要改什么）**与**已排除死胡同（某条路走不通）**在第二、三次压缩后几乎彻底抹除，导致模型反复踩同一个坑。
4. **磁盘无损全量日志闲置**：每次会话在 `<appDataDir>/brain/<conversation-id>/.system_generated/logs/transcript_full.jsonl` 都会**100% 无损保留**所有完整用户输入、思考与工具调用；Artifact 目录（`<appDataDir>/brain/<conversation-id>/`）下的文件也会在每次 `CHECKPOINT` 底部自动列出路径——但若没有显式固化成锚点文件与专用检索工具，压缩后的模型不会主动去查。

---

## 三道防线工作流

### 防线一：日常防爆仓与 `MEMORY_ANCHOR.md` 增量锚定

在长任务中，把**“易失真但绝对不能忘”的信息**外置到当前会话的 Artifact 目录：
`<appDataDir>/brain/<conversation-id>/MEMORY_ANCHOR.md`

- **5 大必填区块**（模板见 [memory_anchor_template.md](./references/memory_anchor_template.md)）：
  1. **铁律与负面约束**：用户明确禁止事项、风格/格式红线、不可覆盖的原文件。
  2. **精确事实与参数账本**：精确绝对路径、变量名、端口/IP/偏移、验收指标、基线数值。
  3. **已排除死胡同**：尝试过且失败的方案 + 失败原因 + ❌ 严禁重试。
  4. **里程碑与当前精确状态**：已完成 / 待完成 / 下一步唯一精确动作。
  5. **关键历史步骤索引**：`transcript_full.jsonl` 中重要长文本所在的 `step_index`。
- **控制主上下文体积，延迟触发压缩**：
  - 读大文件必须用 `StartLine` / `EndLine` 局部读取，先用 `rg` 定位；超过 ~200 行禁止整文件裸读。
  - 宽泛的代码库/日志排查优先委派给 `research` 子代理，主上下文只接收提炼结论。
  - 长数据、中间产物优先写入 `scratch/` 或 Artifact，不把大段原始输出反复贴在对话里。

---

### 防线二：逼近红线（≥380k Tokens）或 `/compact` 前的无损封存

当收到高水位预警，或准备运行 `/compact` 时，按以下步骤执行（**注意**：在多代理并发/Swarm 模式下，务必显式传入 `-c "<conversation-id>"` 防止串台）：

1. **检查当前水位与锚点状态**：
   ```powershell
   python "$HOME/.gemini/config/skills/anti-compaction-amnesia/scripts/context_guardian.py" status -c "<conversation-id>"
   ```
2. **初始化 `MEMORY_ANCHOR.md`（若尚未创建）**：
   ```powershell
   python "$HOME/.gemini/config/skills/anti-compaction-amnesia/scripts/context_guardian.py" init-anchor -c "<conversation-id>"
   ```
3. **补全并验证 5 大区块**：
   - 使用 `read_file` / `edit_file` 更新 `<appDataDir>/brain/<conversation-id>/MEMORY_ANCHOR.md`。
   - 确认最近 1–2 轮用户贴过的长内容（>1000 字节）已将其核心参数和 `step_index` 记入第 2、5 节。
4. **Done when**：`MEMORY_ANCHOR.md` 存在且包含最新铁律、精确参数、死胡同清单与下一步动作，之后再继续执行或触发 `/compact`。

---

### 防线三：出现 `# Resuming from a compaction` 后的强制无损恢复协议

一旦在上下文中看到 `# Resuming from a compaction`：

1. **严禁直接按 `<summary>` 盲干或直接向用户复述/索要刚贴过的内容**。默认认为 `<summary>` 中的细节已被有损泛化，且 `# User Requests` 中所有标注 `<truncated ...>` 的输入都已丢掉后半段。
2. **优先读取自动护盾与 `MEMORY_ANCHOR.md`**：
   - 若已启用全局 `PreInvocation` 护盾，检查本轮注入的 `[ANTI-COMPACTION GUARD]` 内容。
   - 用 `read_file` 读取 `<appDataDir>/brain/<conversation-id>/MEMORY_ANCHOR.md`（若文件存在）。
3. **运行 `recover` 无损恢复被截断的用户输入与压缩前现场**：
   ```powershell
   python "$HOME/.gemini/config/skills/anti-compaction-amnesia/scripts/context_guardian.py" recover -c "<conversation-id>"
   ```
   （不传 `-c` 时默认自动选择最新修改的会话）。
4. **按需精准回查早期细节**：
   - 某条路径、报错、参数或早期规则记不清时，用 `search` 或 `step` 从 `transcript_full.jsonl` 精准提取对应片段：
   ```powershell
   python "$HOME/.gemini/config/skills/anti-compaction-amnesia/scripts/context_guardian.py" search "<关键词或正则>" -c "<conversation-id>"
   python "$HOME/.gemini/config/skills/anti-compaction-amnesia/scripts/context_guardian.py" step <step_index> -c "<conversation-id>"
   ```
5. **回补记忆锚点**：若发现 `<summary>` 里遗漏了刚从 `recover`/`search` 找回的约束或死胡同，顺手更新到 `MEMORY_ANCHOR.md` 后再继续原任务。

---

## CLI 工具箱速查 (`context_guardian.py`)

脚本路径：[context_guardian.py](./scripts/context_guardian.py)（纯 Python 标准库，UTF-8 安全，Windows/macOS/Linux 通用）。

| 子命令 | 用途 | 常用命令示例 |
|---|---|---|
| `status` | 查看当前 token 占用、水位等级（SAFE/WATCH/WARNING/CRITICAL）、历史压缩次数、`MEMORY_ANCHOR.md` 状态 | `python ".../context_guardian.py" status` |
| `init-anchor` | 在当前会话 Artifact 目录生成 `MEMORY_ANCHOR.md` 并自动附带历史 `USER_INPUT` 索引表 | `python ".../context_guardian.py" init-anchor` |
| `recover` | 从 `transcript_full.jsonl` 无损恢复所有用户输入索引（标出 `[TRUNCATED BY CHECKPOINT]`）、压缩前完整用户输入及最后模型快照 | `python ".../context_guardian.py" recover --last-inputs 3 --max-chars 8000` |
| `search` | 按正则/关键词检索 `transcript_full.jsonl`（支持 `--source`、`--type` 过滤） | `python ".../context_guardian.py" search "报错关键词" --type GENERIC --max-results 10` |
| `step` | 精准读取单个或一段 `step_index` 的完整 `content` / `thinking` / `tool_calls` | `python ".../context_guardian.py" step 440 443` |
| `hook` | 检查、开启或关闭 `~/.gemini/config/hooks.json` 中的全局 `PreInvocation` 防失忆护盾 | `python ".../context_guardian.py" hook --status` / `--enable` / `--disable` |

---

## 自动护盾机制说明 (`pre_invocation_guard.py`)

配套钩子脚本 [pre_invocation_guard.py](./scripts/pre_invocation_guard.py) 挂载于 `~/.gemini/config/hooks.json` 的 `PreInvocation` 阶段（边沿触发，日常只扫描末尾 ≤256 KB，零刷屏、异常静默放行 `{}`）：
- **水位跨越提醒**：当活跃 token 首次跨越 **380,000 (74%)** 或 **450,000 (88%)** 时，自动注入系统提示要求立即刷新 `MEMORY_ANCHOR.md`（若 90 秒内已更新则跳过；CRITICAL 级别免打扰窗口缩减为 30 秒，避免漏报）。若会话日志无 token 遥测字段，CLI 会自动根据末次压缩以来的累积字节数估算活跃水位。
- **压缩后秒级恢复**：一旦检测到新触发的活跃 `CHECKPOINT`（自动忽略已被回退的旧检查点或陈旧历史），钩子会自动读取 `MEMORY_ANCHOR.md`，并在按需加载 `transcript_full.jsonl`（小文件全量读，大文件窗口读）后提取被截断的近期/压缩前用户输入与模型执行快照，通过 `systemMessage` 无损注入上下文。

---

## 硬约束（不可违反）

1. **禁止裸读整个 `transcript_full.jsonl`**：长会话的 `transcript_full.jsonl` 可达数 MB 甚至十几 MB，直接用 `read_file` 整文件读取会立刻撑爆当前上下文并引发连环压缩！必须通过 `context_guardian.py recover / search / step` 按需提取。
2. **禁止压缩后向用户重复索要已发过的长输入**：只要该输入在 `CHECKPOINT` 之前发过，`transcript_full.jsonl` 中必有原文，先用 `recover` 取回。
3. **禁止编造被压缩摘要丢失的细节**：遇到模糊路径、版本号、指标或用户要求时，必须通过 `MEMORY_ANCHOR.md` 或 `context_guardian.py search/step` 核实后再行动。

## 不要做的事

- 不要在每次普通工具调用都重写 `MEMORY_ANCHOR.md`（仅在确立新约束、踩到新死胡同、完成里程碑、或水位 ≥380k 时更新）。
- 不要把大段代码或整篇原始日志直接粘贴进 `MEMORY_ANCHOR.md`；用“简明事实 + `step_index` 书签”保持锚点精炼（建议 < 4 KB）。
- 不要手动修改 `<artifactDir>/scratch/.compaction_guard_state.json` 状态文件来绕过边沿检测；如需关闭自动提示，使用 `context_guardian.py hook --disable`。
