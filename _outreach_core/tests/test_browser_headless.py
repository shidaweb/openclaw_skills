"""browser_headless_preference resolution."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.infer import browser_headless_preference


class TestBrowserHeadless(unittest.TestCase):
    def test_brief_yaml_headless(self) -> None:
        with mock.patch(
            "_outreach_core.infer.load_runtime_config",
            return_value={"browser": {"headless": True}},
        ):
            self.assertTrue(browser_headless_preference())

    def test_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"DOORMAN_BROWSER_HEADLESS": "0"}, clear=False):
            with mock.patch(
                "_outreach_core.infer.load_runtime_config",
                return_value={"browser": {"headless": True}},
            ):
                self.assertFalse(browser_headless_preference())


if __name__ == "__main__":
    unittest.main()
