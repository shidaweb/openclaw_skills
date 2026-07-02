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

    def test_required_checkbox_group_checks_only_safe_other_option(self) -> None:
        boxes = [
            {
                "name": "service01", "label": "イベント制作", "value": "event",
                "group_label": "ご希望のサービス 必須", "required": True,
                "checked": False, "disabled": False,
            },
            {
                "name": "service02", "label": "採用支援", "value": "recruit",
                "group_label": "ご希望のサービス 必須", "required": True,
                "checked": False, "disabled": False,
            },
            {
                "name": "service08", "label": "その他", "value": "other",
                "group_label": "ご希望のサービス 必須", "required": True,
                "checked": False, "disabled": False,
            },
        ]
        picked = sp.pick_checkboxes_to_check(boxes)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["name"], "service08")

    def test_required_checkbox_group_does_not_invent_unsafe_preference(self) -> None:
        boxes = [
            {
                "name": "service01", "label": "イベント制作", "value": "event",
                "group_label": "ご希望のサービス 必須", "required": True,
                "checked": False, "disabled": False,
            },
            {
                "name": "service02", "label": "採用支援", "value": "recruit",
                "group_label": "ご希望のサービス 必須", "required": True,
                "checked": False, "disabled": False,
            },
        ]
        self.assertEqual(sp.pick_checkboxes_to_check(boxes), [])

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

    def test_pick_select_gate_actions_falls_back_to_sonota_when_no_b2b_option(self) -> None:
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
        self.assertEqual(actions, [{"name": "contact_category", "value": "その他のお問い合わせ"}])

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

    def test_rank_submit_candidates_prioritizes_in_form_submit_type(self) -> None:
        candidates = [
            {"text": "こちら", "tag": "a", "href": "/x", "is_submit_type": False, "in_form": False},
            {"text": "こちら", "tag": "a", "href": "/y", "is_submit_type": False, "in_form": False},
            {"text": "", "tag": "input", "type": "submit", "is_submit_type": True, "in_form": True},
        ]
        ranked = sp.rank_submit_candidates(candidates, phase="final")
        self.assertTrue(ranked)
        self.assertTrue(ranked[0]["is_submit_type"])
        self.assertTrue(ranked[0]["in_form"])

    def test_rank_submit_candidates_returns_empty_when_all_noise(self) -> None:
        candidates = [
            {"text": "こちら", "tag": "a", "href": "/x", "is_submit_type": False, "in_form": False},
            {"text": "こちら", "tag": "a", "href": "/y", "is_submit_type": False, "in_form": False},
            {"text": "プライバシー", "tag": "a", "href": "/privacy", "is_submit_type": False, "in_form": False},
        ]
        ranked = sp.rank_submit_candidates(candidates, phase="final")
        self.assertEqual(ranked, [])

    def test_pick_route_radio_action_prefers_b2b_option(self) -> None:
        groups = [
            {
                "name": "route",
                "label": "お問い合わせ対象",
                "selected": False,
                "options": [
                    {"label": "お客様", "value": "customer", "checked": False},
                    {"label": "法人（新規提案）", "value": "corp", "checked": False},
                ],
            }
        ]
        act = sp.pick_route_radio_action(groups)
        self.assertIsNotNone(act)
        self.assertEqual(act["name"], "route")
        self.assertIn("法人", act["value"])

    def test_summarize_remaining_submit_gates_lists_unresolved(self) -> None:
        out = sp.summarize_remaining_submit_gates(
            [{"name": "agree", "label": "個人情報の取扱いに同意", "checked": False, "required": True}],
            [
                {
                    "name": "route",
                    "selected": False,
                    "options": [
                        {"label": "お客様", "value": "customer"},
                        {"label": "法人", "value": "corp"},
                    ],
                }
            ],
            [
                {
                    "name": "kind",
                    "selected": False,
                    "options": [
                        {"label": "選択してください", "value": ""},
                        {"label": "お取引・ご提案", "value": "biz"},
                    ],
                }
            ],
        )
        self.assertGreater(out["total"], 0)
        self.assertTrue(out["checkboxes"])
        self.assertTrue(out["radios"])
        self.assertTrue(out["selects"])

    def test_choose_b2b_option_falls_back_to_sonota(self) -> None:
        options = [
            {"label": "選択してください", "value": "", "selected": True, "disabled": False},
            {"label": "個人のお客様", "value": "personal", "selected": False, "disabled": False},
            {"label": "その他", "value": "other", "selected": False, "disabled": False},
        ]
        picked = sp.choose_b2b_option(options)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["value"], "その他")
        self.assertEqual(picked["confidence"], "low")


# v30 §WS-A — B2B scoring is now externally tunable per brief / persona.


