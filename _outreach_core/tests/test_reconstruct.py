"""Status reconstruction helper (§14-G)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _outreach_core.helpers.reconstruct import build_status_report


class TestReconstruct(unittest.TestCase):
    def test_includes_lock_and_needs_attention(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            briefs = root / "briefs"
            briefs.mkdir()
            (briefs / "b1.yaml").write_text("brief:\n  id: b1\n")
            skill = root / "jp-form-outreach"
            data = skill / "data" / "briefs" / "b1"
            data.mkdir(parents=True)
            lock = {
                "run_id": "r1",
                "pid": 999999999,
                "stage": "send",
                "total_targets": 3,
                "current_target_idx": 1,
            }
            (data / "active_run.lock").write_text(json.dumps(lock))
            (data / "needs_attention.jsonl").write_text(
                json.dumps(
                    {"status": "open", "target_id": "co", "name": "Co", "reason": "reCAPTCHA"}
                )
                + "\n"
            )
            with mock.patch("_outreach_core.helpers.reconstruct.SKILLS_ROOT", root), mock.patch(
                "_outreach_core.config.BRIEFS_DIR", briefs
            ):
                text = build_status_report("b1", skill="jp-form-outreach")
            self.assertIn("active_run.lock", text)
            self.assertIn("needs_attention", text)
            self.assertIn("co", text)


if __name__ == "__main__":
    unittest.main()
