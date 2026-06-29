"""Watchdog tick refreshes the per-host heartbeat + healthcheck stale CLI.

Production observation 2026-06-30: a host appeared dark for 9 hours while
its watchdog was actively running every 60 seconds, because the watchdog
tick did not touch ``data/system_health/<host>.json``. That file was only
refreshed when a Slack command arrived or a run was in progress, so an
idle but healthy host looked indistinguishable from a dead one.

These tests pin two complementary fixes:

* :func:`_outreach_core.helpers.watchdog.tick` now calls
  :func:`healthcheck.write_heartbeat` at the top of every tick.
* ``./healthcheck stale [--threshold-sec N]`` enumerates all hosts'
  heartbeats and exits non-zero when any host is older than the threshold,
  giving cron / Slack glue a precise hook for "primary host went dark".
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.helpers import healthcheck as hc  # noqa: E402
from _outreach_core.helpers import watchdog as wd  # noqa: E402


def _no_self_heal():
    return mock.patch.object(wd, "_self_heal")


class TestWatchdogRefreshesHeartbeat(unittest.TestCase):
    """Every tick must touch system_health/<host>.json so an idle host
    cannot appear dark to other observers."""

    def test_tick_writes_heartbeat_in_happy_path(self) -> None:
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
                mock.patch.object(wd, "append_log"), \
                mock.patch.object(hc, "write_heartbeat") as wh:
                wd.tick(root)
            wh.assert_called_once()
            # The watchdog passes its skills_root through so the heartbeat
            # lands in the right tree.
            args, _ = wh.call_args
            self.assertEqual(args[0], root)

    def test_tick_writes_heartbeat_even_on_error_path(self) -> None:
        """A failing recovery path must still leave a fresh heartbeat so
        the operator can see "watchdog ticked at HH:MM but couldn't fix it"
        rather than "host went dark"."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(wd, "read_state", return_value={"restart_attempts": []}), \
                _no_self_heal(), \
                mock.patch.object(wd, "is_gateway_loaded", return_value=False), \
                mock.patch.object(wd, "start_gateway", return_value=True), \
                mock.patch.object(wd, "configured_but_down_channels", return_value=[]), \
                mock.patch.object(wd, "notify_slack"), \
                mock.patch.object(wd, "save_state"), \
                mock.patch.object(wd, "append_log"), \
                mock.patch.object(wd, "record_restart"), \
                mock.patch.object(hc, "write_heartbeat") as wh:
                wd.tick(root)
            wh.assert_called_once()

    def test_tick_swallows_heartbeat_failure(self) -> None:
        """If write_heartbeat raises, tick must still complete — heartbeat
        refresh is observability, not a critical path."""
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
                mock.patch.object(wd, "append_log"), \
                mock.patch.object(hc, "write_heartbeat", side_effect=OSError("disk full")):
                outcome = wd.tick(root)
            # The gateway is healthy, so the verdict is still "ok" — the
            # exception inside the heartbeat refresh did not bubble.
            self.assertEqual(outcome, "ok")


def _seed_heartbeat(
    root: Path,
    host: str,
    *,
    age_sec: int,
    open_needs_attention: int = 0,
    active_runs: list | None = None,
) -> Path:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_sec)).isoformat()
    path = hc.system_health_dir(root) / f"{host}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "host": host,
        "ts": ts,
        "openclaw_pid": 4242,
        "slack_connected": None,
        "last_command_at": ts,
        "active_runs": active_runs or [],
        "open_needs_attention_count": open_needs_attention,
        "doorman_version": hc.DOORMAN_VERSION,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestCollectHostHealth(unittest.TestCase):
    def test_returns_all_hosts_sorted_oldest_first(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_heartbeat(root, "fresh", age_sec=30)
            _seed_heartbeat(root, "stale", age_sec=4000)
            _seed_heartbeat(root, "ancient", age_sec=90_000)
            rows = hc.collect_host_health(root)
            hosts = [r["host"] for r in rows]
            self.assertEqual(hosts, ["ancient", "stale", "fresh"])
            self.assertEqual(rows[-1]["host"], "fresh")
            self.assertGreaterEqual(rows[0]["age_sec"], rows[1]["age_sec"])
            self.assertGreaterEqual(rows[1]["age_sec"], rows[2]["age_sec"])

    def test_handles_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # No system_health/ directory at all.
            self.assertEqual(hc.collect_host_health(root), [])

    def test_skips_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_heartbeat(root, "good", age_sec=10)
            bad = hc.system_health_dir(root) / "broken.json"
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_text("{ not json", encoding="utf-8")
            rows = hc.collect_host_health(root)
            self.assertEqual([r["host"] for r in rows], ["good"])


class TestFindStaleHosts(unittest.TestCase):
    def test_returns_only_rows_older_than_threshold(self) -> None:
        rows = [
            {"host": "fresh", "age_sec": 30},
            {"host": "stale", "age_sec": 1200},
            {"host": "ancient", "age_sec": 90_000},
        ]
        stale = hc.find_stale_hosts(rows, threshold_sec=600)
        self.assertEqual(
            sorted(r["host"] for r in stale),
            ["ancient", "stale"],
        )

    def test_treats_missing_age_as_stale(self) -> None:
        # A heartbeat file whose ts cannot be parsed counts as stale.
        rows = [{"host": "broken", "age_sec": None}]
        stale = hc.find_stale_hosts(rows, threshold_sec=600)
        self.assertEqual(len(stale), 1)


class TestStaleCli(unittest.TestCase):
    def test_exit_code_2_when_any_host_stale(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_heartbeat(root, "fresh", age_sec=30)
            _seed_heartbeat(root, "stale", age_sec=4000)
            with mock.patch.object(hc, "SKILLS_ROOT", root):
                import argparse
                exit_code = hc.cmd_stale(argparse.Namespace(threshold_sec=600))
            self.assertEqual(exit_code, 2)

    def test_exit_code_0_when_all_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_heartbeat(root, "fresh1", age_sec=30)
            _seed_heartbeat(root, "fresh2", age_sec=100)
            with mock.patch.object(hc, "SKILLS_ROOT", root):
                import argparse
                exit_code = hc.cmd_stale(argparse.Namespace(threshold_sec=600))
            self.assertEqual(exit_code, 0)

    def test_format_stale_report_contains_flags_for_each_host(self) -> None:
        rows = [
            {"host": "fresh", "age_sec": 30, "active_runs": [], "ts": "x"},
            {"host": "stale", "age_sec": 4000, "active_runs": [], "ts": "y"},
        ]
        stale = hc.find_stale_hosts(rows, threshold_sec=600)
        report = hc.format_stale_report(rows, stale, threshold_sec=600)
        self.assertIn("🔴 stale", report)
        self.assertIn("🟢 fresh", report)
        self.assertIn("threshold=600s", report)
        self.assertIn("停止疑い", report)


if __name__ == "__main__":
    unittest.main()
