"""Tests for the reCAPTCHA avoidance engine (_outreach_core/avoidance.py, v6 §3.5)."""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _outreach_core import avoidance as A  # noqa: E402


class TestDomainOf(unittest.TestCase):
    def test_strips_www_and_lowercases(self):
        self.assertEqual(A.domain_of("https://www.Example.co.jp/contact"), "example.co.jp")

    def test_bare_host(self):
        self.assertEqual(A.domain_of("example.com"), "example.com")

    def test_empty(self):
        self.assertEqual(A.domain_of(None), "")
        self.assertEqual(A.domain_of(""), "")


class TestLearning(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.url = "https://foo.example.jp/contact"

    def tearDown(self):
        self._tmp.cleanup()

    def test_initial_not_unviable(self):
        self.assertFalse(A.domain_status(self.dir, self.url)["unviable"])

    def test_becomes_unviable_after_repeated_blocks(self):
        A.record_outcome(self.dir, self.url, A.OUTCOME_CAPTCHA_BLOCKED)
        A.record_outcome(self.dir, self.url, A.OUTCOME_CAPTCHA_BLOCKED)
        A.record_outcome(self.dir, self.url, A.OUTCOME_SENT)
        st = A.domain_status(self.dir, self.url)
        self.assertEqual(st["attempts"], 3)
        self.assertEqual(st["captcha_blocks"], 2)
        self.assertTrue(st["unviable"])

    def test_not_unviable_if_mostly_succeeds(self):
        A.record_outcome(self.dir, self.url, A.OUTCOME_CAPTCHA_BLOCKED)
        for _ in range(5):
            A.record_outcome(self.dir, self.url, A.OUTCOME_SENT)
        st = A.domain_status(self.dir, self.url)
        self.assertFalse(st["unviable"])  # 1/6 block rate < 0.6

    def test_skipped_does_not_inflate_attempts(self):
        A.record_outcome(self.dir, self.url, A.OUTCOME_SENT)
        before = A.domain_status(self.dir, self.url)["attempts"]
        A.record_outcome(self.dir, self.url, A.OUTCOME_SKIPPED)
        self.assertEqual(before, A.domain_status(self.dir, self.url)["attempts"])

    def test_outcomes_keyed_by_registrable_domain(self):
        A.record_outcome(self.dir, "https://www.foo.example.jp/a", A.OUTCOME_CAPTCHA_BLOCKED)
        A.record_outcome(self.dir, "https://foo.example.jp/b", A.OUTCOME_CAPTCHA_BLOCKED)
        st = A.domain_status(self.dir, "foo.example.jp")
        self.assertEqual(st["captcha_blocks"], 2)  # same domain despite www / path

    def test_corrupt_stats_is_safe(self):
        A.stats_path(self.dir).write_text("{bad json", encoding="utf-8")
        self.assertEqual(A.read_stats(self.dir), {})
        self.assertFalse(A.domain_status(self.dir, self.url)["unviable"])


class TestUrlUnfriendlyLearning(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.url = "https://foo.example.jp/contact"

    def tearDown(self):
        self._tmp.cleanup()

    def test_initially_friendly(self):
        self.assertFalse(A.is_url_unfriendly(self.dir, self.url))

    def test_mark_and_check(self):
        A.mark_url_unfriendly(self.dir, self.url)
        self.assertTrue(A.is_url_unfriendly(self.dir, self.url))

    def test_per_domain(self):
        A.mark_url_unfriendly(self.dir, self.url)
        self.assertFalse(A.is_url_unfriendly(self.dir, "https://other.jp/x"))

    def test_mark_survives_outcome_records(self):
        A.mark_url_unfriendly(self.dir, self.url)
        A.record_outcome(self.dir, self.url, A.OUTCOME_CONTENT_REJECTED)
        A.record_outcome(self.dir, self.url, A.OUTCOME_SENT)
        self.assertTrue(A.is_url_unfriendly(self.dir, self.url))

    def test_content_rejected_counter(self):
        A.record_outcome(self.dir, self.url, A.OUTCOME_CONTENT_REJECTED)
        row = A.read_stats(self.dir)[A.domain_of(self.url)]
        self.assertEqual(row["content_rejected"], 1)


class TestAdaptiveWarmup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.url = "https://bar.example.jp/contact"

    def tearDown(self):
        self._tmp.cleanup()

    def test_base_when_no_history(self):
        self.assertEqual(A.recommended_warmup_sec(self.dir, self.url), A.DEFAULT_BASE_WARMUP_SEC)

    def test_bumps_per_block_and_caps(self):
        for _ in range(10):
            A.record_outcome(self.dir, self.url, A.OUTCOME_CAPTCHA_BLOCKED)
        self.assertEqual(A.recommended_warmup_sec(self.dir, self.url), A.DEFAULT_MAX_WARMUP_SEC)

    def test_config_override(self):
        cfg = {"avoidance": {"warmup": {"base_sec": 5, "bump_sec": 10, "max_sec": 30}}}
        A.record_outcome(self.dir, self.url, A.OUTCOME_CAPTCHA_BLOCKED)
        self.assertEqual(A.recommended_warmup_sec(self.dir, self.url, cfg), 15)


class TestPacingAndWindow(unittest.TestCase):
    def test_typing_delay_within_range(self):
        for _ in range(50):
            v = A.typing_delay_ms()
            lo, hi = A.DEFAULT_TYPING_DELAY_MS
            self.assertTrue(lo <= v <= hi)

    def test_send_window(self):
        self.assertTrue(A.within_send_window(None, datetime(2026, 5, 31, 12)))
        self.assertFalse(A.within_send_window(None, datetime(2026, 5, 31, 3)))

    def test_send_window_always_when_full_day(self):
        cfg = {"avoidance": {"pacing": {"send_window": [0, 24]}}}
        self.assertTrue(A.within_send_window(cfg, datetime(2026, 5, 31, 3)))

    def test_config_send_window_override(self):
        cfg = {"avoidance": {"pacing": {"send_window": [7, 22]}}}
        self.assertTrue(A.within_send_window(cfg, datetime(2026, 5, 31, 21)))
        self.assertFalse(A.within_send_window(cfg, datetime(2026, 5, 31, 6)))


if __name__ == "__main__":
    unittest.main()
