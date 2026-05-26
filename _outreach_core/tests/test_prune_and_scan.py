"""prune + form_root_selector scan JS."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import events as ev
from _outreach_core.verify import build_scan_required_js


class TestPruneAndScan(unittest.TestCase):
    def test_build_scan_required_uses_saved_selector(self) -> None:
        js = build_scan_required_js({"form_root_selector": "#contact-form"})
        self.assertIn("#contact-form", js)
        self.assertIn("querySelector", js)

    def test_prune_removes_old_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            old_ts = (datetime.utcnow() - timedelta(days=100)).isoformat() + "Z"
            new_ts = datetime.utcnow().isoformat() + "Z"
            lines = [
                json.dumps(
                    {
                        "v": 1,
                        "ts": old_ts,
                        "run_id": "20250101-120000",
                        "kind": "draft.emitted",
                        "stage": "draft",
                    }
                ),
                json.dumps(
                    {
                        "v": 1,
                        "ts": new_ts,
                        "run_id": "20260526-120000",
                        "kind": "draft.emitted",
                        "stage": "draft",
                    }
                ),
            ]
            (data / "events.jsonl").write_text("\n".join(lines) + "\n")
            traces = data / "traces" / "20250101-120000" / "co1"
            traces.mkdir(parents=True)
            (traces / "x.json").write_text("{}")
            stats = ev.prune_data(data, keep_days=90, dry_run=False)
            self.assertEqual(stats["events_removed"], 1)
            self.assertEqual(stats["events_kept"], 1)
            self.assertGreaterEqual(stats["trace_dirs_removed"], 1)
            remaining = (data / "events.jsonl").read_text()
            self.assertIn("20260526", remaining)
            self.assertNotIn("20250101-120000", remaining)


if __name__ == "__main__":
    unittest.main()
