from __future__ import annotations

import unittest

from _outreach_core import contact_url as cu


class TestContactUrl(unittest.TestCase):
    def test_contact_link_candidates_filters_recruit_ir_faq(self) -> None:
        base = "https://example.co.jp/recruit"
        links = [
            {"href": "/recruit/contact", "text": "採用応募フォーム"},
            {"href": "/ir/contact", "text": "IRお問い合わせ"},
            {"href": "/faq", "text": "FAQ"},
            {"href": "/contact", "text": "お問い合わせ"},
            {"href": "/business/inquiry", "text": "法人のお問い合わせ"},
        ]
        out = cu.contact_link_candidates(links, base)
        self.assertIn("https://example.co.jp/contact", out)
        self.assertIn("https://example.co.jp/business/inquiry", out)
        self.assertNotIn("https://example.co.jp/recruit/contact", out)
        self.assertNotIn("https://example.co.jp/ir/contact", out)

    def test_common_contact_paths_same_domain(self) -> None:
        out = cu.common_contact_paths("https://www.example.co.jp/path/x")
        self.assertTrue(out)
        for u in out:
            self.assertTrue(u.startswith("https://www.example.co.jp/"))

    def test_same_registrable_domain(self) -> None:
        self.assertTrue(cu.same_registrable_domain("https://a.example.co.jp/x", "https://b.example.co.jp/y"))
        self.assertFalse(cu.same_registrable_domain("https://example.co.jp", "https://evil.com"))

    def test_classify_ir_and_support_non_contact(self) -> None:
        fields = {
            "inputs": [{"name": "email", "label": "メール"}],
            "textareas": [{"name": "query", "label": "検索キーワード", "placeholder": "search"}],
        }
        kind, _reason = cu.classify_form_type(fields, "IR 投資家情報")
        self.assertEqual(kind, "ir")
        kind2, _reason2 = cu.classify_form_type(fields, "お客様相談室 カスタマーサポート")
        self.assertEqual(kind2, "b2c_support")

    def test_classify_recruit_with_textarea_still_recruit(self) -> None:
        fields = {
            "inputs": [{"name": "applicant_name", "label": "応募者氏名"}],
            "textareas": [{"name": "resume", "label": "志望動機"}],
        }
        kind, _reason = cu.classify_form_type(fields, "中途採用エントリー")
        self.assertEqual(kind, "recruit")

    def test_classify_contact_textarea(self) -> None:
        fields = {
            "inputs": [{"name": "email", "label": "メールアドレス"}],
            "textareas": [{"name": "inquiry_body", "label": "お問い合わせ内容"}],
            "submit_buttons": [{"text": "送信する", "disabled": False}],
        }
        kind, _reason = cu.classify_form_type(fields, "法人のお問い合わせ")
        self.assertEqual(kind, "contact")

    def test_classify_contact_with_placeholder_select_still_contact(self) -> None:
        fields = {
            "inputs": [{"name": "email", "label": "メールアドレス"}],
            "textareas": [{"name": "details", "label": "詳細をお書きください"}],
            "selects": [{"name": "kind", "label": "お問い合わせ種別", "options": ["選択してください", "個人のお客様", "その他"]}],
            "submit_buttons": [{"text": "送信", "disabled": False}],
        }
        kind, _reason = cu.classify_form_type(fields, "お問い合わせ")
        self.assertEqual(kind, "contact")

    def test_classify_pre_form_gate_without_textarea_as_contact(self) -> None:
        fields = {
            "inputs": [{"name": "email", "label": "メールアドレス"}],
            "textareas": [],
            "radios": {"contact_kind": [{"label": "法人のお問い合わせ", "checked": False}]},
            "checkboxes": [{"label": "上記に同意してお問い合わせする", "checked": False}],
        }
        snap = "お問い合わせ お問い合わせ種別 メールフォームはこちら 上記に同意してお問い合わせする"
        kind, reason = cu.classify_form_type(fields, snap)
        self.assertEqual(kind, "contact")
        self.assertEqual(reason, "pre_form_gate")

    def test_is_error_page_detects_404_text(self) -> None:
        self.assertTrue(cu.is_error_page("404 ページが見つかりません", url="https://example.co.jp/contact"))
        self.assertTrue(cu.is_error_page("normal", http_status=404))
        self.assertFalse(cu.is_error_page("お問い合わせフォーム", url="https://example.co.jp/inquiry"))

    def test_is_error_page_ignores_numbers_in_body_copy(self) -> None:
        # v31 §WS2d — 「従業員500名」「1500円」 must not flag an error page.
        self.assertFalse(cu.is_error_page("会社概要 従業員500名 資本金1500万円 お問い合わせ"))
        self.assertFalse(cu.is_error_page("送料 500円（税込） 5030コース"))
        # A real error page leads with the code near the top.
        self.assertTrue(cu.is_error_page("Error 500 - Internal Server Error"))
        self.assertTrue(cu.is_error_page("404 Not Found"))
        # The code buried deep in a long healthy page does not count.
        long_page = "お問い合わせフォーム " + ("会社案内 " * 100) + " エラー 500 "
        self.assertFalse(cu.is_error_page(long_page))

    def test_support_contact_survives_avoid_list(self) -> None:
        # v31 §WS2c — /support/* is avoided EXCEPT when a desired keyword
        # (contact/inquiry) also appears in the path.
        base = "https://example.co.jp/"
        links = [
            {"href": "/support/contact", "text": "お問い合わせ"},
            {"href": "/support/inquiry", "text": "法人お問い合わせ"},
            {"href": "/support/faq-top", "text": "お問い合わせ"},
        ]
        out = cu.contact_link_candidates(links, base)
        self.assertIn("https://example.co.jp/support/contact", out)
        self.assertIn("https://example.co.jp/support/inquiry", out)
        self.assertNotIn("https://example.co.jp/support/faq-top", out)

    def test_common_contact_paths_v31_expansion(self) -> None:
        out = cu.common_contact_paths("https://example.co.jp/")
        for path in ("/contactform", "/contact.html", "/inquiry.html",
                     "/otoiawase.html", "/support/contact", "/ja/contact",
                     "/mailform"):
            self.assertIn(f"https://example.co.jp{path}", out)
        # bare /mail deliberately excluded (webmail false hits)
        self.assertNotIn("https://example.co.jp/mail", out)

    def test_sitemap_mailform_and_anchored_form(self) -> None:
        xml = """
        <urlset>
          <loc>https://example.co.jp/mailform/</loc>
          <loc>https://example.co.jp/form</loc>
          <loc>https://example.co.jp/form.html</loc>
          <loc>https://example.co.jp/reform/</loc>
          <loc>https://example.co.jp/information/</loc>
          <loc>https://example.co.jp/support/contact/</loc>
        </urlset>
        """
        out = cu.extract_contact_urls_from_sitemap(xml)
        self.assertIn("https://example.co.jp/mailform/", out)
        self.assertIn("https://example.co.jp/form", out)
        self.assertIn("https://example.co.jp/form.html", out)
        self.assertIn("https://example.co.jp/support/contact/", out)
        self.assertNotIn("https://example.co.jp/reform/", out)
        self.assertNotIn("https://example.co.jp/information/", out)

    def test_google_forms_service_urls(self) -> None:
        # v31 §WS2b
        self.assertTrue(cu.is_form_service_url("https://forms.gle/AbCd123"))
        self.assertTrue(cu.is_form_service_url(
            "https://docs.google.com/forms/d/e/1FAIpQLSe/viewform?embedded=true"
        ))
        # An arbitrary Google Docs embed is NOT a form service.
        self.assertFalse(cu.is_form_service_url(
            "https://docs.google.com/document/d/abc/pub?embedded=true"
        ))
        self.assertFalse(cu.is_form_service_url("https://example.co.jp/contact"))

    def test_iframe_takeover_accepts_google_forms(self) -> None:
        iframes = [
            {"src": "https://docs.google.com/forms/d/e/1FAIpQLSe/viewform?embedded=true"},
        ]
        src = cu.iframe_form_src(iframes, "https://example.co.jp/contact/")
        self.assertIsNotNone(src)
        self.assertIn("docs.google.com/forms", src)
        # non-form Google embed rejected
        self.assertIsNone(cu.iframe_form_src(
            [{"src": "https://docs.google.com/presentation/d/x/embed"}],
            "https://example.co.jp/contact/",
        ))


if __name__ == "__main__":
    unittest.main()
