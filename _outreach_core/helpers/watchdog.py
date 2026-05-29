#!/usr/bin/env python3
"""External watchdog for OpenClaw / Cowork (v4 §15-C)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.config import SKILLS_ROOT
from _outreach_core.helpers.healthcheck import (
    heartbeat_age_seconds,
    read_health,
    write_heartbeat,
)

STALE_HEARTBEAT_SEC = 300  # 5 minutes
RESTART_WINDOW_MIN = 10
MAX_RESTARTS = 3
ABANDON_COOLDOWN_MIN = 30
ABANDON_NOTIFY_INTERVAL_SEC = 300


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(ts: str) -> datetime | None:
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def watchdog_state_path(skills_root: Path | None = None) -> Path:
    root = skills_root or SKILLS_ROOT
    return root / "data" / "watchdog.state.json"


def watchdog_log_path(skills_root: Path | None = None) -> Path:
    root = skills_root or SKILLS_ROOT
    return root / "data" / "watchdog.log"


def read_state(skills_root: Path | None = None) -> dict[str, Any]:
    path = watchdog_state_path(skills_root)
    if not path.is_file():
        return {"restart_attempts": [], "abandoned_until": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("restart_attempts", [])
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"restart_attempts": [], "abandoned_until": None}


def save_state(state: dict[str, Any], skills_root: Path | None = None) -> None:
    path = watchdog_state_path(skills_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_log(message: str, skills_root: Path | None = None) -> None:
    path = watchdog_log_path(skills_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{_utc_now()} {message}\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


def is_cowork_running() -> bool:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "Cowork"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return out.returncode == 0 and bool(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def relaunch_cowork() -> bool:
    try:
        subprocess.run(
            ["open", "-a", "Cowork"],
            capture_output=True,
            timeout=15,
            check=False,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def notify_slack(text: str, *, level: str = "warn") -> bool:
    try:
        from _outreach_core.notify import post

        post(text, level=level)
        return True
    except Exception:
        return False


def _recent_restart_count(state: dict[str, Any], *, within_minutes: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
    n = 0
    for item in state.get("restart_attempts") or []:
        ts = _parse_ts(str(item.get("ts", "")))
        if ts and ts.astimezone(timezone.utc) >= cutoff:
            n += 1
    return n


def can_restart(state: dict[str, Any]) -> bool:
    abandoned = state.get("abandoned_until")
    if abandoned:
        until = _parse_ts(str(abandoned))
        if until and datetime.now(timezone.utc) < until.astimezone(timezone.utc):
            return False
    return _recent_restart_count(state, within_minutes=RESTART_WINDOW_MIN) < MAX_RESTARTS


def record_restart(state: dict[str, Any], outcome: str) -> None:
    attempts = list(state.get("restart_attempts") or [])
    attempts.append({"ts": _utc_now(), "outcome": outcome})
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=RESTART_WINDOW_MIN)
    attempts = [
        a
        for a in attempts
        if (ts := _parse_ts(str(a.get("ts", "")))) and ts.astimezone(timezone.utc) >= cutoff
    ]
    state["restart_attempts"] = attempts
    if len(attempts) >= MAX_RESTARTS:
        state["abandoned_until"] = (
            datetime.now(timezone.utc) + timedelta(minutes=ABANDON_COOLDOWN_MIN)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")


def tick(skills_root: Path | None = None) -> str:
    """
    One watchdog check. Returns: ok | restarted | abandoned | stuck.
    Never raises to caller (launchd safety).
    """
    root = skills_root or SKILLS_ROOT
    try:
        write_heartbeat(root)
        health = read_health(root)
        age = heartbeat_age_seconds(health)
        state = read_state(root)

        if age is not None and age < STALE_HEARTBEAT_SEC:
            append_log("tick ok", root)
            return "ok"

        cowork_up = is_cowork_running()
        age_txt = f"{age}s" if age is not None else "missing"

        if not cowork_up:
            if can_restart(state):
                notify_slack("⚠️ OpenClaw (Cowork) 停止検知。再起動を試みます。", level="error")
                relaunch_cowork()
                record_restart(state, "relaunched")
                save_state(state, root)
                append_log(f"restarted cowork (heartbeat {age_txt})", root)
                return "restarted"
            notify_slack(
                "🚨 OpenClaw 再起動を 10 分以内に 3 回試行しましたが復旧しません。手動確認が必要です。",
                level="error",
            )
            record_restart(state, "abandoned")
            save_state(state, root)
            append_log(f"abandoned (heartbeat {age_txt})", root)
            return "abandoned"

        notify_slack(
            f"⚠️ Cowork は生存中ですが heartbeat が {age_txt} 前から停止しています。スレッド詰まり疑い。",
            level="warn",
        )
        append_log(f"stuck cowork alive heartbeat {age_txt}", root)
        return "stuck"
    except Exception as exc:
        append_log(f"tick error: {exc!s}", root)
        return "error"


def cmd_tick(args: argparse.Namespace) -> int:
    outcome = tick(Path(args.skills_root) if args.skills_root else None)
    print(outcome)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Doorman watchdog (v4 §15-C)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tick", help="Run one watchdog check")
    p.add_argument("--skills-root", default=None)
    args = ap.parse_args()
    if args.cmd == "tick":
        sys.exit(cmd_tick(args))
    ap.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
