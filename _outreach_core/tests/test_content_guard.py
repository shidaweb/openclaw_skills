"""Tests for content-rejection detection + URL stripping (content_guard.py, v6 §3.6)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _outreach_core import content_guard as G  # noqa: E402


class TestDetectRejection(unittest.TestCase):
    def test_url_phrase(self):
        for txt in [
            "エラー：本文にURLは記載できません",
            "お問い合わせ内容にURLを含めることはできません",
            "リンクは入力できません",
            "URLs are not allowed in this field",
        ]:
            r = G.detect_content_rejection(txt)
            self.assertIsNotNone(r, txt)
            self.assertEqual(r["kind"], "url", txt)

    def test_char_phrase(self):
        for txt in [
            "問い合わせ内容に使用できない文字が使われています",
            "不正な文字が含まれています",
            "invalid characters detected",
        ]:
            r = G.detect_content_rejection(txt)
            self.assertIsNotNone(r, txt)
            self.assertEqual(r["kind"], "char", txt)

    def test_no_rejection_on_success_page(self):
        self.assertIsNone(G.detect_content_rejection("送信が完了しました。ありがとうございました。"))
        self.assertIsNone(G.detect_content_rejection(""))
        self.assertIsNone(G.detect_content_rejection(None))


class TestUrlDetection(unittest.TestCase):
    def test_has_url(self):
        self.assertTrue(G.has_url("予約はこちら https://tenbin.link/book/u-13/torana"))
        self.assertTrue(G.has_url("www.torana.co.jp"))
        self.assertFalse(G.has_url("URLは含まれていない本文です。"))

    def test_find_urls(self):
        urls = G.find_urls("A https://a.com/x B http://b.jp/y")
        self.assertEqual(len(urls), 2)


class TestStripUrls(unittest.TestCase):
    def test_removes_standalone_url_line(self):
        body = (
            "お世話になります。\n"
            "ご相談させてください。\n"
            "https://tenbin.link/book/u-1302066f5d4f/torana\n"
            "よろしくお願いします。"
        )
        clean, removed = G.strip_urls(body)
        self.assertEqual(len(removed), 1)
        self.assertNotIn("tenbin.link", clean)
        self.assertIn("よろしくお願いします", clean)

    def test_stops_at_japanese_no_space(self):
        # URL immediately followed by JP text — must not eat the JP.
        clean, _ = G.strip_urls("詳細はhttps://example.com/aをご覧ください。")
        self.assertNotIn("example.com", clean)
        self.assertIn("をご覧ください", clean)

    def test_no_url_is_unchanged(self):
        txt = "普通の本文です。URLなし。"
        clean, removed = G.strip_urls(txt)
        self.assertEqual(clean, txt)
        self.assertEqual(removed, [])

    def test_sanitize_reports_change(self):
        new, diag = G.sanitize_body("見てね https://x.com/y", kind="url")
        self.assertTrue(diag["changed"])
        self.assertEqual(len(diag["removed_urls"]), 1)
        self.assertFalse(G.has_url(new))

    def test_sanitize_idempotent_when_clean(self):
        new, diag = G.sanitize_body("URLなしの本文", kind="char")
        self.assertFalse(diag["changed"])


if __name__ == "__main__":
    unittest.main()


class TestToneLint(unittest.TestCase):
    """v31 §WS4c — deterministic casual-tone blocklist."""

    def test_casual_endings_flagged(self):
        self.assertTrue(G.find_tone_violations("これ、いいと思うんだよね。"))
        self.assertTrue(G.find_tone_violations("御社に合ってると思うっす。"))
        self.assertTrue(G.find_tone_violations("便利じゃん。"))
        self.assertTrue(G.find_tone_violations("資料を送らせてもらう。"))

    def test_slangy_phrases_flagged(self):
        self.assertTrue(G.find_tone_violations("ぶっちゃけ費用は安いです。"))
        self.assertTrue(G.find_tone_violations("めっちゃ効果があります。"))
        self.assertTrue(G.find_tone_violations("サクッと導入できます。"))
        self.assertTrue(G.find_tone_violations("弊社的にはおすすめです。"))

    def test_line_final_without_punctuation(self):
        self.assertTrue(G.find_tone_violations("ご検討いただければと思うんだよね"))

    def test_clean_keigo_body_does_not_trigger(self):
        clean = (
            "はじめまして、株式会社トラーナの志田と申します。\n"
            "貴社のサービスを拝見し、ぜひ一度お話しさせていただきたく"
            "ご連絡いたしました。\n"
            "以下の内容をご確認のうえ、ご返信いただけますと幸いです。\n"
            "何卒よろしくお願い申し上げます。"
        )
        self.assertEqual(G.find_tone_violations(clean), [])

    def test_polite_variants_do_not_trigger(self):
        # near-misses of the blocklist that are legitimate keigo
        self.assertEqual(G.find_tone_violations("お送りさせていただきます。"), [])
        self.assertEqual(G.find_tone_violations("ご確認くださいませ。"), [])
        self.assertEqual(G.find_tone_violations("超音波機器を扱っております。"), [])
        self.assertEqual(G.find_tone_violations("よろしくお願いいたします。"), [])

    def test_dedup_and_cap(self):
        body = "ぶっちゃけ、" * 20
        out = G.find_tone_violations(body)
        self.assertEqual(out, ["ぶっちゃけ"])

    def test_empty_is_clean(self):
        self.assertEqual(G.find_tone_violations(""), [])
        self.assertEqual(G.find_tone_violations(None), [])
