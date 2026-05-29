#!/usr/bin/env python3
"""External watchdog for the OpenClaw gateway (v4 §15-C).

On this host the agent runtime is the launchd-managed ``ai.openclaw.gateway``
node process (``openclaw gateway``), NOT a "Cowork" app. launchd already has
``KeepAlive=true``, so plain process death is auto-recovered by the OS. The gap
launchd cannot see is a process that is alive but hung/unresponsive (Slack stops
being processed). This watchdog therefore probes responsiveness via
``openclaw health`` and only force-restarts (``launchctl kickstart -k``) after a
sustained failure streak, with rate limiting and Slack notifications.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.config import SKILLS_ROOT
from _outreach_core.helpers.healthcheck import (
    collect_active_runs,
    heartbeat_age_seconds,
    read_health,
)

GATEWAY_LABEL = "ai.openclaw.gateway"
HEALTH_TIMEOUT_SEC = 20
HEALTH_FAIL_RESTART_THRESHOLD = 2  # consecutive failed probes before restart (~2 min)
CHANNEL_FAIL_RESTART_THRESHOLD = 5  # consecutive ticks with a stuck channel before restart (~5 min)
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


def is_gateway_loaded() -> bool:
    """True when launchd has the gateway job registered (managing it)."""
    try:
        out = subprocess.run(
            ["launchctl", "list", GATEWAY_LABEL],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def is_gateway_healthy() -> bool:
    """True when ``openclaw health`` responds successfully within the timeout.

    A timeout or non-zero exit means the gateway is unresponsive (hung) even if
    the process technically still exists.
    """
    try:
        out = subprocess.run(
            ["openclaw", "health"],
            capture_output=True,
            text=True,
            timeout=HEALTH_TIMEOUT_SEC,
            check=False,
        )
        return out.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        # openclaw not on PATH for this launchd context — cannot probe; treat as
        # healthy so we never restart blindly without evidence.
        return True


def configured_but_down_channels() -> list[str]:
    """Return ids of channels that are configured but not running.

    This catches the failure where the gateway process is alive and ``openclaw
    health`` returns 0, yet a chat channel (e.g. Slack) is stuck disconnected —
    typically after a network outage that the channel's own reconnect logic never
    recovered from. Returns [] on any error so parse failures never trigger a
    restart without evidence.
    """
    try:
        out = subprocess.run(
            ["openclaw", "channels", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=HEALTH_TIMEOUT_SEC,
            check=False,
        )
        if out.returncode != 0:
            return []
        data = json.loads(out.stdout)
        channels = data.get("channels") or {}
        down: list[str] = []
        for cid, info in channels.items():
            if isinstance(info, dict) and info.get("configured") and not info.get("running"):
                down.append(str(cid))
        return down
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return []


def restart_gateway() -> bool:
    """Force launchd to kill and relaunch the gateway job."""
    try:
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{GATEWAY_LABEL}"],
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

    Responsiveness (``openclaw health``) is the authoritative signal: launchd's
    KeepAlive already revives a dead process, so the watchdog targets the harder
    case of an alive-but-hung gateway. To avoid flapping on transient blips, a
    force-restart only fires after HEALTH_FAIL_RESTART_THRESHOLD consecutive
    failed probes, and is rate limited. The watchdog never writes the heartbeat
    itself — that would mask staleness used by the secondary stuck-detection,
    whose freshness is owned by HeartbeatSession during active runs.
    """
    root = skills_root or SKILLS_ROOT
    try:
        state = read_state(root)

        if not is_gateway_loaded():
            # launchd is not managing the gateway at all — outside our restart
            # contract. Surface it once per interval and let the operator decide.
            now = datetime.now(timezone.utc)
            last = _parse_ts(str(state.get("last_unloaded_notify") or ""))
            if last is None or (now - last.astimezone(timezone.utc)).total_seconds() >= ABANDON_NOTIFY_INTERVAL_SEC:
                notify_slack(
                    f"🚨 launchd に {GATEWAY_LABEL} が登録されていません。"
                    "gateway サービスの再インストールが必要かもしれません。",
                    level="error",
                )
                state["last_unloaded_notify"] = _utc_now()
                save_state(state, root)
            append_log("gateway not loaded in launchd", root)
            return "stuck"

        healthy = is_gateway_healthy()
        if not healthy:
            streak = int(state.get("health_fail_streak", 0)) + 1
            state["health_fail_streak"] = streak
            if streak < HEALTH_FAIL_RESTART_THRESHOLD:
                save_state(state, root)
                append_log(f"gateway unhealthy streak={streak} (waiting)", root)
                return "stuck"
            if can_restart(state):
                notify_slack(
                    f"⚠️ gateway が応答しません (連続 {streak} 回)。強制再起動します。",
                    level="error",
                )
                restart_gateway()
                record_restart(state, "kickstart")
                state["health_fail_streak"] = 0
                save_state(state, root)
                append_log(f"restarted gateway (unhealthy streak={streak})", root)
                return "restarted"
            notify_slack(
                "🚨 gateway 強制再起動を 10 分以内に 3 回試行しましたが応答しません。手動確認が必要です。",
                level="error",
            )
            record_restart(state, "abandoned")
            save_state(state, root)
            append_log("abandoned (gateway unhealthy)", root)
            return "abandoned"

        # Gateway responsive: clear the health failure streak.
        changed = False
        if state.get("health_fail_streak"):
            state["health_fail_streak"] = 0
            changed = True

        # A configured channel (e.g. Slack) can be stuck disconnected while the
        # gateway itself is healthy — this is what caused "生きてる?" to go
        # unanswered after a network outage. Force-restart the gateway (which
        # cleanly reconnects channels) after a sustained streak, rate limited.
        down = configured_but_down_channels()
        if down:
            streak = int(state.get("channel_fail_streak", 0)) + 1
            state["channel_fail_streak"] = streak
            if streak < CHANNEL_FAIL_RESTART_THRESHOLD:
                save_state(state, root)
                append_log(f"channel down {down} streak={streak} (waiting)", root)
                return "stuck"
            if can_restart(state):
                notify_slack(
                    f"⚠️ チャンネル {down} が gateway 生存中に切断したままです。"
                    "gateway を再起動して再接続します。",
                    level="error",
                )
                restart_gateway()
                record_restart(state, "channel-restart")
                state["channel_fail_streak"] = 0
                save_state(state, root)
                append_log(f"restarted gateway (channel down {down} streak={streak})", root)
                return "restarted"
            notify_slack(
                "🚨 チャンネル切断の復旧を 10 分以内に 3 回試みましたが復旧しません。手動確認が必要です。",
                level="error",
            )
            record_restart(state, "abandoned")
            save_state(state, root)
            append_log(f"abandoned (channel down {down})", root)
            return "abandoned"
        if state.get("channel_fail_streak"):
            state["channel_fail_streak"] = 0
            changed = True

        health = read_health(root)
        age = heartbeat_age_seconds(health)
        active = collect_active_runs(root)
        if active and age is not None and age >= STALE_HEARTBEAT_SEC:
            now = datetime.now(timezone.utc)
            last = _parse_ts(str(state.get("last_stuck_notify") or ""))
            if last is None or (now - last.astimezone(timezone.utc)).total_seconds() >= ABANDON_NOTIFY_INTERVAL_SEC:
                notify_slack(
                    f"⚠️ gateway は応答していますが、実行中 run の heartbeat が {age}s 前から"
                    "更新されていません。タスクが詰まっている可能性があります。",
                    level="warn",
                )
                state["last_stuck_notify"] = _utc_now()
                changed = True
            if changed:
                save_state(state, root)
            append_log(f"stuck heartbeat {age}s active_runs={len(active)}", root)
            return "stuck"

        if state.get("last_stuck_notify"):
            state["last_stuck_notify"] = None
            changed = True
        if changed:
            save_state(state, root)
        append_log("tick ok", root)
        return "ok"
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
