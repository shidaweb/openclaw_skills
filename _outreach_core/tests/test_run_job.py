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
    def setUp(self) -> None:
        # The launcher host gate must not depend on THIS machine's hostname /
        # data/primary_host — patch it open so the runner tests run on any host
        # (mirrors the v20 send-guard handling in test_v15_reliability).
        from _outreach_core import host_role
        self._guard = mock.patch.object(
            host_role, "is_send_allowed", return_value=(True, "test"),
        )
        self._guard.start()
        self.addCleanup(self._guard.stop)
        self._tmp_logs = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_logs.cleanup)
        self._logs_dir = Path(self._tmp_logs.name) / "job_logs"
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._logs_patch = mock.patch.object(
            run_job, "_logs_dir", return_value=self._logs_dir
        )
        self._logs_patch.start()
        self.addCleanup(self._logs_patch.stop)

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
        context = mock.Mock(
            channel_id="C123",
            thread_ts="123.456",
            source="argument+argument_thread",
        )
        with mock.patch.object(
            run_job, "_post", side_effect=lambda t, **k: posts.append(t) or True
        ), mock.patch.object(
            run_job, "resolve_slack_delivery_context", return_value=context
        ), mock.patch(
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
        self.assertEqual(kwargs["env"]["DOORMAN_SLACK_CHANNEL_ID"], "C123")
        self.assertEqual(kwargs["env"]["DOORMAN_SLACK_THREAD_TS"], "123.456")
        self.assertTrue(any("受付" in p for p in posts))
        self.assertTrue(info["notification_ok"])

    def test_slack_flags_after_campaign_are_extracted(self) -> None:
        cleaned, channel, thread = run_job._extract_slack_flags([
            "campaign",
            "--brief",
            "tenbin-link",
            "--slack-channel-id",
            "C123",
            "--slack-thread-ts=123.456",
        ])
        self.assertEqual(cleaned, ["campaign", "--brief", "tenbin-link"])
        self.assertEqual(channel, "C123")
        self.assertEqual(thread, "123.456")

    def test_start_does_not_spawn_when_slack_ack_fails(self) -> None:
        context = mock.Mock(channel_id="C123", thread_ts="", source="argument")
        with mock.patch.object(run_job, "_post", return_value=False), \
                mock.patch.object(
                    run_job, "resolve_slack_delivery_context", return_value=context
                ), mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(run_job, "_skill_dir") as skill_dir:
            d = Path(tempfile.mkdtemp())
            (d / "run.py").write_text("# stub\n")
            skill_dir.return_value = d
            with self.assertRaisesRegex(RuntimeError, "ジョブを起動しませんでした"):
                run_job.start("linkedin-outreach", ["campaign"])
        popen.assert_not_called()

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

    def test_completion_summary_makes_partial_send_clear(self) -> None:
        snap = {
            "stage": "resolve",
            "total": 2,
            "processed": 2,
            "sent": 0,
            "skipped": 2,
            "needs_attention": 0,
            "status": "done",
            "phases": [
                {
                    "stage": "send",
                    "total": 6,
                    "processed": 6,
                    "sent": 4,
                    "skipped": 1,
                    "needs_attention": 1,
                    "status": "done",
                }
            ],
        }
        with mock.patch.object(run_job, "_skill_dir", return_value=Path("/tmp/skill")), \
                mock.patch.object(run_job, "brief_data_dir", return_value=Path("/tmp/brief")), \
                mock.patch("_outreach_core.run_progress.read", return_value=snap):
            out = run_job._completion_summary(
                "jp-form-outreach", ["campaign", "--brief", "doorman-ai"]
            )
        self.assertIn("送信: 6/6件処理 / 送信OK 4 / 未送信・要対応 2", out)
        self.assertIn("リゾルバ: 2/2件処理 / 送信OK 0 / 未送信・要対応 2", out)
        self.assertIn("全件送信完了ではありません", out)

    def test_supervise_exit3_means_other_run_no_restart(self) -> None:
        posts: list[tuple[str, str]] = []
        p1, p2, p3, p4, p5 = self._patches(posts)
        with p1, p2, p3, p4, p5, mock.patch("subprocess.Popen",
                                            return_value=self._proc_returning(3)) as popen:
            code = run_job._supervise("jp-form-outreach", "rid", "/tmp/x.log", ["send"])
        self.assertEqual(code, 3)
        self.assertEqual(popen.call_count, 1)  # never restarted
        self.assertTrue(any("別の run" in t for _lvl, t in posts))

    def test_supervise_exit2_usage_error_is_not_retried(self) -> None:
        posts: list[tuple[str, str]] = []
        p1, p2, p3, p4, p5 = self._patches(posts)
        with p1, p2, p3, p4, p5, mock.patch(
            "subprocess.Popen", return_value=self._proc_returning(2)
        ) as popen:
            code = run_job._supervise(
                "linkedin-outreach", "rid", "/tmp/x.log", ["campaign", "--brief", "bad"]
            )
        self.assertEqual(code, 2)
        self.assertEqual(popen.call_count, 1)
        self.assertTrue(any("自動再試行しません" in t for _lvl, t in posts))

    def test_supervise_auto_restarts_on_crash_then_gives_up(self) -> None:
        posts: list[tuple[str, str]] = []
        import _outreach_core.run_supervisor as RS
        p1, p2, p3, p4, p5 = self._patches(posts)
        with p1, p2, p3, p4, p5, mock.patch("subprocess.Popen",
                                            side_effect=lambda *a, **k: self._proc_returning(1)) as popen, \
                mock.patch.object(run_job, "_post_problem", return_value=True) as problem:
            code = run_job._supervise("jp-form-outreach", "rid", "/tmp/x.log", ["send"])
        self.assertEqual(code, 1)
        # initial launch + MAX_RESTARTS relaunches
        self.assertEqual(popen.call_count, RS.MAX_RESTARTS + 1)
        problem.assert_called_once()
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

    def test_supervise_stalled_give_up_posts_problem(self) -> None:
        import subprocess as _sp
        import _outreach_core.run_supervisor as RS
        posts: list[tuple[str, str]] = []

        stalled = mock.Mock()
        stalled.wait.side_effect = _sp.TimeoutExpired(cmd="x", timeout=1)
        stalled.terminate.return_value = None

        p1, p2, p3, p4, p5 = self._patches(posts)
        with p1, p2, p3, p4, p5, \
                mock.patch.object(RS, "latest_activity_age_sec", return_value=10 ** 6), \
                mock.patch.object(RS, "decide", return_value=RS.ACTION_GIVE_UP_STALLED), \
                mock.patch.object(run_job, "_post_problem", return_value=True) as problem, \
                mock.patch("subprocess.Popen", return_value=stalled):
            code = run_job._supervise("jp-form-outreach", "rid", "/tmp/x.log", ["campaign"])
        self.assertEqual(code, 1)
        stalled.terminate.assert_called()
        problem.assert_called_once()
        self.assertTrue(any(lvl == "error" for lvl, _t in posts))

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
