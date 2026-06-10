"""v15 §F — two-stage classification, sitemap mining, iframe form services."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import contact_url as cu


class TestClassificationIsUncertain(unittest.TestCase):
    """§F acceptance 4 — table-test the uncertainty predicate."""

    def test_table(self) -> None:
        ta = [{"name": "body", "label": "お問い合わせ内容"}]
        cases = [
            # (kind, reason, fields, expected)
            ("unknown_no_textarea", "no valid inquiry textarea", {"inputs": [{}]}, True),
            ("ir", "heading mentions ir", {"textareas": ta, "inputs": [{}]}, True),
            ("recruit", "recruit heading detected", {"inputs": [{}]}, False),
            ("contact", None, {"textareas": ta, "inputs": [{}]}, False),
            ("contact", "textarea_plus_submit", {}, True),  # zero fields collected
            ("b2c_support", "heading mentions b2c_support", {"inputs": [{}]}, False),
        ]
        for kind, reason, fields, expected in cases:
            with self.subTest(kind=kind, reason=reason):
                self.assertEqual(
                    cu.classification_is_uncertain(kind, reason, fields), expected
                )


class TestParseLlmClassification(unittest.TestCase):
    def test_valid_payload(self) -> None:
        raw = json.dumps({
            "form_type": "contact", "confidence": 0.85,
            "b2b_contact_hint_url": "https://x.jp/biz/contact",
        })
        out = cu.parse_llm_classification(raw)
        self.assertEqual(out["form_type"], "contact")
        self.assertAlmostEqual(out["confidence"], 0.85)
        self.assertEqual(out["b2b_contact_hint_url"], "https://x.jp/biz/contact")

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(cu.parse_llm_classification("not json at all"))
        self.assertIsNone(cu.parse_llm_classification(None))
        self.assertIsNone(cu.parse_llm_classification('{"form_type": "alien"}'))
        self.assertIsNone(cu.parse_llm_classification('{"form_type": "contact"}'))

    def test_non_http_hint_dropped(self) -> None:
        out = cu.parse_llm_classification(
            '{"form_type": "contact", "confidence": 0.9, "b2b_contact_hint_url": "javascript:void(0)"}'
        )
        self.assertIsNone(out["b2b_contact_hint_url"])


class TestClassifyFormTypeV2(unittest.TestCase):
    UNCERTAIN_FIELDS = {"inputs": [{"name": "email", "label": "メール"}]}  # no textarea

    def test_llm_not_called_when_heuristic_certain(self) -> None:
        fields = {
            "inputs": [{"name": "email", "label": "メールアドレス"}],
            "textareas": [{"name": "inquiry_body", "label": "お問い合わせ内容"}],
            "submit_buttons": [{"text": "送信する", "disabled": False}],
        }
        called = []
        out = cu.classify_form_type_v2(
            fields, "法人のお問い合わせ",
            infer_fn=lambda p, m: called.append(p) or "{}",
        )
        self.assertEqual(out["kind"], "contact")
        self.assertFalse(out["llm_called"])
        self.assertEqual(called, [])

    def test_llm_overrides_when_confident(self) -> None:
        out = cu.classify_form_type_v2(
            self.UNCERTAIN_FIELDS, "ページ本文",
            infer_fn=lambda p, m: '{"form_type": "recruit", "confidence": 0.9, "b2b_contact_hint_url": null}',
        )
        self.assertEqual(out["kind"], "recruit")
        self.assertEqual(out["src"], "llm")
        self.assertTrue(out["llm_called"])

    def test_low_confidence_keeps_heuristic(self) -> None:
        out = cu.classify_form_type_v2(
            self.UNCERTAIN_FIELDS, "ページ本文",
            infer_fn=lambda p, m: '{"form_type": "recruit", "confidence": 0.4, "b2b_contact_hint_url": null}',
        )
        self.assertEqual(out["kind"], "unknown_no_textarea")
        self.assertEqual(out["src"], "heuristic")

    def test_parse_failure_falls_back_to_heuristic(self) -> None:
        """§F acceptance 5 — unusable LLM output → heuristic verdict."""
        out = cu.classify_form_type_v2(
            self.UNCERTAIN_FIELDS, "ページ本文",
            infer_fn=lambda p, m: "Sorry, I cannot help with that.",
        )
        self.assertEqual(out["kind"], "unknown_no_textarea")
        self.assertEqual(out["src"], "heuristic")
        self.assertTrue(out["llm_called"])

    def test_infer_exception_falls_back(self) -> None:
        def boom(p, m):
            raise TimeoutError("llm timeout")

        out = cu.classify_form_type_v2(self.UNCERTAIN_FIELDS, "x", infer_fn=boom)
        self.assertEqual(out["kind"], "unknown_no_textarea")

    def test_hint_url_carried_even_when_low_confidence(self) -> None:
        out = cu.classify_form_type_v2(
            self.UNCERTAIN_FIELDS, "x",
            infer_fn=lambda p, m: '{"form_type": "b2c_support", "confidence": 0.5, '
                                  '"b2b_contact_hint_url": "https://x.jp/biz"}',
        )
        self.assertEqual(out["b2b_contact_hint_url"], "https://x.jp/biz")
        self.assertEqual(out["src"], "heuristic")


class TestSitemapExtraction(unittest.TestCase):
    """§F acceptance 6."""

    def test_extracts_contact_urls(self) -> None:
        xml = """<?xml version="1.0"?>
        <urlset>
          <url><loc>https://example.co.jp/</loc></url>
          <url><loc>https://example.co.jp/contact/</loc></url>
          <url><loc> https://example.co.jp/inquiry </loc></url>
          <url><loc>https://example.co.jp/recruit/contact</loc></url>
          <url><loc>https://example.co.jp/news/2026</loc></url>
          <url><loc>https://example.co.jp/otoiawase</loc></url>
        </urlset>"""
        out = cu.extract_contact_urls_from_sitemap(xml)
        self.assertIn("https://example.co.jp/contact/", out)
        self.assertIn("https://example.co.jp/inquiry", out)
        self.assertIn("https://example.co.jp/otoiawase", out)
        self.assertNotIn("https://example.co.jp/recruit/contact", out)
        self.assertNotIn("https://example.co.jp/news/2026", out)

    def test_empty_or_garbage(self) -> None:
        self.assertEqual(cu.extract_contact_urls_from_sitemap(""), [])
        self.assertEqual(cu.extract_contact_urls_from_sitemap(None), [])
        self.assertEqual(cu.extract_contact_urls_from_sitemap("<html>404</html>"), [])


class TestIframeFormSrc(unittest.TestCase):
    """§F acceptance 6."""

    def test_known_service_host_accepted(self) -> None:
        iframes = [{"src": "https://form.run/embed/abc"}]
        self.assertEqual(
            cu.iframe_form_src(iframes, "https://example.co.jp/contact"),
            "https://form.run/embed/abc",
        )

    def test_hubspot_accepted(self) -> None:
        iframes = [{"src": "https://share.hsforms.com/xyz"}]
        self.assertIsNotNone(cu.iframe_form_src(iframes, "https://example.co.jp"))

    def test_same_registrable_domain_accepted(self) -> None:
        iframes = [{"src": "https://forms.example.co.jp/inq"}]
        self.assertEqual(
            cu.iframe_form_src(iframes, "https://www.example.co.jp/contact"),
            "https://forms.example.co.jp/inq",
        )

    def test_unrelated_third_party_rejected(self) -> None:
        iframes = [
            {"src": "https://www.youtube.com/embed/xyz"},
            {"src": "https://maps.google.com/embed"},
        ]
        self.assertIsNone(cu.iframe_form_src(iframes, "https://example.co.jp"))

    def test_empty(self) -> None:
        self.assertIsNone(cu.iframe_form_src([], "https://example.co.jp"))
        self.assertIsNone(cu.iframe_form_src(None, "https://example.co.jp"))


class TestExpandedCommonPaths(unittest.TestCase):
    def test_new_paths_present(self) -> None:
        out = cu.common_contact_paths("https://www.example.co.jp/")
        self.assertIn("https://www.example.co.jp/contact-us", out)
        self.assertIn("https://www.example.co.jp/business/inquiry", out)
        self.assertIn("https://www.example.co.jp/support/inquiry", out)


if __name__ == "__main__":
    unittest.main()