class TestNormalizeOptionsV31(unittest.TestCase):
    """v31 §WS3a — compact {t, v} option dicts from the enrich extractor."""

    def test_tv_dicts_normalize(self) -> None:
        out = sp._normalize_options([
            {"t": "法人のお客様", "v": "corp"},
            {"t": "選択してください", "v": ""},
        ])
        self.assertEqual(out[0]["label"], "法人のお客様")
        self.assertEqual(out[0]["value"], "corp")
        # empty value falls back to the display text
        self.assertEqual(out[1]["value"], "選択してください")

    def test_legacy_shapes_still_work(self) -> None:
        out = sp._normalize_options([
            "その他",
            {"label": "A", "value": "a", "selected": True},
            {"text": "B"},
        ])
        self.assertEqual(out[0], {"label": "その他", "value": "その他",
                                  "selected": False, "disabled": False})
        self.assertTrue(out[1]["selected"])
        self.assertEqual(out[2]["label"], "B")

    def test_choose_b2b_option_accepts_tv_dicts(self) -> None:
        picked = sp.choose_b2b_option([
            {"t": "個人のお客様", "v": "personal"},
            {"t": "法人のお客様", "v": "corporate"},
        ])
        self.assertIsNotNone(picked)
        self.assertEqual(picked["value"], "法人のお客様")


class TestB2BScoringOverlay(unittest.TestCase):
    """The hard-coded +6/+3/-8/-4/+1 weights are now defaults that a brief or
    persona yaml can override via ``b2b_scoring`` (or ``send.b2b_scoring``).
    A typo or unknown key must be silently ignored so a config glitch never
    flips the picker's behaviour."""

    def test_defaults_match_legacy_weights(self) -> None:
        # The default constants reproduce the pre-refactor behaviour exactly.
        self.assertEqual(sp.B2B_DEFAULT_SCORES["strong_prefer"], 6)
        self.assertEqual(sp.B2B_DEFAULT_SCORES["weak_prefer"], 3)
        self.assertEqual(sp.B2B_DEFAULT_SCORES["strong_avoid"], -8)
        self.assertEqual(sp.B2B_DEFAULT_SCORES["weak_avoid"], -4)
        self.assertEqual(sp.B2B_DEFAULT_SCORES["sonota_bonus"], 1)
        self.assertEqual(sp.B2B_DEFAULT_SCORES["min_top_score"], 2)
        self.assertEqual(sp.B2B_DEFAULT_SCORES["high_confidence_floor"], 5)
        self.assertEqual(sp.B2B_DEFAULT_SCORES["confidence_gap"], 1)

    def test_b2b_scores_from_config_returns_defaults_when_no_overlay(self) -> None:
        merged = sp.b2b_scores_from_config({})
        self.assertEqual(merged, sp.B2B_DEFAULT_SCORES)

    def test_overlay_merges_only_known_keys(self) -> None:
        merged = sp.b2b_scores_from_config(
            {"b2b_scoring": {"strong_prefer": 9, "bogus_key": 99}},
        )
        self.assertEqual(merged["strong_prefer"], 9)
        # All other keys keep defaults; unknown key is dropped silently.
        self.assertEqual(merged["weak_prefer"], 3)
        self.assertNotIn("bogus_key", merged)

    def test_overlay_under_send_block(self) -> None:
        merged = sp.b2b_scores_from_config(
            {"send": {"b2b_scoring": {"sonota_bonus": 0}}},
        )
        self.assertEqual(merged["sonota_bonus"], 0)
        self.assertEqual(merged["strong_prefer"], 6)

    def test_overlay_rejects_non_numeric_and_bool(self) -> None:
        # Bool is a subclass of int but should NOT be accepted (a yaml `true`
        # would otherwise become 1 silently).
        merged = sp.b2b_scores_from_config(
            {"b2b_scoring": {"strong_prefer": True, "weak_prefer": "lots"}},
        )
        self.assertEqual(merged["strong_prefer"], 6)
        self.assertEqual(merged["weak_prefer"], 3)

    def test_overlay_changes_confidence_threshold(self) -> None:
        # A strong-prefer match ("法人のお客様" via _STRONG_PREFER_RE) scores 6
        # against the default high_confidence_floor=5 → "high". Raising the
        # floor via overlay demotes the picker to "low" without changing
        # which option wins.
        single = [
            {"label": "法人のお客様", "value": "biz",
             "selected": False, "disabled": False},
        ]
        legacy = sp.choose_b2b_option(single)
        self.assertEqual(legacy["confidence"], "high")
        # Raise the floor via overlay → same picker, but now "low".
        sharp = sp.b2b_scores_from_config(
            {"b2b_scoring": {"high_confidence_floor": 10}},
        )
        demoted = sp.choose_b2b_option(single, scores=sharp)
        self.assertEqual(demoted["confidence"], "low")
        # The picked VALUE is unchanged — overlay only affects confidence.
        self.assertEqual(demoted["value"], legacy["value"])

    def test_concept_separated_pickers_are_independent(self) -> None:
        # Placeholder hook to keep the concept-separation tests visually
        # next to their B2B-scoring siblings; the real tests live in
        # TestCheckboxConceptSeparation below.
        self.assertTrue(hasattr(sp, "pick_consent_actions"))
        self.assertTrue(hasattr(sp, "pick_inquiry_type_actions"))
        self.assertTrue(hasattr(sp, "pick_required_checkbox_actions"))

    def test_overlay_min_top_score_rejects_default_winner(self) -> None:
        # A weak_prefer-only option scores +3, above default min_top_score=2.
        options = [
            {"label": "新規お取引のご相談", "value": "biz",
             "selected": False, "disabled": False},
        ]
        default_pick = sp.choose_b2b_option(options)
        self.assertIsNotNone(default_pick)

        # Raise the floor to 7 — the +3 option is now below the bar and the
        # picker reports no winner (caller should escalate / human review).
        strict = sp.b2b_scores_from_config(
            {"b2b_scoring": {"min_top_score": 7}},
        )
        strict_pick = sp.choose_b2b_option(options, scores=strict)
        self.assertIsNone(strict_pick)


