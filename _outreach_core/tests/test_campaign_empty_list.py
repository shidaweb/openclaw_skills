"""v32 FX6 — an empty LIST phase ends cleanly instead of crash-looping.

Production 2026-06-26/27: a stale ``--ids doorman_v5_*`` filter made
bootstrap write 0 targets → ``RuntimeError: campaign failed in list`` →
the supervisor burned 3 futile restarts per occurrence. These tests pin
the classification helpers and the exit-code policy.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _outreach_core import campaign as C  # noqa: E402


def _result(status: str, stopped_after: str, list_detail: dict | None) -> C.CampaignResult:
    phases = []
    if list_detail is not None:
        phases.append(C.PhaseResult("list", status="failed", detail=list_detail))
    ctx = C.CampaignContext(
        brief_id="b1", persona_id="p1", channel="jp_form",
        skill="jp-form-outreach", data_dir=Path("/tmp/x"),
    )
    return C.CampaignResult(
        context=ctx, phases=phases, status=status,
        stopped_after=stopped_after, reconciled=False,
    )


class TestIsEmptyListResult(unittest.TestCase):
    def test_matches_no_targets_list_failure(self):
        r = _result("failed", "list", {"reason": "no targets"})
        self.assertTrue(C.is_empty_list_result(r))

    def test_other_list_failure_is_not_empty(self):
        r = _result("failed", "list", {"reason": "yaml parse error"})
        self.assertFalse(C.is_empty_list_result(r))

    def test_failure_in_other_phase_is_not_empty(self):
        r = _result("failed", "enrich", {"reason": "no targets"})
        self.assertFalse(C.is_empty_list_result(r))

    def test_success_is_not_empty(self):
        r = _result("ok", "send", None)
        self.assertFalse(C.is_empty_list_result(r))


class TestEmptyListExitPolicy(unittest.TestCase):
    def test_stale_ids_filter_is_input_error(self):
        # exit 2 = deterministic input error; the supervisor never retries it
        self.assertEqual(C.empty_list_exit_code(["doorman_v5_01"]), 2)

    def test_exhausted_brief_is_clean_success(self):
        self.assertEqual(C.empty_list_exit_code(None), 0)
        self.assertEqual(C.empty_list_exit_code([]), 0)

    def test_messages_explain_the_cause(self):
        with_ids = C.empty_list_message("doorman-ai", ["doorman_v5_01"])
        self.assertIn("doorman_v5_01", with_ids)
        self.assertIn("--ids", with_ids)
        without = C.empty_list_message("doorman-ai", None)
        self.assertIn("送信済み", without)
        self.assertIn("doorman-ai", without)


if __name__ == "__main__":
    unittest.main()
