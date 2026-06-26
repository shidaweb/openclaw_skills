"""
Run-level stall detection + bounded auto-restart decisions (v6 §15-B).

The gateway watchdog (``helpers/watchdog.py``) recovers a dead/hung *gateway*.
This module recovers a dead/stalled *run* (a `run.py` campaign launched detached
by `run_job`). Two failure modes seen in production:

  1. **False stall kill** — a long Opus draft phase was silent on stdout for
     >600s and a supervising layer killed it mid-progress. (Primary fix: the
     stdout keepalive in ``progress.py``. This module is the safety net.)
  2. **Genuine stall / crash** — the run hangs or exits non-zero and nothing
     resumes it. The campaign is idempotent (already-sent excluded, stale lock
     auto-cleared), so the correct recovery is simply to relaunch the same
     command, bounded so we never restart-loop forever.

All decision logic here is **pure and unit-tested**. The actual process spawning
/ killing lives in ``helpers/run_job.py``, which calls these helpers.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Tunables (overridable via env for ops).
def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, str(default)))
        return v if v > 0 else default
    except (ValueError, TypeError):
        return default


STALL_SEC = _env_int("DOORMAN_RUN_STALL_SEC", 420)        # no activity this long → stalled
POLL_SEC = _env_int("DOORMAN_RUN_POLL_SEC", 30)           # supervisor poll cadence
MAX_RESTARTS = _env_int("DOORMAN_RUN_MAX_RESTARTS", 3)    # within the window
RESTART_WINDOW_MIN = _env_int("DOORMAN_RUN_RESTART_WINDOW_MIN", 30)

# Supervisor actions.
ACTION_CONTINUE = "continue"
ACTION_RESTART_STALLED = "restart_stalled"
ACTION_RESTART_CRASH = "restart_crash"
ACTION_SUCCEEDED = "succeeded"
ACTION_GIVE_UP_STALLED = "give_up_stalled"
ACTION_GIVE_UP_CRASH = "give_up_crash"

_RESTART_ACTIONS = {ACTION_RESTART_STALLED, ACTION_RESTART_CRASH}


# ---------------------------------------------------------------------------
# Stall detection
# ---------------------------------------------------------------------------

def is_stalled(activity_age_sec: float | None, stall_sec: int = STALL_SEC) -> bool:
    """True if the most recent activity is older than the stall threshold."""
    if activity_age_sec is None:
        return False  # unknown activity → don't kill (fail-safe)
    return activity_age_sec >= stall_sec


def should_abort_target(started_at: float | None, now: float | None, limit: int | float) -> bool:
    """True when one target has exceeded its per-target wall-clock budget.

    ``limit <= 0`` disables the guard. Equality counts as timed out so the
    boundary is deterministic for tests and operations.
    """
    try:
        lim = float(limit)
        if lim <= 0:
            return False
        start = float(started_at)
        current = float(now)
    except (TypeError, ValueError):
        return False
    return max(0.0, current - start) >= lim


def latest_activity_age_sec(paths: list[Path], now: float | None = None) -> float | None:
    """Age (seconds) of the most recently modified existing path among ``paths``.

    Used with the run's log file and system_health heartbeat file: whichever
    advanced most recently is the freshest sign of life. Returns None if none of
    the paths exist (treated as 'unknown' → not stalled)."""
    now = now if now is not None else _now_ts()
    newest: float | None = None
    for p in paths:
        try:
            m = Path(p).stat().st_mtime
        except (OSError, TypeError):
            continue
        if newest is None or m > newest:
            newest = m
    if newest is None:
        return None
    return max(0.0, now - newest)


# ---------------------------------------------------------------------------
# Restart accounting (mirrors helpers/watchdog.py)
# ---------------------------------------------------------------------------

def new_state() -> dict[str, Any]:
    return {"restart_attempts": []}


def recent_restart_count(state: dict[str, Any], *, now: datetime | None = None,
                         window_min: int = RESTART_WINDOW_MIN) -> int:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=window_min)
    count = 0
    for item in state.get("restart_attempts") or []:
        ts = _parse_ts(item.get("at"))
        if ts and ts >= cutoff:
            count += 1
    return count


def can_restart(state: dict[str, Any], *, now: datetime | None = None,
                max_restarts: int = MAX_RESTARTS, window_min: int = RESTART_WINDOW_MIN) -> bool:
    return recent_restart_count(state, now=now, window_min=window_min) < max_restarts


def record_restart(state: dict[str, Any], outcome: str, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    attempts = list(state.get("restart_attempts") or [])
    attempts.append({"at": now.isoformat(), "outcome": outcome})
    state["restart_attempts"] = attempts[-50:]
    return state


# ---------------------------------------------------------------------------
# Core decision
# ---------------------------------------------------------------------------

def decide(
    *,
    child_alive: bool,
    exit_code: int | None,
    activity_age_sec: float | None,
    state: dict[str, Any],
    now: datetime | None = None,
    stall_sec: int = STALL_SEC,
    max_restarts: int = MAX_RESTARTS,
    window_min: int = RESTART_WINDOW_MIN,
) -> str:
    """Pure supervisor decision for one poll tick.

    Returns one of the ACTION_* constants.
    """
    if child_alive:
        if is_stalled(activity_age_sec, stall_sec):
            if can_restart(state, now=now, max_restarts=max_restarts, window_min=window_min):
                return ACTION_RESTART_STALLED
            return ACTION_GIVE_UP_STALLED
        return ACTION_CONTINUE
    # Child has exited.
    if exit_code == 0:
        return ACTION_SUCCEEDED
    if can_restart(state, now=now, max_restarts=max_restarts, window_min=window_min):
        return ACTION_RESTART_CRASH
    return ACTION_GIVE_UP_CRASH


def is_restart(action: str) -> bool:
    return action in _RESTART_ACTIONS


def build_give_up_problem(
    action: str,
    *,
    exit_code: int | None,
    activity_age_sec: float | None,
    recent_outcomes: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Build the Slack problem payload for terminal supervisor give-up."""
    if action not in {ACTION_GIVE_UP_STALLED, ACTION_GIVE_UP_CRASH}:
        return None
    counts: dict[str, int] = {}
    for row in recent_outcomes or []:
        if not isinstance(row, dict):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        outcome = row.get("outcome") or payload.get("outcome")
        if outcome:
            counts[str(outcome)] = counts.get(str(outcome), 0) + 1
    if action == ACTION_GIVE_UP_STALLED:
        root = f"run stalled after {int(activity_age_sec or 0)}s without activity"
        kind = "run_give_up_stalled"
        next_action = "ログ末尾と直近 outcome を確認し、詰まった対象を needs_attention から切り分けて再実行してください。"
    else:
        root = f"run crashed with exit={exit_code}"
        kind = "run_give_up_crash"
        next_action = "ログ末尾と例外を確認し、修正後に同じコマンドを再実行してください。"
    return {
        "kind": kind,
        "target": {
            "target_id": "run_supervisor",
            "name": "Doorman run supervisor",
        },
        "detail": {
            "root_cause": root,
            "exit_code": exit_code,
            "activity_age_sec": activity_age_sec,
            "recent_outcomes": counts,
            "next_action": next_action,
            "level": "error",
        },
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "run_supervisor.json"


def load_state(data_dir: Path) -> dict[str, Any]:
    path = state_path(data_dir)
    if not path.exists():
        return new_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("restart_attempts", [])
            return data
    except (ValueError, OSError):
        pass
    return new_state()


def save_state(data_dir: Path, state: dict[str, Any]) -> None:
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _parse_ts(val: Any) -> datetime | None:
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
