from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_run_module():
    if "yaml" not in sys.modules:
        yaml_stub = types.SimpleNamespace(
            safe_dump=lambda obj, **kwargs: "{}\n",
        )
        sys.modules["yaml"] = yaml_stub
    root = Path(__file__).resolve().parent.parent.parent
    run_path = root / "jp-form-outreach" / "run.py"
    spec = importlib.util.spec_from_file_location("jp_form_outreach_run", run_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class TestInquiryTypeSelect(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.run_mod = _load_run_module()

    def test_override_has_priority(self) -> None:
        target = {
            "id": 1,
            "field_map_overrides": {"category_select": "法人のお問い合わせ"},
            "form_fields": {
                "selects": [
                    {
                        "name": "contact_kind",
                        "label": "お問い合わせ区分",
                        "required": True,
                        "options": ["選択してください", "法人のお問い合わせ", "採用"],
                    }
                ]
            },
        }
        out = self.run_mod._ensure_inquiry_type_action(target, {"fields": []}, stage="send")
        self.assertEqual(out["selected"], 0)
        self.assertEqual(out["src"], "override")

    def test_invalid_llm_choice_falls_back_to_b2b_option(self) -> None:
        target = {
            "id": 1,
            "name": "Acme",
            "form_fields": {
                "selects": [
                    {
                        "name": "contact_kind",
                        "label": "お問い合わせ区分",
                        "required": True,
                        "options": [
                            "選択してください",
                            "個人のお客様",
                            "採用について",
                            "お取引・ご提案",
                        ],
                    }
                ]
            },
        }
        plan = {
            "fields": [
                {
                    "name": "contact_kind",
                    "action": "select_option",
                    "value": "選択してください",
                }
            ]
        }
        with (
            patch.object(self.run_mod, "_apply_field_action", return_value={"ok": True}),
            patch.object(self.run_mod, "_emit_event"),
        ):
            out = self.run_mod._ensure_inquiry_type_action(target, plan, stage="send")
        self.assertEqual(out["selected"], 1)
        self.assertEqual(plan["fields"][0]["value"], "お取引・ご提案")

    def test_no_b2b_option_marks_no_b2b(self) -> None:
        target = {
            "id": 1,
            "name": "Acme",
            "form_fields": {
                "selects": [
                    {
                        "name": "contact_kind",
                        "label": "お問い合わせ区分",
                        "required": True,
                        "options": [
                            "選択してください",
                            "個人のお客様",
                            "採用について",
                            "お客様相談室",
                        ],
                    }
                ]
            },
        }
        with (
            patch.object(self.run_mod, "_apply_field_action", return_value={"ok": True}),
            patch.object(self.run_mod, "_emit_event"),
        ):
            out = self.run_mod._ensure_inquiry_type_action(target, {"fields": []}, stage="send")
        self.assertEqual(out["selected"], 0)
        self.assertTrue(out["no_b2b"])

    def test_fill_plan_checkbox_falls_back_to_label(self) -> None:
        plan = {
            "fields": [],
            "checkboxes_to_check": [{"name": "", "label": "利用規約に同意"}],
        }
        with (
            patch.object(self.run_mod, "_check_by_name", return_value={"ok": False, "reason": "not found"}),
            patch.object(self.run_mod, "_check_by_label", return_value={"ok": True}),
        ):
            diag = self.run_mod.fill_form_with_plan(plan, "body", target=None, evaluate_fn=lambda _js: {})
        self.assertTrue(any(x.startswith("checkbox:") for x in diag.get("filled") or []))

    def test_apply_plan_entry_accepts_selector_only_field(self) -> None:
        diag = {"filled": [], "errors": [], "skipped": []}
        entry = {
            "name": "",
            "selector": "form > input[type='text']",
            "action": "set_text",
            "value": "abc",
        }
        with patch.object(self.run_mod, "_apply_field_action", return_value={"ok": True, "value": "abc"}) as ap:
            ok = self.run_mod._apply_plan_entry(entry, "body", diag)
        self.assertTrue(ok)
        self.assertTrue(ap.called)

    def test_llm_click_submit_prefers_selector_click(self) -> None:
        buttons = [{"text": "入力内容を確認する", "selector": "button.confirm"}]
        with (
            patch.object(
                self.run_mod,
                "_llm_pick_final_submit",
                return_value={"text": "入力内容を確認する", "selector": "button.confirm"},
            ),
            patch.object(
                self.run_mod,
                "_click_by_selector",
                return_value={"clicked": True, "text": "入力内容を確認する"},
            ) as cs,
            patch.object(self.run_mod, "_click_by_exact_text", return_value=None) as ct,
        ):
            out = self.run_mod._llm_click_submit_candidate(buttons, {}, phase="final")
        self.assertTrue(out and out.get("clicked"))
        self.assertTrue(cs.called)
        self.assertFalse(ct.called)

    def test_inquiry_type_no_b2b_flags_true_when_llm_or_fallback_true(self) -> None:
        inquiry_fields = [
            {
                "name": "kind",
                "kind": "select_option",
                "options": [
                    {"label": "個人のお客様", "value": "personal"},
                    {"label": "採用", "value": "recruit"},
                ],
            }
        ]
        llm, fallback, no_b2b = self.run_mod._inquiry_type_no_b2b_flags(
            inquiry_fields,
            {"inquiry_type_no_b2b": True},
        )
        self.assertTrue(llm)
        self.assertTrue(no_b2b)
        self.assertTrue(fallback)


if __name__ == "__main__":
    unittest.main()
