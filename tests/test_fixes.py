#!/usr/bin/env python3
"""Regression test suite verifying fixes for REVIEW_ISSUES.md."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import context_guardian as cg  # noqa: E402
import pre_invocation_guard as pig  # noqa: E402


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        for rec in records:
            line = json.dumps(rec, ensure_ascii=False).encode("utf-8") + b"\n"
            fh.write(line)


class TestGuardianAndGuardFixes(unittest.TestCase):
    def test_h1_missing_token_telemetry_estimates_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cid = "5f9d5f3e-1111-2222-3333-444444444444"
            cdir = root / cid
            compact = cdir / ".system_generated" / "logs" / "transcript.jsonl"
            write_jsonl(
                compact,
                [
                    {"step_index": 1, "type": "USER_INPUT", "content": "Hello " * 200},
                    {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "World " * 500},
                ],
            )
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cg.main(["status", "-c", cid, "--brain-root", str(root)])
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("UNKNOWN (transcript lacks token telemetry", text)
            self.assertIn("tokens estimated", text)

    def test_h2_and_l7_concurrent_sessions_and_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for cid in ("aaaa1111", "bbbb2222"):
                compact = root / cid / ".system_generated" / "logs" / "transcript.jsonl"
                write_jsonl(compact, [{"step_index": 1, "type": "USER_INPUT", "content": "hi"}])

            # L7: Path traversal rejected
            with self.assertRaises(ValueError):
                cg.resolve_brain_dir("..", brain_root=root)
            with self.assertRaises(ValueError):
                cg.resolve_brain_dir("../outside", brain_root=root)

            # H2: Concurrent active sessions -> init-anchor with auto refuses, explicit -c succeeds
            err = io.StringIO()
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = cg.main(["init-anchor", "--brain-root", str(root)])
            self.assertEqual(rc, 1)
            self.assertIn("Multiple active conversations", err.getvalue())

            out2 = io.StringIO()
            with redirect_stdout(out2):
                rc2 = cg.main(["init-anchor", "-c", "aaaa1111", "--brain-root", str(root)])
            self.assertEqual(rc2, 0)
            self.assertTrue((root / "aaaa1111" / "MEMORY_ANCHOR.md").is_file())

    def test_h3_and_m5_truncation_classification_and_anchor_size_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cid = "b99351bf"
            full_path = root / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
            compact_path = root / cid / ".system_generated" / "logs" / "transcript.jsonl"

            records = []
            # 1 pre-checkpoint >1KB input (actually truncated)
            records.append({"step_index": 1, "type": "USER_INPUT", "content": "A" * 1500, "created_at": "t1"})
            # 60 short inputs before checkpoint
            for i in range(2, 62):
                records.append({"step_index": i, "type": "USER_INPUT", "content": f"short {i}", "created_at": f"t{i}"})
            records.append({"step_index": 62, "type": "CHECKPOINT", "content": "summary", "created_at": "t62"})
            # Post-checkpoint >1KB input (at-risk, NOT yet truncated by checkpoint!)
            records.append({"step_index": 63, "type": "USER_INPUT", "content": "B" * 1500, "created_at": "t63"})
            # 600-byte input (under 1000B threshold -> normal)
            records.append({"step_index": 64, "type": "USER_INPUT", "content": "C" * 600, "created_at": "t64"})

            write_jsonl(full_path, records)
            write_jsonl(compact_path, records)

            parsed, _ = cg.iter_jsonl_records(full_path)
            cg.canonicalize_records(parsed)
            inputs = cg.collect_user_inputs(parsed, latest_active_ckpt_line=62)
            by_step = {item["step_index"]: item for item in inputs}
            self.assertEqual(by_step[1]["status"], "truncated")
            self.assertEqual(by_step[63]["status"], "at_risk")
            self.assertEqual(by_step[64]["status"], "normal")

            # M5: init-anchor stays well under 4KB despite 63 user inputs
            with redirect_stdout(io.StringIO()):
                cg.main(["init-anchor", "-c", cid, "--brain-root", str(root)])
            anchor_file = root / cid / "MEMORY_ANCHOR.md"
            self.assertLess(anchor_file.stat().st_size, 4500)

    def test_m1_and_m2_rewound_checkpoint_and_post_compaction_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cid = "abcd1234"
            compact_path = root / cid / ".system_generated" / "logs" / "transcript.jsonl"
            records = [
                {"step_index": 441, "type": "USER_INPUT", "content": "try 1"},
                {"step_index": 442, "type": "CHECKPOINT", "content": "rewound ckpt", "created_at": "10:02:25Z"},
                {"step_index": 443, "type": "PLANNER_RESPONSE", "input_tokens": 16000, "cache_read_tokens": 2000},
                # Rewind back to 441!
                {"step_index": 441, "type": "USER_INPUT", "content": "try 2"},
                {"step_index": 442, "type": "PLANNER_RESPONSE", "input_tokens": 240000, "cache_read_tokens": 9000, "content": "active resp"},
            ]
            write_jsonl(compact_path, records)

            out = io.StringIO()
            with redirect_stdout(out):
                cg.main(["status", "-c", cid, "--brain-root", str(root)])
            status_text = out.getvalue()
            self.assertIn("0 active (+1 rewound)", status_text)
            self.assertIn("249,000", status_text)

            # step 442 warns about hidden superseded occurrence
            step_out, step_err = io.StringIO(), io.StringIO()
            with redirect_stdout(step_out), redirect_stderr(step_err):
                cg.main(["step", "442", "-c", cid, "--brain-root", str(root)])
            self.assertIn("Hid 1 earlier superseded occurrence", step_err.getvalue())
            self.assertIn("active resp", step_out.getvalue())

            # M2: Add a fresh active CHECKPOINT at step 443 with no post-compaction planner response yet
            records.append({"step_index": 443, "type": "CHECKPOINT", "content": "real ckpt", "created_at": "10:05:00Z"})
            write_jsonl(compact_path, records)
            out2 = io.StringIO()
            with redirect_stdout(out2):
                cg.main(["status", "-c", cid, "--brain-root", str(root)])
            self.assertIn("POST-COMPACTION (just compacted at step 443", out2.getvalue())

    def test_m3_and_m4_windows_backslash_search_and_nested_tool_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cid = "dddd4444"
            full_path = root / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
            compact_path = root / cid / ".system_generated" / "logs" / "transcript.jsonl"
            records = [
                {
                    "step_index": 10,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "tool_calls": [
                        {
                            "name": "run_command",
                            "args": {"Cwd": json.dumps(r"C:\temp\new\file.txt")},
                        }
                    ],
                },
                {
                    "step_index": 11,
                    "type": "ERROR_MESSAGE",
                    "source": "SYSTEM",
                    "error": "Fatal crash in worker",
                },
            ]
            write_jsonl(full_path, records)
            write_jsonl(compact_path, records)

            # Search Windows path containing \t, \n, \f without -F -> auto literal fallback finds tool_calls!
            out = io.StringIO()
            with redirect_stdout(out):
                cg.main(["search", r"C:\temp\new\file.txt", "-c", cid, "--brain-root", str(root)])
            self.assertIn("Total matches shown: 1", out.getvalue())

            # L4: Search ERROR_MESSAGE type and error field
            out_err = io.StringIO()
            with redirect_stdout(out_err):
                cg.main(["search", "Fatal crash", "--type", "ERROR_MESSAGE", "-c", cid, "--brain-root", str(root)])
            self.assertIn("Total matches shown: 1", out_err.getvalue())

    def test_m6_m7_l10_hooks_bom_and_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = Path(tmp) / "hooks.json"
            # Write UTF-8 BOM with existing custom timeout
            hooks_path.write_bytes(
                "\ufeff".encode("utf-8")
                + json.dumps({cg.HOOK_KEY: {"enabled": True, "PreInvocation": [{"timeout": 25}]}}).encode("utf-8")
            )
            out = io.StringIO()
            with redirect_stdout(out):
                # Default action without flags -> --status
                rc = cg.main(["hook", "--hooks-path", str(hooks_path)])
            self.assertEqual(rc, 0)
            self.assertIn("ENABLED", out.getvalue())

            with redirect_stdout(io.StringIO()):
                cg.main(["hook", "--enable", "--hooks-path", str(hooks_path)])
            saved = json.loads(hooks_path.read_text(encoding="utf-8"))
            cmd = saved[cg.HOOK_KEY]["PreInvocation"][0]["command"]
            self.assertIn("-X utf8", cmd)
            self.assertEqual(saved[cg.HOOK_KEY]["PreInvocation"][0]["timeout"], 25)
            self.assertTrue(hooks_path.with_suffix(".json.bak").is_file())

    def test_med3_and_low4_tail_records_nel_and_exact_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "transcript.jsonl"
            # Include raw U+0085 (NEL) and U+2028 inside JSON string
            r1 = {"step_index": 1, "type": "USER_INPUT", "content": "line1"}
            r2 = {"step_index": 2, "type": "USER_INPUT", "content": "has\u0085nel\u2028and_ls"}
            r3 = {"step_index": 3, "type": "USER_INPUT", "content": "line3"}
            b1 = json.dumps(r1, ensure_ascii=False).encode("utf-8") + b"\n"
            b2 = json.dumps(r2, ensure_ascii=False).encode("utf-8") + b"\n"
            b3 = json.dumps(r3, ensure_ascii=False).encode("utf-8") + b"\n"
            p.write_bytes(b1 + b2 + b3)

            # Exact boundary: tail_bytes == len(b2 + b3), so start lands right at beginning of b2!
            recs = pig.read_tail_records(p, len(b2) + len(b3))
            self.assertEqual([r["step_index"] for r in recs], [2, 3])
            self.assertIn("\u0085", recs[0]["content"])

    def test_high1_high2_low6_low7_low8_guard_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            art_dir = Path(tmp) / "brain" / "0864f417"
            logs_dir = art_dir / ".system_generated" / "logs"
            t_path = logs_dir / "transcript.jsonl"
            f_path = logs_dir / "transcript_full.jsonl"

            # 1) HIGH-1: Stale checkpoint (followed by 3 planner responses) on fresh hook enable -> NO injection
            stale_records = [
                {"step_index": 10, "type": "USER_INPUT", "content": "old prompt"},
                {"step_index": 11, "type": "CHECKPOINT", "created_at": "t11"},
                {"step_index": 12, "type": "PLANNER_RESPONSE", "input_tokens": 20000},
                {"step_index": 13, "type": "PLANNER_RESPONSE", "input_tokens": 30000},
                {"step_index": 14, "type": "PLANNER_RESPONSE", "input_tokens": 40000},
            ]
            write_jsonl(t_path, stale_records)
            write_jsonl(f_path, stale_records)

            payload = {
                "transcriptPath": str(t_path),
                "artifactDirectoryPath": str(art_dir),
                "conversationId": "0864f417",
            }
            res, pending = pig.run_guard(payload)
            self.assertEqual(res, {})
            self.assertIsNone(pending)
            state = pig.load_state(art_dir / "scratch" / ".compaction_guard_state.json")
            self.assertEqual(state["last_handled_checkpoint_step"], 11)

            # 2) LOW-7: Fresh anchor within 90s suppresses Band 1 (390k) WITHOUT advancing last_warned_band
            anchor_path = art_dir / "MEMORY_ANCHOR.md"
            anchor_path.write_text("# Anchor\n", encoding="utf-8")
            stale_records.append({"step_index": 15, "type": "PLANNER_RESPONSE", "input_tokens": 390000})
            write_jsonl(t_path, stale_records)

            res_b1, pending_b1 = pig.run_guard(payload)
            self.assertEqual(res_b1, {})
            self.assertIsNone(pending_b1)
            state_after_b1 = pig.load_state(art_dir / "scratch" / ".compaction_guard_state.json")
            self.assertEqual(state_after_b1["last_warned_band"], 0)

            # Age anchor to 45s (> 30s critical grace) and cross Band 2 (460k) -> MUST fire CRITICAL!
            old_mtime = time.time() - 45
            os.utime(anchor_path, (old_mtime, old_mtime))
            stale_records.append({"step_index": 16, "type": "PLANNER_RESPONSE", "input_tokens": 460000})
            write_jsonl(t_path, stale_records)

            proc = subprocess.run(
                [sys.executable, "-X", "utf8", str(SCRIPTS_DIR / "pre_invocation_guard.py")],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=True,
            )
            # LOW-6: Output must be pure ASCII JSON
            self.assertTrue(proc.stdout.isascii())
            parsed_out = json.loads(proc.stdout)
            self.assertIn("injectSteps", parsed_out)
            self.assertIn("CRITICAL", parsed_out["injectSteps"][0]["systemMessage"]["systemMessage"])

            # 3) Fresh CHECKPOINT -> injects recovery bundle
            stale_records.append({"step_index": 17, "type": "USER_INPUT", "content": "X" * 1200, "created_at": "t17"})
            stale_records.append({"step_index": 18, "type": "CHECKPOINT", "created_at": "t18"})
            write_jsonl(t_path, stale_records)
            write_jsonl(f_path, stale_records)

            proc2 = subprocess.run(
                [sys.executable, "-X", "utf8", str(SCRIPTS_DIR / "pre_invocation_guard.py")],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertTrue(proc2.stdout.isascii())
            parsed_ckpt = json.loads(proc2.stdout)
            sys_msg = parsed_ckpt["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("step_index=18", sys_msg)
            self.assertIn("X" * 100, sys_msg)


if __name__ == "__main__":
    unittest.main()
