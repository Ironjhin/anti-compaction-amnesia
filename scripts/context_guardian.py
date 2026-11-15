#!/usr/bin/env python3
"""Antigravity anti-compaction amnesia guardian CLI.

Inspects Antigravity conversation transcripts (`transcript.jsonl` and lossless
`transcript_full.jsonl`), monitors token watermarks near the ~250k compaction
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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

COMPACTION_THRESHOLD_TOKENS = 250_000
WATCH_THRESHOLD_TOKENS = 150_000
WARNING_THRESHOLD_TOKENS = 185_000
CRITICAL_THRESHOLD_TOKENS = 220_000
CHECKPOINT_USER_TRUNCATE_BYTES = 500

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


def resolve_brain_dir(
    conversation_id: Optional[str] = None,
    brain_root: Optional[Path] = None,
) -> Tuple[str, Path]:
    """Resolve the target conversation's brain directory.

    When `conversation_id` is omitted or `"auto"`, selects the conversation folder
    whose `.system_generated/logs/transcript.jsonl` has the most recent mtime.
    """
    root = brain_root or DEFAULT_BRAIN_ROOT
    if not root.exists():
        raise FileNotFoundError(f"Brain directory does not exist: {root}")

    if conversation_id and conversation_id.lower() != "auto":
        exact = root / conversation_id
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
        raise FileNotFoundError(f"Conversation directory not found: {exact}")

    candidates: List[Tuple[float, Path]] = []
    for entry in root.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        compact_log = entry / ".system_generated" / "logs" / "transcript.jsonl"
        if compact_log.is_file():
            candidates.append((compact_log.stat().st_mtime, entry))

    if not candidates:
        raise FileNotFoundError(f"No conversation transcripts found under {root}")

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = candidates[0][1]
    return selected.name, selected


def get_transcript_paths(brain_dir: Path) -> Tuple[Path, Path]:
    """Return `(compact_path, full_path)` for a conversation brain directory."""
    logs_dir = brain_dir / ".system_generated" / "logs"
    return logs_dir / "transcript.jsonl", logs_dir / "transcript_full.jsonl"


def iter_jsonl_records(path: Path) -> List[Tuple[int, Dict[str, Any]]]:
    """Parse all valid JSON objects from a JSONL file, keeping 1-based line numbers."""
    records: List[Tuple[int, Dict[str, Any]]] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, raw_line in enumerate(fh, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                records.append((line_no, obj))
    return records


def format_step_tokens(record: Dict[str, Any]) -> int:
    """Return `input_tokens + cache_read_tokens` for a step (0 if absent)."""
    return int(record.get("input_tokens") or 0) + int(record.get("cache_read_tokens") or 0)


def watermark_band_label(tokens: int) -> str:
    """Map active context token count to a human-readable risk band."""
    if tokens >= CRITICAL_THRESHOLD_TOKENS:
        return "CRITICAL (>= 220k: compaction imminent)"
    if tokens >= WARNING_THRESHOLD_TOKENS:
        return "WARNING (185k-220k: update MEMORY_ANCHOR.md now)"
    if tokens >= WATCH_THRESHOLD_TOKENS:
        return "WATCH (150k-185k: keep reads targeted)"
    return "SAFE (< 150k)"


def format_tool_calls(tool_calls: Any) -> str:
    """Render tool_calls array concisely and readably."""
    if not tool_calls:
        return "(none)"
    try:
        return json.dumps(tool_calls, ensure_ascii=False, indent=2)
    except Exception:
        return str(tool_calls)


def cmd_status(args: argparse.Namespace) -> int:
    """Report token utilization, compaction checkpoints, and MEMORY_ANCHOR.md status."""
    cid, brain_dir = resolve_brain_dir(args.conversation_id)
    compact_path, full_path = get_transcript_paths(brain_dir)
    if not compact_path.is_file():
        raise FileNotFoundError(f"Missing transcript.jsonl: {compact_path}")

    records = iter_jsonl_records(compact_path)
    latest_tokens = 0
    latest_token_step: Optional[int] = None
    peak_tokens = 0
    peak_token_step: Optional[int] = None
    checkpoints: List[Tuple[int, int, str]] = []
    max_step_index = -1

    for line_no, rec in records:
        step_idx = rec.get("step_index")
        if isinstance(step_idx, int) and step_idx > max_step_index:
            max_step_index = step_idx
        tok = format_step_tokens(rec)
        if tok > 0:
            if tok > peak_tokens:
                peak_tokens = tok
                peak_token_step = step_idx
            if rec.get("type") == "PLANNER_RESPONSE":
                latest_tokens = tok
                latest_token_step = step_idx
        if rec.get("type") == "CHECKPOINT":
            checkpoints.append((line_no, int(step_idx if step_idx is not None else -1), str(rec.get("created_at") or "")))

    compact_mb = compact_path.stat().st_size / (1024 * 1024)
    full_mb = full_path.stat().st_size / (1024 * 1024) if full_path.is_file() else 0.0
    pct = (latest_tokens / COMPACTION_THRESHOLD_TOKENS) * 100.0 if latest_tokens else 0.0

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
    print(f"Total Steps          : {len(records)} records (max step_index={max_step_index})")
    print(
        f"Current Context      : {latest_tokens:,} / {COMPACTION_THRESHOLD_TOKENS:,} tokens "
        f"({pct:.1f}%) [step {latest_token_step}] -> {watermark_band_label(latest_tokens)}"
    )
    print(f"Peak Context Tokens  : {peak_tokens:,} tokens [step {peak_token_step}]")

    if checkpoints:
        ckpt_steps = ", ".join(str(s) for _, s, _ in checkpoints)
        last_line, last_step, last_ts = checkpoints[-1]
        records_since = len(records) - last_line
        step_delta = max_step_index - last_step if max_step_index >= last_step else records_since
        print(
            f"Compactions (CKPT)   : {len(checkpoints)} (step_index: [{ckpt_steps}]; "
            f"last at step {last_step} [{last_ts}], {records_since} records / +{step_delta} steps since)"
        )
    else:
        print("Compactions (CKPT)   : 0 (no CHECKPOINT compaction yet)")

    print(f"MEMORY_ANCHOR.md     : {anchor_status}")
    return 0


def collect_user_inputs(records: List[Tuple[int, Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Collect chronological USER_INPUT entries with extracted prompts and byte lengths."""
    items: List[Dict[str, Any]] = []
    for line_no, rec in records:
        if rec.get("type") != "USER_INPUT":
            continue
        prompt = extract_user_request(rec.get("content"))
        utf8_bytes = len(prompt.encode("utf-8"))
        items.append(
            {
                "line_no": line_no,
                "step_index": rec.get("step_index"),
                "created_at": rec.get("created_at") or "",
                "prompt": prompt,
                "chars": len(prompt),
                "bytes": utf8_bytes,
                "truncated_by_checkpoint": utf8_bytes > CHECKPOINT_USER_TRUNCATE_BYTES,
            }
        )
    return items


