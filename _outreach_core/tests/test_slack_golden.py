"""v30 §WS-G — Slack format golden files.

The per-target Slack line (notify.post_target_event) and the actionable
Slack payload (resolve_queue.build_actionable_payload) are the user-visible
output of WS-D and WS-F. The exact text/structure is part of the operator
contract: a stealth change ("emoji moved", "label shortened") can break
operator habits or downstream tooling that greps the Slack thread.

These tests lock the format against a committed golden file. When the
format LEGITIMATELY changes, run::

    UPDATE_GOLDEN=1 python -m pytest _outreach_core/tests/test_slack_golden.py

to refresh every fixture in one pass; then ``git diff`` shows exactly what
moved.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from _outreach_core import notify  # noqa: E402
from _outreach_core import resolve_queue as rq  # noqa: E402


GOLDEN_DIR = Path(__file__).parent / "fixtures" / "slack_golden"


def _assert_golden(testcase: unittest.TestCase, name: str, produced: str) -> None:
    """Compare ``produced`` against ``GOLDEN_DIR/name``. Refresh the file
    when ``UPDATE_GOLDEN=1`` is set in the env."""
    path = GOLDEN_DIR / name
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(produced, encoding="utf-8")
        return
    expected = path.read_text(encoding="utf-8")
    # Strip trailing newline from file read (text editors sometimes add one).
    if expected.endswith("\n") and not produced.endswith("\n"):
        expected = expected[:-1]
    testcase.assertEqual(
        produced, expected,
        f"Slack format drift detected for {name}. "
        f"Re-run with UPDATE_GOLDEN=1 if the change is intentional.",
    )


class TestTargetEventLineFormat(unittest.TestCase):
    def test_sent(self) -> None:
        text = notify.format_target_event(
            stage="send", status="sent",
            name="株式会社LegalOn Technologies",
            idx=1, total=7,
            detail={
                "verify_status": "sent_ok",
                "form_url": "https://legalontech.example/contact/",
            },
        )
        _assert_golden(self, "target_sent.txt", text)

    def test_skipped_captcha(self) -> None:
        text = notify.format_target_event(
            stage="send", status="skipped",
            name="株式会社ハイブリッドテクノロジーズ",
            idx=3, total=7,
            detail={"reason": "captcha_human_required: v2_checkbox"},
        )
        _assert_golden(self, "target_skipped_captcha.txt", text)

    def test_queued_validation(self) -> None:
        text = notify.format_target_event(
            stage="send", status="queued",
            name="富士ソフト株式会社",
            idx=6, total=7,
            detail={
                "reason_class": "validation_unrecoverable",
                "form_url": "https://sem-inq.example/public/application/add/68",
            },
        )
        _assert_golden(self, "target_queued_validation.txt", text)

    def test_timeout(self) -> None:
        text = notify.format_target_event(
            stage="send", status="timeout",
            name="株式会社Faber Company",
            idx=4, total=7,
            detail={"reason_class": "target_timeout", "elapsed_sec": 300},
        )
        _assert_golden(self, "target_timeout.txt", text)

    def test_queued_with_wizard_stuck(self) -> None:
        # wizard_stuck is a dict (from wizard.StuckReason.as_payload); the
        # formatter must extract the "reason" inner key, not str(dict).
        text = notify.format_target_event(
            stage="send", status="queued",
            name="富士ソフト株式会社",
            idx=2, total=5,
            detail={
                "wizard_stuck": {
                    "reason": "same_button_repeated",
                    "detail": "button '次へ' clicked 3x",
                },
            },
        )
        _assert_golden(self, "target_wizard_stuck.txt", text)


class TestActionablePayloadGolden(unittest.TestCase):
    def test_validation_unrecoverable_actions(self) -> None:
        entry = {
            "target_id": "fujisoft",
            "name": "富士ソフト株式会社",
            "reason_class": "validation_unrecoverable",
            "reason": "想定外の必須項目...",
            "form_url": "https://sem-inq.example/public/application/add/68",
            "diagnostics": {
                "url": "https://sem-inq.example/public/application/add/68",
                "buttons": ["次へ"],
            },
        }
        actions = rq.build_actionable_payload(entry, auto_resolver=True)["actions"]
        produced = json.dumps(actions, ensure_ascii=False, indent=2) + "\n"
        _assert_golden(self, "actions_validation_unrecoverable.json", produced)

    def test_first_submit_not_found_actions(self) -> None:
        entry = {
            "target_id": "geocode",
            "name": "株式会社ジオコード",
            "reason_class": "first_submit_not_found",
            "reason": "submit button not found",
            "form_url": "https://www.example.co.jp/contact/",
            "diagnostics": {"url": "https://www.example.co.jp/contact/"},
        }
        actions = rq.build_actionable_payload(entry, auto_resolver=True)["actions"]
        produced = json.dumps(actions, ensure_ascii=False, indent=2) + "\n"
        _assert_golden(self, "actions_first_submit_not_found.json", produced)


if __name__ == "__main__":
    unittest.main()
