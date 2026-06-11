"""Tests for the browser adapter seam (v21).

Playwright is NOT required: the OpenClaw adapter is tested by monkeypatching
_outreach_core.infer, and the Playwright adapter's pure tab registry is tested
with duck-typed fake pages.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from _outreach_core import adapters
from _outreach_core.adapters.openclaw_browser import OpenClawBrowserAdapter
from _outreach_core.adapters.playwright_browser import TabRegistry


class TestBackendSelection(unittest.TestCase):
    def setUp(self):
        adapters.reset()
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("DOORMAN_BROWSER_BACKEND", None)

    def tearDown(self):
        adapters.reset()
        self._env.stop()

    def test_default_is_openclaw(self):
        self.assertEqual(adapters.select_backend(), "openclaw")
        self.assertEqual(adapters.get_browser().backend, "openclaw")

    def test_env_wins_over_config(self):
        os.environ["DOORMAN_BROWSER_BACKEND"] = "openclaw"
        self.assertEqual(
            adapters.select_backend({"browser": {"backend": "playwright"}}),
            "openclaw",
        )

    def test_config_selects_when_env_absent(self):
        self.assertEqual(
            adapters.select_backend({"browser": {"backend": "playwright"}}),
            "playwright",
        )

    def test_singleton_reused(self):
        a = adapters.get_browser()
        b = adapters.get_browser()
        self.assertIs(a, b)


class TestOpenClawAdapterDelegation(unittest.TestCase):
    def test_delegates_each_primitive(self):
        a = OpenClawBrowserAdapter(profile="p1")
        a._infer = mock.MagicMock()
        a._infer.oc_evaluate.return_value = {"ok": 1}
        a._infer.oc_browser.return_value = "snap"
        a._infer.oc_browser_json.return_value = {"tabs": []}

        self.assertEqual(a.evaluate("() => 1"), {"ok": 1})
        a._infer.oc_evaluate.assert_called_once_with("() => 1", profile="p1")

        self.assertEqual(a.browser("snapshot"), "snap")
        a._infer.oc_browser.assert_called_once_with("snapshot", profile="p1")

        self.assertEqual(a.browser_json("tabs"), {"tabs": []})
        a._infer.oc_browser_json.assert_called_once_with("tabs", profile="p1")


class _FakePage:
    def __init__(self, url):
        self.url = url


class TestTabRegistry(unittest.TestCase):
    def test_add_get_id_current(self):
        r = TabRegistry()
        p1 = _FakePage("https://a.example/")
        tid = r.add(p1)
        self.assertIs(r.get(tid), p1)
        self.assertIs(r.current, p1)
        self.assertEqual(r.id_of(p1), tid)

    def test_open_payload_shape_matches_tab_utils(self):
        from _outreach_core import tab_utils
        r = TabRegistry()
        tid = r.add(_FakePage("https://x/"))
        payload = r.open_payload(tid, "https://x/")
        self.assertEqual(tab_utils.target_id_from_open(payload), tid)

    def test_tabs_payload_shape_and_order(self):
        from _outreach_core import tab_utils
        r = TabRegistry()
        t1 = r.add(_FakePage("https://1/"))
        t2 = r.add(_FakePage("https://2/"))
        payload = r.tabs_payload()
        self.assertEqual(tab_utils.page_target_ids(payload), [t1, t2])
        self.assertTrue(tab_utils.is_tab_open(payload, t1))

    def test_remove_updates_current(self):
        r = TabRegistry()
        t1 = r.add(_FakePage("https://1/"))
        t2 = r.add(_FakePage("https://2/"))
        r.remove(t2)
        self.assertIs(r.current, r.get(t1))
        self.assertIsNone(r.get(t2))

    def test_tabs_payload_excludes_iframes_via_tab_utils(self):
        # registry only ever holds pages, so page_tabs returns all of them
        from _outreach_core import tab_utils
        r = TabRegistry()
        r.add(_FakePage("https://1/"))
        self.assertEqual(len(tab_utils.page_tabs(r.tabs_payload())), 1)


if __name__ == "__main__":
    unittest.main()
