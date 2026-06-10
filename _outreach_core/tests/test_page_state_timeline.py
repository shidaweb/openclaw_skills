"""Tests for v17: send-time page vetting + stage timeline."""

from __future__ import annotations

import unittest

from _outreach_core import send_timeline as tl
from _outreach_core.contact_url import classify_page_form_state


def _fields(inputs=0, textareas=0, buttons=0, radios=0, checks=0) -> dict:
    return {
        "inputs": [{"type": "text", "name": f"i{n}"} for n in range(inputs)],
        "textareas": [{"name": f"t{n}"} for n in range(textareas)],
        "submit_buttons": [{"text": "送信"} for _ in range(buttons)],
        "radios": {f"r{n}": [] for n in range(radios)},
        "checkboxes": [{"label": f"c{n}"} for n in range(checks)],
    }


LONG_BODY = "会社案内 " * 200  # > 600 chars → rendered page


class TestClassifyPageFormState(unittest.TestCase):
    def test_normal_form_ok(self) -> None:
        st = classify_page_form_state(_fields(inputs=5, textareas=1, buttons=1), LONG_BODY)
        self.assertEqual(st["state"], "form_ok")

    def test_guide_page_no_form(self) -> None:
        # ain_holdings case: rendered page, links only, zero controls.
        st = classify_page_form_state(_fields(), LONG_BODY)
        self.assertEqual(st["state"], "no_form")
        self.assertEqual(st["inputs"], 0)

    def test_empty_render_when_body_tiny(self) -> None:
        st = classify_page_form_state(_fields(), "  ")
        self.assertEqual(st["state"], "empty_render")

    def test_error_page(self) -> None:
        st = classify_page_form_state(_fields(), "404 Not Found ページが見つかりません")
        self.assertEqual(st["state"], "error_page")

    def test_gate_like_radios_only(self) -> None:
        st = classify_page_form_state(_fields(radios=2, buttons=1), LONG_BODY)
        self.assertEqual(st["state"], "gate_like")

    def test_hidden_inputs_do_not_count(self) -> None:
        f = {"inputs": [{"type": "hidden"}], "textareas": [], "submit_buttons": []}
        st = classify_page_form_state(f, LONG_BODY)
        self.assertEqual(st["state"], "no_form")


class TestSendTimeline(unittest.TestCase):
    def _sample(self) -> list:
        t: list = []
        tl.add(t, "open", True, url="https://example.com/contact")
        tl.add(t, "page_state", True, state="form_ok", inputs=8)
        tl.add(t, "fill", True, filled=9, unfilled=1)
        tl.add(t, "first_submit", False, flow="confirm", page_state_now="no_form")
        return t

    def test_add_strips_empty_detail(self) -> None:
        t: list = []
        tl.add(t, "open", True, url="x", final_url=None, redirected=None)
        self.assertEqual(t[0]["detail"], {"url": "x"})

    def test_format_contains_marks_and_labels(self) -> None:
        out = tl.format_timeline(self._sample())
        self.assertIn("✓ ページを開く", out)
        self.assertIn("✗ 確認/送信ボタン押下", out)
        self.assertIn("page_state_now=no_form", out)

    def test_first_failure_and_headline(self) -> None:
        t = self._sample()
        f = tl.first_failure(t)
        self.assertEqual(f["stage"], "first_submit")
        self.assertIn("確認/送信ボタン押下で失敗", tl.failure_headline(t))

    def test_no_failure_headline_empty(self) -> None:
        t: list = []
        tl.add(t, "open", True)
        self.assertEqual(tl.failure_headline(t), "")
        self.assertIsNone(tl.first_failure(t))


if __name__ == "__main__":
    unittest.main()
