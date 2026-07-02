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
        rp.start(self.dir, "send", 6)
        rp.bump(self.dir, outcome="sent", name="A")
        rp.bump(self.dir, outcome="queued", name="B")
        rp.bump(self.dir, outcome="skipped", name="C")
        rp.bump(self.dir, outcome="crashed", name="D")
        rp.bump(self.dir, outcome="done", name="E")
        rp.bump(self.dir, outcome="timed_out", name="F")
        snap = rp.read(self.dir)
        self.assertEqual(snap["processed"], 6)
        self.assertEqual(snap["sent"], 1)
        self.assertEqual(snap["needs_attention"], 3)   # queued + crashed + timed_out
        self.assertEqual(snap["skipped"], 1)           # skipped only
        # v31 §WS8c — "done" (= send attempted, verify not certified) gets its
        # own bucket instead of inflating the skipped card.
        self.assertEqual(snap["filled_only"], 1)       # done
        self.assertEqual(snap["current"], "F")

    def test_filled_only_summary_and_html(self):
        # v31 §WS8c — surfaced in the text summary and the dashboard cards.
        snap = {
            "stage": "send", "total": 10, "processed": 5, "sent": 3,
            "filled_only": 2, "skipped": 0, "needs_attention": 0,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        out = rp.format_summary(snap)
        self.assertIn("入力のみ 2", out)
        html = rp.render_html(snap)
        self.assertIn("入力のみ（検証未確定）", html)
        # pre-v31 snapshots (no filled_only key) keep the 4-part summary
        legacy = dict(snap)
        legacy.pop("filled_only")
        self.assertNotIn("入力のみ", rp.format_summary(legacy))

    def test_finish_sets_status(self):
        rp.start(self.dir, "send", 1)
        rp.bump(self.dir, outcome="sent", name="A")
        rp.finish(self.dir, status="done")
        snap = rp.read(self.dir)
        self.assertEqual(snap["status"], "done")
        self.assertIsNone(snap["current"])
        self.assertIn("finished_at", snap)

    def test_transition_reopens_progress_and_preserves_send_phase(self):
        rp.start(self.dir, "send", 2, brief="acme")
        rp.bump(self.dir, outcome="sent", name="A")
        rp.bump(self.dir, outcome="skipped", name="B")
        rp.finish(self.dir)
        rp.transition(self.dir, "resolve", 1, brief="acme")
        snap = rp.read(self.dir)
        self.assertEqual(snap["status"], "running")
        self.assertEqual(snap["stage"], "resolve")
        self.assertEqual(snap["processed"], 0)
        self.assertEqual(snap["brief"], "acme")
        self.assertEqual(snap["phases"][-1]["stage"], "send")
        self.assertEqual(snap["phases"][-1]["sent"], 1)

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
        self.assertIn("送信 12/30", out)
        self.assertIn("送信OK 9", out)
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


class TestRenderHtml(unittest.TestCase):
    def _running(self):
        return {
            "stage": "send", "total": 30, "processed": 12, "sent": 9,
            "skipped": 2, "needs_attention": 1, "status": "running",
            "current": "株式会社サンプル",
            "started_at": (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def test_none_snapshot_renders_placeholder(self):
        html = rp.render_html(None)
        self.assertIn("まだ実行データがありません", html)
        self.assertNotIn("http-equiv='refresh'", html)

    def test_running_has_refresh_and_numbers(self):
        html = rp.render_html(self._running())
        self.assertIn("http-equiv='refresh'", html)   # auto-reload while running
        self.assertIn("12 / 30", html)
        self.assertIn("株式会社サンプル", html)
        self.assertIn(">9<", html)                     # sent count card
        self.assertIn("width:40%", html)               # 12/30 = 40%

    def test_finished_has_no_refresh(self):
        snap = self._running()
        snap["status"] = "done"
        snap["finished_at"] = datetime.now(timezone.utc).isoformat()
        html = rp.render_html(snap)
        self.assertNotIn("http-equiv='refresh'", html)
        self.assertIn("自動更新停止", html)

    def test_html_escaping_of_current(self):
        snap = self._running()
        snap["current"] = "<script>x</script>&co"
        html = rp.render_html(snap)
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_write_html_creates_file(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        try:
            rp.start(d, "send", 3)
            rp.bump(d, outcome="sent", name="A")
            self.assertTrue(rp.html_path(d).is_file())
            self.assertIn("send", rp.html_path(d).read_text(encoding="utf-8"))
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
