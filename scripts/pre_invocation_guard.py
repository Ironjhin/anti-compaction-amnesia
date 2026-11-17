#!/usr/bin/env python3
"""Antigravity PreInvocation lifecycle hook for anti-compaction amnesia prevention.

Reads the PreInvocation JSON payload from stdin, performs a fast tail scan of
`transcript.jsonl` (<256 KB), and acts on two edge-triggered conditions:
  1. New CHECKPOINT compaction detected -> recovers MEMORY_ANCHOR.md, untruncated
     recent/truncated USER_INPUT prompts from transcript_full.jsonl, and the
     pre-compaction PLANNER_RESPONSE snapshot, injecting them as a systemMessage.
  2. Context token watermark crossed (380k / 450k) on interactive user sessions ->
     injects a non-disruptive, capability-aware reminder to persist constraints,
     exact paths/metrics, dead ends, and state into MEMORY_ANCHOR.md.

Fail-safe by design: any unexpected exception outputs `{}` and exits 0 so it
never blocks or breaks the agent execution loop.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from context_guardian import (  # noqa: E402
    BYTES_PER_TOKEN_ESTIMATE,
    CHECKPOINT_USER_TRUNCATE_BYTES,
    COMPACTION_THRESHOLD_TOKENS,
    CRITICAL_THRESHOLD_TOKENS,
    WARNING_THRESHOLD_TOKENS,
    atomic_write_text,
    configure_utf8_stdio,
    estimate_record_bytes,
    extract_user_request,
    format_step_tokens,
    format_tool_calls,
    is_slash_compact_prompt,
)

TAIL_BYTES = 262_144  # 256 KB for fast PreInvocation tail scan
FULL_READ_LIMIT_BYTES = 2 * 1024 * 1024  # Read whole transcript_full.jsonl if < 2 MB
FULL_TAIL_BYTES = 1024 * 1024  # Otherwise read last 1 MB (expandable to 4 MB)
FULL_FALLBACK_TAIL_BYTES = 4 * 1024 * 1024
ANCHOR_RECENT_GRACE_SEC = 90.0
ANCHOR_CRITICAL_GRACE_SEC = 30.0


def read_tail_records(path: Path, tail_bytes: int) -> List[Dict[str, Any]]:
    """Read and parse JSONL lines from the last `tail_bytes` of `path`.

    Splits strictly on byte newline `b'\\n'` (not Unicode linebreaks like U+0085)
    and cleanly handles file-boundary seek offsets (MED-3, LOW-4).
    """
    if not path.is_file():
        return []
    size = path.stat().st_size
    if size == 0:
        return []

    with path.open("rb") as fh:
        if size > tail_bytes:
            # LOW-4: Seek 1 byte earlier so we can reliably detect the preceding newline
            fh.seek(size - tail_bytes - 1)
            raw = fh.read()
            nl = raw.find(b"\n")
            raw = raw[nl + 1 :] if nl != -1 else b""
        else:
            raw = fh.read()

    # MED-3: Split on b"\n" instead of str.splitlines() to avoid breaking on U+0085/U+2028/U+2029
    raw_lines = raw.split(b"\n")
    records: List[Dict[str, Any]] = []
    for raw_line in raw_lines:
        line_str = raw_line.decode("utf-8", errors="replace").strip()
        if not line_str:
            continue
        try:
            obj = json.loads(line_str)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def canonicalize_tail_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Tag superseded/rewound steps in tail records via backward pass."""
    min_step_seen = float("inf")
    for rec in reversed(records):
        step_idx = rec.get("step_index")
        if isinstance(step_idx, int):
            if step_idx >= min_step_seen:
                rec["_superseded"] = True
            else:
                rec["_superseded"] = False
                min_step_seen = step_idx
        else:
            rec["_superseded"] = False
    return records


def watermark_band(tokens: int) -> int:
    """Return 0 (<380k), 1 (380k-449,999), or 2 (>=450k)."""
    if tokens >= CRITICAL_THRESHOLD_TOKENS:
        return 2
    if tokens >= WARNING_THRESHOLD_TOKENS:
        return 1
    return 0


def make_checkpoint_key(rec: Dict[str, Any]) -> str:
    """Unique identifier for a CHECKPOINT event resilient to retried step indices."""
    return f"{rec.get('step_index')}|{rec.get('created_at', '')}"


def load_state(state_path: Path) -> Dict[str, Any]:
    """Load edge-trigger state JSON safely."""
    default = {
        "last_handled_checkpoint_step": -1,
        "last_handled_checkpoint_key": "",
        "last_warned_band": 0,
    }
    if not state_path.is_file():
        return default
    try:
        data = json.loads(state_path.read_text(encoding="utf-8", errors="replace"))
        if isinstance(data, dict):
            default.update(data)
    except Exception:
        pass
    return default


