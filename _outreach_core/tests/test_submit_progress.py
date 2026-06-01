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


if __name__ == "__main__":
    unittest.main()
