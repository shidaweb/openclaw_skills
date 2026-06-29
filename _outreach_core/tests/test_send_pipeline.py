"""v30 §WS-E — send_pipeline.try_iframe_form_takeover (extracted from run.py).

The extracted helper is purely DI-driven, so tests stub ``open_url``,
``emit_event``, and ``sleep_fn`` without monkey-patching ``run.py``. This is
the contract for any future extraction: helpers must take their I/O surface
as explicit parameters."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import send_pipeline as sp


class TestIframeFormTakeover(unittest.TestCase):
    def test_known_service_iframe_navigates_and_rewrites_form_url(self) -> None:
        target = {
            "id": "legalon",
            "name": "LegalOn Technologies",
            "form_url": "https://legalontech.jp/contact/",
        }
        fields = {"iframes": [
            {"src": "https://lp.legalforce-cloud.com/index.php/form/XDFrame"},
        ]}
        opens: list[str] = []
        slept: list[float] = []
        events: list[dict] = []
        src = sp.try_iframe_form_takeover(
            target, fields,
            open_url=lambda s: opens.append(s),
            sleep_fn=lambda s: slept.append(s),
            sleep_sec=0.5,
            emit_event=lambda kind, **kw: events.append({"kind": kind, **kw}),
            trace_dir=None,
            target_id="legalon",
        )
        self.assertIsNotNone(src)
        self.assertEqual(opens, [src])
        self.assertEqual(slept, [0.5])
        kinds = [e["kind"] for e in events]
        self.assertIn("send.iframe_form_takeover", kinds)
        # The target's form_url is rewritten so downstream events / journals
        # report the URL actually being submitted to.
        self.assertEqual(target["form_url"], src)

    def test_no_iframe_returns_none(self) -> None:
        target = {"id": "x", "form_url": "https://x.example/"}
        self.assertIsNone(
            sp.try_iframe_form_takeover(
                target, {"iframes": []},
                open_url=lambda s: None,
            )
        )
        # form_url is preserved when nothing happens.
        self.assertEqual(target["form_url"], "https://x.example/")

    def test_unrelated_iframe_host_returns_none(self) -> None:
        target = {"id": "x", "form_url": "https://x.example/"}
        fields = {"iframes": [{"src": "https://www.youtube.com/embed/abc"}]}
        self.assertIsNone(
            sp.try_iframe_form_takeover(
                target, fields, open_url=lambda s: None,
            )
        )

    def test_open_url_failure_swallowed_returns_none(self) -> None:
        # The send loop must not crash because the takeover navigation failed
        # — fall through to the normal page_has_no_form escalation.
        def _boom(src: str) -> None:
            raise RuntimeError("nav failed")

        target = {"id": "y", "form_url": "https://y.example/"}
        fields = {"iframes": [{"src": "https://share.hsforms.com/abc"}]}
        self.assertIsNone(
            sp.try_iframe_form_takeover(
                target, fields, open_url=_boom,
            )
        )
        # form_url is NOT rewritten on failure.
        self.assertEqual(target["form_url"], "https://y.example/")

    def test_emit_event_failure_does_not_abort(self) -> None:
        # Even if the audit emit raises, the navigation result must survive.
        def _boom_emit(*a, **kw):
            raise RuntimeError("event emit failed")

        target = {"id": "z", "form_url": "https://z.example/"}
        fields = {"iframes": [{"src": "https://share.hsforms.com/abc"}]}
        src = sp.try_iframe_form_takeover(
            target, fields,
            open_url=lambda s: None,
            emit_event=_boom_emit,
        )
        self.assertEqual(src, "https://share.hsforms.com/abc")
        self.assertEqual(target["form_url"], src)

    def test_legacy_shim_in_run_py_still_works(self) -> None:
        # Lock that the run.py shim still produces the same outcome as the
        # direct helper call — protects the integration during the multi-week
        # extraction.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "jp-form-outreach"))
        import run  # noqa: E402

        opens: list[tuple] = []
        events: list[dict] = []
        with mock.patch.object(run, "oc_browser",
                               lambda *a, **k: opens.append((a, k)) or ""):
            with mock.patch.object(run.time, "sleep", lambda s: None):
                with mock.patch.object(
                    run, "_emit_event",
                    lambda kind, **kw: events.append({"kind": kind, **kw}),
                ):
                    target = {"id": "shim", "form_url": "https://shim.example/"}
                    fields = {"iframes": [
                        {"src": "https://share.hsforms.com/shim"},
                    ]}
                    src = run._try_iframe_form_takeover(target, fields, None, "shim")
        self.assertEqual(src, "https://share.hsforms.com/shim")
        # The opened URL is the iframe src (positional arg to oc_browser).
        self.assertTrue(any("share.hsforms.com" in str(a) for a, _ in opens))
        kinds = [e["kind"] for e in events]
        self.assertIn("send.iframe_form_takeover", kinds)


if __name__ == "__main__":
    unittest.main()
