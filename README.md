# Anti-Compaction Amnesia（Antigravity 防有损压缩失忆护盾）

> 面向 Google Antigravity (AGY) 的上下文水位监控、`PreInvocation` 生命周期自动恢复钩子与无损会话日志检索工具包。
>
> **仓库地址**：[`Ironjhin/anti-compaction-amnesia`](https://github.com/Ironjhin/anti-compaction-amnesia)

---

## 1. 项目背景：为什么 Antigravity 会在 ~250k Tokens 压缩后“失忆”？

通过对 Antigravity 本地会话日志（`transcript.jsonl` 与全量无损 `transcript_full.jsonl`）的实测审计，长程任务（跨文件重构、多轮逆向分析、长文档批量修订、持续排错）中的“压缩后失忆”源于以下 4 个机制：

1. **~250k Tokens 阈值触发 `<summary>` 泛化失真**
   当活跃上下文（`input_tokens + cache_read_tokens`）逼近 `250,000 tokens` 时，系统自动插入 `CHECKPOINT` 步骤（`# Resuming from a compaction`），将此前对话压缩为结构化摘要。摘要会将会话细节概括化，导致**精确文件路径、字节偏移、命令参数、性能基线数值、具体报错栈**丢失。
2. **`# User Requests` 历史用户输入硬截断**
   `CHECKPOINT` 步骤附带的 `# User Requests` 列表会对较长的历史用户输入进行硬截断（附上 `<truncated N bytes>` 标记）。若用户在压缩前粘贴了长日志、检测报告、需求文档或代码片段，压缩后模型将无法看到被截断的后半部分内容。
3. **多代重压缩劣化（雪球效应）**
   超长会话可能连续经历数次乃至二十余次 `CHECKPOINT` 压缩（新摘要基于旧摘要再概括）。早期确立的**负面约束（严禁修改的文件/格式红线）**与**已排除死胡同（已验证失败的方案）**在多轮重压缩后极易丢失，导致 Agent 反复重蹈覆辙。
4. **磁盘全量无损日志未被打通**
   Antigravity 在 `<appDataDir>/brain/<conversation-id>/.system_generated/logs/transcript_full.jsonl` 中 **100% 无损保留**了所有历史步骤的完整 `USER_INPUT`、`PLANNER_RESPONSE`（含 `thinking` 与 `tool_calls`）及工具输出；同时每次 `CHECKPOINT` 底部都会自动列出当前会话 Artifact 目录（`<appDataDir>/brain/<conversation-id>/`）下的文件。但默认情况下，模型既不会在压缩前主动将关键约束外置到 Artifact，也不会在压缩后主动按需检索 `transcript_full.jsonl`。

---

## 2. 三道防线架构与仓库目录结构

本项目通过 **Skill 规范约束 + `MEMORY_ANCHOR.md` 结构化记忆锚点 + `PreInvocation` 自动钩子 + 按需无损检索 CLI** 构建三道防线：

- **防线一（日常增量锚定）**：在会话 Artifact 目录下维护 `MEMORY_ANCHOR.md`，固化 5 大核心区块（铁律与负面约束、精确事实与参数账本、已排除死胡同、里程碑与下一步动作、关键 `step_index` 书签）。
- **防线二（185k / 220k 高水位预警封存）**：当上下文跨越 `185,000 tokens`（WARNING）或 `220,000 tokens`（CRITICAL）时，通过 `PreInvocation` 钩子自动注入预警提示，督促 Agent 在触发 ~250k 压缩前更新 `MEMORY_ANCHOR.md`。
- **防线三（`CHECKPOINT` 压缩后自动注入与按需回查）**：检测到新的 `CHECKPOINT` 后，`PreInvocation` 钩子自动提取 `MEMORY_ANCHOR.md`、近期/压缩前完整 `USER_INPUT` 以及压缩前最后一次 `PLANNER_RESPONSE` 快照，通过 `injectSteps` 的 `systemMessage` 无损注入模型上下文，并提供 CLI 工具按需检索 `transcript_full.jsonl`（严禁使用 `read_file` 裸读数 MB 的全量日志）。

### 目录结构

```text
anti-compaction-amnesia/
├── SKILL.md                              # 技能主说明文件（触发条件、三道防线工作流、CLI 速查与硬约束）
├── README.md                             # 项目说明与已知问题概览（本文档）
├── REVIEW_ISSUES.md                      # 完整代码审查报告与待 Carbon 评审的设计问题清单
├── .gitignore                            # Git 忽略规则
├── references/
│   └── memory_anchor_template.md         # MEMORY_ANCHOR.md 五大必填区块标准模板
└── scripts/
    ├── context_guardian.py               # 核心 CLI 工具（status / init-anchor / recover / search / step / hook）
    └── pre_invocation_guard.py           # Antigravity PreInvocation 生命周期自动防护钩子
```

| 文件路径 | 核心职责 |
| :--- | :--- |
| [`SKILL.md`](./SKILL.md) | 定义技能激活条件（出现 `# Resuming from a compaction`、收到高水位预警或用户主动要求防失忆）、三道防线执行步骤及不可违反的硬约束。 |
| [`scripts/context_guardian.py`](./scripts/context_guardian.py) | 纯 Python 标准库实现的 CLI 工具箱：检查上下文 Token 水位、初始化记忆锚点、无损恢复被截断的用户输入、正则/关键词搜索历史步骤、按 `step_index` 精准调阅、管理全局 `hooks.json`。 |
| [`scripts/pre_invocation_guard.py`](./scripts/pre_invocation_guard.py) | 挂载于 `~/.gemini/config/hooks.json` 的 `PreInvocation` 钩子脚本，基于 `.compaction_guard_state.json` 实现边沿触发（高水位预警 + 压缩后自动注入恢复包），任何异常静默输出 `{}` 并以退出码 `0` 放行。 |
| [`references/memory_anchor_template.md`](./references/memory_anchor_template.md) | `MEMORY_ANCHOR.md` 模板，规范化记录负面约束、精确路径/参数、死胡同清单、当前状态与历史 `USER_INPUT` 索引。 |
| [`REVIEW_ISSUES.md`](./REVIEW_ISSUES.md) | 针对当前代码实现（保持原行号不变）的完整静态与实测审查报告，汇总 **5 项 HIGH、10 项 MEDIUM、17 项 LOW/INFO** 问题及复现证据，供 Carbon 深入审查与提出改进建议。 |

---

## 3. 快速安装与 CLI 使用指南

### 3.1 安装到全局 Skills 目录

将本仓库克隆至 Antigravity 全局技能目录 `~/.gemini/config/skills/anti-compaction-amnesia`：

```bash
git clone https://github.com/Ironjhin/anti-compaction-amnesia.git "$HOME/.gemini/config/skills/anti-compaction-amnesia"
```

### 3.2 启用全局 `PreInvocation` 自动护盾钩子

运行以下命令将 `pre_invocation_guard.py` 注册到 `~/.gemini/config/hooks.json`：

```powershell
# 查看钩子状态
python "$HOME/.gemini/config/skills/anti-compaction-amnesia/scripts/context_guardian.py" hook --status

# 开启全局 PreInvocation 自动防失忆护盾
python "$HOME/.gemini/config/skills/anti-compaction-amnesia/scripts/context_guardian.py" hook --enable

# 如需临时关闭
python "$HOME/.gemini/config/skills/anti-compaction-amnesia/scripts/context_guardian.py" hook --disable
```

### 3.3 核心 CLI 命令速查 (`context_guardian.py`)

```powershell
# 1. 查看会话当前 Token 占用、水位等级（SAFE/WATCH/WARNING/CRITICAL）、历史压缩次数与锚点状态
python "$HOME/.gemini/config/skills/anti-compaction-amnesia/scripts/context_guardian.py" status -c "<conversation-id>"

# 2. 在当前会话 Artifact 目录初始化 MEMORY_ANCHOR.md（自动附带历史 USER_INPUT 索引表）
python "$HOME/.gemini/config/skills/anti-compaction-amnesia/scripts/context_guardian.py" init-anchor -c "<conversation-id>"

# 3. 压缩后从 transcript_full.jsonl 无损恢复完整用户输入与压缩前模型快照
python "$HOME/.gemini/config/skills/anti-compaction-amnesia/scripts/context_guardian.py" recover -c "<conversation-id>" --last-inputs 3 --max-chars 8000

# 4. 在 transcript_full.jsonl 中按关键词或正则表达式检索历史细节（支持按 --source 和 --type 过滤）
python "$HOME/.gemini/config/skills/anti-compaction-amnesia/scripts/context_guardian.py" search "<关键词或正则>" -c "<conversation-id>" --max-results 10

# 5. 精确读取单个或区间 step_index 的完整记录（content / thinking / tool_calls）
python "$HOME/.gemini/config/skills/anti-compaction-amnesia/scripts/context_guardian.py" step 440 443 -c "<conversation-id>"
```

---

## 4. 当前存在的问题与待审查清单 (Known Issues for Carbon Review)

> [!IMPORTANT]
> 本仓库当前保留了初版实现的原始代码与行号，并在真实会话日志库（90 个会话 / 180 个 `transcript*.jsonl` 文件，单文件最高 15.8 MB、25 次 `CHECKPOINT`）上完成了全面实测审计。
> 全部 **5 项 HIGH、10 项 MEDIUM、17 项 LOW/INFO（及文档同步项）** 的详细行号、复现证据、根因分析与修复建议均已完整收录于 **[`./REVIEW_ISSUES.md`](./REVIEW_ISSUES.md)**，供 Carbon 审查与给出架构改进建议。

### 4.1 严重问题汇总（HIGH — 共 5 项）

| 编号 | 所属文件与行号 | 问题简述与实测影响 |
| :--- | :--- | :--- |
| **H1** | `scripts/context_guardian.py` (`L190-L207`) | **旧版会话缺失 Token 字段导致误报 `SAFE`**：约 56/90 个较早会话（含 15.8 MB、25 次压缩的重度会话）无 `input_tokens`/`cache_read_tokens` 字段，`status` 输出 `0 / 250,000 (0.0%) [step None] -> SAFE`，在高危会话上给出错误的绿灯信号。 |
| **H2** | `scripts/context_guardian.py` (`L110-L123`) & `SKILL.md` (`L55, L59`) | **`auto` 会话选择在多 Agent/Swarm/多窗口下串台**：未传 `-c` 时默认按 `transcript.jsonl` 最新 `mtime` 选择会话。并发子代理或 Swarm 场景下，`init-anchor` 和 `recover` 极易读写到错误的子代理会话目录。 |
| **H3** | `scripts/context_guardian.py` (`L26, L259`) & `SKILL.md` (`L23`) | **`CHECKPOINT_USER_TRUNCATE_BYTES = 500` 与真实截断阈值不符且误标未压缩输入**：实测真实 `CHECKPOINT` 中 `<truncated N bytes>` 保留前缀为 `1021 B`（10 例）或 `~1392 B`，而非 `500 B`；且代码仅凭字节长度打标，即使输入发生在最后一次压缩之后（或全会话无压缩）也会被误标为 `[TRUNCATED BY CHECKPOINT]`。 |
| **HIGH-1** | `scripts/pre_invocation_guard.py` (`L91-L97, L361-L366`) | **首次启用或状态文件缺失时将历史旧 `CHECKPOINT` 误判为新压缩（已复现）**：当 `.compaction_guard_state.json` 不存在或损坏重置时，末尾 256 KB 内的任何历史 `CHECKPOINT`（如距今已过去 250 步的旧压缩）都会在首次调用时触发 2.8 KB 的“刚刚触发压缩”恢复注入。此外未过滤被回滚（rewound）覆盖的失效 `CHECKPOINT`。 |
| **HIGH-2** | `scripts/pre_invocation_guard.py` (`L372, L396`) | **状态持久化早于输出构造与落盘，异常/超时导致恢复机会永久丢失**：`save_state()` 在 `build_post_compaction_output()` / `build_watermark_output()` 及 `stdout` 写入之前执行；一旦后续读取大文件超时（10s）或抛错回退到 `{}`，状态已标记为“已处理”，该次压缩恢复将永远不再触发。 |

### 4.2 中等问题汇总（MEDIUM — 共 10 项）

| 编号 | 所属文件与行号 | 问题简述 |
| :--- | :--- | :--- |
| **M1** | `scripts/context_guardian.py` (`L202, L243, L274, L481-L486`) | **重试/回滚产生重复 `step_index`**：回滚后同一 `step_index` 可能由 `CHECKPOINT` 变为 `PLANNER_RESPONSE`；`step` 静默覆盖旧记录、`status`/`recover` 计入已回滚的 `CHECKPOINT`、`collect_user_inputs` 重复列出重试的 `USER_INPUT`。 |
| **M2** | `scripts/context_guardian.py` (`L199-L201`) | **刚完成 `CHECKPOINT` 但尚无新 `PLANNER_RESPONSE` 时水位显示滞后**：`status` 仍显示压缩前的高水位（`CRITICAL`），未提示“刚刚完成压缩，等待下一轮响应”。 |
| **M3** | `scripts/context_guardian.py` (`L397-L398`) | **`search` 对 `tool_calls` 参数二次 JSON 转义导致路径检索失效**：`args` 内部值常为序列化 JSON 字符串，`json.dumps` 二次转义后 Windows 路径反斜杠翻倍，直接搜索 `C:\Users\...` 命中数为 0。 |
| **M4** | `scripts/context_guardian.py` (`L420-L423`) | **`search` 正则模式静默误读反斜杠转义**：搜索含 `\t`、`\n` 等路径（如 `C:\temp\new`）时是合法正则，不会触发 `re.error` 字面量回退，导致静默返回 0 条结果。 |
| **M5** | `scripts/context_guardian.py` (`L298-L308, L533-L538`) | **`init-anchor` 与 `recover` 的用户输入索引表无上限**：在 300+ 轮对话中生成 31 KB 的 `MEMORY_ANCHOR.md` 和 36 KB 的 `recover` 输出，违背 `< 4 KB` 精炼锚点原则并加剧上下文膨胀。 |
| **M6** | `scripts/context_guardian.py` (`L564, L611, L623`) | **`hooks.json` 读写脆弱性**：未用 `utf-8-sig` 兼容 Windows PowerShell 写入的 UTF-8 BOM；直接覆盖写无 `.tmp` 原子替换与 `.bak` 备份；配置项非 `dict` 时抛异常崩溃。 |
| **M7** | `scripts/context_guardian.py` (`L575-L583`) | **`build_hook_spec` 命令构造不够健壮**：使用裸 `python` 且仅在路径含空格时加引号，遇到 `&^()%!` 等 `cmd.exe` 元字符或解释器别名时易失效，且启用前未校验钩子脚本是否存在。 |
| **M8** | `scripts/context_guardian.py` (`L29`) | **`DEFAULT_BRAIN_ROOT` 硬编码为 `antigravity/brain`**：未自动探测 CLI（`antigravity-cli/brain`）与 IDE（`antigravity-ide/brain`）目录，且 CLI 未暴露 `--brain-root` 参数。 |
| **MED-3** | `scripts/pre_invocation_guard.py` (`L57`) | **`read_tail_records` 使用 `str.splitlines()` 导致 JSONL 记录被腰斩（已复现）**：`splitlines()` 会按 JSON 未转义的 `U+0085` (NEL)、`U+2028`、`U+2029` 切行；真实日志中存在原始 `U+0085` 字节（`C2 85`），导致含该字符的 `USER_INPUT` 或 `CHECKPOINT` 解析失败被静默丢弃。 |
| **MED-5** | `hooks.json` (`L7`) & `scripts/context_guardian.py` (`L582`) | **Windows `cmd /c` 下裸调 `python` 的环境与编码风险**：依赖宿主进程 `PATH` 顺序（可能命中 Microsoft Store 空桩或非预期 Python 环境），且未显式传入 `-X utf8` 或固定解释器绝对路径。 |

### 4.3 低风险与信息项汇总（LOW / INFO — 共 17 项 + 文档同步项）

- **`context_guardian.py`（11 项：`L1`–`L11`）**：
  - **L1** (`L230`)：`records_since` 将有效记录数 `len(records)` 与文件物理行号 `last_line` 混减。
  - **L2** (`L142`)：`iter_jsonl_records` 静默跳过损坏 JSON 行但未在 `status` 中统计报告。
  - **L3** (`L178, L266`)：当 `transcript_full.jsonl` 缺失回退到有损 `transcript.jsonl` 时未打印警告。
  - **L4** (`L387-L399, L509, L657`)：`--type` 缺少 `ERROR_MESSAGE` 选项，`search` 与 `step` 未检索/展示 `error` 字段。
  - **L5** (`L653`)：`search` 搜索以 `-` 开头的关键词（如 `-rf`）时被 `argparse` 误判为选项。
  - **L6** (`L520, L555`)：`init-anchor --force` 直接覆盖已有锚点文件，无 `.bak` 备份与原子写入。
  - **L7** (`L94-L97`)：`resolve_brain_dir` 未校验 `conversation_id` 格式，传入 `..` 可越界访问父目录。
  - **L8** (`L350-L354, L366-L373`)：`recover` 模型快照硬编码 600/800 字符截断而忽略 `--max-chars`，无压缩分支仅展示 `content`。
  - **L9** (`L470-L515`)：`step` 默认 `--max-chars 0`（不截断），大范围查询多步时会向终端倾倒数 MB 内容。
  - **L10** (`L592-L628`)：`hook` 子命令未设互斥参数组与默认动作，且 `--enable` 会覆盖用户自定义的 `timeout`。
  - **L11** (`L250, L555`)：`.encode("utf-8")` 与文件写入未指定 `errors="replace"`，遇到孤立代理项（surrogates）存在编码异常风险。
- **`pre_invocation_guard.py` 与 `SKILL.md`（6 项 LOW/INFO + 文档同步项）**：
  - **LOW-4** (`pre_invocation_guard.py:L58-L60`)：当 `start = size - tail_bytes` 恰好落在行首边界时，`lines[1:]` 会无条件丢弃第一条完整记录。
  - **LOW-6** (`pre_invocation_guard.py:L426`)：钩子标准输出 `json.dumps` 使用 `ensure_ascii=False`，在非 UTF-8 管道下不如 `ensure_ascii=True` 稳健。
  - **LOW-7** (`pre_invocation_guard.py:L395-L403`)：90 秒 `MEMORY_ANCHOR.md` 免打扰窗口检查位于更新 `last_warned_band` 之后，导致高水位预警被静默吞掉且不再重发。
  - **LOW-8** (`pre_invocation_guard.py:L388-L391`)：缺少向下重置迟滞区间（hysteresis），且当末尾无 Token 记录（`latest_tokens == 0`）时会错误重置 `last_warned_band = 0`。
  - **LOW-9** (`pre_invocation_guard.py:L126-L129, L189, L374-L376`)：当 `CHECKPOINT` 超出 `transcript_full.jsonl` 末尾 4 MB 窗口时，`ckpt_pos = -1` 会把压缩后的最新 `PLANNER_RESPONSE` 误标为“压缩前快照”；回退到 `transcript.jsonl` 时仍声称“无损恢复”。
  - **LOW-10 & LOW-11** (`pre_invocation_guard.py:L110, L349-L352`)：状态文件写入非原子操作；同时 `transcript.jsonl` 中的 Token 计数反映上一轮模型调用，滞后于本轮新产生的工具输出。
  - **INFO-12 & `SKILL.md` (`L108, L110, L124`)**：需实测确认宿主引擎生成 `CHECKPOINT` 与触发 `PreInvocation` 的先后时序；`SKILL.md` 中关于“只扫描末尾 ≤256 KB”及状态文件路径的表述需与实际实现对齐。

👉 **完整审查细节、复现脚本结果与待讨论架构议题请参阅 [`REVIEW_ISSUES.md`](./REVIEW_ISSUES.md)。**
