"""Detached job runner (v4 §15 reliability)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.helpers import run_job


class TestRunJob(unittest.TestCase):
    def test_build_child_command_uses_run_py(self) -> None:
        cmd = run_job.build_child_command("jp-form-outreach", ["campaign", "--limit", "5"])
        self.assertTrue(cmd[1].endswith("run.py"))
        self.assertEqual(cmd[-3:], ["campaign", "--limit", "5"])

    def test_start_rejects_unknown_skill(self) -> None:
        with self.assertRaises(ValueError):
            run_job.start("nope-outreach", ["campaign"])

    def test_start_detaches_and_posts(self) -> None:
        posts: list[str] = []
        fake_proc = mock.Mock()
        fake_proc.pid = 4242
        with mock.patch.object(run_job, "_post", side_effect=lambda t, **k: posts.append(t)), mock.patch(
            "subprocess.Popen", return_value=fake_proc
        ) as popen, mock.patch.object(run_job, "_skill_dir") as skill_dir:
            d = Path(tempfile.mkdtemp())
            (d / "run.py").write_text("# stub\n")
            skill_dir.return_value = d
            info = run_job.start("jp-form-outreach", ["campaign", "--limit", "5"])
        self.assertEqual(info["pid"], 4242)
        self.assertIn("run_id", info)
        popen.assert_called_once()
        # detached: new session + no stdin
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs.get("start_new_session"))
        self.assertTrue(any("開始" in p for p in posts))

    def test_supervise_posts_success(self) -> None:
        posts: list[tuple[str, str]] = []
        result = mock.Mock()
        result.returncode = 0
        with mock.patch.object(
            run_job, "_post", side_effect=lambda t, level="info", **k: posts.append((level, t))
        ), mock.patch("subprocess.run", return_value=result):
            code = run_job._supervise("jp-form-outreach", "20260101-000000", "/tmp/x.log", ["draft"])
        self.assertEqual(code, 0)
        self.assertTrue(any("✅" in t for _lvl, t in posts))

    def test_supervise_posts_failure_on_nonzero_exit(self) -> None:
        posts: list[tuple[str, str]] = []
        result = mock.Mock()
        result.returncode = 3
        with mock.patch.object(
            run_job, "_post", side_effect=lambda t, level="info", **k: posts.append((level, t))
        ), mock.patch("subprocess.run", return_value=result):
            code = run_job._supervise("jp-form-outreach", "rid", "/tmp/x.log", ["send"])
        self.assertEqual(code, 3)
        self.assertTrue(any(lvl == "error" and "異常終了" in t for lvl, t in posts))

    def test_supervise_posts_failure_on_exception(self) -> None:
        posts: list[tuple[str, str]] = []
        with mock.patch.object(
            run_job, "_post", side_effect=lambda t, level="info", **k: posts.append((level, t))
        ), mock.patch("subprocess.run", side_effect=OSError("spawn failed")):
            code = run_job._supervise("jp-form-outreach", "rid", "/tmp/x.log", ["send"])
        self.assertEqual(code, 1)
        self.assertTrue(any(lvl == "error" for lvl, _t in posts))


if __name__ == "__main__":
    unittest.main()
