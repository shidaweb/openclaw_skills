#!/usr/bin/env python3
"""System health heartbeat for Doorman (v4 §15-B)."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.active_run import is_lock_alive, read_lock
from _outreach_core.config import SKILLS_ROOT
from _outreach_core.progress import current_task_path
from _outreach_core.verify import list_open_needs_attention

DOORMAN_VERSION = "v4"
_SKILL_DIRS = ("jp-form-outreach", "linkedin-outreach")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def hostname() -> str:
    return socket.gethostname().split(".")[0]


def system_health_dir(skills_root: Path | None = None) -> Path:
    root = skills_root or SKILLS_ROOT
    return root / "data" / "system_health"


def health_path(skills_root: Path | None = None) -> Path:
    return system_health_dir(skills_root) / f"{hostname()}.json"


def _progress_from_task(data_dir: Path) -> tuple[int, int]:
    path = current_task_path(data_dir)
    if not path.is_file():
        return 0, 0
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            return 0, 0
        ev = json.loads(lines[-1])
        return int(ev.get("current") or 0), int(ev.get("total") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0, 0


def run_activity_age_seconds(
    data_dir: Path,
    *,
    now_epoch: float | None = None,
) -> int | None:
    """Age of the freshest run-owned progress signal.

    The gateway/system health heartbeat is host-level and can be stale even
    while a campaign is actively writing per-brief progress.  Using the
    current-task/run-progress/event mtimes prevents the watchdog from reporting
    a healthy, advancing campaign as stuck.
    """
    candidates = (
        current_task_path(data_dir),
        data_dir / "run_progress.json",
        data_dir / "events.jsonl",
        data_dir / "active_run.lock",
    )
    mtimes: list[float] = []
    for path in candidates:
        try:
            if path.is_file():
                mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    if not mtimes:
        return None
    import time

    now = time.time() if now_epoch is None else float(now_epoch)
    return max(0, int(now - max(mtimes)))


def forward_progress_age_seconds(
    data_dir: Path,
    *,
    now_epoch: float | None = None,
) -> int | None:
    """v32 FX1 — age of the freshest MAIN-THREAD forward-progress write.

    Unlike :func:`run_activity_age_seconds` (the watchdog's "is anything
    alive-ish" probe), this feeds the run supervisor's STALL decision, so it
    must ignore every file a background thread can refresh while the main
    thread is wedged:

    - events.jsonl        — excluded: ``HeartbeatSession._heartbeat_loop``
                            emits ``heartbeat.tick`` from a timer thread.
    - system_health/*.json — never included: the keepalive thread and the
                            watchdog refresh it host-wide every ~60s (this
                            is what silently neutralized stall detection).
    - the run log         — not a file candidate here: the stdout keepalive
                            thread prints every ~60s regardless of progress.

    current_task.jsonl (hb.start/tick/note/end), run_progress.json
    (start/transition/bump/finish) and active_run.lock (acquire/update_lock)
    are written exclusively by main-thread forward progress — verified in
    progress.py / run_progress.py / active_run.py.
    """
    candidates = (
        current_task_path(data_dir),
        data_dir / "run_progress.json",
        data_dir / "active_run.lock",
    )
    mtimes: list[float] = []
    for path in candidates:
        try:
            if path.is_file():
                mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    if not mtimes:
        return None
    import time

    now = time.time() if now_epoch is None else float(now_epoch)
    return max(0, int(now - max(mtimes)))


def collect_active_runs(skills_root: Path | None = None) -> list[dict[str, Any]]:
    root = skills_root or SKILLS_ROOT
    runs: list[dict[str, Any]] = []
    for skill_name in _SKILL_DIRS:
        briefs_root = root / skill_name / "data" / "briefs"
        if not briefs_root.is_dir():
            continue
        for brief_dir in briefs_root.iterdir():
            if not brief_dir.is_dir():
                continue
            lock = read_lock(brief_dir)
            if not lock or not is_lock_alive(lock):
                continue
            cur_task, tot_task = _progress_from_task(brief_dir)
            current = int(lock.get("current_target_idx") or cur_task or 0)
            total = int(lock.get("total_targets") or tot_task or 0)
            runs.append(
                {
                    "brief_id": lock.get("brief_id") or brief_dir.name,
                    "skill": lock.get("skill") or skill_name,
                    "run_id": lock.get("run_id"),
                    "stage": lock.get("stage") or "campaign",
                    "current": current,
                    "total": total,
                    "thread_ts": lock.get("slack_thread_ts") or None,
                    "activity_age_sec": run_activity_age_seconds(brief_dir),
                }
            )
    return runs


def count_open_needs_attention(skills_root: Path | None = None) -> int:
    root = skills_root or SKILLS_ROOT
    total = 0
    for skill_name in _SKILL_DIRS:
        briefs_root = root / skill_name / "data" / "briefs"
        if not briefs_root.is_dir():
            continue
        for brief_dir in briefs_root.iterdir():
            if brief_dir.is_dir():
                total += len(list_open_needs_attention(brief_dir))
    return total


def _detect_openclaw_pid() -> int | None:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "openclaw"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return int(out.stdout.strip().splitlines()[0])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def read_health(skills_root: Path | None = None) -> dict[str, Any] | None:
    path = health_path(skills_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_heartbeat(
    skills_root: Path | None = None,
    *,
    openclaw_pid: int | None = None,
    slack_connected: bool | None = None,
    extra: dict[str, Any] | None = None,
    touch_command: bool = False,
) -> Path:
    """Write data/system_health/<hostname>.json. Never raises."""
    try:
        path = health_path(skills_root)
        prev = read_health(skills_root) or {}
        now = _utc_now()
        payload: dict[str, Any] = {
            "host": hostname(),
            "ts": now,
            "openclaw_pid": openclaw_pid if openclaw_pid is not None else _detect_openclaw_pid(),
            "slack_connected": slack_connected,
            "last_command_at": now if touch_command else prev.get("last_command_at"),
            "active_runs": collect_active_runs(skills_root),
            "open_needs_attention_count": count_open_needs_attention(skills_root),
            "doorman_version": DOORMAN_VERSION,
        }
        if extra:
            payload.update(extra)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path
    except Exception:
        try:
            return health_path(skills_root)
        except Exception:
            root = skills_root or SKILLS_ROOT
            return root / "data" / "system_health" / f"{hostname()}.json"


def touch_last_command(skills_root: Path | None = None) -> Path:
    """Update last_command_at only (Slack message received)."""
    path = health_path(skills_root)
    try:
        prev = read_health(skills_root) or {}
        prev["host"] = hostname()
        prev["ts"] = _utc_now()
        prev["last_command_at"] = _utc_now()
        if "doorman_version" not in prev:
            prev["doorman_version"] = DOORMAN_VERSION
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(prev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass
    return path


def heartbeat_age_seconds(health: dict[str, Any] | None) -> int | None:
    if not health:
        return None
    ts = _parse_ts(health.get("ts"))
    if not ts:
        return None
    return int((datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds())


def _fmt_age(seconds: int | None) -> str:
    if seconds is None:
        return "不明"
    if seconds < 60:
        return f"{seconds}秒前"
    if seconds < 3600:
        return f"{seconds // 60}分前"
    return f"{seconds // 3600}時間{(seconds % 3600) // 60}分前"


def format_ping_line(health: dict[str, Any] | None) -> str:
    age = heartbeat_age_seconds(health)
    if health is None:
        return "⚠️ 稼働記録がまだありません（復旧: ./healthcheck write-heartbeat）"
    runs = health.get("active_runs") or []
    run_bits: list[str] = []
    for r in runs[:3]:
        skill = (r.get("skill") or "?").replace("jp-form-outreach", "JPフォーム").replace(
            "linkedin-outreach", "LinkedIn"
        )
        stage = {
            "campaign": "キャンペーン",
            "send": "送信",
            "draft": "ドラフト",
            "enrich": "調査",
            "resolve": "リゾルバ",
        }.get(str(r.get("stage") or ""), str(r.get("stage") or "?"))
        run_bits.append(
            f"{skill} {stage} {r.get('current', 0)}/{r.get('total', 0)}"
        )
    runs_txt = "、".join(run_bits) if run_bits else "なし"
    na = int(health.get("open_needs_attention_count") or 0)
    last_ev = _latest_event_kind()
    ev_txt = f" / 最終イベント: {last_ev}" if last_ev else ""
    return (
        f"✅ 稼働中。heartbeat {_fmt_age(age)} / 実行中 {len(runs)}件（{runs_txt}） / "
        f"要対応 {na}件{ev_txt}"
    )


def _latest_event_kind(skills_root: Path | None = None) -> str | None:
    root = skills_root or SKILLS_ROOT
    best_ts: datetime | None = None
    best_label: str | None = None
    for skill_name in _SKILL_DIRS:
        briefs_root = root / skill_name / "data" / "briefs"
        if not briefs_root.is_dir():
            continue
        for brief_dir in briefs_root.iterdir():
            ev_path = brief_dir / "events.jsonl"
            if not ev_path.is_file():
                continue
            try:
                lines = [ln for ln in ev_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                if not lines:
                    continue
                ev = json.loads(lines[-1])
                ts = _parse_ts(ev.get("ts"))
                if ts and (best_ts is None or ts > best_ts):
                    best_ts = ts
                    tid = ev.get("target_id") or "?"
                    best_label = f"{_event_kind_label(ev.get('kind'))} ({tid})"
            except (OSError, json.JSONDecodeError):
                continue
    return best_label


def format_status(health: dict[str, Any] | None) -> str:
    lines = [format_ping_line(health), ""]
    if health:
        lines.append("## system_health（内部状態）")
        lines.append(json.dumps(health, ensure_ascii=False, indent=2))
    lines.append("")
    lines.append("## 最近のイベント（5件）")
    for row in _recent_events(5):
        lines.append(f"- {row}")
    return "\n".join(lines)


def _event_kind_label(kind: Any) -> str:
    return {
        "slack.notified": "Slack通知",
        "slack.problem_notified": "Slack要確認通知",
        "slack.problem_deduped": "Slack重複通知抑制",
        "send.target_outcome": "送信結果",
        "send.target_timeout": "1社タイムアウト",
        "send.lead_crashed": "1社エラー",
        "send.unverified_prior_attempt": "未検証送信候補",
        "send.interrupted_before_submit_resume_skip": "中断復帰スキップ",
        "campaign.awaiting_upfront_approval": "承認待ち",
        "heartbeat.tick": "ハートビート",
    }.get(str(kind or ""), str(kind or "?"))


def _recent_events(limit: int, skills_root: Path | None = None) -> list[str]:
    root = skills_root or SKILLS_ROOT
    collected: list[tuple[datetime, str]] = []
    for skill_name in _SKILL_DIRS:
        briefs_root = root / skill_name / "data" / "briefs"
        if not briefs_root.is_dir():
            continue
        for brief_dir in briefs_root.iterdir():
            ev_path = brief_dir / "events.jsonl"
            if not ev_path.is_file():
                continue
            try:
                lines = [ln for ln in ev_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                for line in lines[-limit:]:
                    ev = json.loads(line)
                    ts = _parse_ts(ev.get("ts"))
                    if ts:
                        label = (
                            f"{ev.get('ts')} {_event_kind_label(ev.get('kind'))} "
                            f"brief={brief_dir.name} target={ev.get('target_id', '-')}"
                        )
                        collected.append((ts, label))
            except (OSError, json.JSONDecodeError):
                continue
    collected.sort(key=lambda x: x[0], reverse=True)
    return [label for _, label in collected[:limit]] or ["(none)"]


def read_watchdog_state(skills_root: Path | None = None) -> dict[str, Any] | None:
    root = skills_root or SKILLS_ROOT
    path = root / "data" / "watchdog.state.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def watchdog_liveness_line(skills_root: Path | None = None) -> str:
    """One-line, subprocess-free view of whether the watchdog itself is ticking.

    This makes a *dead watchdog* visible from a normal "ping" — the failure mode
    where nothing else would ever tell you supervision has stopped."""
    st = read_watchdog_state(skills_root)
    last = (st or {}).get("last_tick_epoch")
    if not last:
        return "🐶 watchdog: ❓ 未確認（まだ tick されていないかも）"
    import time as _time

    age = int(_time.time() - float(last))
    interval = 60
    try:
        from _outreach_core import gateway_config as _gw

        interval = int(
            _gw.watchdog_tuning(_gw.load_gateway_config(skills_root or SKILLS_ROOT)).get(
                "interval_sec", 60
            )
        )
    except Exception:
        pass
    age_txt = f"{age}s前" if age < 90 else f"{age // 60}分前"
    if age <= interval * 4:
        return f"🐶 watchdog: ✅ 稼働中（last tick {age_txt}）"
    return (
        f"🐶 watchdog: 🚨 停止疑い（last tick {age_txt}）"
        " — 復旧: python3 -m _outreach_core.helpers.watchdog ensure"
    )


def watchdog_error_line(skills_root: Path | None = None) -> str | None:
    """v32 FX5 — surface a non-empty ``data/watchdog.err`` as a warning line.

    The watchdog once died on an import error and stderr silently accumulated
    in watchdog.err for a MONTH while every ping said nothing. Any content in
    that file means the launchd job hit stderr at least once since install —
    worth a human glance. Returns None when the file is absent/empty.
    """
    root = skills_root or SKILLS_ROOT
    path = root / "data" / "watchdog.err"
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size <= 0:
            return None
        tail = ""
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = [ln.strip() for ln in fh.readlines() if ln.strip()]
        if lines:
            tail = lines[-1][:160]
    except OSError:
        return None
    return (
        f"🐶 watchdog.err: ⚠ 内容あり（{size} bytes / 最終行: {tail or '?'}）"
        " — import失敗等の可能性。中身を確認し、解消後は truncate してください"
    )


def _opportunistic_watchdog_ensure(skills_root: Path | None = None) -> None:
    """Whenever a human pings, take the chance to re-ensure the watchdog is
    installed+loaded. A second, independent recovery path for a dead watchdog.
    Best-effort; never raises."""
    try:
        from _outreach_core.helpers import watchdog as _wd

        _wd.ensure_watchdog_installed(skills_root)
    except Exception:
        pass


def cmd_write_heartbeat(_args: argparse.Namespace) -> int:
    path = write_heartbeat()
    print(f"wrote {path}")
    return 0


def cmd_touch_command(_args: argparse.Namespace) -> int:
    path = touch_last_command()
    print(f"updated last_command_at → {path}")
    return 0


def _refresh_then_read() -> dict[str, Any] | None:
    """Recompute the heartbeat on demand so ping/status never show stale data.

    A user asking "進捗どう？" implies OpenClaw is responding right now, so a fresh
    ts is truthful at this moment. Falls back to the last written file if the
    refresh fails for any reason.
    """
    try:
        write_heartbeat(touch_command=True)
    except Exception:
        pass
    return read_health()


def cmd_ping(_args: argparse.Namespace) -> int:
    print(format_ping_line(_refresh_then_read()))
    print(watchdog_liveness_line())
    err_line = watchdog_error_line()
    if err_line:
        print(err_line)
    _opportunistic_watchdog_ensure()
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    print(format_status(_refresh_then_read()))
    print("")
    print(watchdog_liveness_line())
    err_line = watchdog_error_line()
    if err_line:
        print(err_line)
    _opportunistic_watchdog_ensure()
    return 0


# v30: stale-host detection across the whole fleet.
#
# Each host writes its own data/system_health/<host>.json (when running
# commands, when its watchdog ticks, or via ``./healthcheck write-heartbeat``).
# A primary host that's been silent for hours is a real outage signal — but
# until now there was no one-shot command to surface it. ``./healthcheck
# stale`` reads every JSON under data/system_health/ and reports rows whose
# ts is older than ``threshold``. Exit code 2 when stale rows exist so a
# launchd / cron caller can route to Slack.

STALE_HEARTBEAT_THRESHOLD_SEC = 600  # 10 minutes — twice the default watchdog tick


def collect_host_health(
    skills_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Read every per-host system_health/*.json under ``skills_root``.

    Returns a list of ``{host, age_sec, ts, openclaw_pid, active_runs, ...}``
    dicts sorted by oldest heartbeat first so the call site can present the
    stalest hosts at the top of a Slack message.
    """
    root = skills_root or SKILLS_ROOT
    out: list[dict[str, Any]] = []
    dir_path = system_health_dir(root)
    if not dir_path.is_dir():
        return out
    for entry in dir_path.iterdir():
        if not entry.is_file() or entry.suffix.lower() != ".json":
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        age = heartbeat_age_seconds(data)
        out.append({
            "host": str(data.get("host") or entry.stem),
            "age_sec": age,
            "ts": data.get("ts"),
            "openclaw_pid": data.get("openclaw_pid"),
            "active_runs": data.get("active_runs") or [],
            "last_command_at": data.get("last_command_at"),
            "open_needs_attention_count": data.get("open_needs_attention_count"),
            "file": str(entry),
        })
    out.sort(key=lambda r: (r["age_sec"] is None, r["age_sec"] or 0), reverse=True)
    return out


def find_stale_hosts(
    rows: list[dict[str, Any]],
    *,
    threshold_sec: int,
) -> list[dict[str, Any]]:
    """Filter ``rows`` (from :func:`collect_host_health`) to those whose
    heartbeat exceeds ``threshold_sec`` or is missing entirely. Pure helper
    so the report and the CLI exit-code share the same definition.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        age = row.get("age_sec")
        if age is None or age > threshold_sec:
            out.append(row)
    return out


def format_stale_report(
    rows: list[dict[str, Any]],
    stale: list[dict[str, Any]],
    *,
    threshold_sec: int,
) -> str:
    """Pretty-print the stale-host report. Always lists every host's state
    (not just the stale ones) so the operator can confirm that the OTHER
    hosts are healthy in one glance."""
    if not rows:
        return "稼働中ホストがまだ記録されていません。"
    lines = [
        f"ホスト稼働状況（threshold={threshold_sec}s, "
        f"stale {len(stale)}/{len(rows)}）:",
    ]
    for r in rows:
        age_label = _fmt_age(r.get("age_sec"))
        flag = "🔴" if r in stale else "🟢"
        active = len(r.get("active_runs") or [])
        lines.append(
            f"  {flag} {r['host']:<16} heartbeat {age_label}"
            f" · active_runs={active}"
        )
    if stale:
        lines.append("")
        lines.append("停止疑いのホスト:")
        for r in stale:
            lines.append(
                f"  - {r['host']}: 最終 ts={r.get('ts')} "
                f"(復旧: 当該機で ./healthcheck write-heartbeat か watchdog 再起動)"
            )
    return "\n".join(lines)


def cmd_stale(args: argparse.Namespace) -> int:
    threshold = int(getattr(args, "threshold_sec", STALE_HEARTBEAT_THRESHOLD_SEC))
    rows = collect_host_health()
    stale = find_stale_hosts(rows, threshold_sec=threshold)
    print(format_stale_report(rows, stale, threshold_sec=threshold))
    # v32 FX5 — this is the only cron-able healthcheck command, so a dead/
    # erroring watchdog must surface (and exit non-zero) here too.
    err_line = watchdog_error_line()
    if err_line:
        print(err_line)
    # Exit 2 (distinct from ``error``=1) when stale hosts exist so cron /
    # Slack glue can branch on it cleanly.
    return 2 if (stale or err_line) else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Doorman system health (v4 §15-B)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("write-heartbeat", help="Refresh data/system_health/<host>.json")
    sub.add_parser("touch-command", help="Update last_command_at (Slack message received)")
    sub.add_parser("ping", help="One-line alive summary")
    sub.add_parser("status", help="Ping + health JSON + recent events")
    stale_parser = sub.add_parser(
        "stale",
        help="List hosts whose heartbeat is older than the threshold",
    )
    stale_parser.add_argument(
        "--threshold-sec",
        type=int,
        default=STALE_HEARTBEAT_THRESHOLD_SEC,
        help=f"Age threshold in seconds (default: {STALE_HEARTBEAT_THRESHOLD_SEC})",
    )
    args = ap.parse_args()
    if args.cmd == "write-heartbeat":
        sys.exit(cmd_write_heartbeat(args))
    if args.cmd == "touch-command":
        sys.exit(cmd_touch_command(args))
    if args.cmd == "status":
        sys.exit(cmd_status(args))
    if args.cmd == "stale":
        sys.exit(cmd_stale(args))
    sys.exit(cmd_ping(args))


if __name__ == "__main__":
    main()
