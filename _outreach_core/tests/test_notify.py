"""notify.post is no-op without webhook and never raises."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import notify


class TestNotify(unittest.TestCase):
    def test_no_webhook_returns_false(self) -> None:
        with mock.patch.object(notify, "_webhook_url", return_value=""):
            with mock.patch.object(notify, "openclaw_slack_ready", return_value=False):
                self.assertFalse(notify.post("hello"))

    def test_openclaw_bot_api_path(self) -> None:
        with mock.patch.object(notify, "_webhook_url", return_value=""):
            with mock.patch.object(notify, "openclaw_slack_ready", return_value=True):
                with mock.patch.object(notify, "_post_slack_api", return_value=True) as api:
                    self.assertTrue(notify.post("pipeline ok"))
                    api.assert_called_once()

    def test_never_raises_on_failure(self) -> None:
        with mock.patch.object(notify, "_webhook_url", return_value="http://127.0.0.1:9/"), \
                mock.patch.object(notify, "openclaw_slack_ready", return_value=False):
            self.assertFalse(notify.post("test", level="warn"))

    def test_thread_uses_bot_even_when_webhook_exists(self) -> None:
        with mock.patch.object(notify, "_webhook_url", return_value="https://hooks.example"), \
                mock.patch.object(notify, "openclaw_slack_ready", return_value=True), \
                mock.patch.object(notify, "_post_slack_api", return_value=True) as api, \
                mock.patch.object(notify, "_post_webhook", return_value=True) as webhook:
            self.assertTrue(
                notify.post(
                    "thread reply",
                    thread_ts="123.456",
                    channel_id="C123",
                )
            )
        api.assert_called_once_with(
            mock.ANY,
            thread_ts="123.456",
            channel_id="C123",
        )
        webhook.assert_not_called()

    def test_thread_failure_falls_back_to_channel_bot(self) -> None:
        with mock.patch.object(notify, "_webhook_url", return_value="https://hooks.example"), \
                mock.patch.object(notify, "openclaw_slack_ready", return_value=True), \
                mock.patch.object(notify, "_post_slack_api", side_effect=[False, True]) as api, \
                mock.patch.object(notify, "_post_webhook", return_value=True) as webhook:
            self.assertTrue(
                notify.post("thread reply", thread_ts="123.456", channel_id="C123")
            )
        self.assertEqual(api.call_count, 2)
        self.assertEqual(api.call_args_list[0].kwargs["thread_ts"], "123.456")
        self.assertIsNone(api.call_args_list[1].kwargs["thread_ts"])
        self.assertIn("スレッド投稿に失敗", api.call_args_list[1].args[0])
        webhook.assert_not_called()

    def test_thread_without_bot_falls_back_to_webhook_with_notice(self) -> None:
        with mock.patch.object(notify, "_webhook_url", return_value="https://hooks.example"), \
                mock.patch.object(notify, "openclaw_slack_ready", return_value=False), \
                mock.patch.object(notify, "_post_webhook", return_value=True) as webhook:
            self.assertTrue(notify.post("thread reply", thread_ts="123.456"))
        webhook.assert_called_once()
        self.assertIn("スレッド投稿に失敗", webhook.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
