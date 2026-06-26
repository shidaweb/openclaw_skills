"""v28 P1 canonical send outcome taxonomy tests."""

from __future__ import annotations

import unittest

from _outreach_core import outcomes as oc


class TestClassifyOutcome(unittest.TestCase):
    def test_strong_verify_wins(self) -> None:
        self.assertEqual(
            oc.classify_outcome(
                result_state="done",
                verify_verdict="sent_ok",
                timeline=[{"stage": "verify", "ok": False, "detail": {}}],
            ),
            oc.SENT,
        )

    def test_timeline_first_failure_wins_over_raw_skipped(self) -> None:
        timeline = [
            {"stage": "open", "ok": True, "detail": {}},
            {"stage": "first_submit", "ok": False, "detail": {"flow": "confirm"}},
            {"stage": "verify", "ok": False, "detail": {}},
        ]
        self.assertEqual(
            oc.classify_outcome(
                result_state="skipped",
                verify_verdict=None,
                timeline=timeline,
            ),
            oc.SUBMIT_INEFFECTIVE,
        )

    def test_raw_states_map_to_canonical(self) -> None:
        cases = [
            ("validation_stuck", oc.VALIDATION_STUCK),
            ("submit_click_ineffective", oc.SUBMIT_INEFFECTIVE),
            ("page_has_no_form", oc.NO_FORM),
            ("cloudflare_blocking", oc.CAPTCHA_BLOCKED),
            ("wizard_too_deep", oc.MULTISTEP_TOO_DEEP),
            ("timed_out", oc.NETWORK_ERROR),
            ("crashed", oc.CRASHED),
            ("totally new raw state", oc.UNKNOWN),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(
                    oc.classify_outcome(
                        result_state=raw,
                        verify_verdict=None,
                        timeline=[],
                    ),
                    expected,
                )

    def test_build_payload_has_bucket_and_metadata(self) -> None:
        payload = oc.build_target_outcome_payload(
            target={
                "id": "a",
                "name": "株式会社A",
                "form_url": "https://example.co.jp/contact",
            },
            result={"outcome": "timed_out", "reason": "browser timeout"},
            started_at=10.0,
            finished_at=75.4,
            timeline=[],
        )
        self.assertEqual(payload["outcome"], oc.NETWORK_ERROR)
        self.assertEqual(payload["bucket"], "system")
        self.assertEqual(payload["company"], "株式会社A")
        self.assertEqual(payload["elapsed_sec"], 65)

    def test_all_outcomes_have_bucket(self) -> None:
        self.assertEqual(set(oc.OUTCOMES), set(oc.BUCKET))
        for code in oc.OUTCOMES:
            self.assertIn(oc.outcome_bucket(code), {"list", "fill", "submit", "confirm", "system"})
            self.assertTrue(oc.outcome_label_ja(code))


if __name__ == "__main__":
    unittest.main()
