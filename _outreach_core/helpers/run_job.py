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
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.config import SKILLS_ROOT
from _outreach_core.config import resolve_brief_id
from _outreach_core.history import load_sent_set
from _outreach_core.openclaw_slack import resolve_slack_delivery_context
from _outreach_core.paths import brief_data_dir

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


def _sent_count(skill: str, brief_id: str) -> int:
    skill_dir = _skill_dir(skill)
    data_dir = brief_data_dir(skill_dir, brief_id)
    return len(load_sent_set(data_dir))


def _has_flag(args: list[str], flag: str) -> bool:
    return any(a == flag or a.startswith(f"{flag}=") for a in args)


def build_campaign_args(
    *,
    brief_id: str,
    remaining: int,
    per_run_limit: int,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Build one campaign invocation for drive mode.

    Ensures `campaign --brief <id>` and injects `--limit` unless caller already
    set one in extra args.
    """
    args = ["campaign", "--brief", brief_id]
    if extra_args:
        args.extend(extra_args)
    if not _has_flag(args, "--brief"):
        args.extend(["--brief", brief_id])
    if not _has_flag(args, "--limit"):
        run_limit = max(1, min(int(per_run_limit), max(1, int(remaining))))
        args.extend(["--limit", str(run_limit)])
    return args


def _post(
    text: str,
    *,
    level: str = "info",
    thread_ts: str | None = None,
    channel_id: str | None = None,
) -> bool:
    try:
        from _outreach_core import notify

        ok = notify.post(
            text,
            level=level,
            thread_ts=thread_ts,
            channel_id=channel_id,
        )
        setattr(_post, "last_error", notify.last_delivery_error())
        setattr(_post, "last_route", notify.last_delivery_route())
        return ok
    except Exception:
        setattr(_post, "last_error", "notify_exception")
        setattr(_post, "last_route", "none")
        return False


def _record_notification(
    log_path: str | Path,
    *,
    run_id: str,
    skill: str,
    phase: str,
    ok: bool,
    channel_id: str | None,
    thread_ts: str | None,
    route: str | None = None,
    error: str | None = None,
) -> None:
    """Persist delivery attempts even when no campaign event context exists."""
    try:
        route_s = route if isinstance(route, str) else ""
        error_s = error if isinstance(error, str) else ""
        path = Path(log_path).with_suffix(".notify.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "skill": skill,
            "phase": phase,
            "ok": bool(ok),
            "channel_id": channel_id or None,
            "thread": bool(thread_ts),
            "route": route_s or None,
            "error": error_s or None,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _job_post(
    text: str,
    *,
    run_id: str,
    skill: str,
    log_path: str | Path,
    phase: str,
    level: str = "info",
    thread_ts: str | None = None,
    channel_id: str | None = None,
) -> bool:
    ok = bool(
        _post(
            text,
            level=level,
            thread_ts=thread_ts,
            channel_id=channel_id,
        )
    )
    _record_notification(
        log_path,
        run_id=run_id,
        skill=skill,
        phase=phase,
        ok=ok,
        channel_id=channel_id,
        thread_ts=thread_ts,
        route=getattr(_post, "last_route", "") if isinstance(getattr(_post, "last_route", ""), str) else None,
        error=getattr(_post, "last_error", "") if isinstance(getattr(_post, "last_error", ""), str) else None,
    )
    return ok


def _extract_slack_flags(
    run_args: list[str],
) -> tuple[list[str], str | None, str | None]:
    """Accept launcher Slack flags anywhere without passing them to run.py."""
    cleaned: list[str] = []
    channel_id: str | None = None
    thread_ts: str | None = None
    i = 0
    while i < len(run_args):
        arg = run_args[i]
        matched = False
        for flag, field in (
            ("--slack-channel-id", "channel"),
            ("--slack-thread-ts", "thread"),
        ):
            value: str | None = None
            if arg == flag:
                if i + 1 >= len(run_args):
                    raise ValueError(f"{flag} requires a value")
                value = run_args[i + 1]
                i += 1
            elif arg.startswith(f"{flag}="):
                value = arg.split("=", 1)[1]
            if value is not None:
                if field == "channel":
                    channel_id = value
                else:
                    thread_ts = value
                matched = True
                break
        if not matched:
            cleaned.append(arg)
        i += 1
    return cleaned, channel_id, thread_ts


def _post_problem(
    problem: dict[str, object] | None,
    *,
    thread_ts: str | None = None,
    channel_id: str | None = None,
) -> bool:
    if not problem:
        return False
    try:
        from _outreach_core.notify import post_problem

        return post_problem(
            str(problem.get("kind") or "run_problem"),
            problem.get("target") if isinstance(problem.get("target"), dict) else {},
            problem.get("detail") if isinstance(problem.get("detail"), dict) else {},
            thread_ts=thread_ts,
            channel_id=channel_id,
        )
    except Exception:
        return False


def _brief_arg(run_args: list[str]) -> str | None:
    for i, arg in enumerate(run_args):
        if arg == "--brief" and i + 1 < len(run_args):
            return run_args[i + 1]
        if arg.startswith("--brief="):
            return arg.split("=", 1)[1]
    return None


def _recent_target_outcomes(skill: str, run_args: list[str], *, limit: int = 20) -> list[dict[str, object]]:
    brief_id = _brief_arg(run_args)
    if not brief_id:
        return []
    try:
        from _outreach_core import events as ev

        data_dir = brief_data_dir(_skill_dir(skill), resolve_brief_id(brief_id))
        rows = [
            r for r in ev.load_events(data_dir)
            if r.get("kind") == "send.target_outcome"
        ]
        return rows[-limit:]
    except Exception:
        return []


def _progress_int(row: dict[str, object], key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _format_progress_phase(row: dict[str, object]) -> tuple[str, int]:
    stage = str(row.get("stage") or "?")
    label = {
        "campaign": "キャンペーン",
        "list": "リスト作成",
        "fetch-leads": "リスト取得",
        "fetch-from-csv": "CSV取込",
        "enrich": "フォーム調査",
        "draft": "ドラフト作成",
        "send": "送信",
        "resolve": "リゾルバ",
        "research": "調査",
    }.get(stage, stage)
    total = _progress_int(row, "total")
    processed = _progress_int(row, "processed")
    sent = _progress_int(row, "sent")
    skipped = _progress_int(row, "skipped")
    needs_attention = _progress_int(row, "needs_attention")
    not_sent = skipped + needs_attention + max(0, processed - sent - skipped - needs_attention)
    line = (
        f"{label}: {processed}/{total}件処理 / "
        f"送信OK {sent} / 未送信・要対応 {not_sent}"
    )
    return line, not_sent


def _completion_summary(skill: str, run_args: list[str]) -> str:
    brief_id = _brief_arg(run_args)
    if not brief_id:
        return ""
    try:
        from _outreach_core import run_progress

        data_dir = brief_data_dir(_skill_dir(skill), resolve_brief_id(brief_id))
        snap = run_progress.read(data_dir)
    except Exception:
        return ""
    if not isinstance(snap, dict) or not snap:
        return ""
    phases: list[dict[str, object]] = []
    for row in snap.get("phases") or []:
        if isinstance(row, dict):
            phases.append(row)
    phases.append(snap)
    lines = ["結果サマリ:"]
    not_sent_total = 0
    for row in phases[-4:]:
        line, not_sent = _format_progress_phase(row)
        lines.append(f"  • {line}")
        not_sent_total += not_sent
    if not_sent_total > 0:
        lines.append("※「ジョブ終了」は処理終了の意味です。全件送信完了ではありません。")
    return "\n" + "\n".join(lines)


def _host_execution_blocked(
    thread_ts: str | None = None,
    channel_id: str | None = None,
) -> tuple[bool, str]:
    """Refuse to launch ANY Doorman campaign on a non-primary host.

    This is the launcher-level half of the host split: while the v20 guard in
    ``run.py`` blocks the actual submit, this stops the dev machine (e.g.
    ``NorimitsuM5MBP``) from even spinning up enrich/draft/send jobs. Only the
    designated primary (``MacMiniHome``, from ``data/primary_host``) launches.

    Returns ``(blocked, reason)``. Never blocks when no primary is configured
    (guard off) or when ``DOORMAN_FORCE_SEND=1`` is set for intentional local
    runs.
    """
    from _outreach_core import host_role

    allowed, reason = host_role.is_send_allowed()
    if allowed:
        return False, reason
    host = host_role.current_host()
    primary = host_role.configured_primary_host()
    _post(
        f"⏭ このホストではDoormanを実行しません（実行担当ではありません）。\n"
        f"　このマシン: {host} / 実行担当(primary): {primary}\n"
        f"　Doormanの実行は {primary} 側でのみ行われます（開発機での誤実行防止）。\n"
        f"　意図的にこのマシンで動かすなら DOORMAN_FORCE_SEND=1 を付けてください。",
        level="warn",
        thread_ts=thread_ts,
        channel_id=channel_id,
    )
    print(f"[run_job] BLOCKED by host guard: {reason}")
    return True, reason


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
    run_args, embedded_channel, embedded_thread = _extract_slack_flags(run_args)
    context = resolve_slack_delivery_context(
        channel_id=slack_channel_id or embedded_channel,
        thread_ts=slack_thread_ts or embedded_thread,
    )
    slack_channel_id = context.channel_id or None
    slack_thread_ts = context.thread_ts or None

    blocked, reason = _host_execution_blocked(slack_thread_ts, slack_channel_id)
    if blocked:
        return {"run_id": None, "pid": None, "log": None,
                "blocked": True, "reason": reason}
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
    acknowledged = _job_post(
        f"🚀 受付: {skill} `{cmd_label}` (run_id={run_id})\n"
        f"起動処理に入りました。進捗は約10分ごとに自動投稿します。",
        run_id=run_id,
        skill=skill,
        log_path=log_path,
        phase="start",
        level="info",
        thread_ts=slack_thread_ts,
        channel_id=slack_channel_id,
    )
    # A Slack-routed launch must never continue silently. For a local CLI with
    # no Slack route, notification remains best-effort.
    if slack_channel_id and not acknowledged:
        raise RuntimeError(
            "Slackへの開始通知に失敗したため、ジョブを起動しませんでした。"
            f" 通知監査: {log_path.with_suffix('.notify.jsonl')}"
        )
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
        _job_post(
            f"❌ ジョブ起動に失敗しました: {skill} {cmd_label} — {exc}",
            run_id=run_id,
            skill=skill,
            log_path=log_path,
            phase="spawn_failed",
            level="error",
            thread_ts=slack_thread_ts,
            channel_id=slack_channel_id,
        )
        raise

    return {
        "run_id": run_id,
        "pid": proc.pid,
        "log": str(log_path),
        "notification_ok": acknowledged,
        "slack_context_source": context.source,
        "slack_thread": bool(slack_thread_ts),
    }


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
    channel_id = os.environ.get("DOORMAN_SLACK_CHANNEL_ID", "").strip() or None
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
            _job_post(
                f"❌ 異常終了: {skill} `{cmd_label}` (run_id={run_id})\n例外: {exc!s}",
                run_id=run_id,
                skill=skill,
                log_path=log_path,
                phase="terminal",
                level="error",
                thread_ts=thread_ts,
                channel_id=channel_id,
            )
            return 1

        # Keep the machine awake while this run lives (auto-exits with the child).
        caf = _start_caffeinate(proc.pid)

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
                          level="warn", thread_ts=thread_ts, channel_id=channel_id)
                    _kill(proc)
                    killed_for_stall = True
                    break
                if action == RS.ACTION_GIVE_UP_STALLED:
                    _kill(proc)
                    _stop(caf)
                    _post_problem(
                        RS.build_give_up_problem(
                            action,
                            exit_code=None,
                            activity_age_sec=age,
                            recent_outcomes=_recent_target_outcomes(skill, run_args),
                        ),
                        thread_ts=thread_ts,
                        channel_id=channel_id,
                    )
                    _job_post(
                        f"❌ stall が再試行上限に到達（{RS.MAX_RESTARTS}回/"
                        f"{RS.RESTART_WINDOW_MIN}分）。中断します: {skill} "
                        f"`{cmd_label}`。ログ: {log_path}",
                        run_id=run_id,
                        skill=skill,
                        log_path=log_path,
                        phase="terminal",
                        level="error",
                        thread_ts=thread_ts,
                        channel_id=channel_id,
                    )
                    return 1
                # ACTION_CONTINUE → keep polling

        _stop(caf)
        if killed_for_stall:
            continue  # relaunch (idempotent resume)

        # Child exited on its own.
        if code == 0:
            summary = _completion_summary(skill, run_args)
            _job_post(
                f"✅ ジョブ終了: {skill} `{cmd_label}` (run_id={run_id}){summary}",
                run_id=run_id,
                skill=skill,
                log_path=log_path,
                phase="terminal",
                level="info",
                thread_ts=thread_ts,
                channel_id=channel_id,
            )
            return 0
        if code == 3:
            # ActiveRunError — another LIVE run holds the lock; restarting won't help.
            _job_post(
                f"⏸ 別の run が進行中のため起動できませんでした: {skill} `{cmd_label}`。",
                run_id=run_id,
                skill=skill,
                log_path=log_path,
                phase="terminal",
                level="warn",
                thread_ts=thread_ts,
                channel_id=channel_id,
            )
            return 3
        if code in (2, 4):
            # 2 = CLI/brief usage error, 4 = deterministic quality/completion
            # gate. Replaying the identical command cannot repair either one
            # and previously produced three noisy retries for a misspelled
            # brief id.
            label = "CLI/brief error" if code == 2 else "quality/completion gate"
            _job_post(
                f"❌ {label} (exit={code})。自動再試行しません: "
                f"{skill} `{cmd_label}`。ログ: {log_path}",
                run_id=run_id,
                skill=skill,
                log_path=log_path,
                phase="terminal",
                level="error",
                thread_ts=thread_ts,
                channel_id=channel_id,
            )
            return code

        action = RS.decide(child_alive=False, exit_code=code, activity_age_sec=None, state=state)
        if action == RS.ACTION_RESTART_CRASH:
            RS.record_restart(state, "crash")
            RS.save_state(state_dir, state)
            _post(f"♻️ 異常終了 (exit={code}) → 自動再開: {skill} `{cmd_label}` "
                  f"(run_id={run_id})。データは保全されています。",
                  level="warn", thread_ts=thread_ts, channel_id=channel_id)
            continue
        _post_problem(
            RS.build_give_up_problem(
                action,
                exit_code=code,
                activity_age_sec=None,
                recent_outcomes=_recent_target_outcomes(skill, run_args),
            ),
            thread_ts=thread_ts,
            channel_id=channel_id,
        )
        _job_post(
            f"❌ 異常終了 (exit={code})。再試行上限に到達したため中断します: "
            f"{skill} `{cmd_label}`。ログ末尾を確認してください: {log_path}",
            run_id=run_id,
            skill=skill,
            log_path=log_path,
            phase="terminal",
            level="error",
            thread_ts=thread_ts,
            channel_id=channel_id,
        )
        return code


def _start_caffeinate(pid: int) -> "subprocess.Popen | None":
    """Hold the Mac awake (no idle/disk sleep) for as long as the run lives.

    A laptop sleeping mid-run is a top cause of stalls: launchd ticks stop and
    the browser session can drop. ``caffeinate -w <pid>`` exits automatically
    when the child exits, so it self-cleans even on crash. Best-effort: returns
    None if caffeinate is unavailable.
    """
    exe = shutil.which("caffeinate")
    if not exe:
        return None
    try:
        return subprocess.Popen(  # noqa: S603 - trusted, fixed argv
            [exe, "-i", "-m", "-w", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None


def _stop(proc: "subprocess.Popen | None") -> None:
    if proc is None:
        return
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass


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
    if info.get("blocked"):
        print(f"スキップ: このホストは実行担当ではありません（{info.get('reason')}）")
        return 0
    print(f"run_id={info['run_id']} pid={info['pid']} log={info['log']}")
    print("ジョブは別プロセスで実行中です。進捗はSlackへ自動投稿されます。")
    return 0


def cmd_drive(args: argparse.Namespace) -> int:
    """Run campaign repeatedly until target sends or max runtime is reached.

    This is an ops wrapper for "8 hours / 50 sends" style unattended operation.
    Each pass still runs through `_supervise`, so stall/crash auto-restart
    semantics are preserved.
    """
    skill = args.skill
    if skill not in _SKILLS:
        raise ValueError(f"unknown skill {skill!r}; expected one of {_SKILLS}")

    context = resolve_slack_delivery_context()
    drive_channel_id = context.channel_id or None
    drive_thread_ts = context.thread_ts or None
    if drive_channel_id:
        os.environ["DOORMAN_SLACK_CHANNEL_ID"] = drive_channel_id
    if drive_thread_ts:
        os.environ["DOORMAN_SLACK_THREAD_TS"] = drive_thread_ts
    blocked, reason = _host_execution_blocked(drive_thread_ts, drive_channel_id)
    if blocked:
        print(f"[drive] skipped: host not primary ({reason})")
        return 0

    brief_id = resolve_brief_id(args.brief)
    target_sends = max(1, int(args.target_sends))
    max_hours = float(args.max_hours)
    max_runtime_sec = max(60, int(max_hours * 3600))
    per_run_limit = max(1, int(args.per_run_limit))
    sleep_sec = max(0, int(args.sleep_sec))
    extra_args = list(args.run_args or [])
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]

    start_mono = time.monotonic()
    baseline_sent = _sent_count(skill, brief_id)
    no_progress_passes = 0
    pass_no = 0
    thread_ts = drive_thread_ts

    _post(
        f"🕒 長時間運転を開始: {skill} brief={brief_id}\n"
        f"目標: +{target_sends}件送信 / 上限: {max_hours:g}h / "
        f"1周あたりlimit: {per_run_limit}",
        level="info",
        thread_ts=thread_ts,
        channel_id=drive_channel_id,
    )

    while True:
        current = _sent_count(skill, brief_id)
        delta = max(0, current - baseline_sent)
        elapsed = int(time.monotonic() - start_mono)
        if delta >= target_sends:
            _post(
                f"✅ 目標到達: +{delta}件送信（目標 +{target_sends}）"
                f" / 経過 {elapsed // 60}分",
                level="info",
                thread_ts=thread_ts,
                channel_id=drive_channel_id,
            )
            print(f"[drive] done: reached target (+{delta})")
            return 0
        if elapsed >= max_runtime_sec:
            _post(
                f"⏹ 時間上限に到達: +{delta}件送信 / 目標 +{target_sends}件 "
                f"(max {max_hours:g}h)",
                level="warn",
                thread_ts=thread_ts,
                channel_id=drive_channel_id,
            )
            print(f"[drive] stop: time limit reached (+{delta}/{target_sends})")
            return 0

        remaining = target_sends - delta
        run_args = build_campaign_args(
            brief_id=brief_id,
            remaining=remaining,
            per_run_limit=per_run_limit,
            extra_args=extra_args,
        )
        pass_no += 1
        run_id = f"{_now_id()}-p{pass_no}"
        log_path = _logs_dir() / f"{skill}-{run_id}.log"
        print(
            f"[drive] pass {pass_no}: sent +{delta}/{target_sends}, "
            f"remaining={remaining}, cmd={' '.join(run_args)}"
        )
        code = _supervise(skill, run_id, str(log_path), run_args)

        after = _sent_count(skill, brief_id)
        progressed = after > current
        if progressed:
            no_progress_passes = 0
        else:
            no_progress_passes += 1

        if code != 0 and no_progress_passes >= 3:
            _post(
                f"❌ 3周連続で送信進捗なし（現在 +{max(0, after - baseline_sent)}件）。"
                "安全のため停止します。ログを確認してください。",
                level="error",
                thread_ts=thread_ts,
                channel_id=drive_channel_id,
            )
            return code

        if sleep_sec > 0:
            time.sleep(sleep_sec)


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
    p = sub.add_parser(
        "drive",
        help="Repeat campaign until target sends reached or max runtime elapsed",
    )
    p.add_argument("skill", choices=list(_SKILLS))
    p.add_argument("--brief", required=True, help="Brief id")
    p.add_argument("--target-sends", type=int, default=50,
                   help="Stop when this many NEW sends are added (default: 50)")
    p.add_argument("--max-hours", type=float, default=8.0,
                   help="Stop after this many hours even if target not reached (default: 8)")
    p.add_argument("--per-run-limit", type=int, default=10,
                   help="`campaign --limit` per pass if not supplied in --run-args")
    p.add_argument("--sleep-sec", type=int, default=30,
                   help="Pause between passes in seconds (default: 30)")
    p.add_argument(
        "run_args",
        nargs=argparse.REMAINDER,
        help="Optional extra args appended to `campaign` (e.g. --skip-enrich)",
    )
    args = ap.parse_args()
    if args.cmd == "start":
        # Strip a leading '--' if the caller used `job start <skill> -- <args>`
        if args.run_args and args.run_args[0] == "--":
            args.run_args = args.run_args[1:]
        sys.exit(cmd_start(args))
    if args.cmd == "drive":
        if args.run_args and args.run_args[0] == "--":
            args.run_args = args.run_args[1:]
        sys.exit(cmd_drive(args))
    ap.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
