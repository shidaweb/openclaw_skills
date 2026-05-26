"""Slack channel ↔ brief binding (v4 §14-F)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _outreach_core import channel_state
from _outreach_core.config import BriefError


class TestChannelState(unittest.TestCase):
    def test_bind_and_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            briefs = root / "briefs"
            briefs.mkdir()
            (briefs / "test-brief.yaml").write_text("brief:\n  id: test-brief\n")
            ch_dir = root / "data" / "channel_state"
            ch_dir.mkdir(parents=True)

            with mock.patch.object(channel_state, "SKILLS_ROOT", root), mock.patch.object(
                channel_state, "CHANNEL_STATE_DIR", ch_dir
            ), mock.patch.object(channel_state, "BRIEFS_DIR", briefs), mock.patch(
                "_outreach_core.config.BRIEFS_DIR", briefs
            ):
                channel_state.bind(
                    "CTEST123",
                    "test-brief",
                    default_channels=["jp_form"],
                )
                bid, channels, is_new = channel_state.resolve_brief_for_channel("CTEST123")

            self.assertFalse(is_new)
            self.assertEqual(bid, "test-brief")
            self.assertEqual(channels, ["jp_form"])

    def test_new_channel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ch_dir = root / "data" / "channel_state"
            ch_dir.mkdir(parents=True)
            with mock.patch.object(channel_state, "SKILLS_ROOT", root), mock.patch.object(
                channel_state, "CHANNEL_STATE_DIR", ch_dir
            ):
                bid, channels, is_new = channel_state.resolve_brief_for_channel("CNEW999")
            self.assertTrue(is_new)
            self.assertIsNone(bid)
            self.assertEqual(channels, [])

    def test_config_resolve_via_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            briefs = root / "briefs"
            briefs.mkdir()
            (briefs / "env-brief.yaml").write_text("brief:\n  id: env-brief\n")
            ch_dir = root / "data" / "channel_state"
            ch_dir.mkdir(parents=True)
            state = {
                "channel_id": "CENV001",
                "default_brief": "env-brief",
                "default_channels": ["linkedin"],
            }
            (ch_dir / "CENV001.json").write_text(json.dumps(state))

            from _outreach_core.config import resolve_brief_id

            with mock.patch.object(channel_state, "SKILLS_ROOT", root), mock.patch.object(
                channel_state, "CHANNEL_STATE_DIR", ch_dir
            ), mock.patch("_outreach_core.config.BRIEFS_DIR", briefs), mock.patch.dict(
                "os.environ", {"DOORMAN_SLACK_CHANNEL_ID": "CENV001"}, clear=False
            ):
                self.assertEqual(resolve_brief_id(None), "env-brief")

    def test_unbound_channel_raises_via_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ch_dir = root / "data" / "channel_state"
            ch_dir.mkdir(parents=True)
            from _outreach_core.config import resolve_brief_id

            with mock.patch.object(channel_state, "SKILLS_ROOT", root), mock.patch.object(
                channel_state, "CHANNEL_STATE_DIR", ch_dir
            ), mock.patch("_outreach_core.config.BRIEFS_DIR", root / "briefs"), mock.patch.dict(
                "os.environ", {"DOORMAN_SLACK_CHANNEL_ID": "CUNBOUND"}, clear=False
            ):
                with self.assertRaises(BriefError):
                    resolve_brief_id(None)


if __name__ == "__main__":
    unittest.main()
