"""watchdog (v4 §15-C; v14 §W1-W5)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.helpers import watchdog as wd


def _no_self_heal():
    return mock.patch.object(wd, "_self_heal")


class TestWatchdogTick(unittest.TestCase):
    def test_tick_ok_when_gateway_healthy_and_no_active_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "read_state", return_value={"restart_attempts": []}), \
                _no_self_heal(), \
                mock.patch.object(wd, "is_gateway_loaded", return_value=True), \
                mock.patch.object(wd, "is_gateway_healthy", return_value=True), \
                mock.patch.object(wd, "configured_but_down_channels", return_value=[]), \
                mock.patch.object(wd, "read_health", return_value={"ts": "old"}), \
                mock.patch.object(wd, "heartbeat_age_seconds", return_value=600), \
                mock.patch.object(wd, "collect_active_runs", return_value=[]), \
                mock.patch.object(wd, "save_state"), \
                mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "ok")

    def test_tick_started_when_gateway_not_loaded(self) -> None:
        """§W1: dead/unregistered gateway → watchdog runs start_cmd."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "read_state", return_value={"restart_attempts": []}), \
                _no_self_heal(), \
                mock.patch.object(wd, "is_gateway_loaded", return_value=False), \
                mock.patch.object(wd, "start_gateway", return_value=True) as start, \
                mock.patch.object(wd, "restart_gateway", return_value=True) as kick, \
                mock.patch.object(wd, "notify_slack", return_value=True), \
                mock.patch.object(wd, "save_state"), \
                mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "started")
            start.assert_called_once()
            kick.assert_not_called()

    def test_tick_abandoned_when_not_loaded_over_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "read_state", return_value={"restart_attempts": []}), \
                _no_self_heal(), \
                mock.patch.object(wd, "is_gateway_loaded", return_value=False), \
                mock.patch.object(wd, "can_restart", return_value=False), \
                mock.patch.object(wd, "start_gateway", return_value=True) as start, \
                mock.patch.object(wd, "_escalate"), \
                mock.patch.object(wd, "record_restart"), \
                mock.patch.object(wd, "save_state"), \
                mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "abandoned")
            start.assert_not_called()

    def test_tick_waits_before_restart_on_first_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "read_state", return_value={"restart_attempts": [], "health_fail_streak": 0}), \
                _no_self_heal(), \
                mock.patch.object(wd, "is_gateway_loaded", return_value=True), \
                mock.patch.object(wd, "is_gateway_healthy", return_value=False), \
                mock.patch.object(wd, "restart_gateway", return_value=True) as kick, \
                mock.patch.object(wd, "save_state"), \
                mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "stuck")
            kick.assert_not_called()

    def test_tick_restarted_when_unhealthy_streak_reached(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = {"restart_attempts": [], "health_fail_streak": 1}
            with mock.patch.object(wd, "read_state", return_value=state), \
                _no_self_heal(), \
                mock.patch.object(wd, "is_gateway_loaded", return_value=True), \
                mock.patch.object(wd, "is_gateway_healthy", return_value=False), \
                mock.patch.object(wd, "can_restart", return_value=True), \
                mock.patch.object(wd, "restart_gateway", return_value=True) as kick, \
                mock.patch.object(wd, "start_gateway", return_value=True) as start, \
                mock.patch.object(wd, "notify_slack", return_value=True), \
                mock.patch.object(wd, "record_restart"), \
                mock.patch.object(wd, "save_state"), \
                mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "restarted")
            kick.assert_called_once()
            start.assert_not_called()

    def test_tick_abandoned_after_rate_limit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = {
                "restart_attempts": [{"ts": wd._utc_now(), "outcome": "kickstart"}] * 3,
                "health_fail_streak": 5,
            }
            with mock.patch.object(wd, "read_state", return_value=state), \
                _no_self_heal(), \
                mock.patch.object(wd, "is_gateway_loaded", return_value=True), \
                mock.patch.object(wd, "is_gateway_healthy", return_value=False), \
                mock.patch.object(wd, "can_restart", return_value=False), \
                mock.patch.object(wd, "_escalate"), \
                mock.patch.object(wd, "record_restart"), \
                mock.patch.object(wd, "save_state"), \
                mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "abandoned")

    def test_tick_stuck_when_active_run_stale_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "read_state", return_value={"restart_attempts": []}), \
                _no_self_heal(), \
                mock.patch.object(wd, "is_gateway_loaded", return_value=True), \
                mock.patch.object(wd, "is_gateway_healthy", return_value=True), \
                mock.patch.object(wd, "configured_but_down_channels", return_value=[]), \
                mock.patch.object(wd, "read_health", return_value={"ts": "old"}), \
                mock.patch.object(wd, "heartbeat_age_seconds", return_value=400), \
                mock.patch.object(wd, "collect_active_runs", return_value=[{"run_id": "r1"}]), \
                mock.patch.object(wd, "notify_slack", return_value=True), \
                mock.patch.object(wd, "save_state"), \
                mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "stuck")

    def test_tick_waits_then_restarts_on_stuck_channel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "read_state", return_value={"restart_attempts": [], "channel_fail_streak": 0}), \
                _no_self_heal(), \
                mock.patch.object(wd, "is_gateway_loaded", return_value=True), \
                mock.patch.object(wd, "is_gateway_healthy", return_value=True), \
                mock.patch.object(wd, "configured_but_down_channels", return_value=["slack"]), \
                mock.patch.object(wd, "restart_gateway", return_value=True) as kick, \
                mock.patch.object(wd, "save_state"), \
                mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "stuck")
            kick.assert_not_called()

            state = {"restart_attempts": [], "channel_fail_streak": wd.CHANNEL_FAIL_RESTART_THRESHOLD - 1}
            with mock.patch.object(wd, "read_state", return_value=state), \
                _no_self_heal(), \
                mock.patch.object(wd, "is_gateway_loaded", return_value=True), \
                mock.patch.object(wd, "is_gateway_healthy", return_value=True), \
                mock.patch.object(wd, "configured_but_down_channels", return_value=["slack"]), \
                mock.patch.object(wd, "can_restart", return_value=True), \
                mock.patch.object(wd, "restart_gateway", return_value=True) as kick2, \
                mock.patch.object(wd, "notify_slack", return_value=True), \
                mock.patch.object(wd, "record_restart"), \
                mock.patch.object(wd, "save_state"), \
                mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "restarted")
            kick2.assert_called_once()

    def test_tick_survives_internal_exception(self) -> None:
        with mock.patch.object(wd, "read_state", side_effect=RuntimeError("boom")):
            with mock.patch.object(wd, "append_log"):
                outcome = wd.tick()
        self.assertEqual(outcome, "error")

    def test_tick_restarts_on_recent_cli_backend_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = {"restart_attempts": []}
            timeout = {"marker": "m1", "epoch": 1_000_000.0, "ts": "2026-06-26T04:31:00Z"}
            with mock.patch.object(wd, "read_state", return_value=state), \
                _no_self_heal(), \
                mock.patch.object(wd, "is_gateway_loaded", return_value=True), \
                mock.patch.object(wd, "is_gateway_healthy", return_value=True), \
                mock.patch.object(wd, "configured_but_down_channels", return_value=[]), \
                mock.patch.object(wd, "latest_cli_backend_timeout", return_value=timeout), \
                mock.patch.object(wd, "can_restart", return_value=True), \
                mock.patch.object(wd, "restart_gateway", return_value=True) as kick, \
                mock.patch.object(wd, "notify_slack", return_value=True) as notify, \
                mock.patch.object(wd, "record_restart"), \
                mock.patch.object(wd, "save_state"), \
                mock.patch.object(wd, "append_log"):
                outcome = wd.tick(root)
            self.assertEqual(outcome, "restarted")
            kick.assert_called_once()
            notify.assert_called_once()
            self.assertEqual(state["last_cli_backend_timeout_marker"], "m1")

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


