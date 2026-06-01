#!/usr/bin/env python3
"""
Detached job runner for Doorman (v4 §15 reliability layer).

Goal: the OpenClaw agent must NEVER block its Slack turn on a long pipeline.
`job start` launches `run.py <args>` as a fully detached process and returns
immediately with a run_id + pid. The detached supervisor guarantees three
Slack posts independent of the agent:

  1. 🚀 start  (posted synchronously by `start`, before returning)
  2. … heartbeats (posted by run.py's HeartbeatSession while it runs)
  3. ✅/❌ terminal (posted by the supervisor on ANY exit, incl. crash)

This converts "agent occupied for 10 minutes" into "agent fires job, gets a
run_id in <2s, is free to keep talking on Slack". Status is then answerable
any time via `healthcheck ping` / `brief status` (file-based, stateless).

Usage (agent):
  ./job start jp-form-outreach campaign --brief torana-line-crm --limit 5
  ./job start linkedin-outreach send --brief torana-line-crm --ids 1,3 --auto-send

Internal (spawned by start, do not call directly):
  python -m _outreach_core.helpers.run_job __supervise__ <skill> <run_id> <logfile> -- <args...>
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.config import SKILLS_ROOT

_SKILLS = ("jp-form-outreach", "linkedin-outreach")


def _now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _skill_dir(skill: str) -> Path:
    return SKILLS_ROOT / skill


def _venv_python(skill_dir: Path) -> str:
    cand = skill_dir / ".venv" / "bin" / "python"
    return str(cand) if cand.exists() else sys.executable


def _logs_dir() -> Path:
    d = SKILLS_ROOT / "data" / "job_logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _post(text: str, *, level: str = "info", thread_ts: str | None = None) -> bool:
    try:
        from _outreach_core.notify import post

        return post(text, level=level, thread_ts=thread_ts)
    except Exception:
        return False


def build_child_command(skill: str, run_args: list[str]) -> list[str]:
    skill_dir = _skill_dir(skill)
    py = _venv_python(skill_dir)
    return [py, str(skill_dir / "run.py"), *run_args]


def start(
    skill: str,
    run_args: list[str],
    *,
    slack_channel_id: str | None = None,
    slack_thread_ts: str | None = None,
) -> dict[str, object]:
    """Launch run.py detached. Returns {run_id, pid, log}. Never blocks."""
    if skill not in _SKILLS:
        raise ValueError(f"unknown skill {skill!r}; expected one of {_SKILLS}")
    skill_dir = _skill_dir(skill)
    if not (skill_dir / "run.py").is_file():
        raise FileNotFoundError(skill_dir / "run.py")

    run_id = _now_id()
    log_path = _logs_dir() / f"{skill}-{run_id}.log"

    env = dict(os.environ)
    if slack_channel_id:
        env["DOORMAN_SLACK_CHANNEL_ID"] = slack_channel_id
    if slack_thread_ts:
        env["DOORMAN_SLACK_THREAD_TS"] = slack_thread_ts
    env.setdefault("PYTHONPATH", str(SKILLS_ROOT))

    supervisor_cmd = [
        sys.executable,
        "-m",
        "_outreach_core.helpers.run_job",
        "__supervise__",
        skill,
        run_id,
        str(log_path),
        "--",
        *run_args,
    ]

    cmd_label = " ".join(run_args) or "(no args)"
    try:
        with log_path.open("w", encoding="utf-8") as logf:
            proc = subprocess.Popen(  # noqa: S603 - trusted internal command
                supervisor_cmd,
                cwd=str(SKILLS_ROOT),
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as exc:
        _post(
            f"❌ ジョブ起動に失敗しました: {skill} {cmd_label} — {exc}",
            level="error",
            thread_ts=slack_thread_ts,
        )
        raise

    _post(
        f"🚀 開始: {skill} `{cmd_label}` (run_id={run_id}, pid={proc.pid})\n"
        f"進捗は自動で投稿します。状況確認は「ping」「進捗どう？」。",
        level="info",
        thread_ts=slack_thread_ts,
    )
    return {"run_id": run_id, "pid": proc.pid, "log": str(log_path)}


def _health_files() -> list[Path]:
    """system_health/*.json — the child run's heartbeat advances these."""
    d = SKILLS_ROOT / "data" / "system_health"
    try:
        return list(d.glob("*.json"))
    except OSError:
        return []


def _supervise(skill: str, run_id: str, log_path: str, run_args: list[str]) -> int:
    """Run run.py as a child with **stall detection + bounded auto-restart**.

    The campaign is idempotent (already-sent excluded, stale lock auto-cleared),
    so a stalled or crashed run is recovered by relaunching the same command,
    bounded by run_supervisor's restart budget. Guarantees a terminal Slack post.
    """
    from _outreach_core import run_supervisor as RS

    thread_ts = os.environ.get("DOORMAN_SLACK_THREAD_TS", "").strip() or None
    cmd = build_child_command(skill, run_args)
    cmd_label = " ".join(run_args) or "(no args)"
    state_dir = SKILLS_ROOT / "data"
    state = RS.load_state(state_dir)
    activity_paths = [Path(log_path)] + _health_files()

    while True:
        try:
            proc = subprocess.Popen(  # noqa: S603 - trusted internal command
                cmd, cwd=str(_skill_dir(skill)), stdin=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            _post(f"❌ 異常終了: {skill} `{cmd_label}` (run_id={run_id})\n例外: {exc!s}",
                  level="error", thread_ts=thread_ts)
            return 1

        killed_for_stall = False
        code: int | None = None
        while True:
            try:
                code = proc.wait(timeout=RS.POLL_SEC)
                break  # child exited
            except subprocess.TimeoutExpired:
                age = RS.latest_activity_age_sec([Path(log_path)] + _health_files())
                action = RS.decide(child_alive=True, exit_code=None,
                                   activity_age_sec=age, state=state)
                if action == RS.ACTION_RESTART_STALLED:
                    RS.record_restart(state, "stalled")
                    RS.save_state(state_dir, state)
                    _post(f"♻️ stall検知（{int(age or 0)}s 無進捗）→ 自動再開: {skill} "
                          f"`{cmd_label}` (run_id={run_id})。データは保全されています。",
                          level="warn", thread_ts=thread_ts)
                    _kill(proc)
                    killed_for_stall = True
                    break
                if action == RS.ACTION_GIVE_UP_STALLED:
                    _kill(proc)
                    _post(f"❌ stall が再試行上限に到達（{RS.MAX_RESTARTS}回/{RS.RESTART_WINDOW_MIN}分）"
                          f"。中断します: {skill} `{cmd_label}`。ログ: {log_path}",
                          level="error", thread_ts=thread_ts)
                    return 1
                # ACTION_CONTINUE → keep polling

        if killed_for_stall:
            continue  # relaunch (idempotent resume)

        # Child exited on its own.
        if code == 0:
            _post(f"✅ 完了: {skill} `{cmd_label}` (run_id={run_id})",
                  level="info", thread_ts=thread_ts)
            return 0
        if code == 3:
            # ActiveRunError — another LIVE run holds the lock; restarting won't help.
            _post(f"⏸ 別の run が進行中のため起動できませんでした: {skill} `{cmd_label}`。",
                  level="warn", thread_ts=thread_ts)
            return 3

        action = RS.decide(child_alive=False, exit_code=code, activity_age_sec=None, state=state)
        if action == RS.ACTION_RESTART_CRASH:
            RS.record_restart(state, "crash")
            RS.save_state(state_dir, state)
            _post(f"♻️ 異常終了 (exit={code}) → 自動再開: {skill} `{cmd_label}` "
                  f"(run_id={run_id})。データは保全されています。",
                  level="warn", thread_ts=thread_ts)
            continue
        _post(f"❌ 異常終了 (exit={code})。再試行上限に到達したため中断します: "
              f"{skill} `{cmd_label}`。ログ末尾を確認してください: {log_path}",
              level="error", thread_ts=thread_ts)
        return code


def _kill(proc: "subprocess.Popen") -> None:
    """Terminate a child process, escalating to kill, swallowing errors."""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def cmd_start(args: argparse.Namespace) -> int:
    info = start(
        args.skill,
        args.run_args,
        slack_channel_id=args.slack_channel_id,
        slack_thread_ts=args.slack_thread_ts,
    )
    print(f"run_id={info['run_id']} pid={info['pid']} log={info['log']}")
    print("Job is running detached. Agent turn can end now; progress posts to Slack.")
    return 0


def main() -> None:
    # Internal supervisor dispatch (bypasses argparse subcommands for `--` passthrough).
    if len(sys.argv) >= 5 and sys.argv[1] == "__supervise__":
        skill = sys.argv[2]
        run_id = sys.argv[3]
        log_path = sys.argv[4]
        rest = sys.argv[5:]
        if rest and rest[0] == "--":
            rest = rest[1:]
        sys.exit(_supervise(skill, run_id, log_path, rest))

    ap = argparse.ArgumentParser(
        description="Doorman detached job runner (v4 §15 reliability)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start", help="Launch run.py detached; returns run_id immediately")
    p.add_argument("skill", choices=list(_SKILLS))
    p.add_argument("--slack-channel-id", default=None)
    p.add_argument("--slack-thread-ts", default=None)
    p.add_argument("run_args", nargs=argparse.REMAINDER, help="Args passed to run.py")
    args = ap.parse_args()
    if args.cmd == "start":
        # Strip a leading '--' if the caller used `job start <skill> -- <args>`
        if args.run_args and args.run_args[0] == "--":
            args.run_args = args.run_args[1:]
        sys.exit(cmd_start(args))
    ap.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
