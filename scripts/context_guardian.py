#!/usr/bin/env python3
"""Antigravity anti-compaction amnesia guardian CLI.

Inspects Antigravity conversation transcripts (`transcript.jsonl` and lossless
`transcript_full.jsonl`), monitors token watermarks near the ~512k compaction
threshold, initializes and displays `MEMORY_ANCHOR.md`, recovers truncated user
inputs and pre-compaction model context, and manages the global `PreInvocation`
guard hook in `~/.gemini/config/hooks.json`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

COMPACTION_THRESHOLD_TOKENS = 512_000
WATCH_THRESHOLD_TOKENS = 307_000
WARNING_THRESHOLD_TOKENS = 380_000
CRITICAL_THRESHOLD_TOKENS = 450_000
CHECKPOINT_USER_TRUNCATE_BYTES = 1000
BYTES_PER_TOKEN_ESTIMATE = 3.5

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_BRAIN_ROOT = Path.home() / ".gemini" / "antigravity" / "brain"
DEFAULT_CONFIG_DIR = Path.home() / ".gemini" / "config"
DEFAULT_HOOKS_PATH = DEFAULT_CONFIG_DIR / "hooks.json"
DEFAULT_TEMPLATE_PATH = SKILL_DIR / "references" / "memory_anchor_template.md"
GUARD_SCRIPT_PATH = SKILL_DIR / "scripts" / "pre_invocation_guard.py"
HOOK_KEY = "anti-compaction-amnesia"

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_BOILERPLATE_TAG_RE = re.compile(
    r"<(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>.*?</(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>",
    re.DOTALL,
)
_VALID_CID_RE = re.compile(r"^[0-9a-fA-F-]{4,64}$")


def configure_utf8_stdio() -> None:
    """Ensure stdin/stdout/stderr use UTF-8 so Windows shells handle CJK/emoji safely."""
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def extract_user_request(raw_content: Optional[str]) -> str:
    """Extract clean user prompt text from a USER_INPUT step's content."""
    if not raw_content:
        return ""
    match = _USER_REQUEST_RE.search(raw_content)
    if match:
        return match.group(1).strip()
    cleaned = _BOILERPLATE_TAG_RE.sub("", raw_content)
    return cleaned.strip()


def truncate_text(text: str, max_chars: int) -> str:
    """Truncate text cleanly while noting how many characters were omitted."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n... [truncated {omitted} chars; pass --max-chars 0 or larger to view all]"


def single_line_preview(text: str, max_chars: int = 120) -> str:
    """Collapse whitespace into a single-line preview."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - 3] + "..."


def resolve_brain_root(custom_root: Optional[Path] = None) -> Path:
    """Resolve the brain root directory across standard Antigravity variants."""
    if custom_root is not None:
        return custom_root

    env_root = os.environ.get("AGY_BRAIN_ROOT")
    if env_root:
        p = Path(env_root).expanduser()
        if p.exists():
            return p

    candidates = [
        DEFAULT_BRAIN_ROOT,
        Path.home() / ".gemini" / "antigravity-cli" / "brain",
        Path.home() / ".gemini" / "antigravity-ide" / "brain",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate

    return DEFAULT_BRAIN_ROOT


def resolve_brain_dir(
    conversation_id: Optional[str] = None,
    brain_root: Optional[Path] = None,
    is_write_op: bool = False,
) -> Tuple[str, Path]:
    """Resolve the target conversation's brain directory.

    When `conversation_id` is omitted or `"auto"`, selects the conversation folder
    whose `.system_generated/logs/transcript.jsonl` has the most recent mtime.
    If multiple conversations were modified within 120s, prints a warning to stderr;
    for write operations (such as init-anchor), requires explicit `-c` to prevent
    accidental cross-session overwrites.
    """
    root = resolve_brain_root(brain_root)
    if not root.exists():
        raise FileNotFoundError(f"Brain directory does not exist: {root}")

    if conversation_id and conversation_id.lower() != "auto":
        # Security & path traversal check (L7)
        if not _VALID_CID_RE.match(conversation_id):
            raise ValueError(
                f"Invalid conversation ID format '{conversation_id}'. "
                f"Expected alphanumeric and hyphen characters (4-64 chars)."
            )

        exact = (root / conversation_id).resolve()
        try:
            exact.relative_to(root.resolve())
        except ValueError:
            raise ValueError(f"Directory traversal detected in conversation ID: {conversation_id}")

        if exact.is_dir():
            return exact.name, exact

        matches = [
            d
            for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".") and d.name.startswith(conversation_id)
        ]
        if len(matches) == 1:
            return matches[0].name, matches[0]
        if len(matches) > 1:
            names = ", ".join(m.name for m in matches[:5])
            raise ValueError(f"Ambiguous conversation ID prefix '{conversation_id}': {names}")
        raise FileNotFoundError(f"Conversation directory not found for ID: {conversation_id} in {root}")

    candidates: List[Tuple[float, Path]] = []
    now = time.time()
    recent_active: List[str] = []

    for entry in root.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        compact_log = entry / ".system_generated" / "logs" / "transcript.jsonl"
        if compact_log.is_file():
            mtime = compact_log.stat().st_mtime
            candidates.append((mtime, entry))
            if (now - mtime) <= 120.0:
                recent_active.append(entry.name)

    if not candidates:
        raise FileNotFoundError(f"No conversation transcripts found under {root}")

    # Warn if multiple sessions are active concurrently (H2)
    if len(recent_active) > 1:
        active_list = ", ".join(recent_active[:4])
        print(
            f"[WARNING] Multiple conversations recently active ({active_list}). "
            f"In multi-agent/swarm mode, pass -c <conversation-id> explicitly to avoid cross-talk.",
            file=sys.stderr,
        )
        if is_write_op:
            raise ValueError(
                f"Multiple active conversations detected in the last 120s ({active_list}). "
                f"For write operations (like init-anchor), please pass --conversation-id / -c explicitly."
            )

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = candidates[0][1]
    return selected.name, selected


