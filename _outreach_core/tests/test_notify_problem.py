"""Problem notification path for v28 P2."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import notify  # noqa: E402


class TestNotifyProblem(unittest.TestCase):
    def setUp(self) -> None:
        notify._PROBLEM_RECENT.clear()

    def tearDown(self) -> None:
        notify._PROBLEM_RECENT.clear()

    def test_recent_problem_seen_boundary(self) -> None:
        recent = {"needs_attention:t1": 100.0}
        self.assertTrue(
            notify._recent_problem_seen(
                "needs_attention:t1", 150.0, recent=recent, window_sec=60
            )
        )
        self.assertFalse(
            notify._recent_problem_seen(
                "needs_attention:t1", 160.0, recent=recent, window_sec=60
            )
        )
        self.assertFalse(
            notify._recent_problem_seen(
                "needs_attention:t1", 150.0, recent=recent, window_sec=0
            )
        )

    def test_format_problem_message_has_required_context(self) -> None:
        text = notify.format_problem_message(
            "needs_attention",
            {
                "target_id": "t1",
                "name": "ブックオフグループホールディングス株式会社",
                "form_url": "https://example.co.jp/contact",
                "action_needed": "field_values",
            },
            {"root_cause": "想定外のプルダウン"},
        )
        self.assertIn("根本原因: 想定外のプルダウン", text)
        self.assertIn("会社: ブックオフグループホールディングス株式会社", text)
        self.assertIn("URL: https://example.co.jp/contact", text)
        self.assertIn("次アクション:", text)

    def test_format_problem_message_humanizes_reason_and_action_codes(self) -> None:
        text = notify.format_problem_message(
            "needs_attention",
            {
                "target_id": "t1",
                "name": "株式会社X",
                "reason_class": "target_timeout",
                "action_needed": "manual_verify",
            },
            {},
        )
        self.assertIn("根本原因: 1社処理がタイムアウトしました", text)
        self.assertIn("次アクション: 送信済みか目視確認してください。", text)
        self.assertNotIn("target_timeout", text)
        self.assertNotIn("manual_verify", text)

    def test_format_problem_message_humanizes_form_reason_codes(self) -> None:
        text = notify.format_problem_message(
            "needs_attention",
            {
                "target_id": "t1",
                "name": "株式会社フォーム",
                "reason_class": "page_has_no_form",
                "action_needed": "auto_resolve",
            },
            {},
        )
        self.assertIn("根本原因: フォーム未検出", text)
        self.assertIn("次アクション: 自動リゾルバで再試行します。", text)
        self.assertNotIn("page_has_no_form", text)
        self.assertNotIn("auto_resolve", text)

    def test_post_problem_dedupes_same_target_kind(self) -> None:
        with mock.patch.object(notify, "_webhook_url", return_value=""), \
                mock.patch.object(notify, "openclaw_slack_ready", return_value=True), \
                mock.patch.object(notify, "_post_slack_api", return_value=True) as api:
            self.assertTrue(
                notify.post_problem(
                    "needs_attention",
                    {"target_id": "t1", "name": "会社1"},
                    {"reason": "validation"},
                    thread_ts="123.456",
                )
            )
            self.assertFalse(
                notify.post_problem(
                    "needs_attention",
                    {"target_id": "t1", "name": "会社1"},
                    {"reason": "validation"},
                    thread_ts="123.456",
                )
            )
        api.assert_called_once()

    def test_thread_prefers_bot_even_when_webhook_exists(self) -> None:
        with mock.patch.dict(os.environ, {"DOORMAN_SLACK_THREAD_TS": "123.456"}), \
                mock.patch.object(notify, "_webhook_url", return_value="https://hooks.example"), \
                mock.patch.object(notify, "openclaw_slack_ready", return_value=True), \
                mock.patch.object(notify, "_post_slack_api", return_value=True) as api, \
                mock.patch.object(notify, "_post_webhook", return_value=True) as webhook:
            self.assertTrue(
                notify.post_problem(
                    "target_timeout",
                    {"target_id": "t2", "name": "会社2"},
                    {"reason": "timeout"},
                )
            )
        api.assert_called_once()
        webhook.assert_not_called()

    def test_thread_problem_falls_back_to_channel_bot(self) -> None:
        with mock.patch.object(notify, "_webhook_url", return_value="https://hooks.example"), \
                mock.patch.object(notify, "openclaw_slack_ready", return_value=True), \
                mock.patch.object(notify, "_post_slack_api", side_effect=[False, True]) as api, \
                mock.patch.object(notify, "_post_webhook", return_value=True) as webhook:
            self.assertTrue(
                notify.post_problem(
                    "needs_attention",
                    {"target_id": "t3", "name": "会社3"},
                    {"reason": "validation"},
                    thread_ts="123.456",
                    channel_id="C123",
                )
            )
        self.assertEqual(api.call_count, 2)
        self.assertEqual(api.call_args_list[0].kwargs["thread_ts"], "123.456")
        self.assertIsNone(api.call_args_list[1].kwargs["thread_ts"])
        self.assertIn("スレッド投稿に失敗", api.call_args_list[1].args[0])
        webhook.assert_not_called()

    def test_needs_attention_append_posts_problem_once(self) -> None:
        from _outreach_core.verify import append_needs_attention

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(notify, "post_problem", return_value=True) as post_problem:
            append_needs_attention(Path(tmp), {"target_id": "t1", "name": "会社1", "reason": "x"})
            post_problem.assert_called_once()
            row = json.loads((Path(tmp) / "needs_attention.jsonl").read_text().strip())
        self.assertEqual(row["status"], "open")


if __name__ == "__main__":
    unittest.main()