def cmd_recover(args: argparse.Namespace) -> int:
    """Recover untruncated user inputs, pre-compaction context, and MEMORY_ANCHOR.md."""
    cid, brain_dir = resolve_brain_dir(args.conversation_id)
    compact_path, full_path = get_transcript_paths(brain_dir)
    source_path = full_path if full_path.is_file() else compact_path
    if not source_path.is_file():
        raise FileNotFoundError(f"No transcript file found under {brain_dir}")

    records = iter_jsonl_records(source_path)
    checkpoints = [
        (idx, line_no, rec)
        for idx, (line_no, rec) in enumerate(records)
        if rec.get("type") == "CHECKPOINT"
    ]
    user_inputs = collect_user_inputs(records)

    print(f"=== Anti-Compaction Recovery: {cid} ===")
    print(f"Source Transcript: {source_path.name} ({len(records)} records)")
    print()

    print("## 1. Compaction Summary")
    if checkpoints:
        steps_list = ", ".join(str(r.get("step_index")) for _, _, r in checkpoints)
        _, last_line, last_rec = checkpoints[-1]
        print(
            f"- CHECKPOINT count: {len(checkpoints)} (step_index: [{steps_list}])\n"
            f"- Latest CHECKPOINT: line {last_line}, step_index={last_rec.get('step_index')}, "
            f"created_at={last_rec.get('created_at')}"
        )
    else:
        print("- CHECKPOINT count: 0 (no compaction occurred yet)")
    print()

    print("## 2. All User Inputs Index")
    if not user_inputs:
        print("- (No USER_INPUT records found)")
    else:
        for item in user_inputs:
            flag = " [TRUNCATED BY CHECKPOINT]" if item["truncated_by_checkpoint"] else ""
            preview = single_line_preview(item["prompt"], 120)
            print(
                f"- line={item['line_no']} step={item['step_index']} ({item['created_at']}) "
                f"[{item['chars']} chars / {item['bytes']} bytes]{flag}: {preview}"
            )
    print()

    # Select last N inputs + the latest pre-compaction USER_INPUT if a CHECKPOINT exists
    selected_map: Dict[int, Dict[str, Any]] = {}
    if args.last_inputs > 0 and user_inputs:
        for item in user_inputs[-args.last_inputs :]:
            selected_map[item["line_no"]] = item

    if checkpoints:
        last_ckpt_line = checkpoints[-1][1]
        pre_ckpt_inputs = [item for item in user_inputs if item["line_no"] < last_ckpt_line]
        if pre_ckpt_inputs:
            pre_item = pre_ckpt_inputs[-1]
            selected_map[pre_item["line_no"]] = pre_item

    selected_inputs = [selected_map[k] for k in sorted(selected_map.keys())]

    print(f"## 3. Full Untruncated User Inputs (last {args.last_inputs} + pre-compaction input)")
    if not selected_inputs:
        print("(none)")
    else:
        for item in selected_inputs:
            flag = " [TRUNCATED BY CHECKPOINT]" if item["truncated_by_checkpoint"] else ""
            print(
                f"--- [USER_INPUT line={item['line_no']} step={item['step_index']} "
                f"at {item['created_at']} | {item['bytes']} bytes{flag}] ---"
            )
            print(truncate_text(item["prompt"], args.max_chars))
            print()

    print("## 4. Pre-Compaction & Post-Compaction Model Context")
    if checkpoints:
        last_pos, last_line, last_ckpt = checkpoints[-1]
        pre_window = records[max(0, last_pos - 6) : last_pos]
        print(f"### Before Latest CHECKPOINT (line {last_line}, step {last_ckpt.get('step_index')})")
        for line_no, rec in pre_window:
            rtype = rec.get("type")
            if rtype not in ("PLANNER_RESPONSE", "GENERIC"):
                continue
            print(f"[line={line_no} step={rec.get('step_index')} type={rtype}]")
            if rec.get("thinking"):
                print("Thinking:", truncate_text(str(rec["thinking"]), 600))
            if rec.get("tool_calls"):
                print("ToolCalls:", truncate_text(format_tool_calls(rec["tool_calls"]), 600))
            if rec.get("content"):
                print("Content:", truncate_text(str(rec["content"]), 800))
            print()

        post_window = records[last_pos + 1 : last_pos + 5]
        if post_window:
            print("### Immediately After Latest CHECKPOINT")
            for line_no, rec in post_window:
                print(f"[line={line_no} step={rec.get('step_index')} type={rec.get('type')}]")
                if rec.get("content"):
                    print(truncate_text(str(rec["content"]), 600))
                print()
    else:
        recent_model = [(ln, r) for ln, r in records[-8:] if r.get("type") in ("PLANNER_RESPONSE", "GENERIC")]
        if not recent_model:
            print("(No recent model steps)")
        for line_no, rec in recent_model[-4:]:
            print(f"[line={line_no} step={rec.get('step_index')} type={rec.get('type')}]")
            if rec.get("content"):
                print(truncate_text(str(rec["content"]), 600))
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
        fields.append(("tool_calls", format_tool_calls(rec["tool_calls"])))
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
    cid, brain_dir = resolve_brain_dir(args.conversation_id)
    compact_path, full_path = get_transcript_paths(brain_dir)
    source_path = full_path if full_path.is_file() else compact_path
    if not source_path.is_file():
        raise FileNotFoundError(f"No transcript file found under {brain_dir}")

    try:
        regex = re.compile(args.pattern, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(args.pattern), re.IGNORECASE)

    records = iter_jsonl_records(source_path)
    hits = 0
    print(f"Searching {source_path.name} for /{args.pattern}/i in conversation {cid}...")
    print()

    for line_no, rec in records:
        if args.source and str(rec.get("source", "")).upper() != args.source.upper():
            continue
        if args.type and str(rec.get("type", "")).upper() != args.type.upper():
            continue

        for field_name, text in extract_search_fields(rec):
            match = regex.search(text)
            if not match:
                continue
            hits += 1
            print(
                f"[{hits}] step={rec.get('step_index')} (line={line_no}) "
                f"type={rec.get('type')} source={rec.get('source')} "
                f"field={field_name} at {rec.get('created_at')}"
            )
            print(snippet_around(text, match.start(), match.end(), args.context_chars))
            print("-" * 60)
            if hits >= args.max_results:
                print(f"Reached --max-results={args.max_results}. Narrow query or increase --max-results.")
                return 0

    if hits == 0:
        print("No matches found.")
    else:
        print(f"Total matches shown: {hits}")
    return 0


