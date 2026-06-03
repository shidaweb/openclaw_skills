"""Helper CLIs: dump_exclude_set, append_targets."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys
import types

try:
    import yaml
except ImportError:  # pragma: no cover - local test env may not have pyyaml
    yaml = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.helpers.append_targets import append_jp_form
from _outreach_core.helpers.dump_exclude_set import dump_exclude_sets


def _ensure_yaml_module() -> None:
    if "yaml" in sys.modules:
        return
    sys.modules["yaml"] = types.SimpleNamespace(
        safe_load=lambda s: json.loads(s) if str(s).strip() else {},
        safe_dump=lambda obj, **kwargs: json.dumps(obj, ensure_ascii=False),
    )


class TestHelpers(unittest.TestCase):
    def test_dump_exclude_sets_shape(self) -> None:
        out = dump_exclude_sets()
        self.assertIn("linkedin", out)
        self.assertIn("jp_form", out)
        self.assertIn("canonical", out)
        self.assertIsInstance(out["linkedin"], list)

    def test_append_targets_dedup(self) -> None:
        _ensure_yaml_module()
        with tempfile.TemporaryDirectory() as tmp:
            ypath = Path(tmp) / "targets.yaml"
            ypath.write_text('{"companies":[]}', encoding="utf-8")
            items = [{"id": "test_co", "name": "テスト株式会社", "industry": "EdTech"}]
            n1 = append_jp_form(items, ypath)
            n2 = append_jp_form(items, ypath)
            self.assertEqual(n1, 1)
            self.assertEqual(n2, 0)

    def test_append_targets_dedup_loose_alias(self) -> None:
        _ensure_yaml_module()
        with tempfile.TemporaryDirectory() as tmp:
            ypath = Path(tmp) / "targets.yaml"
            ypath.write_text(
                '{"companies":[{"id":"geo_hd","name":"GEOホールディングス"}]}',
                encoding="utf-8",
            )
            items = [{"id": "geo_holdings", "name": "geo holdings", "industry": "Retail"}]
            n = append_jp_form(items, ypath)
            self.assertEqual(n, 0)

    def test_append_targets_exclude_ids(self) -> None:
        _ensure_yaml_module()
        with tempfile.TemporaryDirectory() as tmp:
            ypath = Path(tmp) / "targets.yaml"
            ypath.write_text('{"companies":[]}', encoding="utf-8")
            items = [{"id": "nichii_gakkan", "name": "ニチイ学館", "industry": "HR"}]
            n = append_jp_form(items, ypath, exclude_ids={"nichii_gakkan"})
            self.assertEqual(n, 0)

    def test_append_targets_keeps_contact_url_candidates(self) -> None:
        _ensure_yaml_module()
        with tempfile.TemporaryDirectory() as tmp:
            ypath = Path(tmp) / "targets.yaml"
            ypath.write_text('{"companies":[]}', encoding="utf-8")
            items = [
                {
                    "id": "cand_co",
                    "name": "候補株式会社",
                    "contact_url_candidates": [
                        "https://example.co.jp/recruit",
                        "https://example.co.jp/contact",
                    ],
                }
            ]
            n = append_jp_form(items, ypath)
            self.assertEqual(n, 1)
            text = ypath.read_text(encoding="utf-8")
            if yaml is None:
                self.assertIn("contact_url_candidates", text)
                self.assertIn("https://example.co.jp/recruit", text)
                self.assertIn("https://example.co.jp/contact", text)
            else:
                data = yaml.safe_load(text)
                row = (data.get("companies") or [])[0]
                self.assertEqual(
                    row.get("contact_url_candidates"),
                    ["https://example.co.jp/recruit", "https://example.co.jp/contact"],
                )


if __name__ == "__main__":
    unittest.main()
