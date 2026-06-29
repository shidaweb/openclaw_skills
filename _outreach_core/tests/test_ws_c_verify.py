"""v30 §WS-C — verify scoring explainability and double-check.

Production logs (2026-06-29 doorman-ai) showed per-target score lines like
``send_score=-8`` followed seconds later by ``send_score=11`` for the same
target. The flip is benign (pre/post visibility data), but the trace had no
way to explain WHICH evidence moved the score, so the operator couldn't
distinguish a real anomaly from a normal observation transition. WS-C adds:

  * Per-signal :data:`score_breakdown` so each contributor is listed.
  * Per-pass :data:`score_history` so pre/post/recheck phases are named.
  * NFKC + punctuation normalization for the LLM-tiebreak hallucination guard.
  * Opt-in ``recheck_after_sec`` double-check that downgrades unstable
    sent_ok verdicts to uncertain.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import send_state as core_ss
from _outreach_core import verify as v


class TestScoreBreakdown(unittest.TestCase):
    def test_breakdown_records_each_contribution(self) -> None:
        evidence = {
            "url": "https://example.co.jp/contact/thanks",
            "url_success": True,
            "has_success_keyword": True,
            "form_gone_visible": True,
            "text": "お問い合わせありがとうございました。送信完了しました。",
        }
        result = core_ss.assess_submission_result(evidence)
        breakdown = result.get("score_breakdown")
        self.assertIsNotNone(breakdown)
        # The breakdown should include the URL hit, keyword hit, and form-gone
        # contribution. Each entry is a {signal, points} dict.
        by_signal = {row["signal"]: row["points"] for row in breakdown}
        self.assertIn("success_url", by_signal)
        self.assertEqual(by_signal["success_url"], 2)
        self.assertIn("success_keyword", by_signal)
        self.assertIn("form_gone", by_signal)
        # The sum of positive contributions matches the final score (no
        # signal silently slipped through).
        self.assertEqual(sum(by_signal.values()), result["score"])

    def test_breakdown_records_negative_contributions(self) -> None:
        evidence = {
            "has_error_keyword": True,
            "form_still_present": True,
            "visible_textareas": 1,
            "text": "入力エラーがあります。必須項目を確認してください。",
        }
        result = core_ss.assess_submission_result(evidence)
        breakdown = result.get("score_breakdown") or []
        by_signal = {row["signal"]: row["points"] for row in breakdown}
        self.assertIn("error_keyword", by_signal)
        self.assertEqual(by_signal["error_keyword"], -3)
        self.assertIn("form_still_present", by_signal)
        self.assertEqual(by_signal["form_still_present"], -2)
        self.assertLess(result["score"], 0)


class TestScoreHistory(unittest.TestCase):
    def test_pre_and_post_visibility_passes_are_recorded(self) -> None:
        # Tag the first call as pre_visibility, the second as post_visibility.
        # Production logs flipped between -8 and +11 without explanation; the
        # history field makes that flip auditable.
        evidence = {
            "has_error_keyword": True,
            "text": "入力エラーがあります",
        }
        v._record_submission_result(evidence, pass_label="pre_visibility")
        # Now add visibility data simulating the form having vanished after
        # a successful submission.
        evidence.update({
            "visible_forms": 0,
            "visible_textareas": 0,
            "form_gone_visible": True,
            "form_still_present": False,
            "has_success_keyword": True,
            "has_error_keyword": False,
            "text": "送信完了しました。ありがとうございました。",
        })
        v._record_submission_result(evidence, pass_label="post_visibility")
        hist = evidence.get("score_history")
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]["pass"], "pre_visibility")
        self.assertEqual(hist[1]["pass"], "post_visibility")
        # The scores moved between passes — that's exactly what the history
        # is for. Both entries carry their own breakdown so the operator can
        # explain the flip.
        self.assertIn("score_breakdown", hist[0])
        self.assertIn("score_breakdown", hist[1])


class TestLLMTiebreakNormalization(unittest.TestCase):
    """NFKC + punctuation collapse for the hallucination guard."""

    def test_fullwidth_halfwidth_paren_match(self) -> None:
        # Page has ASCII parens, LLM quotes 全角 — should still match.
        page = "お問い合わせを受け付けました(自動返信)"
        raw = (
            '{"verdict": "sent", '
            '"quote": "お問い合わせを受け付けました（自動返信）"}'
        )
        parsed = v.parse_llm_tiebreak(raw, page)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["verdict"], "sent")

    def test_fullwidth_space_collapsed(self) -> None:
        # Page has a 全角 space (U+3000) inside the success sentence; the LLM
        # quote uses ASCII space.
        page = "お問い合わせ\u3000ありがとうございました"
        raw = (
            '{"verdict": "sent", '
            '"quote": "お問い合わせ ありがとうございました"}'
        )
        parsed = v.parse_llm_tiebreak(raw, page)
        self.assertIsNotNone(parsed)

    def test_trailing_punctuation_dropped(self) -> None:
        page = "お問い合わせ完了"
        raw = '{"verdict": "sent", "quote": "お問い合わせ完了。"}'
        parsed = v.parse_llm_tiebreak(raw, page)
        self.assertIsNotNone(parsed)

    def test_hallucinated_quote_still_rejected(self) -> None:
        # A quote that does not exist on the page — even with normalization
        # — must still be rejected.
        page = "送信中..."
        raw = '{"verdict": "sent", "quote": "完了画面を表示しました"}'
        parsed = v.parse_llm_tiebreak(raw, page)
        self.assertIsNone(parsed)

    def test_unclear_does_not_require_quote_match(self) -> None:
        # ``unclear`` verdicts have no hallucination risk for the verdict
        # itself; the quote field is informational.
        page = "なにかが起きました"
        raw = '{"verdict": "unclear", "quote": "完全に違う文字列"}'
        parsed = v.parse_llm_tiebreak(raw, page)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["verdict"], "unclear")


class TestDoubleCheck(unittest.TestCase):
    """``recheck_after_sec`` downgrades unstable sent_ok verdicts."""

    def _target(self) -> dict:
        return {"id": "t1", "name": "テスト株式会社"}

    def _build_sent_browser_verify(self) -> dict:
        return {
            "url": "https://example.co.jp/contact/thanks",
            "text": "送信完了しました。ありがとうございました。",
            "visible_forms": 0,
            "visible_textareas": 0,
            "form_gone_visible": True,
            "form_still_present": False,
            "submission_sent": True,
            "submission_statuses": ["success"],
            "has_success_keyword": True,
            "has_error_keyword": False,
        }

    def _build_failed_browser_verify(self) -> dict:
        return {
            "url": "https://example.co.jp/contact/",
            "text": "入力エラーがあります",
            "visible_forms": 1,
            "visible_textareas": 1,
            "form_gone_visible": False,
            "form_still_present": True,
            "has_success_keyword": False,
            "has_error_keyword": True,
        }

    def test_no_recheck_means_no_change(self) -> None:
        # Default behaviour (recheck_after_sec=0) is unchanged: a single
        # sent_ok verdict returns ok without re-fetching.
        result = v.verify_send_completed(
            self._target(),
            channel="jp_form",
            browser_verify=self._build_sent_browser_verify(),
            snapshot="送信完了しました",
            verify_strict=False,
        )
        self.assertEqual(result["status"], "ok")

    def test_recheck_confirms_sent_ok(self) -> None:
        # When the recheck observation ALSO reports sent_ok, the verdict
        # stays ok and a score_history row with pass="recheck" is recorded.
        slept: list[float] = []

        def fake_evaluate(_js):
            # Re-observation returns the SAME success page.
            return {
                **self._build_sent_browser_verify(),
            }

        result = v.verify_send_completed(
            self._target(),
            channel="jp_form",
            browser_verify=self._build_sent_browser_verify(),
            snapshot="送信完了しました",
            evaluate_fn=fake_evaluate,
            verify_strict=False,
            recheck_after_sec=2.0,
            sleep_fn=slept.append,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(slept, [2.0])
        history = result["evidence"].get("score_history") or []
        passes = [h["pass"] for h in history]
        self.assertIn("recheck", passes)

    def test_recheck_downgrades_to_uncertain_on_disagreement(self) -> None:
        # First pass says sent_ok, recheck reports failed — we downgrade to
        # uncertain rather than reporting a flaky success.
        slept: list[float] = []

        def fake_evaluate(_js):
            return self._build_failed_browser_verify()

        result = v.verify_send_completed(
            self._target(),
            channel="jp_form",
            browser_verify=self._build_sent_browser_verify(),
            snapshot="送信完了しました",
            evaluate_fn=fake_evaluate,
            verify_strict=False,
            recheck_after_sec=1.5,
            sleep_fn=slept.append,
        )
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(slept, [1.5])
        self.assertIn("揺れあり", result["reason"])

    def test_recheck_without_evaluate_fn_is_silent(self) -> None:
        # No evaluate_fn → can't recheck → first verdict stands.
        result = v.verify_send_completed(
            self._target(),
            channel="jp_form",
            browser_verify=self._build_sent_browser_verify(),
            snapshot="送信完了しました",
            verify_strict=False,
            recheck_after_sec=2.0,
        )
        self.assertEqual(result["status"], "ok")

    def test_recheck_swallows_evaluate_exceptions(self) -> None:
        # If the recheck fetch raises, we MUST NOT fail the verdict — we just
        # keep the first verdict and continue.
        def boom(_js):
            raise RuntimeError("network blip")

        result = v.verify_send_completed(
            self._target(),
            channel="jp_form",
            browser_verify=self._build_sent_browser_verify(),
            snapshot="送信完了しました",
            evaluate_fn=boom,
            verify_strict=False,
            recheck_after_sec=1.0,
            sleep_fn=lambda s: None,
        )
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
