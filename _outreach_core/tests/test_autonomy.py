"""Tests for the autonomous operation profile (_outreach_core/autonomy.py, v5 §12)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _outreach_core import autonomy as A  # noqa: E402


AUTO = {
    "autonomy": {
        "mode": "autonomous",
        "draft_self_score": {"enabled": True, "threshold": 0.8, "on_error": "send"},
        "upfront_approval": {"required": True, "sample_drafts": 2},
    }
}


class TestConfigParsing(unittest.TestCase):
    def test_defaults_when_no_block(self):
        cfg = A.autonomy_config({})
        self.assertEqual(cfg["mode"], "supervised")
        self.assertEqual(cfg["on_blocker"], "skip_and_log")
        self.assertTrue(cfg["draft_self_score"]["enabled"])
        self.assertAlmostEqual(cfg["draft_self_score"]["threshold"], 0.75)
        self.assertTrue(cfg["upfront_approval"]["required"])

    def test_defaults_when_none(self):
        self.assertFalse(A.is_autonomous(None))
        self.assertEqual(A.blocker_action(None), "escalate")

    def test_autonomous_mode_detected(self):
        self.assertTrue(A.is_autonomous(AUTO))
        self.assertEqual(A.score_threshold(AUTO), 0.8)
        self.assertEqual(A.sample_draft_count(AUTO), 2)

    def test_supervised_never_autonomous_helpers(self):
        sup = {"autonomy": {"mode": "supervised"}}
        self.assertFalse(A.is_autonomous(sup))
        self.assertFalse(A.self_score_enabled(sup))
        self.assertFalse(A.upfront_approval_required(sup))
        # Supervised always escalates regardless of on_blocker override.
        sup2 = {"autonomy": {"mode": "supervised", "on_blocker": "skip_and_log"}}
        self.assertEqual(A.blocker_action(sup2), "escalate")

    def test_blocker_override_in_autonomous(self):
        cfg = {"autonomy": {"mode": "autonomous", "on_blocker": "escalate"}}
        self.assertEqual(A.blocker_action(cfg), "escalate")

    def test_self_score_enabled_requires_autonomous(self):
        # enabled flag true but supervised → still off
        cfg = {"autonomy": {"mode": "supervised", "draft_self_score": {"enabled": True}}}
        self.assertFalse(A.self_score_enabled(cfg))
        self.assertTrue(A.self_score_enabled(AUTO))

    def test_bad_types_fall_back_to_defaults(self):
        cfg = {"autonomy": {"draft_self_score": {"threshold": "notanumber"}}}
        self.assertAlmostEqual(A.score_threshold(cfg), 0.75)


class TestScoreParsing(unittest.TestCase):
    def test_parse_clean_json(self):
        r = A.parse_score_response('{"score": 0.9, "verdict": "send", "reason": "ok"}')
        self.assertEqual(r["score"], 0.9)
        self.assertEqual(r["verdict"], "send")

    def test_parse_with_surrounding_prose(self):
        r = A.parse_score_response('採点結果です: {"score":0.5,"verdict":"skip","reason":"弱い"} 以上')
        self.assertEqual(r["score"], 0.5)
        self.assertEqual(r["verdict"], "skip")

    def test_parse_clamps_range(self):
        self.assertEqual(A.parse_score_response('{"score": 1.7}')["score"], 1.0)
        self.assertEqual(A.parse_score_response('{"score": -3}')["score"], 0.0)

    def test_parse_invalid(self):
        self.assertIsNone(A.parse_score_response("no json"))
        self.assertIsNone(A.parse_score_response(None))
        self.assertIsNone(A.parse_score_response('{"noscore": 1}'))
        self.assertIsNone(A.parse_score_response('{"score": "abc"}'))


class TestSelfScoreDraft(unittest.TestCase):
    DRAFT = {"name": "Acme", "draft": {"subject": "s", "body": "本文"}}

    def test_send_above_threshold(self):
        d = A.self_score_draft(
            self.DRAFT, AUTO,
            oc_infer_fn=lambda p, model=None: '{"score":0.95,"verdict":"send","reason":"good"}',
        )
        self.assertTrue(d["send"])
        self.assertEqual(d["score"], 0.95)
        self.assertFalse(d["errored"])

    def test_skip_below_threshold(self):
        d = A.self_score_draft(
            self.DRAFT, AUTO,
            oc_infer_fn=lambda p, model=None: '{"score":0.4,"verdict":"send","reason":"meh"}',
        )
        self.assertFalse(d["send"])

    def test_explicit_skip_verdict_overrides_high_score(self):
        # Even a high score cannot override an explicit skip verdict.
        d = A.self_score_draft(
            self.DRAFT, AUTO,
            oc_infer_fn=lambda p, model=None: '{"score":0.99,"verdict":"skip","reason":"不適切"}',
        )
        self.assertFalse(d["send"])

    def test_inference_exception_fails_open_to_send(self):
        def boom(p, model=None):
            raise RuntimeError("network down")
        d = A.self_score_draft(self.DRAFT, AUTO, oc_infer_fn=boom)
        self.assertTrue(d["send"])  # on_error default "send"
        self.assertTrue(d["errored"])

    def test_inference_exception_fails_closed_when_configured(self):
        cfg = {"autonomy": {"mode": "autonomous",
                            "draft_self_score": {"on_error": "skip"}}}
        def boom(p, model=None):
            raise RuntimeError("x")
        d = A.self_score_draft(self.DRAFT, cfg, oc_infer_fn=boom)
        self.assertFalse(d["send"])

    def test_unparseable_response_uses_on_error(self):
        d = A.self_score_draft(
            self.DRAFT, AUTO,
            oc_infer_fn=lambda p, model=None: "garbage non-json",
        )
        self.assertTrue(d["send"])
        self.assertTrue(d["errored"])


class TestUpfrontApprovalState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_initial_not_approved(self):
        self.assertFalse(A.is_upfront_approved(self.dir))

    def test_pending_does_not_approve(self):
        A.mark_pending_approval(self.dir, {"sendable": 5})
        self.assertFalse(A.is_upfront_approved(self.dir))
        self.assertIsNotNone(A.read_autonomy_state(self.dir)["pending"])

    def test_approve_flips_and_clears_pending(self):
        A.mark_pending_approval(self.dir, {"sendable": 5})
        st = A.mark_upfront_approved(self.dir, by="cli", note="go")
        self.assertTrue(A.is_upfront_approved(self.dir))
        self.assertFalse(st["_was_already_approved"])
        self.assertIsNone(A.read_autonomy_state(self.dir)["pending"])

    def test_approve_is_idempotent(self):
        A.mark_upfront_approved(self.dir)
        st = A.mark_upfront_approved(self.dir)
        self.assertTrue(st["_was_already_approved"])

    def test_revoke(self):
        A.mark_upfront_approved(self.dir)
        A.revoke_upfront_approval(self.dir)
        self.assertFalse(A.is_upfront_approved(self.dir))

    def test_corrupt_state_file_is_safe(self):
        A.autonomy_state_path(self.dir).write_text("{not json", encoding="utf-8")
        self.assertFalse(A.is_upfront_approved(self.dir))


if __name__ == "__main__":
    unittest.main()