def save_state(state_path: Path, state: Dict[str, Any]) -> None:
    """Persist edge-trigger state atomically via PID-unique .tmp and os.replace (LOW-10)."""
    try:
        atomic_write_text(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        pass


def clip(text: str, limit: int) -> str:
    """Clip long strings cleanly without breaking UTF-8."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [clipped {len(text) - limit} chars; use context_guardian.py step/recover for full text]"


def read_full_window(full_path: Path, target_ckpt_key: str) -> List[Dict[str, Any]]:
    """Load transcript_full.jsonl efficiently, ensuring the target checkpoint is included."""
    size = full_path.stat().st_size
    if size <= FULL_READ_LIMIT_BYTES:
        return read_tail_records(full_path, size + 1)

    records = read_tail_records(full_path, FULL_TAIL_BYTES)
    if any(r.get("type") == "CHECKPOINT" and make_checkpoint_key(r) == target_ckpt_key for r in records):
        return records
    return read_tail_records(full_path, FULL_FALLBACK_TAIL_BYTES)


def extract_compaction_recovery_bundle(
    full_path: Path,
    target_ckpt_key: str,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Extract untruncated active USER_INPUTs (or worker SYSTEM_MESSAGEs) and pre-compaction PLANNER_RESPONSE."""
    records = read_full_window(full_path, target_ckpt_key)
    if not records:
        return [], None

    canonicalize_tail_records(records)

    ckpt_pos = -1
    for idx, rec in enumerate(records):
        if (
            not rec.get("_superseded")
            and rec.get("type") == "CHECKPOINT"
            and make_checkpoint_key(rec) == target_ckpt_key
        ):
            ckpt_pos = idx

    active_user_inputs: List[Tuple[int, Dict[str, Any], str, int]] = []
    for idx, rec in enumerate(records):
        if rec.get("_superseded") or rec.get("type") != "USER_INPUT":
            continue
        prompt = extract_user_request(rec.get("content"))
        utf8_bytes = len(prompt.encode("utf-8", errors="replace"))
        active_user_inputs.append((idx, rec, prompt, utf8_bytes))

    recovered_inputs: List[Dict[str, Any]] = []

    if active_user_inputs:
        substantive = [item for item in active_user_inputs if not is_slash_compact_prompt(item[2])]
        pool = substantive if substantive else active_user_inputs

        chosen_positions: List[int] = []
        # 1) Latest 2 substantive USER_INPUTs overall
        for idx, _, _, _ in pool[-2:]:
            chosen_positions.append(idx)

        if ckpt_pos >= 0:
            # 2) Substantive USER_INPUT immediately preceding the CHECKPOINT
            pre_pool = [idx for idx, _, _, _ in pool if idx < ckpt_pos]
            for idx in pre_pool[-2:]:
                chosen_positions.append(idx)

            # 3) Up to 2 most recent >1KB truncated pre-checkpoint USER_INPUTs (even if older than short turns!)
            pre_truncated = [
                idx
                for idx, _, _, b_len in active_user_inputs
                if idx < ckpt_pos and b_len > CHECKPOINT_USER_TRUNCATE_BYTES
            ]
            for idx in pre_truncated[-2:]:
                chosen_positions.append(idx)

        seen_dedup = set()
        for pos in sorted(set(chosen_positions)):
            rec = records[pos]
            prompt = extract_user_request(rec.get("content"))
            dedup_key = (rec.get("step_index"), prompt)
            if dedup_key in seen_dedup:
                continue
            seen_dedup.add(dedup_key)
            utf8_bytes = len(prompt.encode("utf-8", errors="replace"))
            is_before_ckpt = (ckpt_pos >= 0) and (pos < ckpt_pos)
            recovered_inputs.append(
                {
                    "kind": "USER_INPUT",
                    "step_index": rec.get("step_index"),
                    "created_at": rec.get("created_at", ""),
                    "bytes": utf8_bytes,
                    "truncated_in_checkpoint": is_before_ckpt and (utf8_bytes > CHECKPOINT_USER_TRUNCATE_BYTES),
                    "at_risk": (not is_before_ckpt) and (utf8_bytes > CHECKPOINT_USER_TRUNCATE_BYTES),
                    "prompt": clip(prompt, 4000),
                }
            )
    elif ckpt_pos > 0:
        # Autonomous Swarm Worker / Subagent fallback (no USER_INPUT steps in transcript):
        # recover initial assignment / recent pre-checkpoint SYSTEM_MESSAGE steps.
        sys_msgs = [
            (idx, r)
            for idx, r in enumerate(records[:ckpt_pos])
            if not r.get("_superseded") and r.get("type") == "SYSTEM_MESSAGE" and r.get("content")
        ]
        chosen_sys: List[Tuple[int, Dict[str, Any]]] = []
        if sys_msgs:
            chosen_sys.append(sys_msgs[0])
            if len(sys_msgs) > 1:
                chosen_sys.append(sys_msgs[-1])
        for _pos, rec in chosen_sys:
            content = str(rec.get("content") or "").strip()
            utf8_bytes = len(content.encode("utf-8", errors="replace"))
            recovered_inputs.append(
                {
                    "kind": "SYSTEM_MESSAGE",
                    "step_index": rec.get("step_index"),
                    "created_at": rec.get("created_at", ""),
                    "bytes": utf8_bytes,
                    "truncated_in_checkpoint": False,
                    "at_risk": False,
                    "prompt": clip(content, 2500),
                }
            )

    # LOW-9: Only look for active pre_planner if ckpt_pos was found in window
    pre_planner: Optional[Dict[str, Any]] = None
    if ckpt_pos >= 0:
        for pos in range(ckpt_pos - 1, -1, -1):
            rec = records[pos]
            if not rec.get("_superseded") and rec.get("type") == "PLANNER_RESPONSE":
                pre_planner = rec
                break

    return recovered_inputs, pre_planner


def build_post_compaction_output(
    conversation_id: str,
    artifact_dir: Path,
    full_path: Path,
    ckpt_step: int,
    ckpt_key: str,
    is_lossy_source: bool = False,
) -> Dict[str, Any]:
    """Construct official PreInvocation injectSteps JSON after a newly detected CHECKPOINT compaction."""
    recovered_inputs, pre_planner = extract_compaction_recovery_bundle(full_path, ckpt_key)
    anchor_path = artifact_dir / "MEMORY_ANCHOR.md"
    guardian_posix = (SCRIPT_DIR / "context_guardian.py").as_posix()

    source_desc = "transcript.jsonl (有损回退)" if is_lossy_source else "transcript_full.jsonl"

    sections: List[str] = [
        f"[ANTI-COMPACTION GUARD] 检测到当前会话刚刚在 step_index={ckpt_step} 触发了有损上下文压缩 (CHECKPOINT)。",
        "严禁仅凭压缩摘要 `<summary>` 或被截断至 ~1000B 的 `# User Requests` 猜测细节；以下是从磁盘无损恢复的关键记忆：",
        "",
    ]

    if anchor_path.is_file():
        anchor_text = clip(anchor_path.read_text(encoding="utf-8", errors="replace").strip(), 6000)
        sections.extend(
            [
                f"### 1. 已固化记忆锚点 (`{anchor_path}`)",
                anchor_text,
                "",
            ]
        )
    else:
        sections.extend(
            [
                "### 1. 记忆锚点状态",
                f"[NOTE] 尚未创建 `{anchor_path}`。若当前会话具备写入与命令权限（非只读子代理/非只读规划阶段），可在当前步骤收尾时运行 `init-anchor` 固化关键约束与死胡同。",
                "",
            ]
        )

    sections.append(f"### 2. 从 `{source_desc}` 恢复的近期/压缩前完整输入")
    if recovered_inputs:
        for item in recovered_inputs:
            trunc_note = ""
            if item["truncated_in_checkpoint"]:
                trunc_note = " [CHECKPOINT已截断此输入(>1KB)]"
            elif item.get("at_risk"):
                trunc_note = " [>1KB 易截断]"

            kind = item.get("kind", "USER_INPUT")
            sections.append(
                f"- **{kind} step={item['step_index']}** ({item['created_at']}, {item['bytes']} bytes){trunc_note}:"
            )
            sections.append(item["prompt"])
            sections.append("")
    else:
        sections.append("(未检索到近期输入记录)")
        sections.append("")

    if pre_planner:
        sections.append(
            f"### 3. 压缩前最后一次模型执行快照 (PLANNER_RESPONSE step={pre_planner.get('step_index')})"
        )
        parts: List[str] = []
        if pre_planner.get("thinking"):
            parts.append("Thinking: " + clip(str(pre_planner["thinking"]), 500))
        if pre_planner.get("tool_calls"):
            parts.append("ToolCalls: " + clip(format_tool_calls(pre_planner["tool_calls"]), 500))
        if pre_planner.get("content"):
            parts.append("Content: " + clip(str(pre_planner["content"]), 700))
        combined = "\n".join(parts) if parts else "(empty)"
        sections.append(clip(combined, 1500))
        sections.append("")

    sections.extend(
        [
            "### 4. 按需无损回查命令（如具备命令行工具权限；禁止用 read_file 裸读整个 transcript_full.jsonl）",
            f'- 完整恢复报告: `python "{guardian_posix}" recover -c "{conversation_id}"`',
            f'- 关键词检索: `python "{guardian_posix}" search "<关键词>" -c "{conversation_id}"`',
            f'- 精确读取某步: `python "{guardian_posix}" step <step_index> -c "{conversation_id}"`',
        ]
    )

    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": "\n".join(sections),
                }
            }
        ]
    }


