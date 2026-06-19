"""
v26 regression tests — the 2026-06-13 maruman / wacoal_store / actus failures.

1. maruman — the page defines its OWN global confirm(form) that validates and
   submits; our dialog shim clobbered it (log showed confirm receiving an
   HTMLFormElement) → submit silently dead. Shim must delegate to page-defined
   confirm/alert and only replace NATIVE dialogs.
2. wacoal_store — server bounced the form back with 「住所（番地）は全角で
   入力してください」; state stayed "input" (no known error keyword) so no fixer
   ran. Needs: zenkaku error parsing + ASCII→全角 conversion + a silent-bounce
   rescue round in the submission loop.
3. actus — submit cascade clicked 「変更する」(name=submitBack): concatenated
   button text broke anchored patterns and r"submit" matched the name attr.
   Click JS must match attributes separately and deny back/modify controls.
"""

from __future__ import annotations

import unittest

from _outreach_core import form_validation as fv
from _outreach_core import send_state


class TestDialogShimDelegation(unittest.TestCase):
    """String pins for the in-page JS (no JS runtime in pytest)."""

    def test_shim_checks_for_native_confirm(self) -> None:
        js = send_state.DIALOG_AUTOACCEPT_JS
        self.assertIn("native code", js)
        self.assertIn("orig.apply", js)  # page-defined confirm is delegated

    def test_shim_still_logs_and_disarms_beforeunload(self) -> None:
        js = send_state.DIALOG_AUTOACCEPT_JS
        self.assertIn("__ocDialogLog", js)
        self.assertIn("onbeforeunload", js)


class TestZenkakuConversion(unittest.TestCase):
    def test_wacoal_banchi(self) -> None:
        self.assertEqual(fv.to_zenkaku("20-16"), "２０－１６")

    def test_mixed_building_name(self) -> None:
        self.assertEqual(
            fv.to_zenkaku("ユニバース千葉ビル1階"), "ユニバース千葉ビル１階"
        )

    def test_idempotent(self) -> None:
        once = fv.to_zenkaku("20-16 A")
        self.assertEqual(fv.to_zenkaku(once), once)

    def test_space_and_empty(self) -> None:
        self.assertEqual(fv.to_zenkaku("a b"), "ａ　ｂ")
        self.assertEqual(fv.to_zenkaku(""), "")
        self.assertEqual(fv.to_zenkaku(None), "")


class TestZenkakuErrorParsing(unittest.TestCase):
    def test_wacoal_error_parsed(self) -> None:
        errors = fv.parse_validation_errors("住所（番地）は全角で入力してください")
        self.assertEqual(errors[0]["kind"], "zenkaku")
        self.assertEqual(errors[0]["field"], "住所（番地）")

    def test_bare_parenthesized_hint_ignored(self) -> None:
        # petline-style static hint — no field name, must NOT become an error.
        errors = fv.parse_validation_errors("（全角で入力してください）")
        self.assertEqual([e for e in errors if e["kind"] == "zenkaku"], [])

    def test_required_still_parsed(self) -> None:
        errors = fv.parse_validation_errors("「お名前」を入力してください")
        self.assertTrue(any(e["kind"] == "required" for e in errors))


class TestClickCascadeDenyAndPartsMatch(unittest.TestCase):
    """String pins for _CLICK_BUTTON_BY_TEXT_JS (no JS runtime in pytest)."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util
        import sys
        import types
        from pathlib import Path

        if "yaml" not in sys.modules:
            sys.modules["yaml"] = types.SimpleNamespace(
                safe_dump=lambda obj, **kwargs: "{}\n",
                safe_load=lambda s: {},
            )
        root = Path(__file__).resolve().parent.parent.parent
        spec = importlib.util.spec_from_file_location(
            "jp_form_outreach_run_v26", root / "jp-form-outreach" / "run.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        cls.js = module._CLICK_BUTTON_BY_TEXT_JS

    def test_deny_list_blocks_back_buttons(self) -> None:
        self.assertIn("denyRe", self.js)
        for needle in ("戻る", "変更する", "submit_?back"):
            self.assertIn(needle, self.js)

    def test_attributes_matched_separately(self) -> None:
        self.assertIn("parts.some", self.js)

    def test_validation_status_containers_are_never_clicked(self) -> None:
        self.assertIn("diagnosticMetaRe", self.js)
        self.assertIn("diagnosticTextRe", self.js)
        self.assertIn("submit[-_]?error", self.js)

    def test_click_commits_active_field_and_dispatches_pointer_sequence(self) -> None:
        self.assertIn("commitAndClick", self.js)
        self.assertIn("pointerdown", self.js)
        self.assertIn("active.blur", self.js)


class TestNativeValidationDiagnostics(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util
        import sys
        import types
        from pathlib import Path

        if "yaml" not in sys.modules:
            sys.modules["yaml"] = types.SimpleNamespace(
                safe_dump=lambda obj, **kwargs: "{}\n",
                safe_load=lambda s: {},
            )
        root = Path(__file__).resolve().parent.parent.parent
        spec = importlib.util.spec_from_file_location(
            "jp_form_outreach_run_native_validation", root / "jp-form-outreach" / "run.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        cls.module = module

    def test_constraint_snapshot_reads_exact_validity_reasons(self) -> None:
        js = self.module._FORM_CONSTRAINTS_JS
        for needle in ("willValidate", "validationMessage", "valueMissing", "customError"):
            self.assertIn(needle, js)
        self.assertNotIn("reportValidity", js)

    def test_native_rows_convert_to_actionable_error_schema(self) -> None:
        rows = self.module._native_validation_errors({
            "invalid": [{
                "name": "requestGroup[]",
                "label": "お問い合わせ種別",
                "reasons": ["valueMissing"],
                "message": "このフィールドを入力してください。",
            }]
        })
        self.assertEqual(rows[0]["field"], "お問い合わせ種別")
        self.assertEqual(rows[0]["kind"], "valueMissing")
        self.assertIn("入力", rows[0]["message"])

    def test_native_radio_group_errors_are_deduplicated(self) -> None:
        rows = self.module._native_validation_errors({
            "invalid": [
                {"name": "kind", "label": "種別 A", "reasons": ["valueMissing"]},
                {"name": "kind", "label": "種別 B", "reasons": ["valueMissing"]},
            ]
        })
        self.assertEqual(len(rows), 1)

    def test_checkbox_scan_prefers_table_row_and_unique_selector(self) -> None:
        js = self.module._LIST_CHECKBOX_GATES_JS
        self.assertIn("el.closest('tr') ||", js)
        self.assertIn("boxes.length === 1", js)
        self.assertIn("group_label", js)


if __name__ == "__main__":
    unittest.main()
