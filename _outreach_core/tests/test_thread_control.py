"""Tests for inbound Slack thread control (v27 stop-on-reply)."""

from __future__ import annotations

import unittest

from _outreach_core import thread_control as tc


class TestFilterReplies(unittest.TestCase):
    def _msgs(self):
        return [
            {"ts": "100.0", "text": "root", "user": "U1"},          # thread root (<= after)
            {"ts": "150.0", "bot_id": "B1", "text": "[send] 開始"},   # our bot post
            {"ts": "160.0", "subtype": "channel_join", "text": "joined", "user": "U2"},
            {"ts": "170.0", "text": "  ", "user": "U2"},            # empty
            {"ts": "180.0", "text": "止めて", "user": "U2"},          # human, keep
            {"ts": "190.0", "text": "あとこれも", "user": "U3"},       # human, keep
        ]

    def test_keeps_only_new_human_text(self):
        out = tc.filter_new_human_replies(self._msgs(), after_ts=120.0)
        self.assertEqual([m["text"] for m in out], ["止めて", "あとこれも"])

    def test_after_ts_excludes_older(self):
        out = tc.filter_new_human_replies(self._msgs(), after_ts=185.0)
        self.assertEqual([m["text"] for m in out], ["あとこれも"])

    def test_excludes_own_bot_user(self):
        msgs = [{"ts": "200.0", "text": "止めて", "user": "UBOT"}]
        out = tc.filter_new_human_replies(msgs, after_ts=100.0, bot_user_id="UBOT")
        self.assertEqual(out, [])

    def test_empty_and_none(self):
        self.assertEqual(tc.filter_new_human_replies(None, after_ts=0.0), [])
        self.assertEqual(tc.filter_new_human_replies([], after_ts=0.0), [])


class TestKeywordAndParse(unittest.TestCase):
    def test_keyword_japanese(self):
        for t in ("止めて", "一旦ストップで", "中断してください", "やめて"):
            self.assertTrue(tc.keyword_stop(t), t)

    def test_keyword_english_caseless(self):
        self.assertTrue(tc.keyword_stop("Please STOP now"))
        self.assertTrue(tc.keyword_stop("abort it"))

    def test_keyword_negative(self):
        for t in ("いい感じだね", "3番だけ直して", "進捗どう？"):
            self.assertFalse(tc.keyword_stop(t), t)

    def test_parse_plain_json(self):
        d = tc.parse_stop_decision('{"stop": true, "reason": "user asked"}')
        self.assertTrue(d["stop"])
        self.assertEqual(d["reason"], "user asked")

    def test_parse_fenced_json(self):
        d = tc.parse_stop_decision('```json\n{"stop": false, "reason": "question"}\n```')
        self.assertFalse(d["stop"])

    def test_parse_stringy_bool(self):
        self.assertTrue(tc.parse_stop_decision('{"stop": "yes"}')["stop"])

    def test_parse_garbage_is_safe(self):
        self.assertFalse(tc.parse_stop_decision("not json at all")["stop"])
        self.assertFalse(tc.parse_stop_decision(None)["stop"])


class TestInterpretStop(unittest.TestCase):
    def test_keyword_shortcuts_llm(self):
        called = []
        stop, reason = tc.interpret_stop("止めて", infer=lambda p: called.append(p) or "{}")
        self.assertTrue(stop)
        self.assertEqual(reason, "keyword")
        self.assertEqual(called, [])  # LLM not consulted

    def test_llm_says_stop(self):
        stop, _ = tc.interpret_stop(
            "今日のところは一区切りにしよう",
            infer=lambda p: '{"stop": true, "reason": "wrap up"}',
        )
        self.assertTrue(stop)

    def test_llm_says_continue(self):
        stop, _ = tc.interpret_stop(
            "3番の会社だけ文面直して",
            infer=lambda p: '{"stop": false, "reason": "edit one"}',
        )
        self.assertFalse(stop)

    def test_llm_error_is_safe(self):
        def boom(_p):
            raise RuntimeError("llm down")

        stop, reason = tc.interpret_stop("曖昧な文", infer=boom)
        self.assertFalse(stop)
        self.assertEqual(reason, "infer_error")


class TestShouldStop(unittest.TestCase):
    def _watcher(self, messages, *, infer=None):
        def fetcher(**_kw):
            return messages, ""

        return tc.ThreadStopWatcher(
            channel="C1", thread_ts="100.0", token="xoxb-x",
            baseline_ts=100.0, infer=infer, fetcher=fetcher,
        )

    def test_stop_on_keyword_reply(self):
        w = self._watcher([{"ts": "200.0", "text": "止めて", "user": "U2"}])
        stop, reason = w.should_stop()
        self.assertTrue(stop)
        self.assertIn("止めて", reason)

    def test_no_new_replies_continues(self):
        w = self._watcher([{"ts": "200.0", "bot_id": "B1", "text": "[send] tick"}])
        self.assertEqual(w.should_stop(), (False, None))

    def test_cursor_advances_no_double_fire(self):
        w = self._watcher([{"ts": "200.0", "text": "進捗は？", "user": "U2"}],
                          infer=lambda p: '{"stop": false}')
        self.assertEqual(w.should_stop(), (False, None))
        # Same message must not be re-evaluated (cursor moved past 200.0).
        self.assertEqual(w._last_ts, 200.0)

    def test_disabled_when_unconfigured(self):
        w = tc.ThreadStopWatcher(channel="", thread_ts="", token="")
        self.assertFalse(w.enabled)
        self.assertEqual(w.should_stop(), (False, None))

    def test_fetch_error_does_not_stop(self):
        def fetcher(**_kw):
            return None, "missing_scope"

        w = tc.ThreadStopWatcher(
            channel="C1", thread_ts="100.0", token="t",
            baseline_ts=100.0, fetcher=fetcher,
        )
        # Patch notify so the one-time scope warning doesn't hit the network.
        w._notify = lambda *_a, **_k: None
        self.assertEqual(w.should_stop(), (False, None))


if __name__ == "__main__":
    unittest.main()
