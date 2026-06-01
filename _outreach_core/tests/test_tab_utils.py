"""Tests for browser tab management helpers (tab_utils.py + infer JSON, v6 §17)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _outreach_core import tab_utils as T  # noqa: E402
from _outreach_core import infer  # noqa: E402

# Real shapes captured from `openclaw browser --json tabs` / `... open`.
OPEN_PAYLOAD = {
    "targetId": "AC5CD51D122FEDE9BAA11FC168E26213",
    "title": "", "url": "https://example.com/",
    "type": "page", "suggestedTargetId": "t494", "tabId": "t494",
}
TABS_PAYLOAD = {
    "tabs": [
        {"targetId": "P1", "type": "page", "url": "https://a.co.jp/"},
        {"targetId": "IF", "type": "iframe", "url": "https://accounts.google.com/Rotate"},
        {"targetId": "P2", "type": "page", "url": "https://b.co.jp/"},
        {"targetId": "P3", "type": "page", "url": "https://www.park24.co.jp/contact/"},
    ]
}


class TestTargetIdParsing(unittest.TestCase):
    def test_open_returns_target_id(self):
        self.assertEqual(T.target_id_from_open(OPEN_PAYLOAD), "AC5CD51D122FEDE9BAA11FC168E26213")

    def test_open_bad_payload(self):
        self.assertIsNone(T.target_id_from_open(None))
        self.assertIsNone(T.target_id_from_open({"no": "id"}))
        self.assertIsNone(T.target_id_from_open("string"))

    def test_page_tabs_excludes_iframes(self):
        ids = T.page_target_ids(TABS_PAYLOAD)
        self.assertEqual(ids, ["P1", "P2", "P3"])
        self.assertNotIn("IF", ids)

    def test_is_tab_open(self):
        self.assertTrue(T.is_tab_open(TABS_PAYLOAD, "P2"))
        self.assertFalse(T.is_tab_open(TABS_PAYLOAD, "IF"))  # iframe not a tab
        self.assertFalse(T.is_tab_open(TABS_PAYLOAD, "ZZZ"))

    def test_find_tab(self):
        self.assertEqual(T.find_tab(TABS_PAYLOAD, "P3")["url"], "https://www.park24.co.jp/contact/")
        self.assertIsNone(T.find_tab(TABS_PAYLOAD, "nope"))


class TestTabCap(unittest.TestCase):
    def test_no_overflow_under_cap(self):
        self.assertEqual(T.closable_overflow(TABS_PAYLOAD, protect=set(), cap=5), [])

    def test_closes_oldest_first(self):
        # 3 pages, cap 2 → close exactly 1, the oldest (P1). P3 protected as newest.
        out = T.closable_overflow(TABS_PAYLOAD, protect=set(), cap=2, keep_newest=1)
        self.assertEqual(out, ["P1"])

    def test_closes_two_when_cap_one(self):
        # cap 1 → must close 2 (P1, P2); P3 protected as newest.
        out = T.closable_overflow(TABS_PAYLOAD, protect=set(), cap=1, keep_newest=1)
        self.assertEqual(out, ["P1", "P2"])

    def test_protect_is_never_closed(self):
        out = T.closable_overflow(TABS_PAYLOAD, protect={"P1"}, cap=1, keep_newest=1)
        self.assertNotIn("P1", out)
        self.assertIn("P2", out)


class TestSameSite(unittest.TestCase):
    def test_subdomains_same_company(self):
        self.assertTrue(T.same_site("https://www.park24.co.jp/contact/", "https://ssl.park24.co.jp/x"))

    def test_different_co_jp_companies_are_NOT_same(self):
        # The dangerous bug guard: a.co.jp vs b.co.jp must be different.
        self.assertFalse(T.same_site("https://a.co.jp", "https://b.co.jp"))

    def test_different_com(self):
        self.assertFalse(T.same_site("https://foo.com", "https://bar.com"))

    def test_same_com(self):
        self.assertTrue(T.same_site("https://example.com/a", "https://example.com/b"))

    def test_empty_is_false(self):
        self.assertFalse(T.same_site("", "https://x.com"))
        self.assertFalse(T.same_site(None, None))

    def test_registrable_domain(self):
        self.assertEqual(T.registrable_domain("https://www.park24.co.jp/x"), "park24.co.jp")
        self.assertEqual(T.registrable_domain("a.co.jp"), "a.co.jp")
        self.assertEqual(T.registrable_domain("https://example.com"), "example.com")


class TestExtractJson(unittest.TestCase):
    def test_clean_json(self):
        self.assertEqual(infer.extract_json_payload('{"targetId":"X"}'), {"targetId": "X"})

    def test_banner_decorated(self):
        s = "🦞 OpenClaw 2026.5.27\n│\n◇\n{\"targetId\":\"ABC\",\"type\":\"page\"}"
        self.assertEqual(infer.extract_json_payload(s)["targetId"], "ABC")

    def test_array_payload(self):
        self.assertEqual(infer.extract_json_payload('[1,2,3]'), [1, 2, 3])

    def test_garbage(self):
        self.assertIsNone(infer.extract_json_payload("no json here"))
        self.assertIsNone(infer.extract_json_payload(""))
        self.assertIsNone(infer.extract_json_payload(None))


if __name__ == "__main__":
    unittest.main()
