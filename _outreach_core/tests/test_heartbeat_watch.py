from __future__ import annotations

import unittest
from unittest import mock

from _outreach_core.helpers import heartbeat_watch


class TestHeartbeatWatchLifecycle(unittest.TestCase):
    def test_end_event_stops_before_posting(self) -> None:
        end_event = {
            "event": "end",
            "task": "campaign",
            "current": 10,
            "total": 10,
            "message": "done",
        }
        with mock.patch.object(heartbeat_watch, "webhook_configured", return_value=True), mock.patch.object(
            heartbeat_watch, "_read_last_event", return_value=end_event
        ), mock.patch.object(heartbeat_watch, "post", return_value=True) as post:
            code = heartbeat_watch.run_watch(
                "linkedin-outreach",
                interval_sec=1,
                poll_sec=0,
                idle_timeout_sec=60,
            )
        self.assertEqual(code, 0)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
