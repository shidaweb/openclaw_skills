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

    def test_build_campaign_args_injects_brief_and_limit(self) -> None:
        args = run_job.build_campaign_args(
            brief_id="torana-line-crm",
            remaining=7,
            per_run_limit=10,
            extra_args=None,
        )
        self.assertEqual(
            args,
            ["campaign", "--brief", "torana-line-crm", "--limit", "7"],
        )

    def test_build_campaign_args_respects_existing_limit(self) -> None:
        args = run_job.build_campaign_args(
            brief_id="torana-line-crm",
            remaining=50,
            per_run_limit=10,
            extra_args=["--limit", "3", "--skip-enrich"],
        )
        self.assertEqual(
            args,
            ["campaign", "--brief", "torana-line-crm", "--limit", "3", "--skip-enrich"],
        )

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
            # Neutralize the caffeinate sibling so it doesn't consume the mocked
            # subprocess.Popen side_effects used to model the child process.
            mock.patch.object(run_job, "_start_caffeinate", return_value=None),
        )

    def test_supervise_posts_success(self) -> None:
        posts: list[tuple[str, str]] = []
        p1, p2, p3, p4, p5 = self._patches(posts)
        with p1, p2, p3, p4, p5, mock.patch("subprocess.Popen",
                                            return_value=self._proc_returning(0)):
            code = run_job._supervise("jp-form-outreach", "rid", "/tmp/x.log", ["draft"])
        self.assertEqual(code, 0)
        self.assertTrue(any("✅" in t for _lvl, t in posts))

    def test_supervise_exit3_means_other_run_no_restart(self) -> None:
        posts: list[tuple[str, str]] = []
        p1, p2, p3, p4, p5 = self._patches(posts)
        with p1, p2, p3, p4, p5, mock.patch("subprocess.Popen",
                                            return_value=self._proc_returning(3)) as popen:
            code = run_job._supervise("jp-form-outreach", "rid", "/tmp/x.log", ["send"])
        self.assertEqual(code, 3)
        self.assertEqual(popen.call_count, 1)  # never restarted
        self.assertTrue(any("別の run" in t for _lvl, t in posts))

    def test_supervise_auto_restarts_on_crash_then_gives_up(self) -> None:
        posts: list[tuple[str, str]] = []
        import _outreach_core.run_supervisor as RS
        p1, p2, p3, p4, p5 = self._patches(posts)
        with p1, p2, p3, p4, p5, mock.patch("subprocess.Popen",
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

        p1, p2, p3, p4, p5 = self._patches(posts)
        with p1, p2, p3, p4, p5, \
                mock.patch.object(RS, "latest_activity_age_sec", return_value=10 ** 6), \
                mock.patch("subprocess.Popen", side_effect=[stalled, healthy]) as popen:
            code = run_job._supervise("jp-form-outreach", "rid", "/tmp/x.log", ["campaign"])
        self.assertEqual(code, 0)
        self.assertEqual(popen.call_count, 2)            # stalled killed → relaunched
        stalled.terminate.assert_called()                # we killed the stalled one
        self.assertTrue(any("stall" in t for _lvl, t in posts))

    def test_supervise_posts_failure_on_spawn_exception(self) -> None:
        posts: list[tuple[str, str]] = []
        p1, p2, p3, p4, p5 = self._patches(posts)
        with p1, p2, p3, p4, p5, mock.patch("subprocess.Popen", side_effect=OSError("spawn failed")):
            code = run_job._supervise("jp-form-outreach", "rid", "/tmp/x.log", ["send"])
        self.assertEqual(code, 1)
        self.assertTrue(any(lvl == "error" for lvl, _t in posts))

    def test_start_caffeinate_none_when_unavailable(self) -> None:
        with mock.patch.object(run_job.shutil, "which", return_value=None):
            self.assertIsNone(run_job._start_caffeinate(123))

    def test_start_caffeinate_waits_on_pid(self) -> None:
        with mock.patch.object(run_job.shutil, "which", return_value="/usr/bin/caffeinate"), \
                mock.patch("subprocess.Popen") as popen:
            run_job._start_caffeinate(4242)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/caffeinate")
        self.assertIn("-w", argv)
        self.assertIn("4242", argv)

    def test_stop_handles_none(self) -> None:
        run_job._stop(None)  # must not raise

    def test_drive_stops_on_target(self) -> None:
        args = mock.Mock(
            skill="jp-form-outreach",
            brief="torana-line-crm",
            target_sends=2,
            max_hours=8.0,
            per_run_limit=10,
            sleep_sec=0,
            run_args=[],
        )
        sent_counts = [0, 0, 2, 2]  # baseline, first check, after pass, next check
        posts: list[tuple[str, str]] = []
        with mock.patch.object(run_job, "resolve_brief_id", return_value="torana-line-crm"), \
                mock.patch.object(run_job, "_sent_count", side_effect=lambda *a, **k: sent_counts.pop(0)), \
                mock.patch.object(run_job, "_supervise", return_value=0) as sup, \
                mock.patch.object(run_job, "_post", side_effect=lambda t, level="info", **k: posts.append((level, t))):
            code = run_job.cmd_drive(args)
        self.assertEqual(code, 0)
        sup.assert_called_once()
        self.assertTrue(any("目標到達" in t for _lvl, t in posts))

    def test_drive_stops_on_time_limit(self) -> None:
        args = mock.Mock(
            skill="jp-form-outreach",
            brief="torana-line-crm",
            target_sends=50,
            max_hours=0.0001,  # clamped to >=60s internally
            per_run_limit=10,
            sleep_sec=0,
            run_args=[],
        )
        t = {"n": 0}
        posts: list[tuple[str, str]] = []

        def fake_mono():
            t["n"] += 1
            return 0 if t["n"] == 1 else 65  # over 60s clamp

        with mock.patch.object(run_job, "resolve_brief_id", return_value="torana-line-crm"), \
                mock.patch.object(run_job, "_sent_count", return_value=0), \
                mock.patch.object(run_job.time, "monotonic", side_effect=fake_mono), \
                mock.patch.object(run_job, "_supervise", return_value=0) as sup, \
                mock.patch.object(run_job, "_post", side_effect=lambda t, level="info", **k: posts.append((level, t))):
            code = run_job.cmd_drive(args)
        self.assertEqual(code, 0)
        sup.assert_not_called()
        self.assertTrue(any("時間上限" in t for _lvl, t in posts))


if __name__ == "__main__":
    unittest.main()
