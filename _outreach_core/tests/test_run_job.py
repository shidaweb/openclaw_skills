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

    # --- supervisor: stall detection + bounded auto-restart (v6 §15-B) ---

    @staticmethod
    def _proc_returning(code):
        """Fake Popen result whose wait() returns ``code`` immediately."""
        p = mock.Mock()
        p.wait.return_value = code
        p.returncode = code
        return p

    def _patches(self, posts):
        import _outreach_core.run_supervisor as RS
        return (
            mock.patch.object(run_job, "_post",
                              side_effect=lambda t, level="info", **k: posts.append((level, t))),
            mock.patch.object(run_job, "_health_files", return_value=[]),
            mock.patch.object(RS, "save_state", lambda *a, **k: None),
            mock.patch.object(RS, "load_state", lambda *a, **k: RS.new_state()),
        )

    def test_supervise_posts_success(self) -> None:
        posts: list[tuple[str, str]] = []
        p1, p2, p3, p4 = self._patches(posts)
        with p1, p2, p3, p4, mock.patch("subprocess.Popen",
                                        return_value=self._proc_returning(0)):
            code = run_job._supervise("jp-form-outreach", "rid", "/tmp/x.log", ["draft"])
        self.assertEqual(code, 0)
        self.assertTrue(any("✅" in t for _lvl, t in posts))

    def test_supervise_exit3_means_other_run_no_restart(self) -> None:
        posts: list[tuple[str, str]] = []
        p1, p2, p3, p4 = self._patches(posts)
        with p1, p2, p3, p4, mock.patch("subprocess.Popen",
                                        return_value=self._proc_returning(3)) as popen:
            code = run_job._supervise("jp-form-outreach", "rid", "/tmp/x.log", ["send"])
        self.assertEqual(code, 3)
        self.assertEqual(popen.call_count, 1)  # never restarted
        self.assertTrue(any("別の run" in t for _lvl, t in posts))

    def test_supervise_auto_restarts_on_crash_then_gives_up(self) -> None:
        posts: list[tuple[str, str]] = []
        import _outreach_core.run_supervisor as RS
        p1, p2, p3, p4 = self._patches(posts)
        with p1, p2, p3, p4, mock.patch("subprocess.Popen",
                                        side_effect=lambda *a, **k: self._proc_returning(1)) as popen:
            code = run_job._supervise("jp-form-outreach", "rid", "/tmp/x.log", ["send"])
        self.assertEqual(code, 1)
        # initial launch + MAX_RESTARTS relaunches
        self.assertEqual(popen.call_count, RS.MAX_RESTARTS + 1)
        self.assertTrue(any("♻️" in t for _lvl, t in posts))   # restart notices
        self.assertTrue(any(lvl == "error" for lvl, _t in posts))  # final give-up

    def test_supervise_restarts_stalled_then_succeeds(self) -> None:
        import subprocess as _sp
        import _outreach_core.run_supervisor as RS
        posts: list[tuple[str, str]] = []

        stalled = mock.Mock()
        stalled.wait.side_effect = _sp.TimeoutExpired(cmd="x", timeout=1)
        stalled.terminate.return_value = None
        healthy = self._proc_returning(0)

        p1, p2, p3, p4 = self._patches(posts)
        with p1, p2, p3, p4, \
                mock.patch.object(RS, "latest_activity_age_sec", return_value=10 ** 6), \
                mock.patch("subprocess.Popen", side_effect=[stalled, healthy]) as popen:
            code = run_job._supervise("jp-form-outreach", "rid", "/tmp/x.log", ["campaign"])
        self.assertEqual(code, 0)
        self.assertEqual(popen.call_count, 2)            # stalled killed → relaunched
        stalled.terminate.assert_called()                # we killed the stalled one
        self.assertTrue(any("stall" in t for _lvl, t in posts))

    def test_supervise_posts_failure_on_spawn_exception(self) -> None:
        posts: list[tuple[str, str]] = []
        p1, p2, p3, p4 = self._patches(posts)
        with p1, p2, p3, p4, mock.patch("subprocess.Popen", side_effect=OSError("spawn failed")):
            code = run_job._supervise("jp-form-outreach", "rid", "/tmp/x.log", ["send"])
        self.assertEqual(code, 1)
        self.assertTrue(any(lvl == "error" for lvl, _t in posts))


if __name__ == "__main__":
    unittest.main()
