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


# v30 §WS-D — per-target Slack lifecycle notifications.


class TestFormatTargetEvent(unittest.TestCase):
    def test_sent_line_layout(self) -> None:
        text = notify.format_target_event(
            stage="send",
            status="sent",
            name="株式会社LegalOn Technologies",
            idx=1, total=7,
            detail={"button": "送信", "verify_reason": "explicit_sent_status"},
        )
        # First line is the headline with index, emoji, name and status label.
        head, body = text.split("\n", 1)
        self.assertEqual(head, "[1/7] ✅ 株式会社LegalOn Technologies · send 送信完了")
        self.assertIn("button=送信", body)
        self.assertIn("verify_reason=explicit_sent_status", body)

    def test_skipped_uses_skip_label(self) -> None:
        text = notify.format_target_event(
            stage="send", status="skipped",
            name="株式会社Foo", idx=3, total=5,
            detail={"reason": "captcha_human_required"},
        )
        self.assertIn("⏭", text)
        self.assertIn("スキップ", text)
        self.assertIn("captcha_human_required", text)

    def test_queued_status_label(self) -> None:
        text = notify.format_target_event(
            stage="send", status="queued",
            name="株式会社Bar", idx=2, total=4,
            detail={"reason_class": "validation_unrecoverable"},
        )
        self.assertIn("⚠️", text)
        self.assertIn("要確認キュー", text)

    def test_unknown_status_passes_through(self) -> None:
        # Future status codes should still produce a sensible line rather than
        # crashing the notification path.
        text = notify.format_target_event(
            stage="enrich", status="custom_xyz",
            name="新規ステータス", idx=None, total=None,
        )
        self.assertIn("新規ステータス", text)
        self.assertIn("custom_xyz", text)

    def test_no_idx_omits_index(self) -> None:
        text = notify.format_target_event(
            stage="send", status="sent", name="株式会社X",
        )
        self.assertNotIn("[", text.split("\n", 1)[0])

    def test_long_detail_truncated(self) -> None:
        long = "x" * 500
        text = notify.format_target_event(
            stage="send", status="queued", name="株式会社Y",
            detail={"reason": long},
        )
        # The detail string is bounded so the Slack line never blows up.
        self.assertLess(len(text), 400)

    def test_wizard_stuck_payload_renders(self) -> None:
        # The wizard.compute_stuck_reason payload is a dict — make sure the
        # formatter does not just print the dict repr.
        text = notify.format_target_event(
            stage="send", status="queued",
            name="株式会社富士ソフト", idx=6, total=7,
            detail={
                "wizard_stuck": {
                    "reason": "same_button_repeated",
                    "detail": "button '次へ' clicked 3x without progress",
                },
            },
        )
        self.assertIn("wizard_stuck=", text)
        # The mapping's "reason" key wins over a stringified dict.
        self.assertIn("same_button_repeated", text)
        self.assertNotIn("{'reason'", text)


class TestPostTargetEvent(unittest.TestCase):
    def test_disabled_via_env_short_circuits(self) -> None:
        with mock.patch.dict("os.environ", {"DOORMAN_TARGET_EVENT_NOTIFY": "0"}):
            with mock.patch.object(notify, "_post_slack_api", return_value=True) as api:
                ok = notify.post_target_event(
                    stage="send", status="sent",
                    target={"name": "X", "id": "x"},
                )
        self.assertFalse(ok)
        api.assert_not_called()

    def test_calls_post_underneath_with_warn_for_queued(self) -> None:
        # Queued/blocked statuses map to level=warn so the ⚠ prefix lands.
        with mock.patch.object(notify, "post", return_value=True) as posted:
            notify.post_target_event(
                stage="send", status="queued",
                target={"name": "株式会社X", "id": "x"},
                idx=1, total=2,
                detail={"reason_class": "validation_unrecoverable"},
            )
        posted.assert_called_once()
        kwargs = posted.call_args.kwargs
        self.assertEqual(kwargs["level"], "warn")
        body = posted.call_args.args[0]
        self.assertIn("[1/2]", body)
        self.assertIn("⚠️", body)

    def test_sent_uses_info_level(self) -> None:
        with mock.patch.object(notify, "post", return_value=True) as posted:
            notify.post_target_event(
                stage="send", status="sent",
                target={"name": "株式会社Y", "id": "y"},
                idx=2, total=5,
            )
        self.assertEqual(posted.call_args.kwargs["level"], "info")

    def test_target_id_fallback_when_name_missing(self) -> None:
        with mock.patch.object(notify, "post", return_value=True) as posted:
            notify.post_target_event(
                stage="send", status="sent",
                target={"id": "fallback_id"},
            )
        body = posted.call_args.args[0]
        self.assertIn("fallback_id", body)

    def test_thread_ts_and_channel_passed_through(self) -> None:
        with mock.patch.object(notify, "post", return_value=True) as posted:
            notify.post_target_event(
                stage="send", status="sent",
                target={"name": "Z"}, thread_ts="123.456", channel_id="C99",
            )
        kwargs = posted.call_args.kwargs
        self.assertEqual(kwargs["thread_ts"], "123.456")
        self.assertEqual(kwargs["channel_id"], "C99")


