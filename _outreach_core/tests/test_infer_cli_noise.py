"""
Regression tests for the 2026-06-10 "Doctor warnings" outage.

The OpenClaw CLI began printing a Doctor-warnings banner box into EVERY stdout
(conflicting slack plugin metadata in shared SQLite state). The old evaluate
parser did json.loads over the whole text → every oc_evaluate returned None →
0 button candidates → consecutive send failures (duskin / ain_holdings /
asics_corp / gakkyusha). These tests pin banner tolerance with the REAL banner.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from _outreach_core import infer

# Verbatim banner captured from resolve_snapshot_ain_holdings.txt (2026-06-10).
DOCTOR_BANNER = (
    "│\n"
    "◇  Doctor warnings ──────────────────────────────────────────────────────╮\n"
    "│                                                                        │\n"
    "│  - Left plugin install index in place because shared SQLite state has  │\n"
    "│    conflicting plugin install metadata for: slack                      │\n"
    "│                                                                        │\n"
    "├────────────────────────────────────────────────────────────────────────╯\n"
)


class TestStripCliNoise(unittest.TestCase):
    def test_banner_fully_removed(self) -> None:
        self.assertEqual(infer._strip_cli_noise(DOCTOR_BANNER), "")

    def test_payload_survives(self) -> None:
        out = DOCTOR_BANNER + '{"a": 1}\n'
        self.assertEqual(infer._strip_cli_noise(out), '{"a": 1}')

    def test_snapshot_lines_kept(self) -> None:
        snap = DOCTOR_BANNER + "- generic [active] [ref=e1]:\n  - link \"送信\" [ref=e2]\n"
        stripped = infer._strip_cli_noise(snap)
        self.assertIn("- generic [active]", stripped)
        self.assertIn("送信", stripped)
        self.assertNotIn("Doctor warnings", stripped)

    def test_lobster_banner_and_empty(self) -> None:
        self.assertEqual(infer._strip_cli_noise("🦞 openclaw\n\n42\n"), "42")
        self.assertEqual(infer._strip_cli_noise(None), "")
        self.assertEqual(infer._strip_cli_noise(""), "")

    def test_error_cleaner_keeps_cause_and_drops_migration_warning(self) -> None:
        err = (
            "[state-migrations] Legacy state migration warnings:\n"
            "- Left plugin install index in place because shared SQLite state has conflicting plugin install metadata for: slack\n"
            'GatewayClientRequestError: tab not found: browser tab "ABC" not found.\n'
        )
        clean = infer._clean_cli_error(err)
        self.assertNotIn("state-migrations", clean)
        self.assertNotIn("plugin install", clean)
        self.assertIn("tab not found", clean)


class TestParseEvaluateOutput(unittest.TestCase):
    def test_object_after_banner(self) -> None:
        out = DOCTOR_BANNER + '{"buttons": [{"text": "送信する"}]}\n'
        self.assertEqual(
            infer.parse_evaluate_output(out), {"buttons": [{"text": "送信する"}]}
        )

    def test_array_after_banner(self) -> None:
        self.assertEqual(infer.parse_evaluate_output(DOCTOR_BANNER + "[1, 2]\n"), [1, 2])

    def test_scalar_after_banner(self) -> None:
        self.assertEqual(infer.parse_evaluate_output(DOCTOR_BANNER + "123\n"), 123)

    def test_double_encoded_string(self) -> None:
        out = DOCTOR_BANNER + '"{\\"clicked\\": true}"\n'
        self.assertEqual(infer.parse_evaluate_output(out), {"clicked": True})

    def test_trailing_junk_tolerated(self) -> None:
        out = '{"ok": true}\nsome trailing prose\n'
        self.assertEqual(infer.parse_evaluate_output(out), {"ok": True})

    def test_banner_only_returns_none(self) -> None:
        self.assertIsNone(infer.parse_evaluate_output(DOCTOR_BANNER))
        self.assertIsNone(infer.parse_evaluate_output(""))
        self.assertIsNone(infer.parse_evaluate_output(None))


class TestExtractJsonPayloadBanner(unittest.TestCase):
    def test_open_payload_after_banner(self) -> None:
        out = DOCTOR_BANNER + '{"targetId": "ABC123"}\n'
        self.assertEqual(infer.extract_json_payload(out), {"targetId": "ABC123"})

    def test_json_with_trailing_decoration(self) -> None:
        out = DOCTOR_BANNER + '{"targetId": "X"}\n╰──────╯\n'
        self.assertEqual(infer.extract_json_payload(out), {"targetId": "X"})


class TestBrowserErrorLogging(unittest.TestCase):
    def test_missing_tab_during_close_is_quiet(self) -> None:
        err = (
            "[state-migrations] Legacy state migration warnings:\n"
            'GatewayClientRequestError: tab not found: browser tab "ABC" not found.\n'
        )
        buf = io.StringIO()
        with mock.patch.object(infer, "_run", return_value=(1, "", err)), \
                contextlib.redirect_stderr(buf):
            self.assertIsNone(infer.oc_browser("close", "ABC"))
        self.assertEqual(buf.getvalue(), "")

    def test_real_browser_error_keeps_clean_cause(self) -> None:
        err = (
            "[state-migrations] Legacy state migration warnings:\n"
            "GatewayClientRequestError: navigation failed\n"
        )
        buf = io.StringIO()
        with mock.patch.object(infer, "_run", return_value=(1, "", err)), \
                contextlib.redirect_stderr(buf):
            self.assertIsNone(infer.oc_browser("open", "https://example.com"))
        logged = buf.getvalue()
        self.assertIn("navigation failed", logged)
        self.assertNotIn("state-migrations", logged)


if __name__ == "__main__":
    unittest.main()
