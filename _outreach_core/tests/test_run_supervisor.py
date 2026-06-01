"""Tests for run-level stall detection + bounded restart (run_supervisor.py, v6 §15-B)."""

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _outreach_core import run_supervisor as RS  # noqa: E402


class TestIsStalled(unittest.TestCase):
    def test_none_is_not_stalled(self):
        self.assertFalse(RS.is_stalled(None, 100))

    def test_below_threshold(self):
        self.assertFalse(RS.is_stalled(50, 100))

    def test_at_or_above_threshold(self):
        self.assertTrue(RS.is_stalled(100, 100))
        self.assertTrue(RS.is_stalled(999, 100))


class TestLatestActivityAge(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_none_when_no_paths_exist(self):
        self.assertIsNone(RS.latest_activity_age_sec([self.dir / "nope.log"]))

    def test_uses_most_recent(self):
        old = self.dir / "old.log"
        new = self.dir / "new.json"
        old.write_text("x")
        new.write_text("y")
        now = time.time()
        os.utime(old, (now - 500, now - 500))
        os.utime(new, (now - 10, now - 10))
        age = RS.latest_activity_age_sec([old, new], now=now)
        self.assertLess(age, 30)  # newest file is ~10s old


class TestRestartAccounting(unittest.TestCase):
    def test_window_excludes_old_attempts(self):
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        state = {"restart_attempts": [
            {"at": (now - timedelta(minutes=60)).isoformat(), "outcome": "crash"},  # outside 30m
            {"at": (now - timedelta(minutes=5)).isoformat(), "outcome": "crash"},   # inside
        ]}
        self.assertEqual(RS.recent_restart_count(state, now=now, window_min=30), 1)

    def test_can_restart_until_max(self):
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        state = RS.new_state()
        for _ in range(RS.MAX_RESTARTS - 1):
            RS.record_restart(state, "crash", now=now)
        self.assertTrue(RS.can_restart(state, now=now))
        RS.record_restart(state, "crash", now=now)
        self.assertFalse(RS.can_restart(state, now=now))

    def test_old_restarts_free_up_budget(self):
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        state = {"restart_attempts": [
            {"at": (now - timedelta(minutes=90)).isoformat(), "outcome": "crash"}
            for _ in range(RS.MAX_RESTARTS)
        ]}
        # all outside window → budget restored
        self.assertTrue(RS.can_restart(state, now=now))


class TestDecide(unittest.TestCase):
    def setUp(self):
        self.state = RS.new_state()

    def test_continue_when_active(self):
        self.assertEqual(
            RS.decide(child_alive=True, exit_code=None, activity_age_sec=30, state=self.state),
            RS.ACTION_CONTINUE,
        )

    def test_unknown_activity_continues(self):
        self.assertEqual(
            RS.decide(child_alive=True, exit_code=None, activity_age_sec=None, state=self.state),
            RS.ACTION_CONTINUE,
        )

    def test_restart_when_stalled(self):
        self.assertEqual(
            RS.decide(child_alive=True, exit_code=None, activity_age_sec=10**6, state=self.state),
            RS.ACTION_RESTART_STALLED,
        )

    def test_succeeded(self):
        self.assertEqual(
            RS.decide(child_alive=False, exit_code=0, activity_age_sec=None, state=self.state),
            RS.ACTION_SUCCEEDED,
        )

    def test_restart_on_crash(self):
        self.assertEqual(
            RS.decide(child_alive=False, exit_code=1, activity_age_sec=None, state=self.state),
            RS.ACTION_RESTART_CRASH,
        )

    def test_give_up_after_budget_exhausted(self):
        now = datetime.now(timezone.utc)
        for _ in range(RS.MAX_RESTARTS):
            RS.record_restart(self.state, "crash", now=now)
        self.assertEqual(
            RS.decide(child_alive=False, exit_code=1, activity_age_sec=None, state=self.state, now=now),
            RS.ACTION_GIVE_UP_CRASH,
        )
        self.assertEqual(
            RS.decide(child_alive=True, exit_code=None, activity_age_sec=10**6, state=self.state, now=now),
            RS.ACTION_GIVE_UP_STALLED,
        )

    def test_is_restart_helper(self):
        self.assertTrue(RS.is_restart(RS.ACTION_RESTART_STALLED))
        self.assertTrue(RS.is_restart(RS.ACTION_RESTART_CRASH))
        self.assertFalse(RS.is_restart(RS.ACTION_CONTINUE))
        self.assertFalse(RS.is_restart(RS.ACTION_SUCCEEDED))


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_roundtrip(self):
        state = RS.new_state()
        RS.record_restart(state, "stalled")
        RS.save_state(self.dir, state)
        loaded = RS.load_state(self.dir)
        self.assertEqual(len(loaded["restart_attempts"]), 1)

    def test_missing_is_blank(self):
        self.assertEqual(RS.load_state(self.dir), {"restart_attempts": []})

    def test_corrupt_is_safe(self):
        RS.state_path(self.dir).write_text("{bad", encoding="utf-8")
        self.assertEqual(RS.load_state(self.dir), {"restart_attempts": []})


if __name__ == "__main__":
    unittest.main()
