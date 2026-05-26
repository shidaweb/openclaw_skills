"""Verify send completion and needs_attention detection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.verify import (
    append_needs_attention,
    verify_send_completed,
)


class TestVerify(unittest.TestCase):
    def test_uncertain_without_success_keywords(self) -> None:
        target = {"id": "t1", "name": "テスト株式会社", "form_fields": {"inputs": []}}
        snap = "<html><body>フォーム送信処理中です</body></html>"
        result = verify_send_completed(target, "jp_form", snapshot=snap)
        self.assertEqual(result["status"], "uncertain")

    def test_needs_attention_plan_gap(self) -> None:
        target = {
            "id": "t2",
            "name": "Example Co",
            "form_fields": {
                "inputs": [
                    {"name": "industry", "required": True, "label": "業界"},
                ],
            },
        }
        plan = {"fields": [{"name": "email", "action": "set_text"}]}
        result = verify_send_completed(target, "jp_form", snapshot="", plan=plan)
        self.assertEqual(result["status"], "needs_attention")
        self.assertTrue(result.get("unresolved_fields"))

    def test_uncertain_writes_needs_attention_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            target = {"id": "t3", "name": "Co"}
            result = verify_send_completed(
                target, "jp_form", snapshot="no keywords here"
            )
            from _outreach_core.verify import handle_verify_result

            handle_verify_result(target, result, data, channel="jp_form")
            path = data / "needs_attention.jsonl"
            self.assertTrue(path.exists())
            row = json.loads(path.read_text().strip().splitlines()[-1])
            self.assertEqual(row["status"], "open")


if __name__ == "__main__":
    unittest.main()
