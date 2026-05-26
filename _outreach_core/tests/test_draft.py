"""Draft char_limit enforcement."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.draft import (
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


if __name__ == "__main__":
    unittest.main()
