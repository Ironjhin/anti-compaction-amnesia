# Anti-Compaction Amnesia 代码审查与现存问题清单 (For Carbon Review)

> **文档目的**：本文档旨在为 **Carbon** 提供一份完整、精确、带实测复现证据的代码审查报告与架构讨论清单。  
> 为保证文档中引用的**所有文件名与行号（Line Numbers）与仓库源码 100% 严格对应**，当前仓库中的核心代码（`scripts/context_guardian.py`、`scripts/pre_invocation_guard.py`、`references/memory_anchor_template.md`、`SKILL.md`）**保持审查时的原始状态未作修改**。请 Carbon 基于本文档列出的数据、根因分析与开放架构问题给出改进建议或直接提交修复方案。

---

## 1. 代码库概览与审查测试基线

### 1.1 核心文件结构

| 文件路径 | 行数 | 职责说明 |
|---|---|---|
| [`scripts/context_guardian.py`](./scripts/context_guardian.py) | 699 行 | CLI 工具箱，提供 `status`（水位与压缩统计）、`recover`（从 `transcript_full.jsonl` 恢复完整输入与压缩前快照）、`search`（正则/关键词检索）、`step`（按 `step_index` 调取单步或区间记录）、`init-anchor`（初始化 `MEMORY_ANCHOR.md`）与 `hook`（管理全局 `hooks.json`）。 |
| [`scripts/pre_invocation_guard.py`](./scripts/pre_invocation_guard.py) | 434 行 | 挂载于 `$HOME/.gemini/config/hooks.json` 的 `PreInvocation` 生命周期钩子。扫描 `transcript.jsonl` 末尾（256 KB），在跨越 185k / 220k token 水位或检测到新 `CHECKPOINT` 时通过 `injectSteps.systemMessage` 自动注入预警或恢复包。 |
| [`SKILL.md`](./SKILL.md) | 124 行 | 技能主指令文档，定义触发条件、Antigravity 压缩失忆的 4 个机制、三道防线工作流与硬约束。 |
| [`references/memory_anchor_template.md`](./references/memory_anchor_template.md) | 50 行 | `MEMORY_ANCHOR.md` 的五段式结构化模板（铁律与负面约束、精确事实与参数账本、已排除死胡同、里程碑与当前状态、关键历史步骤索引）。 |

### 1.2 实测审查基线（真实会话日志样本库）

本次审查在只读条件下对本地 **90 个真实会话（共 180 个 `transcript.jsonl` / `transcript_full.jsonl` 文件，总计 65,662 条步骤记录，最大单文件 15.77 MB / 11,769 条记录）** 进行了全量静态与动态实测。

### 1.3 问题统计总览

| 模块 | HIGH (高危) | MEDIUM (中危) | LOW / INFO (低危与说明) | 合计 |
|---|---|---|---|---|
| `scripts/context_guardian.py` (`@guardian-rev`) | 3 (`H1`–`H3`) | 8 (`M1`–`M8`) | 11 (`L1`–`L11`) | **22** |
| `scripts/pre_invocation_guard.py` & `SKILL.md` (`@hook-rev`) | 2 (`HIGH-1`–`HIGH-2`) | 2 (`MED-3`, `MED-5`) | 9 (`LOW-4`, `LOW-6`–`LOW-11`, `INFO-12`, `SKILL.md`) | **13** |
| **总计** | **5** | **10** | **20** | **35** |

---

## 2. `scripts/context_guardian.py` 审查发现（`@guardian-rev`）

### 2.1 HIGH（高危问题）

