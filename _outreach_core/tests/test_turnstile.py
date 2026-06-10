"""Tests for Cloudflare Turnstile / managed-challenge detection (v18)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _outreach_core import captcha as C  # noqa: E402


class TestTurnstileClassification(unittest.TestCase):
    def test_interstitial_blocks(self):
        # Full-page managed challenge: no form behind it.
        s = C.classify_live_state({
            "kind": "turnstile_interstitial",
            "turnstile_present": True,
            "cf_challenge_present": True,
            "has_form": False,
        })
        self.assertTrue(s["present"])
        self.assertTrue(s["blocking"])
        self.assertTrue(s["cloudflare"])

    def test_interactive_challenge_blocks(self):
        s = C.classify_live_state({
            "kind": "turnstile_challenge",
            "turnstile_present": True,
            "has_form": False,
        })
        self.assertTrue(s["blocking"])
        self.assertTrue(s["cloudflare"])

    def test_embedded_widget_not_blocking(self):
        # Turnstile widget sitting on a real form usually auto/managed-passes;
        # we must NOT treat its mere presence as blocking (that would skip a
        # perfectly sendable form).
        s = C.classify_live_state({
            "kind": "turnstile_widget",
            "turnstile_present": True,
            "has_form": True,
        })
        self.assertTrue(s["present"])
        self.assertFalse(s["blocking"])
        self.assertTrue(s["cloudflare"])  # still a CF domain → warmup hygiene

    def test_cf_challenge_present_marks_cloudflare_even_if_kind_widget(self):
        s = C.classify_live_state({
            "kind": "turnstile_widget",
            "turnstile_present": True,
            "cf_challenge_present": True,
            "has_form": True,
        })
        self.assertTrue(s["cloudflare"])

    def test_is_cloudflare_helper(self):
        self.assertTrue(C.is_cloudflare("turnstile_interstitial"))
        self.assertTrue(C.is_cloudflare("turnstile_widget"))
        self.assertFalse(C.is_cloudflare("v2_challenge"))
        self.assertFalse(C.is_cloudflare("none"))

    def test_reason_labels_present(self):
        for kind in ("turnstile_interstitial", "turnstile_challenge", "turnstile_widget"):
            label = C.reason_label({"kind": kind})
            self.assertIn("Cloudflare", label)

    def test_blocking_kinds_membership(self):
        self.assertIn("turnstile_interstitial", C._BLOCKING_KINDS)
        self.assertIn("turnstile_challenge", C._BLOCKING_KINDS)
        self.assertNotIn("turnstile_widget", C._BLOCKING_KINDS)

    def test_non_cf_states_not_cloudflare(self):
        for kind in ("none", "v3_invisible", "v2_checkbox", "v2_challenge", "hcaptcha"):
            s = C.classify_live_state({"kind": kind})
            self.assertFalse(s["cloudflare"], kind)

    def test_garbage_payload_still_safe(self):
        s = C.classify_live_state(None)
        self.assertEqual(s["kind"], "unknown")
        self.assertFalse(s["blocking"])
        self.assertFalse(s["cloudflare"])


class TestAvoidanceCloudflareLearning(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp()
        self.data_dir = Path(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_mark_and_read_cloudflare_domain(self):
        from _outreach_core import avoidance as A
        url = "https://cart.duskin.jp/inquiry_co_jp?shop_cd=04"
        self.assertFalse(A.is_cloudflare_domain(self.data_dir, url))
        A.mark_cloudflare(self.data_dir, url)
        self.assertTrue(A.is_cloudflare_domain(self.data_dir, url))
        # same registrable domain, different path → still flagged
        self.assertTrue(A.is_cloudflare_domain(self.data_dir, "https://cart.duskin.jp/other"))


if __name__ == "__main__":
    unittest.main()
