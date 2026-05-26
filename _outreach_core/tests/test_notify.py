"""notify.post is no-op without webhook and never raises."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import notify


class TestNotify(unittest.TestCase):
    def test_no_webhook_returns_false(self) -> None:
        with mock.patch.object(notify, "_webhook_url", return_value=""):
            self.assertFalse(notify.post("hello"))

    def test_never_raises_on_failure(self) -> None:
        with mock.patch.object(notify, "_webhook_url", return_value="http://127.0.0.1:9/"):
            self.assertFalse(notify.post("test", level="warn"))


if __name__ == "__main__":
    unittest.main()
