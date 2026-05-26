"""Tests for cross-skill history exclusion."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import history


class TestGlobalExclude(unittest.TestCase):
    def test_load_id_set_includes_canonical_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            path = data / "sent_history.jsonl"
            path.write_text(
                json.dumps({"id": "foo_slug", "canonical_id": "acme_corp"}) + "\n"
            )
            ids = history.load_sent_set(data)
            self.assertIn("foo_slug", ids)
            self.assertIn("acme_corp", ids)

    def test_is_excluded_by_canonical(self) -> None:
        exclude = {"acme_corp"}
        draft = {"id": "other_slug", "name": "Acme Corp"}
        self.assertTrue(history.is_excluded("other_slug", exclude, draft))

    def test_canonical_company_id_normalizes(self) -> None:
        a = history.canonical_company_id("  Acme Corp.  ")
        b = history.canonical_company_id("acme corp")
        self.assertEqual(a, b)
        self.assertTrue(a)


if __name__ == "__main__":
    unittest.main()