class TestDecideAction(unittest.TestCase):
    def test_dead_within_budget_starts(self) -> None:
        self.assertEqual(
            wd.decide_action(
                loaded=False, healthy=False, health_fail_streak=0,
                health_fail_threshold=2, can_restart=True,
            ),
            "start",
        )

    def test_dead_over_budget_abandons(self) -> None:
        self.assertEqual(
            wd.decide_action(
                loaded=False, healthy=False, health_fail_streak=0,
                health_fail_threshold=2, can_restart=False,
            ),
            "abandoned",
        )

    def test_hung_at_threshold_kickstarts(self) -> None:
        self.assertEqual(
            wd.decide_action(
                loaded=True, healthy=False, health_fail_streak=2,
                health_fail_threshold=2, can_restart=True,
            ),
            "kickstart",
        )

    def test_hung_below_threshold_waits(self) -> None:
        self.assertEqual(
            wd.decide_action(
                loaded=True, healthy=False, health_fail_streak=1,
                health_fail_threshold=2, can_restart=True,
            ),
            "wait",
        )

    def test_wake_skips_wait_and_acts_now(self) -> None:
        self.assertEqual(
            wd.decide_action(
                loaded=True, healthy=False, health_fail_streak=1,
                health_fail_threshold=2, can_restart=True, woke_from_sleep=True,
            ),
            "kickstart",
        )

    def test_healthy_is_ok(self) -> None:
        self.assertEqual(
            wd.decide_action(
                loaded=True, healthy=True, health_fail_streak=0,
                health_fail_threshold=2, can_restart=True,
            ),
            "ok",
        )

    def test_unknown_health_is_ok_no_evidence(self) -> None:
        self.assertEqual(
            wd.decide_action(
                loaded=True, healthy=None, health_fail_streak=0,
                health_fail_threshold=2, can_restart=True,
            ),
            "ok",
        )


