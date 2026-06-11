"""Tests for batch backend-parity validation (v21 Phase 2) — pure parts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _outreach_core.tools import backend_parity_batch as bpb


class TestCollectUrls(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.dir = Path(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_queue(self, rows):
        (self.dir / "resolve_queue.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8",
        )

    def test_reads_form_url_and_dedupes(self):
        self._write_queue([
            {"form_url": "https://a.example/contact"},
            {"form_url": "https://a.example/contact"},   # dup
            {"form_url": "https://b.example/inquiry"},
        ])
        urls = bpb.collect_urls(self.dir)
        self.assertEqual(urls, ["https://a.example/contact", "https://b.example/inquiry"])

    def test_falls_back_to_diagnostics_url(self):
        self._write_queue([{"diagnostics": {"url": "https://c.example/form"}}])
        self.assertEqual(bpb.collect_urls(self.dir), ["https://c.example/form"])

    def test_extra_urls_first_and_deduped(self):
        self._write_queue([{"form_url": "https://a.example/x"}])
        urls = bpb.collect_urls(self.dir, extra=["https://z.example/y", "https://a.example/x"])
        self.assertEqual(urls, ["https://z.example/y", "https://a.example/x"])

    def test_limit(self):
        self._write_queue([{"form_url": f"https://h{i}.example/c"} for i in range(10)])
        self.assertEqual(len(bpb.collect_urls(self.dir, limit=3)), 3)

    def test_ignores_non_http_and_missing(self):
        self._write_queue([{"form_url": ""}, {"name": "no url"},
                           {"form_url": "ftp://x/y"}, {"form_url": "https://ok.example/c"}])
        self.assertEqual(bpb.collect_urls(self.dir), ["https://ok.example/c"])

    def test_missing_file_is_empty(self):
        self.assertEqual(bpb.collect_urls(self.dir), [])


class TestSummarize(unittest.TestCase):
    def _row(self, match=True, single=False, diffs=None):
        return {"url": "u", "by_backend": {},
                "comparison": {"match": match, "single": single, "diffs": diffs or {}}}

    def test_all_match(self):
        s = bpb.summarize_batch([self._row(), self._row()])
        self.assertEqual(s, {"total": 2, "match": 2, "divergence": 0, "all_match": True})

    def test_divergence_counted(self):
        s = bpb.summarize_batch([self._row(), self._row(match=False, diffs={"page_state": ["a", "b"]})])
        self.assertEqual(s["match"], 1)
        self.assertEqual(s["divergence"], 1)
        self.assertFalse(s["all_match"])

    def test_single_backend_not_divergence(self):
        s = bpb.summarize_batch([self._row(match=True, single=True)])
        self.assertTrue(s["all_match"])
        self.assertEqual(s["divergence"], 0)


class TestFormat(unittest.TestCase):
    def test_format_marks_and_summary(self):
        rows = [{
            "url": "https://x/c",
            "by_backend": {
                "openclaw": {"ok": True, "page_state": "form_ok", "captcha_kind": "none",
                             "captcha_blocking": False, "textareas": 1, "submit_buttons": 1},
                "playwright": {"ok": True, "page_state": "no_form", "captcha_kind": "none",
                               "captcha_blocking": False, "textareas": 0, "submit_buttons": 0},
            },
            "comparison": {"match": False, "single": False,
                           "diffs": {"page_state": ["form_ok", "no_form"]}},
        }]
        out = bpb.format_batch({"rows": rows, "summary": bpb.summarize_batch(rows)},
                               ["openclaw", "playwright"])
        self.assertIn("✗ https://x/c", out)
        self.assertIn("page_state: 'form_ok' vs 'no_form'", out)
        self.assertIn("DIVERGENCE", out)


if __name__ == "__main__":
    unittest.main()
