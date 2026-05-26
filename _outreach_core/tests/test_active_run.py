"""active_run.lock (v4 §14-H)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _outreach_core.active_run import (
    ActiveRunError,
    acquire_lock,
    campaign_run_lock,
    is_lock_alive,
    read_lock,
    remove_lock,
)


class TestActiveRun(unittest.TestCase):
    def test_acquire_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            acquire_lock(
                data,
                run_id="r1",
                stage="campaign",
                total_targets=5,
                skill="jp-form-outreach",
                brief_id="test",
            )
            lock = read_lock(data)
            assert lock is not None
            self.assertEqual(lock["run_id"], "r1")
            self.assertTrue(is_lock_alive(lock))
            remove_lock(data)
            self.assertIsNone(read_lock(data))

    def test_blocks_second_live_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            acquire_lock(
                data,
                run_id="r1",
                stage="campaign",
                total_targets=1,
                skill="jp-form-outreach",
                brief_id="test",
            )
            with self.assertRaises(ActiveRunError):
                acquire_lock(
                    data,
                    run_id="r2",
                    stage="campaign",
                    total_targets=1,
                    skill="jp-form-outreach",
                    brief_id="test",
                )
            remove_lock(data)

    def test_stale_lock_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            acquire_lock(
                data,
                run_id="old",
                stage="campaign",
                total_targets=1,
                skill="jp-form-outreach",
                brief_id="test",
            )
            lock = read_lock(data)
            assert lock is not None
            lock["pid"] = 999999999
            from _outreach_core.active_run import write_lock

            write_lock(data, lock)
            acquire_lock(
                data,
                run_id="new",
                stage="campaign",
                total_targets=1,
                skill="jp-form-outreach",
                brief_id="test",
            )
            self.assertEqual(read_lock(data)["run_id"], "new")

    def test_campaign_context_manager(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            with campaign_run_lock(
                data,
                run_id="ctx",
                brief_id="b",
                skill="s",
                total_targets=0,
            ):
                self.assertTrue((data / "active_run.lock").is_file())
            self.assertFalse((data / "active_run.lock").is_file())


if __name__ == "__main__":
    unittest.main()
