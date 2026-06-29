"""v30 next — _fix_hiragana_errors actively rescues 「ひらがなのみで入力」 errors.

Pilot 2026-06-29 (sunstar) hit text-extracted format complaints that the
old parser mis-labeled as "required". With the form_validation regex
extensions for zenkaku-length / hiragana / phone-format, the three error
classes route to the existing _fix_zenkaku_errors / _fix_phone_format_errors
+ the new _fix_hiragana_errors helpers introduced here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "jp-form-outreach"))

import run  # noqa: E402


class TestFixHiraganaErrors(unittest.TestCase):
    def test_no_hiragana_kind_in_errors_skips(self) -> None:
        # Only fires when at least one ``kind=='hiragana'`` error is present —
        # we never spuriously demote a legitimate katakana field.
        with mock.patch.object(run, "_evaluate") as ev:
            res = run._fix_hiragana_errors([
                {"field": "お名前", "kind": "required"},
            ])
        self.assertEqual(res, [])
        ev.assert_not_called()

    def test_converts_katakana_to_hiragana(self) -> None:
        # _KANA_INPUTS_JS returns rows with current value; the fixer converts
        # katakana to hiragana and writes back via _apply_field_action.
        applied: list[tuple] = []

        def _eval(js: str):
            return [
                {"name": "kana_sei", "value": "シダ"},
                {"name": "kana_mei", "value": "ノリミツ"},
            ]

        def _apply(name, action, value):
            applied.append((name, action, value))
            return {"ok": True}

        with mock.patch.object(run, "_evaluate", side_effect=_eval), \
                mock.patch.object(run, "_apply_field_action", side_effect=_apply):
            res = run._fix_hiragana_errors([{"field": "ふりがな", "kind": "hiragana"}])

        # Both kana fields rewritten with hiragana equivalents.
        self.assertEqual(len(res), 2)
        self.assertEqual([a[0] for a in applied], ["kana_sei", "kana_mei"])
        self.assertEqual([a[2] for a in applied], ["しだ", "のりみつ"])

    def test_already_hiragana_field_left_alone(self) -> None:
        applied: list[tuple] = []

        def _apply(name, action, value):
            applied.append((name, action, value))
            return {"ok": True}

        with mock.patch.object(run, "_evaluate", return_value=[
                {"name": "kana", "value": "しだ"},  # already hiragana
        ]), mock.patch.object(run, "_apply_field_action", side_effect=_apply):
            res = run._fix_hiragana_errors([{"field": "ふりがな", "kind": "hiragana"}])
        self.assertEqual(res, [])
        self.assertEqual(applied, [])

    def test_empty_field_value_skipped(self) -> None:
        with mock.patch.object(run, "_evaluate", return_value=[
                {"name": "kana", "value": ""},
        ]), mock.patch.object(run, "_apply_field_action") as apply:
            res = run._fix_hiragana_errors([{"field": "kana", "kind": "hiragana"}])
        self.assertEqual(res, [])
        apply.assert_not_called()

    def test_swallows_evaluate_exception(self) -> None:
        def _boom(*a, **k):
            raise RuntimeError("eval failed")

        with mock.patch.object(run, "_evaluate", side_effect=_boom):
            res = run._fix_hiragana_errors([{"field": "x", "kind": "hiragana"}])
        self.assertEqual(res, [])


class TestParseProductionFormatErrors(unittest.TestCase):
    """End-to-end: production sunstar text → parsed kinds → wired into the
    three fixers via _harvest_and_fix_validation_errors logic.

    We don't invoke the run.py harvester directly (it touches DOM); we lock
    that the parser categorizes Sunstar's specific error text correctly,
    which is the precondition for the fixers to fire.
    """

    def test_sunstar_round2_text_routes_to_correct_kinds(self) -> None:
        from _outreach_core import form_validation as fv

        # The page text Sunstar surfaces after a failed POST:
        text = (
            "お名前 は全角64文字以内で入力してください\n"
            "フリガナ はひらがなのみで入力してください\n"
            "住所 は全角255文字以内で入力してください\n"
            "電話番号 は電話番号形式で入力してください\n"
        )
        errs = fv.parse_validation_errors(text)
        by_kind: dict[str, list[str]] = {}
        for e in errs:
            by_kind.setdefault(e["kind"], []).append(e["field"])
        self.assertIn("zenkaku", by_kind)
        self.assertIn("hiragana", by_kind)
        self.assertIn("format", by_kind)
        # No spurious "required" entries from these format messages.
        self.assertNotIn("required", by_kind)


if __name__ == "__main__":
    unittest.main()