class TestStaleActiveRuns(unittest.TestCase):
    def test_fresh_run_activity_overrides_stale_host_heartbeat(self) -> None:
        runs = [{"run_id": "fresh", "activity_age_sec": 20}]
        self.assertEqual(
            wd.stale_active_runs(
                runs, threshold_sec=300, fallback_age_sec=3000
            ),
            [],
        )

    def test_only_stale_run_is_returned(self) -> None:
        runs = [
            {"run_id": "fresh", "activity_age_sec": 20},
            {"run_id": "stale", "activity_age_sec": 301},
        ]
        self.assertEqual(
            [r["run_id"] for r in wd.stale_active_runs(runs, threshold_sec=300)],
            ["stale"],
        )

    def test_missing_run_age_uses_host_fallback(self) -> None:
        rows = wd.stale_active_runs(
            [{"run_id": "legacy"}],
            threshold_sec=300,
            fallback_age_sec=400,
        )
        self.assertEqual(rows[0]["activity_age_sec"], 400)


class TestDetectWake(unittest.TestCase):
    def test_gap_beyond_factor_is_wake(self) -> None:
        # interval 60s, factor 3 → threshold 180s; gap 300s → wake.
        self.assertTrue(
            wd.detect_wake(now_epoch=1_000_300, last_tick_epoch=1_000_000, interval_sec=60, factor=3)
        )

    def test_small_gap_is_not_wake(self) -> None:
        self.assertFalse(
            wd.detect_wake(now_epoch=1_000_120, last_tick_epoch=1_000_000, interval_sec=60, factor=3)
        )

    def test_no_prior_tick_is_not_wake(self) -> None:
        self.assertFalse(
            wd.detect_wake(now_epoch=1_000_000, last_tick_epoch=None, interval_sec=60, factor=3)
        )


