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


def _tabs(*ids):
    return {"tabs": [{"targetId": t, "type": "page", "url": f"https://{t}.co.jp/"}
                     for t in ids]}


class TestClosableOverflowOwned(unittest.TestCase):
    """v32 FX3 — the per-run cap must be structurally unable to touch a
    sibling brief's tabs on the shared browser."""

    def test_siblings_tabs_never_returned_even_far_over_global_cap(self):
        # 6 open tabs, we own only 2, cap 1 → close exactly our oldest own tab
        payload = _tabs("SIB1", "SIB2", "SIB3", "OWN1", "SIB4", "OWN2")
        out = T.closable_overflow_owned(
            payload, owned={"OWN1", "OWN2"}, protect=set(), cap=1
        )
        self.assertEqual(out, ["OWN1"])
        for sib in ("SIB1", "SIB2", "SIB3", "SIB4"):
            self.assertNotIn(sib, out)

    def test_under_own_cap_closes_nothing(self):
        payload = _tabs("SIB1", "SIB2", "SIB3", "SIB4", "OWN1")
        self.assertEqual(
            T.closable_overflow_owned(payload, owned={"OWN1"}, protect=set(), cap=1),
            [],
        )

    def test_protect_and_keep_newest_still_honored(self):
        payload = _tabs("OWN1", "OWN2", "OWN3", "OWN4")
        owned = {"OWN1", "OWN2", "OWN3", "OWN4"}
        out = T.closable_overflow_owned(
            payload, owned=owned, protect={"OWN1"}, cap=2
        )
        # OWN1 protected (resolver), OWN4 newest → OWN2/OWN3 candidates,
        # over = 2 → both closed
        self.assertEqual(out, ["OWN2", "OWN3"])

    def test_empty_owned_set_degrades_to_nothing(self):
        payload = _tabs("SIB1", "SIB2", "SIB3", "SIB4", "SIB5")
        self.assertEqual(
            T.closable_overflow_owned(payload, owned=set(), protect=set(), cap=1),
            [],
        )

    def test_owned_but_already_closed_ids_ignored(self):
        payload = _tabs("OWN1")
        out = T.closable_overflow_owned(
            payload, owned={"OWN1", "GONE1", "GONE2", "GONE3"}, protect=set(), cap=1
        )
        self.assertEqual(out, [])


class TestOrphanTabIds(unittest.TestCase):
    """v32 FX3 — dead-run sweep semantics."""

    def test_dead_run_tabs_minus_resolver_pending(self):
        out = T.orphan_tab_ids(
            ["T1", "T2", "T3"],
            open_page_ids={"T1", "T2", "T3", "UNKNOWN"},
            resolver_tab_ids={"T2"},   # kept open on purpose for the resolver
        )
        self.assertEqual(out, {"T1", "T3"})

    def test_unknown_open_tabs_never_touched(self):
        out = T.orphan_tab_ids(["T1"], open_page_ids={"UNKNOWN", "T1"},
                               resolver_tab_ids=set())
        self.assertEqual(out, {"T1"})
        self.assertNotIn("UNKNOWN", out)

    def test_already_closed_recorded_tabs_skipped(self):
        out = T.orphan_tab_ids(["GONE"], open_page_ids={"OTHER"},
                               resolver_tab_ids=set())
        self.assertEqual(out, set())

    def test_empty_or_none_record_is_safe(self):
        self.assertEqual(T.orphan_tab_ids(None, {"T1"}, set()), set())
        self.assertEqual(T.orphan_tab_ids([], {"T1"}, set()), set())