def build_watermark_output(
    conversation_id: str,
    artifact_dir: Path,
    latest_tokens: int,
    band: int,
) -> Dict[str, Any]:
    """Construct official PreInvocation injectSteps JSON when context crosses 380k / 450k warning bands."""
    anchor_path = artifact_dir / "MEMORY_ANCHOR.md"
    guardian_posix = (SCRIPT_DIR / "context_guardian.py").as_posix()
    pct = (latest_tokens / COMPACTION_THRESHOLD_TOKENS) * 100.0
    level = "CRITICAL (即将触发压缩)" if band >= 2 else "WARNING (高水位预警)"

    sys_msg = "\n".join(
        [
            f"[CONTEXT WATERMARK {level}] 当前上下文已达 ~{latest_tokens:,} / {COMPACTION_THRESHOLD_TOKENS:,} tokens ({pct:.1f}%)。",
            "Antigravity 即将或很快会在 ~512k tokens 触发有损 CHECKPOINT 压缩（仅保留泛化摘要，且历史用户输入将被截断至 ~1000 字节）。",
            f"请在不打断当前正在执行的工具链或子代理协调的前提下，于本轮步骤收尾时创建或更新 `{anchor_path}`，确保以下 5 项完整固化：",
            "1. 铁律与负面约束（用户禁止事项、不可改动的文件/参数）",
            "2. 精确事实与参数账本（绝对路径、偏移量、命令、精确指标）",
            "3. 已排除死胡同（失败方案及原因，防压缩后重复踩坑）",
            "4. 里程碑与下一步唯一精确动作",
            "5. 关键历史步骤 step_index 书签",
            f'初始化命令：`python "{guardian_posix}" init-anchor -c "{conversation_id}"`',
            "（注：若当前处于只读模式如 Plan Mode、`research`/`browser` 子代理或无写入权限，请忽略文件写入指令并直接继续当前主线任务。）",
        ]
    )
    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": sys_msg,
                }
            }
        ]
    }


