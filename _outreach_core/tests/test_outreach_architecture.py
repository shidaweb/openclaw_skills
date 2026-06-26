from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _outreach_core import campaign, channel_state, config, persona, prompt, routing
from _outreach_core.helpers import reconstruct


class TestPersonaCampaignSeparation(unittest.TestCase):
    def test_persona_overrides_legacy_inline_sender(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            briefs = root / "briefs"
            personas = root / "personas"
            skill = root / "skill"
            briefs.mkdir()
            personas.mkdir()
            skill.mkdir()
            (briefs / "launch.yaml").write_text(
                "brief:\n  id: launch\n  persona_id: alice\n"
                "sender:\n  name: Legacy\nproduct:\n  name: Product\n",
                encoding="utf-8",
            )
            (personas / "alice.yaml").write_text(
                "persona:\n  id: alice\nsender:\n  name: Alice\n",
                encoding="utf-8",
            )
            (skill / "config.yaml").write_text("model:\n  max_chars: 400\n", encoding="utf-8")
            with mock.patch.object(config, "BRIEFS_DIR", briefs), mock.patch.object(
                persona, "PERSONAS_DIR", personas
            ):
                merged = config.load_merged_config(skill, "launch", channel="linkedin")
            self.assertEqual(merged["sender"]["name"], "Alice")
            self.assertEqual(merged["product"]["name"], "Product")
            self.assertEqual(merged["_persona_id"], "alice")
            self.assertEqual(merged["_channel"], "linkedin")

    def test_prompt_override_is_selected_by_channel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "jp.md").write_text("JP PERSONA", encoding="utf-8")
            (root / "li.md").write_text("LINKEDIN PERSONA", encoding="utf-8")
            cfg = {
                "_channel": "linkedin",
                "prompts_overrides": {
                    "jp_form_system_persona": "prompts/jp.md",
                    "linkedin_system_persona": "prompts/li.md",
                },
            }
            block = prompt.build_system_block(cfg, root)
            self.assertIn("LINKEDIN PERSONA", block)
            self.assertNotIn("JP PERSONA", block)


