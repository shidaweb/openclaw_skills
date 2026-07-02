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


class TestFormStructureHealth(unittest.TestCase):
    """v30 next — detect broken form structure early so a 300s lead timeout
    is replaced by an immediate skip with a precise reason.

    Pilot 2026-06-30 V9 batch (株式会社テンダ): the form's input.name attribute
    rendered as "[object HTMLInputElement]" — a JS framework bug that defies
    every field-by-name fixer. The legacy path ran the full fill/submit
    cycle, hit a disabled submit button, and finally timed out at 300s.
    """

    def test_object_html_name_detected_as_broken(self) -> None:
        self.assertTrue(fv.field_name_is_broken("[object HTMLInputElement]"))
        self.assertTrue(fv.field_name_is_broken("[object HTMLTextAreaElement]"))
        self.assertTrue(fv.field_name_is_broken("[object Object]"))

    def test_normal_field_names_not_broken(self) -> None:
        for name in ("email", "company_name", "your-name", "tel", "data[User][email]"):
            self.assertFalse(fv.field_name_is_broken(name))

    def test_empty_name_not_treated_as_broken(self) -> None:
        # Empty / missing names are a different problem (heuristic fallback
        # by label / placeholder). field_name_is_broken specifically targets
        # the malformed-object-stringification case.
        self.assertFalse(fv.field_name_is_broken(""))
        self.assertFalse(fv.field_name_is_broken(None))

    def test_form_has_broken_structure_one_or_more_object_names(self) -> None:
        fields = {
            "inputs": [
                {"name": "name", "type": "text"},
                {"name": "[object HTMLInputElement]", "type": "text"},
            ],
        }
        self.assertTrue(fv.form_has_broken_structure(fields))

    def test_form_has_broken_structure_clean_form(self) -> None:
        fields = {
            "inputs": [{"name": "name"}, {"name": "email"}],
            "textareas": [{"name": "message"}],
        }
        self.assertFalse(fv.form_has_broken_structure(fields))

    def test_form_has_broken_structure_handles_missing_dict(self) -> None:
        self.assertFalse(fv.form_has_broken_structure(None))
        self.assertFalse(fv.form_has_broken_structure({}))


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


class TestFormatCategorization(unittest.TestCase):
    """v30 next — format-class errors (zenkaku-length, hiragana, phone-format)
    must NOT slip through ``_ERR_REQUIRED_RE``'s trailing 「入力してください」
    catch-all. Production 2026-06-29 (sunstar) showed text-extracted errors
    like 「全角64文字以内で[required]」 / 「ひらがなのみで[required]」 /
    「電話番号形式で[required]」 — all real format constraints mis-labeled as
    'required', so the existing zenkaku / phone format fixers never fired.
    """

    def test_zenkaku_length_classified_as_zenkaku(self) -> None:
        text = "お名前 は全角64文字以内で入力してください"
        errs = fv.parse_validation_errors(text)
        kinds = [(e["field"], e["kind"]) for e in errs]
        self.assertIn(("お名前", "zenkaku"), kinds)
        # Not also re-classified as required.
        self.assertFalse(any(k == "required" for _, k in kinds))

    def test_zenkaku_length_three_digit_chars(self) -> None:
        # 255-char limit (sunstar 住所 case) — the digit must be allowed any
        # length, not just single digits.
        text = "住所 は全角255文字以内で入力してください"
        errs = fv.parse_validation_errors(text)
        self.assertTrue(any(e["kind"] == "zenkaku" for e in errs))

    def test_hiragana_only_classified_as_hiragana(self) -> None:
        text = "フリガナ はひらがなのみで入力してください"
        errs = fv.parse_validation_errors(text)
        kinds = [(e["field"], e["kind"]) for e in errs]
        self.assertIn(("フリガナ", "hiragana"), kinds)
        self.assertFalse(any(k == "required" for _, k in kinds))

    def test_hiragana_without_nomi_still_matches(self) -> None:
        text = "ふりがな はひらがなで入力してください"
        errs = fv.parse_validation_errors(text)
        self.assertTrue(any(e["kind"] == "hiragana" for e in errs))

    def test_phone_format_classified_as_format(self) -> None:
        # 「電話番号形式で入力してください」 → format kind so the existing
        # _fix_phone_format_errors helper picks it up.
        text = "電話番号 は電話番号形式で入力してください"
        errs = fv.parse_validation_errors(text)
        kinds = [(e["field"], e["kind"]) for e in errs]
        self.assertTrue(any("電話番号" in f and k == "format" for f, k in kinds))

    def test_other_required_still_classified_as_required(self) -> None:
        # A plain 「お名前を入力してください」 keeps its required kind — the
        # new regexes are additions, not replacements.
        text = "お名前を入力してください"
        errs = fv.parse_validation_errors(text)
        self.assertEqual(errs[0]["field"], "お名前")
        self.assertEqual(errs[0]["kind"], "required")

    def test_prefix_less_zenkaku_uses_fallback_label(self) -> None:
        # Production redux #3 (sunstar 2026-06-29) — pages emit bare
        # 「全角64文字以内で入力してください」 with NO leading field label.
        # Must still classify as zenkaku (was leaking to required before).
        text = "全角64文字以内で入力してください"
        errs = fv.parse_validation_errors(text)
        kinds = [(e["field"], e["kind"]) for e in errs]
        self.assertIn(("全角文字", "zenkaku"), kinds)
        self.assertNotIn(("全角64文字以内で", "required"), kinds)

    def test_prefix_less_hiragana_uses_fallback_label(self) -> None:
        text = "ひらがなのみで入力してください"
        errs = fv.parse_validation_errors(text)
        kinds = [(e["field"], e["kind"]) for e in errs]
        self.assertIn(("ふりがな", "hiragana"), kinds)

    def test_prefix_less_phone_format_uses_fallback_label(self) -> None:
        text = "電話番号形式で入力してください"
        errs = fv.parse_validation_errors(text)
        kinds = [(e["field"], e["kind"]) for e in errs]
        self.assertIn(("電話番号", "format"), kinds)


