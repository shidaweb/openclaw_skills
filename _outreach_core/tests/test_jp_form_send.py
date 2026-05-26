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


if __name__ == "__main__":
    unittest.main()
