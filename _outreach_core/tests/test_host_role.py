"""Tests for primary-host gating (v20)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from _outreach_core import host_role


class TestPrimaryHostGuard(unittest.TestCase):
    def setUp(self):
        # Isolate from any real env / data/primary_host file by pointing the
        # module's PRIMARY_HOST_FILE at a guaranteed-nonexistent path.
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("DOORMAN_PRIMARY_HOST", None)
        os.environ.pop("DOORMAN_FORCE_SEND", None)
        self._orig_file = host_role.PRIMARY_HOST_FILE
        host_role.PRIMARY_HOST_FILE = Path("/nonexistent/doorman/primary_host_xyz")

    def tearDown(self):
        host_role.PRIMARY_HOST_FILE = self._orig_file
        self._env.stop()

    def test_no_primary_means_allowed_everywhere(self):
        allowed, reason = host_role.is_send_allowed({}, host="any-host")
        self.assertTrue(allowed)
        self.assertEqual(reason, "no_primary_configured")

    def test_env_primary_match_allows(self):
        os.environ["DOORMAN_PRIMARY_HOST"] = "MacMini-Prod"
        allowed, _ = host_role.is_send_allowed({}, host="MacMini-Prod")
        self.assertTrue(allowed)

    def test_env_primary_mismatch_blocks(self):
        os.environ["DOORMAN_PRIMARY_HOST"] = "MacMini-Prod"
        allowed, reason = host_role.is_send_allowed({}, host="MacBookPro-Dev")
        self.assertFalse(allowed)
        self.assertIn("not_primary", reason)

    def test_case_and_whitespace_insensitive(self):
        os.environ["DOORMAN_PRIMARY_HOST"] = "  MacMini-Prod  "
        allowed, _ = host_role.is_send_allowed({}, host="macmini-prod")
        self.assertTrue(allowed)

    def test_force_send_override(self):
        os.environ["DOORMAN_PRIMARY_HOST"] = "MacMini-Prod"
        os.environ["DOORMAN_FORCE_SEND"] = "1"
        allowed, reason = host_role.is_send_allowed({}, host="MacBookPro-Dev")
        self.assertTrue(allowed)
        self.assertEqual(reason, "force_send_override")

    def test_config_primary_used_when_env_absent(self):
        cfg = {"execution": {"primary_host": "MacMini-Prod"}}
        allowed, _ = host_role.is_send_allowed(cfg, host="MacMini-Prod")
        self.assertTrue(allowed)
        blocked, _ = host_role.is_send_allowed(cfg, host="other")
        self.assertFalse(blocked)

    def test_env_overrides_config(self):
        os.environ["DOORMAN_PRIMARY_HOST"] = "FromEnv"
        cfg = {"execution": {"primary_host": "FromConfig"}}
        self.assertEqual(host_role.configured_primary_host(cfg), "FromEnv")


if __name__ == "__main__":
    unittest.main()
