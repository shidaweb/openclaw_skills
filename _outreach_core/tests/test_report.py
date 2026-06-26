"""Tests for report summaries (autonomy + period send summary)."""

from datetime import datetime, timezone
import unittest

from _outreach_core.helpers.report import (
    inquiry_type_summary,
    _skip_reason_bucket,
    autonomous_summary,
    period_bounds,
    research_quality_summary,
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
        self.assertEqual(summary["sent_companies"][0]["source"], "sent_history")
        self.assertIn("first submit button not found", summary["failure_reasons"])

    def test_send_summary_all_period_includes_old_rows(self):
        sent_rows = [
            {"id": "x1", "name": "古い会社", "subject": "旧提案", "sent_at": "2025-01-01T00:00:00Z"},
        ]
        summary = send_period_summary(sent_rows, [], [], period="all")
        self.assertEqual(summary["successes"], 1)
        self.assertEqual(summary["sent_companies"][0]["company"], "古い会社")

    def test_verify_ok_event_counts_as_success_without_sent_history(self):
        events = [
            {
                "kind": "send.verify.completed",
                "ts": "2026-06-10T01:02:05Z",
                "target_id": "verified_only",
                "payload": {
                    "status": "ok",
                    "reason": "Generic 株式会社: 送信完了を確認",
                    "name": "Generic 株式会社",
                    "subject": "ご提案Generic",
                    "send_verdict": "sent_ok",
                    "send_score": 5,
                    "send_reason": "explicit_sent_status",
                },
            }
        ]
        now = datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc)
        summary = send_period_summary([], [], events, period="this_month", now=now)
        self.assertEqual(summary["attempts"], 1)
        self.assertEqual(summary["successes"], 1)
        self.assertEqual(summary["failures"], 0)
        self.assertEqual(summary["success_sources"]["verify_event"], 1)
        self.assertEqual(summary["sent_companies"][0]["company"], "Generic 株式会社")
        self.assertEqual(summary["sent_companies"][0]["source"], "verify_event")
        self.assertEqual(summary["sent_companies"][0]["send_reason"], "explicit_sent_status")

    def test_sent_history_wins_over_stale_skip_and_failure_event(self):
        sent_rows = [
            {
                "id": "bookoff_group",
                "name": "ブックオフグループホールディングス株式会社",
                "subject": "ご提案",
                "sent_at": "2026-06-16T20:30:03Z",
            }
        ]
        skip_rows = [
            {
                "id": "bookoff_group",
                "name": "ブックオフグループホールディングス株式会社",
                "reason": "RESOLVER_FAILED: wizard_too_deep",
                "skipped_at": "2026-06-16T12:03:04Z",
            }
        ]
        events = [
            {
                "kind": "send.queued_for_resolver",
                "ts": "2026-06-16T11:31:56Z",
                "target_id": "bookoff_group",
                "payload": {"reason": "multi-step form exceeded 4 steps"},
            }
        ]
        now = datetime(2026, 6, 17, 0, 0, tzinfo=timezone.utc)
        summary = send_period_summary(sent_rows, skip_rows, events, period="this_month", now=now)
        self.assertEqual(summary["attempts"], 1)
        self.assertEqual(summary["successes"], 1)
        self.assertEqual(summary["failures"], 0)
        self.assertEqual(summary["failed_companies"], [])


class TestResearchQualitySummary(unittest.TestCase):
    def test_research_quality_kpis(self):
        events = [
            {"kind": "enrich.form.completed", "ts": "2026-06-01T00:00:00Z"},
            {"kind": "enrich.form.completed", "ts": "2026-06-01T00:01:00Z"},
            {
                "kind": "enrich.form.skipped_non_contact",
                "ts": "2026-06-01T00:02:00Z",
                "payload": {"kind": "recruit", "correction_attempts": 1},
            },
            {
                "kind": "enrich.form.url_corrected",
                "ts": "2026-06-01T00:03:00Z",
                "payload": {"attempt_no": 1},
            },
        ]
        sent_rows = [
            {"id": "a", "sent_at": "2026-06-01T00:05:00Z"},
        ]
        summary = research_quality_summary(events, sent_rows, since=datetime(2026, 5, 31, tzinfo=timezone.utc))
        self.assertEqual(summary["enrich_attempts"], 3)
        self.assertEqual(summary["contact_classified"], 2)
        self.assertEqual(summary["non_contact"], 1)
        self.assertEqual(summary["sent_successes"], 1)
        self.assertAlmostEqual(summary["wrong_url_rate"], round(1 / 3, 4))
        self.assertEqual(summary["non_contact_by_kind"]["recruit"], 1)


class TestInquiryTypeSummary(unittest.TestCase):
    def test_counts_confidence_source_and_no_b2b(self):
        events = [
            {
                "kind": "send.inquiry_type",
                "payload": {"confidence": "high", "src": "llm"},
            },
            {
                "kind": "send.inquiry_type",
                "payload": {"confidence": "low", "src": "fallback"},
            },
            {
                "kind": "enrich.inquiry_type_selected",
                "payload": {
                    "confidence_counts": {"high": 2, "low": 1},
                    "src_counts": {"llm": 1, "fallback": 2},
                },
            },
            {
                "kind": "enrich.form.screen_skipped",
                "payload": {"reason": "no_b2b_inquiry_type"},
            },
        ]
        s = inquiry_type_summary(events)
        self.assertEqual(s["selected_send"], 2)
        self.assertEqual(s["selected_enrich"], 1)
        self.assertEqual(s["no_b2b_inquiry_type"], 1)
        self.assertEqual(s["confidence_counts"]["high"], 3)
        self.assertEqual(s["confidence_counts"]["low"], 2)
        self.assertEqual(s["source_counts"]["llm"], 2)
        self.assertEqual(s["source_counts"]["fallback"], 3)


if __name__ == "__main__":
    unittest.main()
