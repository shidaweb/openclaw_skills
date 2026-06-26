"""linkedin-outreach send stage invariants (v4 §11-A-6)."""

from __future__ import annotations

import re
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def _load_linkedin_run():
    root = Path(__file__).resolve().parent.parent.parent
    path = root / "linkedin-outreach" / "run.py"
    spec = importlib.util.spec_from_file_location("linkedin_outreach_run_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class TestLinkedinSend(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.run_mod = _load_linkedin_run()

    def test_stage_send_has_no_input_calls(self) -> None:
        run_py = Path(__file__).resolve().parent.parent.parent / "linkedin-outreach" / "run.py"
        text = run_py.read_text(encoding="utf-8")
        m = re.search(r"^def stage_send\(", text, re.M)
        self.assertIsNotNone(m)
        start = m.start()
        rest = text[start + 1 :]
        m2 = re.search(r"^def [a-z_]+\(", rest, re.M)
        end = start + 1 + m2.start() if m2 else len(text)
        block = text[start:end]
        self.assertNotIn("input(", block, "stage_send must not call input()")

    def test_sequence_cr_selects_connection_request(self) -> None:
        cfg = {"sequence": {"steps": [{"id": "cr", "max_chars": 300}]}}
        self.assertEqual(
            self.run_mod.resolve_touchpoint(cfg),
            self.run_mod.TOUCHPOINT_CONNECTION,
        )
        self.assertEqual(
            self.run_mod.touchpoint_char_limit(cfg, self.run_mod.TOUCHPOINT_CONNECTION),
            300,
        )

    def test_connection_prompt_never_assumes_acceptance(self) -> None:
        block = self.run_mod.build_user_block(
            {"id": "x", "name": "JR", "_enrich_status": "ready"},
            300,
            touchpoint=self.run_mod.TOUCHPOINT_CONNECTION,
        )
        self.assertIn("has NOT connected yet", block)
        self.assertIn("do not say 'thanks for connecting'", block)

    def test_empty_snapshot_is_not_enriched(self) -> None:
        self.assertEqual(self.run_mod.snapshot_problem(""), "empty_snapshot")
        self.assertEqual(self.run_mod.snapshot_problem("   "), "empty_snapshot")

    def test_japanese_ui_public_profile_yields_draftable_signals(self) -> None:
        snapshot = '''
- generic [ref=e1]:
  - main [ref=e2]:
    - heading "Steve Tate" [level=2] [ref=e3]
    - paragraph [ref=e4]: · 3次
    - paragraph [ref=e5]: Fractional CMO / Growth Marketing Executive / Advisor
    - paragraph [ref=e6]: ロサンゼルス都市エリア
    - heading "自己紹介" [level=2] [ref=e7]
    - paragraph [ref=e8]:
      - text: I build brands and advise founders on practical growth systems.
      - text: Book me for office hours through my independent practice.
    - heading "アクティビティ" [level=2] [ref=e9]
    - paragraph [ref=e10]:
      - text: Recently I wrote about diagnosing growth bottlenecks before scaling ads.
    - heading "職歴" [level=2] [ref=e11]
      - heading "Fractional CMO" [level=3] [ref=e12]
'''
        parsed = self.run_mod.parse_profile(snapshot)
        self.assertEqual(
            parsed["headline"],
            "Fractional CMO / Growth Marketing Executive / Advisor",
        )
        self.assertIn("advise founders", parsed["about"])
        self.assertTrue(parsed["recent_activity"])
        self.assertGreaterEqual(self.run_mod.enrichment_signal_count(parsed), 3)
        self.assertEqual(parsed["_profile_parser"], "snapshot_multilingual_fallback")

    def test_visible_browser_setting_is_explicitly_started(self) -> None:
        with mock.patch.object(
            self.run_mod.core_infer, "browser_headless_preference", return_value=False
        ), mock.patch.object(
            self.run_mod.core_infer, "oc_browser_start", return_value=True
        ) as start:
            mode = self.run_mod.ensure_campaign_browser()
        self.assertFalse(mode)
        start.assert_called_once_with(headless=False)

    def test_no_recent_posts_is_not_counted_as_activity(self) -> None:
        snapshot = '''
- main [ref=e1]:
  - heading "Sally Kenny" [level=2] [ref=e2]
  - paragraph [ref=e3]: Chief Marketing Officer | Fractional CMO
  - heading "自己紹介" [level=2] [ref=e4]
    - text: Fractional marketing leadership for specialist professional firms.
  - heading "アクティビティ" [level=2] [ref=e5]
    - text: Sally has no recent posts
    - text: Recent posts Sally shares will be displayed here.
'''
        parsed = self.run_mod.parse_profile(snapshot)
        self.assertEqual(parsed["recent_activity"], [])
        self.assertEqual(self.run_mod.enrichment_signal_count(parsed), 2)

    def test_public_csv_url_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            csv_path = tmp / "targets.csv"
            out = tmp / "leads.jsonl"
            csv_path.write_text(
                "linkedin_url,name,company,note\n"
                "https://www.linkedin.com/in/jrhowardcfo/,JR Howard,JR Howard CFO,30 startups\n",
                encoding="utf-8",
            )
            old_data = self.run_mod.DATA_DIR
            try:
                self.run_mod.DATA_DIR = tmp
                self.run_mod.SKIP_HISTORY_PATH = tmp / "skip_history.jsonl"
                self.run_mod.SENT_HISTORY_PATH = tmp / "sent_history.jsonl"
                self.run_mod.stage_fetch_from_csv(csv_path, out)
            finally:
                self.run_mod.DATA_DIR = old_data
            row = json.loads(out.read_text(encoding="utf-8").strip())
            self.assertEqual(
                row["profile_url"],
                "https://www.linkedin.com/in/jrhowardcfo/",
            )
            self.assertNotIn("/sales/people/", row["profile_url"])

    def test_csv_campaign_limit_is_applied(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            csv_path = tmp / "targets.csv"
            out = tmp / "leads.jsonl"
            csv_path.write_text(
                "linkedin_url,name,company\n"
                "https://www.linkedin.com/in/one/,One,One Co\n"
                "https://www.linkedin.com/in/two/,Two,Two Co\n"
                "https://www.linkedin.com/in/three/,Three,Three Co\n",
                encoding="utf-8",
            )
            old_data = self.run_mod.DATA_DIR
            try:
                self.run_mod.DATA_DIR = tmp
                self.run_mod.SKIP_HISTORY_PATH = tmp / "skip_history.jsonl"
                self.run_mod.SENT_HISTORY_PATH = tmp / "sent_history.jsonl"
                self.run_mod.stage_fetch_from_csv(csv_path, out, limit=2)
            finally:
                self.run_mod.DATA_DIR = old_data
            rows = [json.loads(line) for line in out.read_text().splitlines()]
            self.assertEqual([row["name"] for row in rows], ["One", "Two"])


if __name__ == "__main__":
    unittest.main()
