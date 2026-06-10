"""v15 §S — wizard continuation predicate, date/address helpers, newsletter opt-out."""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import form_validation as fv
from _outreach_core import submit_progress as sp


class TestWizardShouldContinue(unittest.TestCase):
    def test_success_keyword_stops(self) -> None:
        self.assertFalse(sp.wizard_should_continue("お問い合わせを受け付けました", 1))

    def test_form_gone_stops(self) -> None:
        self.assertFalse(sp.wizard_should_continue("確認ページ", 0))

    def test_input_step_continues(self) -> None:
        self.assertTrue(sp.wizard_should_continue("ステップ3: 詳細情報を入力してください", 1))

    def test_empty_page_stops(self) -> None:
        self.assertFalse(sp.wizard_should_continue("", 0))


class TestDefaultDateValue(unittest.TestCase):
    """§S acceptance 12 — 7 business days out, ISO format."""

    def test_from_monday(self) -> None:
        # Mon 2026-06-01 + 7 business days = Wed 2026-06-10
        self.assertEqual(fv.default_date_value(date(2026, 6, 1)), "2026-06-10")

    def test_from_friday_skips_weekend(self) -> None:
        # Fri 2026-06-05 + 7 business days = Tue 2026-06-16
        self.assertEqual(fv.default_date_value(date(2026, 6, 5)), "2026-06-16")

    def test_iso_shape(self) -> None:
        out = fv.default_date_value()
        self.assertRegex(out, r"^\d{4}-\d{2}-\d{2}$")


class TestSplitJpAddress(unittest.TestCase):
    """§S acceptance 12."""

    def test_tokyo_with_building(self) -> None:
        out = fv.split_jp_address("東京都千代田区丸の内1-1-1 サンプルビル7F")
        self.assertEqual(out["prefecture"], "東京都")
        self.assertEqual(out["city"], "千代田区")
        self.assertEqual(out["address_line"], "丸の内1-1-1")
        self.assertEqual(out["building"], "サンプルビル7F")

    def test_shi_ku_compound(self) -> None:
        out = fv.split_jp_address("千葉県千葉市中央区新町2-3")
        self.assertEqual(out["prefecture"], "千葉県")
        self.assertEqual(out["city"], "千葉市中央区")
        self.assertEqual(out["address_line"], "新町2-3")

    def test_fu_and_no_building(self) -> None:
        out = fv.split_jp_address("大阪府大阪市北区梅田1-2-3")
        self.assertEqual(out["prefecture"], "大阪府")
        self.assertEqual(out["city"], "大阪市北区")
        self.assertEqual(out["address_line"], "梅田1-2-3")
        self.assertEqual(out["building"], "")

    def test_empty(self) -> None:
        out = fv.split_jp_address("")
        self.assertEqual(out, {"prefecture": "", "city": "", "address_line": "", "building": ""})


class TestNewsletterOptOut(unittest.TestCase):
    """§S acceptance 12 — optional newsletter boxes must NOT be auto-checked."""

    def test_optional_newsletter_not_checked(self) -> None:
        for label in ("メルマガを希望する", "ニュースレターの配信を希望", "最新情報の案内を希望する"):
            with self.subTest(label=label):
                box = {"label": label, "required": False, "checked": False}
                self.assertFalse(sp.should_auto_check_checkbox(box))

    def test_required_newsletter_still_checked(self) -> None:
        # required gate — without it the form cannot submit at all
        box = {"label": "メルマガ配信に同意する", "required": True, "checked": False}
        self.assertTrue(sp.should_auto_check_checkbox(box))

    def test_agreement_still_checked(self) -> None:
        box = {"label": "個人情報の取扱いに同意する", "required": False, "checked": False}
        self.assertTrue(sp.should_auto_check_checkbox(box))

    def test_pick_checkboxes_excludes_newsletter(self) -> None:
        boxes = [
            {"label": "プライバシーポリシーに同意", "required": False, "checked": False},
            {"label": "メルマガを希望する", "required": False, "checked": False},
        ]
        picked = sp.pick_checkboxes_to_check(boxes)
        self.assertEqual(len(picked), 1)
        self.assertIn("プライバシー", picked[0]["label"])


class TestWizardLoopWiring(unittest.TestCase):
    """§S acceptance 11 — confirm flow stays a special case of the step loop."""

    def test_run_py_has_wizard_loop(self) -> None:
        run_py = Path(__file__).resolve().parent.parent.parent / "jp-form-outreach" / "run.py"
        text = run_py.read_text(encoding="utf-8")
        self.assertIn("MAX_FORM_STEPS = 4", text)
        self.assertIn("_advance_wizard_steps", text)
        self.assertIn("wizard_too_deep", text)


if __name__ == "__main__":
    unittest.main()
