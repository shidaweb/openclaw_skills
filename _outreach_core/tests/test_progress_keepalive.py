"""Tests for the always-on stdout keepalive in HeartbeatSession (v6 §15-B)."""

import io
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TestKeepalive(unittest.TestCase):
    def setUp(self):
        os.environ["DOORMAN_KEEPALIVE_SEC"] = "1"
        # Reimport so the interval env is picked up by the helper.
        import importlib

        from _outreach_core import progress
        importlib.reload(progress)
        self.progress = progress

    def tearDown(self):
        os.environ.pop("DOORMAN_KEEPALIVE_SEC", None)

    def test_keepalive_emits_during_silence(self):
        import tempfile

        buf = io.StringIO()
        hb = self.progress.HeartbeatSession(
            Path("jp-form-outreach"), "draft", 5,
            heartbeat=None, data_dir=Path(tempfile.mkdtemp()),
        )
        with redirect_stdout(buf):
            hb.start("drafting")
            time.sleep(2.3)  # simulate a slow, silent LLM call
            hb.end("done")
        out = buf.getvalue()
        self.assertGreaterEqual(out.count("[keepalive]"), 1,
                                f"expected keepalive lines, got: {out!r}")

    def test_no_slack_thread_when_mode_none(self):
        import tempfile

        hb = self.progress.HeartbeatSession(
            Path("jp-form-outreach"), "draft", 1,
            heartbeat=None, data_dir=Path(tempfile.mkdtemp()),
        )
        hb.start()
        # keepalive thread exists, slack heartbeat thread does not
        self.assertIsNotNone(hb._keepalive_thread)
        self.assertIsNone(hb._thread)
        hb.end()


if __name__ == "__main__":
    unittest.main()
