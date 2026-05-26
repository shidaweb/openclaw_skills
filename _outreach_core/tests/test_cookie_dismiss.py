"""Cookie consent banner dismissal (§11-A-7)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.cookie_dismiss import (
    DISMISS_COOKIE_BANNER_JS,
    apply_cookie_dismiss,
    cookie_consent_mode,
    dismiss_cookie_banner,
)


class TestCookieDismiss(unittest.TestCase):
    """
    We don't actually run JS here — we use a stub evaluate_fn that pretends
    to be a browser returning canned results for given JS bodies. The JS
    source itself is asserted to contain the key patterns.
    """

    # ---------- JS content assertions ----------

    def test_js_includes_known_sdk_selectors(self) -> None:
        for sel in (
            "#onetrust-accept-btn-handler",
            "#truste-consent-button",
            "#CybotCookiebotDialogBodyButtonAccept",
        ):
            self.assertIn(sel, DISMISS_COOKIE_BANNER_JS)

    def test_js_has_deny_list_for_reject_patterns(self) -> None:
        for pat in ("同意しない", "拒否", "reject", "decline"):
            self.assertIn(pat, DISMISS_COOKIE_BANNER_JS)

    def test_js_checks_deny_before_accept_click(self) -> None:
        # Per-button: deny check runs before accept pattern loop
        loop = DISMISS_COOKIE_BANNER_JS.split("for (const b of buttons)")[1]
        deny_pos = loop.index("DENY_TEXT_PATTERNS.some")
        accept_pos = loop.index("for (const re of ACCEPT_TEXT_PATTERNS)")
        self.assertLess(deny_pos, accept_pos)

    def test_js_scopes_text_match_under_banner_root(self) -> None:
        # 親要素の id/class が cookie / consent / gdpr を含むものに限定
        for tok in ('[id*="cookie"', '[class*="consent"', '[id*="gdpr"'):
            self.assertIn(tok, DISMISS_COOKIE_BANNER_JS)

    # ---------- wrapper behaviour assertions ----------

    def test_mode_skip_returns_immediately_without_evaluating(self) -> None:
        calls = []

        def fake_eval(js: str):
            calls.append(js)
            return {"dismissed": True, "method": "id"}

        res = dismiss_cookie_banner(fake_eval, mode="skip")
        self.assertFalse(res["dismissed"])
        self.assertEqual(res.get("reason"), "skipped_by_config")
        self.assertEqual(calls, [])

    def test_id_method_returns_dismissed_true(self) -> None:
        def fake_eval(js: str):
            return {
                "dismissed": True,
                "method": "id",
                "selector": "#onetrust-accept-btn-handler",
            }

        res = dismiss_cookie_banner(fake_eval, retries=0)
        self.assertTrue(res["dismissed"])
        self.assertEqual(res["method"], "id")
        self.assertEqual(res["selector"], "#onetrust-accept-btn-handler")
        self.assertEqual(res["mode"], "accept")

    def test_text_method_returns_dismissed_true(self) -> None:
        def fake_eval(js: str):
            return {"dismissed": True, "method": "text", "text": "すべて受け入れる"}

        res = dismiss_cookie_banner(fake_eval, retries=0)
        self.assertTrue(res["dismissed"])
        self.assertEqual(res["method"], "text")
        self.assertEqual(res["text"], "すべて受け入れる")

    def test_no_banner_returns_dismissed_false_without_raising(self) -> None:
        def fake_eval(js: str):
            return {"dismissed": False}

        res = dismiss_cookie_banner(fake_eval, retries=1, wait_sec=0.0)
        self.assertFalse(res["dismissed"])
        self.assertEqual(res["mode"], "accept")

    def test_evaluate_fn_exception_is_swallowed(self) -> None:
        def boom(js: str):
            raise RuntimeError("CDP disconnected")

        res = dismiss_cookie_banner(boom, retries=0, wait_sec=0.0)
        self.assertFalse(res["dismissed"])
        self.assertIn("error", res)
        self.assertIn("CDP disconnected", res["error"])

    def test_retries_settles_when_second_attempt_succeeds(self) -> None:
        attempts = {"n": 0}

        def fake_eval(js: str):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return {"dismissed": False}
            return {"dismissed": True, "method": "id", "selector": "#cookie-accept"}

        res = dismiss_cookie_banner(fake_eval, retries=2, wait_sec=0.0)
        self.assertTrue(res["dismissed"])
        self.assertEqual(attempts["n"], 2)

    def test_cookie_consent_mode_from_config(self) -> None:
        self.assertEqual(cookie_consent_mode({"browser": {"cookie_consent": "skip"}}), "skip")
        self.assertEqual(cookie_consent_mode({}), "accept")

    def test_apply_cookie_dismiss_calls_emit_on_success(self) -> None:
        events: list[str] = []

        def fake_eval(js: str):
            return {"dismissed": True, "method": "id", "selector": "#cookie-accept"}

        apply_cookie_dismiss(
            fake_eval,
            {"browser": {"cookie_consent": "accept"}},
            stage="enrich",
            target_id="t1",
            emit_event=lambda kind, **kw: events.append(kind),
        )
        self.assertEqual(events, ["cookie.dismissed"])


if __name__ == "__main__":
    unittest.main()
