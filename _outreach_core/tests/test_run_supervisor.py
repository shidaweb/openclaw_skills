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


class TestTargetAbort(unittest.TestCase):
    def test_limit_disabled(self):
        self.assertFalse(RS.should_abort_target(0, 10**9, 0))
        self.assertFalse(RS.should_abort_target(0, 10**9, -1))

    def test_below_limit_continues(self):
        self.assertFalse(RS.should_abort_target(100.0, 279.9, 180))

    def test_equal_or_over_limit_aborts(self):
        self.assertTrue(RS.should_abort_target(100.0, 280.0, 180))
        self.assertTrue(RS.should_abort_target(100.0, 281.0, 180))

    def test_bad_values_are_safe(self):
        self.assertFalse(RS.should_abort_target(None, 200.0, 180))
        self.assertFalse(RS.should_abort_target(100.0, None, 180))
        self.assertFalse(RS.should_abort_target(100.0, 200.0, "bad"))


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


class TestGiveUpProblem(unittest.TestCase):
    def test_none_for_non_give_up(self):
        self.assertIsNone(
            RS.build_give_up_problem(
                RS.ACTION_RESTART_CRASH, exit_code=1, activity_age_sec=None
            )
        )

    def test_stalled_payload(self):
        payload = RS.build_give_up_problem(
            RS.ACTION_GIVE_UP_STALLED,
            exit_code=None,
            activity_age_sec=999,
            recent_outcomes=[
                {"outcome": "network_error"},
                {"payload": {"outcome": "validation_stuck"}},
                {"payload": {"outcome": "network_error"}},
            ],
        )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["kind"], "run_give_up_stalled")
        self.assertEqual(payload["detail"]["recent_outcomes"]["network_error"], 2)
        self.assertIn("999", payload["detail"]["root_cause"])

    def test_crash_payload(self):
        payload = RS.build_give_up_problem(
            RS.ACTION_GIVE_UP_CRASH,
            exit_code=2,
            activity_age_sec=None,
            recent_outcomes=[],
        )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["kind"], "run_give_up_crash")
        self.assertEqual(payload["detail"]["exit_code"], 2)


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


class TestPerBriefState(unittest.TestCase):
    """v32 FX2 — restart budgets keyed per skill+brief, atomic writes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_state_key_shapes(self):
        self.assertEqual(
            RS.state_key("jp-form-outreach", "torana-line-crm"),
            "jp-form-outreach--torana-line-crm",
        )
        self.assertIsNone(RS.state_key("jp-form-outreach", None))
        self.assertIsNone(RS.state_key("jp-form-outreach", ""))
        self.assertIsNone(RS.state_key("", "brief"))
        # path-hostile chars sanitized
        self.assertNotIn("/", RS.state_key("jp-form-outreach", "a/b../c"))

    def test_keyed_path_vs_legacy(self):
        keyed = RS.state_path(self.dir, "jp-form-outreach--b1")
        self.assertEqual(keyed.parent.name, "run_supervisor")
        legacy = RS.state_path(self.dir, None)
        self.assertEqual(legacy.name, "run_supervisor.json")
        self.assertEqual(legacy.parent, self.dir)

    def test_budget_isolation_between_briefs(self):
        # 3 restarts on brief A must NOT count against brief B — the
        # production failure was brief C giving up on its first crash
        # because A+B had exhausted the shared budget.
        key_a = RS.state_key("jp-form-outreach", "brief-a")
        key_b = RS.state_key("jp-form-outreach", "brief-b")
        state_a = RS.load_state(self.dir, key_a)
        for _ in range(RS.MAX_RESTARTS):
            RS.record_restart(state_a, "crash")
        RS.save_state(self.dir, state_a, key_a)
        self.assertFalse(RS.can_restart(RS.load_state(self.dir, key_a)))
        self.assertTrue(RS.can_restart(RS.load_state(self.dir, key_b)))

    def test_keyed_round_trip_and_atomicity(self):
        key = RS.state_key("jp-form-outreach", "brief-x")
        state = RS.new_state()
        RS.record_restart(state, "stalled")
        RS.save_state(self.dir, state, key)
        loaded = RS.load_state(self.dir, key)
        self.assertEqual(len(loaded["restart_attempts"]), 1)
        # no temp litter left behind by the atomic write
        litter = list((self.dir / "run_supervisor").glob(".rs_*"))
        self.assertEqual(litter, [])

    def test_legacy_file_untouched_by_keyed_saves(self):
        key = RS.state_key("jp-form-outreach", "brief-y")
        RS.save_state(self.dir, RS.new_state(), key)
        self.assertFalse((self.dir / "run_supervisor.json").exists())


if __name__ == "__main__":
    unittest.main()


class TestEffectiveActivityAge(unittest.TestCase):
    """v32 FX1 — young-child guard."""

    def test_young_child_is_never_stalled(self):
        # files carry the PREVIOUS run's mtimes right after a relaunch
        self.assertIsNone(RS.effective_activity_age(10**6, 30))
        self.assertIsNone(RS.effective_activity_age(10**6, RS.STALL_SEC - 1))

    def test_old_child_trusts_file_age(self):
        self.assertEqual(
            RS.effective_activity_age(1200, RS.STALL_SEC + 1), 1200
        )

    def test_unknown_ages_pass_through(self):
        self.assertIsNone(RS.effective_activity_age(None, RS.STALL_SEC + 1))
        self.assertEqual(RS.effective_activity_age(1200, None), 1200)

    def test_stall_sec_default_raised_to_900(self):
        # env override still respected; default must cover a legitimately
        # slow single lead (adaptive warmup + Opus retries > 7 min)
        self.assertEqual(RS._env_int("_MISSING_ENV_", 900), 900)
        self.assertGreaterEqual(RS.STALL_SEC, 900)
