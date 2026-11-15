#!/usr/bin/env python3
"""Antigravity PreInvocation lifecycle hook for anti-compaction amnesia prevention.

Reads the PreInvocation JSON payload from stdin, performs a fast tail scan of
`transcript.jsonl` (<256 KB), and acts on two edge-triggered conditions:
  1. New CHECKPOINT compaction detected -> recovers MEMORY_ANCHOR.md, untruncated
     recent/pre-compaction USER_INPUT prompts from transcript_full.jsonl, and the
     pre-compaction PLANNER_RESPONSE snapshot, injecting them as a systemMessage.
  2. Context token watermark crossed (185k / 220k) -> injects an urgent reminder
     to persist constraints, exact paths/metrics, dead ends, and state into
     MEMORY_ANCHOR.md before compaction triggers.

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
    CHECKPOINT_USER_TRUNCATE_BYTES,
    COMPACTION_THRESHOLD_TOKENS,
    CRITICAL_THRESHOLD_TOKENS,
    WARNING_THRESHOLD_TOKENS,
    configure_utf8_stdio,
    extract_user_request,
    format_step_tokens,
    format_tool_calls,
)

TAIL_BYTES = 262_144  # 256 KB for fast PreInvocation tail scan
FULL_READ_LIMIT_BYTES = 2 * 1024 * 1024  # Read whole transcript_full.jsonl if < 2 MB
FULL_TAIL_BYTES = 1024 * 1024  # Otherwise read last 1 MB (expandable to 4 MB)
FULL_FALLBACK_TAIL_BYTES = 4 * 1024 * 1024
ANCHOR_RECENT_GRACE_SEC = 90.0


def read_tail_records(path: Path, tail_bytes: int) -> List[Dict[str, Any]]:
    """Read and parse JSONL lines from the last `tail_bytes` of `path`."""
    if not path.is_file():
        return []
    size = path.stat().st_size
    start = max(0, size - tail_bytes)
    with path.open("rb") as fh:
        fh.seek(start)
        raw = fh.read()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0 and lines:
        # Drop the first partial line when seeking mid-file
        lines = lines[1:]
    records: List[Dict[str, Any]] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def watermark_band(tokens: int) -> int:
    """Return 0 (<185k), 1 (185k-219,999), or 2 (>=220k)."""
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
    """Persist edge-trigger state atomically enough for single-turn hooks."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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
    """Extract untruncated USER_INPUTs and pre-compaction PLANNER_RESPONSE."""
    records = read_full_window(full_path, target_ckpt_key)
    if not records:
        return [], None

    ckpt_pos = -1
    for idx, rec in enumerate(records):
        if rec.get("type") == "CHECKPOINT" and make_checkpoint_key(rec) == target_ckpt_key:
            ckpt_pos = idx

    all_user_inputs: List[Tuple[int, Dict[str, Any]]] = [
        (idx, rec) for idx, rec in enumerate(records) if rec.get("type") == "USER_INPUT"
    ]

    chosen_positions: List[int] = []
    # Always include the latest 2 overall USER_INPUT steps
    for idx, _ in all_user_inputs[-2:]:
        chosen_positions.append(idx)

    # Also ensure the USER_INPUT immediately preceding the latest CHECKPOINT is included
    if ckpt_pos >= 0:
        pre_ckpt_inputs = [idx for idx, _ in all_user_inputs if idx < ckpt_pos]
        if pre_ckpt_inputs:
            last_pre_idx = pre_ckpt_inputs[-1]
            if last_pre_idx not in chosen_positions:
                chosen_positions.append(last_pre_idx)
            if len(pre_ckpt_inputs) >= 2 and len(chosen_positions) < 3:
                prev_pre_idx = pre_ckpt_inputs[-2]
                if prev_pre_idx not in chosen_positions:
                    chosen_positions.append(prev_pre_idx)

    chosen_positions = sorted(set(chosen_positions))
    recovered_inputs: List[Dict[str, Any]] = []
    seen_dedup = set()
    for pos in chosen_positions:
        rec = records[pos]
        prompt = extract_user_request(rec.get("content"))
        dedup_key = (rec.get("step_index"), prompt)
        if dedup_key in seen_dedup:
            continue
        seen_dedup.add(dedup_key)
        utf8_bytes = len(prompt.encode("utf-8"))
        recovered_inputs.append(
            {
                "step_index": rec.get("step_index"),
                "created_at": rec.get("created_at", ""),
                "bytes": utf8_bytes,
                "truncated_in_checkpoint": utf8_bytes > CHECKPOINT_USER_TRUNCATE_BYTES,
                "prompt": clip(prompt, 4000),
            }
        )

    pre_planner: Optional[Dict[str, Any]] = None
    search_end = ckpt_pos if ckpt_pos >= 0 else len(records)
    for pos in range(search_end - 1, -1, -1):
        rec = records[pos]
        if rec.get("type") == "PLANNER_RESPONSE":
            pre_planner = rec
            break

    return recovered_inputs, pre_planner


