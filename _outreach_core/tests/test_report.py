"""Tests for report summaries (autonomy + period send summary)."""

from datetime import datetime, timezone
import unittest

from _outreach_core.helpers.report import (
    _skip_reason_bucket,
    autonomous_summary,
    period_bounds,
    send_period_summary,
)


def _scored(score, send, errored=False):
    return {
        "kind": "send.self_scored",
        "payload": {"score": score, "send": send, "errored": errored},
    }


def _skipped(reason):
    return {"kind": "send.auto_skipped", "payload": {"reason": reason}}


class TestAutonomousSummary(unittest.TestCase):
    def test_no_autonomous_events_is_inactive(self):
        events = [
            {"kind": "send.verify.completed", "payload": {"status": "ok"}},
            {"kind": "draft.emitted", "payload": {}},
        ]
        self.assertEqual(autonomous_summary(events), {"active": False})

    def test_self_score_counts_and_average(self):
        events = [
            _scored(0.9, True),
            _scored(0.8, True),
            _scored(0.5, False),
        ]
        summary = autonomous_summary(events)
        self.assertTrue(summary["active"])
        self.assertEqual(summary["self_scored"], 3)
        self.assertEqual(summary["self_scored_sent"], 2)
        self.assertEqual(summary["self_scored_gated"], 1)
        self.assertEqual(summary["self_scored_errored"], 0)
        self.assertAlmostEqual(summary["avg_score"], round((0.9 + 0.8 + 0.5) / 3, 3))

    def test_errored_score_counted_and_excluded_from_average(self):
        events = [_scored(0.8, True), _scored(None, True, errored=True)]
        summary = autonomous_summary(events)
        self.assertEqual(summary["self_scored_errored"], 1)
        # the None score must not poison the average
        self.assertEqual(summary["avg_score"], 0.8)

    def test_no_scores_yields_none_average(self):
        events = [_skipped("wrong_form")]
        summary = autonomous_summary(events)
        self.assertTrue(summary["active"])
        self.assertIsNone(summary["avg_score"])

    def test_skip_reason_buckets(self):
        events = [
            _skipped("self_score_below_threshold: 関連性が低い"),
            _skipped("reCAPTCHA v2 visible (warmup insufficient)"),
            _skipped("WRONG_FORM_TYPE_DETECTED — recruit form"),
            _skipped("first submit button not found (flow=confirm)"),
            _skipped("confirm-page final submit not found"),
        ]
        summary = autonomous_summary(events)
        self.assertEqual(summary["auto_skipped"], 5)
        self.assertEqual(summary["skip_reasons"]["self_score_below_threshold"], 1)
        self.assertEqual(summary["skip_reasons"]["captcha_v2_visible"], 1)
        self.assertEqual(summary["skip_reasons"]["wrong_form_type"], 1)
        self.assertEqual(summary["skip_reasons"]["first_submit_not_found"], 1)
        self.assertEqual(summary["skip_reasons"]["confirm_submit_not_found"], 1)

    def test_awaiting_upfront_approval_marks_active(self):
        events = [{"kind": "campaign.awaiting_upfront_approval", "payload": {"sendable": 12}}]
        summary = autonomous_summary(events)
        self.assertTrue(summary["active"])
        self.assertEqual(summary["awaiting_upfront_approval"], 1)
        self.assertEqual(summary["self_scored"], 0)

    def test_bucket_helper_fallback(self):
        self.assertEqual(_skip_reason_bucket("totally novel reason: details"), "totally novel reason")
        self.assertEqual(_skip_reason_bucket(""), "other")


class TestSendPeriodSummary(unittest.TestCase):
    def test_period_bounds_this_month_and_last_month(self):
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        this_start, this_end = period_bounds("this_month", now=now)
        last_start, last_end = period_bounds("last_month", now=now)
        self.assertEqual(this_start.isoformat(), "2026-06-01T00:00:00+00:00")
        self.assertEqual(this_end.isoformat(), now.isoformat())
        self.assertEqual(last_start.isoformat(), "2026-05-01T00:00:00+00:00")
        self.assertEqual(last_end.isoformat(), "2026-06-01T00:00:00+00:00")

    def test_send_summary_counts_companies_and_failure_reasons(self):
        sent_rows = [
            {
                "id": "a1",
                "name": "株式会社A",
                "subject": "ご提案A",
                "sent_at": "2026-06-10T01:02:03Z",
            }
        ]
        skip_rows = [
            {
                "id": "b1",
                "name": "株式会社B",
                "reason": "first submit button not found",
                "skipped_at": "2026-06-10T02:00:00Z",
            }
        ]
        events = [
            {
                "kind": "send.verify.completed",
                "ts": "2026-06-10T01:02:05Z",
                "target_id": "a1",
                "payload": {"status": "ok", "reason": "sent"},
            },
            {
                "kind": "send.first_button_missing",
                "ts": "2026-06-10T02:00:01Z",
                "target_id": "b1",
                "payload": {"patterns": ["送信"]},
            },
        ]
        now = datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc)
        summary = send_period_summary(sent_rows, skip_rows, events, period="this_month", now=now)
        self.assertEqual(summary["successes"], 1)
        self.assertEqual(summary["attempts"], 2)
        self.assertEqual(summary["failures"], 1)
        self.assertEqual(summary["sent_companies"][0]["company"], "株式会社A")
        self.assertEqual(summary["sent_companies"][0]["content"], "ご提案A")
        self.assertIn("first submit button not found", summary["failure_reasons"])

    def test_send_summary_all_period_includes_old_rows(self):
        sent_rows = [
            {"id": "x1", "name": "古い会社", "subject": "旧提案", "sent_at": "2025-01-01T00:00:00Z"},
        ]
        summary = send_period_summary(sent_rows, [], [], period="all")
        self.assertEqual(summary["successes"], 1)
        self.assertEqual(summary["sent_companies"][0]["company"], "古い会社")


if __name__ == "__main__":
    unittest.main()
