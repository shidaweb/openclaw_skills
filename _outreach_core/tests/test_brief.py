"""Multi-brief config and history isolation (v4 §14-L)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _outreach_core import history
from _outreach_core.config import (
    ACTIVE_BRIEF_FILE,
    BRIEFS_DIR,
    BriefError,
    load_merged_config,
    resolve_brief_id,
)


class TestBriefConfig(unittest.TestCase):
    # Hermetic since v32.1: these used to resolve against the MACHINE's real
    # briefs/ (gitignored) and _active.txt, so they passed locally and failed
    # on every clean checkout — invisible while CI itself was broken.

    def _brief_env(self, td: str, active: str | None):
        from _outreach_core import config as cfg_mod

        briefs = Path(td) / "briefs"
        briefs.mkdir()
        (briefs / "fixture-brief.yaml").write_text("brief:\n  goal: test\n")
        active_file = briefs / "_active.txt"
        if active is not None:
            active_file.write_text(active + "\n")
        return (
            mock.patch.object(cfg_mod, "BRIEFS_DIR", briefs),
            mock.patch.object(cfg_mod, "ACTIVE_BRIEF_FILE", active_file),
        )

    def test_resolve_from_active(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p1, p2 = self._brief_env(td, active="fixture-brief")
            with p1, p2:
                self.assertEqual(resolve_brief_id(None), "fixture-brief")

    def test_resolve_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p1, p2 = self._brief_env(td, active=None)
            with p1, p2:
                self.assertEqual(resolve_brief_id("fixture-brief"), "fixture-brief")

    def test_missing_brief_raises(self) -> None:
        with self.assertRaises(BriefError):
            resolve_brief_id("no-such-brief-xyz")

    def test_merge_brief_wins(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            briefs = root / "briefs"
            briefs.mkdir()
            (briefs / "_active.txt").write_text("test-brief\n")
            (briefs / "test-brief.yaml").write_text(
                "brief:\n  id: test-brief\nmodel:\n  max_chars: 999\n"
            )
            skill = root / "skill"
            skill.mkdir()
            (skill / "config.yaml").write_text("model:\n  max_chars: 400\n")

            with mock.patch("_outreach_core.config.BRIEFS_DIR", briefs), mock.patch(
                "_outreach_core.config.ACTIVE_BRIEF_FILE", briefs / "_active.txt"
            ):
                merged = load_merged_config(skill, "test-brief")
            self.assertEqual(merged["model"]["max_chars"], 999)


class TestBriefHistoryIsolation(unittest.TestCase):
    def test_global_exclude_per_brief(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "jp-form-outreach" / "data" / "briefs"
            li = root / "linkedin-outreach" / "data" / "briefs"
            a_dir = jp / "brief-a"
            b_dir = jp / "brief-b"
            a_dir.mkdir(parents=True)
            b_dir.mkdir(parents=True)
            li.mkdir(parents=True)

            sent_line = json.dumps({"id": "company-1", "sent_at": "2026-01-01"}) + "\n"
            (a_dir / "sent_history.jsonl").write_text(sent_line)
            (b_dir / "sent_history.jsonl").write_text("")

            with mock.patch.object(history, "SKILLS_ROOT", root), mock.patch(
                "_outreach_core.config.resolve_brief_id",
                side_effect=lambda x: x,
            ):
                exclude_a = history.load_global_exclude_set("brief-a")
                exclude_b = history.load_global_exclude_set("brief-b")

            self.assertIn("company-1", exclude_a)
            self.assertNotIn("company-1", exclude_b)


if __name__ == "__main__":
    unittest.main()
