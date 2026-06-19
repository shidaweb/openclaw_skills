"""form_validation (v15) — furigana script + subject + inline-error parsing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import form_validation as fv


class TestScriptDetection(unittest.TestCase):
    def test_contains_kanji(self) -> None:
        self.assertTrue(fv.contains_kanji("志田典道"))
        self.assertFalse(fv.contains_kanji("シダノリミチ"))
        self.assertFalse(fv.contains_kanji("しだのりみち"))

    def test_is_katakana(self) -> None:
        self.assertTrue(fv.is_katakana("シダ"))
        self.assertTrue(fv.is_katakana("シダ ノリミチ"))
        self.assertFalse(fv.is_katakana("しだ"))
        self.assertFalse(fv.is_katakana("志田"))
        self.assertFalse(fv.is_katakana(""))

    def test_is_hiragana(self) -> None:
        self.assertTrue(fv.is_hiragana("しだ"))
        self.assertFalse(fv.is_hiragana("シダ"))
        self.assertFalse(fv.is_hiragana("志田"))

    def test_kana_conversions_roundtrip(self) -> None:
        self.assertEqual(fv.katakana_to_hiragana("シダノリミチ"), "しだのりみち")
        self.assertEqual(fv.hiragana_to_katakana("しだのりみち"), "シダノリミチ")


class TestPostalNormalization(unittest.TestCase):
    def test_postal_label_detection(self) -> None:
        self.assertTrue(fv.is_postal_field_label("郵便番号"))
        self.assertTrue(fv.is_postal_field_label("g_postalCode"))
        self.assertTrue(fv.is_postal_field_label("zip_code"))
        self.assertFalse(fv.is_postal_field_label("電話番号"))

    def test_normalizes_hyphenated_and_fullwidth_postal_codes(self) -> None:
        self.assertEqual(fv.normalize_postal_code("260-0003"), "2600003")
        self.assertEqual(fv.normalize_postal_code("２６０－０００３"), "2600003")

    def test_does_not_rewrite_non_postal_length(self) -> None:
        self.assertEqual(fv.normalize_postal_code("03-1234-5678"), "03-1234-5678")


class TestLabelClassification(unittest.TestCase):
    def test_expected_kana_kind(self) -> None:
        self.assertEqual(fv.expected_kana_kind("フリガナ（姓）"), "katakana")
        self.assertEqual(fv.expected_kana_kind("カナ"), "katakana")
        self.assertEqual(fv.expected_kana_kind("ふりがな（名）"), "hiragana")
        self.assertIsNone(fv.expected_kana_kind("姓"))
        self.assertIsNone(fv.expected_kana_kind("お名前"))

    def test_name_part(self) -> None:
        self.assertEqual(fv.name_part("フリガナ（姓）"), "sei")
        self.assertEqual(fv.name_part("フリガナ（名）"), "mei")
        self.assertEqual(fv.name_part("セイ"), "sei")
        self.assertEqual(fv.name_part("メイ"), "mei")
        self.assertIsNone(fv.name_part("フリガナ"))


class TestFuriganaValue(unittest.TestCase):
    SENDER = {"name_kana": "シダノリミチ", "name_furigana": "しだのりみち"}

    def test_katakana_sei_mei_split(self) -> None:
        self.assertEqual(fv.furigana_value_for_label("フリガナ（姓）", self.SENDER), "シダ")
        self.assertEqual(fv.furigana_value_for_label("フリガナ（名）", self.SENDER), "ノリミチ")

    def test_hiragana_field(self) -> None:
        self.assertEqual(fv.furigana_value_for_label("ふりがな（姓）", self.SENDER), "しだ")

    def test_full_when_no_part(self) -> None:
        self.assertEqual(fv.furigana_value_for_label("フリガナ", self.SENDER), "シダノリミチ")

    def test_explicit_sender_split_wins(self) -> None:
        sender = dict(self.SENDER, name_kana_sei="シダ", name_kana_mei="ノリミチ")
        self.assertEqual(fv.furigana_value_for_label("フリガナ（名）", sender), "ノリミチ")

    def test_derive_hiragana_from_katakana(self) -> None:
        sender = {"name_kana": "シダノリミチ"}  # no furigana provided
        self.assertEqual(fv.furigana_value_for_label("ふりがな", sender), "しだのりみち")

    def test_non_kana_label_returns_none(self) -> None:
        self.assertIsNone(fv.furigana_value_for_label("お名前", self.SENDER))


class TestNeedsKanaFix(unittest.TestCase):
    def test_kanji_in_katakana_field_needs_fix(self) -> None:
        # The exact YAMAHA bug: kanji in フリガナ（姓）.
        self.assertTrue(fv.needs_kana_fix("フリガナ（姓）", "志田典道"))

    def test_correct_katakana_ok(self) -> None:
        self.assertFalse(fv.needs_kana_fix("フリガナ（姓）", "シダ"))

    def test_hiragana_in_katakana_field_needs_fix(self) -> None:
        self.assertTrue(fv.needs_kana_fix("フリガナ（姓）", "しだ"))

    def test_empty_not_flagged_here(self) -> None:
        self.assertFalse(fv.needs_kana_fix("フリガナ（姓）", ""))

    def test_non_kana_field_never_flagged(self) -> None:
        self.assertFalse(fv.needs_kana_fix("姓", "志田"))


class TestKanaFieldCorrection(unittest.TestCase):
    SENDER = {"name_kana": "シダノリミチ", "name_furigana": "しだのりみち"}

    def test_kanji_in_sei_field(self) -> None:
        self.assertEqual(fv.kana_field_correction("フリガナ（姓）", "志田典道", self.SENDER), "シダ")

    def test_wrong_split_full_reading_in_mei_field(self) -> None:
        # valid katakana but the full reading landed in the 名 sub-field
        self.assertEqual(fv.kana_field_correction("フリガナ（名）", "シダノリミツ", self.SENDER), "ノリミチ")

    def test_correct_sei_no_change(self) -> None:
        self.assertIsNone(fv.kana_field_correction("フリガナ（姓）", "シダ", self.SENDER))

    def test_full_field_valid_kana_not_touched(self) -> None:
        # bare フリガナ field holding a valid full reading is left alone
        self.assertIsNone(fv.kana_field_correction("フリガナ", "シダノリミチ", self.SENDER))

    def test_full_field_kanji_corrected(self) -> None:
        self.assertEqual(fv.kana_field_correction("フリガナ", "志田典道", self.SENDER), "シダノリミチ")

    def test_non_kana_field(self) -> None:
        self.assertIsNone(fv.kana_field_correction("姓", "志田", self.SENDER))


class TestSubject(unittest.TestCase):
    def test_is_subject_label(self) -> None:
        self.assertTrue(fv.is_subject_label("お問い合わせタイトル"))
        self.assertTrue(fv.is_subject_label("件名"))
        self.assertTrue(fv.is_subject_label("ご用件"))
        self.assertFalse(fv.is_subject_label("お問い合わせ内容"))
        self.assertFalse(fv.is_subject_label("本文"))

    def test_derive_subject_uses_draft(self) -> None:
        self.assertEqual(
            fv.derive_subject({"subject": "LINE連携CRMのご提案"}), "LINE連携CRMのご提案"
        )

    def test_derive_subject_fallback_on_skip(self) -> None:
        self.assertEqual(fv.derive_subject({"subject": "SKIP"}), "サービスのご提案")
        self.assertEqual(fv.derive_subject(None), "サービスのご提案")

    def test_derive_subject_capped(self) -> None:
        self.assertLessEqual(len(fv.derive_subject({"subject": "あ" * 100})), 48)


class TestParseValidationErrors(unittest.TestCase):
    def test_parses_yamaha_errors(self) -> None:
        text = (
            '"フリガナ（姓）"の形式が正しくありません。\n'
            '"お問い合わせタイトル"を入力してください。'
        )
        errs = fv.parse_validation_errors(text)
        fields = {(e["field"], e["kind"]) for e in errs}
        self.assertIn(("フリガナ（姓）", "format"), fields)
        self.assertIn(("お問い合わせタイトル", "required"), fields)

    def test_required_phrasings(self) -> None:
        self.assertTrue(
            any(e["kind"] == "required" for e in fv.parse_validation_errors("メールアドレスは必須です"))
        )
        self.assertTrue(
            any(e["kind"] == "required" for e in fv.parse_validation_errors("電話番号を入力してください"))
        )

    def test_no_errors_on_clean_text(self) -> None:
        self.assertEqual(fv.parse_validation_errors("送信が完了しました。ありがとうございました。"), [])
        self.assertFalse(fv.has_validation_errors("お問い合わせ完了"))

    def test_dedup(self) -> None:
        text = "件名を入力してください\n件名を入力してください"
        self.assertEqual(len(fv.parse_validation_errors(text)), 1)

    def test_hankaku_numeric_is_format_not_required(self) -> None:
        errs = fv.parse_validation_errors(
            "- generic [ref=e386]: 数値を半角で入力してください。"
        )
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["kind"], "hankaku_numeric")


if __name__ == "__main__":
    unittest.main()
