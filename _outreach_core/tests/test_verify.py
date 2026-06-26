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

            with mock.patch("_outreach_core.notify.post", return_value=True), \
                    mock.patch("_outreach_core.notify.post_problem", return_value=True):
                handle_verify_result(target, result, data, channel="jp_form")
            path = data / "needs_attention.jsonl"
            self.assertTrue(path.exists())
            row = json.loads(path.read_text().strip().splitlines()[-1])
            self.assertEqual(row["status"], "open")

    def test_handle_verify_result_can_suppress_attention_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            target = {"id": "t4", "name": "リゾルバ確認用株式会社"}
            result = {
                "status": "uncertain",
                "reason": "リゾルバ確認用株式会社: 送信完了画面が確認できません",
            }
            from _outreach_core.verify import handle_verify_result

            with mock.patch("_outreach_core.notify.post_problem", return_value=True) as post_problem:
                outcome = handle_verify_result(
                    target,
                    result,
                    data,
                    channel="jp_form",
                    record_attention=False,
                )

            self.assertEqual(outcome, "uncertain")
            self.assertFalse((data / "needs_attention.jsonl").exists())
            post_problem.assert_not_called()

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

    def test_jp_form_error_banner_beats_weak_success_keyword(self) -> None:
        target = {"id": "b1", "name": "Benesse", "form_fields": {"inputs": []}}
        snap = "ご入力 ご確認 完了\n入力内容にエラーがあります。内容をご確認ください。"
        result = verify_send_completed(
            target,
            "jp_form",
            snapshot=snap,
            browser_verify={"url": "https://www.example.co.jp/entry/inquiry", "text": snap},
        )
        self.assertEqual(result["status"], "needs_attention")

    def test_jp_form_thanks_with_unrelated_search_form_is_ok(self) -> None:
        target = {"id": "h1", "name": "はるやま", "form_fields": {"inputs": []}}
        snap = "お問い合わせありがとうございました。内容を確認次第、ご連絡させて頂きます。"
        result = verify_send_completed(
            target,
            "jp_form",
            snapshot=snap,
            browser_verify={"url": "https://example.co.jp/contact/index.php?status=dec", "text": snap},
            evaluate_fn=lambda _js: {
                "visible_forms": 1,
                "visible_textareas": 0,
                "editable_visible": 1,
                "submit_controls": 1,
                "final_submit_controls": 0,
                "empty_required": [],
            },
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("success_without_pending_submit", result["evidence"]["send_signals"])

    def test_jp_form_pre_submit_intro_is_not_success(self) -> None:
        target = {"id": "y1", "name": "山田CG", "form_fields": {"inputs": []}}
        snap = (
            "お問い合わせ（注意事項）\n"
            "当社グループのサービスや採用情報などに関してお問い合わせを受け付けております。\n"
            "お問い合わせをお送りいただく前に、注意事項をお読みください。\n"
            "上記に同意してフォームに進む"
        )
        result = verify_send_completed(
            target,
            "jp_form",
            snapshot=snap,
            browser_verify={
                "url": "https://www.yamada-cg.co.jp/contact/",
                "text": snap,
                "visible_forms": 0,
                "visible_textareas": 0,
                "editable_visible": 0,
                "submit_controls": 0,
                "final_submit_controls": 0,
            },
        )
        self.assertEqual(result["status"], "uncertain")
        self.assertIn(
            "pre_submit_intro_success_ignored",
            result["evidence"]["send_signals"],
        )

    def test_jp_form_progress_done_label_with_pending_submit_is_not_ok(self) -> None:
        target = {"id": "p1", "name": "PDP", "form_fields": {"inputs": []}}
        snap = "入力画面 確認画面 送信完了 志田典道 shida@torana.co.jp 送信する"
        result = verify_send_completed(
            target,
            "jp_form",
            snapshot=snap,
            browser_verify={"url": "https://example.co.jp/contact/confirm", "text": snap},
            evaluate_fn=lambda _js: {
                "visible_forms": 1,
                "visible_textareas": 0,
                "editable_visible": 0,
                "submit_controls": 2,
                "final_submit_controls": 1,
                "empty_required": [],
            },
        )
        self.assertNotEqual(result["status"], "ok")
        self.assertIn("pending_submit_control", result["evidence"]["send_signals"])


if __name__ == "__main__":
    unittest.main()
