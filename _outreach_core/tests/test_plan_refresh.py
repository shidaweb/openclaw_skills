"""v30 next — re-analyze stale LLM form plan on first validation bounce.

Pilot run 2026-06-29 (sunstar / kagome_d2c) showed targets failing at
``submit_click_ineffective`` despite the v30 wizard / parser guards because
the cached ``_llm_plan`` had stale selectors (the page had changed since
enrich, or the original plan missed a required field). The validation_error
handler had a "live_rescue" pass for radios/selects but no way to call the
form analyzer again with the current DOM.

This module pins:

  * :func:`_validation_errors_suggest_plan_refresh` triggers on native
    validity reasons (valueMissing / patternMismatch / …) and NOT on plain
    text-extracted 「必須」 errors.
  * :func:`_refresh_llm_plan_and_refill` runs at most once per target (the
    ``_plan_refreshed`` flag), escalates to Opus when the brief configures
    that escalation, and replaces ``target["_llm_plan"]`` on success.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "jp-form-outreach"))

import run  # noqa: E402


class TestValidationErrorsSuggestPlanRefresh(unittest.TestCase):
    def test_native_value_missing_triggers_refresh(self) -> None:
        errs = [{"field": "お問い合わせ内容", "kind": "valueMissing"}]
        self.assertTrue(run._validation_errors_suggest_plan_refresh(errs))

    def test_pattern_mismatch_triggers_refresh(self) -> None:
        errs = [{"field": "tel", "kind": "patternMismatch"}]
        self.assertTrue(run._validation_errors_suggest_plan_refresh(errs))

    def test_compound_native_kind_triggers_refresh(self) -> None:
        # _native_validation_errors joins reasons with '+': "ariaInvalid+valueMissing".
        errs = [{"field": "x", "kind": "ariaInvalid+valueMissing"}]
        self.assertTrue(run._validation_errors_suggest_plan_refresh(errs))

    def test_plain_required_text_does_not_trigger(self) -> None:
        # Pre-v30 regex output: {"field": "お名前", "kind": "required"}.
        # These can be false-positives or already-mapped fields — no refresh.
        errs = [{"field": "お名前", "kind": "required"}]
        self.assertFalse(run._validation_errors_suggest_plan_refresh(errs))

    def test_zenkaku_format_does_not_trigger(self) -> None:
        errs = [{"field": "住所", "kind": "zenkaku"}]
        self.assertFalse(run._validation_errors_suggest_plan_refresh(errs))

    def test_empty_or_none_safe(self) -> None:
        self.assertFalse(run._validation_errors_suggest_plan_refresh(None))
        self.assertFalse(run._validation_errors_suggest_plan_refresh([]))


class TestRefreshLlmPlanAndRefill(unittest.TestCase):
    def _config(self) -> dict:
        return {"model": {"name": "claude-cli/claude-sonnet-4-6",
                          "form_analyzer_name": "claude-cli/claude-sonnet-4-6",
                          "form_analyzer_escalation_name": "claude-cli/claude-opus-4-7"}}

    def test_already_refreshed_short_circuits(self) -> None:
        # The function MUST be idempotent — a target that already got a
        # refresh in this run is silently skipped so a pathological form
        # cannot burn one Opus call per validation round.
        target = {"id": "x", "name": "X", "_plan_refreshed": True}
        with mock.patch.object(run, "_llm_analyze_form") as analyze, \
                mock.patch.object(run, "_evaluate"), \
                mock.patch.object(run, "fill_form_with_plan"):
            res = run._refresh_llm_plan_and_refill(
                target, self._config(), "body",
                trigger_reason="t", trace_dir=None,
            )
        self.assertEqual(res, {"refreshed": False, "reason": "already_refreshed_once"})
        analyze.assert_not_called()

    def test_first_call_marks_flag_and_invokes_analyzer(self) -> None:
        target = {"id": "x", "name": "X"}
        new_plan = {"fields": [{"name": "body", "value": "...", "action": "set_text"}]}
        with mock.patch.object(run, "_evaluate", return_value={"inputs": [], "textareas": [{}], "form_root_selector": "form"}), \
                mock.patch.object(run, "_llm_analyze_form", return_value=new_plan) as analyze, \
                mock.patch.object(run, "fill_form_with_plan", return_value={"filled": ["body=..."], "errors": []}), \
                mock.patch.object(run, "_emit_event"):
            res = run._refresh_llm_plan_and_refill(
                target, self._config(), "body",
                trigger_reason="validation_round1:valueMissing",
                trace_dir=None,
            )
        self.assertTrue(res["refreshed"])
        self.assertEqual(res["filled"], 1)
        self.assertTrue(target["_plan_refreshed"])
        # The cached plan was replaced.
        self.assertEqual(target["_llm_plan"], new_plan)
        # force_refresh=True so the analyzer ignores any cached plan.
        kwargs = analyze.call_args.kwargs
        self.assertTrue(kwargs.get("force_refresh"))
        self.assertIn("plan_refresh", kwargs.get("escalation_reason") or "")

    def test_analyzer_returns_no_plan_still_marks_flag(self) -> None:
        # When the analyzer fails (network, bad output), we still consume the
        # one refresh slot so a flaky LLM doesn't get retried per validation
        # round. The send loop falls through to its existing recovery paths.
        target = {"id": "x", "name": "X"}
        with mock.patch.object(run, "_evaluate", return_value={"inputs": []}), \
                mock.patch.object(run, "_llm_analyze_form", return_value=None), \
                mock.patch.object(run, "_emit_event"):
            res = run._refresh_llm_plan_and_refill(
                target, self._config(), "body",
                trigger_reason="t", trace_dir=None,
            )
        self.assertTrue(res["refreshed"])
        self.assertEqual(res["fields"], 0)
        self.assertTrue(target["_plan_refreshed"])

    def test_escalation_model_used_when_configured(self) -> None:
        # The refresh is the right moment to spend Opus — we already know the
        # cheaper plan was wrong. The config's escalation model is passed
        # through to _llm_analyze_form via _config_with_form_analyzer_model.
        target = {"id": "x", "name": "X"}
        captured_configs: list[dict] = []

        def _spy_analyze(t, cfg, **kw):
            captured_configs.append(cfg)
            return {"fields": []}

        with mock.patch.object(run, "_evaluate", return_value={"inputs": []}), \
                mock.patch.object(run, "_llm_analyze_form", side_effect=_spy_analyze), \
                mock.patch.object(run, "_emit_event"):
            run._refresh_llm_plan_and_refill(
                target, self._config(), "body",
                trigger_reason="t", trace_dir=None,
            )
        self.assertTrue(captured_configs)
        # The forwarded config carries the escalated model on .model.form_analyzer_name.
        escalated_model = (
            (captured_configs[0].get("model") or {}).get("form_analyzer_name")
        )
        self.assertEqual(escalated_model, "claude-cli/claude-opus-4-7")


if __name__ == "__main__":
    unittest.main()