def build_post_compaction_output(
    conversation_id: str,
    artifact_dir: Path,
    full_path: Path,
    ckpt_step: int,
    ckpt_key: str,
) -> Dict[str, Any]:
    """Construct injectSteps JSON after a newly detected CHECKPOINT compaction."""
    recovered_inputs, pre_planner = extract_compaction_recovery_bundle(full_path, ckpt_key)
    anchor_path = artifact_dir / "MEMORY_ANCHOR.md"
    guardian_posix = (SCRIPT_DIR / "context_guardian.py").as_posix()

    sections: List[str] = [
        f"[ANTI-COMPACTION GUARD] 检测到当前会话刚刚在 step_index={ckpt_step} 触发了有损上下文压缩 (CHECKPOINT)。",
        "严禁仅凭压缩摘要 `<summary>` 或被截断至 ~500B 的 `# User Requests` 猜测细节；以下是从磁盘无损恢复的关键记忆：",
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
                f"[WARNING] 尚未创建 `{anchor_path}`。请立即用 `init-anchor` 并补充关键约束、路径、死胡同与当前状态，防止后续再次压缩丢失。",
                "",
            ]
        )

    sections.append("### 2. 从 `transcript_full.jsonl` 无损恢复的近期/压缩前完整用户输入")
    if recovered_inputs:
        for item in recovered_inputs:
            trunc_note = " [CHECKPOINT已截断此输入(>500B)]" if item["truncated_in_checkpoint"] else ""
            sections.append(
                f"- **USER_INPUT step={item['step_index']}** ({item['created_at']}, {item['bytes']} bytes){trunc_note}:"
            )
            sections.append(item["prompt"])
            sections.append("")
    else:
        sections.append("(未检索到近期 USER_INPUT 记录)")
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
            "### 4. 按需无损回查命令（禁止用 read_file 裸读整个 transcript_full.jsonl）",
            f'- 完整恢复报告: `python "{guardian_posix}" recover -c "{conversation_id}"`',
            f'- 关键词检索: `python "{guardian_posix}" search "<关键词>" -c "{conversation_id}"`',
            f'- 精确读取某步: `python "{guardian_posix}" step <step_index> -c "{conversation_id}"`',
        ]
    )

    user_md = (
        f"> 🛡️ **防失忆护盾 (Anti-Compaction Guard)**：检测到会话刚触发有损压缩（Step #{ckpt_step}），"
        f"已自动从 `transcript_full.jsonl` 与 `MEMORY_ANCHOR.md` 无损恢复完整用户输入与核心记忆锚点。"
    )
    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": "\n".join(sections),
                    "metadata": {"user_markdown": user_md},
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
    """Construct injectSteps JSON when context crosses 185k / 220k warning bands."""
    anchor_path = artifact_dir / "MEMORY_ANCHOR.md"
    guardian_posix = (SCRIPT_DIR / "context_guardian.py").as_posix()
    pct = (latest_tokens / COMPACTION_THRESHOLD_TOKENS) * 100.0
    level = "CRITICAL (即将触发压缩)" if band >= 2 else "WARNING (高水位预警)"

    sys_msg = "\n".join(
        [
            f"[CONTEXT WATERMARK {level}] 当前上下文已达 {latest_tokens:,} / {COMPACTION_THRESHOLD_TOKENS:,} tokens ({pct:.1f}%)。",
            "Antigravity 即将或很快会在 ~250k tokens 触发有损 CHECKPOINT 压缩（仅保留泛化摘要，且历史用户输入将被截断至 ~500 字节）。",
            f"请立即创建或更新 `{anchor_path}`，确保以下 5 项完整固化（如尚未初始化可先运行 `init-anchor`）：",
            "1. 铁律与负面约束（用户禁止事项、不可改动的文件/参数）",
            "2. 精确事实与参数账本（绝对路径、偏移量、命令、精确指标）",
            "3. 已排除死胡同（失败方案及原因，防压缩后重复踩坑）",
            "4. 里程碑与下一步唯一精确动作",
            "5. 关键历史步骤 step_index 书签",
            f'初始化/检查命令：`python "{guardian_posix}" init-anchor -c "{conversation_id}"`',
        ]
    )
    user_md = (
        f"> ⚠️ **上下文高水位预警**：当前上下文已达 **{latest_tokens:,} tokens**"
        f"（逼近 ~250k 有损压缩阈值），已提醒 Agent 将关键约束与进度无损固化至 `MEMORY_ANCHOR.md`。"
    )
    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": sys_msg,
                    "metadata": {"user_markdown": user_md},
                }
            }
        ]
    }