# v30 §WS-A — concept-separated checkbox pickers.
#
# pick_checkboxes_to_check used to mix three concepts (consent / inquiry-type
# group / standalone required). Each concept now has its own picker so a
# brief-specific tweak to one rule never touches the others.


class TestCheckboxConceptSeparation(unittest.TestCase):
    def _consent(self) -> dict:
        return {
            "label": "個人情報の取扱いに同意します",
            "name": "agreement",
            "checked": False,
            "required": True,
        }

    def _inquiry_group(self, label: str) -> dict:
        return {
            "label": label,
            "name": "inquiry_topic",
            "group_label": "お問い合わせ項目（必須）",
            "checked": False,
            "required": True,
        }

    def _standalone_required(self) -> dict:
        return {
            "label": "メルマガを希望しない",
            "name": "no_newsletter_request",
            "checked": False,
            "required": True,
        }

    def test_consent_picker_returns_only_consent(self) -> None:
        boxes = [
            self._consent(),
            self._inquiry_group("ビジネスのご相談"),
            self._standalone_required(),
        ]
        out = sp.pick_consent_actions(boxes)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "agreement")

    def test_consent_picker_skips_newsletter_even_if_required(self) -> None:
        # Newsletter / 配信 opt-ins must not be auto-checked, ever.
        newsletter = {
            "label": "ニュースレターを受け取る",
            "checked": False,
            "required": True,
        }
        self.assertEqual(sp.pick_consent_actions([newsletter]), [])

    def test_consent_picker_skips_already_checked(self) -> None:
        already = {**self._consent(), "checked": True}
        self.assertEqual(sp.pick_consent_actions([already]), [])

    def test_inquiry_type_picker_returns_one_per_group(self) -> None:
        # Two options in the same group → exactly one should be picked.
        boxes = [
            self._inquiry_group("法人のお取引について"),
            self._inquiry_group("採用に関するお問い合わせ"),
        ]
        out = sp.pick_inquiry_type_actions(boxes)
        self.assertEqual(len(out), 1)
        # The B2B picker prefers 法人 / お取引 over 採用.
        self.assertIn("お取引", out[0]["label"])

    def test_inquiry_type_picker_ignores_consent(self) -> None:
        boxes = [self._consent(), self._inquiry_group("ビジネス")]
        out = sp.pick_inquiry_type_actions(boxes)
        # Consent never appears in the inquiry-type bucket.
        names = [b["name"] for b in out]
        self.assertNotIn("agreement", names)

    def test_inquiry_type_picker_single_option_group_passes_through(self) -> None:
        # A group with exactly one option is committed as-is — there is no
        # multi-option ambiguity to resolve.
        single = [self._inquiry_group("ご提案")]
        out = sp.pick_inquiry_type_actions(single)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["label"], "ご提案")

    def test_required_picker_returns_only_standalone_required(self) -> None:
        boxes = [
            self._consent(),
            self._inquiry_group("ビジネス"),
            self._standalone_required(),
        ]
        out = sp.pick_required_checkbox_actions(boxes)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "no_newsletter_request")

    def test_facade_preserves_legacy_behavior(self) -> None:
        # pick_checkboxes_to_check now concatenates the three concept pickers;
        # the output set must match the historical behaviour for a typical
        # mixed-form input. We sort by name for a stable comparison.
        boxes = [
            self._consent(),
            self._inquiry_group("法人のお取引について"),
            self._inquiry_group("採用に関するお問い合わせ"),
            self._standalone_required(),
        ]
        out = sp.pick_checkboxes_to_check(boxes)
        names = sorted(b["name"] for b in out)
        self.assertEqual(
            names,
            ["agreement", "inquiry_topic", "no_newsletter_request"],
        )

    def test_facade_handles_non_list_input_defensively(self) -> None:
        for bad in (None, {}, "not a list", 0):
            self.assertEqual(sp.pick_checkboxes_to_check(bad), [])
            self.assertEqual(sp.pick_consent_actions(bad), [])
            self.assertEqual(sp.pick_inquiry_type_actions(bad), [])
            self.assertEqual(sp.pick_required_checkbox_actions(bad), [])


if __name__ == "__main__":
    unittest.main()
