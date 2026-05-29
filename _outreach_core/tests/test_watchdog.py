"""watchdog (v4 §15-C)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.helpers import watchdog as wd


class TestWatchdog(unittest.TestCase):
    def test_tick_ok_when_heartbeat_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fresh = {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "active_runs": [],
                "open_needs_attention_count": 0,
            }
            with mock.patch.object(wd, "SKILLS_ROOT", root), mock.patch.object(
                wd, "write_heartbeat"
            ), mock.patch.object(wd, "read_health", return_value=fresh), mock.patch.object(
                wd, "heartbeat_age_seconds", return_value=30
            ), mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "ok")

    def test_tick_restarted_when_stale_and_cowork_down(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "SKILLS_ROOT", root), mock.patch.object(
                wd, "write_heartbeat"
            ), mock.patch.object(wd, "read_health", return_value={"ts": "old"}), mock.patch.object(
                wd, "heartbeat_age_seconds", return_value=600
            ), mock.patch.object(wd, "is_cowork_running", return_value=False), mock.patch.object(
                wd, "can_restart", return_value=True
            ), mock.patch.object(wd, "relaunch_cowork", return_value=True), mock.patch.object(
                wd, "notify_slack", return_value=True
            ), mock.patch.object(wd, "record_restart"), mock.patch.object(
                wd, "save_state"
            ), mock.patch.object(wd, "read_state", return_value={"restart_attempts": []}), mock.patch.object(
                wd, "append_log"
            ):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "restarted")

    def test_tick_abandoned_after_rate_limit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = {"restart_attempts": [{"ts": wd._utc_now(), "outcome": "relaunched"}] * 3}
            with mock.patch.object(wd, "SKILLS_ROOT", root), mock.patch.object(
                wd, "write_heartbeat"
            ), mock.patch.object(wd, "read_health", return_value={}), mock.patch.object(
                wd, "heartbeat_age_seconds", return_value=600
            ), mock.patch.object(wd, "is_cowork_running", return_value=False), mock.patch.object(
                wd, "can_restart", return_value=False
            ), mock.patch.object(wd, "notify_slack", return_value=True), mock.patch.object(
                wd, "record_restart"
            ), mock.patch.object(wd, "save_state"), mock.patch.object(
                wd, "read_state", return_value=state
            ), mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "abandoned")

    def test_tick_stuck_when_cowork_alive_stale_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "SKILLS_ROOT", root), mock.patch.object(
                wd, "write_heartbeat"
            ), mock.patch.object(wd, "read_health", return_value={}), mock.patch.object(
                wd, "heartbeat_age_seconds", return_value=400
            ), mock.patch.object(wd, "is_cowork_running", return_value=True), mock.patch.object(
                wd, "read_state", return_value={"restart_attempts": []}
            ), mock.patch.object(wd, "notify_slack", return_value=True), mock.patch.object(
                wd, "append_log"
            ):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "stuck")

    def test_tick_survives_internal_exception(self) -> None:
        with mock.patch.object(wd, "write_heartbeat", side_effect=RuntimeError("boom")):
            with mock.patch.object(wd, "append_log"):
                outcome = wd.tick()
        self.assertEqual(outcome, "error")

    def test_rate_limit_state_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state: dict = {"restart_attempts": []}
            for _ in range(3):
                wd.record_restart(state, "relaunched")
            self.assertFalse(wd.can_restart(state))
            wd.save_state(state, root)
            loaded = json.loads((root / "data" / "watchdog.state.json").read_text())
            self.assertEqual(len(loaded["restart_attempts"]), 3)


if __name__ == "__main__":
    unittest.main()
