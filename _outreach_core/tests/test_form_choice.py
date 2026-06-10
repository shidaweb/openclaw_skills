"""Tests for flexible select/radio choice + name splitting (v16).

Covers:
  - name_part compound-word fix (氏名/お名前/会社名 must NOT be "mei")
  - split_jp_name / sender_name_parts (space-aware, explicit keys win)
  - choose_option_for_label (label-aware select/radio answers)
  - pick_select_gate_actions / pick_radio_gate_actions with sender context
"""

from __future__ import annotations

import unittest

from _outreach_core import form_validation as fv
from _outreach_core import submit_progress as sp


def _opts(*labels: str, selected: str | None = None) -> list[dict]:
    return [
        {"label": l, "value": l, "selected": l == selected, "disabled": False}
        for l in labels
    ]


SENDER = {
    "name": "志田典道",
    "name_kana": "シダノリミツ",
    "name_furigana": "しだのりみつ",
    "prefecture": "千葉県",
    "industry_keywords": "サービス 小売 EC",
}


class TestNamePartFix(unittest.TestCase):
    def test_full_name_labels_are_not_mei(self) -> None:
        # Regression: these were misclassified as "mei", causing the kana guard
        # to enforce the given-name half into FULL-name furigana fields.
        for label in ("氏名", "お名前", "名前", "お名前（フリガナ）", "フルネーム"):
            self.assertIsNone(fv.name_part(label), label)

    def test_non_name_labels_are_none(self) -> None:
        for label in ("会社名", "法人名", "件名", "題名", "部署名", "担当者名", "旧姓"):
            self.assertIsNone(fv.name_part(label), label)

    def test_sei_variants(self) -> None:
        for label in ("姓", "フリガナ（姓）", "セイ", "苗字", "名字", "Last Name", "せい"):
            self.assertEqual(fv.name_part(label), "sei", label)

    def test_mei_variants(self) -> None:
        for label in ("名", "フリガナ（名）", "メイ", "First Name", "めい"):
            self.assertEqual(fv.name_part(label), "mei", label)


class TestNameSplit(unittest.TestCase):
    def test_space_split_wins(self) -> None:
        self.assertEqual(fv.split_jp_name("五十嵐 剛"), ("五十嵐", "剛"))
        self.assertEqual(fv.split_jp_name("山田　太郎"), ("山田", "太郎"))

    def test_legacy_two_char_fallback(self) -> None:
        self.assertEqual(fv.split_jp_name("志田典道"), ("志田", "典道"))

    def test_empty(self) -> None:
        self.assertEqual(fv.split_jp_name(""), ("", ""))

    def test_sender_name_parts_explicit_keys_win(self) -> None:
        s = dict(SENDER, name="五十嵐剛", name_sei="五十嵐", name_mei="剛")
        parts = fv.sender_name_parts(s)
        self.assertEqual(parts["name_sei"], "五十嵐")
        self.assertEqual(parts["name_mei"], "剛")

    def test_sender_name_parts_derives_hiragana_from_kana(self) -> None:
        s = {"name": "志田典道", "name_kana": "シダ ノリミツ"}
        parts = fv.sender_name_parts(s)
        self.assertEqual(parts["name_kana_sei"], "シダ")
        self.assertEqual(parts["name_furigana_sei"], "しだ")
        self.assertEqual(parts["name_furigana_mei"], "のりみつ")


