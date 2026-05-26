"""events.jsonl + report CLI (v4 §13)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import events as ev
from _outreach_core.draft import stage_draft


class TestEvents(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        ev.configure(skill="jp-form-outreach", data_dir=self.data, run_id="20260526-120000")

    def tearDown(self) -> None:
        self.tmp.cleanup()
        ev._ctx.skill = ""
        ev._ctx.data_dir = None
        ev._ctx.run_id = None

    def test_emit_schema(self) -> None:
        ev.emit("draft.requested", stage="draft", target_id="t1", payload={"model": "x"})
        row = json.loads((self.data / "events.jsonl").read_text().strip())
        self.assertEqual(row["v"], 1)
        self.assertIn("ts", row)
        self.assertEqual(row["kind"], "draft.requested")
        self.assertEqual(row["stage"], "draft")
        self.assertEqual(row["run_id"], "20260526-120000")

    def test_redact_sender_strips_pii(self) -> None:
        sender = {"email": "secret@example.com", "company": "Torana"}
        obj = {"email": "secret@example.com", "note": "contact secret@example.com"}
        out = ev.redact_sender(obj, sender)
        self.assertEqual(out["email"], "<sender.email>")
        self.assertNotIn("secret@example.com", out["note"])

    def test_stage_draft_emits_requested_and_emitted(self) -> None:
        leads = self.data.parent / "leads.jsonl"
        out = self.data / "drafts.jsonl"
        leads.write_text(
            json.dumps({"id": "co1", "name": "テスト社", "hook_context": "x" * 50})
            + "\n",
            encoding="utf-8",
        )

        def fake_infer(prompt: str, model: str) -> str:
            return json.dumps(
                {"subject": "ご相談", "body": "短い本文です。"},
                ensure_ascii=False,
            )

        def skip_fn(_: list) -> None:
            pass

        def user_block(lead: dict, max_c: int) -> str:
            return f"<user max={max_c}></user>"

        prompts = Path(__file__).resolve().parent.parent.parent / "jp-form-outreach" / "prompts"
        config = {"model": {"name": "test", "max_chars": 400}, "sender": {"company": "T"}}
        stage_draft(
            leads,
            out,
            config,
            prompts_dir=prompts,
            build_user_block=user_block,
            oc_infer_fn=fake_infer,
            append_skip_fn=skip_fn,
            default_model="test",
            skill="jp-form-outreach",
            data_dir=self.data,
            sender=config["sender"],
        )
        kinds = [json.loads(l)["kind"] for l in (self.data / "events.jsonl").read_text().splitlines() if l.strip()]
        self.assertIn("draft.requested", kinds)
        self.assertIn("draft.emitted", kinds)

    def test_report_draft_quality_empty(self) -> None:
        repo = Path(__file__).resolve().parent.parent.parent
        env = {**os.environ, "PYTHONPATH": str(repo)}
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "_outreach_core.helpers.report",
                "draft-quality",
                "--since",
                "7d",
                "--skill",
                "jp-form-outreach",
            ],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Draft Quality Report", proc.stdout)


if __name__ == "__main__":
    unittest.main()
