"""Dynamic required field detection after form fill (v4 A-3)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.verify import scan_new_required_after_fill


class TestFormFillScan(unittest.TestCase):
    def test_detects_new_empty_required(self) -> None:
        def fake_eval(_js: str) -> dict:
            return {
                "empty_required": [
                    {"name": "email", "label": "メール"},
                    {"name": "company", "label": "会社名"},
                ],
            }

        baseline = {"email"}
        new = scan_new_required_after_fill(
            fake_eval, baseline_empty_names=baseline, filled_names=set()
        )
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["name"], "company")

    def test_ignores_filled_names(self) -> None:
        def fake_eval(_js: str) -> dict:
            return {"empty_required": [{"name": "company", "label": "会社名"}]}

        new = scan_new_required_after_fill(
            fake_eval,
            baseline_empty_names=set(),
            filled_names={"company"},
        )
        self.assertEqual(new, [])


if __name__ == "__main__":
    unittest.main()