class TestShouldEscalate(unittest.TestCase):
    def test_escalates_when_dead_long_enough_first_time(self) -> None:
        now = 1_000_000.0
        self.assertTrue(
            wd.should_escalate(
                abandoned=True,
                last_ok_epoch=now - 20 * 60,
                now_epoch=now,
                dead_alert_min=15,
                last_escalation_epoch=None,
                renotify_sec=1800,
            )
        )

    def test_no_escalate_when_recently_ok(self) -> None:
        now = 1_000_000.0
        self.assertFalse(
            wd.should_escalate(
                abandoned=True,
                last_ok_epoch=now - 5 * 60,
                now_epoch=now,
                dead_alert_min=15,
                last_escalation_epoch=None,
                renotify_sec=1800,
            )
        )

    def test_no_escalate_when_not_abandoned(self) -> None:
        self.assertFalse(
            wd.should_escalate(
                abandoned=False, last_ok_epoch=None, now_epoch=1.0,
                dead_alert_min=15, last_escalation_epoch=None, renotify_sec=1800,
            )
        )

    def test_renotify_throttled(self) -> None:
        now = 1_000_000.0
        self.assertFalse(
            wd.should_escalate(
                abandoned=True,
                last_ok_epoch=now - 60 * 60,
                now_epoch=now,
                dead_alert_min=15,
                last_escalation_epoch=now - 600,  # 10 min ago, < 30 min floor
                renotify_sec=1800,
            )
        )
        self.assertTrue(
            wd.should_escalate(
                abandoned=True,
                last_ok_epoch=now - 60 * 60,
                now_epoch=now,
                dead_alert_min=15,
                last_escalation_epoch=now - 2000,  # > 30 min ago
                renotify_sec=1800,
            )
        )


class TestVendorPing(unittest.TestCase):
    def test_payload_is_pii_free(self) -> None:
        payload = wd.vendor_ping_payload(install_id="abc123", status="dead", now_iso="2026-01-01T00:00:00Z")
        self.assertEqual(set(payload.keys()), {"install_id", "status", "ts", "schema"})
        blob = json.dumps(payload).lower()
        for forbidden in ("host", "user", "name", "email", "company", "draft", "target"):
            self.assertNotIn(forbidden, blob)

    def test_disabled_by_default(self) -> None:
        cfg = {"vendor_ping": {"enabled": False, "url": "https://example.com/ping"}}
        called = wd.send_vendor_ping(cfg, {"install_id": "x", "status": "dead"})
        self.assertFalse(called)


class TestSelfHeal(unittest.TestCase):
    def test_self_heal_reinstalls_watchdog_when_unloaded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "is_watchdog_loaded", return_value=False), \
                mock.patch.object(wd, "ensure_watchdog_installed", return_value=True) as ensure, \
                mock.patch.object(wd, "gateway_keepalive_state", return_value=True), \
                mock.patch.object(wd, "reassert_gateway_keepalive") as reassert, \
                mock.patch.object(wd, "append_log"):
                wd._self_heal({}, root)
            ensure.assert_called_once()
            reassert.assert_not_called()

    def test_self_heal_reasserts_keepalive_when_false(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "is_watchdog_loaded", return_value=True), \
                mock.patch.object(wd, "reload_watchdog") as reload, \
                mock.patch.object(wd, "gateway_keepalive_state", return_value=False), \
                mock.patch.object(wd, "reassert_gateway_keepalive", return_value=True) as reassert, \
                mock.patch.object(wd, "append_log"):
                wd._self_heal({}, root)
            reload.assert_not_called()
            reassert.assert_called_once()

    def test_should_reload_and_reassert_predicates(self) -> None:
        self.assertTrue(wd.should_reload_watchdog(False))
        self.assertFalse(wd.should_reload_watchdog(True))
        self.assertTrue(wd.should_reassert_keepalive(False))
        self.assertFalse(wd.should_reassert_keepalive(True))
        self.assertFalse(wd.should_reassert_keepalive(None))


