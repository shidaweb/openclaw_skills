"""jp-form-outreach send stage invariants (v4)."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


class TestJpFormSend(unittest.TestCase):
    def test_stage_send_has_no_input_calls(self) -> None:
        run_py = Path(__file__).resolve().parent.parent.parent / "jp-form-outreach" / "run.py"
        text = run_py.read_text(encoding="utf-8")
        m = re.search(r"^def stage_send\(", text, re.M)
        self.assertIsNotNone(m)
        start = m.start()
        rest = text[start + 1 :]
        m2 = re.search(r"^def [a-z_]+\(", rest, re.M)
        end = start + 1 + m2.start() if m2 else len(text)
        block = text[start:end]
        self.assertNotIn("input(", block, "stage_send must not call input()")

    def test_form_fields_js_scoped_to_pick_root(self) -> None:
        run_py = Path(__file__).resolve().parent.parent.parent / "jp-form-outreach" / "run.py"
        text = run_py.read_text(encoding="utf-8")
        self.assertIn("const pickRoot = () =>", text)
        self.assertIn("root.querySelectorAll('input,textarea,select')", text)

    def test_form_fields_js_captures_v31_attributes(self) -> None:
        # v31 §WS3a-c — the extractor must emit visibility, format constraints
        # and option values. JS strings can't be unit-tested directly; these
        # source assertions pin the capture contract.
        run_py = Path(__file__).resolve().parent.parent.parent / "jp-form-outreach" / "run.py"
        text = run_py.read_text(encoding="utf-8")
        self.assertIn("const isVisible = (el) =>", text)
        self.assertIn("visible: visible", text)
        self.assertIn("fmt.pattern = el.getAttribute('pattern')", text)
        self.assertIn("fmt.inputmode = el.getAttribute('inputmode')", text)
        self.assertIn("fmt.autocomplete = el.getAttribute('autocomplete')", text)
        # option {t, v} capture with the raised cap
        self.assertIn("({ t: (o.text || '').trim().slice(0, 40), v: o.value })", text)
        self.assertIn(".slice(0, 100)", text)

    def test_analyzer_prompt_has_v31_rules(self) -> None:
        run_py = Path(__file__).resolve().parent.parent.parent / "jp-form-outreach" / "run.py"
        text = run_py.read_text(encoding="utf-8")
        self.assertIn("27. Fields with visible:false", text)
        self.assertIn("28. Honor pattern/inputmode/autocomplete", text)

    def test_option_label_value_handles_all_shapes(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "jp-form-outreach"))
        import run
        # new compact shape
        self.assertEqual(run._option_label_value({"t": "法人のお客様", "v": "corp"}),
                         ("法人のお客様", "corp"))
        # legacy plain string
        self.assertEqual(run._option_label_value("その他"), ("その他", "その他"))
        # legacy label/value dicts
        self.assertEqual(run._option_label_value({"label": "A", "value": "a"}), ("A", "a"))
        # text-only dict: value falls back to text
        self.assertEqual(run._option_label_value({"t": "選択してください"}),
                         ("選択してください", "選択してください"))
        # value-only (rare): label falls back to value
        self.assertEqual(run._option_label_value({"v": "x"}), ("x", "x"))

    def test_extract_inquiry_type_fields_accepts_tv_options(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "jp-form-outreach"))
        import run
        form_fields = {
            "selects": [{
                "name": "inquiry_kind",
                "label": "お問い合わせ種別",
                "required": True,
                "options": [
                    {"t": "選択してください", "v": ""},
                    {"t": "業務提携について", "v": "partnership"},
                    "その他",  # legacy string mixed in
                ],
            }],
            "radios": {},
        }
        fields = run._extract_inquiry_type_fields(form_fields)
        self.assertEqual(len(fields), 1)
        opts = fields[0]["options"]
        self.assertIn({"label": "業務提携について", "value": "partnership"}, opts)
        self.assertIn({"label": "その他", "value": "その他"}, opts)

    def test_broken_form_structure_wired_into_enrich_and_send(self) -> None:
        # v31 §WS3d — the committed helper must actually be consulted.
        run_py = Path(__file__).resolve().parent.parent.parent / "jp-form-outreach" / "run.py"
        text = run_py.read_text(encoding="utf-8")
        self.assertEqual(text.count("fv.form_has_broken_structure("), 2,
                         "expected enrich + send preflight call sites")
        self.assertIn("enrich.broken_form_structure", text)
        self.assertIn('"broken_form_structure"', text)


if __name__ == "__main__":
    unittest.main()
