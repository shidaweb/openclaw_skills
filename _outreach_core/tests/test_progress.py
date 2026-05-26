"""Heartbeat posts only when --heartbeat slack and webhook configured."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.progress import HeartbeatSession


class TestProgress(unittest.TestCase):
    def test_writes_current_task_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp)
            data = skill / "data"
            data.mkdir()
            hb = HeartbeatSession(skill, "send", 2, heartbeat=None, data_dir=data)
            hb.start()
            hb.tick(1, "first")
            hb.end()
            lines = (data / "current_task.jsonl").read_text().strip().splitlines()
            self.assertGreaterEqual(len(lines), 2)

    def test_heartbeat_calls_notify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp)
            data = skill / "data"
            data.mkdir()
            with mock.patch("_outreach_core.notify.post", return_value=True) as post:
                with mock.patch(
                    "_outreach_core.progress.heartbeat_interval_sec", return_value=1
                ):
                    hb = HeartbeatSession(skill, "send", 1, heartbeat="slack", data_dir=data)
                    hb.start()
                    time.sleep(2.5)
                    hb.end()
            self.assertTrue(post.called)


if __name__ == "__main__":
    unittest.main()
