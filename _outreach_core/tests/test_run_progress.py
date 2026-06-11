"""Tests for the live run-progress snapshot (v22)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _outreach_core import run_progress as rp


class TestSnapshotIO(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.dir = Path(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_start_then_read(self):
        rp.start(self.dir, "send", 30)
        snap = rp.read(self.dir)
        self.assertEqual(snap["stage"], "send")
        self.assertEqual(snap["total"], 30)
        self.assertEqual(snap["processed"], 0)
        self.assertEqual(snap["status"], "running")

    def test_bump_counts_by_outcome(self):
        rp.start(self.dir, "send", 5)
        rp.bump(self.dir, outcome="sent", name="A")
        rp.bump(self.dir, outcome="queued", name="B")
        rp.bump(self.dir, outcome="skipped", name="C")
        rp.bump(self.dir, outcome="crashed", name="D")
        rp.bump(self.dir, outcome="done", name="E")
        snap = rp.read(self.dir)
        self.assertEqual(snap["processed"], 5)
        self.assertEqual(snap["sent"], 1)
        self.assertEqual(snap["needs_attention"], 2)   # queued + crashed
        self.assertEqual(snap["skipped"], 2)           # skipped + done
        self.assertEqual(snap["current"], "E")

    def test_finish_sets_status(self):
        rp.start(self.dir, "send", 1)
        rp.bump(self.dir, outcome="sent", name="A")
        rp.finish(self.dir, status="done")
        snap = rp.read(self.dir)
        self.assertEqual(snap["status"], "done")
        self.assertIsNone(snap["current"])
        self.assertIn("finished_at", snap)

    def test_read_missing_is_none(self):
        self.assertIsNone(rp.read(self.dir))

    def test_bump_without_start_is_safe(self):
        rp.bump(self.dir, outcome="sent", name="X")  # must not raise
        snap = rp.read(self.dir)
        self.assertEqual(snap["processed"], 1)
        self.assertEqual(snap["sent"], 1)


class TestFormatting(unittest.TestCase):
    def test_format_none(self):
        self.assertIn("見つかりません", rp.format_summary(None))

    def test_format_running_has_counts_and_current(self):
        snap = {
            "stage": "send", "total": 30, "processed": 12, "sent": 9,
            "skipped": 2, "needs_attention": 1, "status": "running",
            "current": "株式会社X",
            "started_at": (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(),
        }
        out = rp.format_summary(snap)
        self.assertIn("send 12/30", out)
        self.assertIn("送信 9", out)
        self.assertIn("要対応 1", out)
        self.assertIn("株式会社X", out)
        self.assertIn("残り目安", out)

    def test_eta_none_when_not_computable(self):
        self.assertIsNone(rp.eta_seconds({"processed": 0, "total": 10}))
        self.assertIsNone(rp.eta_seconds({"processed": 10, "total": 10,
                                          "started_at": datetime.now(timezone.utc).isoformat()}))

    def test_eta_positive(self):
        start = datetime.now(timezone.utc) - timedelta(seconds=60)
        eta = rp.eta_seconds({"processed": 6, "total": 30,
                              "started_at": start.isoformat()})
        # 60s/6 = 10s per item × 24 remaining = 240s
        self.assertTrue(200 <= eta <= 280)


if __name__ == "__main__":
    unittest.main()
