"""Confirmed sends are mirrored to the brief target YAML."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

import sys

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "jp-form-outreach"))

import run  # noqa: E402


class TestTargetStatusSync(unittest.TestCase):
    def test_sync_is_scoped_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "brief.yaml"
            path.write_text(
                "companies:\n"
                "  - id: sent_one\n"
                "    name: 送信社\n"
                "    status: pending\n"
                "  - id: untouched\n"
                "    name: 未送信社\n"
                "    status: pending\n",
                encoding="utf-8",
            )
            changed = run._sync_targets_sent_status(
                [{"id": "sent_one"}],
                targets_path=path,
                sent_at="2026-06-19T00:00:00Z",
            )
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            by_id = {row["id"]: row for row in data["companies"]}
            self.assertEqual(changed, 1)
            self.assertEqual(by_id["sent_one"]["status"], "sent")
            self.assertEqual(
                by_id["sent_one"]["sent_at"], "2026-06-19T00:00:00Z"
            )
            self.assertEqual(by_id["untouched"]["status"], "pending")
            self.assertEqual(list(path.parent.glob("*.tmp.*")), [])

    def test_sync_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "brief.yaml"
            path.write_text(
                "companies:\n"
                "  - id: done\n"
                "    status: sent\n"
                "    sent_at: '2026-06-18T00:00:00Z'\n",
                encoding="utf-8",
            )
            self.assertEqual(
                run._sync_targets_sent_status(
                    [{"id": "done"}], targets_path=path
                ),
                0,
            )


if __name__ == "__main__":
    unittest.main()
