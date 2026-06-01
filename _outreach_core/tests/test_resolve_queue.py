"""Tests for the resolver queue + actionable messages (resolve_queue.py, v6 §16)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _outreach_core import resolve_queue as Q  # noqa: E402


def _entry(tid="park24", **kw):
    base = {
        "target_id": tid,
        "name": "パーク24株式会社",
        "reason_class": "first_submit_not_found",
        "reason": "first submit button not found (flow=confirm)",
        "form_url": "https://www.park24.co.jp/contact/",
        "diagnostics": {
            "url": "https://www.park24.co.jp/contact/confirm",
            "buttons": ["お問い合わせを送信する", "入力内容を修正する", "トップへ戻る"],
            "snapshot_path": "data/resolve_snapshot_park24.txt",
        },
    }
    base.update(kw)
    return base


class TestQueue(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_enqueue_and_read(self):
        Q.enqueue(self.dir, _entry())
        rows = Q.read_queue(self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target_id"], "park24")
        self.assertEqual(rows[0]["status"], "pending")

    def test_dedup_by_target_id(self):
        Q.enqueue(self.dir, _entry())
        Q.enqueue(self.dir, _entry(reason="updated"))
        rows = Q.read_queue(self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "updated")

    def test_pending_excludes_resolved(self):
        Q.enqueue(self.dir, _entry("a"))
        Q.enqueue(self.dir, _entry("b"))
        Q.mark(self.dir, "a", "resolved")
        pend = Q.pending(self.dir)
        self.assertEqual([r["target_id"] for r in pend], ["b"])

    def test_mark_sets_status_and_note(self):
        Q.enqueue(self.dir, _entry("a"))
        self.assertTrue(Q.mark(self.dir, "a", "skipped", note="deep resolver failed"))
        row = Q.read_queue(self.dir)[0]
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["note"], "deep resolver failed")

    def test_mark_missing_returns_false(self):
        self.assertFalse(Q.mark(self.dir, "ghost", "resolved"))

    def test_remove_and_clear(self):
        Q.enqueue(self.dir, _entry("a"))
        Q.enqueue(self.dir, _entry("b"))
        Q.remove(self.dir, "a")
        self.assertEqual(len(Q.read_queue(self.dir)), 1)
        Q.clear(self.dir)
        self.assertEqual(Q.read_queue(self.dir), [])

    def test_corrupt_lines_skipped(self):
        Q.queue_path(self.dir).write_text('{"target_id":"a","status":"pending"}\n{bad\n', encoding="utf-8")
        rows = Q.read_queue(self.dir)
        self.assertEqual(len(rows), 1)


class TestMessages(unittest.TestCase):
    def test_actionable_message_has_buttons_and_no_susume(self):
        msg = Q.build_actionable_message(_entry(), auto_resolver=True)
        # The fix: show candidate buttons (the real submit target the regex missed)
        self.assertIn("お問い合わせを送信する", msg)
        # And do NOT tell the user to type 進めて (which only retries the same failure)
        self.assertNotIn("進めて", msg)
        self.assertIn("skip", msg)

    def test_message_mentions_auto_resolver_when_autonomous(self):
        self.assertIn("自動リゾルバ", Q.build_actionable_message(_entry(), auto_resolver=True))

    def test_message_mentions_command_when_not_autonomous(self):
        self.assertIn("resolve-queue", Q.build_actionable_message(_entry(), auto_resolver=False))

    def test_humanize_reason(self):
        self.assertIn("最終送信ボタン", Q.humanize_reason("confirm_submit_not_found"))

    def test_queue_summary(self):
        self.assertEqual(Q.queue_summary([]), "リゾルバキューは空です。")
        s = Q.queue_summary([_entry("a"), _entry("b", reason_class="wrong_form_type")])
        self.assertIn("保留 2 件", s)


if __name__ == "__main__":
    unittest.main()
