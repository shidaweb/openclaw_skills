"""v32 FX4 — gateway-outage grace gate (pure decision half).

Signature strings are the EXACT production log fingerprints from the 10×3
investigation (data/job_logs/*): GatewayClientRequestError tab-not-found,
GatewayTransportError 25000ms timeout, Playwright page-closed and
connectOverCDP timeouts.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _outreach_core import gateway_gate as gg  # noqa: E402


class TestSignatureMatching(unittest.TestCase):
    def test_production_error_strings_match(self):
        production = (
            'GatewayClientRequestError: tab not found: browser tab "254DF840" not found',
            "GatewayTransportError: gateway timeout after 25000ms",
            "Error: Page closed before browser action completed.",
            "browserType.connectOverCDP: Timeout 9000ms exceeded",
            "connect ECONNREFUSED 127.0.0.1:18789",
        )
        for text in production:
            self.assertTrue(gg.is_gateway_error_text(text), text)

    def test_case_insensitive(self):
        self.assertTrue(gg.is_gateway_error_text("TAB NOT FOUND: t494"))

    def test_non_gateway_errors_do_not_match(self):
        for text in (
            "KeyError: 'form_fields'",
            "validation_stuck: 必須項目を自動修復できません",
            "TimeoutError: lead_soft_timeout after 300s",
            "",
            None,
        ):
            self.assertFalse(gg.is_gateway_error_text(text), repr(text))


class TestWaitPolicy(unittest.TestCase):
    def test_keeps_waiting_within_budget(self):
        self.assertTrue(gg.should_keep_waiting(1000.0, 1000.0 + 10, 60))
        self.assertTrue(gg.should_keep_waiting(1000.0, 1000.0 + 59.9, 60))

    def test_stops_at_budget(self):
        self.assertFalse(gg.should_keep_waiting(1000.0, 1000.0 + 60, 60))
        self.assertFalse(gg.should_keep_waiting(1000.0, 1000.0 + 3600, 60))

    def test_bad_values_stop_waiting(self):
        self.assertFalse(gg.should_keep_waiting(None, 1000.0, 60))

    def test_default_budget_covers_watchdog_abandoned_cycle(self):
        # watchdog worst case: burn 3-restart budget (~3 min) + 30-min
        # abandoned cooldown + next kickstart ≈ 34 min. The default wait
        # must clear that with margin.
        self.assertGreaterEqual(gg.WAIT_SEC_DEFAULT, 35 * 60)

    def test_env_overrides(self):
        with mock.patch.dict("os.environ", {"DOORMAN_GATEWAY_WAIT_SEC": "120",
                                            "DOORMAN_GATEWAY_POLL_SEC": "5"}):
            self.assertEqual(gg.wait_sec(), 120)
            self.assertEqual(gg.poll_sec(), 5)
        with mock.patch.dict("os.environ", {"DOORMAN_GATEWAY_WAIT_SEC": "junk"}):
            self.assertEqual(gg.wait_sec(), gg.WAIT_SEC_DEFAULT)

    def test_exit_code_constant(self):
        # 5 must stay distinct from the supervisor's existing terminal codes
        # (2=usage, 3=active-run, 4=quality gate).
        self.assertEqual(gg.EXIT_GATEWAY_UNAVAILABLE, 5)


class TestWaitForGatewayWiring(unittest.TestCase):
    """run.py side: recovery / timeout paths with a stubbed browser."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "jp-form-outreach"))
        import run
        # NB: attribute must not be named "run" — TestCase.run() is load-bearing.
        cls.run_mod = run

    def test_alive_gateway_returns_immediately(self):
        run = self.run_mod
        with mock.patch.object(run, "_list_tabs_payload", return_value={"tabs": []}), \
                mock.patch.object(run.time, "sleep") as slept:
            self.assertTrue(run._wait_for_gateway(None))
        slept.assert_not_called()

    def test_recovers_after_polls_and_notes_heartbeat(self):
        run = self.run_mod
        # down for 2 probes, then back
        payloads = iter([None, None, None, {"tabs": []}])
        hb = mock.Mock()
        with mock.patch.object(run, "_list_tabs_payload",
                               side_effect=lambda: next(payloads)), \
                mock.patch.object(run, "_emit_event"), \
                mock.patch("_outreach_core.notify.post", return_value=True), \
                mock.patch.object(run.time, "sleep"):
            self.assertTrue(run._wait_for_gateway(hb, reason="test"))
        # each poll wrote a main-thread forward-progress note (FX1 contract:
        # "waiting for the gateway" must read as alive, not stalled)
        self.assertGreaterEqual(hb.note.call_count, 1)
        self.assertIn("gateway復旧待ち", hb.note.call_args.args[0])

    def test_timeout_raises_gateway_unavailable(self):
        run = self.run_mod
        clock = {"t": 1000.0}

        def _fake_time():
            clock["t"] += 30.0
            return clock["t"]

        with mock.patch.object(run, "_list_tabs_payload", return_value=None), \
                mock.patch.object(run, "_emit_event"), \
                mock.patch("_outreach_core.notify.post", return_value=True), \
                mock.patch.object(run.time, "sleep"), \
                mock.patch.object(run.time, "time", side_effect=_fake_time), \
                mock.patch.dict("os.environ", {"DOORMAN_GATEWAY_WAIT_SEC": "60"}):
            with self.assertRaises(gg.GatewayUnavailableError):
                run._ensure_gateway_or_raise(None, reason="test")


if __name__ == "__main__":
    unittest.main()
