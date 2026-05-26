"""Slack onboarding wizard CLI (v4 §14-N acceptance 32)."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.helpers import brief as brief_cli


class TestBriefOnboarding(unittest.TestCase):
    def test_write_from_json_creates_brief_and_bind(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            briefs = root / "briefs"
            briefs.mkdir()
            (briefs / "_template.yaml").write_text(
                "brief:\n  id: placeholder\nmodel:\n  name: claude-cli/claude-opus-4-7\n",
                encoding="utf-8",
            )
            ch_dir = root / "data" / "channel_state"
            ch_dir.mkdir(parents=True)
            answers = {
                "sender": {"company": "テスト株式会社", "name": "志田"},
                "product": {"one_liner": "LINE CRM"},
                "desired_channels": ["jp_form"],
            }
            ans_path = root / "answers.json"
            ans_path.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")

            args = argparse.Namespace(
                brief_id="test-onboard",
                answers=str(ans_path),
                display_name="テスト onboard",
                bind_channel="CONBOARD01",
                channel_name="",
                default_channels="jp_form",
            )
            with mock.patch.object(brief_cli, "BRIEFS_DIR", briefs), mock.patch.object(
                brief_cli, "BRIEF_TEMPLATE", briefs / "_template.yaml"
            ), mock.patch.object(brief_cli, "SKILLS_ROOT", root), mock.patch(
                "_outreach_core.config.BRIEFS_DIR", briefs
            ), mock.patch("_outreach_core.channel_state.CHANNEL_STATE_DIR", ch_dir), mock.patch(
                "_outreach_core.channel_state.SKILLS_ROOT", root
            ):
                rc = brief_cli.cmd_write_from_json(args)
            self.assertEqual(rc, 0)
            dest = briefs / "test-onboard.yaml"
            self.assertTrue(dest.is_file())
            text = dest.read_text(encoding="utf-8")
            self.assertIn("テスト株式会社", text)
            state = json.loads((ch_dir / "CONBOARD01.json").read_text(encoding="utf-8"))
            self.assertEqual(state["default_brief"], "test-onboard")


if __name__ == "__main__":
    unittest.main()
