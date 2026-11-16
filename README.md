# Anti-Compaction Amnesia（Antigravity 防有损压缩失忆护盾）

> 面向 Google Antigravity (AGY) 的上下文水位监控、`PreInvocation` 生命周期自动恢复钩子与无损会话日志检索工具包。
>
> **仓库地址**：[`Ironjhin/anti-compaction-amnesia`](https://github.com/Ironjhin/anti-compaction-amnesia)

---

## 1. 项目背景：为什么 Antigravity 会在 ~512k Tokens 压缩后“失忆”？

通过对 Antigravity 本地会话日志（`transcript.jsonl` 与全量无损 `transcript_full.jsonl`）的实测审计，长程任务（跨文件重构、多轮逆向分析、长文档批量修订、持续排错）中的“压缩后失忆”源于以下 4 个机制：

1. **~512k Tokens 阈值触发 `<summary>` 泛化失真**
   当活跃上下文（`input_tokens + cache_read_tokens`）逼近 `512,000 tokens` 时，系统自动插入 `CHECKPOINT` 步骤（`# Resuming from a compaction`），将此前对话压缩为结构化摘要。摘要会将会话细节概括化，导致**精确文件路径、字节偏移、命令参数、性能基线数值、具体报错栈**丢失。
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
- **防线二（380k / 450k 高水位预警封存）**：当上下文跨越 `380,000 tokens`（WARNING）或 `450,000 tokens`（CRITICAL）时，通过 `PreInvocation` 钩子自动注入预警提示，督促 Agent 在触发 ~512k 压缩前更新 `MEMORY_ANCHOR.md`。
- **防线三（`CHECKPOINT` 压缩后自动注入与按需回查）**：检测到新的 `CHECKPOINT` 后，`PreInvocation` 钩子自动提取 `MEMORY_ANCHOR.md`、近期/压缩前完整 `USER_INPUT` 以及压缩前最后一次 `PLANNER_RESPONSE` 快照，通过 `injectSteps` 的 `systemMessage` 无损注入模型上下文，并提供 CLI 工具按需检索 `transcript_full.jsonl`（严禁使用 `read_file` 裸读数 MB 的全量日志）。

### 目录结构

```text
anti-compaction-amnesia/
├── SKILL.md                              # 技能主说明文件（触发条件、三道防线工作流、CLI 速查与硬约束）
├── README.md                             # 项目说明与修复概览（本文档）
├── REVIEW_ISSUES.md                      # 完整代码审查报告、问题状态（✅ 35/35 已解决）与架构总结
├── .gitignore                            # Git 忽略规则
├── references/
│   └── memory_anchor_template.md         # MEMORY_ANCHOR.md 五大必填区块标准模板
├── scripts/
│   ├── context_guardian.py               # 核心 CLI 工具（status / init-anchor / recover / search / step / hook）
│   └── pre_invocation_guard.py           # Antigravity PreInvocation 生命周期自动防护钩子
└── tests/
    └── test_fixes.py                     # 覆盖核心缺陷与重构逻辑的单元/集成回归测试
```

| 文件路径 | 核心职责 |
| :--- | :--- |
| [`SKILL.md`](./SKILL.md) | 定义技能激活条件（出现 `# Resuming from a compaction`、收到高水位预警或用户主动要求防失忆）、三道防线执行步骤及不可违反的硬约束。 |
| [`scripts/context_guardian.py`](./scripts/context_guardian.py) | 纯 Python 标准库实现的 CLI 工具箱：检查上下文 Token 水位、初始化记忆锚点、无损恢复被截断的用户输入、正则/关键词搜索历史步骤、按 `step_index` 精准调阅、管理全局 `hooks.json`。 |
| [`scripts/pre_invocation_guard.py`](./scripts/pre_invocation_guard.py) | 挂载于 `~/.gemini/config/hooks.json` 的 `PreInvocation` 钩子脚本，基于 `.compaction_guard_state.json` 实现边沿触发（高水位预警 + 压缩后自动注入恢复包），任何异常静默输出 `{}` 并以退出码 `0` 放行。 |
| [`references/memory_anchor_template.md`](./references/memory_anchor_template.md) | `MEMORY_ANCHOR.md` 模板，规范化记录负面约束、精确路径/参数、死胡同清单、当前状态与历史 `USER_INPUT` 索引。 |
| [`REVIEW_ISSUES.md`](./REVIEW_ISSUES.md) | 完整静态与实测审查报告，汇总 **5 项 HIGH、10 项 MEDIUM、20 项 LOW/INFO/文档** 问题（已逐条标注 `✅ 已解决`，共 **35/35**）与第 5 节修复总结。 |
| [`tests/test_fixes.py`](./tests/test_fixes.py) | 针对已修复缺陷的单元与集成回归测试套件。 |

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

## 4. 已知问题与修复进展 (Review Issues & Resolutions)

> [!NOTE]
> 在真实会话日志库（90 个会话 / 180 个 `transcript*.jsonl` 文件，单文件最高 15.8 MB、25 次 `CHECKPOINT`）上进行了全面实测审计，共发现 **5 项 HIGH、10 项 MEDIUM、17 项 LOW/INFO** 问题。
> **当前版本已全量完成上述问题的修复与架构重构**，详细的根因分析、复现证据与修复实现总结已完整收录于 **[`./REVIEW_ISSUES.md`](./REVIEW_ISSUES.md)**。

### 4.1 核心修复与改进清单（HIGH & MEDIUM）

| 编号 | 涉及模块 | 问题与修复方案 | 状态 |
| :--- | :--- | :--- | :--- |
| **H1** | `scripts/context_guardian.py` | **缺失 Token 字段会话水位估算**：引入按末次压缩以来累积字节数估算活跃上下文的算法（`1 token ≈ 3.5 bytes`），消除虚假 `SAFE`。 | ✅ 已修复 |
| **H2 / L7** | `scripts/context_guardian.py` | **多代理会话防串台与路径穿越校验**：严格校验会话 ID 格式防穿越；120s 内多会话活跃时预警，写操作强制要求指定 `-c`。 | ✅ 已修复 |
| **H3** | `scripts/context_guardian.py` | **真实截断阈值 (~1000B) 与状态精确分类**：将阈值修正为 1000B；严格区分压缩前已截断（`[TRUNCATED BY CHECKPOINT]`）与压缩后待截断风险（`[>1KB AT-RISK]`）。 | ✅ 已修复 |
| **HIGH-1** | `scripts/pre_invocation_guard.py` | **首次启用/状态丢失旧检查点误报消除**：引入 `post_ckpt_planners` 判定，仅在压缩刚触发（响应轮次 ≤ 1）时注入恢复包；自动过滤已被回退的失效检查点。 | ✅ 已修复 |
| **HIGH-2** | `scripts/pre_invocation_guard.py` | **状态持久化时序重构**：先完成恢复输出构造并向 `stdout` 刷写成功，后持久化状态，杜绝异常丢失恢复机会。 | ✅ 已修复 |
| **M1** | `scripts/context_guardian.py` | **主干分支统一规范化**：反向扫描标记 `_superseded` 步骤，区分活跃/回退检查点，防止旧步骤吞掉当前记录。 | ✅ 已修复 |
| **M2** | `scripts/context_guardian.py` | **压缩刚完成瞬时状态提示**：显式展示 `POST-COMPACTION` 状态，提示等待第一轮模型恢复响应。 | ✅ 已修复 |
| **M3 / M4** | `scripts/context_guardian.py` | **Windows 反斜杠与嵌套 JSON 检索修复**：解包嵌套 JSON 字符串消除重复转义；增加 `-F/--literal` 并在无正则命中时自动字面量回退。 | ✅ 已修复 |
| **M5** | `scripts/context_guardian.py` | **记忆锚点体积控制**：限制自动生成索引为截断/高风险输入+最近 10 条（最多 25 行），锚点严格控制在 < 4 KB。 | ✅ 已修复 |
| **M6 / LOW-10** | 核心脚本 | **配置文件读写原子性与 UTF-8 BOM 兼容**：支持 `utf-8-sig`，写入采用 `.tmp` + `os.replace` 原子替换，备份 `.bak`。 | ✅ 已修复 |
| **M7 / MED-5** | `scripts/context_guardian.py` | **解释器路径与编码参数健壮性**：使用当前 Python 解释器绝对路径并显式附加 `-X utf8`。 | ✅ 已修复 |
| **M8** | `scripts/context_guardian.py` | **多宿主 Brain 根目录自动探测**：支持 `$AGY_BRAIN_ROOT`、命令行 `--brain-root` 以及 CLI/IDE 目录自动探测。 | ✅ 已修复 |
| **MED-3 / LOW-4** | `scripts/pre_invocation_guard.py` | **二进制安全切行与边界读取**：严格按 `b"\n"` 分割防 `U+0085` (NEL) 误断行，边界多读 1 字节准确丢弃残行。 | ✅ 已修复 |

### 4.2 低风险与信息项汇总（LOW / INFO — 共 17 项 + 文档同步项，✅ 全部已解决）

- **`context_guardian.py`（11 项：`L1`–`L11`）**：
  - ✅ **L1** (`L230`)：`records_since` 改为基于有效记录列表下标差计算，不再混用物理行号。
  - ✅ **L2** (`L142`)：`iter_jsonl_records` 统计损坏/空行跳过数量并在 `status` 中报告。
  - ✅ **L3** (`L178, L266`)：缺失 `transcript_full.jsonl` 回退到有损 `transcript.jsonl` 时向 `stderr` 打印警告。
  - ✅ **L4** (`L387-L399, L509, L657`)：`--type` 已支持 `ERROR_MESSAGE`，`search` 与 `step` 已检索并展示 `error` 字段。
  - ✅ **L5** (`L653`)：`search` 帮助文案与 `SKILL.md` 已补充 `--` 分隔符用法说明（`search -- "-rf"`）。
  - ✅ **L6** (`L520, L555`)：`init-anchor --force` 覆盖前自动创建 `.bak` 备份并使用 `.tmp` + `os.replace` 原子写入。
  - ✅ **L7** (`L94-L97`)：`resolve_brain_dir` 已校验 `conversation_id` 格式并禁止路径穿越。
  - ✅ **L8** (`L350-L354, L366-L373`)：`recover` 第 4 节尊重 `--max-chars`，无压缩分支同样展示 `thinking` 与 `tool_calls`。
  - ✅ **L9** (`L470-L515`)：`step` 查询超过 3 步且未显式限制时默认应用每字段 4,000 字符安全上限（`--max-chars -1` 可取消）。
  - ✅ **L10** (`L592-L628`)：`hook` 已改为互斥参数组并默认 `--status`，保留用户自定义 `timeout` 等字段。
  - ✅ **L11** (`L250, L555`)：UTF-8 编解码与文件写入已统一指定 `errors="replace"`。
- **`pre_invocation_guard.py` 与 `SKILL.md`（6 项 LOW/INFO + 文档同步项）**：
  - ✅ **LOW-4** (`pre_invocation_guard.py`)：从文件末尾 seek 时多回退 1 字节并精确跳过首个 `b"\n"`，消除边界整行丢失。
  - ✅ **LOW-6** (`pre_invocation_guard.py`)：`main()` 改为 `json.dumps(..., ensure_ascii=True)` 输出纯 ASCII JSON，免疫代码页乱码。
  - ✅ **LOW-7** (`pre_invocation_guard.py`)：免打扰窗口检查移至更新 `last_warned_band` 之前，且 CRITICAL 级缩短至 30 秒。
  - ✅ **LOW-8** (`pre_invocation_guard.py`)：仅在检测到有效 token（`latest_tokens > 0`）且水位回落时才重置 `last_warned_band`。
  - ✅ **LOW-9** (`pre_invocation_guard.py`)：`ckpt_pos < 0` 时不再误将压缩后回复当成压缩前快照，有损回退已显式标注。
  - ✅ **LOW-10 & LOW-11** (`pre_invocation_guard.py` & `SKILL.md`)：状态文件改为 `.tmp` + `os.replace` 原子写入，并在文档补充滞后说明。
  - ✅ **INFO-12 & `SKILL.md`** (`SKILL.md` & `README.md`)：`SKILL.md` 已同步真实截断阈值、尾部扫描与按需读全量规则以及真实状态文件路径。

👉 **完整审查细节、逐条解决状态与第 5 节修复总结请参阅 [`REVIEW_ISSUES.md`](./REVIEW_ISSUES.md)。**