class TestOpenClawRuntimePatch(unittest.TestCase):
    def test_patch_needed_when_runtime_settings_missing(self) -> None:
        self.assertTrue(wd.openclaw_runtime_patch_needed({"agents": {"defaults": {}}}))

    def test_apply_openclaw_runtime_patch_writes_required_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "openclaw.json"
            path.write_text(json.dumps({"agents": {"defaults": {}}}), encoding="utf-8")
            changed = wd.apply_openclaw_runtime_patch(path)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(changed)
        defaults = data["agents"]["defaults"]
        self.assertEqual(defaults["timeoutSeconds"], wd.OPENCLAW_AGENT_TIMEOUT_SECONDS)
        backend = defaults["cliBackends"][wd.OPENCLAW_CLI_BACKEND_ID]
        self.assertEqual(
            backend["reliability"]["watchdog"]["fresh"]["noOutputTimeoutMs"],
            wd.OPENCLAW_CLI_NO_OUTPUT_TIMEOUT_MS,
        )
        self.assertEqual(
            backend["reliability"]["watchdog"]["resume"]["noOutputTimeoutMs"],
            wd.OPENCLAW_CLI_NO_OUTPUT_TIMEOUT_MS,
        )
        self.assertEqual(defaults["heartbeat"]["every"], "10m")
        self.assertIn("日本語", defaults["heartbeat"]["prompt"])

    def test_latest_cli_backend_timeout_reads_recent_json_log_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "openclaw.log"
            log.write_text(
                '{"time":"2026-06-26T04:31:00Z","message":"CLI subprocess: '
                'timed out after 180s (no-output stall)."}\n',
                encoding="utf-8",
            )
            now = wd._parse_ts("2026-06-26T04:32:00Z").timestamp()
            item = wd.latest_cli_backend_timeout(log_paths=[log], now_epoch=now, lookback_sec=600)
        self.assertIsNotNone(item)
        self.assertEqual(item["ts"], "2026-06-26T04:31:00Z")


class TestRecentAuthFailures(unittest.TestCase):
    """v32.1 — expired-OAuth 401 streak detection from the gateway log."""

    # Real production shapes (2026-07-12 → 07-14 outage).
    LINE_EMBEDDED = (
        '{"0":"Embedded agent failed before reply: Invalid authentication '
        'credentials","_meta":{"date":"%s","logLevelName":"ERROR"}}'
    )
    LINE_401 = (
        '{"0":"Embedded agent failed before reply: Failed to authenticate. '
        'API Error: 401 The socket connection was closed unexpectedly",'
        '"_meta":{"date":"%s"}}'
    )

    def _write(self, td: str, *lines: str) -> Path:
        log = Path(td) / "openclaw.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return log

    def test_streak_detected_with_count_and_newest_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = self._write(
                td,
                self.LINE_EMBEDDED % "2026-07-14T11:32:04.096Z",
                self.LINE_401 % "2026-07-14T11:42:07.649Z",
            )
            now = wd._parse_ts("2026-07-14T11:45:00Z").timestamp()
            item = wd.recent_auth_failures(log_paths=[log], now_epoch=now, lookback_sec=1500)
        self.assertIsNotNone(item)
        self.assertEqual(item["count"], 2)
        self.assertEqual(item["ts"], "2026-07-14T11:42:07Z")

    def test_old_failures_outside_lookback_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = self._write(td, self.LINE_EMBEDDED % "2026-07-14T08:00:00Z")
            now = wd._parse_ts("2026-07-14T11:45:00Z").timestamp()
            self.assertIsNone(
                wd.recent_auth_failures(log_paths=[log], now_epoch=now, lookback_sec=1500)
            )

    def test_non_auth_errors_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = self._write(
                td,
                '{"0":"GatewayTransportError: gateway timeout after 25000ms",'
                '"_meta":{"date":"2026-07-14T11:42:00Z"}}',
            )
            now = wd._parse_ts("2026-07-14T11:45:00Z").timestamp()
            self.assertIsNone(
                wd.recent_auth_failures(log_paths=[log], now_epoch=now, lookback_sec=1500)
            )


