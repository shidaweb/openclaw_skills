from __future__ import annotations

import unittest

from _outreach_core import submit_progress as sp


class TestSubmitProgress(unittest.TestCase):
    def test_agreement_label_detects_privacy_phrase(self) -> None:
        self.assertTrue(sp.is_agreement_label("「個人情報の取扱いについて」に同意する"))

    def test_agreement_label_detects_policy_terms(self) -> None:
        self.assertTrue(sp.is_agreement_label("プライバシーポリシーに同意"))
        self.assertTrue(sp.is_agreement_label("利用規約に同意します"))

    def test_should_auto_check_required_even_without_label(self) -> None:
        box = {"label": "", "required": True, "checked": False}
        self.assertTrue(sp.should_auto_check_checkbox(box))

    def test_already_checked_is_not_target(self) -> None:
        box = {"label": "個人情報の取扱いに同意", "required": True, "checked": True}
        self.assertFalse(sp.should_auto_check_checkbox(box))

    def test_pick_checkboxes_filters_unchecked_required_or_agreement(self) -> None:
        boxes = [
            {"name": "a", "label": "個人情報の取扱いに同意する", "required": False, "checked": False},
            {"name": "b", "label": "メルマガを受け取る", "required": False, "checked": False},
            {"name": "c", "label": "", "required": True, "checked": False},
            {"name": "d", "label": "利用規約", "required": True, "checked": True},
        ]
        picked = sp.pick_checkboxes_to_check(boxes)
        self.assertEqual([b.get("name") for b in picked], ["a", "c"])

    def test_pick_radio_gate_actions_prefers_business_option(self) -> None:
        groups = [
            {
                "name": "contact_kind",
                "label": "お問い合わせ種別",
                "required": True,
                "selected": False,
                "options": [
                    {"label": "個人のお問い合わせ", "value": "personal", "checked": False},
                    {"label": "法人のお問い合わせ", "value": "corp", "checked": False},
                ],
            }
        ]
        actions = sp.pick_radio_gate_actions(groups)
        self.assertEqual(actions, [{"name": "contact_kind", "value": "法人のお問い合わせ"}])

    def test_pick_radio_gate_actions_skips_selected_group(self) -> None:
        groups = [
            {
                "name": "kind",
                "label": "お問い合わせ種別",
                "required": True,
                "selected": True,
                "options": [{"label": "法人", "value": "corp", "checked": True}],
            }
        ]
        self.assertEqual(sp.pick_radio_gate_actions(groups), [])

    def test_pick_radio_gate_actions_uses_other_when_safe(self) -> None:
        groups = [
            {
                "name": "topic",
                "label": "カテゴリ",
                "required": True,
                "selected": False,
                "options": [
                    {"label": "その他", "value": "other", "checked": False},
                    {"label": "採用", "value": "recruit", "checked": False},
                ],
            }
        ]
        actions = sp.pick_radio_gate_actions(groups)
        self.assertEqual(actions, [{"name": "topic", "value": "その他"}])

    def test_pick_select_gate_actions_prefers_business_proposal(self) -> None:
        groups = [
            {
                "name": "contact_category",
                "label": "お問い合わせ区分",
                "required": True,
                "selected": False,
                "options": [
                    {"label": "ー以下から選択してくださいー", "value": "", "selected": True, "disabled": False},
                    {"label": "採用に関するお問い合わせ", "value": "recruit", "selected": False, "disabled": False},
                    {"label": "広報、IRなどのお問い合わせ", "value": "ir", "selected": False, "disabled": False},
                    {"label": "業務用食材、備品、消耗品のご提案について", "value": "proposal", "selected": False, "disabled": False},
                    {"label": "その他のお問い合わせ", "value": "other", "selected": False, "disabled": False},
                ],
            }
        ]
        actions = sp.pick_select_gate_actions(groups)
        self.assertEqual(actions, [{"name": "contact_category", "value": "業務用食材、備品、消耗品のご提案について"}])

    def test_pick_select_gate_actions_returns_none_when_no_b2b_option(self) -> None:
        groups = [
            {
                "name": "contact_category",
                "label": "お問い合わせ区分",
                "required": True,
                "selected": False,
                "options": [
                    {"label": "ー以下から選択してくださいー", "value": "", "selected": True, "disabled": False},
                    {"label": "採用に関するお問い合わせ", "value": "recruit", "selected": False, "disabled": False},
                    {"label": "その他のお問い合わせ", "value": "other", "selected": False, "disabled": False},
                ],
            }
        ]
        actions = sp.pick_select_gate_actions(groups)
        self.assertEqual(actions, [])

    def test_pick_select_gate_actions_skips_selected_group(self) -> None:
        groups = [
            {
                "name": "contact_category",
                "label": "お問い合わせ区分",
                "required": True,
                "selected": True,
                "options": [
                    {"label": "業務用食材、備品、消耗品のご提案について", "value": "proposal", "selected": True, "disabled": False},
                ],
            }
        ]
        self.assertEqual(sp.pick_select_gate_actions(groups), [])

    def test_validate_choice_rejects_placeholder_and_missing(self) -> None:
        options = [
            {"label": "ー以下から選択してくださいー", "value": "", "selected": True, "disabled": False},
            {"label": "法人のお問い合わせ", "value": "corp", "selected": False, "disabled": False},
        ]
        self.assertTrue(sp.validate_choice(options, "法人のお問い合わせ"))
        self.assertFalse(sp.validate_choice(options, "ー以下から選択してくださいー"))
        self.assertFalse(sp.validate_choice(options, "存在しない選択肢"))

    def test_is_inquiry_type_field_by_name_or_label(self) -> None:
        self.assertTrue(sp.is_inquiry_type_field({"name": "contact_subject", "label": "項目"}))
        self.assertTrue(sp.is_inquiry_type_field({"name": "foo", "label": "お問い合わせ区分"}))
        self.assertFalse(sp.is_inquiry_type_field({"name": "email", "label": "メールアドレス"}))

    def test_choose_b2b_option_confidence_and_placeholder_exclusion(self) -> None:
        options = [
            {"label": "選択してください", "value": "", "selected": True, "disabled": False},
            {"label": "個人のお客様", "value": "personal", "selected": False, "disabled": False},
            {"label": "採用に関するお問い合わせ", "value": "recruit", "selected": False, "disabled": False},
            {"label": "お取引・ご提案", "value": "biz", "selected": False, "disabled": False},
        ]
        picked = sp.choose_b2b_option(options)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["value"], "お取引・ご提案")
        self.assertIn(picked["confidence"], ("high", "low"))


if __name__ == "__main__":
    unittest.main()