class TestNotifyAuditJsonl(unittest.TestCase):
    """v30 §WS-D — when ``DOORMAN_NOTIFY_AUDIT_PATH`` is set, per-target Slack
    deliveries also append a row to that file. The existing run-level
    start/terminal rows are unaffected."""

    def test_audit_appended_when_path_set(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "x.notify.jsonl"
            env = {
                "DOORMAN_NOTIFY_AUDIT_PATH": str(audit_path),
                "DOORMAN_RUN_ID": "20260630-000000",
                "DOORMAN_SKILL": "jp-form-outreach",
            }
            with mock.patch.dict("os.environ", env), \
                    mock.patch.object(notify, "post", return_value=True):
                notify.post_target_event(
                    stage="send", status="sent",
                    target={"id": "legalon", "name": "株式会社LegalOn Technologies"},
                    idx=1, total=7,
                    thread_ts="123.456", channel_id="C77",
                )
            text = audit_path.read_text()
            row = json.loads(text.strip())
            self.assertEqual(row["phase"], "target.sent")
            self.assertEqual(row["target_id"], "legalon")
            self.assertEqual(row["stage"], "send")
            self.assertEqual(row["status"], "sent")
            self.assertEqual(row["name"], "株式会社LegalOn Technologies")
            self.assertEqual(row["run_id"], "20260630-000000")
            self.assertEqual(row["skill"], "jp-form-outreach")
            self.assertTrue(row["ok"])
            self.assertTrue(row["thread"])
            self.assertEqual(row["channel_id"], "C77")

    def test_no_audit_when_path_unset(self) -> None:
        # The post still goes through; the audit silently skips.
        with mock.patch.dict("os.environ", {"DOORMAN_NOTIFY_AUDIT_PATH": ""}, clear=False), \
                mock.patch.object(notify, "post", return_value=True):
            ok = notify.post_target_event(
                stage="send", status="sent", target={"id": "x", "name": "X"},
            )
        # The function should not raise, returns the post() result.
        self.assertTrue(ok)

    def test_audit_records_failure_ok_false(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "x.notify.jsonl"
            with mock.patch.dict("os.environ", {"DOORMAN_NOTIFY_AUDIT_PATH": str(audit_path)}), \
                    mock.patch.object(notify, "post", return_value=False):
                notify.post_target_event(
                    stage="send", status="queued",
                    target={"id": "y", "name": "Y"},
                )
            row = json.loads(audit_path.read_text().strip())
            self.assertFalse(row["ok"])
            self.assertEqual(row["phase"], "target.queued")

    def test_audit_appends_multiple_targets(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "x.notify.jsonl"
            with mock.patch.dict("os.environ", {"DOORMAN_NOTIFY_AUDIT_PATH": str(audit_path)}), \
                    mock.patch.object(notify, "post", return_value=True):
                for tid in ("a", "b", "c"):
                    notify.post_target_event(
                        stage="send", status="sent",
                        target={"id": tid, "name": tid.upper()},
                    )
            rows = [json.loads(l) for l in audit_path.read_text().splitlines() if l.strip()]
            self.assertEqual([r["target_id"] for r in rows], ["a", "b", "c"])

    def test_audit_silent_when_target_event_disabled(self) -> None:
        # If the env disables per-target notifications, NO audit row is
        # written either — the function returns early.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "x.notify.jsonl"
            env = {
                "DOORMAN_NOTIFY_AUDIT_PATH": str(audit_path),
                "DOORMAN_TARGET_EVENT_NOTIFY": "0",
            }
            with mock.patch.dict("os.environ", env):
                notify.post_target_event(
                    stage="send", status="sent",
                    target={"id": "z", "name": "Z"},
                )
            self.assertFalse(audit_path.exists())


if __name__ == "__main__":
    unittest.main()
