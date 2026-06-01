"""Tests for the per-client `.local` prompt override (distribution split, v6.1)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _outreach_core import prompt as P  # noqa: E402


class TestLocalOverride(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "system_persona.md").write_text("SHARED NEUTRAL", encoding="utf-8")
        (self.dir / "examples.md").write_text("SHARED EXAMPLES", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_falls_back_to_shared_when_no_local(self):
        p = P._prefer_local(self.dir, "system_persona.md")
        self.assertEqual(p.name, "system_persona.md")
        self.assertEqual(p.read_text(encoding="utf-8"), "SHARED NEUTRAL")

    def test_prefers_local_when_present(self):
        (self.dir / "system_persona.local.md").write_text("CLIENT TUNED", encoding="utf-8")
        p = P._prefer_local(self.dir, "system_persona.md")
        self.assertEqual(p.name, "system_persona.local.md")
        self.assertEqual(p.read_text(encoding="utf-8"), "CLIENT TUNED")

    def test_examples_local_override(self):
        (self.dir / "examples.local.md").write_text("CLIENT FEWSHOT", encoding="utf-8")
        p = P._prefer_local(self.dir, "examples.md")
        self.assertEqual(p.read_text(encoding="utf-8"), "CLIENT FEWSHOT")

    def test_build_system_block_uses_local(self):
        (self.dir / "system_persona.local.md").write_text("LOCAL PERSONA", encoding="utf-8")
        (self.dir / "examples.local.md").write_text("LOCAL FEWSHOT", encoding="utf-8")
        block = P.build_system_block({"sender": {"name": "X"}}, self.dir)
        self.assertIn("LOCAL PERSONA", block)
        self.assertIn("LOCAL FEWSHOT", block)
        self.assertNotIn("SHARED NEUTRAL", block)


if __name__ == "__main__":
    unittest.main()
