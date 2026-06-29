"""v30 §WS-F — verify-model routing and structured Slack action hints.

Two thin, low-risk changes:

  * ``run._verify_model(config)`` resolves the LLM model for the verify-stage
    tiebreak independently from the form analyzer model. Previously these
    were conflated, so a brief that escalated form analysis to Opus also
    silently inflated every verify call's LLM bill.
  * ``resolve_queue.build_actionable_payload(entry, auto_resolver=)`` returns
    a ``{text, actions}`` dict alongside the legacy text-only message. The
    actions list is the contract the OpenClaw Slack bot will consume once it
    is updated to render Block-Kit buttons; tests pin the structure today so
    the bot side has a stable target to integrate against.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "jp-form-outreach"))

from _outreach_core import resolve_queue as rq  # noqa: E402
import run  # noqa: E402


class TestVerifyModelRouting(unittest.TestCase):
    def test_default_is_sonnet(self) -> None:
        from _outreach_core import infer as core_infer

        self.assertEqual(run._verify_model({}), core_infer.DEFAULT_MODEL)
        self.assertEqual(run._verify_model(None), core_infer.DEFAULT_MODEL)

    def test_verify_name_wins_over_form_analyzer_name(self) -> None:
        # A brief that escalates form analysis to Opus must NOT drag verify
        # along — verify_name is the dedicated knob.
        config = {
            "model": {
                "form_analyzer_name": "claude-cli/claude-opus-4-7",
                "verify_name": "claude-cli/claude-sonnet-4-6",
            },
        }
        self.assertEqual(run._verify_model(config), "claude-cli/claude-sonnet-4-6")

    def test_form_analyzer_name_used_when_no_verify_name(self) -> None:
        # Backward compatibility: existing briefs without a verify_name
        # behave exactly as before — verify uses the form analyzer's model.
        config = {"model": {"form_analyzer_name": "claude-cli/claude-haiku-4-5"}}
        self.assertEqual(run._verify_model(config), "claude-cli/claude-haiku-4-5")

    def test_top_level_name_as_fallback(self) -> None:
        config = {"model": {"name": "claude-cli/claude-haiku-4-5"}}
        self.assertEqual(run._verify_model(config), "claude-cli/claude-haiku-4-5")

    def test_independent_from_form_analyzer_escalation(self) -> None:
        # form_analyzer escalates to Opus, verify stays on its own knob.
        config = {
            "model": {
                "form_analyzer_name": "claude-cli/claude-sonnet-4-6",
                "form_analyzer_escalation_name": "claude-cli/claude-opus-4-7",
                "verify_name": "claude-cli/claude-haiku-4-5",
            },
        }
        self.assertEqual(run._verify_model(config), "claude-cli/claude-haiku-4-5")
        # And form_analyzer_escalation_model still returns Opus —
        # the two routes are decoupled.
        self.assertEqual(
            run._form_analyzer_escalation_model(config),
            "claude-cli/claude-opus-4-7",
        )


class TestActionableSlackPayload(unittest.TestCase):
    def _entry(self, **overrides) -> dict:
        base = {
            "target_id": "fujisoft",
            "name": "富士ソフト株式会社",
            "channel": "jp_form",
            "reason_class": "validation_unrecoverable",
            "reason": "想定外の必須項目...",
            "form_url": "https://sem-inq.fsi.co.jp/public/application/add/68",
            "diagnostics": {
                "url": "https://sem-inq.fsi.co.jp/public/application/add/68",
                "buttons": ["次へ"],
                "snapshot_path": "",
            },
        }
        base.update(overrides)
        return base

    def test_payload_contains_text_and_actions(self) -> None:
        payload = rq.build_actionable_payload(self._entry(), auto_resolver=True)
        self.assertIn("text", payload)
        self.assertIn("actions", payload)
        self.assertIsInstance(payload["text"], str)
        self.assertIsInstance(payload["actions"], list)
        self.assertTrue(payload["text"].startswith("⚠️"))

    def test_default_actions_url_skip(self) -> None:
        actions = rq.build_actionable_payload(self._entry(), auto_resolver=True)["actions"]
        labels = [a["label"] for a in actions]
        self.assertIn("URL を開く", labels)
        self.assertIn("スキップ", labels)
        url_action = next(a for a in actions if a["label"] == "URL を開く")
        self.assertIn("url", url_action)
        self.assertTrue(url_action["url"].startswith("https://"))
        skip_action = next(a for a in actions if a["label"] == "スキップ")
        self.assertEqual(skip_action.get("command"), "fujisoft skip")
        self.assertEqual(skip_action.get("style"), "danger")

    def test_resolvable_reasons_include_retry_action(self) -> None:
        # first_submit_not_found is in RESOLVABLE_REASONS — a retry button is
        # offered because the resolver can plausibly fix it on a second pass.
        entry = self._entry(reason_class="first_submit_not_found")
        actions = rq.build_actionable_payload(entry, auto_resolver=True)["actions"]
        labels = [a["label"] for a in actions]
        self.assertIn("再試行", labels)
        retry = next(a for a in actions if a["label"] == "再試行")
        self.assertEqual(retry["command"], "doorman resolve fujisoft")

    def test_unresolvable_reason_omits_retry(self) -> None:
        # validation_unrecoverable is NOT in RESOLVABLE_REASONS — no retry
        # because the resolver cannot fix it on a re-run.
        actions = rq.build_actionable_payload(
            self._entry(reason_class="validation_unrecoverable"),
            auto_resolver=True,
        )["actions"]
        labels = [a["label"] for a in actions]
        self.assertNotIn("再試行", labels)

    def test_no_url_omits_url_action(self) -> None:
        # A target without a form_url shouldn't surface an empty URL button.
        entry = self._entry(form_url="", diagnostics={"url": "", "buttons": []})
        actions = rq.build_actionable_payload(entry, auto_resolver=True)["actions"]
        labels = [a["label"] for a in actions]
        self.assertNotIn("URL を開く", labels)
        # Skip is still present so the operator has at least one move.
        self.assertIn("スキップ", labels)

    def test_no_target_id_omits_skip_and_retry(self) -> None:
        entry = self._entry(target_id="")
        actions = rq.build_actionable_payload(entry, auto_resolver=True)["actions"]
        labels = [a["label"] for a in actions]
        self.assertNotIn("スキップ", labels)
        self.assertNotIn("再試行", labels)

    def test_text_is_identical_to_legacy_message(self) -> None:
        # The text field must equal the legacy build_actionable_message so
        # any consumer that grepped the slack_message string keeps working.
        entry = self._entry()
        payload = rq.build_actionable_payload(entry, auto_resolver=True)
        legacy = rq.build_actionable_message(entry, auto_resolver=True)
        self.assertEqual(payload["text"], legacy)


if __name__ == "__main__":
    unittest.main()