class TestThreadScopedRouting(unittest.TestCase):
    def test_two_threads_in_one_channel_keep_independent_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            briefs = root / "briefs"
            personas = root / "personas"
            threads = root / "data" / "thread_state"
            briefs.mkdir(parents=True)
            personas.mkdir()
            for bid in ("a", "b"):
                (briefs / f"{bid}.yaml").write_text(f"brief:\n  id: {bid}\n", encoding="utf-8")
            for pid in ("pa", "pb"):
                (personas / f"{pid}.yaml").write_text(f"persona:\n  id: {pid}\n", encoding="utf-8")
            with mock.patch.object(channel_state, "THREAD_STATE_DIR", threads), mock.patch.object(
                channel_state, "BRIEFS_DIR", briefs
            ), mock.patch.object(config, "BRIEFS_DIR", briefs), mock.patch.object(
                persona, "PERSONAS_DIR", personas
            ), mock.patch.dict("os.environ", {}, clear=True):
                channel_state.bind_thread(
                    "CTEST", "1.1", brief_id="a", persona_id="pa", channel="linkedin"
                )
                channel_state.bind_thread(
                    "CTEST", "2.2", brief_id="b", persona_id="pb", channel="jp_form"
                )
                one = channel_state.load_thread_state("CTEST", "1.1")
                two = channel_state.load_thread_state("CTEST", "2.2")
                route_one = routing.resolve_route(
                    slack_channel_id="CTEST", slack_thread_ts="1.1"
                )
                route_two = routing.resolve_route(
                    slack_channel_id="CTEST", slack_thread_ts="2.2"
                )
            self.assertEqual((one or {})["persona_id"], "pa")
            self.assertEqual((one or {})["channel"], "linkedin")
            self.assertEqual((two or {})["brief_id"], "b")
            self.assertEqual((two or {})["channel"], "jp_form")
            self.assertEqual((route_one.brief_id, route_one.persona_id, route_one.skill),
                             ("a", "pa", "linkedin-outreach"))
            self.assertEqual((route_two.brief_id, route_two.persona_id, route_two.skill),
                             ("b", "pb", "jp-form-outreach"))

    def test_status_shows_thread_campaign_persona_and_channel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            briefs = root / "briefs"
            personas = root / "personas"
            threads = root / "data" / "thread_state"
            briefs.mkdir(parents=True)
            personas.mkdir()
            (briefs / "c.yaml").write_text("brief:\n  id: c\n", encoding="utf-8")
            (personas / "p.yaml").write_text("persona:\n  id: p\n", encoding="utf-8")
            with mock.patch.object(channel_state, "THREAD_STATE_DIR", threads), mock.patch.object(
                channel_state, "BRIEFS_DIR", briefs
            ), mock.patch.object(config, "BRIEFS_DIR", briefs), mock.patch.object(
                persona, "PERSONAS_DIR", personas
            ), mock.patch.object(reconstruct, "SKILLS_ROOT", root):
                channel_state.bind_thread(
                    "CTEST", "3.3", brief_id="c", persona_id="p", channel="linkedin"
                )
                text = reconstruct.build_status_report(
                    channel_id="CTEST", thread_ts="3.3"
                )
            self.assertIn("campaign: c", text)
            self.assertIn("persona: p", text)
            self.assertIn("outreach_channel: linkedin", text)

    def test_explicit_channel_routes_to_skill(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            briefs = root / "briefs"
            personas = root / "personas"
            briefs.mkdir()
            personas.mkdir()
            (briefs / "c.yaml").write_text(
                "brief:\n  id: c\n  persona_id: p\ndesired_channels: [jp_form, linkedin]\n",
                encoding="utf-8",
            )
            (personas / "p.yaml").write_text("persona:\n  id: p\n", encoding="utf-8")
            with mock.patch.object(config, "BRIEFS_DIR", briefs), mock.patch.object(
                persona, "PERSONAS_DIR", personas
            ), mock.patch.dict("os.environ", {}, clear=True):
                route = routing.resolve_route(brief_id="c", channel="linkedin")
            self.assertEqual(route.skill, "linkedin-outreach")
            self.assertEqual(route.persona_id, "p")


class TestCampaignRunner(unittest.TestCase):
    def test_runs_canonical_phase_order_and_writes_summary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            calls: list[str] = []

            def phase(name: str):
                def run(_ctx):
                    calls.append(name)
                    return campaign.PhaseResult(name, total=1, ready=1, sent=1 if name == "send" else 0)

                return run

            adapter = campaign.FunctionChannelAdapter(
                "linkedin", phase("list"), phase("enrich"), phase("draft"), phase("send")
            )
            ctx = campaign.CampaignContext(
                brief_id="c",
                persona_id="p",
                channel="linkedin",
                skill="linkedin-outreach",
                data_dir=data,
            )
            result = campaign.CampaignRunner(ctx).run(adapter)
            self.assertEqual(calls, ["list", "enrich", "draft", "send"])
            self.assertEqual(result.status, "complete")
            self.assertTrue(result.reconciled)
            summary = json.loads((data / "campaign_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["context"]["persona_id"], "p")
            self.assertEqual(summary["phases"][-1]["phase"], "send")
            self.assertTrue(summary["reconciled"])

    def test_persona_switch_requires_explicit_workspace_reset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)

            def phase(name: str):
                return lambda _ctx: campaign.PhaseResult(name)

            adapter = campaign.FunctionChannelAdapter(
                "linkedin", phase("list"), phase("enrich"), phase("draft"), phase("send")
            )
            first = campaign.CampaignContext("c", "p1", "linkedin", "linkedin-outreach", data)
            second = campaign.CampaignContext("c", "p2", "linkedin", "linkedin-outreach", data)
            campaign.CampaignRunner(first).run(adapter, stop_after="draft")
            with self.assertRaisesRegex(RuntimeError, "--clean"):
                campaign.CampaignRunner(second).run(adapter, stop_after="draft")
            result = campaign.CampaignRunner(second).run(
                adapter, stop_after="draft", replace_context=True
            )
            self.assertEqual(result.context.persona_id, "p2")
