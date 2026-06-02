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


if __name__ == "__main__":
    unittest.main()