class TestTickAuthFailureRestart(unittest.TestCase):
    """v32.1 — tick wiring: streak → restart; single blip → no restart;
    same marker → no double restart."""

    def _tick(self, state, auth_fail, can_restart=True):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            root = Path(td)
            patches = [
                mock.patch.object(wd, "read_state", return_value=state),
                _no_self_heal(),
                mock.patch.object(wd, "is_gateway_loaded", return_value=True),
                mock.patch.object(wd, "is_gateway_healthy", return_value=True),
                mock.patch.object(wd, "configured_but_down_channels", return_value=[]),
                mock.patch.object(wd, "latest_cli_backend_timeout", return_value=None),
                mock.patch.object(wd, "recent_auth_failures", return_value=auth_fail),
                mock.patch.object(wd, "can_restart", return_value=can_restart),
                mock.patch.object(wd, "notify_slack", return_value=True),
                mock.patch.object(wd, "record_restart"),
                mock.patch.object(wd, "save_state"),
                mock.patch.object(wd, "append_log"),
                mock.patch.object(wd, "collect_active_runs", return_value=[]),
                mock.patch.object(wd, "read_health", return_value=None),
            ]
            for p in patches:
                stack.enter_context(p)
            kick = stack.enter_context(
                mock.patch.object(wd, "restart_gateway", return_value=True)
            )
            outcome = wd.tick(root)
            return outcome, kick

    def test_streak_triggers_restart(self) -> None:
        state = {"restart_attempts": []}
        auth_fail = {"marker": "a1", "epoch": 1.0, "ts": "t", "count": 3}
        outcome, kick = self._tick(state, auth_fail)
        self.assertEqual(outcome, "restarted")
        kick.assert_called_once()
        self.assertEqual(state["last_auth_failure_marker"], "a1")

    def test_single_blip_below_threshold_no_restart(self) -> None:
        state = {"restart_attempts": []}
        auth_fail = {"marker": "a1", "epoch": 1.0, "ts": "t", "count": 1}
        outcome, kick = self._tick(state, auth_fail)
        kick.assert_not_called()

    def test_same_marker_not_restarted_twice(self) -> None:
        state = {"restart_attempts": [], "last_auth_failure_marker": "a1"}
        auth_fail = {"marker": "a1", "epoch": 1.0, "ts": "t", "count": 5}
        outcome, kick = self._tick(state, auth_fail)
        kick.assert_not_called()

    def test_budget_exhausted_escalates_to_relogin_message(self) -> None:
        state = {"restart_attempts": []}
        auth_fail = {"marker": "a2", "epoch": 1.0, "ts": "t", "count": 4}
        outcome, kick = self._tick(state, auth_fail, can_restart=False)
        self.assertEqual(outcome, "stuck")
        kick.assert_not_called()


class TestWatchdogLiveness(unittest.TestCase):
    def test_recent_tick_is_ok(self) -> None:
        self.assertEqual(
            wd.watchdog_liveness(1_000_000.0, 1_000_030.0, interval_sec=60, factor=4), "ok"
        )

    def test_old_tick_is_stale(self) -> None:
        self.assertEqual(
            wd.watchdog_liveness(1_000_000.0, 1_000_400.0, interval_sec=60, factor=4), "stale"
        )

    def test_no_tick_is_unknown(self) -> None:
        self.assertEqual(wd.watchdog_liveness(None, 1_000_000.0, interval_sec=60), "unknown")


