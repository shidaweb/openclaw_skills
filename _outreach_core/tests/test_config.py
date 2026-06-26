"""Runtime config defaults."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.config import heartbeat_interval_sec


class TestHeartbeatDefaults(unittest.TestCase):
    def test_default_heartbeat_interval_is_ten_minutes(self) -> None:
        self.assertEqual(heartbeat_interval_sec({}), 600)

    def test_invalid_heartbeat_interval_falls_back_to_ten_minutes(self) -> None:
        self.assertEqual(heartbeat_interval_sec({"heartbeat": {"interval_sec": "bad"}}), 600)

    def test_configured_heartbeat_interval_wins(self) -> None:
        self.assertEqual(heartbeat_interval_sec({"heartbeat": {"interval_sec": 120}}), 120)


if __name__ == "__main__":
    unittest.main()
