"""reCAPTCHA v3 warmup helper (§11-A-8)."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.warmup import (
    WARMUP_INTERACTION_JS,
    apply_warmup_if_enabled,
    captcha_warmup_seconds,
    captcha_warmup_strategy,
    root_url_of,
    warmup_browser_session,
)


class TestWarmupHelpers(unittest.TestCase):
    def test_root_url_strips_path(self) -> None:
        self.assertEqual(
            root_url_of("https://corp.example.jp/contact/form/"),
            "https://corp.example.jp/",
        )

    def test_root_url_handles_query(self) -> None:
        self.assertEqual(
            root_url_of("https://a.b.jp/x?foo=1"),
            "https://a.b.jp/",
        )

    def test_root_url_passes_through_non_url(self) -> None:
        self.assertEqual(root_url_of("not-a-url"), "not-a-url")

    def test_strategy_defaults_to_passthrough(self) -> None:
        self.assertEqual(captcha_warmup_strategy({}), "passthrough")
        self.assertEqual(captcha_warmup_strategy(None), "passthrough")

    def test_strategy_reads_config(self) -> None:
        self.assertEqual(
            captcha_warmup_strategy({"captcha": {"v3_strategy": "passthrough_with_warmup"}}),
            "passthrough_with_warmup",
        )

    def test_seconds_cap_at_90(self) -> None:
        self.assertEqual(
            captcha_warmup_seconds({"captcha": {"warmup_sec": 200}}), 90
        )

    def test_seconds_floor_at_zero(self) -> None:
        self.assertEqual(
            captcha_warmup_seconds({"captcha": {"warmup_sec": -5}}), 0
        )

    def test_seconds_default(self) -> None:
        self.assertEqual(captcha_warmup_seconds({}, default=15), 15)


class TestWarmupBrowserSession(unittest.TestCase):
    def test_zero_duration_skips(self) -> None:
        calls: list[tuple] = []

        def fake_browser(*args, **kwargs):
            calls.append(("browser", args, kwargs))

        def fake_eval(js: str):
            calls.append(("eval", js))
            return {"ok": True}

        diag = warmup_browser_session(
            seed_url="https://example.jp/",
            oc_browser_fn=fake_browser,
            evaluate_fn=fake_eval,
            duration_sec=0,
        )
        self.assertIn("skipped", diag)
        self.assertEqual(calls, [])

    def test_normal_flow_opens_and_evaluates(self) -> None:
        calls: list[str] = []

        def fake_browser(*args, **kwargs):
            calls.append(f"browser:{args}")

        def fake_eval(js: str):
            calls.append("eval")
            self.assertIn("mousemove", js)  # JS body contains interaction
            return {"ok": True, "viewport": {"w": 1024, "h": 768}}

        t0 = time.time()
        diag = warmup_browser_session(
            seed_url="https://example.jp/",
            oc_browser_fn=fake_browser,
            evaluate_fn=fake_eval,
            duration_sec=2,  # short for test speed
        )
        elapsed = time.time() - t0
        self.assertGreaterEqual(elapsed, 1.5)
        self.assertIn("open_seed", str(diag["steps"]))
        self.assertEqual(diag["duration_sec"], 2)
        self.assertIn("eval", calls)

    def test_browser_open_failure_is_swallowed(self) -> None:
        def boom(*args, **kwargs):
            raise RuntimeError("CDP disconnected")

        def fake_eval(js: str):
            return {"ok": True}

        diag = warmup_browser_session(
            seed_url="https://example.jp/",
            oc_browser_fn=boom,
            evaluate_fn=fake_eval,
            duration_sec=1,
        )
        self.assertIn("error", diag)
        self.assertIn("CDP disconnected", diag["error"])

    def test_evaluate_exception_is_captured_in_steps(self) -> None:
        def fake_browser(*args, **kwargs):
            pass

        def boom(js: str):
            raise RuntimeError("eval broke")

        diag = warmup_browser_session(
            seed_url="https://example.jp/",
            oc_browser_fn=fake_browser,
            evaluate_fn=boom,
            duration_sec=1,
        )
        # Should still complete; error captured in steps
        self.assertNotIn("error", diag)  # top-level OK (browser opened)
        steps_str = str(diag.get("steps"))
        self.assertIn("eval broke", steps_str)


class TestApplyWarmupIfEnabled(unittest.TestCase):
    def test_skipped_when_strategy_passthrough(self) -> None:
        called: list[str] = []

        def fake_browser(*args, **kwargs):
            called.append("browser")

        def fake_eval(js: str):
            called.append("eval")

        diag = apply_warmup_if_enabled(
            form_url="https://example.jp/contact/",
            config={"captcha": {"v3_strategy": "passthrough"}},
            oc_browser_fn=fake_browser,
            evaluate_fn=fake_eval,
        )
        self.assertTrue(diag.get("skipped"))
        self.assertEqual(diag.get("strategy"), "passthrough")
        self.assertEqual(called, [])

    def test_runs_when_strategy_enabled(self) -> None:
        events: list[tuple] = []
        calls: list[str] = []

        def fake_browser(*args, **kwargs):
            calls.append("browser")

        def fake_eval(js: str):
            calls.append("eval")
            return {"ok": True}

        diag = apply_warmup_if_enabled(
            form_url="https://example.jp/contact/",
            config={
                "captcha": {"v3_strategy": "passthrough_with_warmup", "warmup_sec": 1}
            },
            oc_browser_fn=fake_browser,
            evaluate_fn=fake_eval,
            emit_event=lambda kind, **kw: events.append((kind, kw)),
            target_id="t1",
        )
        self.assertEqual(diag.get("strategy"), "passthrough_with_warmup")
        self.assertIn("elapsed_sec", diag)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "captcha.warmup.applied")
        self.assertEqual(events[0][1]["target_id"], "t1")

    def test_no_emit_event_callback_does_not_crash(self) -> None:
        diag = apply_warmup_if_enabled(
            form_url="https://example.jp/contact/",
            config={
                "captcha": {"v3_strategy": "passthrough_with_warmup", "warmup_sec": 1}
            },
            oc_browser_fn=lambda *a, **k: None,
            evaluate_fn=lambda js: {"ok": True},
            emit_event=None,
        )
        self.assertEqual(diag.get("strategy"), "passthrough_with_warmup")


if __name__ == "__main__":
    unittest.main()