def run_guard(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate PreInvocation payload and return hook response dict."""
    transcript_str = payload.get("transcriptPath")
    artifact_str = payload.get("artifactDirectoryPath")
    conversation_id = str(payload.get("conversationId") or "auto")
    if not transcript_str or not artifact_str:
        return {}

    transcript_path = Path(transcript_str)
    artifact_dir = Path(artifact_str)
    if not transcript_path.is_file():
        return {}

    tail_records = read_tail_records(transcript_path, TAIL_BYTES)
    if not tail_records:
        return {}

    latest_tokens = 0
    latest_checkpoint: Optional[Dict[str, Any]] = None
    for rec in tail_records:
        if rec.get("type") == "PLANNER_RESPONSE":
            tok = format_step_tokens(rec)
            if tok > 0:
                latest_tokens = tok
        elif rec.get("type") == "CHECKPOINT":
            latest_checkpoint = rec

    current_band = watermark_band(latest_tokens)
    state_path = artifact_dir / "scratch" / ".compaction_guard_state.json"
    state = load_state(state_path)

    # Edge-triggered Case A: new CHECKPOINT compaction detected
    if latest_checkpoint is not None:
        ckpt_step = int(latest_checkpoint.get("step_index") if latest_checkpoint.get("step_index") is not None else -1)
        ckpt_key = make_checkpoint_key(latest_checkpoint)
        prev_step = int(state.get("last_handled_checkpoint_step", -1))
        prev_key = str(state.get("last_handled_checkpoint_key", ""))
        if ckpt_step > prev_step or ckpt_key != prev_key:
            state["last_handled_checkpoint_step"] = ckpt_step
            state["last_handled_checkpoint_key"] = ckpt_key
            # Align watermark band so the immediately following turn does not false-alarm
            # on pre-compaction token counts; subsequent turns drop back to band 0.
            state["last_warned_band"] = current_band
            save_state(state_path, state)

            full_path = transcript_path.parent / "transcript_full.jsonl"
            if not full_path.is_file():
                full_path = transcript_path
            return build_post_compaction_output(
                conversation_id=conversation_id,
                artifact_dir=artifact_dir,
                full_path=full_path,
                ckpt_step=ckpt_step,
                ckpt_key=ckpt_key,
            )

    last_warned_band = int(state.get("last_warned_band", 0))

    # Reset downward silently once post-compaction context drops below previous watermark
    if current_band < last_warned_band:
        state["last_warned_band"] = current_band
        save_state(state_path, state)
        return {}

    # Edge-triggered Case B: context crossed into a higher watermark band (>= 185k or >= 220k)
    if current_band > last_warned_band and latest_tokens >= WARNING_THRESHOLD_TOKENS:
        state["last_warned_band"] = current_band
        save_state(state_path, state)

        anchor_path = artifact_dir / "MEMORY_ANCHOR.md"
        if anchor_path.is_file():
            age_sec = time.time() - anchor_path.stat().st_mtime
            if 0 <= age_sec <= ANCHOR_RECENT_GRACE_SEC:
                return {}

        return build_watermark_output(
            conversation_id=conversation_id,
            artifact_dir=artifact_dir,
            latest_tokens=latest_tokens,
            band=current_band,
        )

    return {}


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
        result = run_guard(payload)
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        return 0
    except Exception:
        sys.stdout.write("{}\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
