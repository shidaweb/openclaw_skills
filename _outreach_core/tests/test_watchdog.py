"""watchdog (v4 §15-C)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.helpers import watchdog as wd


class TestWatchdog(unittest.TestCase):
    def test_tick_ok_when_gateway_healthy_and_no_active_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "SKILLS_ROOT", root), mock.patch.object(
                wd, "read_state", return_value={"restart_attempts": []}
            ), mock.patch.object(wd, "is_gateway_loaded", return_value=True), mock.patch.object(
                wd, "is_gateway_healthy", return_value=True
            ), mock.patch.object(wd, "read_health", return_value={"ts": "old"}), mock.patch.object(
                wd, "heartbeat_age_seconds", return_value=600
            ), mock.patch.object(wd, "collect_active_runs", return_value=[]), mock.patch.object(
                wd, "append_log"
            ):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "ok")

    def test_tick_waits_before_restart_on_first_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "SKILLS_ROOT", root), mock.patch.object(
                wd, "read_state", return_value={"restart_attempts": [], "health_fail_streak": 0}
            ), mock.patch.object(wd, "is_gateway_loaded", return_value=True), mock.patch.object(
                wd, "is_gateway_healthy", return_value=False
            ), mock.patch.object(wd, "restart_gateway", return_value=True) as kick, mock.patch.object(
                wd, "save_state"
            ), mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "stuck")
            kick.assert_not_called()

    def test_tick_restarted_when_unhealthy_streak_reached(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = {"restart_attempts": [], "health_fail_streak": 1}
            with mock.patch.object(wd, "SKILLS_ROOT", root), mock.patch.object(
                wd, "read_state", return_value=state
            ), mock.patch.object(wd, "is_gateway_loaded", return_value=True), mock.patch.object(
                wd, "is_gateway_healthy", return_value=False
            ), mock.patch.object(wd, "can_restart", return_value=True), mock.patch.object(
                wd, "restart_gateway", return_value=True
            ) as kick, mock.patch.object(wd, "notify_slack", return_value=True), mock.patch.object(
                wd, "record_restart"
            ), mock.patch.object(wd, "save_state"), mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "restarted")
            kick.assert_called_once()

    def test_tick_abandoned_after_rate_limit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = {
                "restart_attempts": [{"ts": wd._utc_now(), "outcome": "kickstart"}] * 3,
                "health_fail_streak": 5,
            }
            with mock.patch.object(wd, "SKILLS_ROOT", root), mock.patch.object(
                wd, "read_state", return_value=state
            ), mock.patch.object(wd, "is_gateway_loaded", return_value=True), mock.patch.object(
                wd, "is_gateway_healthy", return_value=False
            ), mock.patch.object(wd, "can_restart", return_value=False), mock.patch.object(
                wd, "notify_slack", return_value=True
            ), mock.patch.object(wd, "record_restart"), mock.patch.object(
                wd, "save_state"
            ), mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "abandoned")

    def test_tick_stuck_when_active_run_stale_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "SKILLS_ROOT", root), mock.patch.object(
                wd, "read_state", return_value={"restart_attempts": []}
            ), mock.patch.object(wd, "is_gateway_loaded", return_value=True), mock.patch.object(
                wd, "is_gateway_healthy", return_value=True
            ), mock.patch.object(wd, "read_health", return_value={"ts": "old"}), mock.patch.object(
                wd, "heartbeat_age_seconds", return_value=400
            ), mock.patch.object(wd, "collect_active_runs", return_value=[{"run_id": "r1"}]), mock.patch.object(
                wd, "notify_slack", return_value=True
            ), mock.patch.object(wd, "save_state"), mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "stuck")

    def test_tick_ok_when_stale_but_no_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "SKILLS_ROOT", root), mock.patch.object(
                wd, "read_state", return_value={"restart_attempts": []}
            ), mock.patch.object(wd, "is_gateway_loaded", return_value=True), mock.patch.object(
                wd, "is_gateway_healthy", return_value=True
            ), mock.patch.object(wd, "read_health", return_value={"ts": "old"}), mock.patch.object(
                wd, "heartbeat_age_seconds", return_value=600
            ), mock.patch.object(wd, "collect_active_runs", return_value=[]), mock.patch.object(
                wd, "notify_slack", return_value=True
            ), mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "ok")

    def test_tick_stuck_when_gateway_not_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "SKILLS_ROOT", root), mock.patch.object(
                wd, "read_state", return_value={"restart_attempts": []}
            ), mock.patch.object(wd, "is_gateway_loaded", return_value=False), mock.patch.object(
                wd, "notify_slack", return_value=True
            ), mock.patch.object(wd, "save_state"), mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "stuck")

    def test_tick_survives_internal_exception(self) -> None:
        with mock.patch.object(wd, "read_state", side_effect=RuntimeError("boom")):
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
