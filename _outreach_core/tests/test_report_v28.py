"""v28 P1 outcome-based send funnel report tests."""

from __future__ import annotations

import unittest

from _outreach_core import outcomes as oc
from _outreach_core.helpers.report import diff_against_snapshot, summarize_outcomes


def _target_event(target_id: str, company: str, outcome: str, root: str = "") -> dict:
    return {
        "kind": "send.target_outcome",
        "target_id": target_id,
        "payload": {
            "company": company,
            "outcome": outcome,
            "bucket": oc.outcome_bucket(outcome),
            "root_cause": root,
        },
    }


class TestOutcomeSummary(unittest.TestCase):
    def test_summarize_outcomes_groups_bucket_outcome_and_examples(self) -> None:
        events = [
            _target_event("a", "株式会社A", oc.SENT),
            _target_event("b", "株式会社B", oc.SUBMIT_INEFFECTIVE, "最終送信ボタン押下で失敗"),
            _target_event("c", "株式会社C", oc.SUBMIT_INEFFECTIVE, "最終送信ボタン押下で失敗"),
            _target_event("d", "株式会社D", oc.NO_FORM, "ページ状態で失敗"),
        ]
        summary = summarize_outcomes(events)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["buckets"]["confirm"]["count"], 1)
        self.assertEqual(summary["buckets"]["submit"]["count"], 2)
        self.assertEqual(
            summary["buckets"]["submit"]["outcomes"][oc.SUBMIT_INEFFECTIVE]["companies"],
            ["株式会社B", "株式会社C"],
        )
        self.assertEqual(summary["root_causes"]["最終送信ボタン押下で失敗"], 2)

    def test_unknown_raw_outcome_is_normalized_for_report(self) -> None:
        summary = summarize_outcomes([
            {
                "kind": "send.target_outcome",
                "target_id": "x",
                "payload": {"company": "X", "outcome": "new_future_code"},
            }
        ])
        self.assertEqual(summary["outcomes"][oc.UNKNOWN]["count"], 1)

    def test_diff_against_snapshot(self) -> None:
        curr = {
            "total": 3,
            "outcomes": {
                oc.SENT: {"count": 2},
                oc.NO_FORM: {"count": 1},
            },
        }
        prev = {
            "total": 2,
            "outcomes": {
                oc.SENT: {"count": 1},
                oc.SUBMIT_INEFFECTIVE: {"count": 1},
            },
        }
        diff = diff_against_snapshot(curr, prev)
        self.assertFalse(diff["baseline"])
        self.assertEqual(diff["total_delta"], 1)
        self.assertEqual(diff["outcome_deltas"][oc.SENT], 1)
        self.assertEqual(diff["outcome_deltas"][oc.NO_FORM], 1)
        self.assertEqual(diff["outcome_deltas"][oc.SUBMIT_INEFFECTIVE], -1)

    def test_diff_without_previous_is_baseline(self) -> None:
        diff = diff_against_snapshot({"total": 1, "outcomes": {}}, None)
        self.assertTrue(diff["baseline"])
        self.assertIsNone(diff["total_delta"])


if __name__ == "__main__":
    unittest.main()
