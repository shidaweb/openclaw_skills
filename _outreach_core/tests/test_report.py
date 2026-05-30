"""Tests for the autonomous-mode report summary (v5 §12 observability)."""

import unittest

from _outreach_core.helpers.report import _skip_reason_bucket, autonomous_summary


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


if __name__ == "__main__":
    unittest.main()