def cmd_step(args: argparse.Namespace) -> int:
    """Print complete untruncated record(s) for a `step_index` or inclusive range."""
    cid, brain_dir = resolve_brain_dir(args.conversation_id)
    compact_path, full_path = get_transcript_paths(brain_dir)
    source_path = full_path if full_path.is_file() else compact_path
    if not source_path.is_file():
        raise FileNotFoundError(f"No transcript file found under {brain_dir}")

    start_step = args.step_index
    end_step = args.end_step if args.end_step is not None else start_step
    if end_step < start_step:
        start_step, end_step = end_step, start_step

    records = iter_jsonl_records(source_path)

    if args.all_occurrences:
        selected = [
            (ln, rec)
            for ln, rec in records
            if isinstance(rec.get("step_index"), int) and start_step <= rec["step_index"] <= end_step
        ]
    else:
        latest_by_step: Dict[int, Tuple[int, Dict[str, Any]]] = {}
        for ln, rec in records:
            step_idx = rec.get("step_index")
            if isinstance(step_idx, int) and start_step <= step_idx <= end_step:
                latest_by_step[step_idx] = (ln, rec)
        selected = [latest_by_step[s] for s in sorted(latest_by_step.keys())]

    if not selected:
        print(f"No steps found in range [{start_step}, {end_step}] for conversation {cid}.")
        return 1

    for ln, rec in selected:
        print(
            f"=== step={rec.get('step_index')} (line={ln}) type={rec.get('type')} "
            f"source={rec.get('source')} status={rec.get('status')} at {rec.get('created_at')} ==="
        )
        tok = format_step_tokens(rec)
        if tok > 0:
            print(
                f"Tokens: input={rec.get('input_tokens')}, cache_read={rec.get('cache_read_tokens')}, "
                f"total_prompt={tok:,}, output={rec.get('output_tokens')}"
            )
        if rec.get("thinking"):
            print("--- thinking ---")
            print(truncate_text(str(rec["thinking"]), args.max_chars))
        if rec.get("tool_calls"):
            print("--- tool_calls ---")
            print(truncate_text(format_tool_calls(rec["tool_calls"]), args.max_chars))
        if rec.get("content"):
            print("--- content ---")
            print(truncate_text(str(rec["content"]), args.max_chars))
        print()
    return 0


