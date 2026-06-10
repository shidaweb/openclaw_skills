"""v15 §R3 — send journal: double-send prevention + crash resume."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import send_journal as sj


class TestShouldSkipResume(unittest.TestCase):
    def test_unverified_attempt_is_skipped(self) -> None:
        entries = [
            {"target_id": "t1", "phase": "submit_attempted"},
        ]
        self.assertTrue(sj.should_skip_resume(entries, "t1"))

    def test_verified_attempt_is_not_skipped(self) -> None:
        entries = [
            {"target_id": "t1", "phase": "submit_attempted"},
            {"target_id": "t1", "phase": "verified", "outcome": "sent_ok"},
        ]
        self.assertFalse(sj.should_skip_resume(entries, "t1"))

    def test_new_attempt_after_verified_reopens(self) -> None:
        entries = [
            {"target_id": "t1", "phase": "submit_attempted"},
            {"target_id": "t1", "phase": "verified", "outcome": "not_confirmed"},
            {"target_id": "t1", "phase": "submit_attempted"},
        ]
        self.assertTrue(sj.should_skip_resume(entries, "t1"))

    def test_unknown_target_not_skipped(self) -> None:
        self.assertFalse(sj.should_skip_resume([], "tX"))
        self.assertFalse(
            sj.should_skip_resume([{"target_id": "t1", "phase": "submit_attempted"}], "t2")
        )

    def test_unverified_ids_set(self) -> None:
        entries = [
            {"target_id": "a", "phase": "submit_attempted"},
            {"target_id": "b", "phase": "submit_attempted"},
            {"target_id": "b", "phase": "verified", "outcome": "sent_ok"},
            {"target_id": "c", "phase": "verified", "outcome": "no_click"},
        ]
        self.assertEqual(sj.unverified_attempt_ids(entries), {"a"})


class TestJournalFile(unittest.TestCase):
    def test_append_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            sj.append_journal(data, "t1", sj.PHASE_SUBMIT_ATTEMPTED, form_url="https://x.jp/c")
            sj.append_journal(data, "t1", sj.PHASE_VERIFIED, outcome="sent_ok")
            rows = sj.load_journal(data)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["phase"], "submit_attempted")
            self.assertEqual(rows[0]["form_url"], "https://x.jp/c")
            self.assertEqual(rows[1]["outcome"], "sent_ok")
            self.assertTrue(all("ts" in r for r in rows))
            self.assertFalse(sj.should_skip_resume(rows, "t1"))

    def test_load_tolerates_garbage_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            p = sj.journal_path(data)
            p.write_text('{"target_id": "t1", "phase": "submit_attempted"}\nnot-json\n\n')
            rows = sj.load_journal(data)
            self.assertEqual(len(rows), 1)
            self.assertTrue(sj.should_skip_resume(rows, "t1"))

    def test_missing_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sj.load_journal(Path(tmp)), [])


if __name__ == "__main__":
    unittest.main()