class TestAriaSnapshotLeakage(unittest.TestCase):
    """v30 §WS-A: production runs concatenated Playwright aria-snapshot output with
    page text and fed both into the regex parser. Body paragraphs and row labels
    got captured as 'required' fields, so the resolver looped clicking "次へ" with
    nothing to fix. Reproduces Fujisoft, SUPER STUDIO, MIL needs_attention items
    from 2026-06-29 / 2026-06-27 runs.
    """

    def test_fujisoft_consent_paragraph_not_flagged_as_required(self) -> None:
        # The full privacy-policy paragraph from the Fujisoft form's consent table.
        # Ends with 「が含まれます。」 — no 〜してください, no 必須 marker.
        text = (
            "- text: 自動的に収集・記録される個人情報には、お客様のアクセスログ情報"
            "（アプリケーション異常終了時の情報、 アクセスしたページ、ドメイン名、IP"
            " アドレス、参照元情報、使用しているブラウザの種類、アクセス日時、 Cookie"
            " 情報、利用した検索エンジン、検索エンジンに入力した検索キーワード、弊社"
            "から配信されるメール 文面記載のクリックカウント URL のうち、どの URLから"
            "流入したかなど）が含まれます。"
        )
        errs = fv.parse_validation_errors(text)
        self.assertEqual(
            errs, [],
            f"aria-snapshot text-node body should not be parsed as a field error: {errs}",
        )

    def test_fujisoft_instruction_paragraph_not_flagged(self) -> None:
        # Instruction line that DOES contain a verb in the regex set: the aria
        # tree leaks 「ご記入」 from the form help text.
        text = "- text: 以下の項目に必要事項をご記入後、「次へ」ボタンを押してください。"
        errs = fv.parse_validation_errors(text)
        self.assertEqual(errs, [], f"instruction body should not be a field: {errs}")

    def test_aria_tree_text_with_verb_not_flagged(self) -> None:
        # A 「〜を入力し〜」 sentence appearing inside a body paragraph — exactly
        # what Fujisoft's privacy text contained ("検索エンジンに入力した").
        text = "- text: 弊社では、検索エンジンに入力した検索キーワードを記録します。"
        errs = fv.parse_validation_errors(text)
        self.assertEqual(errs, [])

    def test_aria_tree_row_label_not_flagged(self) -> None:
        # Row/cell labels with embedded errors-icon text ("！ 必ず…してください")
        # — the SUPER STUDIO / MIL logs surfaced exactly this kind of leak.
        text = (
            '- row "部署名 必須 ！ 必ず入力してください" [ref=e35]:\n'
            '  - rowheader "部署名 必須" [ref=e36]\n'
            '  - cell "！ 必ず入力してください" [ref=e38]'
        )
        errs = fv.parse_validation_errors(text)
        self.assertEqual(errs, [], f"row/cell tree nodes leaked as fields: {errs}")

    def test_aria_tree_generic_node_not_flagged(self) -> None:
        # Bare tree node descriptors must never produce a field entry.
        text = "- generic [ref=e42]\n- paragraph [ref=e13]\n- /url: https://x.example/"
        errs = fv.parse_validation_errors(text)
        self.assertEqual(errs, [])

    def test_genuine_error_alongside_snapshot_still_parsed(self) -> None:
        # Mixing real error lines with snapshot noise: real ones survive, noise
        # is filtered. (The aria-tree line below contains the SAME 「入力し」
        # verb pattern as the real error.)
        text = (
            "- text: 自動的に収集・記録される個人情報を当社に入力してください。\n"
            "メールアドレスを入力してください\n"
            '- row "氏名 必須" [ref=e35]'
        )
        errs = fv.parse_validation_errors(text)
        kinds = [(e["field"], e["kind"]) for e in errs]
        self.assertIn(("メールアドレス", "required"), kinds)
        # The aria-snapshot lines must NOT contribute extra required entries.
        self.assertEqual(len(errs), 1, f"expected 1 error, got {kinds}")


