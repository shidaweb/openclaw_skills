"""v15 §V — weighted evidence scoring, LLM tiebreak guard, visibility signals."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import verify as vf


class TestScoreSendEvidence(unittest.TestCase):
    """§V acceptance 8 — table test for the pure scoring function."""

    def test_table(self) -> None:
        cases = [
            # (evidence, score, verdict)
            ({"url_success": True, "has_success_keyword": True}, 4, "sent_ok"),
            ({"url_success": True, "form_gone_visible": True}, 4, "sent_ok"),
            ({"has_success_keyword": True, "form_gone_visible": True}, 4, "sent_ok"),
            ({"has_success_keyword": True}, 2, "uncertain"),
            (
                {
                    "has_success_keyword": True,
                    "text": "お問い合わせを受け付けております。内容の入力に進んでください。",
                    "visible_forms": 0,
                    "visible_textareas": 0,
                    "final_submit_controls": 0,
                },
                0,
                "uncertain",
            ),
            ({"form_gone_visible": True}, 2, "uncertain"),
            ({}, 0, "uncertain"),
            ({"form_still_present": True}, -2, "failed"),
            ({"has_error_keyword": True}, -3, "failed"),
            ({"cf7_sent": True}, 5, "sent_ok"),
            ({"cf7_invalid": True}, -4, "failed"),
            ({"submission_sent": True}, 5, "sent_ok"),
            ({"submission_invalid": True}, -4, "failed"),
            (
                {"cf7_sent": True, "form_still_present": True},
                3,
                "sent_ok",
            ),
            (
                {"submission_sent": True, "form_still_present": True},
                3,
                "sent_ok",
            ),
            ({"has_error_keyword": True, "form_still_present": True}, -5, "failed"),
            ({"has_success_keyword": True, "has_error_keyword": True}, -1, "uncertain"),
            (
                {"url_success": True, "has_success_keyword": True,
                 "form_still_present": True},
                2, "uncertain",
            ),
        ]
        for evidence, score, verdict in cases:
            with self.subTest(evidence=evidence):
                self.assertEqual(vf.score_send_evidence(evidence), score)
                self.assertEqual(vf.verdict_from_score(score), verdict)


class TestLlmTiebreakGuard(unittest.TestCase):
    """§V acceptance 9 — quote hallucination guard."""

    PAGE = "お問い合わせの送信が完了いたしました。担当より連絡します。"

    def test_quote_in_page_accepted(self) -> None:
        raw = json.dumps({"verdict": "sent", "quote": "送信が完了いたしました"}, ensure_ascii=False)
        out = vf.parse_llm_tiebreak(raw, self.PAGE)
        self.assertEqual(out["verdict"], "sent")

    def test_hallucinated_quote_rejected(self) -> None:
        raw = json.dumps({"verdict": "sent", "quote": "送信ありがとうございました（存在しない文）"}, ensure_ascii=False)
        self.assertIsNone(vf.parse_llm_tiebreak(raw, self.PAGE))

    def test_missing_quote_rejected_for_decisive_verdicts(self) -> None:
        self.assertIsNone(vf.parse_llm_tiebreak('{"verdict": "sent", "quote": ""}', self.PAGE))
        self.assertIsNone(vf.parse_llm_tiebreak('{"verdict": "not_sent"}', self.PAGE))

    def test_unclear_needs_no_quote(self) -> None:
        out = vf.parse_llm_tiebreak('{"verdict": "unclear", "quote": ""}', self.PAGE)
        self.assertEqual(out["verdict"], "unclear")

    def test_garbage_rejected(self) -> None:
        self.assertIsNone(vf.parse_llm_tiebreak("no json", self.PAGE))
        self.assertIsNone(vf.parse_llm_tiebreak('{"verdict": "maybe"}', self.PAGE))
        self.assertIsNone(vf.parse_llm_tiebreak(None, self.PAGE))

    def test_whitespace_normalized_quote_match(self) -> None:
        raw = json.dumps({"verdict": "sent", "quote": "送信が 完了いたしました"}, ensure_ascii=False)
        out = vf.parse_llm_tiebreak(raw, self.PAGE)
        self.assertEqual(out["verdict"], "sent")


class TestVerifyIntegrationV15(unittest.TestCase):
    def test_form_gone_plus_thanks_url_is_ok_without_keyword(self) -> None:
        """Score path: url(+2) + form visibly gone(+2) = 4 → ok."""
        target = {"id": "v1", "name": "Visible 株式会社", "form_fields": {"inputs": []}}
        result = vf.verify_send_completed(
            target, "jp_form",
            snapshot="ご入力いただいた内容を受け付けます",  # no success keyword
            browser_verify={"url": "https://example.co.jp/contact/complete", "text": ""},
            evaluate_fn=lambda js: {"visible_forms": 0, "visible_textareas": 0,
                                    "empty_required": []},
        )
        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(result["evidence"]["score"], 3)

    def test_form_still_visible_without_success_is_failed(self) -> None:
        """Score path: form still present(−2) → failed → needs_attention."""
        target = {"id": "v2", "name": "Sticky 株式会社", "form_fields": {"inputs": []}}
        result = vf.verify_send_completed(
            target, "jp_form",
            snapshot="お問い合わせフォーム",
            browser_verify={"url": "https://example.co.jp/contact", "text": ""},
            evaluate_fn=lambda js: {"visible_forms": 1, "visible_textareas": 1,
                                    "empty_required": []},
        )
        self.assertEqual(result["status"], "needs_attention")

    def test_cf7_sent_overrides_sticky_visible_form(self) -> None:
        """CF7 Ajax success can leave the textarea visible; sent state is decisive."""
        target = {"id": "v2b", "name": "CF7 株式会社", "form_fields": {"inputs": []}}
        result = vf.verify_send_completed(
            target,
            "jp_form",
            snapshot="お問い合わせフォーム",
            browser_verify={
                "url": "https://example.co.jp/contact",
                "text": "お問い合わせフォーム",
                "cf7_sent": True,
                "cf7_invalid": False,
                "cf7_statuses": ["sent wpcf7-form sent"],
                "cf7_response_text": "ありがとうございます。メッセージは送信されました。",
            },
            evaluate_fn=lambda js: {"visible_forms": 1, "visible_textareas": 1,
                                    "empty_required": []},
        )
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["evidence"]["cf7_sent"])

    def test_cf7_invalid_keeps_visible_form_failed(self) -> None:
        target = {"id": "v2c", "name": "CF7 Invalid 株式会社", "form_fields": {"inputs": []}}
        result = vf.verify_send_completed(
            target,
            "jp_form",
            snapshot="入力内容に問題があります。確認してもう一度お試しください。",
            browser_verify={
                "url": "https://example.co.jp/contact",
                "text": "入力内容に問題があります。",
                "cf7_sent": False,
                "cf7_invalid": True,
                "cf7_statuses": ["invalid wpcf7-form invalid"],
            },
            evaluate_fn=lambda js: {"visible_forms": 1, "visible_textareas": 1,
                                    "empty_required": []},
        )
        self.assertEqual(result["status"], "needs_attention")

    def test_generic_sent_status_overrides_sticky_visible_form(self) -> None:
        """Non-CF7 Ajax success can also leave the form visible."""
        target = {"id": "v2d", "name": "Generic 株式会社", "form_fields": {"inputs": []}}
        result = vf.verify_send_completed(
            target,
            "jp_form",
            snapshot="お問い合わせフォーム",
            browser_verify={
                "url": "https://example.co.jp/contact",
                "text": "お問い合わせフォーム",
                "submission_sent": True,
                "submission_statuses": ["success form-success submitted"],
                "submission_status_text": "お問い合わせを受け付けました。",
            },
            evaluate_fn=lambda js: {"visible_forms": 1, "visible_textareas": 1,
                                    "empty_required": []},
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["evidence"]["send_verdict"], "sent_ok")

    def test_generic_invalid_keeps_visible_form_failed(self) -> None:
        target = {"id": "v2e", "name": "Generic Invalid 株式会社", "form_fields": {"inputs": []}}
        result = vf.verify_send_completed(
            target,
            "jp_form",
            snapshot="入力内容に問題があります。",
            browser_verify={
                "url": "https://example.co.jp/contact",
                "text": "入力内容に問題があります。",
                "submission_invalid": True,
                "submission_statuses": ["error form-error invalid"],
                "submission_status_text": "入力内容に問題があります。",
            },
            evaluate_fn=lambda js: {"visible_forms": 1, "visible_textareas": 1,
                                    "empty_required": []},
        )
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["evidence"]["send_verdict"], "failed")

    def test_uncertain_band_uses_llm_tiebreak_sent(self) -> None:
        """§V2: uncertain + LLM verdict sent (verbatim quote) → ok."""
        page = "担当者が内容を確認します。"
        target = {"id": "v3", "name": "Tiebreak 株式会社", "form_fields": {"inputs": []}}
        result = vf.verify_send_completed(
            target, "jp_form",
            snapshot=page,
            browser_verify={"url": "https://example.co.jp/contact/step3", "text": page},
            evaluate_fn=lambda js: {"visible_forms": 0, "visible_textareas": 0,
                                    "empty_required": []},
            infer_fn=lambda p, m: json.dumps(
                {"verdict": "sent", "quote": "担当者が内容を確認します"}, ensure_ascii=False
            ),
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("tiebreak", result["reason"])

    def test_uncertain_band_hallucinated_tiebreak_stays_uncertain(self) -> None:
        page = "担当者が内容を確認します。"
        target = {"id": "v4", "name": "Halluc 株式会社", "form_fields": {"inputs": []}}
        result = vf.verify_send_completed(
            target, "jp_form",
            snapshot=page,
            browser_verify={"url": "https://example.co.jp/contact/step3", "text": page},
            evaluate_fn=lambda js: {"visible_forms": 0, "visible_textareas": 0,
                                    "empty_required": []},
            infer_fn=lambda p, m: '{"verdict": "sent", "quote": "ページに無い文章"}',
        )
        self.assertEqual(result["status"], "uncertain")

    def test_no_infer_fn_behaves_as_before(self) -> None:
        target = {"id": "v5", "name": "Legacy 株式会社", "form_fields": {"inputs": []}}
        result = vf.verify_send_completed(
            target, "jp_form", snapshot="フォーム送信処理中です"
        )
        self.assertEqual(result["status"], "uncertain")


class TestPostSubmitEvidenceAlwaysSaved(unittest.TestCase):
    """§V acceptance 10 — run.py must dump post_submit_evidence.txt
    unconditionally (before the ok/failed branch), not only on failure."""

    def test_dump_is_unconditional_in_send_path(self) -> None:
        run_py = Path(__file__).resolve().parent.parent.parent / "jp-form-outreach" / "run.py"
        text = run_py.read_text(encoding="utf-8")
        idx = text.find('"post_submit_evidence.txt"')
        self.assertGreater(idx, 0, "post_submit_evidence.txt dump missing")
        # The dump must occur BEFORE the verify outcome branching in the send path.
        verify_call = text.find("vresult = verify_send_completed(", idx)
        self.assertGreater(
            verify_call, idx,
            "evidence dump must happen before verify (i.e. for sent_ok too)",
        )


if __name__ == "__main__":
    unittest.main()
