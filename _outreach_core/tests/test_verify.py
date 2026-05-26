"""Verify send completion and needs_attention detection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys
from unittest import mock

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
            target = {"id": "t3", "name": "テスト株式会社（ユニットテスト）"}
            result = verify_send_completed(
                target, "jp_form", snapshot="no keywords here"
            )
            from _outreach_core.verify import handle_verify_result

            with mock.patch("_outreach_core.notify.post", return_value=True):
                handle_verify_result(target, result, data, channel="jp_form")
            path = data / "needs_attention.jsonl"
            self.assertTrue(path.exists())
            row = json.loads(path.read_text().strip().splitlines()[-1])
            self.assertEqual(row["status"], "open")

    def test_linkedin_success_keyword_in_snapshot(self) -> None:
        target = {"id": "li1", "name": "Ada Lovelace"}
        snap = 'button "Close" [ref=e1]\nMessage sent to Ada'
        result = verify_send_completed(
            target,
            "linkedin",
            snapshot=snap,
            browser_verify={"sent": False, "reason": "compose modal still open"},
        )
        self.assertEqual(result["status"], "ok")

    def test_jp_form_url_thanks(self) -> None:
        target = {"id": "f1", "name": "Example 株式会社", "form_fields": {"inputs": []}}
        snap = "お問い合わせありがとうございました"
        result = verify_send_completed(
            target,
            "jp_form",
            snapshot=snap,
            browser_verify={
                "url": "https://example.co.jp/contact/thanks",
                "text": snap,
            },
        )
        self.assertEqual(result["status"], "ok")

    def test_jp_form_success_before_unrelated_required(self) -> None:
        """Thanks page + login form required must not block ok (nativecamp-style)."""
        target = {
            "id": "nativecamp",
            "name": "ネイティブキャンプ",
            "form_fields": {
                "inputs": [
                    {"name": "data[User][email]", "required": True, "label": "メール"},
                ],
            },
        }
        snap = "送信完了\nありがとうございました"
        result = verify_send_completed(
            target,
            "jp_form",
            snapshot=snap,
            browser_verify={
                "url": "https://nativecamp.net/contact/thanks",
                "text": snap,
            },
            plan={"fields": []},
            evaluate_fn=lambda _js: {
                "empty_required": [{"name": "data[User][email]", "label": "メール", "type": "text"}],
            },
        )
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
