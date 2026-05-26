"""Refine default resolution (v4 B-5)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.draft import resolve_refine_enabled


class TestRefineConfig(unittest.TestCase):
    def test_default_true_without_config(self) -> None:
        self.assertTrue(resolve_refine_enabled({}))

    def test_config_false(self) -> None:
        self.assertFalse(
            resolve_refine_enabled({"draft": {"refine_default": False}})
        )

    def test_cli_no_refine_wins(self) -> None:
        self.assertFalse(
            resolve_refine_enabled(
                {"draft": {"refine_default": True}},
                cli_no_refine=True,
            )
        )

    def test_cli_refine_wins_over_config_false(self) -> None:
        self.assertTrue(
            resolve_refine_enabled(
                {"draft": {"refine_default": False}},
                cli_refine=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
