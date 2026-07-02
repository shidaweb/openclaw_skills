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


class TestZenkakuGuard(unittest.TestCase):
    """v31 §WS5a — harvest→decide→apply zenkaku fixer with the pure guard."""

    def _run_fixer(self, rows):
        applied: list[tuple] = []

        def _apply(name, action, value):
            applied.append((name, action, value))
            return {"ok": True}

        with mock.patch.object(run, "_evaluate", return_value=rows), \
                mock.patch.object(run, "_apply_field_action", side_effect=_apply):
            res = run._fix_zenkaku_errors([{"field": "住所（番地）", "kind": "zenkaku"}])
        return res, applied

    def test_address_field_is_converted(self) -> None:
        res, applied = self._run_fixer([
            {"name": "addr2", "value": "1-2-3", "ctx": "住所（番地）", "type": "text"},
        ])
        self.assertEqual(len(res), 1)
        self.assertEqual(applied[0][2], "１－２－３")

    def test_email_in_text_input_is_protected(self) -> None:
        # the production hazard: an email in a text-typed input inside a
        # matching row used to be corrupted to full-width
        res, applied = self._run_fixer([
            {"name": "email2", "value": "shida@torana.co.jp",
             "ctx": "住所（番地）メールアドレス", "type": "text"},
        ])
        self.assertEqual(res, [])
        self.assertEqual(applied, [])

    def test_email_type_is_protected_regardless_of_ctx(self) -> None:
        res, applied = self._run_fixer([
            {"name": "m", "value": "abc", "ctx": "住所（番地）", "type": "email"},
        ])
        self.assertEqual(res, [])

    def test_phone_like_value_is_protected(self) -> None:
        res, applied = self._run_fixer([
            {"name": "t", "value": "043-123-4567", "ctx": "住所（番地）", "type": "text"},
        ])
        self.assertEqual(res, [])

    def test_url_value_is_protected(self) -> None:
        res, applied = self._run_fixer([
            {"name": "hp", "value": "https://torana.co.jp",
             "ctx": "住所（番地）", "type": "text"},
        ])
        self.assertEqual(res, [])


class TestPhoneFormatRotation(unittest.TestCase):
    """v31 §WS5b — validation bounces walk NEW formats instead of toggling."""

    ERRORS = [{"field": "電話番号", "kind": "format"}]

    def _run_fixer(self, value, round_idx):
        applied: list[tuple] = []

        def _apply(name, action, v):
            applied.append((name, action, v))
            return {"ok": True}

        rows = [{"name": "tel", "value": value}]
        with mock.patch.object(run, "_evaluate", return_value=rows), \
                mock.patch.object(run, "_apply_field_action", side_effect=_apply):
            run._fix_phone_format_errors(self.ERRORS, round_idx=round_idx)
        return applied[0][2] if applied else None

    def test_round0_strips_hyphens(self) -> None:
        self.assertEqual(self._run_fixer("043-123-4567", 0), "0431234567")

    def test_round1_tries_second_candidate(self) -> None:
        # hyphenated current: candidates = [digits, legacy 2-4-4 toggle]
        # (area-aware 3-3-4 equals the current value so it's excluded)
        self.assertEqual(self._run_fixer("043-123-4567", 1), "04-3123-4567")

    def test_digits_only_round0_is_area_code_aware(self) -> None:
        # 043 is a 3-digit area code → 3-3-4, not the legacy 2-4-4
        self.assertEqual(self._run_fixer("0431234567", 0), "043-123-4567")

    def test_rounds_wrap_around(self) -> None:
        first = self._run_fixer("0431234567", 0)
        wrapped = self._run_fixer("0431234567", 2)
        self.assertEqual(first, wrapped)  # 2 candidates → round 2 wraps to 0
