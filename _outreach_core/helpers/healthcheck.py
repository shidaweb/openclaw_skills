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


def format_ping_line(health: dict[str, Any] | None) -> str:
    age = heartbeat_age_seconds(health)
    if health is None:
        return "⚠️ no heartbeat file yet — run: python3 -m _outreach_core.helpers.healthcheck write-heartbeat"
    age_s = f"{age}s ago" if age is not None else "unknown"
    runs = health.get("active_runs") or []
    run_bits: list[str] = []
    for r in runs[:3]:
        skill = (r.get("skill") or "?").replace("-outreach", "")
        run_bits.append(
            f"{skill} {r.get('stage', '?')} {r.get('current', 0)}/{r.get('total', 0)}"
        )
    runs_txt = ", ".join(run_bits) if run_bits else "none"
    na = int(health.get("open_needs_attention_count") or 0)
    last_ev = _latest_event_kind()
    ev_txt = f" / last event: {last_ev}" if last_ev else ""
    return (
        f"✅ alive. heartbeat {age_s} / active runs: {len(runs)} ({runs_txt}) / "
        f"open needs_attention: {na}{ev_txt}"
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
                    best_label = f"{ev.get('kind', '?')} ({tid})"
            except (OSError, json.JSONDecodeError):
                continue
    return best_label


def format_status(health: dict[str, Any] | None) -> str:
    lines = [format_ping_line(health), ""]
    if health:
        lines.append("## system_health")
        lines.append(json.dumps(health, ensure_ascii=False, indent=2))
    lines.append("")
    lines.append("## recent events (last 5)")
    for row in _recent_events(5):
        lines.append(f"- {row}")
    return "\n".join(lines)


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
                            f"{ev.get('ts')} {ev.get('kind')} "
                            f"brief={brief_dir.name} target={ev.get('target_id', '-')}"
                        )
                        collected.append((ts, label))
            except (OSError, json.JSONDecodeError):
                continue
    collected.sort(key=lambda x: x[0], reverse=True)
    return [label for _, label in collected[:limit]] or ["(none)"]


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
        write_heartbeat()
    except Exception:
        pass
    return read_health()


def cmd_ping(_args: argparse.Namespace) -> int:
    print(format_ping_line(_refresh_then_read()))
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    print(format_status(_refresh_then_read()))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Doorman system health (v4 §15-B)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("write-heartbeat", help="Refresh data/system_health/<host>.json")
    sub.add_parser("touch-command", help="Update last_command_at (Slack message received)")
    sub.add_parser("ping", help="One-line alive summary")
    sub.add_parser("status", help="Ping + health JSON + recent events")
    args = ap.parse_args()
    if args.cmd == "write-heartbeat":
        sys.exit(cmd_write_heartbeat(args))
    if args.cmd == "touch-command":
        sys.exit(cmd_touch_command(args))
    if args.cmd == "status":
        sys.exit(cmd_status(args))
    sys.exit(cmd_ping(args))


if __name__ == "__main__":
    main()
