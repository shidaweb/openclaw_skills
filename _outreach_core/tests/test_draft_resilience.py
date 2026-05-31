from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.draft import stage_draft


def _user_block(lead: dict, max_chars: int) -> str:
    return f"<user id={lead.get('id')} max={max_chars}></user>"


def _skip_sink(targets: list[dict]) -> None:
    _ = targets


class TestDraftResilience(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.in_path = self.root / "enriched.jsonl"
        self.out_path = self.root / "drafts.jsonl"
        self.prompts = Path(__file__).resolve().parent.parent.parent / "jp-form-outreach" / "prompts"
        self.config = {
            "model": {"name": "fake-model", "max_chars": 400},
            "sender": {"company": "Torana", "name": "Alice"},
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_leads(self, n: int) -> None:
        rows = []
        for i in range(1, n + 1):
            rows.append({"id": f"id{i}", "name": f"Lead{i}", "hook_context": f"ctx-{i}"})
        self.in_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8",
        )

    def test_incremental_persistence_survives_midrun_crash(self) -> None:
        self._write_leads(5)
        calls = {"n": 0}

        def flaky_infer(prompt: str, model: str) -> str:
            _ = prompt, model
            calls["n"] += 1
            if calls["n"] == 4:
                raise RuntimeError("synthetic crash")
            return '{"subject":"件名","body":"本文"}'

        with self.assertRaises(RuntimeError):
            stage_draft(
                self.in_path,
                self.out_path,
                self.config,
                prompts_dir=self.prompts,
                build_user_block=_user_block,
                oc_infer_fn=flaky_infer,
                append_skip_fn=_skip_sink,
                default_model="fake-model",
            )

        rows_after_crash = [
            json.loads(line)
            for line in self.out_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(rows_after_crash), 3)
        self.assertEqual([r["id"] for r in rows_after_crash], ["id1", "id2", "id3"])

        calls2 = {"n": 0}

        def stable_infer(prompt: str, model: str) -> str:
            _ = prompt, model
            calls2["n"] += 1
            return '{"subject":"件名","body":"本文"}'

        stage_draft(
            self.in_path,
            self.out_path,
            self.config,
            prompts_dir=self.prompts,
            build_user_block=_user_block,
            oc_infer_fn=stable_infer,
            append_skip_fn=_skip_sink,
            default_model="fake-model",
        )
        rows = [
            json.loads(line)
            for line in self.out_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(rows), 5)
        self.assertEqual({r["id"] for r in rows}, {"id1", "id2", "id3", "id4", "id5"})
        self.assertEqual(calls2["n"], 2)  # only the missing ids are drafted

    def test_parse_retry_cap_records_skip(self) -> None:
        self._write_leads(1)
        calls = {"n": 0}
        captured_skips: list[dict] = []

        def bad_infer(prompt: str, model: str) -> str:
            _ = prompt, model
            calls["n"] += 1
            return "```json\nnot-json\n```"

        def capture_skip(rows: list[dict]) -> None:
            captured_skips.extend(rows)

        cfg = {
            **self.config,
            "draft": {"parse_retry_max_attempts": 2, "lead_soft_timeout_sec": 30},
        }
        stage_draft(
            self.in_path,
            self.out_path,
            cfg,
            prompts_dir=self.prompts,
            build_user_block=_user_block,
            oc_infer_fn=bad_infer,
            append_skip_fn=capture_skip,
            default_model="fake-model",
        )
        self.assertEqual(calls["n"], 2)
        rows = [json.loads(line) for line in self.out_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].get("draft") or {}).get("subject"), "SKIP")
        self.assertIn("parse_error", (rows[0].get("draft") or {}).get("body", ""))
        self.assertEqual(len(captured_skips), 1)
        self.assertEqual((captured_skips[0].get("draft") or {}).get("subject"), "SKIP")

    def test_limit_caps_processed_rows(self) -> None:
        self._write_leads(4)
        calls = {"n": 0}

        def infer(prompt: str, model: str) -> str:
            _ = prompt, model
            calls["n"] += 1
            return '{"subject":"件名","body":"本文"}'

        stage_draft(
            self.in_path,
            self.out_path,
            self.config,
            prompts_dir=self.prompts,
            build_user_block=_user_block,
            oc_infer_fn=infer,
            append_skip_fn=_skip_sink,
            default_model="fake-model",
            limit=2,
        )
        self.assertEqual(calls["n"], 2)
        rows = [json.loads(line) for line in self.out_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