def run_guard(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Tuple[Path, Dict[str, Any]]]]:
    """Evaluate PreInvocation payload.

    Returns:
        (response_dict, optional_state_to_persist)
        HIGH-2: State is only persisted after output construction & stdout flush succeed.
    """
    transcript_str = payload.get("transcriptPath")
    artifact_str = payload.get("artifactDirectoryPath")
    conversation_id = str(payload.get("conversationId") or "auto")
    if not transcript_str or not artifact_str:
        return {}, None

    transcript_path = Path(transcript_str)
    artifact_dir = Path(artifact_str)
    if not transcript_path.is_file():
        return {}, None

    tail_records = read_tail_records(transcript_path, TAIL_BYTES)
    if not tail_records:
        return {}, None

    canonicalize_tail_records(tail_records)

    latest_tokens = 0
    post_planner_bytes = 0
    latest_checkpoint: Optional[Dict[str, Any]] = None
    last_ckpt_idx: Optional[int] = None
    has_explicit_user_input = bool(str(payload.get("lastUserInput") or "").strip())

    for idx, rec in enumerate(tail_records):
        if rec.get("_superseded"):
            continue
        rtype = rec.get("type")
        if rtype == "USER_INPUT":
            has_explicit_user_input = True
        if rtype == "PLANNER_RESPONSE":
            tok = format_step_tokens(rec)
            if tok > 0:
                latest_tokens = tok
                post_planner_bytes = 0
            elif latest_tokens > 0:
                post_planner_bytes += estimate_record_bytes(rec)
        elif rtype == "CHECKPOINT":
            latest_checkpoint = rec
            last_ckpt_idx = idx
            # Reset pre-compaction token count across compaction boundary!
            latest_tokens = 0
            post_planner_bytes = 0
        elif latest_tokens > 0:
            post_planner_bytes += estimate_record_bytes(rec)

    effective_tokens = (
        latest_tokens + int(post_planner_bytes / BYTES_PER_TOKEN_ESTIMATE)
        if latest_tokens > 0
        else 0
    )
    current_band = watermark_band(effective_tokens)
    state_path = artifact_dir / "scratch" / ".compaction_guard_state.json"
    state = load_state(state_path)

    # Edge-triggered Case A: new active CHECKPOINT compaction detected
    if latest_checkpoint is not None and last_ckpt_idx is not None:
        ckpt_step = int(latest_checkpoint.get("step_index") if latest_checkpoint.get("step_index") is not None else -1)
        ckpt_key = make_checkpoint_key(latest_checkpoint)
        prev_step = int(state.get("last_handled_checkpoint_step", -1))
        prev_key = str(state.get("last_handled_checkpoint_key", ""))

        if ckpt_step > prev_step or ckpt_key != prev_key:
            # HIGH-1: Check if this is truly a newly triggered checkpoint or a stale checkpoint
            # from long ago (e.g. hook enabled mid-session or state reset).
            post_ckpt_planners = [
                r for r in tail_records[last_ckpt_idx + 1 :]
                if r.get("type") == "PLANNER_RESPONSE" and not r.get("_superseded")
            ]

            if len(post_ckpt_planners) > 1:
                state["last_handled_checkpoint_step"] = ckpt_step
                state["last_handled_checkpoint_key"] = ckpt_key
                state["last_warned_band"] = current_band
                save_state(state_path, state)
                return {}, None

            full_path = transcript_path.parent / "transcript_full.jsonl"
            is_lossy = False
            if not full_path.is_file():
                full_path = transcript_path
                is_lossy = True

            output = build_post_compaction_output(
                conversation_id=conversation_id,
                artifact_dir=artifact_dir,
                full_path=full_path,
                ckpt_step=ckpt_step,
                ckpt_key=ckpt_key,
                is_lossy_source=is_lossy,
            )

            state["last_handled_checkpoint_step"] = ckpt_step
            state["last_handled_checkpoint_key"] = ckpt_key
            state["last_warned_band"] = current_band
            return output, (state_path, state)

    last_warned_band = int(state.get("last_warned_band", 0))

    # LOW-8: Only consider band resets if we actually saw valid post-compaction token telemetry
    if effective_tokens > 0 and current_band < last_warned_band:
        state["last_warned_band"] = current_band
        save_state(state_path, state)
        return {}, None

    # Edge-triggered Case B: context crossed into a higher watermark band (>= 380k or >= 450k)
    if current_band > last_warned_band and effective_tokens >= WARNING_THRESHOLD_TOKENS:
        # Autonomous Swarm Workers / Monitors have no USER_INPUT records: never interrupt
        # their tight post_message + swarm_wait protocol with init-anchor detours!
        if not has_explicit_user_input:
            state["last_warned_band"] = current_band
            save_state(state_path, state)
            return {}, None

        # LOW-7: Check anchor grace window BEFORE advancing last_warned_band
        anchor_path = artifact_dir / "MEMORY_ANCHOR.md"
        if anchor_path.is_file():
            age_sec = time.time() - anchor_path.stat().st_mtime
            grace_sec = ANCHOR_CRITICAL_GRACE_SEC if current_band >= 2 else ANCHOR_RECENT_GRACE_SEC
            if 0 <= age_sec <= grace_sec:
                return {}, None

        output = build_watermark_output(
            conversation_id=conversation_id,
            artifact_dir=artifact_dir,
            latest_tokens=effective_tokens,
            band=current_band,
        )

        state["last_warned_band"] = current_band
        return output, (state_path, state)

    return {}, None


def main() -> int:
    configure_utf8_stdio()
    try:
        raw_input = sys.stdin.read()
        if not raw_input or not raw_input.strip():
            sys.stdout.write("{}\n")
            return 0
        payload = json.loads(raw_input)
        if not isinstance(payload, dict):
            sys.stdout.write("{}\n")
            return 0

        result, pending_state = run_guard(payload)

        # LOW-6: ensure_ascii=True produces clean pure-ASCII JSON (\uXXXX), 100% immune to Windows codepage issues
        output_str = json.dumps(result, ensure_ascii=True)
        sys.stdout.write(output_str + "\n")
        sys.stdout.flush()

        # HIGH-2: Only save state after successful output serialization & flush
        if pending_state is not None:
            state_p, state_obj = pending_state
            save_state(state_p, state_obj)

        return 0
    except Exception:
        sys.stdout.write("{}\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
