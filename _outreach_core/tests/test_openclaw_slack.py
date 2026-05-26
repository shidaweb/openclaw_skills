"""OpenClaw Slack channel resolution from session fixtures."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import openclaw_slack


class TestOpenclawSlack(unittest.TestCase):
    def test_channel_from_sessions(self) -> None:
        sessions = {
            "agent:main:slack:channel:c09d38ugjtc": {
                "updatedAt": 100,
                "channel": "slack",
                "lastTo": "channel:C09D38UGJTC",
            },
            "agent:main:slack:channel:c09d38ugjtc:thread:1": {
                "updatedAt": 200,
                "channel": "slack",
                "lastTo": "channel:C09D38UGJTC",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sess_path = base / "agents/main/sessions/sessions.json"
            sess_path.parent.mkdir(parents=True)
            sess_path.write_text(json.dumps(sessions))
            with mock.patch.object(openclaw_slack, "openclaw_home", return_value=base):
                with mock.patch.object(openclaw_slack, "load_sender_brief", return_value={}):
                    cid = openclaw_slack.slack_channel_id_from_sessions()
            self.assertEqual(cid, "C09D38UGJTC")

    def test_resolve_prefers_sender_brief_channel_id(self) -> None:
        with mock.patch.object(
            openclaw_slack,
            "load_sender_brief",
            return_value={"slack": {"channel_id": "C11111111"}},
        ):
            self.assertEqual(openclaw_slack.resolve_slack_channel_id(), "C11111111")


if __name__ == "__main__":
    unittest.main()
