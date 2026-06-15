"""Tests for the launcher-level host gate in run_job (hard host split).

The v20 guard in run.py blocks the actual submit; this gate stops a non-primary
host (e.g. the dev MacBook) from launching ANY Doorman campaign at all.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from _outreach_core import host_role
from _outreach_core.helpers import run_job


class TestRunJobHostGate(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("DOORMAN_PRIMARY_HOST", None)
        os.environ.pop("DOORMAN_FORCE_SEND", None)
        # Isolate from any real data/primary_host file.
        self._orig_file = host_role.PRIMARY_HOST_FILE
        host_role.PRIMARY_HOST_FILE = Path("/nonexistent/doorman/primary_host_xyz")
        # Never hit Slack during tests.
        self._post = mock.patch.object(run_job, "_post", return_value=True)
        self._post.start()

    def tearDown(self):
        host_role.PRIMARY_HOST_FILE = self._orig_file
        self._post.stop()
        self._env.stop()

    def test_blocked_on_non_primary_host(self):
        os.environ["DOORMAN_PRIMARY_HOST"] = "MacMiniHome"
        with mock.patch.object(host_role, "current_host", return_value="NorimitsuM5MBP"):
            blocked, reason = run_job._host_execution_blocked()
        self.assertTrue(blocked)
        self.assertIn("not_primary", reason)

    def test_allowed_on_primary_host(self):
        os.environ["DOORMAN_PRIMARY_HOST"] = "MacMiniHome"
        with mock.patch.object(host_role, "current_host", return_value="MacMiniHome"):
            blocked, _ = run_job._host_execution_blocked()
        self.assertFalse(blocked)

    def test_force_send_override_allows_non_primary(self):
        os.environ["DOORMAN_PRIMARY_HOST"] = "MacMiniHome"
        os.environ["DOORMAN_FORCE_SEND"] = "1"
        with mock.patch.object(host_role, "current_host", return_value="NorimitsuM5MBP"):
            blocked, reason = run_job._host_execution_blocked()
        self.assertFalse(blocked)
        self.assertEqual(reason, "force_send_override")

    def test_no_primary_configured_allows_everywhere(self):
        with mock.patch.object(host_role, "current_host", return_value="AnyHost"):
            blocked, reason = run_job._host_execution_blocked()
        self.assertFalse(blocked)
        self.assertEqual(reason, "no_primary_configured")

    def test_start_returns_blocked_dict_on_non_primary(self):
        os.environ["DOORMAN_PRIMARY_HOST"] = "MacMiniHome"
        with mock.patch.object(host_role, "current_host", return_value="NorimitsuM5MBP"):
            # Patch Popen so a bug that lets execution through is caught loudly.
            with mock.patch.object(run_job.subprocess, "Popen") as popen:
                info = run_job.start("jp-form-outreach", ["send", "--ids", "1"])
        self.assertTrue(info.get("blocked"))
        self.assertIsNone(info.get("pid"))
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
