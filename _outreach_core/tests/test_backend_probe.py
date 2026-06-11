"""Tests for the backend parity probe (v21 Phase 2) — pure comparison/format."""

from __future__ import annotations

import unittest

from _outreach_core.tools import backend_probe as bp


def _result(**kw):
    base = {
        "ok": True, "page_state": "form_ok", "captcha_kind": "none",
        "captcha_blocking": False, "textareas": 1, "submit_buttons": 1,
        "radio_groups": 0,
    }
    base.update(kw)
    return base


class TestCompareProbes(unittest.TestCase):
    def test_identical_signals_match(self):
        cmp = bp.compare_probes(_result(), _result())
        self.assertTrue(cmp["match"])
        self.assertEqual(cmp["diffs"], {})

    def test_page_state_divergence(self):
        cmp = bp.compare_probes(_result(page_state="form_ok"),
                                _result(page_state="no_form"))
        self.assertFalse(cmp["match"])
        self.assertEqual(cmp["diffs"]["page_state"], ["form_ok", "no_form"])

    def test_captcha_divergence(self):
        cmp = bp.compare_probes(
            _result(captcha_kind="none", captcha_blocking=False),
            _result(captcha_kind="turnstile_interstitial", captcha_blocking=True),
        )
        self.assertFalse(cmp["match"])
        self.assertIn("captcha_kind", cmp["diffs"])
        self.assertIn("captcha_blocking", cmp["diffs"])

    def test_error_in_one_backend_is_not_a_match(self):
        cmp = bp.compare_probes(_result(), {"ok": False, "error": "boom"})
        self.assertFalse(cmp["match"])
        self.assertFalse(cmp["b_ok"])

    def test_non_signal_keys_ignored(self):
        # title/body_len differ but are NOT signal keys → still a match.
        cmp = bp.compare_probes(
            _result(title="A", body_len=100),
            _result(title="B", body_len=250),
        )
        self.assertTrue(cmp["match"])


class TestFieldsSynthesis(unittest.TestCase):
    def test_fields_shape_drives_classifier(self):
        from _outreach_core.contact_url import classify_page_form_state
        probe = {"inputs": 5, "textareas": 1, "submit_buttons": 1,
                 "radio_groups": 0, "checkboxes": 0, "body_head": "お問い合わせ " * 100}
        fields = bp._fields_from_probe(probe)
        state = classify_page_form_state(fields, probe["body_head"])
        self.assertEqual(state["state"], "form_ok")

    def test_no_controls_is_no_form(self):
        from _outreach_core.contact_url import classify_page_form_state
        probe = {"inputs": 0, "textareas": 0, "submit_buttons": 0,
                 "radio_groups": 0, "checkboxes": 0, "body_head": "会社案内 " * 200}
        fields = bp._fields_from_probe(probe)
        state = classify_page_form_state(fields, probe["body_head"])
        self.assertEqual(state["state"], "no_form")


class TestFormatComparison(unittest.TestCase):
    def test_match_line(self):
        out = bp.format_comparison(
            "https://x/", {"openclaw": _result(), "playwright": _result()},
            bp.compare_probes(_result(), _result()),
        )
        self.assertIn("✓ MATCH", out)

    def test_divergence_line(self):
        a, b = _result(), _result(page_state="no_form")
        out = bp.format_comparison(
            "https://x/", {"openclaw": a, "playwright": b}, bp.compare_probes(a, b)
        )
        self.assertIn("✗ DIVERGENCE", out)
        self.assertIn("page_state", out)


if __name__ == "__main__":
    unittest.main()
