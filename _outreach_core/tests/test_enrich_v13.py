from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_run_module():
    if "yaml" not in sys.modules:
        yaml_stub = types.SimpleNamespace(
            safe_dump=lambda obj, **kwargs: "{}\n",
        )
        sys.modules["yaml"] = yaml_stub
    root = Path(__file__).resolve().parent.parent.parent
    run_path = root / "jp-form-outreach" / "run.py"
    spec = importlib.util.spec_from_file_location("jp_form_outreach_run_v13", run_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class TestEnrichV13(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.run_mod = _load_run_module()

    def _write_targets(self, rows: list[dict]) -> tuple[Path, Path]:
        tmp = Path(tempfile.mkdtemp())
        inp = tmp / "targets.jsonl"
        out = tmp / "enriched.jsonl"
        with inp.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return inp, out

    def test_seed_contact_does_not_probe_candidates(self) -> None:
        inp, out = self._write_targets(
            [{"id": 1, "name": "A", "form_url": "https://example.co.jp/inquiry/"}]
        )
        fields = {
            "inputs": [{"name": "email", "label": "メール"}],
            "textareas": [{"name": "body", "label": "お問い合わせ内容"}],
            "submit_buttons": [{"text": "送信", "disabled": False}],
        }

        def fake_eval(js: str):
            if js == self.run_mod._FORM_FIELDS_JS:
                return fields
            if js == self.run_mod._PAGE_LINKS_JS:
                return []
            return {}

        with (
            patch.object(self.run_mod, "_evaluate", side_effect=fake_eval),
            patch.object(self.run_mod, "oc_browser", side_effect=lambda *a, **k: "ok"),
            patch.object(self.run_mod, "_classify_form_type", return_value=("contact", None)),
            patch.object(self.run_mod, "_build_contact_candidates") as bcc,
            patch.object(self.run_mod, "_emit_event"),
            patch.object(self.run_mod.time, "sleep", return_value=None),
            patch("_outreach_core.cookie_dismiss.apply_cookie_dismiss", return_value=None),
        ):
            self.run_mod.stage_enrich(inp, out, config={}, limit=1)
        self.assertFalse(bcc.called)

    def test_candidate_404_is_skipped_and_error_event_emitted(self) -> None:
        inp, out = self._write_targets(
            [{"id": 2, "name": "B", "form_url": "https://example.co.jp/inquiry/"}]
        )
        current = {"url": ""}
        snapshot_map = {
            "https://example.co.jp/inquiry/": "お問い合わせフォーム",
            "https://example.co.jp/contact": "404 ページが見つかりません",
            "https://example.co.jp/inquiry2": "お問い合わせ 送信",
        }
        fields = {
            "inputs": [{"name": "email", "label": "メール"}],
            "textareas": [{"name": "body", "label": "お問い合わせ内容"}],
            "submit_buttons": [{"text": "送信", "disabled": False}],
        }

        def fake_browser(cmd: str, *args):
            if cmd == "open":
                current["url"] = str(args[0]) if args else ""
                return ""
            if cmd == "snapshot":
                return snapshot_map.get(current["url"], "")
            return ""

        def fake_eval(js: str):
            if js == self.run_mod._FORM_FIELDS_JS:
                return fields
            if js == self.run_mod._PAGE_LINKS_JS:
                return []
            return {}

        emitted: list[str] = []

        def capture(kind: str, **_kwargs):
            emitted.append(kind)

        with (
            patch.object(self.run_mod, "oc_browser", side_effect=fake_browser),
            patch.object(self.run_mod, "_evaluate", side_effect=fake_eval),
            patch.object(self.run_mod, "_build_contact_candidates", return_value=["https://example.co.jp/contact", "https://example.co.jp/inquiry2"]),
            patch.object(self.run_mod, "_classify_form_type", side_effect=[("unknown_no_textarea", "x"), ("contact", None)]),
            patch.object(self.run_mod, "_emit_event", side_effect=capture),
            patch.object(self.run_mod.time, "sleep", return_value=None),
            patch("_outreach_core.cookie_dismiss.apply_cookie_dismiss", return_value=None),
        ):
            self.run_mod.stage_enrich(inp, out, config={}, limit=1)
        self.assertIn("enrich.nav.error_page", emitted)
        lines = out.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        self.assertEqual(row.get("form_url"), "https://example.co.jp/inquiry2")


if __name__ == "__main__":
    unittest.main()