def cmd_init_anchor(args: argparse.Namespace) -> int:
    """Create `<artifact_dir>/MEMORY_ANCHOR.md` from template and append USER_INPUT index."""
    cid, brain_dir = resolve_brain_dir(args.conversation_id)
    anchor_path = brain_dir / "MEMORY_ANCHOR.md"
    if anchor_path.exists() and not args.force:
        print(f"MEMORY_ANCHOR.md already exists at {anchor_path} (pass --force to overwrite).")
        return 0

    if DEFAULT_TEMPLATE_PATH.is_file():
        template = DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8", errors="replace").rstrip()
    else:
        template = "# MEMORY ANCHOR\n\n## 1. 铁律与负面约束\n\n## 2. 精确事实与参数账本\n\n## 3. 已排除死胡同\n\n## 4. 里程碑与当前精确状态\n\n## 5. 关键历史步骤索引\n"

    compact_path, full_path = get_transcript_paths(brain_dir)
    source_path = full_path if full_path.is_file() else compact_path
    auto_rows: List[str] = []
    if source_path.is_file():
        for item in collect_user_inputs(iter_jsonl_records(source_path)):
            flag = " [>500B 易被截断]" if item["truncated_by_checkpoint"] else ""
            preview = single_line_preview(item["prompt"], 90).replace("|", "\\|")
            auto_rows.append(
                f"| `{item['step_index']}` | `USER_INPUT` | {item['created_at']}{flag}: {preview} |"
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
                "### 自动生成的历史用户输入索引",
                "| `step_index` | 类型 | 时间与预览 |",
                "|---|---|---|",
                *auto_rows,
            ]
        )
    anchor_path.write_text("\n".join(content_parts) + "\n", encoding="utf-8")
    print(f"Created MEMORY_ANCHOR.md at: {anchor_path}")
    return 0


