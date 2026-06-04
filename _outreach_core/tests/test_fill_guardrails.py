"""run.py wiring for the v15 furigana/subject guardrails + validation recovery.

Simulates the exact YAMAHA failure (kanji in フリガナ（姓）, empty お問い合わせタイトル)
without a browser by stubbing the DOM read/write helpers.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_run_module():
    if "yaml" not in sys.modules:
        sys.modules["yaml"] = types.SimpleNamespace(safe_dump=lambda obj, **kwargs: "{}\n")
    root = Path(__file__).resolve().parent.parent.parent
    run_path = root / "jp-form-outreach" / "run.py"
    spec = importlib.util.spec_from_file_location("jp_form_outreach_run", run_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


_YAMAHA_FIELDS = [
    {"idx": 0, "tag": "input", "type": "text", "label": "姓", "name": "sei", "value": "志田", "required": True},
    {"idx": 1, "tag": "input", "type": "text", "label": "名", "name": "mei", "value": "典道", "required": True},
    {"idx": 2, "tag": "input", "type": "text", "label": "フリガナ（姓）", "name": "kana_sei", "value": "志田典道", "required": True},
    {"idx": 3, "tag": "input", "type": "text", "label": "フリガナ（名）", "name": "kana_mei", "value": "シダノリミツ", "required": True},
    {"idx": 4, "tag": "input", "type": "text", "label": "お問い合わせタイトル", "name": "title", "value": "", "required": True},
    {"idx": 5, "tag": "textarea", "type": "", "label": "お問い合わせ内容", "name": "body", "value": "本文です", "required": True},
]

_SENDER = {"name": "志田 典道", "name_kana": "シダノリミチ", "name_furigana": "しだのりみち"}


class TestApplyFillGuardrails(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.run_mod = _load_run_module()

    def test_fixes_kanji_furigana_and_empty_title(self) -> None:
        captured: list[dict] = []
        target = {"id": 1, "draft": {"subject": "LINE連携CRMのご提案"}}
        diag: dict = {"filled": [], "warnings": []}
        with patch.object(self.run_mod, "_read_text_fields", return_value=list(_YAMAHA_FIELDS)), \
             patch.object(self.run_mod, "_set_text_fields", side_effect=lambda fixes: captured.extend(fixes) or len(fixes)):
            summary = self.run_mod._apply_fill_guardrails(target, _SENDER, "本文です", diag)

        by_idx = {f["idx"]: f["value"] for f in captured}
        # フリガナ（姓） kanji → katakana sei
        self.assertEqual(by_idx.get(2), "シダ")
        # フリガナ（名） wrong-split kana → katakana mei
        self.assertEqual(by_idx.get(3), "ノリミチ")
        # empty title → draft subject
        self.assertEqual(by_idx.get(4), "LINE連携CRMのご提案")
        # body textarea must NOT be touched
        self.assertNotIn(5, by_idx)
        # correct kanji name fields must NOT be touched
        self.assertNotIn(0, by_idx)
        self.assertIsNotNone(summary.get("subject_filled"))

    def test_no_fix_when_already_correct(self) -> None:
        good = [
            {"idx": 0, "tag": "input", "type": "text", "label": "フリガナ（姓）", "value": "シダ", "required": True},
            {"idx": 1, "tag": "input", "type": "text", "label": "件名", "value": "ご提案", "required": True},
        ]
        captured: list[dict] = []
        with patch.object(self.run_mod, "_read_text_fields", return_value=good), \
             patch.object(self.run_mod, "_set_text_fields", side_effect=lambda fixes: captured.extend(fixes) or len(fixes)):
            self.run_mod._apply_fill_guardrails({"draft": {"subject": "x"}}, _SENDER, "b", {"filled": []})
        self.assertEqual(captured, [])


class TestHarvestValidationErrors(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.run_mod = _load_run_module()

    def test_recovers_from_inline_errors(self) -> None:
        page_text = (
            '"フリガナ（姓）"の形式が正しくありません。\n'
            '"お問い合わせタイトル"を入力してください。'
        )
        captured: list[dict] = []

        def fake_eval(js):
            # PAGE_EVIDENCE_JS returns a dict with page text
            return {"text": page_text, "url": "https://example.com/form"}

        with patch.object(self.run_mod, "_evaluate", side_effect=fake_eval), \
             patch.object(self.run_mod, "oc_browser", return_value=""), \
             patch.object(self.run_mod, "_read_text_fields", return_value=list(_YAMAHA_FIELDS)), \
             patch.object(self.run_mod, "_set_text_fields", side_effect=lambda fixes: captured.extend(fixes) or len(fixes)), \
             patch.object(self.run_mod, "_emit_event", return_value=None):
            out = self.run_mod._harvest_and_fix_validation_errors(
                {"id": 1, "draft": {"subject": "ご提案"}},
                {"sender": _SENDER},
                "本文です",
                stage="send",
                trace_dir=None,
            )

        self.assertTrue(out["recoverable"])
        self.assertGreaterEqual(out["fixed"], 2)
        kinds = {(e["field"], e["kind"]) for e in out["errors"]}
        self.assertIn(("フリガナ（姓）", "format"), kinds)
        self.assertIn(("お問い合わせタイトル", "required"), kinds)

    def test_clean_page_is_not_recoverable(self) -> None:
        with patch.object(self.run_mod, "_evaluate", return_value={"text": "送信が完了しました", "url": "x"}), \
             patch.object(self.run_mod, "oc_browser", return_value="お問い合わせありがとうございました"), \
             patch.object(self.run_mod, "_emit_event", return_value=None):
            out = self.run_mod._harvest_and_fix_validation_errors(
                {"id": 1}, {"sender": _SENDER}, "b", stage="send", trace_dir=None
            )
        self.assertEqual(out["errors"], [])
        self.assertFalse(out["recoverable"])


if __name__ == "__main__":
    unittest.main()