#### `H1`：`cmd_status` 在不含 token 字段的旧版/特定会话日志上误报 `0 / 250,000 (0.0%) -> SAFE`
- **位置**：[`scripts/context_guardian.py` L149–L152, L190–L207, L221–L225](./scripts/context_guardian.py#L190-L225)
- **复现与实测证据**：
  - 扫描本地全部 90 个真实会话发现，仅 **34 个较新会话**包含 `input_tokens` / `cache_read_tokens` / `output_tokens` 字段（共 2,443 条记录带 token 字段）；其余 **56 个会话完全没有写入 token 字段**。
  - 在体量最大、风险最高的真实会话 `5f9d5f3e`（`transcript_full.jsonl` 达 **15.77 MB**，共 **11,769 条记录**，历史上已触发 **25 次 `CHECKPOINT` 压缩**）上运行 `status`，输出为：
    ```text
    Transcript Size      : 13.48 MB (compact) / 15.77 MB (full)
    Total Steps          : 11769 records (max step_index=11790)
    Current Context      : 0 / 250,000 tokens (0.0%) [step None] -> SAFE (< 150k)
    Peak Context Tokens  : 0 tokens [step None]
    Compactions (CKPT)   : 25 (...; last at step 11453, 337 records / +337 steps since)
    ```
- **根因分析**：
  - `format_step_tokens(rec)`（L149–L151）在字段缺失时一律返回 `0`；`cmd_status` 未区分“token 统计确实为 0”与“整份日志根本不含 `input_tokens`/`cache_read_tokens` 字段”，导致在最危险的超长会话上给出 `SAFE (< 150k)` 的虚假安全信号。
- **候选修复方向**：
  - 在遍历记录时维护 `saw_token_fields: bool`。若遍历结束后为 `False`，则将状态标记为 `UNKNOWN (transcript lacks token telemetry)`，并基于最新一次有效 `CHECKPOINT` 之后的累计 UTF-8 字节数（如 `bytes_since_last_ckpt // 4`）给出粗略的估算水位与明确提示。

---

#### `H2`：`resolve_brain_dir` 的 `"auto"` 模式按最新 `mtime` 盲选会话，在多代理（Swarm/Subagent）或多窗口并发时极易串台
- **位置**：[`scripts/context_guardian.py` L81–L124, L642, L646, L653, L667, L673](./scripts/context_guardian.py#L81-L124) 及 [`SKILL.md` L55, L59](./SKILL.md#L55-L60)
- **复现与实测证据**：
  - 当省略 `-c`（默认 `"auto"`）时，`resolve_brain_dir` 扫描 `brain/` 下所有子目录并选择 `transcript.jsonl` 的 `st_mtime` 最新者（L116–L122）。
  - 在本次 Swarm 并发审查期间实测运行无 `-c` 的 `python context_guardian.py status`，脚本自动选中的是当前最后写入磁盘的 Worker 子会话（`50f83b70`，仅 19 步），而非主会话（Coordinator `0e9a649c`）。
- **根因分析**：
  - Antigravity 支持 Subagent、Swarm 并发工作以及多 IDE 窗口同时开启。各会话的 `transcript.jsonl` 在毫秒级交替追加写入。
  - 若主代理或子代理按 `SKILL.md` 第 55、59 行的示例执行无 `-c` 的 `context_guardian.py init-anchor` 或 `recover`，将直接把 `MEMORY_ANCHOR.md` 写进错误的会话目录，或从其他代理的会话中恢复错误的上下文。
- **候选修复方向**：
  1. `SKILL.md` 中所有命令示例应强制带 `-c "<conversation-id>"`（Agent 的 `<user_information>` 中始终包含当前会话的精确 `Conversation ID`）。
  2. 在 `resolve_brain_dir` 使用 `"auto"` 时，检查过去 120 秒内是否有 `> 1` 个会话的 `transcript.jsonl` 被修改；若有，向 `sys.stderr` 打印显式 `[WARNING]` 列出所有近期活跃会话 ID；对于写操作 `init-anchor`，当检测到多会话并发活跃且未显式传入 `-c` 时应拒绝执行并要求指定 `-c`。

---

#### `H3`：`CHECKPOINT_USER_TRUNCATE_BYTES = 500` 与真实截断阈值不符，且无论是否发生压缩均误标 `[TRUNCATED BY CHECKPOINT]`
- **位置**：[`scripts/context_guardian.py` L26, L259, L303, L331, L534](./scripts/context_guardian.py#L26)（同时影响 `pre_invocation_guard.py` L183, L213, L238, L303 与 `SKILL.md` L23, L63 及模板 L48）
- **复现与实测证据**：
  - **实测发现 1（真实截断阈值并非 500 字节）**：编写脚本扫描本地全部 180 个日志文件中所有 `CHECKPOINT` 的 `# User Requests` 段落，提取所有带 `<truncated N bytes>` 标记的条目（共 12 处真实截断样本），测量 `<truncated ...>` 前保留的原文长度：
    - 其中 **5 处保留了精确的 1,021 UTF-8 字节**（961–997 字符）；
    - 另外 **7 处保留了 1,391–1,392 UTF-8 字节**（561–593 个含中文字符）；
    - **没有任何一处在 500 字节处截断**。
  - **实测发现 2（未发生压缩或压缩后的输入也被标为“已被截断”）**：在 `collect_user_inputs`（L259）中，`truncated_by_checkpoint` 仅判断 `utf8_bytes > CHECKPOINT_USER_TRUNCATE_BYTES`（500B），既不检查该输入之后是否发生过 `CHECKPOINT`，也不检查 `CHECKPOINT` 的 `# User Requests` 中是否真有 `<truncated ...>`。在会话 `b99351bf` 中，42 条用户输入里有 15 条被标上 `[TRUNCATED BY CHECKPOINT]`，包括最后一次 `CHECKPOINT` **之后**才发送、根本不可能被压缩截断的输入。
- **根因分析**：
  - 将静态字节数比较直接等同于“已被 `CHECKPOINT` 截断”，混淆了“未来发生压缩时有被截断风险（at-risk）”与“已经被历史 `CHECKPOINT` 截断（actually truncated）”。
- **候选修复方向**：
  1. 解析最新有效 `CHECKPOINT` 的 `# User Requests` 段落中紧邻 `<truncated \d+ bytes>` 的条目文本前缀，与该 `CHECKPOINT` 之前的 `USER_INPUT` 做前缀匹配，精确识别真正被截断的输入。
  2. 将启发式阈值常量调整为 `~1000` 字节，且严格区分标签：位于最新有效 `CHECKPOINT` 之前的超长输入标为 `[TRUNCATED BY CHECKPOINT]`，位于 `CHECKPOINT` 之后（或尚无 `CHECKPOINT`）的超长输入标为 `[>1KB AT-RISK OF TRUNCATION]`。

---

### 2.2 MEDIUM（中危问题）

#### `M1`：未处理重试/回退（Rewind）产生的重复及乱序 `step_index`，导致被撤销的 `CHECKPOINT` 仍被计入且关键步骤被静默覆盖
- **位置**：[`scripts/context_guardian.py` L190–L204, L243–L262, L274–L279, L480–L486](./scripts/context_guardian.py#L190-L204)
- **复现与实测证据**：
  - 在 `5f9d5f3e` 中存在 **12 个重复的 `step_index` 和 23 处 `step_index` 回退（order breaks）**；在 `b99351bf` 和 `270c6a38` 中同样存在回退重用。
  - **关键实测案例（`b99351bf` 第 442–447 行）**：
    ```text
    line 442: step_index=441 USER_INPUT       (10:02:25Z)
    line 443: step_index=442 CHECKPOINT       (10:02:25Z, input_tokens=86742, cache_read=2688)
    line 444: step_index=443 PLANNER_RESPONSE (10:02:34Z, input_tokens=16770, cache_read=2560)
    --- [此时会话从 step 441 发生回退/重试] ---
    line 445: step_index=441 USER_INPUT       (10:03:10Z)
    line 446: step_index=442 PLANNER_RESPONSE (10:03:11Z, input_tokens=241497, cache_read=7895)
    line 447: step_index=443 GENERIC          (10:03:23Z)
    ```
    可以看到，line 443 的 `CHECKPOINT (step=442)` 在 45 秒后被回退撤销，`step=442` 被重新分配给了未压缩的 `PLANNER_RESPONSE`（token 高达 249,392）！
  - 这导致 4 处连锁缺陷：
    1. **`cmd_step` 静默吞掉 `CHECKPOINT`**：执行 `context_guardian.py step 442 -c b99351bf` 时，`latest_by_step[442]`（L485）静默覆盖了 line 443 的 `CHECKPOINT`，仅输出 line 446 的 `PLANNER_RESPONSE`，且没有任何“存在早期同名步骤被隐藏”的提示。
    2. **`cmd_status` 与 `cmd_recover` 将已撤销的 `CHECKPOINT` 当作当前有效压缩点**：`b99351bf` 中 line 443 的 `CHECKPOINT` 已被回退，但 `status`（L202）和 `recover`（L274）仍将其作为最新一次压缩锚点，导致 `recover` 第 4 节提取的是已被撤销分支上的上下文。
    3. **`collect_user_inputs` 产生重复条目**：`step=441` 的两条相同 `USER_INPUT`（line 442 与 line 445）同时出现在 `recover` 第 2、3 节和 `init-anchor` 生成的 Markdown 表格中。
    4. **仅靠 `step_index` 无法唯一标识记录**：`init-anchor`（L537）生成的表格只记录 `step_index` 而不记录 `line_no`。
- **候选修复方向**：
  - 定义判断准则：若某条记录 `idx` 的 `step_index = S`，而在 `idx` **之后**存在另一条记录其 `step_index <= S`，则说明前一条记录所在的执行分支已被回退覆盖（Superseded/Rewound）。
  - `status` 与 `recover` 应区分 **Active CHECKPOINT** 与 **Rewound CHECKPOINT**（如输出 `Compactions (CKPT): 1 active (+1 rewound)`），且 `recover` 仅以最后一个 Active `CHECKPOINT` 作为分界线。
  - `collect_user_inputs` 默认对同一 `step_index` 去重（保留最后一次有效记录并标注 `[retried x2]`），同时在 `init-anchor` 索引表中输出 `step (line)`。
  - `cmd_step` 在默认非 `--all-occurrences` 模式下若覆盖了同名 `step_index`，应打印提示：`[NOTE] Hid N earlier superseded occurrence(s) of step_index=... (pass --all-occurrences to view all)`。

---

#### `M2`：`cmd_status` 在刚完成 `CHECKPOINT` 但尚未产生新 `PLANNER_RESPONSE` 时，仍显示压缩前的 `CRITICAL` 水位
- **位置**：[`scripts/context_guardian.py` L194–L204, L221–L224](./scripts/context_guardian.py#L194-L204)
- **复现与根因分析**：
  - `cmd_status` 仅在 `rec.get("type") == "PLANNER_RESPONSE"` 时更新 `latest_tokens`（L199–L201）。
  - 实测发现：部分 `CHECKPOINT` 记录自身也带有 `input_tokens` / `cache_read_tokens`（全库有 6 条 `CHECKPOINT` 含 token 字段，代表生成摘要时的消耗，如 `b99351bf` line 443 为 `86742 + 2688 = 89,430` tokens，随后首次 `PLANNER_RESPONSE` 降至 `19,330` tokens）；而当 `CHECKPOINT` 刚写入磁盘、模型尚未返回压缩后的第一条 `PLANNER_RESPONSE` 时，`latest_tokens` 仍停留在压缩前的 ~250k，导致 `status` 在刚刚压缩完的瞬间仍然报告 `CRITICAL (>= 220k: compaction imminent)`。
- **候选修复方向**：
  - 若日志中最后一个活跃事件是 `CHECKPOINT`（即其后尚无带 token 的 `PLANNER_RESPONSE`），`Current Context` 应显式报告：`POST-COMPACTION (just compacted at step X; awaiting first post-compaction model response; pre-compaction context was Y tokens)`。

---

#### `M3`：`cmd_search` 对 `tool_calls` 进行二次 JSON 序列化，导致 Windows 反斜杠路径（如 `C:\Users\...`）因四重转义而无法搜出
- **位置**：[`scripts/context_guardian.py` L165–L172, L397–L398](./scripts/context_guardian.py#L397-L398)
- **复现与实测证据**：
  - 在 `transcript_full.jsonl` 中，`tool_calls` 数组内每个工具调用的 `args` 字段值**本身已经是 JSON 序列化后的字符串**，例如：
    ```json
    {"name": "run_command", "args": {"Cwd": "\"c:\\\\Users\\\\<user>\\\\Desktop\\\\esurfing_flutter\""}}
    ```
  - 当 `extract_search_fields`（L398）调用 `format_tool_calls` -> `json.dumps(tool_calls, indent=2)` 时，字符串被**再次转义**，内存中的被检索文本变成了 `"c:\\\\Users\\\\<user>\\\\Desktop..."`。
  - 实测在 `5f9d5f3e` 中执行：
    ```powershell
    python context_guardian.py search "C:\Users\<user>\Desktop" -c 5f9d5f3e --type PLANNER_RESPONSE
    ```
    结果：**4,999 条 `tool_calls` 记录无一命中**（仅命中 1 条 `thinking` 文本）；必须把搜索词写成四重反斜杠 `Users\\\\<user>\\\\Desktop` 才能搜出 `tool_calls`！
- **候选修复方向**：
  - 为 `search` 构造 `tool_calls` 检索文本时，遍历每个 tool call 的 `args` 字典，对字符串类型的 value 尝试一次 `json.loads(v)` 解包（若解包成功则使用反转义后的真实字符串），或者在 `format_tool_calls` 基础上递归解包嵌套 JSON 字符串后再供正则匹配。

---

#### `M4`：`cmd_search` 将含反斜杠的普通文本（如 Windows 路径）静默编译为合法正则转义序列，导致漏搜且不触发字面量回退
- **位置**：[`scripts/context_guardian.py` L420–L423, L651–L662](./scripts/context_guardian.py#L420-L423)
- **复现与根因分析**：
  - L420–L423 逻辑为：先尝试 `re.compile(args.pattern, re.IGNORECASE)`，仅在抛出 `re.error` 时才回退到 `re.escape(args.pattern)`。
  - 当用户搜索 Windows 路径如 `C:\temp\new\file.txt` 或 `D:\backup\test\1` 时，`\t`、`\n`、`\b`、`\f`、`\1` 在 Python 正则中**全部是合法转义序列**（分别代表制表符、换行符、退格/词边界、换页符、第 1 组反向引用），`re.compile` 不会报错，结果返回 `No matches found` 且不触发 `re.escape` 回退。
- **候选修复方向**：
  1. 为 `search` 子命令增加 `-F` / `--literal`（或 `--fixed-strings`）选项，直接按字面量 `re.escape` 搜索。
  2. 当未传 `-F`、模式串中包含 `\`、且按正则搜索结果为 0 条时，自动用 `re.escape(args.pattern)` 重试一次并打印提示。

---

#### `M5`：`cmd_init_anchor` 与 `cmd_recover` 的用户输入索引表无上限，在长会话中生成 31 KB 锚点文件，违背 `SKILL.md` `< 4 KB` 约束
- **位置**：[`scripts/context_guardian.py` L298–L309, L531–L555](./scripts/context_guardian.py#L531-L555) 及 [`SKILL.md` L123](./SKILL.md#L123)
- **复现与实测证据**：
  - `SKILL.md` L123 明确规定：“用‘简明事实 + `step_index` 书签’保持锚点精炼（建议 < 4 KB）”。
  - 但在沙箱中对真实会话 `5f9d5f3e`（307 条 `USER_INPUT`）运行 `init-anchor`，生成的 `MEMORY_ANCHOR.md` 初始大小高达 **31,190 字节（31.2 KB，含 619 行表格相关内容）**；在 `b99351bf` 上也达到 **9,320 字节（9.3 KB）**。
  - 同理，`cmd_recover` 第 2 节（L298–L308）无条件打印所有历史 `USER_INPUT` 索引，在 `5f9d5f3e` 上单次 `recover` 输出达 **36 KB**。
  - 更严重的是，`pre_invocation_guard.py`（L218）在压缩后会读取 `MEMORY_ANCHOR.md` 前 6,000 字符注入上下文——若文件被 300 行自动生成的低价值短输入（如“继续”“好的”）填满，反而挤占宝贵的上下文窗口。
- **候选修复方向**：
  - `cmd_init_anchor` 自动生成的索引表应仅包含：**所有超长/被截断的高风险输入（`> 1000B`）+ 最近 N 条（如最近 10 条）用户输入**，总行数上限控制在 20–25 行以内（约 2 KB），其余提示通过 `context_guardian.py search/recover` 查询。
  - `cmd_recover` 增加 `--index-limit` 参数（默认如 `30`），第 2 节优先展示所有被截断/超长输入及最近 30 条输入，并提示省略了多少条早期短输入。

---

#### `M6`：`load_hooks_json` 与 `cmd_hook` 不支持 UTF-8 BOM、非原子写入 `hooks.json`，且条目类型异常时崩溃
- **位置**：[`scripts/context_guardian.py` L560–L570, L589–L630](./scripts/context_guardian.py#L560-L630)
- **复现与实测证据**：
  1. **UTF-8 BOM 崩溃（已复现）**：在 Windows 上，用户或脚本常使用 PowerShell 5.1 的 `Set-Content -Encoding UTF8` 编辑配置文件，这会在文件头写入 `EF BB BF`（UTF-8 BOM）。由于 `load_hooks_json`（L564）使用 `encoding="utf-8"` 而非 `"utf-8-sig"`，实测传入带 BOM 的 `hooks.json` 时直接报错退出：
     ```text
     [ERROR] Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)
     ```
  2. **非字典条目崩溃（已复现）**：若 `hooks.json` 中 `"anti-compaction-amnesia"` 的值被误写为非 `dict`（如布尔值或字符串），`hook --status` 在 L603 崩溃（`'str' object has no attribute 'get'`），`hook --disable` 在 L622 崩溃（`'str' object does not support item assignment`）。
  3. **非原子覆盖写入**：`hooks.json` 是全局共享配置文件，可能包含其他插件或用户的自定义 Hook。L611 与 L623 直接调用 `hooks_path.write_text(...)` 原地截断写入，既无 `.bak` 备份，也未使用 `.tmp` + `os.replace` 原子替换；且 `--enable`（L610）会直接覆盖用户已修改过的 `timeout` 等自定义字段。
- **候选修复方向**：
  - `load_hooks_json` 改用 `encoding="utf-8-sig"`；检查 `isinstance(entry, dict)`；写入前备份 `hooks.json.bak` 并通过同目录临时文件 + `os.replace` 原子落盘。

---

#### `M7`：`build_hook_spec` 使用裸 `python` 且仅在路径含空格时加引号，在 Windows `cmd /c` 下存在环境与特殊字符隐患
- **位置**：[`scripts/context_guardian.py` L573–L586](./scripts/context_guardian.py#L573-L586)（关联 `pre_invocation_guard.py` 的 `MED-5`）
- **复现与根因分析**：
  - 根据 Antigravity 官方文档 `docs/hooks.md` L129–L130，Windows 上的 Hook 命令通过 `cmd /c` 执行。
  - `build_hook_spec` 生成的命令为 `"command": f"python {quoted_path}"`（L582）：
    1. 裸 `python` 依赖宿主进程启动 `cmd /c` 时的 `PATH` 顺序。在未配置全局 PATH 或存在 WindowsApps 微软商店空桩（Store Stub，退出码 9009）的机器上，Hook 会静默失效。
    2. L576 `quoted_path = f'"{script_posix}"' if " " in script_posix else script_posix` 仅在路径包含空格时才加双引号。但在 Windows `cmd /c` 中，若用户目录名或路径包含 `&`、`^`、`(`、`)`、`%`、`!` 等合法文件名字符（无空格），不加引号会导致 `cmd /c` 解析语法错误。
    3. 未传入 `-X utf8` 标志，也未在 `--enable` 时校验 `GUARD_SCRIPT_PATH.is_file()` 是否存在。
- **候选修复方向**：
  - 始终对解释器路径与脚本路径加双引号，并优先使用当前可用的 Python 解释器绝对路径（或 `py -3`）加 `-X utf8`：
    ```python
    py_exe = Path(sys.executable).resolve().as_posix()
    script_posix = GUARD_SCRIPT_PATH.resolve().as_posix()
    command = f'"{py_exe}" -X utf8 "{script_posix}"'
    ```

---

#### `M8`：`DEFAULT_BRAIN_ROOT` 硬编码为 `~/.gemini/antigravity/brain`，不支持 `antigravity-cli` / `antigravity-ide` 且未暴露 CLI 参数
- **位置**：[`scripts/context_guardian.py` L29, L81–L93](./scripts/context_guardian.py#L29)
- **复现与根因分析**：
  - 根据 Antigravity 内置文档 `agy-customizations/docs/hooks.md` L173–L180，不同宿主界面的目录名不同：
    - Antigravity 2.0：`~/.gemini/antigravity/`
    - CLI：`~/.gemini/antigravity-cli/`
    - IDE：`~/.gemini/antigravity-ide/`
  - `resolve_brain_dir` 虽然在函数签名中预留了 `brain_root: Optional[Path] = None`（L83），但 `build_parser()` 中没有任何子命令暴露 `--brain-root` 参数，各 `cmd_*` 调用 `resolve_brain_dir(args.conversation_id)` 时也从未传入 `brain_root`。
- **候选修复方向**：
  - 支持环境变量 `AGY_BRAIN_ROOT` 与命令行参数 `--brain-root`；未指定时自动探测 `~/.gemini/antigravity/brain`、`~/.gemini/antigravity-cli/brain`、`~/.gemini/antigravity-ide/brain` 中存在的目录。

---

### 2.3 LOW（低危与易用性问题）

#### `L1`：`cmd_status` 中 `records_since = len(records) - last_line` 混用了有效记录数与文件物理行号
- **位置**：[`scripts/context_guardian.py` L230](./scripts/context_guardian.py#L230)
- **说明**：`len(records)` 是解析成功的 JSON 对象数量，而 `last_line` 是文件 1-based 物理行号。一旦文件中存在空行或因并发写入截断被跳过的损坏行（L138, L143），两者相减得出的 `records_since` 就会偏小甚至为负数。应改为使用该 `CHECKPOINT` 在 `records` 列表中的下标 `len(records) - 1 - last_ckpt_idx`。

#### `L2`：`iter_jsonl_records` 静默忽略损坏的 JSON 行，未在 `status` 中报告跳过行数
- **位置**：[`scripts/context_guardian.py` L132–L146](./scripts/context_guardian.py#L132-L146)
- **说明**：流式追加写入时末尾可能出现半行 JSON，静默跳过是合理的容错设计；但如果文件中间存在多行损坏记录，完全不统计也不在 `status` 中提示会掩盖日志损坏问题。

#### `L3`：`recover` / `search` / `step` 在缺失 `transcript_full.jsonl` 时静默回退到有损 `transcript.jsonl`
- **位置**：[`scripts/context_guardian.py` L269, L416, L463](./scripts/context_guardian.py#L269)
- **说明**：在 `5f9d5f3e` 的 `transcript.jsonl` 中，共有 **751 条记录包含 `truncated_fields`**（其中 `content` 被截断 593 次，`tool_calls` 155 次，`thinking` 5 次）。当 `transcript_full.jsonl` 缺失而回退到 `transcript.jsonl` 时，脚本未向 `stderr` 或输出头部打印任何有损回退警告。

#### `L4`：`search --type` 缺少 `ERROR_MESSAGE` 枚举值，且 `search` 与 `step` 均忽略了记录中的 `error` 字段
- **位置**：[`scripts/context_guardian.py` L387–L399, L503–L512, L655–L659](./scripts/context_guardian.py#L655-L659)
- **复现证据**：全库扫描发现真实日志中存在 **6 种 `type`**：`PLANNER_RESPONSE`、`GENERIC`、`SYSTEM_MESSAGE`、`USER_INPUT`、`CHECKPOINT` 以及 **`ERROR_MESSAGE`（在 `5f9d5f3e` 中有 78 条，全库共 156 条）**；同时有 **544 条记录包含顶层 `"error"` 字段**。但 `--type` 的 `choices`（L657）未列出 `ERROR_MESSAGE`，ทำให้无法按 `--type ERROR_MESSAGE` 过滤；且 `extract_search_fields` 与 `cmd_step` 均只处理 `content`/`thinking`/`tool_calls`，完全丢弃了 `rec.get("error")`。

#### `L5`：`cmd_search` 搜索以 `-` 开头的关键词（如命令行参数 `-rf`、`-c`）时被 `argparse` 拦截报错
- **位置**：[`scripts/context_guardian.py` L652](./scripts/context_guardian.py#L652)
- **复现证据**：实测执行 `python context_guardian.py search "-rf" -c 5f9d5f3e` 直接被 `argparse` 当作未知选项拒绝（退出码 2）。应在帮助信息与 `SKILL.md` 中说明使用 `--` 分隔符（`search -- "-rf"`）。

#### `L6`：`cmd_init_anchor --force` 直接覆盖手写 `MEMORY_ANCHOR.md` 且无备份
- **位置**：[`scripts/context_guardian.py` L520–L522, L555](./scripts/context_guardian.py#L520-L522)
- **说明**：若 Agent 在已精心维护的 `MEMORY_ANCHOR.md` 上误传 `--force`，原有的铁律、精确参数与死胡同记录会被空模板瞬间冲掉。应在 `--force` 覆盖前自动备份为 `MEMORY_ANCHOR.md.bak` 并采用原子写入。

#### `L7`：`resolve_brain_dir` 未校验 `conversation_id` 格式，允许路径穿越（如 `-c ..`）
- **位置**：[`scripts/context_guardian.py` L94–L97](./scripts/context_guardian.py#L94-L97)
- **复现证据**：实测调用 `resolve_brain_dir("..", brain_root)` 时，`exact = root / ".."` 满足 `exact.is_dir()`，直接返回了父目录。应对 `conversation_id` 做正则校验（如 `^[0-9a-fA-F-]{4,36}$`）并确保 `exact.resolve().parent == root.resolve()`。

#### `L8`：`cmd_recover` 第 4 节硬编码截断长度（600/800 字符），且在无 `CHECKPOINT` 分支漏打 `thinking` 与 `tool_calls`
- **位置**：[`scripts/context_guardian.py` L349–L373](./scripts/context_guardian.py#L349-L373)
- **说明**：第 4 节在有 `CHECKPOINT` 时打印 `thinking`（硬编码 600 字）、`tool_calls`（600 字）、`content`（800 字），忽略了用户传入的 `--max-chars`；而在 `else`（尚无 `CHECKPOINT`）分支（L366–L373）只打印了 `content`，完全漏掉了 `thinking` 和 `tool_calls`。

#### `L9`：`cmd_step` 默认 `--max-chars 0`（无限制）配合大区间参数时可一次性向终端倾倒数 MB 数据
- **位置**：[`scripts/context_guardian.py` L467–L470, L668](./scripts/context_guardian.py#L467-L470)
- **说明**：`SKILL.md` 硬约束严禁裸读大段原始日志撑爆上下文，但 `step 1 1000` 在默认 `--max-chars 0` 下会无截断输出上千步的全部 `thinking`、`tool_calls` 与 `content`。当查询步数 `> 3` 且未显式指定 `--max-chars` 时，应设置安全默认上限（如每字段 4,000 字符）。

#### `L10`：`cmd_hook` 的 `--status` / `--enable` / `--disable` 未使用 `mutually_exclusive_group`，且无参调用时报错
- **位置**：[`scripts/context_guardian.py` L592–L594, L677–L682](./scripts/context_guardian.py#L677-L682)
- **说明**：使用 `p_hook.add_mutually_exclusive_group()` 并在未传标志时默认执行 `--status` 更符合 CLI 直觉。

#### `L11`：`prompt.encode("utf-8")` 与 `anchor_path.write_text(..., encoding="utf-8")` 未指定 `errors="replace"`
- **位置**：[`scripts/context_guardian.py` L250, L555](./scripts/context_guardian.py#L250)
- **说明**：虽然全库 180 个真实日志中未检出孤立代理项（Lone Surrogates `[\uD800-\uDFFF]`），但 JSON 解析器允许解析 `\ud83d` 这类未配对代理项。一旦日志中出现孤立代理项，`.encode("utf-8")` 会抛出 `UnicodeEncodeError`。建议统一加上 `errors="replace"`。

---

### 2.4 `context_guardian.py` 验证通过项（Verified Good）

1. **Token 计算公式准确**：在全部 34 个含 token 字段的真实会话中，`input_tokens` 与 `cache_read_tokens` 均为 `int` 类型，且在所有 `status == "DONE"` 的 `PLANNER_RESPONSE` 上 100% 存在；`input_tokens + cache_read_tokens` 精确反映了模型当轮的活跃上下文窗口占用（实测观察到会话在达到 `249,392`–`262,672` tokens 时触发 `CHECKPOINT`）。
2. **大文件性能与内存表现良好**：在最大真实日志 `5f9d5f3e`（15.77 MB `transcript_full.jsonl` / 13.48 MB `transcript.jsonl`，11,769 条记录）上使用 `tracemalloc` 实测：
   - `status`：耗时 **0.44 s**，峰值堆内存 **37 MB**
   - `recover`：耗时 **0.34 s**，峰值堆内存 **40 MB**
   - `search`（全表扫描无命中）：耗时 **0.52 s**，峰值堆内存 **40 MB**
   - `step`（单步提取）：耗时 **0.31 s**，峰值堆内存 **40 MB**
   - （注：若未来出现 >100 MB 的极端日志，可将 `iter_jsonl_records` 从一次性返回 `List` 改为生成器 `Iterator` 流式迭代，使 `status` / `search` 内存降至 O(1)。）

---

## 3. `scripts/pre_invocation_guard.py`、`hooks.json` 与 `SKILL.md` 审查发现（`@hook-rev`）

### 3.1 协议契约验证基线（Contract Check: PASS）

- 对照 Antigravity 官方 `agy-customizations/docs/hooks.md`（L98, L149–L167, L314–L335）核验：
  - **输入字段**：脚本读取的 `transcriptPath`、`artifactDirectoryPath`、`conversationId`（L331–L333）与官方 camelCase 契约完全一致。
  - **输出格式**：返回的 `{"injectSteps": [{"systemMessage": {"systemMessage": ..., "metadata": {"user_markdown": ...}}}]}`（L276–L285, L317–L326）及空操作 `{}` 均严格符合 `PreInvocation` 规范。
  - **执行耗时**：在 Windows `cmd /c` 下通过管道将真实日志 `0864f417` 喂给钩子脚本，单次端到端耗时 **250–280 ms**（远低于 10 秒超时限制）。

---

### 3.2 HIGH（高危问题）

#### `HIGH-1`：首次启用钩子或状态文件丢失时，会将 256 KB 尾部内的陈旧历史 `CHECKPOINT` 误判为“刚刚触发压缩”并注入过时恢复包（已复现）
- **位置**：[`scripts/pre_invocation_guard.py` L89–L104, L347–L383](./scripts/pre_invocation_guard.py#L347-L383)
- **复现与实测证据**：
  - 当会话目录尚无 `scratch/.compaction_guard_state.json`（例如用户在长会话中途首次启用本钩子、清理了 `scratch/`、或状态文件损坏被重置为 `last_handled_checkpoint_step: -1`）时，只要 `transcript.jsonl` 末尾 256 KB 内存在任何历史 `CHECKPOINT`，L366 的 `if ckpt_step > prev_step or ckpt_key != prev_key:` 就会立即触发。
  - **实测复现**：将真实会话 `0864f417` 的 Payload 通过管道传入钩子（初始无状态文件），脚本立即输出了一段 **2.8 KB 的 `[ANTI-COMPACTION GUARD] 检测到当前会话刚刚在 step_index=986 触发了有损上下文压缩`** 注入包——而实际上 `step_index=986` 是 **约 250 步之前的陈旧压缩点**！这会导致用户首次开启钩子后，所有已有长会话都会在下一轮被错误注入几百步之前的过期上下文。
  - 此外，若末尾 256 KB 内的最后一个 `CHECKPOINT` 是像 `b99351bf` line 443 那样**已被回退撤销（Rewound）**的 `CHECKPOINT`，钩子同样会误触发。
- **根因分析**：
  - L348–L355 在扫描 `tail_records` 时，只记录了最后一个 `CHECKPOINT`，却**没有检查该 `CHECKPOINT` 之后是否已经执行了多轮正常的 `PLANNER_RESPONSE`**（也没有检查该 `CHECKPOINT` 是否被后续更小的 `step_index` 回退覆盖）。
- **候选修复方向**：
  1. 忽略已被回退的 `CHECKPOINT`（其后存在 `step_index <= ckpt_step` 的记录）。
  2. 统计该 `CHECKPOINT` 之后在 `tail_records` 中出现的带 token 的 `PLANNER_RESPONSE` 数量 `post_ckpt_planner_count`。仅当 `post_ckpt_planner_count <= 1`（即确实处于压缩刚结束的恢复窗口内）时才构造并返回 `injectSteps` 恢复包；若 `post_ckpt_planner_count > 1`，说明这是历史遗留的旧 `CHECKPOINT`，此时仅静默更新 `.compaction_guard_state.json` 中的 `last_handled_checkpoint_step` / `key` 并返回 `{}`。

---

#### `HIGH-2`：在构造输出与刷写 `stdout` 之前就提前持久化了 `save_state`，一旦后续异常或超时将永久丢失恢复注入
- **位置**：[`scripts/pre_invocation_guard.py` L372 (早于 L377), L396 (早于 L404)](./scripts/pre_invocation_guard.py#L366-L409)
- **复现与根因分析**：
  - 在 Case A（检测到新 `CHECKPOINT`）中，脚本在第 372 行就调用了 `save_state(state_path, state)` 将该 `CHECKPOINT` 标记为“已处理”，随后才在第 377 行调用 `build_post_compaction_output(...)` 去读取 1–4 MB 的 `transcript_full.jsonl` 并构造输出，最后在 `main()` 第 426 行写入 `stdout`。
  - 在 Case B（水位预警）中，同样在第 396 行先调用了 `save_state`，随后才在第 404 行构造输出。
  - 如果在 `save_state` 之后发生任何异常（例如 `transcript_full.jsonl` 读取 I/O 错误、编码异常、或超过 10 秒被宿主超时终止），`main()` 的 `except Exception:`（L428–L430）会兜底输出 `{}`，但磁盘上的 `.compaction_guard_state.json` **已经永久记录了该 `CHECKPOINT` 或水位等级已处理**，下一轮调用时将不再触发，导致最关键的压缩恢复包彻底丢失！
- **候选修复方向**：
  - 严格调整时序：先完成 `build_post_compaction_output` / `build_watermark_output` 的构建，甚至在 `main()` 中将 JSON 序列化并 `sys.stdout.write(...)` + `sys.stdout.flush()` 成功后，最后一步再原子写入 `state_path`。偶发的重复提醒远好于永久静默丢失。

---

### 3.3 MEDIUM（中危问题）

#### `MED-3`：`read_tail_records` 使用 `str.splitlines()` 切分 JSONL，遇到行内未转义的 `U+0085` / `U+2028` / `U+2029` 会把单条 JSON 劈成两半导致记录静默丢失（已复现）
- **位置**：[`scripts/pre_invocation_guard.py` L56–L62](./scripts/pre_invocation_guard.py#L56-L62)
- **复现与实测证据**：
  - JSONL 规范以字节 `0x0A`（`\n`）或 `\r\n` 作为记录分隔符。但 Python 的 `str.splitlines()` 不仅按 `\r` / `\n` 切分，还会按 Unicode 行分隔符 **`U+0085` (NEL, UTF-8 `C2 85`)、`U+2028` (LS)、`U+2029` (PS)、`\x0b`、`\x0c`、`\x1c`–`\x1e`** 切分！
  - 标准 JSON 编码器默认不会转义字符串内部的 `U+0085` 等字符。**实测扫描本地真实会话日志，在 2 个真实会话的 JSON 记录内部检出了原始 `C2 85` (`U+0085`) 字节**。
  - 当某条 `USER_INPUT`（例如用户粘贴的终端日志或文档）或 `CHECKPOINT` 的 `content` 字符串中含有 `U+0085` 时，`text.splitlines()` 会把这一行合法的 JSON 对象从中间劈成两个非法片段，紧接着在 L67–L69 被 `except json.JSONDecodeError: continue` **静默丢弃**！
  - 这意味着：含此类字符的用户输入在压缩恢复时会凭空消失；若 `CHECKPOINT` 摘要中恰好包含该字符，钩子将**完全漏检这次压缩**。
- **候选修复方向**：
  - 在字节层面上仅按 `b"\n"` 切分，然后再对每一行单独做 UTF-8 解码：
    ```python
    raw_lines = raw.split(b"\n")
    ```

---

#### `MED-5`：`hooks.json` 与 `context_guardian.py` 使用裸 `python`，在 Windows `cmd /c` 环境下存在解释器解析与编码风险
- **位置**：`$HOME/.gemini/config/hooks.json` L6 及 [`scripts/context_guardian.py` L582](./scripts/context_guardian.py#L582)
- **复现与实测证据**：
  - 在当前机器的 PowerShell 中测试 `Get-Command python -All`，发现存在两个不同解释器：`WindowsApps\python.exe`（转发至 `pythoncore-3.14-64`）与 `C:\msys64\mingw64\bin\python.exe`。
  - 当 Antigravity 宿主通过 `cmd /c` 启动 Hook 时，其环境变量 `PATH` 未必与用户交互式终端一致；若命中未安装 Python 时的微软商店 Stub（退出码 9009，无 stdout 输出）或其它未预期的 Python 环境，Hook 将失效。
- **候选修复方向**：
  - 在 `hooks.json` 与 `build_hook_spec()` 中使用 `sys.executable` 的绝对路径并显式加上 `-X utf8` 标志（如 `"<python_abs_path>" -X utf8 "<guard_script_abs_path>"`）。

---

### 3.4 LOW / INFO（低危问题、边界情况与文档偏差）

#### `LOW-4`：`read_tail_records` 在 `start > 0` 时无条件丢弃第一行，当 `start` 恰好落在行首边界时会凭空丢掉一条完整记录（已复现）
- **位置**：[`scripts/pre_invocation_guard.py` L52–L60](./scripts/pre_invocation_guard.py#L52-L60)
- **复现与根因分析**：
  - L58–L60 写有 `if start > 0 and lines: lines = lines[1:]`，假设 `start` 一定落在一行中间。但如果 `start = size - tail_bytes` 恰好位于上一行换行符 `\n` 之后的第一字节（即新记录的行首），`lines[0]` 实际上是一条完整的 JSON 记录，却被无条件丢弃了（实测构造 4 行样本在边界处读取，预期返回 `[3, 4]`，实际只返回 `[4]`）。
- **候选修复方向**：
  - 当 `size > tail_bytes` 时，向前多读 1 个字节：`seek(size - tail_bytes - 1)`，并在字节流中丢弃第一个 `b"\n"` 及其之前的部分：
    ```python
    if size > tail_bytes:
        fh.seek(size - tail_bytes - 1)
        raw = fh.read()
        nl = raw.find(b"\n")
        raw = raw[nl + 1 :] if nl != -1 else b""
    ```

---

#### `LOW-6`：`main()` 输出 Hook 结果时使用 `ensure_ascii=False`，依赖 Windows 管道 UTF-8 重配置成功
- **位置**：[`scripts/pre_invocation_guard.py` L426](./scripts/pre_invocation_guard.py#L426)
- **说明**：L426 使用 `json.dumps(result, ensure_ascii=False)` 直接向 `stdout` 输出中文字符与 Emoji（如 `🛡️`、`⚠️`）。虽然 L415 调用了 `configure_utf8_stdio()`，但在 `cmd /c` 管道及不同宿主编码设置下，直接输出 `ensure_ascii=True`（纯 ASCII 的 `\uXXXX` JSON 转义序列）在被宿主 `json.loads` 解析时与原文 100% 等价，且**彻底消除了任何跨平台/跨代码页乱码的可能性**。

---

#### `LOW-7`：90 秒锚点免打扰窗口（Grace Window）在检查前就已经更新了 `last_warned_band`，导致 `220k CRITICAL` 预警被静默吞掉且不再触发
- **位置**：[`scripts/pre_invocation_guard.py` L394–L409](./scripts/pre_invocation_guard.py#L394-L409)
- **复现与根因分析**：
  - 当 token 跨越 185k（Band 1）或 220k（Band 2）时，L395–L396 **先将 `state["last_warned_band"] = current_band` 写入磁盘**，然后才在 L398–L402 检查 `MEMORY_ANCHOR.md` 的修改时间是否在 90 秒以内（若是则返回 `{}` 跳过提醒）。
  - 场景复现：假设在 186k tokens 时触发了 Band 1 提醒，Agent 立即更新了 `MEMORY_ANCHOR.md`；随后在 60 秒内，Agent 连续执行了几个大输出工具调用，上下文迅速暴涨跨越 220k（Band 2 CRITICAL）。此时进入 L394：脚本先把 `last_warned_band` 设为 `2` 并落盘，然后发现 `MEMORY_ANCHOR.md` 距上次修改仅过去 60 秒（`< 90s`），于是返回 `{}` 不发提醒。等到 90 秒过去、token 涨到 240k 时，由于 `last_warned_band` 已经是 `2`，**220k 的 CRITICAL 最高级别预警被永久吞掉了**！
- **候选修复方向**：
  - 先检查 90 秒免打扰窗口（且建议对 Band 2 `CRITICAL` 缩短免打扰时间或始终提醒）；仅当确实决定发送预警消息时，才更新 `state["last_warned_band"] = current_band`。

---

#### `LOW-8`：当尾部 256 KB 内暂无带 token 的 `PLANNER_RESPONSE`（`latest_tokens == 0`）时，会将 `last_warned_band` 重置为 0，缺乏迟滞（Hysteresis）保护
- **位置**：[`scripts/pre_invocation_guard.py` L346–L356, L388–L391](./scripts/pre_invocation_guard.py#L388-L391)
- **根因分析**：
  - 若某次超大工具调用输出（如单条 `GENERIC` 步骤占了 >256 KB）把上一条 `PLANNER_RESPONSE` 挤出了 256 KB 尾部窗口，此时 `latest_tokens` 算出来为 `0`，`current_band` 为 `0`。
  - L388 `if current_band < last_warned_band:` 会误以为发生了压缩回落，把 `last_warned_band` 重置为 `0`。下一轮 `PLANNER_RESPONSE` 出现时，又会重复触发一次已经发过的 185k/220k 预警。
- **候选修复方向**：
  - 若 `latest_tokens == 0`（即尾部未读到有效 token 记录），直接返回 `{}` 而不修改 `state["last_warned_band"]`。

---

#### `LOW-9`：当目标 `CHECKPOINT` 超出 `transcript_full.jsonl` 的 4 MB 读取窗口时，`ckpt_pos == -1` 会把压缩后的最新模型回复误标为“压缩前快照”
- **位置**：[`scripts/pre_invocation_guard.py` L141–L145, L188–L195, L248–L261, L374–L376](./scripts/pre_invocation_guard.py#L188-L195)
- **根因分析**：
  1. 在 `extract_compaction_recovery_bundle` 中，若未在窗口内找到目标 `CHECKPOINT`（`ckpt_pos == -1`），L189 `search_end = ckpt_pos if ckpt_pos >= 0 else len(records)` 会从列表末尾向前找最后一条 `PLANNER_RESPONSE`，并在第 3 节（L250）将其标题打印为 `### 3. 压缩前最后一次模型执行快照`——但当 `ckpt_pos == -1` 时，文件末尾的 `PLANNER_RESPONSE` 实际上是**压缩后**的回复！
  2. 此外，L374–L376 在 `transcript_full.jsonl` 不存在时静默回退到 `transcript.jsonl`，但 L213 和 L235 仍然宣称“从 `transcript_full.jsonl` 无损恢复”。
- **候选修复方向**：
  - 当 `ckpt_pos < 0` 时，令 `pre_planner = None`（省略第 3 节），不要从 `len(records)` 倒搜；当回退到 `transcript.jsonl` 时在输出中明确标注有损回退。

---

#### `LOW-10`：`save_state` 非原子写入 `.compaction_guard_state.json`
- **位置**：[`scripts/pre_invocation_guard.py` L107–L110](./scripts/pre_invocation_guard.py#L107-L110)
- **说明**：`state_path.write_text(...)` 若在写入中途被进程终止，会留下半截损坏的 JSON 文件。下一轮 `load_state`（L98–L104）捕获异常后会回退到全默认状态（`last_handled_checkpoint_step: -1`），进而直接触发 `HIGH-1`（把历史旧 `CHECKPOINT` 当成新压缩再次注入）。应使用同目录 `.tmp` 临时文件 + `os.replace` 原子写入。

---

#### `LOW-11`：Token 水位检测天然滞后 1 个模型轮次（One-Step Token Lag）
- **位置**：[`scripts/pre_invocation_guard.py` L348–L352](./scripts/pre_invocation_guard.py#L348-L352)
- **说明**：`PreInvocation` 在模型调用**之前**运行，此时当前轮次尚未产生 `PLANNER_RESPONSE`，因此脚本读到的 `latest_tokens` 是**上一轮** `PLANNER_RESPONSE` 的 `input_tokens + cache_read_tokens`，尚未计入上一轮刚返回的 `GENERIC` 工具输出体积和本轮新增的 `USER_INPUT` 体积。虽然 220k 到 250k 之间留有 30k tokens 的缓冲裕量，但如果用户在 215k 时执行了一个返回 40k tokens 的工具调用，系统会在触发 220k 预警之前直接冲破 250k 触发压缩。建议在 `SKILL.md` 中明确说明这一机制。

---

#### `INFO-12`：`PreInvocation` 钩子与宿主内部自动 `CHECKPOINT` 生成的先后时序待实测确认
- **位置**：[`scripts/pre_invocation_guard.py` L360–L383](./scripts/pre_invocation_guard.py#L360-L383) 及 [`SKILL.md` L110](./SKILL.md#L110)
- **实测数据分析**：
  - 检查真实会话 `0864f417` 的时间戳：
    - `step 610 (USER_INPUT)`：`12:12:02Z`
    - `step 611 (CHECKPOINT)`：`12:12:02Z`
    - `step 612 (PLANNER_RESPONSE)`：`12:12:17Z`
  - 这引出了一个核心生命周期时序问题：当一次调用即将触发压缩时，Antigravity 宿主究竟是**先写入 `CHECKPOINT` 再运行 `PreInvocation` 钩子**，还是**先运行 `PreInvocation` 钩子、再执行内部 `CHECKPOINT` 压缩、然后直接调模型**？
  - 如果是后者（`PreInvocation` -> `CHECKPOINT` -> `Model Call #1`），那么在压缩发生的那一轮，`PreInvocation` 运行时尚未写入 `CHECKPOINT`，恢复包要到**压缩后的第 2 次模型调用（`Model Call #2`）** 才会被注入！

---

#### `SKILL.md` 文档表述偏差
1. **[`SKILL.md` L108](./SKILL.md#L108)**：写有“只扫描末尾 ≤256 KB”，表述不完整。实际上 `pre_invocation_guard.py` 在平时扫描 `transcript.jsonl` 末尾 256 KB，但在检测到 `CHECKPOINT` 触发恢复时，会读取最多 **2 MB 全量或 1 MB–4 MB 尾部**的 `transcript_full.jsonl`（`pre_invocation_guard.py` L41–L43, L120–L129）。
2. **[`SKILL.md` L110](./SKILL.md#L110)**：写有“在模型恢复后的首次调用自动读取……”，受限于上述 `INFO-12` 的宿主生命周期顺序，若 `PreInvocation` 早于 `CHECKPOINT` 执行，则实际注入发生在压缩后的第二次模型调用。
3. **[`SKILL.md` L124](./SKILL.md#L124)**：写有“不要在 `hooks.json` 中手动修改状态文件来绕过边沿检测”，表述容易引起误解——边沿检测状态文件并非保存在 `hooks.json` 中，而是保存在会话 Artifact 目录下的 `<artifactDir>/scratch/.compaction_guard_state.json`。

---

## 4. 待 Carbon 重点审查与建议的开放架构问题 (Open Architectural Questions for Carbon)

请 **Carbon** 在审查上述具体代码缺陷的同时，重点针对以下 6 个架构与设计权衡给出决策建议：

### 问题 1：`PreInvocation` 与 `CHECKPOINT` 的生命周期时序与零延迟恢复 (`INFO-12`, `LOW-11`)
- **背景**：如果 Antigravity 宿主在单次模型调用周期内的执行顺序是 `PreInvocation Hook -> (内部检查发现超 250k) -> 生成 CHECKPOINT -> 调用 LLM`，那么在压缩刚完成后的第一次 LLM 推理时，`PreInvocation` 已经跑过了，`pre_invocation_guard.py` 的恢复包要等到下一次工具调用后的 `PreInvocation`（即压缩后第 2 轮推理）才能注入。
- **请 Carbon 建议**：
  1. 是否应当在接近 250k（例如 `>= 235k tokens` 或预计加上本轮 `lastUserInput` 将逼近 250k）时，在 `PreInvocation` 阶段就**提前预注入（Pre-emptive Injection）** 最核心的 `MEMORY_ANCHOR.md` 摘要？还是说在 `CHECKPOINT` 之前的注入同样会被 `CHECKPOINT` 摘要器压掉？
  2. 是否有更优的 Hook 事件组合（例如结合 `PostToolUse` 或 `PostInvocation` 提前感知大体积工具输出导致的 token 激增）来弥补 `LOW-11` 的一步滞后？

### 问题 2：缺失 Token 遥测字段的会话（Legacy / Stripped Transcripts）如何精确估算水位 (`H1`, `LOW-8`)
- **背景**：实测本地 90 个会话中有 56 个不含 `input_tokens` / `cache_read_tokens`（包括 15.8 MB、25 次压缩的 `5f9d5f3e`）。在这些会话上，`status` 和 `pre_invocation_guard.py` 的水位预警目前完全失效（永远显示 0 tokens / `SAFE`）。
- **请 Carbon 建议**：
  - 当检测不到 `input_tokens` 字段时，`context_guardian.py` 和 `pre_invocation_guard.py` 应当采用何种回退估算模型？
  - 候选方案 A：统计自最新一次有效 `CHECKPOINT` 以来所有记录的 `content + thinking + tool_calls` UTF-8 字节数（或字符数），加上最新 `CHECKPOINT` 自身的字节数，按经验系数（如中文/代码混合场景 `1 token ≈ 3.2 ~ 4.0 bytes`）估算当前活跃上下文 token 数，并标注 `(estimated)`。
  - 候选方案 B：在 `pre_invocation_guard.py` 的 256 KB 尾部扫描中，若无 token 字段，是否需要向上回溯到最近一个 `CHECKPOINT` 来计算累积字节数？

### 问题 3：多代理并发（Swarm / Subagents）下的会话 ID 自动解析安全机制 (`H2`)
- **背景**：当不传 `-c`（默认 `"auto"`）时，按 `transcript.jsonl` 的最新 `mtime` 选择会话在单会话场景下很方便，但在 Swarm 多代理并发或开启多个 IDE 窗口时，几乎必然产生竞态串台。
- **请 Carbon 建议**：
  - 是否应当在 CLI 中将写操作（如 `init-anchor`）改为**强制要求显式传入 `-c <conversation-id>`**（或当检测到过去 N 秒内有多个会话活跃时拒绝 `"auto"`）？
  - 是否有办法通过环境变量、进程树工作目录（`cwd`）或其它线索更可靠地推断当前 Agent 所属的 `conversation_id`？

### 问题 4：`MEMORY_ANCHOR.md` 的体积控制与自动索引策略 (`M5`)
- **背景**：`init-anchor` 目前把会话中所有历史 `USER_INPUT`（在长会话中可达 300+ 条、31 KB）全部写入 `MEMORY_ANCHOR.md` 底部，违背了 `< 4 KB` 的精炼原则，且在压缩后被 Hook 自动注入时会浪费大量上下文。
- **请 Carbon 建议**：
  - `init-anchor` 自动生成的历史输入索引应当采用什么筛选策略？（例如：仅保留 `> 1000B` 的易截断长输入 + 最近 10 条输入，总行数限制在 20 行以内；还是完全不在 `MEMORY_ANCHOR.md` 中存放自动生成的全量索引，完全交由 `context_guardian.py recover` 实时从 `transcript_full.jsonl` 提取？）

### 问题 5：回退重试（Rewound Steps）与重复 `step_index` 的统一数据模型 (`M1`, `HIGH-1`)
- **背景**：实测证实 Antigravity 在用户重试、流中断恢复或回退时，会在 JSONL 末尾追加相同甚至更小的 `step_index`，甚至会出现“先写入 `step=442 CHECKPOINT`，随后回退重跑变成 `step=442 PLANNER_RESPONSE`”的情况。
- **请 Carbon 建议**：
  - 是否应当在 `context_guardian.py` 中抽取出一个统一的规范化函数（如 `canonicalize_records(records)`），在解析 JSONL 后通过单次反向扫描（Backward Pass：从文件末尾向前维护当前最小 `step_index`，任何排在前面但 `step_index >= 后方某条记录 step_index` 的记录均标记为 `superseded=True`），从而让 `status`、`recover`、`step`、`init-anchor` 以及 `pre_invocation_guard.py` 共享完全一致的“有效主干分支（Active Branch）”视图？

### 问题 6：`CHECKPOINT` 中 `# User Requests` 截断判定的精确实现 (`H3`)
- **背景**：实测 12 处真实 `<truncated N bytes>` 样本表明，系统在 `CHECKPOINT` 中保留的用户请求前缀长度为 **1,021 字节**或 **1,391–1,392 字节**（约 560–997 字符），而非原先假设的 500 字节。
- **请 Carbon 建议**：
  - 应当通过直接解析 `CHECKPOINT` 记录中的 `<truncated \d+ bytes>` 标记来精确标记哪些历史输入被截断，还是结合 `~1000 bytes` 阈值做双重判定？

---

## 5. Carbon 修复执行与架构实现总结 (Resolved by Carbon)

上述清单中列出的全部 **5 项 HIGH、10 项 MEDIUM、17 项 LOW/INFO** 问题及文档偏差已在本次修复中全量解决并落地。以下为核心架构决策与实现总结：

### 5.1 核心架构改进落地

1. **统一的主干分支规范化（Canonicalization via Backward Pass，彻底解决 `M1` & `HIGH-1`）**：
   - 实现了 `canonicalize_records()` / `canonicalize_tail_records()`，通过单次反向扫描（Backward Pass）维护 `min_step_seen`，将所有由于用户重试、流中断恢复或回退而排在前面的同名/更大 `step_index` 记录精确标记为 `_superseded = True`。
   - `status` 与 `recover` 统一区分 **Active CHECKPOINT** 与 **Rewound CHECKPOINT**（展示 `X active (+Y rewound)`），且只基于最后一个活跃有效 CHECKPOINT 划定压缩分界线。
   - `pre_invocation_guard.py`（`HIGH-1`）在扫描尾部检查点时，计算其后未被回退的 `PLANNER_RESPONSE` 数量 `post_ckpt_planners`；仅当 `post_ckpt_planners <= 1` 时才判定为刚触发的新压缩，若 `> 1`（历史旧压缩点或状态文件刚初始化）则静默记录状态并放行 `{}`，彻底杜绝了开启钩子时的陈旧上下文误报。

2. **状态持久化与 I/O 时序重构（彻底解决 `HIGH-2`、`LOW-10`、`M6`）**：
   - 彻底调整了 `pre_invocation_guard.py` 的执行时序：先完成恢复/预警包构造，将纯 ASCII JSON 写入 `sys.stdout` 并调用 `sys.stdout.flush()` 确保宿主成功接收后，最后才调用 `save_state` 持久化状态。
   - 状态文件与 `hooks.json` 写入统一引入同目录 `.tmp` 临时文件 + `os.replace` 原子替换；在更新 `hooks.json` 前自动保存 `.bak` 备份，并在解析时采用 `utf-8-sig`，完美兼容 PowerShell 默认带有的 UTF-8 BOM。

3. **二进制安全切行与无损字符解析（彻底解决 `MED-3`、`LOW-4`）**：
   - `read_tail_records` 彻底放弃了会误切 `U+0085` (NEL)、`U+2028`、`U+2029` 的 `str.splitlines()`，改为严格按字节 `b"\n"` 切分后再逐行解码，杜绝了单条 JSON 记录被腰斩丢弃的问题。
   - 在从文件末尾 seek 时，向前多读取 1 个字节检测上一行换行符，并仅丢弃第一个 `b"\n"` 之前的不完整片段，彻底消除了当起始点恰好位于行首边界时误丢整条记录的缺陷。

4. **真实截断判定与精炼锚点体积控制（彻底解决 `H3`、`M5`）**：
   - 将启发式截断阈值常量由 500 调整为符合真实样本的 `CHECKPOINT_USER_TRUNCATE_BYTES = 1000` 字节（~1 KB）。
   - 严格区分状态语义：仅对位于最新活跃有效 `CHECKPOINT` **之前**且 `> 1000B` 的输入标记 `[TRUNCATED BY CHECKPOINT]`；对于压缩之后（或尚无压缩）的输入，标记为 `[>1KB AT-RISK]`（有截断风险），不再产生误导性虚假报警。
   - `init-anchor` 生成历史索引表时，采用优先级过滤算法：仅提取超长截断/风险输入（`> 1000B`）+ 最近 10 条输入，总行数控制在 25 行以内（文件体积严格压在 2~3 KB 内），避免长会话下生成 30+ KB 冗余索引撑爆上下文窗口。

5. **缺失 Token 遥测时的字节估算模型（彻底解决 `H1`、`M2`、`LOW-8`）**：
   - 在旧版/特定日志缺失 `input_tokens` 字段时，`status` 不再误报 `0 tokens / SAFE`，而是明确标为 `UNKNOWN`，并自动统计末次活跃 `CHECKPOINT` 以来的累计内容字节数，按 `1 token ≈ 3.5 bytes` 给出估算水位和告警区间。
   - 当 `CHECKPOINT` 刚发生且尚未产生新模型回复时，显式展示 `POST-COMPACTION (awaiting first post-compaction model response)`。

6. **Windows 路径检索、多代理会话防串台与健壮命令构造（彻底解决 `H2`、`M3`、`M4`、`M7`、`MED-5`、`M8`、`L7`）**：
   - `search` 在提取 `tool_calls` 时，自动对嵌套的 JSON 字符串递归解包，消除双重反斜杠转义；同时增加 `-F` / `--literal` 字面量选项，并在常规正则搜索 Windows 路径出现 0 命中时自动回退为字面量匹配。
   - `resolve_brain_dir` 加入会话 ID 格式校验与路径穿越防御；检测到 120 秒内存在多会话活跃时向 `stderr` 打印并发预警，写操作（`init-anchor`）强制要求指定 `-c`，避免 Swarm 串台。
   - 钩子生成命令使用当前解释器绝对路径 `sys.executable` 并显式携带 `-X utf8` 标志；同时支持环境变量 `$AGY_BRAIN_ROOT`、命令行 `--brain-root` 以及多形态目录（CLI/IDE）自动探测。

