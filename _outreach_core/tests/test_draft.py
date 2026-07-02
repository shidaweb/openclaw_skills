"""Draft char_limit enforcement."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.draft import (
    body_too_similar,
    opening_too_similar,
    enforce_char_limit,
    hard_truncate_draft,
    resolve_max_chars,
)


class TestDraftCharLimit(unittest.TestCase):
    def test_resolve_max_chars_from_textarea_maxlength(self) -> None:
        lead = {
            "form_fields": {"textareas": [{"max_length": 500}]},
            "char_limit": 400,
        }
        config = {"model": {"max_chars": 400, "max_chars_extended": 1800}}
        self.assertEqual(
            resolve_max_chars(lead, config, default_max=400, extended_max=1800),
            500,
        )

    def test_enforce_char_limit_compresses_via_infer(self) -> None:
        lead: dict = {}
        config = {"model": {"name": "claude-cli/claude-opus-4-7"}}
        draft = {"subject": "ご相談", "body": "あ" * 450}
        short = {"subject": "ご相談", "body": "短い本文"}

        def fake_infer(prompt: str, model: str) -> str:
            return '{"subject": "ご相談", "body": "短い本文"}'

        out = enforce_char_limit(
            lead, draft, config, 400, oc_infer_fn=fake_infer, label="t"
        )
        self.assertLessEqual(len(out["body"]), 400)

    def test_hard_truncate_keeps_calendar_tail(self) -> None:
        body = ("本文" * 80) + "\nカレンダー：https://tenbin.link/book/x"
        draft = {"subject": "x", "body": body}
        out = hard_truncate_draft(draft, 120)
        self.assertLessEqual(len(out["body"]), 120)
        self.assertIn("tenbin.link", out["body"])

    def test_hard_truncate_cuts_at_sentence_boundary(self) -> None:
        # v31 §WS4a — the cut lands on 。 instead of mid-sentence + …
        body = (
            "はじめまして、株式会社トラーナの志田と申します。"
            "御社のサービスを拝見しご連絡いたしました。"
            "弊社はサブスク事業者向けのLINE CRM支援を行っております。"
            "ぜひ一度お話しできれば幸いです。"
        )
        draft = {"subject": "x", "body": body}
        out = hard_truncate_draft(draft, 60)
        self.assertLessEqual(len(out["body"]), 60)
        self.assertTrue(out["body"].endswith("。"), out["body"])
        self.assertNotIn("…", out["body"])

    def test_hard_truncate_falls_back_when_no_boundary(self) -> None:
        # one giant sentence: honoring a boundary would drop everything →
        # keep the legacy char cut + …
        body = "あ" * 500
        out = hard_truncate_draft({"subject": "x", "body": body}, 100)
        self.assertLessEqual(len(out["body"]), 100)
        self.assertTrue(out["body"].endswith("…"))


class TestBodySimilarity(unittest.TestCase):
    """v31 §WS4b — whole-body trigram guard."""

    BASE = (
        "はじめまして、株式会社トラーナの志田と申します。"
        "貴社のサブスク事業を拝見しご連絡いたしました。"
        "弊社はLINEを活用したCRM改善のご支援を行っており、"
        "解約率の低減と再入会の促進に実績がございます。"
        "ぜひ一度、情報交換のお時間をいただけますと幸いです。"
    )

    def test_identical_body_is_similar(self) -> None:
        self.assertTrue(body_too_similar(self.BASE, [self.BASE]))

    def test_distinct_opener_same_boilerplate_is_similar(self) -> None:
        # the case opening_too_similar misses: only the first sentence differs
        variant = self.BASE.replace(
            "はじめまして、株式会社トラーナの志田と申します。",
            "突然のご連絡失礼いたします。トラーナの志田でございます。",
        )
        self.assertFalse(
            opening_too_similar(variant, [self.BASE]),
            "premise: the opening guard must NOT fire for this pair",
        )
        self.assertTrue(body_too_similar(variant, [self.BASE]))

    def test_genuinely_different_body_is_not_similar(self) -> None:
        other = (
            "貴社の玩具サブスクリプション事業についてお伺いしたく、"
            "ご連絡差し上げました。物流面の課題解決を専門としており、"
            "在庫回転率の改善事例を多数保有しております。"
            "資料をお送りしてもよろしいでしょうか。"
        )
        self.assertFalse(body_too_similar(other, [self.BASE]))

    def test_empty_inputs_are_safe(self) -> None:
        self.assertFalse(body_too_similar("", [self.BASE]))
        self.assertFalse(body_too_similar(self.BASE, []))
        self.assertFalse(body_too_similar(self.BASE, [""]))


if __name__ == "__main__":
    unittest.main()
