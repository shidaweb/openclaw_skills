"""Helper CLIs: dump_exclude_set, append_targets."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.helpers.append_targets import append_jp_form
from _outreach_core.helpers.dump_exclude_set import dump_exclude_sets


class TestHelpers(unittest.TestCase):
    def test_dump_exclude_sets_shape(self) -> None:
        out = dump_exclude_sets()
        self.assertIn("linkedin", out)
        self.assertIn("jp_form", out)
        self.assertIn("canonical", out)
        self.assertIsInstance(out["linkedin"], list)

    def test_append_targets_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ypath = Path(tmp) / "targets.yaml"
            ypath.write_text("companies: []\n", encoding="utf-8")
            items = [{"id": "test_co", "name": "テスト株式会社", "industry": "EdTech"}]
            n1 = append_jp_form(items, ypath)
            n2 = append_jp_form(items, ypath)
            self.assertEqual(n1, 1)
            self.assertEqual(n2, 0)

    def test_append_targets_keeps_contact_url_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ypath = Path(tmp) / "targets.yaml"
            ypath.write_text("companies: []\n", encoding="utf-8")
            items = [
                {
                    "id": "cand_co",
                    "name": "候補株式会社",
                    "contact_url_candidates": [
                        "https://example.co.jp/recruit",
                        "https://example.co.jp/contact",
                    ],
                }
            ]
            n = append_jp_form(items, ypath)
            self.assertEqual(n, 1)
            data = yaml.safe_load(ypath.read_text(encoding="utf-8"))
            row = (data.get("companies") or [])[0]
            self.assertEqual(
                row.get("contact_url_candidates"),
                ["https://example.co.jp/recruit", "https://example.co.jp/contact"],
            )


if __name__ == "__main__":
    unittest.main()
