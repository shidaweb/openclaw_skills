from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _load_run_module():
    if "yaml" not in sys.modules:
        sys.modules["yaml"] = types.SimpleNamespace(safe_dump=lambda obj, **kwargs: "{}\n")
    root = Path(__file__).resolve().parent.parent.parent
    run_path = root / "jp-form-outreach" / "run.py"
    spec = importlib.util.spec_from_file_location("jp_form_outreach_runtime_guards", run_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class TestWrongFormRuntimeGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.run_mod = _load_run_module()

    def test_incidental_reservation_warning_does_not_abort_contact_form(self) -> None:
        diag = {
            "warnings": [
                "time_01〜time_05 は予約日時フィールドと推定されるが、任意のためスキップ"
            ],
            "skipped": ["time_01", "time_02", "time_03"],
        }
        self.assertIsNone(self.run_mod._detect_wrong_form_type(diag))

    def test_incidental_recruitment_word_does_not_abort_contact_form(self) -> None:
        diag = {"warnings": ["ページ内ナビゲーションに採用情報へのリンクあり"], "skipped": []}
        self.assertIsNone(self.run_mod._detect_wrong_form_type(diag))

    def test_explicit_dedicated_recruitment_form_still_aborts(self) -> None:
        diag = {"warnings": ["採用専用フォームのためB2B提案には不適切"], "skipped": []}
        self.assertIsNotNone(self.run_mod._detect_wrong_form_type(diag))

    def test_checkbox_script_does_not_set_true_then_toggle_false(self) -> None:
        script = self.run_mod._CHECK_BY_NAME_JS
        self.assertNotIn("cb.checked = true", script)
        self.assertLess(script.index("cb.click()"), script.index("setter.call(cb, true)"))

    def test_submit_clicker_rejects_passive_submit_wrappers(self) -> None:
        self.assertIn("if (!isNativeControl(b)) continue", self.run_mod._CLICK_BUTTON_BY_TEXT_JS)


if __name__ == "__main__":
    unittest.main()
