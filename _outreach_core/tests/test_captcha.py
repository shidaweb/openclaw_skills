"""Tests for live reCAPTCHA classification (_outreach_core/captcha.py, v6 §3.5)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _outreach_core import captcha as C  # noqa: E402


class TestClassifyLiveState(unittest.TestCase):
    def test_none(self):
        s = C.classify_live_state({"kind": "none", "checkbox_present": False, "challenge_visible": False})
        self.assertFalse(s["present"])
        self.assertFalse(s["blocking"])

    def test_v3_invisible_not_blocking(self):
        s = C.classify_live_state({"kind": "v3_invisible", "checkbox_present": False, "challenge_visible": False})
        self.assertTrue(s["present"])
        self.assertFalse(s["blocking"])  # v3 never blocks a submit

    def test_v2_checkbox_not_blocking_by_default(self):
        s = C.classify_live_state({"kind": "v2_checkbox", "checkbox_present": True, "challenge_visible": False})
        self.assertFalse(s["blocking"])  # presence of a checkbox ≠ blocking

    def test_v2_challenge_blocks(self):
        s = C.classify_live_state({"kind": "v2_challenge", "checkbox_present": True, "challenge_visible": True})
        self.assertTrue(s["blocking"])

    def test_hcaptcha_blocks(self):
        s = C.classify_live_state({"kind": "hcaptcha", "checkbox_present": False, "challenge_visible": False})
        self.assertTrue(s["blocking"])

    def test_garbage_payload_is_safe_nonblocking(self):
        # Critical: a failed eval must NOT be reported as a captcha (that was the
        # original false-positive bug — non-captcha failures mislabeled).
        for bad in (None, "oops", 42, []):
            s = C.classify_live_state(bad)
            self.assertEqual(s["kind"], "unknown")
            self.assertFalse(s["blocking"])

    def test_challenge_visible_overrides_kind(self):
        s = C.classify_live_state({"kind": "v2_checkbox", "challenge_visible": True})
        self.assertTrue(s["blocking"])


class TestIsBlocking(unittest.TestCase):
    def test_block_on_checkbox_opt_in(self):
        self.assertFalse(C.is_blocking("v2_checkbox"))
        self.assertTrue(C.is_blocking("v2_checkbox", block_on_checkbox=True))

    def test_v3_and_none_never_block(self):
        self.assertFalse(C.is_blocking("v3_invisible"))
        self.assertFalse(C.is_blocking("none"))


class TestReasonLabel(unittest.TestCase):
    def test_truthful_labels(self):
        self.assertIn("画像チャレンジ", C.reason_label({"kind": "v2_challenge"}))
        self.assertIn("非ブロッキング", C.reason_label({"kind": "v3_invisible"}))
        self.assertIn("なし", C.reason_label({"kind": "none"}))
        self.assertIn("判定不能", C.reason_label({"kind": "unknown"}))

    def test_js_constant_is_a_function_expression(self):
        # Sanity: the JS payload should be an arrow function returning an object.
        self.assertIn("kind", C.LIVE_CAPTCHA_JS)
        self.assertIn("bframe", C.LIVE_CAPTCHA_JS)


if __name__ == "__main__":
    unittest.main()