def get_transcript_paths(brain_dir: Path) -> Tuple[Path, Path]:
    """Return `(compact_path, full_path)` for a conversation brain directory."""
    logs_dir = brain_dir / ".system_generated" / "logs"
    return logs_dir / "transcript.jsonl", logs_dir / "transcript_full.jsonl"


def iter_jsonl_records(path: Path) -> Tuple[List[Tuple[int, Dict[str, Any]]], int]:
    """Parse all valid JSON objects from a JSONL file, keeping 1-based line numbers.

    Returns:
        (records, skipped_lines_count)
    """
    records: List[Tuple[int, Dict[str, Any]]] = []
    skipped_count = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, raw_line in enumerate(fh, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped_count += 1
                continue
            if isinstance(obj, dict):
                records.append((line_no, obj))
            else:
                skipped_count += 1
    return records, skipped_count


def canonicalize_records(
    records: List[Tuple[int, Dict[str, Any]]]
) -> List[Tuple[int, Dict[str, Any]]]:
    """Tag superseded/rewound steps via backward pass.

    If a record's step_index is >= any step_index seen subsequently in the file,
    that earlier step was superseded by a retry/rewind branch.
    """
    min_step_seen = float("inf")
    for _line_no, rec in reversed(records):
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


def format_step_tokens(record: Dict[str, Any]) -> int:
    """Return `input_tokens + cache_read_tokens` for a step (0 if absent)."""
    return int(record.get("input_tokens") or 0) + int(record.get("cache_read_tokens") or 0)


def watermark_band_label(tokens: int) -> str:
    """Map active context token count to a human-readable risk band."""
    if tokens >= CRITICAL_THRESHOLD_TOKENS:
        return "CRITICAL (>= 450k: compaction imminent)"
    if tokens >= WARNING_THRESHOLD_TOKENS:
        return "WARNING (380k-450k: update MEMORY_ANCHOR.md now)"
    if tokens >= WATCH_THRESHOLD_TOKENS:
        return "WATCH (307k-380k: keep reads targeted)"
    return "SAFE (< 307k)"


def unescape_nested_json(val: Any) -> Any:
    """Recursively unpack JSON-serialized strings inside dict/list structures."""
    if isinstance(val, str):
        s = val.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")) or (s.startswith('"') and s.endswith('"')):
            try:
                parsed = json.loads(s)
                return unescape_nested_json(parsed)
            except Exception:
                return val
        return val
    if isinstance(val, dict):
        return {k: unescape_nested_json(v) for k, v in val.items()}
    if isinstance(val, list):
        return [unescape_nested_json(item) for item in val]
    return val


def format_tool_calls(tool_calls: Any) -> str:
    """Render tool_calls array concisely and readably, unwrapping nested JSON strings."""
    if not tool_calls:
        return "(none)"
    try:
        clean_calls = unescape_nested_json(tool_calls)
        return json.dumps(clean_calls, ensure_ascii=False, indent=2)
    except Exception:
        return str(tool_calls)


def estimate_active_tokens(
    records: List[Tuple[int, Dict[str, Any]]],
    last_ckpt_idx: Optional[int],
) -> Tuple[int, int]:
    """Estimate token usage when transcript lacks telemetry fields.

    Calculates UTF-8 bytes of content, thinking, tool_calls, and error
    accumulated since the latest active CHECKPOINT (or start).
    Returns: (estimated_tokens, total_bytes)
    """
    start_pos = (last_ckpt_idx + 1) if last_ckpt_idx is not None else 0
    total_bytes = 0
    for i in range(start_pos, len(records)):
        _ln, rec = records[i]
        for field in ("content", "thinking", "tool_calls", "error"):
            val = rec.get(field)
            if val is not None:
                if isinstance(val, (dict, list)):
                    total_bytes += len(json.dumps(val, ensure_ascii=False).encode("utf-8", errors="replace"))
                else:
                    total_bytes += len(str(val).encode("utf-8", errors="replace"))

    est_tokens = int(total_bytes / BYTES_PER_TOKEN_ESTIMATE)
    return est_tokens, total_bytes


def cmd_status(args: argparse.Namespace) -> int:
    """Report token utilization, compaction checkpoints, and MEMORY_ANCHOR.md status."""
    brain_root = Path(args.brain_root) if getattr(args, "brain_root", None) else None
    cid, brain_dir = resolve_brain_dir(args.conversation_id, brain_root=brain_root)
    compact_path, full_path = get_transcript_paths(brain_dir)
    if not compact_path.is_file():
        raise FileNotFoundError(f"Missing transcript.jsonl: {compact_path}")

    records, skipped_count = iter_jsonl_records(compact_path)
    canonicalize_records(records)

    latest_tokens = 0
    latest_token_step: Optional[int] = None
    peak_tokens = 0
    peak_token_step: Optional[int] = None
    saw_token_fields = False
    max_step_index = -1

    all_checkpoints: List[Tuple[int, int, str, bool, int]] = []  # (line_no, step_idx, created_at, superseded, rec_idx)
    last_active_ckpt_idx: Optional[int] = None
    last_active_ckpt_step: Optional[int] = None
    last_active_ckpt_ts: str = ""

    last_active_planner_idx: Optional[int] = None
    pre_ckpt_tokens = 0

    for idx, (line_no, rec) in enumerate(records):
        step_idx = rec.get("step_index")
        if isinstance(step_idx, int) and step_idx > max_step_index:
            max_step_index = step_idx

        tok = format_step_tokens(rec)
        has_token_field = (rec.get("input_tokens") is not None) or (rec.get("cache_read_tokens") is not None)
        if has_token_field:
            saw_token_fields = True

        is_superseded = rec.get("_superseded", False)

        if tok > 0 and not is_superseded:
            if tok > peak_tokens:
                peak_tokens = tok
                peak_token_step = step_idx
            if rec.get("type") == "PLANNER_RESPONSE":
                latest_tokens = tok
                latest_token_step = step_idx
                last_active_planner_idx = idx

        if rec.get("type") == "CHECKPOINT":
            all_checkpoints.append(
                (line_no, int(step_idx if step_idx is not None else -1), str(rec.get("created_at") or ""), is_superseded, idx)
            )
            if not is_superseded:
                last_active_ckpt_idx = idx
                last_active_ckpt_step = int(step_idx if step_idx is not None else -1)
                last_active_ckpt_ts = str(rec.get("created_at") or "")
                pre_ckpt_tokens = latest_tokens

    active_ckpts = [c for c in all_checkpoints if not c[3]]
    rewound_ckpts = [c for c in all_checkpoints if c[3]]

    compact_mb = compact_path.stat().st_size / (1024 * 1024)
    full_mb = full_path.stat().st_size / (1024 * 1024) if full_path.is_file() else 0.0

    anchor_path = brain_dir / "MEMORY_ANCHOR.md"
    if anchor_path.is_file():
        st = anchor_path.stat()
        mtime_str = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        anchor_status = f"EXISTS ({st.st_size} bytes, modified {mtime_str})"
    else:
        anchor_status = "MISSING (not initialized yet)"

    print(f"Conversation ID      : {cid}")
    print(f"Artifact Directory   : {brain_dir}")
    print(f"Transcript Size      : {compact_mb:.2f} MB (compact) / {full_mb:.2f} MB (full)")
    skip_str = f" [skipped {skipped_count} malformed/blank lines]" if skipped_count > 0 else ""
    print(f"Total Steps          : {len(records)} records (max step_index={max_step_index}){skip_str}")

    # Determine context watermark status (H1, M2)
    if not saw_token_fields:
        est_tokens, est_bytes = estimate_active_tokens(records, last_active_ckpt_idx)
        ref_label = f"last CHECKPOINT (step {last_active_ckpt_step})" if last_active_ckpt_step is not None else "conversation start"
        est_pct = (est_tokens / COMPACTION_THRESHOLD_TOKENS) * 100.0
        print(
            f"Current Context      : UNKNOWN (transcript lacks token telemetry; "
            f"~{est_tokens:,} tokens estimated / {est_pct:.1f}% based on {est_bytes:,} bytes since {ref_label}) -> {watermark_band_label(est_tokens)}"
        )
        print("Peak Context Tokens  : UNKNOWN (no token fields in transcript)")
    elif (
        last_active_ckpt_idx is not None
        and (last_active_planner_idx is None or last_active_planner_idx < last_active_ckpt_idx)
    ):
        # M2: Just compacted, waiting for first post-compaction model response
        print(
            f"Current Context      : POST-COMPACTION (just compacted at step {last_active_ckpt_step}; "
            f"awaiting first post-compaction model response; pre-compaction context was {pre_ckpt_tokens:,} tokens)"
        )
        print(f"Peak Context Tokens  : {peak_tokens:,} tokens [step {peak_token_step}]")
    else:
        pct = (latest_tokens / COMPACTION_THRESHOLD_TOKENS) * 100.0 if latest_tokens else 0.0
        print(
            f"Current Context      : {latest_tokens:,} / {COMPACTION_THRESHOLD_TOKENS:,} tokens "
            f"({pct:.1f}%) [step {latest_token_step}] -> {watermark_band_label(latest_tokens)}"
        )
        print(f"Peak Context Tokens  : {peak_tokens:,} tokens [step {peak_token_step}]")

    # Compaction history (M1, L1)
    if active_ckpts or rewound_ckpts:
        active_steps = ", ".join(str(s) for _, s, _, _, _ in active_ckpts) if active_ckpts else "(none)"
        rewound_note = f" (+{len(rewound_ckpts)} rewound)" if rewound_ckpts else ""
        if active_ckpts:
            _line, last_step, last_ts, _rewound, last_idx = active_ckpts[-1]
            records_since = len(records) - 1 - last_idx
            step_delta = max_step_index - last_step if max_step_index >= last_step else records_since
            print(
                f"Compactions (CKPT)   : {len(active_ckpts)} active{rewound_note} "
                f"(step_index: [{active_steps}]; last at step {last_step} [{last_ts}], "
                f"{records_since} records / +{step_delta} steps since)"
            )
        else:
            print(f"Compactions (CKPT)   : 0 active{rewound_note} (all historical checkpoints were rewound)")
    else:
        print("Compactions (CKPT)   : 0 (no CHECKPOINT compaction yet)")

    print(f"MEMORY_ANCHOR.md     : {anchor_status}")
    return 0


def collect_user_inputs(
    records: List[Tuple[int, Dict[str, Any]]],
    latest_active_ckpt_line: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Collect chronological USER_INPUT entries with extracted prompts and byte lengths.

    Accurately classifies inputs:
    - 'truncated': Occurred before active CHECKPOINT and bytes > CHECKPOINT_USER_TRUNCATE_BYTES.
    - 'at_risk': Occurred after active CHECKPOINT (or no CHECKPOINT) and bytes > CHECKPOINT_USER_TRUNCATE_BYTES.
    - 'normal': Safe size (<= CHECKPOINT_USER_TRUNCATE_BYTES).
    """
    items: List[Dict[str, Any]] = []
    seen_steps: Dict[int, int] = {}

    for line_no, rec in records:
        if rec.get("type") != "USER_INPUT":
            continue
        step_idx = rec.get("step_index")
        if isinstance(step_idx, int):
            seen_steps[step_idx] = seen_steps.get(step_idx, 0) + 1

        prompt = extract_user_request(rec.get("content"))
        utf8_bytes = len(prompt.encode("utf-8", errors="replace"))

        is_before_ckpt = (latest_active_ckpt_line is not None) and (line_no < latest_active_ckpt_line)
        if utf8_bytes > CHECKPOINT_USER_TRUNCATE_BYTES:
            if is_before_ckpt:
                status = "truncated"
            else:
                status = "at_risk"
        else:
            status = "normal"

        items.append(
            {
                "line_no": line_no,
                "step_index": step_idx,
                "created_at": rec.get("created_at") or "",
                "prompt": prompt,
                "chars": len(prompt),
                "bytes": utf8_bytes,
                "status": status,
                "superseded": rec.get("_superseded", False),
            }
        )

    for item in items:
        s_idx = item["step_index"]
        if isinstance(s_idx, int) and seen_steps.get(s_idx, 0) > 1:
            item["retried"] = True
        else:
            item["retried"] = False

    return items


def cmd_recover(args: argparse.Namespace) -> int:
    """Recover untruncated user inputs, pre-compaction context, and MEMORY_ANCHOR.md."""
    brain_root = Path(args.brain_root) if getattr(args, "brain_root", None) else None
    cid, brain_dir = resolve_brain_dir(args.conversation_id, brain_root=brain_root)
    compact_path, full_path = get_transcript_paths(brain_dir)

    is_lossy = False
    if full_path.is_file():
        source_path = full_path
    elif compact_path.is_file():
        source_path = compact_path
        is_lossy = True
        print(
            "[WARNING] transcript_full.jsonl not found; falling back to lossy transcript.jsonl "
            "(some fields may be truncated).",
            file=sys.stderr,
        )
    else:
        raise FileNotFoundError(f"No transcript file found under {brain_dir}")

    records, _ = iter_jsonl_records(source_path)
    canonicalize_records(records)

    checkpoints = [
        (idx, line_no, rec)
        for idx, (line_no, rec) in enumerate(records)
        if rec.get("type") == "CHECKPOINT"
    ]
    active_checkpoints = [c for c in checkpoints if not c[2].get("_superseded")]
    rewound_checkpoints = [c for c in checkpoints if c[2].get("_superseded")]

    latest_active_ckpt_line = active_checkpoints[-1][1] if active_checkpoints else None
    user_inputs = collect_user_inputs(records, latest_active_ckpt_line=latest_active_ckpt_line)

    print(f"=== Anti-Compaction Recovery: {cid} ===")
    source_tag = " (lossy compact)" if is_lossy else " (lossless full)"
    print(f"Source Transcript: {source_path.name}{source_tag} ({len(records)} records)")
    print()

    print("## 1. Compaction Summary")
    if active_checkpoints or rewound_checkpoints:
        rewound_str = f" (+{len(rewound_checkpoints)} rewound)" if rewound_checkpoints else ""
        if active_checkpoints:
            steps_list = ", ".join(str(r.get("step_index")) for _, _, r in active_checkpoints)
            _, last_line, last_rec = active_checkpoints[-1]
            print(
                f"- Active CHECKPOINT count: {len(active_checkpoints)}{rewound_str} (step_index: [{steps_list}])\n"
                f"- Latest Active CHECKPOINT: line {last_line}, step_index={last_rec.get('step_index')}, "
                f"created_at={last_rec.get('created_at')}"
            )
        else:
            print(f"- Active CHECKPOINT count: 0{rewound_str} (all historical checkpoints were rewound)")
    else:
        print("- CHECKPOINT count: 0 (no compaction occurred yet)")
    print()

    # Section 2: User Inputs Index (M5: limit table length, highlight truncated/at-risk)
    print("## 2. User Inputs Index")
    if not user_inputs:
        print("- (No USER_INPUT records found)")
    else:
        index_limit = getattr(args, "index_limit", 30)
        # Prioritize truncated / at-risk inputs plus latest N inputs
        displayed_inputs = user_inputs
        omitted_count = 0
        if index_limit > 0 and len(user_inputs) > index_limit:
            prio_set: Set[int] = set()
            for idx, item in enumerate(user_inputs):
                if item["status"] in ("truncated", "at_risk"):
                    prio_set.add(idx)
            for idx in range(max(0, len(user_inputs) - index_limit), len(user_inputs)):
                prio_set.add(idx)

            displayed_inputs = [user_inputs[i] for i in sorted(prio_set)]
            omitted_count = len(user_inputs) - len(displayed_inputs)

        for item in displayed_inputs:
            flag = ""
            if item["status"] == "truncated":
                flag = " [TRUNCATED BY CHECKPOINT]"
            elif item["status"] == "at_risk":
                flag = " [>1KB AT-RISK]"
            if item.get("superseded"):
                flag += " [SUPERSEDED]"
            elif item.get("retried"):
                flag += " [RETRIED]"

            preview = single_line_preview(item["prompt"], 110)
            print(
                f"- line={item['line_no']} step={item['step_index']} ({item['created_at']}) "
                f"[{item['chars']} chars / {item['bytes']} bytes]{flag}: {preview}"
            )
        if omitted_count > 0:
            print(f"- ... [Omitted {omitted_count} earlier short inputs; pass --index-limit 0 to view all]")
    print()

    # Section 3: Full Untruncated User Inputs
    selected_map: Dict[int, Dict[str, Any]] = {}
    active_inputs = [item for item in user_inputs if not item.get("superseded")]
    if args.last_inputs > 0 and active_inputs:
        for item in active_inputs[-args.last_inputs :]:
            selected_map[item["line_no"]] = item

    if active_checkpoints:
        last_ckpt_line = active_checkpoints[-1][1]
        pre_ckpt_inputs = [item for item in active_inputs if item["line_no"] < last_ckpt_line]
        if pre_ckpt_inputs:
            pre_item = pre_ckpt_inputs[-1]
            selected_map[pre_item["line_no"]] = pre_item

    selected_inputs = [selected_map[k] for k in sorted(selected_map.keys())]

    print(f"## 3. Full Untruncated User Inputs (last {args.last_inputs} + pre-compaction input)")
    if not selected_inputs:
        print("(none)")
    else:
        for item in selected_inputs:
            flag = ""
            if item["status"] == "truncated":
                flag = " [TRUNCATED BY CHECKPOINT]"
            elif item["status"] == "at_risk":
                flag = " [>1KB AT-RISK]"
            print(
                f"--- [USER_INPUT line={item['line_no']} step={item['step_index']} "
                f"at {item['created_at']} | {item['bytes']} bytes{flag}] ---"
            )
            print(truncate_text(item["prompt"], args.max_chars))
            print()

    # Section 4: Pre-Compaction & Post-Compaction Model Context (L8)
    print("## 4. Pre-Compaction & Post-Compaction Model Context")
    if active_checkpoints:
        last_pos, last_line, last_ckpt = active_checkpoints[-1]
        pre_window = records[max(0, last_pos - 6) : last_pos]
        print(f"### Before Latest Active CHECKPOINT (line {last_line}, step {last_ckpt.get('step_index')})")
        for line_no, rec in pre_window:
            rtype = rec.get("type")
            if rtype not in ("PLANNER_RESPONSE", "GENERIC"):
                continue
            print(f"[line={line_no} step={rec.get('step_index')} type={rtype}]")
            char_cap = args.max_chars if args.max_chars > 0 else 800
            if rec.get("thinking"):
                print("Thinking:", truncate_text(str(rec["thinking"]), char_cap))
            if rec.get("tool_calls"):
                print("ToolCalls:", truncate_text(format_tool_calls(rec["tool_calls"]), char_cap))
            if rec.get("content"):
                print("Content:", truncate_text(str(rec["content"]), char_cap))
            print()

        post_window = records[last_pos + 1 : last_pos + 5]
        if post_window:
            print("### Immediately After Latest Active CHECKPOINT")
            for line_no, rec in post_window:
                print(f"[line={line_no} step={rec.get('step_index')} type={rec.get('type')}]")
                char_cap = args.max_chars if args.max_chars > 0 else 800
                if rec.get("thinking"):
                    print("Thinking:", truncate_text(str(rec["thinking"]), char_cap))
                if rec.get("tool_calls"):
                    print("ToolCalls:", truncate_text(format_tool_calls(rec["tool_calls"]), char_cap))
                if rec.get("content"):
                    print("Content:", truncate_text(str(rec["content"]), char_cap))
                print()
    else:
        recent_model = [(ln, r) for ln, r in records[-8:] if r.get("type") in ("PLANNER_RESPONSE", "GENERIC")]
        if not recent_model:
            print("(No recent model steps)")
        for line_no, rec in recent_model[-4:]:
            print(f"[line={line_no} step={rec.get('step_index')} type={rec.get('type')}]")
            char_cap = args.max_chars if args.max_chars > 0 else 800
            if rec.get("thinking"):
                print("Thinking:", truncate_text(str(rec["thinking"]), char_cap))
            if rec.get("tool_calls"):
                print("ToolCalls:", truncate_text(format_tool_calls(rec["tool_calls"]), char_cap))
            if rec.get("content"):
                print("Content:", truncate_text(str(rec["content"]), char_cap))
            print()

    print("## 5. MEMORY_ANCHOR.md Content")
    anchor_path = brain_dir / "MEMORY_ANCHOR.md"
    if anchor_path.is_file():
        print(anchor_path.read_text(encoding="utf-8", errors="replace"))
    else:
        print(
            f"[WARNING] MEMORY_ANCHOR.md does not exist at {anchor_path}.\n"
            f"Run: python \"{Path(__file__).resolve()}\" init-anchor --conversation-id {cid}"
        )
    return 0


def extract_search_fields(rec: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return searchable `(field_name, text)` pairs for a transcript record."""
    fields: List[Tuple[str, str]] = []
    if rec.get("content"):
        text = str(rec["content"])
        if rec.get("type") == "USER_INPUT":
            text = extract_user_request(text) or text
        fields.append(("content", text))
    if rec.get("thinking"):
        fields.append(("thinking", str(rec["thinking"])))
    if rec.get("tool_calls"):
        fields.append(("tool_calls", format_tool_calls(rec["tool_calls"]).replace("\\\\", "\\")))
    if rec.get("error"):  # L4: include error field
        err_val = rec["error"]
        err_text = (
            json.dumps(err_val, ensure_ascii=False).replace("\\\\", "\\")
            if isinstance(err_val, (dict, list))
            else str(err_val)
        )
        fields.append(("error", err_text))
    return fields


def snippet_around(text: str, start: int, end: int, context_chars: int) -> str:
    """Return a text window centered around `[start, end)`."""
    half = max(40, context_chars // 2)
    left = max(0, start - half)
    right = min(len(text), end + half)
    prefix = "... " if left > 0 else ""
    suffix = " ..." if right < len(text) else ""
    return f"{prefix}{text[left:right]}{suffix}"


def cmd_search(args: argparse.Namespace) -> int:
    """Search lossless `transcript_full.jsonl` by regex/keyword."""
    brain_root = Path(args.brain_root) if getattr(args, "brain_root", None) else None
    cid, brain_dir = resolve_brain_dir(args.conversation_id, brain_root=brain_root)
    compact_path, full_path = get_transcript_paths(brain_dir)

    is_lossy = False
    if full_path.is_file():
        source_path = full_path
    elif compact_path.is_file():
        source_path = compact_path
        is_lossy = True
        print(
            "[WARNING] transcript_full.jsonl not found; falling back to lossy transcript.jsonl "
            "(some fields may be truncated).",
            file=sys.stderr,
        )
    else:
        raise FileNotFoundError(f"No transcript file found under {brain_dir}")

    literal_mode = getattr(args, "literal", False)
    if literal_mode:
        regex = re.compile(re.escape(args.pattern), re.IGNORECASE)
    else:
        try:
            regex = re.compile(args.pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(args.pattern), re.IGNORECASE)

    records, _ = iter_jsonl_records(source_path)
    hits = 0
    search_desc = f"literal '{args.pattern}'" if literal_mode else f"/{args.pattern}/i"
    print(f"Searching {source_path.name} for {search_desc} in conversation {cid}...")
    print()

    def do_scan(target_re: re.Pattern) -> int:
        nonlocal hits
        hits = 0
        for line_no, rec in records:
            if args.source and str(rec.get("source", "")).upper() != args.source.upper():
                continue
            if args.type and str(rec.get("type", "")).upper() != args.type.upper():
                continue

            for field_name, text in extract_search_fields(rec):
                match = target_re.search(text)
                if not match:
                    continue
                hits += 1
                superseded_tag = " [SUPERSEDED]" if rec.get("_superseded") else ""
                print(
                    f"[{hits}] step={rec.get('step_index')} (line={line_no}) "
                    f"type={rec.get('type')} source={rec.get('source')} "
                    f"field={field_name} at {rec.get('created_at')}{superseded_tag}"
                )
                print(snippet_around(text, match.start(), match.end(), args.context_chars))
                print("-" * 60)
                if hits >= args.max_results:
                    print(f"Reached --max-results={args.max_results}. Narrow query or increase --max-results.")
                    return hits
        return hits

    do_scan(regex)

    # M4: If no hits, pattern contains backslash, and not in literal mode, auto retry with literal escape
    if hits == 0 and not literal_mode and "\\" in args.pattern:
        literal_re = re.compile(re.escape(args.pattern), re.IGNORECASE)
        print(f"[NOTE] No regex matches found for /{args.pattern}/i; retrying as literal string search...")
        print()
        do_scan(literal_re)

    if hits == 0:
        print("No matches found.")
    else:
        print(f"Total matches shown: {hits}")
    return 0


def cmd_step(args: argparse.Namespace) -> int:
    """Print complete untruncated record(s) for a `step_index` or inclusive range."""
    brain_root = Path(args.brain_root) if getattr(args, "brain_root", None) else None
    cid, brain_dir = resolve_brain_dir(args.conversation_id, brain_root=brain_root)
    compact_path, full_path = get_transcript_paths(brain_dir)

    if full_path.is_file():
        source_path = full_path
    elif compact_path.is_file():
        source_path = compact_path
        print(
            "[WARNING] transcript_full.jsonl not found; falling back to lossy transcript.jsonl "
            "(some fields may be truncated).",
            file=sys.stderr,
        )
    else:
        raise FileNotFoundError(f"No transcript file found under {brain_dir}")

    start_step = args.step_index
    end_step = args.end_step if args.end_step is not None else start_step
    if end_step < start_step:
        start_step, end_step = end_step, start_step

    records, _ = iter_jsonl_records(source_path)
    canonicalize_records(records)

    if args.all_occurrences:
        selected = [
            (ln, rec)
            for ln, rec in records
            if isinstance(rec.get("step_index"), int) and start_step <= rec["step_index"] <= end_step
        ]
    else:
        latest_by_step: Dict[int, Tuple[int, Dict[str, Any]]] = {}
        superseded_counts: Dict[int, int] = {}
        for ln, rec in records:
            step_idx = rec.get("step_index")
            if isinstance(step_idx, int) and start_step <= step_idx <= end_step:
                if step_idx in latest_by_step:
                    superseded_counts[step_idx] = superseded_counts.get(step_idx, 0) + 1
                latest_by_step[step_idx] = (ln, rec)
        selected = [latest_by_step[s] for s in sorted(latest_by_step.keys())]

        # M1 notification
        total_hidden = sum(superseded_counts.values())
        if total_hidden > 0:
            print(
                f"[NOTE] Hid {total_hidden} earlier superseded occurrence(s) in range [{start_step}, {end_step}]. "
                f"Pass --all-occurrences to view all retried records.\n",
                file=sys.stderr,
            )

    if not selected:
        print(f"No steps found in range [{start_step}, {end_step}] for conversation {cid}.")
        return 1

    # L9: Safe default char limit if querying multi-step range with default max_chars == 0
    max_chars = args.max_chars
    if max_chars == 0 and len(selected) > 3:
        max_chars = 4000
        print(
            f"[NOTE] Applying safety cap of {max_chars} chars per field for {len(selected)} steps. "
            f"Pass --max-chars -1 for completely unlimited dump.\n",
            file=sys.stderr,
        )
    elif max_chars < 0:
        max_chars = 0

    for ln, rec in selected:
        superseded_flag = " [SUPERSEDED]" if rec.get("_superseded") else ""
        print(
            f"=== step={rec.get('step_index')} (line={ln}) type={rec.get('type')} "
            f"source={rec.get('source')} status={rec.get('status')} at {rec.get('created_at')}{superseded_flag} ==="
        )
        tok = format_step_tokens(rec)
        if tok > 0:
            print(
                f"Tokens: input={rec.get('input_tokens')}, cache_read={rec.get('cache_read_tokens')}, "
                f"total_prompt={tok:,}, output={rec.get('output_tokens')}"
            )
        if rec.get("error"):
            print("--- error ---")
            err_val = rec["error"]
            err_str = json.dumps(err_val, ensure_ascii=False, indent=2) if isinstance(err_val, (dict, list)) else str(err_val)
            print(truncate_text(err_str, max_chars))
        if rec.get("thinking"):
            print("--- thinking ---")
            print(truncate_text(str(rec["thinking"]), max_chars))
        if rec.get("tool_calls"):
            print("--- tool_calls ---")
            print(truncate_text(format_tool_calls(rec["tool_calls"]), max_chars))
        if rec.get("content"):
            print("--- content ---")
            print(truncate_text(str(rec["content"]), max_chars))
        print()
    return 0


def cmd_init_anchor(args: argparse.Namespace) -> int:
    """Create `<artifact_dir>/MEMORY_ANCHOR.md` from template and append filtered USER_INPUT index."""
    brain_root = Path(args.brain_root) if getattr(args, "brain_root", None) else None
    cid, brain_dir = resolve_brain_dir(args.conversation_id, brain_root=brain_root, is_write_op=True)
    anchor_path = brain_dir / "MEMORY_ANCHOR.md"

    if anchor_path.exists():
        if not args.force:
            print(f"MEMORY_ANCHOR.md already exists at {anchor_path} (pass --force to overwrite).")
            return 0
        # L6: create backup before force overwriting
        bak_path = brain_dir / "MEMORY_ANCHOR.md.bak"
        try:
            bak_path.write_bytes(anchor_path.read_bytes())
            print(f"[NOTE] Backed up existing anchor to: {bak_path}")
        except Exception as e:
            print(f"[WARNING] Could not create backup: {e}", file=sys.stderr)

    if DEFAULT_TEMPLATE_PATH.is_file():
        template = DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8", errors="replace").rstrip()
    else:
        template = (
            "# MEMORY ANCHOR\n\n"
            "## 1. 铁律与负面约束\n\n"
            "## 2. 精确事实与参数账本\n\n"
            "## 3. 已排除死胡同\n\n"
            "## 4. 里程碑与当前精确状态\n\n"
            "## 5. 关键历史步骤索引\n"
        )

    compact_path, full_path = get_transcript_paths(brain_dir)
    source_path = full_path if full_path.is_file() else compact_path
    auto_rows: List[str] = []

    if source_path.is_file():
        records, _ = iter_jsonl_records(source_path)
        canonicalize_records(records)
        active_checkpoints = [
            (idx, ln, r) for idx, (ln, r) in enumerate(records)
            if r.get("type") == "CHECKPOINT" and not r.get("_superseded")
        ]
        last_ckpt_line = active_checkpoints[-1][1] if active_checkpoints else None
        all_inputs = collect_user_inputs(records, latest_active_ckpt_line=last_ckpt_line)

        # M5: Filter index table to high-risk long/truncated inputs + up to last 10 inputs (max 25 rows)
        prio_indices: Set[int] = set()
        for idx, item in enumerate(all_inputs):
            if item["status"] in ("truncated", "at_risk"):
                prio_indices.add(idx)
        for idx in range(max(0, len(all_inputs) - 10), len(all_inputs)):
            prio_indices.add(idx)

        selected_indices = sorted(prio_indices)
        if len(selected_indices) > 25:
            # Keep all truncated/at-risk, and fill remainder with latest
            at_risk = [i for i in selected_indices if all_inputs[i]["status"] in ("truncated", "at_risk")]
            normal = [i for i in selected_indices if all_inputs[i]["status"] == "normal"]
            needed = max(0, 25 - len(at_risk))
            selected_indices = sorted(set(at_risk + normal[-needed:]))

        omitted_short = len(all_inputs) - len(selected_indices)

        for i in selected_indices:
            item = all_inputs[i]
            flag = ""
            if item["status"] == "truncated":
                flag = " [>1KB 已截断]"
            elif item["status"] == "at_risk":
                flag = " [>1KB 易截断]"
            if item.get("superseded"):
                flag += " [已回退]"

            preview = single_line_preview(item["prompt"], 80).replace("|", "\\|")
            line_str = f"L{item['line_no']}"
            auto_rows.append(
                f"| `{item['step_index']}` ({line_str}) | `USER_INPUT` | {item['created_at']}{flag}: {preview} |"
            )

        if omitted_short > 0:
            auto_rows.append(
                f"| `...` | `OMITTED` | 省略了 {omitted_short} 条历史短输入；使用 `context_guardian.py recover` 调取完整清单 |"
            )

    content_parts = [
        template,
        "",
        f"<!-- Initialized for conversation {cid} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} -->",
    ]
    if auto_rows:
        content_parts.extend(
            [
                "",
                "### 自动生成的历史用户输入索引（高风险超长输入与近期输入）",
                "| `step_index (line)` | 类型 | 时间与预览 |",
                "|---|---|---|",
                *auto_rows,
            ]
        )

    # Atomic write (L6, L11)
    final_text = "\n".join(content_parts) + "\n"
    tmp_path = brain_dir / "MEMORY_ANCHOR.md.tmp"
    tmp_path.write_text(final_text, encoding="utf-8", errors="replace")
    os.replace(tmp_path, anchor_path)

    print(f"Created MEMORY_ANCHOR.md at: {anchor_path}")
    return 0


def load_hooks_json(path: Path) -> Dict[str, Any]:
    """Load hooks.json safely, supporting UTF-8 BOM, returning `{}` if missing."""
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}, got {type(data).__name__}")
    return data


def build_hook_spec(existing_entry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the PreInvocation hook entry for `anti-compaction-amnesia`.

    Uses absolute path of current Python interpreter with -X utf8 for Windows cmd /c safety (M7, MED-5).
    """
    py_exe = Path(sys.executable).resolve().as_posix()
    script_posix = GUARD_SCRIPT_PATH.resolve().as_posix()
    command = f'"{py_exe}" -X utf8 "{script_posix}"'

    spec: Dict[str, Any] = {
        "enabled": True,
        "PreInvocation": [
            {
                "type": "command",
                "command": command,
                "timeout": 10,
            }
        ],
    }
    if isinstance(existing_entry, dict):
        # Preserve user's custom timeout or other fields if already configured
        for key, val in existing_entry.items():
            if key not in ("enabled", "PreInvocation"):
                spec[key] = val
            elif key == "PreInvocation" and isinstance(val, list) and val:
                first = val[0]
                if isinstance(first, dict) and "timeout" in first:
                    spec["PreInvocation"][0]["timeout"] = first["timeout"]
    return spec


def cmd_hook(args: argparse.Namespace) -> int:
    """Inspect, enable, or disable the global PreInvocation guard in hooks.json."""
    hooks_path = Path(args.hooks_path) if args.hooks_path else DEFAULT_HOOKS_PATH
    hooks_data = load_hooks_json(hooks_path)

    if args.status or (not args.enable and not args.disable):
        entry = hooks_data.get(HOOK_KEY)
        if not entry:
            print(f"Hook '{HOOK_KEY}': NOT INSTALLED (checked {hooks_path})")
        elif not isinstance(entry, dict):
            print(f"Hook '{HOOK_KEY}': INVALID FORMAT ({type(entry).__name__}) in {hooks_path}")
        else:
            enabled = entry.get("enabled", True)
            print(f"Hook '{HOOK_KEY}': {'ENABLED' if enabled else 'DISABLED'} in {hooks_path}")
            print(json.dumps(entry, ensure_ascii=False, indent=2))
        return 0

    if args.enable:
        if not GUARD_SCRIPT_PATH.is_file():
            raise FileNotFoundError(f"Guard script does not exist at {GUARD_SCRIPT_PATH}")

        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        existing_entry = hooks_data.get(HOOK_KEY) if isinstance(hooks_data.get(HOOK_KEY), dict) else None
        hooks_data[HOOK_KEY] = build_hook_spec(existing_entry)

        # Atomic backup and write (M6)
        if hooks_path.is_file():
            bak_path = hooks_path.with_suffix(".json.bak")
            try:
                bak_path.write_bytes(hooks_path.read_bytes())
            except Exception:
                pass

        tmp_path = hooks_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(hooks_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            errors="replace",
        )
        os.replace(tmp_path, hooks_path)
        print(f"Enabled PreInvocation hook '{HOOK_KEY}' in {hooks_path}")
        return 0

    if args.disable:
        if HOOK_KEY not in hooks_data:
            print(f"Hook '{HOOK_KEY}' is not present in {hooks_path}; nothing to disable.")
            return 0
        if isinstance(hooks_data[HOOK_KEY], dict):
            hooks_data[HOOK_KEY]["enabled"] = False
        else:
            hooks_data[HOOK_KEY] = {"enabled": False}

        if hooks_path.is_file():
            bak_path = hooks_path.with_suffix(".json.bak")
            try:
                bak_path.write_bytes(hooks_path.read_bytes())
            except Exception:
                pass

        tmp_path = hooks_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(hooks_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            errors="replace",
        )
        os.replace(tmp_path, hooks_path)
        print(f"Disabled PreInvocation hook '{HOOK_KEY}' in {hooks_path}")
        return 0

    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="context_guardian.py",
        description="Prevent Antigravity lossy compaction amnesia via watermark checks, lossless transcript recovery, and MEMORY_ANCHOR.md.",
    )
    parser.add_argument("--brain-root", default=None, help="Custom brain root directory (or set $AGY_BRAIN_ROOT).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Show context token usage, compaction checkpoints, and anchor status.")
    p_status.add_argument("--conversation-id", "-c", default="auto", help="Conversation UUID (default: auto/latest).")
    p_status.add_argument("--brain-root", default=None, help="Custom brain root directory (or set $AGY_BRAIN_ROOT).")
    p_status.set_defaults(func=cmd_status)

    p_recover = sub.add_parser("recover", help="Recover untruncated user inputs and pre-compaction context.")
    p_recover.add_argument("--conversation-id", "-c", default="auto", help="Conversation UUID (default: auto/latest).")
    p_recover.add_argument("--brain-root", default=None, help="Custom brain root directory (or set $AGY_BRAIN_ROOT).")
    p_recover.add_argument("--last-inputs", type=int, default=3, help="Number of recent USER_INPUT steps to show in full.")
    p_recover.add_argument("--index-limit", type=int, default=30, help="Max inputs to list in section 2 (0 for all).")
    p_recover.add_argument("--max-chars", type=int, default=8000, help="Max chars per recovered input (0 for unlimited).")
    p_recover.set_defaults(func=cmd_recover)

    p_search = sub.add_parser("search", help="Search lossless transcript_full.jsonl by regex/keyword. Use -- for patterns starting with '-'.")
    p_search.add_argument("pattern", help="Regex or literal keyword to search (case-insensitive). For patterns starting with '-', use: search -- '-pattern'")
    p_search.add_argument("--conversation-id", "-c", default="auto", help="Conversation UUID (default: auto/latest).")
    p_search.add_argument("--brain-root", default=None, help="Custom brain root directory (or set $AGY_BRAIN_ROOT).")
    p_search.add_argument("--source", choices=["USER_EXPLICIT", "MODEL", "SYSTEM"], help="Filter by step source.")
    p_search.add_argument(
        "--type",
        choices=["USER_INPUT", "PLANNER_RESPONSE", "GENERIC", "CHECKPOINT", "SYSTEM_MESSAGE", "ERROR_MESSAGE"],
        help="Filter by step type.",
    )
    p_search.add_argument("-F", "--literal", "--fixed-strings", action="store_true", help="Search as literal string instead of regex.")
    p_search.add_argument("--context-chars", type=int, default=400, help="Snippet length around match.")
    p_search.add_argument("--max-results", type=int, default=20, help="Maximum matches to print.")
    p_search.set_defaults(func=cmd_search)

    p_step = sub.add_parser("step", help="Inspect complete untruncated record(s) for a step_index or range.")
    p_step.add_argument("step_index", type=int, help="Starting step_index.")
    p_step.add_argument("end_step", type=int, nargs="?", default=None, help="Optional inclusive ending step_index.")
    p_step.add_argument("--conversation-id", "-c", default="auto", help="Conversation UUID (default: auto/latest).")
    p_step.add_argument("--brain-root", default=None, help="Custom brain root directory (or set $AGY_BRAIN_ROOT).")
    p_step.add_argument("--max-chars", type=int, default=0, help="Max chars per field (0 for default cap / -1 for unlimited).")
    p_step.add_argument("--all-occurrences", action="store_true", help="Include earlier retried/superseded occurrences of step_index.")
    p_step.set_defaults(func=cmd_step)

    p_init = sub.add_parser("init-anchor", help="Initialize MEMORY_ANCHOR.md in the conversation artifact directory.")
    p_init.add_argument("--conversation-id", "-c", default="auto", help="Conversation UUID (default: auto/latest).")
    p_init.add_argument("--brain-root", default=None, help="Custom brain root directory (or set $AGY_BRAIN_ROOT).")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing MEMORY_ANCHOR.md (saves .bak first).")
    p_init.set_defaults(func=cmd_init_anchor)

    p_hook = sub.add_parser("hook", help="Manage the global PreInvocation guard in hooks.json.")
    hook_group = p_hook.add_mutually_exclusive_group()
    hook_group.add_argument("--status", action="store_true", help="Check hook status (default action).")
    hook_group.add_argument("--enable", action="store_true", help="Enable hook in ~/.gemini/config/hooks.json.")
    hook_group.add_argument("--disable", action="store_true", help="Disable hook in ~/.gemini/config/hooks.json.")
    p_hook.add_argument("--hooks-path", default=None, help="Optional custom hooks.json path.")
    p_hook.set_defaults(func=cmd_hook)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
