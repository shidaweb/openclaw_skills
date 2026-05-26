"""Heartbeat posts only when --heartbeat slack and webhook configured."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.progress import HeartbeatSession, resolve_heartbeat_mode


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

    def test_heartbeat_uses_thread_ts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp)
            data = skill / "data"
            data.mkdir()
            calls: list[dict] = []

            def capture(text: str, **kwargs: object) -> bool:
                calls.append(kwargs)
                return True

            with mock.patch("_outreach_core.notify.post", side_effect=capture):
                with mock.patch("_outreach_core.progress.heartbeat_interval_sec", return_value=1):
                    hb = HeartbeatSession(
                        skill,
                        "send",
                        1,
                        heartbeat="slack",
                        data_dir=data,
                        slack_thread_ts="1716714800.123456",
                    )
                    hb.start()
                    time.sleep(2.5)
                    hb.end()
            thread_values = [c.get("thread_ts") for c in calls]
            self.assertIn("1716714800.123456", thread_values)
            self.assertIn(None, thread_values)

    def test_resolve_auto_respects_enabled_for(self) -> None:
        brief = {
            "slack": {"incoming_webhook_url": "https://hooks.slack.com/test"},
            "heartbeat": {"enabled_for": ["enrich"]},
        }
        with mock.patch("_outreach_core.progress.load_runtime_config", return_value=brief):
            with mock.patch("_outreach_core.progress.webhook_configured", return_value=True):
                self.assertEqual(resolve_heartbeat_mode(None, task="enrich"), "slack")
                self.assertIsNone(resolve_heartbeat_mode(None, task="draft"))
                self.assertEqual(resolve_heartbeat_mode("off", task="enrich"), None)


if __name__ == "__main__":
    unittest.main()
