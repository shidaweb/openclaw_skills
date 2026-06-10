"""Tests for confirm-page instruction detection (v19).

Pages that tell the human to click send — 「上記の内容でよろしければ、送信ボタンを
クリックしてください」 — must be recognized so we can confidently click a
generically-labelled send button.
"""

from __future__ import annotations

import unittest

from _outreach_core import submit_progress as sp


class TestDetectConfirmInstruction(unittest.TestCase):
    def test_classic_confirm_instruction(self):
        self.assertTrue(sp.detect_confirm_instruction(
            "上記の内容でよろしければ、送信ボタンをクリックしてください。"
        ))

    def test_content_confirm_variants(self):
        for t in (
            "ご記入内容をご確認の上、送信ボタンを押してください。",
            "入力内容をご確認のうえ送信してください",
            "下記の内容でお間違いなければ送信ボタンを押下してください",
            "内容をご確認の上、お送りください。",
        ):
            self.assertTrue(sp.detect_confirm_instruction(t), t)

    def test_bare_imperative(self):
        self.assertTrue(sp.detect_confirm_instruction("送信ボタンを押してください"))
        self.assertTrue(sp.detect_confirm_instruction("確定ボタンをクリックしてください"))

    def test_negatives(self):
        for t in (
            "",
            None,
            "お問い合わせフォーム",
            "必須項目をご入力ください。",          # asks to INPUT, not send
            "個人情報の取り扱いについて同意してください。",
            "ご意見・ご感想をお聞かせください。",
        ):
            self.assertFalse(sp.detect_confirm_instruction(t), repr(t))

    def test_preamble_far_from_send_is_not_matched(self):
        # 内容 preamble and 送信 too far apart (different topic) → not a confirm cue.
        t = "上記の内容は参考情報です。" + "あ" * 200 + "別途お電話で送信先をご案内します。"
        self.assertFalse(sp.detect_confirm_instruction(t))

    def test_multiline_text(self):
        t = "ご入力ありがとうございます。\n\n上記の内容でよろしければ\n送信ボタンをクリックしてください。"
        self.assertTrue(sp.detect_confirm_instruction(t))


if __name__ == "__main__":
    unittest.main()