if __name__ == "__main__":
    unittest.main()


class TestHyphenateJpPhone(unittest.TestCase):
    """v31 §WS5b — area-code-aware hyphenation tables."""

    def test_two_digit_area_codes(self) -> None:
        self.assertEqual(fv.hyphenate_jp_phone("0312345678"), "03-1234-5678")
        self.assertEqual(fv.hyphenate_jp_phone("0612345678"), "06-1234-5678")

    def test_three_digit_area_codes(self) -> None:
        # the legacy 2-4-4 was wrong for these
        self.assertEqual(fv.hyphenate_jp_phone("0431234567"), "043-123-4567")
        self.assertEqual(fv.hyphenate_jp_phone("0451234567"), "045-123-4567")
        self.assertEqual(fv.hyphenate_jp_phone("0521234567"), "052-123-4567")

    def test_free_dial(self) -> None:
        self.assertEqual(fv.hyphenate_jp_phone("0120345678"), "0120-345-678")
        self.assertEqual(fv.hyphenate_jp_phone("0800123456"), "0800-123-456")

    def test_mobile_and_ip(self) -> None:
        self.assertEqual(fv.hyphenate_jp_phone("09012345678"), "090-1234-5678")
        self.assertEqual(fv.hyphenate_jp_phone("05012345678"), "050-1234-5678")

    def test_non_phone_passthrough(self) -> None:
        self.assertEqual(fv.hyphenate_jp_phone("12345"), "12345")
        self.assertEqual(fv.hyphenate_jp_phone("1234567890"), "1234567890")  # no leading 0
        self.assertEqual(fv.hyphenate_jp_phone(""), "")


class TestPhoneFormatCandidates(unittest.TestCase):
    def test_from_hyphenated(self) -> None:
        cands = fv.phone_format_candidates("043-123-4567")
        self.assertEqual(cands[0], "0431234567")
        self.assertNotIn("043-123-4567", cands)  # current excluded
        self.assertEqual(len(cands), len(set(cands)))  # deduped

    def test_from_digits_only(self) -> None:
        cands = fv.phone_format_candidates("0431234567")
        self.assertEqual(cands[0], "043-123-4567")  # area-aware first
        self.assertIn("04-3123-4567", cands)        # legacy toggle last

    def test_non_phone_yields_nothing(self) -> None:
        self.assertEqual(fv.phone_format_candidates("abc"), [])
        self.assertEqual(fv.phone_format_candidates(""), [])


class TestZenkakuFixAllowed(unittest.TestCase):
    """v31 §WS5a — guard corpus."""

    def test_address_value_allowed(self) -> None:
        self.assertTrue(fv.zenkaku_fix_allowed("addr2", "住所（番地）", "1-2-3", "text"))

    def test_protected_input_types(self) -> None:
        for t in ("email", "url", "tel", "number"):
            self.assertFalse(fv.zenkaku_fix_allowed("f", "住所", "abc", t))

    def test_protected_context_keywords(self) -> None:
        self.assertFalse(fv.zenkaku_fix_allowed("mail_addr", "", "abc", "text"))
        self.assertFalse(fv.zenkaku_fix_allowed("f", "メールアドレス", "abc", "text"))
        self.assertFalse(fv.zenkaku_fix_allowed("f", "ホームページURL", "abc", "text"))
        self.assertFalse(fv.zenkaku_fix_allowed("f", "電話番号", "0311112222", "text"))
        self.assertFalse(fv.zenkaku_fix_allowed("f", "郵便番号", "2600003", "text"))

    def test_protected_value_shapes(self) -> None:
        self.assertFalse(fv.zenkaku_fix_allowed("f", "住所", "shida@torana.co.jp", "text"))
        self.assertFalse(fv.zenkaku_fix_allowed("f", "住所", "https://torana.co.jp", "text"))
        self.assertFalse(fv.zenkaku_fix_allowed("f", "住所", "043-123-4567", "text"))

    def test_empty_value_rejected(self) -> None:
        self.assertFalse(fv.zenkaku_fix_allowed("f", "住所", "", "text"))