class TestOverallStatus(unittest.TestCase):
    def test_down_when_gateway_not_loaded(self) -> None:
        self.assertEqual(wd.overall_status({"gateway_loaded": False}), "down")

    def test_down_when_abandoned(self) -> None:
        self.assertEqual(
            wd.overall_status({"gateway_loaded": True, "gateway_healthy": True, "abandoned": True}),
            "down",
        )

    def test_degraded_when_unhealthy(self) -> None:
        self.assertEqual(
            wd.overall_status({"gateway_loaded": True, "gateway_healthy": False}), "degraded"
        )

    def test_degraded_when_watchdog_stale(self) -> None:
        self.assertEqual(
            wd.overall_status(
                {"gateway_loaded": True, "gateway_healthy": True, "watchdog_liveness": "stale"}
            ),
            "degraded",
        )

    def test_degraded_when_channel_down(self) -> None:
        self.assertEqual(
            wd.overall_status(
                {"gateway_loaded": True, "gateway_healthy": True, "channels_down": ["slack"]}
            ),
            "degraded",
        )

    def test_ok_when_all_green(self) -> None:
        self.assertEqual(
            wd.overall_status(
                {
                    "gateway_loaded": True,
                    "gateway_healthy": True,
                    "watchdog_liveness": "ok",
                    "channels_down": [],
                }
            ),
            "ok",
        )


class TestStatusSummary(unittest.TestCase):
    def test_summary_and_format(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "read_state", return_value={"restart_attempts": [], "last_tick_epoch": __import__("time").time(), "last_ok_epoch": __import__("time").time()}), \
                mock.patch.object(wd, "is_gateway_loaded", return_value=True), \
                mock.patch.object(wd, "is_gateway_healthy", return_value=True), \
                mock.patch.object(wd, "is_watchdog_loaded", return_value=True), \
                mock.patch.object(wd, "configured_but_down_channels", return_value=[]):
                summary = wd.status_summary(root)
            self.assertEqual(summary["status"], "ok")
            text = wd.format_status_summary(summary)
            self.assertIn("正常", text)
            self.assertIn("gateway", text)


class TestEnsureWatchdogInstalled(unittest.TestCase):
    def test_regenerates_plist_when_missing_then_loads(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plist = root / "agent.plist"
            with mock.patch.object(wd, "render_watchdog_plist", return_value="<plist/>"), \
                mock.patch.object(wd, "is_watchdog_loaded", return_value=False), \
                mock.patch.object(wd, "_run") as run:
                ok = wd.ensure_watchdog_installed(root, plist=plist)
            self.assertTrue(ok)
            self.assertTrue(plist.is_file())
            self.assertEqual(plist.read_text(), "<plist/>")
            run.assert_called_once()  # launchctl load

    def test_no_write_when_loaded_and_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plist = root / "agent.plist"
            plist.write_text("<existing/>")
            with mock.patch.object(wd, "is_watchdog_loaded", return_value=True), \
                mock.patch.object(wd, "_run") as run:
                ok = wd.ensure_watchdog_installed(root, plist=plist)
            self.assertTrue(ok)
            self.assertEqual(plist.read_text(), "<existing/>")  # untouched
            run.assert_not_called()

    def test_render_substitutes_placeholders(self) -> None:
        # Uses the real template shipped in scripts/.
        out = wd.render_watchdog_plist()
        self.assertNotIn("{{SKILLS_DIR}}", out)
        self.assertNotIn("{{PYTHON3}}", out)
        self.assertNotIn("{{PATH}}", out)


class TestPreferredWatchdogPython(unittest.TestCase):
    """v32 FX5 — the plist must bake the venv python when present."""

    def test_prefers_executable_venv_python(self) -> None:
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            venv_py = root / "jp-form-outreach" / ".venv" / "bin" / "python3"
            venv_py.parent.mkdir(parents=True)
            venv_py.write_text("#!/bin/sh\n")
            os.chmod(venv_py, 0o755)
            self.assertEqual(wd.preferred_watchdog_python(root), str(venv_py))

    def test_falls_back_to_sys_executable(self) -> None:
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                wd.preferred_watchdog_python(Path(tmp)), sys.executable
            )

    def test_non_executable_venv_python_is_skipped(self) -> None:
        import os
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            venv_py = root / "jp-form-outreach" / ".venv" / "bin" / "python3"
            venv_py.parent.mkdir(parents=True)
            venv_py.write_text("")
            os.chmod(venv_py, 0o644)
            self.assertEqual(wd.preferred_watchdog_python(root), sys.executable)


if __name__ == "__main__":
    unittest.main()
