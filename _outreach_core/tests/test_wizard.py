"""wizard (v30 §WS-A) — pure state machine for multi-step form traversal."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import wizard as wz


class TestWizardConfig(unittest.TestCase):
    def test_defaults_match_historical_tolerances(self) -> None:
        # Locked defaults: the run loop has historically exited at
        # validation_rounds > 2 and no_progress >= 2. wizard.py preserves both
        # so introducing the gate does not tighten well-behaved forms.
        cfg = wz.WizardConfig()
        self.assertEqual(cfg.max_hops, 4)
        self.assertEqual(cfg.max_same_button_clicks, 3)
        self.assertEqual(cfg.max_no_progress, 2)
        self.assertEqual(cfg.max_validation_rounds, 2)


class TestRecordClick(unittest.TestCase):
    def test_same_button_increments_streak(self) -> None:
        st = wz.WizardState()
        wz.record_click(st, "次へ")
        self.assertEqual(st.same_button_count, 1)
        wz.record_click(st, "次へ")
        self.assertEqual(st.same_button_count, 2)
        wz.record_click(st, "次へ")
        self.assertEqual(st.same_button_count, 3)

    def test_different_button_resets_streak(self) -> None:
        st = wz.WizardState()
        wz.record_click(st, "次へ")
        wz.record_click(st, "次へ")
        wz.record_click(st, "送信")
        self.assertEqual(st.same_button_count, 1)
        self.assertEqual(st.last_button_text, "送信")

    def test_whitespace_normalised(self) -> None:
        # 「次へ」 and 「次へ 」 are the same button — the streak must build.
        st = wz.WizardState()
        wz.record_click(st, "次へ")
        wz.record_click(st, "次へ ")
        wz.record_click(st, " 次へ\n")
        self.assertEqual(st.same_button_count, 3)

    def test_empty_button_does_not_extend_streak(self) -> None:
        st = wz.WizardState()
        wz.record_click(st, "次へ")
        wz.record_click(st, "")
        # The empty click is an unidentified action; do not let it propagate
        # the previous streak nor start a new one.
        self.assertEqual(st.same_button_count, 0)


class TestBumpAfterObservation(unittest.TestCase):
    def test_hop_advances_each_observation(self) -> None:
        st = wz.WizardState()
        wz.bump_after_observation(st, observation_state="input", fingerprint="fp1")
        wz.bump_after_observation(st, observation_state="input", fingerprint="fp2")
        self.assertEqual(st.hop, 2)

    def test_same_fingerprint_increments_no_progress(self) -> None:
        st = wz.WizardState()
        wz.bump_after_observation(st, observation_state="input", fingerprint="fp1")
        wz.bump_after_observation(st, observation_state="input", fingerprint="fp1")
        self.assertEqual(st.no_progress, 1)
        wz.bump_after_observation(st, observation_state="input", fingerprint="fp1")
        self.assertEqual(st.no_progress, 2)

    def test_fingerprint_change_resets_no_progress(self) -> None:
        st = wz.WizardState()
        wz.bump_after_observation(st, observation_state="input", fingerprint="fp1")
        wz.bump_after_observation(st, observation_state="input", fingerprint="fp1")
        wz.bump_after_observation(st, observation_state="input", fingerprint="fp2")
        self.assertEqual(st.no_progress, 0)

    def test_validation_error_increments_rounds(self) -> None:
        st = wz.WizardState()
        wz.bump_after_observation(
            st, observation_state="validation_error", fingerprint="fp1"
        )
        wz.bump_after_observation(
            st, observation_state="validation_error", fingerprint="fp2"
        )
        self.assertEqual(st.validation_rounds, 2)

    def test_same_button_streak_preserved_across_fingerprint_flicker(self) -> None:
        # Fujisoft pattern: state stays "input" through 3 clicks, but the
        # fingerprint shifts each time (validation banner re-renders). The
        # stuck guard must NOT clear the streak on a fingerprint flicker.
        st = wz.WizardState()
        wz.bump_after_observation(st, observation_state="input", fingerprint="A")
        wz.record_click(st, "次へ")
        wz.bump_after_observation(st, observation_state="input", fingerprint="B")
        wz.record_click(st, "次へ")
        wz.bump_after_observation(st, observation_state="input", fingerprint="C")
        wz.record_click(st, "次へ")
        self.assertEqual(st.same_button_count, 3)

    def test_same_button_streak_cleared_on_state_transition(self) -> None:
        # input → confirm is a real phase change; the streak resets so a
        # subsequent click on a *different* page does not inherit history.
        st = wz.WizardState()
        wz.bump_after_observation(st, observation_state="input", fingerprint="A")
        wz.record_click(st, "次へ")
        wz.bump_after_observation(st, observation_state="input", fingerprint="A")
        wz.record_click(st, "次へ")
        self.assertEqual(st.same_button_count, 2)
        wz.bump_after_observation(st, observation_state="confirm", fingerprint="B")
        self.assertEqual(st.same_button_count, 0)


class TestComputeStuckReason(unittest.TestCase):
    def test_clean_state_is_not_stuck(self) -> None:
        st = wz.WizardState()
        self.assertIsNone(wz.compute_stuck_reason(st, wz.WizardConfig()))

    def test_fujisoft_same_button_three_clicks_is_stuck(self) -> None:
        # Reproduces 2026-06-29 Fujisoft: 3x 次へ on the same input page.
        st = wz.WizardState()
        wz.record_click(st, "次へ")
        wz.record_click(st, "次へ")
        wz.record_click(st, "次へ")
        reason = wz.compute_stuck_reason(st, wz.WizardConfig())
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertEqual(reason.code, wz.REASON_SAME_BUTTON)
        self.assertIn("次へ", reason.detail)
        self.assertIn("3", reason.detail)

    def test_super_studio_same_confirm_button_is_stuck(self) -> None:
        # Reproduces 2026-06-27 SUPER STUDIO: 3x 内容確認へ.
        st = wz.WizardState()
        for _ in range(3):
            wz.record_click(st, "内容確認へ")
        reason = wz.compute_stuck_reason(st, wz.WizardConfig())
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertEqual(reason.code, wz.REASON_SAME_BUTTON)
        self.assertIn("内容確認へ", reason.detail)

    def test_no_progress_threshold(self) -> None:
        # Two consecutive same-fingerprint observations is the threshold.
        st = wz.WizardState()
        wz.bump_after_observation(st, observation_state="input", fingerprint="A")
        wz.bump_after_observation(st, observation_state="input", fingerprint="A")
        wz.bump_after_observation(st, observation_state="input", fingerprint="A")
        reason = wz.compute_stuck_reason(st, wz.WizardConfig())
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertEqual(reason.code, wz.REASON_NO_PROGRESS)

    def test_validation_rounds_priority_over_same_button(self) -> None:
        # If both validation rounds and same-button streak fire, the more
        # specific (validation) wins so Slack messages keep their diagnostic.
        st = wz.WizardState()
        st.validation_rounds = 3
        st.same_button_count = 3
        st.last_button_text = "次へ"
        reason = wz.compute_stuck_reason(st, wz.WizardConfig())
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertEqual(reason.code, wz.REASON_VALIDATION_UNRECOVERABLE)

    def test_max_hops_terminal(self) -> None:
        st = wz.WizardState(hop=5)
        reason = wz.compute_stuck_reason(st, wz.WizardConfig())
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertEqual(reason.code, wz.REASON_MAX_HOPS)

    def test_max_hops_not_yet_triggered(self) -> None:
        # Exactly at the limit is still acceptable; only over the limit stucks.
        st = wz.WizardState(hop=4)
        self.assertIsNone(wz.compute_stuck_reason(st, wz.WizardConfig()))


class TestProgressReset(unittest.TestCase):
    def test_progress_reset_clears_same_button_and_no_progress(self) -> None:
        st = wz.WizardState(
            no_progress=2,
            same_button_count=2,
            last_button_text="次へ",
        )
        wz.reset_progress_counters(st)
        self.assertEqual(st.no_progress, 0)
        self.assertEqual(st.same_button_count, 0)
        # last_button_text is informational; resetting only clears streaks.

    def test_post_reset_not_stuck(self) -> None:
        # Production flow: a transient bounce primed the counters, but a real
        # phase transition then arrives. Reset must clear stuck signals.
        st = wz.WizardState(no_progress=2, same_button_count=3, last_button_text="次へ")
        wz.reset_progress_counters(st)
        self.assertIsNone(wz.compute_stuck_reason(st, wz.WizardConfig()))


class TestNextPhase(unittest.TestCase):
    def test_single_flow_always_final(self) -> None:
        for obs in ("input", "validation_error", "confirm"):
            self.assertEqual(
                wz.next_phase(
                    flow="single",
                    observation_state=obs,
                    confirm_seen=False,
                    has_visible_textarea=True,
                ),
                wz.PHASE_FINAL,
            )

    def test_confirm_flow_input_uses_first(self) -> None:
        self.assertEqual(
            wz.next_phase(
                flow="confirm",
                observation_state="input",
                confirm_seen=False,
                has_visible_textarea=True,
            ),
            wz.PHASE_FIRST,
        )

    def test_confirm_flow_confirm_uses_final(self) -> None:
        self.assertEqual(
            wz.next_phase(
                flow="confirm",
                observation_state="confirm",
                confirm_seen=True,
                has_visible_textarea=False,
            ),
            wz.PHASE_FINAL,
        )

    def test_confirm_flow_validation_with_textarea_is_first(self) -> None:
        # A validation bounce while a textarea is still visible means we are
        # still on the input page — fire the first-step cascade again.
        self.assertEqual(
            wz.next_phase(
                flow="confirm",
                observation_state="validation_error",
                confirm_seen=False,
                has_visible_textarea=True,
            ),
            wz.PHASE_FIRST,
        )

    def test_confirm_flow_validation_without_textarea_is_final(self) -> None:
        # A validation bounce without a textarea suggests the confirm page
        # surfaced an error on a residual field — fire the final cascade.
        self.assertEqual(
            wz.next_phase(
                flow="confirm",
                observation_state="validation_error",
                confirm_seen=True,
                has_visible_textarea=False,
            ),
            wz.PHASE_FINAL,
        )


class TestHappyPath(unittest.TestCase):
    """Walk a clean confirm-flow run through the wizard."""

    def test_legalon_confirm_flow_progression(self) -> None:
        # Inspired by 2026-06-29 LegalOn run: input → click → confirm → done.
        cfg = wz.WizardConfig()
        st = wz.WizardState()
        # Observe the input page.
        wz.bump_after_observation(st, observation_state="input", fingerprint="A")
        phase = wz.next_phase(
            flow="confirm", observation_state="input",
            confirm_seen=False, has_visible_textarea=True,
        )
        self.assertEqual(phase, wz.PHASE_FIRST)
        wz.record_click(st, "入力内容の確認")
        self.assertIsNone(wz.compute_stuck_reason(st, cfg))

        # Observe the confirm page.
        wz.bump_after_observation(st, observation_state="confirm", fingerprint="B")
        st.confirm_seen = True
        wz.reset_progress_counters(st)
        phase = wz.next_phase(
            flow="confirm", observation_state="confirm",
            confirm_seen=True, has_visible_textarea=False,
        )
        self.assertEqual(phase, wz.PHASE_FINAL)
        wz.record_click(st, "送信")
        self.assertIsNone(wz.compute_stuck_reason(st, cfg))

        # Observe the done page.
        wz.bump_after_observation(st, observation_state="done", fingerprint="C")
        self.assertIsNone(wz.compute_stuck_reason(st, cfg))


if __name__ == "__main__":
    unittest.main()
