"""Per-brief active run lock (v4 §14-H)."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


class ActiveRunError(RuntimeError):
    """Another run is already in progress for this brief."""


def lock_path(data_dir: Path) -> Path:
    return data_dir / "active_run.lock"


def read_lock(data_dir: Path) -> dict[str, Any] | None:
    path = lock_path(data_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_lock(data_dir: Path, payload: dict[str, Any]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path(data_dir).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def remove_lock(data_dir: Path) -> None:
    lock_path(data_dir).unlink(missing_ok=True)


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def is_lock_alive(lock: dict[str, Any] | None) -> bool:
    if not lock:
        return False
    pid = lock.get("pid")
    if not isinstance(pid, int):
        return False
    return is_process_alive(pid)


def acquire_lock(
    data_dir: Path,
    *,
    run_id: str,
    stage: str,
    total_targets: int,
    skill: str,
    brief_id: str,
    slack_channel_id: str | None = None,
    slack_thread_ts: str | None = None,
) -> dict[str, Any]:
    """Create lock or raise ActiveRunError if another live run exists."""
    existing = read_lock(data_dir)
    if existing and is_lock_alive(existing):
        rid = existing.get("run_id", "?")
        pid = existing.get("pid", "?")
        raise ActiveRunError(
            f"進行中の run があります (run_id={rid}, pid={pid}). "
            "完了を待つか、プロセスを停止してから再実行してください。"
        )
    if existing and not is_lock_alive(existing):
        print(
            f"[active_run] stale lock removed (run_id={existing.get('run_id')}, "
            f"pid={existing.get('pid')})"
        )
        remove_lock(data_dir)

    payload: dict[str, Any] = {
        "run_id": run_id,
        "pid": os.getpid(),
        "started_at": datetime.utcnow().isoformat() + "Z",
        "stage": stage,
        "total_targets": total_targets,
        "current_target_idx": 0,
        "skill": skill,
        "brief_id": brief_id,
        "slack_channel_id": slack_channel_id or "",
        "slack_thread_ts": slack_thread_ts or "",
    }
    write_lock(data_dir, payload)
    return payload


def update_lock(
    data_dir: Path,
    *,
    stage: str | None = None,
    current_target_idx: int | None = None,
) -> None:
    lock = read_lock(data_dir)
    if not lock:
        return
    if stage is not None:
        lock["stage"] = stage
    if current_target_idx is not None:
        lock["current_target_idx"] = current_target_idx
    write_lock(data_dir, lock)


@contextmanager
def campaign_run_lock(
    data_dir: Path,
    *,
    run_id: str,
    brief_id: str,
    skill: str,
    total_targets: int,
    slack_channel_id: str | None = None,
    slack_thread_ts: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Acquire active_run.lock for campaign; always removed on exit."""
    payload = acquire_lock(
        data_dir,
        run_id=run_id,
        stage="campaign",
        total_targets=total_targets,
        skill=skill,
        brief_id=brief_id,
        slack_channel_id=slack_channel_id,
        slack_thread_ts=slack_thread_ts,
    )
    try:
        yield payload
    finally:
        remove_lock(data_dir)