def load_hooks_json(path: Path) -> Dict[str, Any]:
    """Load hooks.json safely, returning `{}` if missing or empty."""
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def build_hook_spec() -> Dict[str, Any]:
    """Return the PreInvocation hook entry for `anti-compaction-amnesia`."""
    script_posix = GUARD_SCRIPT_PATH.as_posix()
    quoted_path = f'"{script_posix}"' if " " in script_posix else script_posix
    return {
        "enabled": True,
        "PreInvocation": [
            {
                "type": "command",
                "command": f"python {quoted_path}",
                "timeout": 10,
            }
        ],
    }


def cmd_hook(args: argparse.Namespace) -> int:
    """Inspect, enable, or disable the global PreInvocation guard in hooks.json."""
    hooks_path = Path(args.hooks_path) if args.hooks_path else DEFAULT_HOOKS_PATH
    action_count = sum(bool(x) for x in (args.status, args.enable, args.disable))
    if action_count != 1:
        raise ValueError("Specify exactly one of --status, --enable, or --disable.")

    hooks_data = load_hooks_json(hooks_path)

    if args.status:
        entry = hooks_data.get(HOOK_KEY)
        if not entry:
            print(f"Hook '{HOOK_KEY}': NOT INSTALLED (checked {hooks_path})")
        else:
            enabled = entry.get("enabled", True)
            print(f"Hook '{HOOK_KEY}': {'ENABLED' if enabled else 'DISABLED'} in {hooks_path}")
            print(json.dumps(entry, ensure_ascii=False, indent=2))
        return 0

    if args.enable:
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        hooks_data[HOOK_KEY] = build_hook_spec()
        hooks_path.write_text(
            json.dumps(hooks_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Enabled PreInvocation hook '{HOOK_KEY}' in {hooks_path}")
        return 0

    if args.disable:
        if HOOK_KEY not in hooks_data:
            print(f"Hook '{HOOK_KEY}' is not present in {hooks_path}; nothing to disable.")
            return 0
        hooks_data[HOOK_KEY]["enabled"] = False
        hooks_path.write_text(
            json.dumps(hooks_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Disabled PreInvocation hook '{HOOK_KEY}' in {hooks_path}")
        return 0

    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="context_guardian.py",
        description="Prevent Antigravity lossy compaction amnesia via watermark checks, lossless transcript recovery, and MEMORY_ANCHOR.md.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Show context token usage, compaction checkpoints, and anchor status.")
    p_status.add_argument("--conversation-id", "-c", default="auto", help="Conversation UUID (default: auto/latest).")
    p_status.set_defaults(func=cmd_status)

    p_recover = sub.add_parser("recover", help="Recover untruncated user inputs and pre-compaction context.")
    p_recover.add_argument("--conversation-id", "-c", default="auto", help="Conversation UUID (default: auto/latest).")
    p_recover.add_argument("--last-inputs", type=int, default=3, help="Number of recent USER_INPUT steps to show in full.")
    p_recover.add_argument("--max-chars", type=int, default=8000, help="Max chars per recovered input (0 for unlimited).")
    p_recover.set_defaults(func=cmd_recover)

    p_search = sub.add_parser("search", help="Search lossless transcript_full.jsonl by regex/keyword.")
    p_search.add_argument("pattern", help="Regex or literal keyword to search (case-insensitive).")
    p_search.add_argument("--conversation-id", "-c", default="auto", help="Conversation UUID (default: auto/latest).")
    p_search.add_argument("--source", choices=["USER_EXPLICIT", "MODEL", "SYSTEM"], help="Filter by step source.")
    p_search.add_argument(
        "--type",
        choices=["USER_INPUT", "PLANNER_RESPONSE", "GENERIC", "CHECKPOINT", "SYSTEM_MESSAGE"],
        help="Filter by step type.",
    )
    p_search.add_argument("--context-chars", type=int, default=400, help="Snippet length around match.")
    p_search.add_argument("--max-results", type=int, default=20, help="Maximum matches to print.")
    p_search.set_defaults(func=cmd_search)

    p_step = sub.add_parser("step", help="Inspect complete untruncated record(s) for a step_index or range.")
    p_step.add_argument("step_index", type=int, help="Starting step_index.")
    p_step.add_argument("end_step", type=int, nargs="?", default=None, help="Optional inclusive ending step_index.")
    p_step.add_argument("--conversation-id", "-c", default="auto", help="Conversation UUID (default: auto/latest).")
    p_step.add_argument("--max-chars", type=int, default=0, help="Max chars per field (0 for unlimited).")
    p_step.add_argument("--all-occurrences", action="store_true", help="Include earlier retried occurrences of a step_index.")
    p_step.set_defaults(func=cmd_step)

    p_init = sub.add_parser("init-anchor", help="Initialize MEMORY_ANCHOR.md in the conversation artifact directory.")
    p_init.add_argument("--conversation-id", "-c", default="auto", help="Conversation UUID (default: auto/latest).")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing MEMORY_ANCHOR.md.")
    p_init.set_defaults(func=cmd_init_anchor)

    p_hook = sub.add_parser("hook", help="Manage the global PreInvocation guard in hooks.json.")
    p_hook.add_argument("--status", action="store_true", help="Check hook status.")
    p_hook.add_argument("--enable", action="store_true", help="Enable hook in ~/.gemini/config/hooks.json.")
    p_hook.add_argument("--disable", action="store_true", help="Disable hook in ~/.gemini/config/hooks.json.")
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