class TestChooseOptionForLabel(unittest.TestCase):
    def test_prefecture_matches_sender(self) -> None:
        got = fv.choose_option_for_label(
            "都道府県", _opts("選択してください", "東京都", "千葉県", "大阪府"), SENDER
        )
        self.assertEqual(got["value"], "千葉県")

    def test_prefecture_without_sender_is_none(self) -> None:
        self.assertIsNone(
            fv.choose_option_for_label("都道府県", _opts("東京都", "千葉県"), {})
        )

    def test_employee_band_prefers_smallest(self) -> None:
        got = fv.choose_option_for_label(
            "従業員数", _opts("選択してください", "1〜10名", "11〜50名", "51名以上"), SENDER
        )
        self.assertEqual(got["value"], "1〜10名")

    def test_referral_prefers_sonota(self) -> None:
        got = fv.choose_option_for_label(
            "当社を知ったきっかけ", _opts("展示会", "紹介", "その他"), SENDER
        )
        self.assertEqual(got["value"], "その他")

    def test_budget_prefers_undecided(self) -> None:
        got = fv.choose_option_for_label(
            "ご予算", _opts("〜50万円", "50〜100万円", "未定"), SENDER
        )
        self.assertEqual(got["value"], "未定")

    def test_contact_method_prefers_email(self) -> None:
        got = fv.choose_option_for_label(
            "ご希望の連絡方法", _opts("電話", "メール", "どちらでも"), SENDER
        )
        self.assertEqual(got["value"], "メール")

    def test_gender_without_config_is_none(self) -> None:
        self.assertIsNone(
            fv.choose_option_for_label("性別", _opts("男性", "女性"), SENDER)
        )

    def test_gender_with_config(self) -> None:
        got = fv.choose_option_for_label(
            "性別", _opts("男性", "女性"), dict(SENDER, gender="男性")
        )
        self.assertEqual(got["value"], "男性")

    def test_industry_keyword_match(self) -> None:
        got = fv.choose_option_for_label(
            "業種", _opts("製造業", "小売業", "金融"), SENDER
        )
        self.assertEqual(got["value"], "小売業")

    def test_inquiry_type_falls_back_to_b2b_scorer(self) -> None:
        got = fv.choose_option_for_label(
            "お問い合わせ種別",
            _opts("商品について", "採用について", "法人のお客様", "その他"),
            SENDER,
        )
        self.assertEqual(got["value"], "法人のお客様")

    def test_unknown_label_generic_sonota(self) -> None:
        got = fv.choose_option_for_label(
            "ご希望の時間帯", _opts("午前", "午後", "その他"), SENDER
        )
        self.assertEqual(got["value"], "その他")

    def test_placeholder_and_selected_skipped(self) -> None:
        got = fv.choose_option_for_label(
            "都道府県",
            _opts("選択してください", "千葉県", selected="千葉県"),
            SENDER,
        )
        self.assertIsNone(got)  # already selected → nothing to do


class TestGateActionsWithSender(unittest.TestCase):
    def test_select_gate_known_label_not_required(self) -> None:
        groups = [{
            "name": "pref", "label": "都道府県", "required": False,
            "selected": False,
            "options": _opts("選択してください", "東京都", "千葉県"),
        }]
        actions = sp.pick_select_gate_actions(groups, sender=SENDER)
        self.assertEqual(actions, [{"name": "pref", "value": "千葉県"}])

    def test_select_gate_unknown_optional_still_skipped(self) -> None:
        groups = [{
            "name": "color", "label": "好きな色", "required": False,
            "selected": False, "options": _opts("赤", "青"),
        }]
        self.assertEqual(sp.pick_select_gate_actions(groups, sender=SENDER), [])

    def test_radio_gate_contact_method(self) -> None:
        groups = [{
            "name": "how", "label": "ご希望の連絡方法", "required": True,
            "selected": False,
            "options": [
                {"label": "電話", "value": "tel", "checked": False},
                {"label": "メール", "value": "mail", "checked": False},
            ],
        }]
        actions = sp.pick_radio_gate_actions(groups, sender=SENDER)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["value"], "メール")

    def test_radio_gate_b2b_fallback_still_works(self) -> None:
        groups = [{
            "name": "kind", "label": "お問い合わせ種別", "required": True,
            "selected": False,
            "options": [
                {"label": "個人のお客様", "value": "p", "checked": False},
                {"label": "法人のお客様", "value": "c", "checked": False},
            ],
        }]
        actions = sp.pick_radio_gate_actions(groups, sender=SENDER)
        self.assertEqual(actions[0]["value"], "法人のお客様")


class TestKanaCorrectionWithFix(unittest.TestCase):
    def test_full_name_furigana_not_forced_to_mei(self) -> None:
        # 「お名前（フリガナ）」 holds the full reading — must NOT be "corrected"
        # to the mei half (the pre-fix behavior).
        got = fv.kana_field_correction("お名前（フリガナ）", "シダノリミツ", SENDER)
        self.assertIsNone(got)

    def test_split_field_still_enforced(self) -> None:
        got = fv.kana_field_correction("フリガナ（名）", "シダノリミツ", SENDER)
        self.assertEqual(got, "ノリミツ")


if __name__ == "__main__":
    unittest.main()
