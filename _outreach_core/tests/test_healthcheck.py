"""healthcheck (v4 §15-B)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import progress
from _outreach_core.active_run import acquire_lock, remove_lock
from _outreach_core.helpers import healthcheck as hc


class TestHealthcheck(unittest.TestCase):
    def test_write_heartbeat_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(hc, "SKILLS_ROOT", root):
                path = hc.write_heartbeat(root)
            self.assertTrue(path.is_file())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["doorman_version"], "v4")
            self.assertIn("ts", data)

    def test_write_heartbeat_never_raises_on_failure(self) -> None:
        with mock.patch.object(hc, "health_path", side_effect=OSError("disk full")):
            path = hc.write_heartbeat()
            self.assertIsInstance(path, Path)

    def test_collect_active_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "jp-form-outreach" / "data" / "briefs" / "test-brief"
            data.mkdir(parents=True)
            acquire_lock(
                data,
                run_id="r1",
                stage="send",
                total_targets=10,
                skill="jp-form-outreach",
                brief_id="test-brief",
            )
            with mock.patch.object(hc, "SKILLS_ROOT", root):
                runs = hc.collect_active_runs(root)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["brief_id"], "test-brief")
            self.assertIsNotNone(runs[0]["activity_age_sec"])
            remove_lock(data)

    def test_run_activity_prefers_fresh_per_brief_progress(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            task = progress.current_task_path(data)
            task.write_text('{}\n', encoding="utf-8")
            now = time.time()
            os.utime(task, (now - 12, now - 12))
            self.assertEqual(
                hc.run_activity_age_seconds(data, now_epoch=now),
                12,
            )

    def test_format_ping_line(self) -> None:
        line = hc.format_ping_line(
            {
                "ts": hc._utc_now(),
                "active_runs": [],
                "open_needs_attention_count": 0,
            }
        )
        self.assertIn("稼働中", line)
        self.assertIn("heartbeat", line)
        self.assertIn("実行中", line)
        self.assertIn("要対応", line)

    def test_heartbeat_session_syncs_health(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "jp-form-outreach"
            data = skill / "data" / "briefs" / "b1"
            data.mkdir(parents=True)
            with mock.patch.object(hc, "SKILLS_ROOT", root), mock.patch.object(
                progress, "load_merged_config", side_effect=FileNotFoundError
            ), mock.patch.object(progress, "load_runtime_config", return_value={}):
                hb = progress.HeartbeatSession(
                    skill,
                    "draft",
                    3,
                    heartbeat=None,
                    data_dir=data,
                )
                hb.start("test")
                hb.tick(1, "one")
                hb.end("done")
            health_path = root / "data" / "system_health" / f"{hc.hostname()}.json"
            self.assertTrue(health_path.is_file())


class TestWatchdogErrorLine(unittest.TestCase):
    """v32 FX5 — a non-empty watchdog.err must be visible (it once hid a
    month-long watchdog outage)."""

    def test_missing_file_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(hc.watchdog_error_line(Path(tmp)))

    def test_empty_file_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "watchdog.err").write_text("")
            self.assertIsNone(hc.watchdog_error_line(root))

    def test_nonempty_file_surfaces_last_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "watchdog.err").write_text(
                "Traceback (most recent call last):\n"
                "TabError: inconsistent use of tabs and spaces in indentation\n"
            )
            line = hc.watchdog_error_line(root)
            self.assertIsNotNone(line)
            self.assertIn("watchdog.err", line)
            self.assertIn("TabError", line)


if __name__ == "__main__":
    unittest.main()
