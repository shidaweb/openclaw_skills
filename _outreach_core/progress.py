"""Long-running task progress log + optional Slack heartbeat."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from _outreach_core.config import heartbeat_interval_sec, load_merged_config, load_runtime_config
from _outreach_core.notify import webhook_configured

_log = logging.getLogger(__name__)

# Tasks that may auto-enable Slack heartbeat when webhook + enabled_for allow it.
_ALL_TASKS = frozenset(
    {
        "research",
        "campaign",
        "fetch-leads",
        "fetch-from-csv",
        "enrich",
        "draft",
        "send",
    }
)


def resolve_heartbeat_mode(explicit: str | None, *, task: str) -> str | None:
    """
    Resolve heartbeat for a stage.

    explicit: None or \"auto\" → use brief yaml (webhook + heartbeat.enabled_for).
    \"slack\" → force on (notify no-ops if webhook empty).
    \"off\" → force off.
    """
    if explicit == "off":
        return None
    if explicit == "slack":
        return "slack"
    if not webhook_configured():
        return None
    brief = load_runtime_config()
    hb = brief.get("heartbeat") or {}
    enabled = hb.get("enabled_for")
    if enabled is not None:
        allowed = {str(x) for x in enabled}
        if not (allowed & {"all", "*"} or task in allowed):
            return None
    elif task not in _ALL_TASKS:
        return None
    return "slack"


def current_task_path(data_dir: Path) -> Path:
    return data_dir / "current_task.jsonl"


def _append_event(data_dir: Path, event: dict[str, Any]) -> None:
    path = current_task_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {**event, "ts": datetime.utcnow().isoformat() + "Z"}
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# Always-on stdout keepalive interval. A long LLM phase (e.g. Opus drafting 30
# companies sequentially) can be silent on stdout for many minutes, which made
# supervising layers (subagent harness, run_job) kill the run as "stalled" even
# though it was making progress. Emitting a heartbeat line well under any such
# timeout keeps the run visibly alive. Override with DOORMAN_KEEPALIVE_SEC.
def _keepalive_interval() -> int:
    try:
        v = int(os.environ.get("DOORMAN_KEEPALIVE_SEC", "60"))
        return v if v > 0 else 60
    except (ValueError, TypeError):
        return 60


def compose_heartbeat_message(
    task: str,
    current: int,
    total: int,
    elapsed_sec: float,
    last_action: str,
    progress_summary: str | None = None,
) -> str:
    """Pure heartbeat-line builder. When a run_progress summary is available
    (sent/skipped/needs_attention/ETA), it carries the richer status; otherwise
    fall back to the bare current/total + elapsed line."""
    if progress_summary:
        return f"[{task}] {progress_summary}"
    mins = int((elapsed_sec or 0) / 60)
    return f"[{task}] {current}/{total} 件目 · 経過 {mins} 分 · {last_action}"


class HeartbeatSession:
    """Context manager: progress events + optional 5-min Slack webhook pings."""

    def __init__(
        self,
        skill_dir: Path,
        task: str,
        total: int,
        *,
        heartbeat: str | None = None,
        data_dir: Path | None = None,
        brief_id: str | None = None,
        slack_thread_ts: str | None = None,
    ) -> None:
        self.skill_dir = skill_dir
        self.task = task
        self.total = total
        self.heartbeat_mode = heartbeat
        self.data_dir = data_dir or (skill_dir / "data")
        self._thread_ts = slack_thread_ts or os.environ.get("DOORMAN_SLACK_THREAD_TS", "").strip() or None
        self._started_at: float | None = None
        self._current = 0
        self._last_action = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._keepalive_thread: threading.Thread | None = None
        self._keepalive_interval = _keepalive_interval()
        try:
            cfg = load_merged_config(skill_dir, brief_id)
        except FileNotFoundError:
            cfg = load_runtime_config(brief_id)
        self._interval = heartbeat_interval_sec(cfg)

    def _sync_system_health(self) -> None:
        try:
            from _outreach_core.helpers.healthcheck import write_heartbeat

            write_heartbeat()
        except Exception:
            pass

    def start(self, message: str = "") -> None:
        self._started_at = time.time()
        self._last_action = message or f"{self.task} started"
        _append_event(
            self.data_dir,
            {"event": "start", "task": self.task, "total": self.total, "message": self._last_action},
        )
        self._sync_system_health()
        # Always-on stdout keepalive (independent of Slack mode). Guarantees the
        # process is never silent long enough to be mistaken for stalled.
        self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keepalive_thread.start()
        if self.heartbeat_mode == "slack":
            from _outreach_core.notify import post

            post(f"[{self.task}] 開始 (全 {self.total} 件)", level="info", thread_ts=self._thread_ts)
            self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._thread.start()

    def note(self, message: str) -> None:
        """Update last action + log without changing progress counter."""
        self._last_action = message
        _append_event(
            self.data_dir,
            {
                "event": "note",
                "task": self.task,
                "current": self._current,
                "total": self.total,
                "message": message,
            },
        )

    def tick(self, current: int, message: str = "") -> None:
        self._current = current
        self._last_action = message or f"completed item {current}/{self.total}"
        _append_event(
            self.data_dir,
            {
                "event": "tick",
                "task": self.task,
                "current": current,
                "total": self.total,
                "message": self._last_action,
            },
        )
        self._sync_system_health()

    def end(self, message: str = "") -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._keepalive_thread:
            self._keepalive_thread.join(timeout=2.0)
        elapsed_min = 0
        if self._started_at:
            elapsed_min = int((time.time() - self._started_at) / 60)
        summary = message or f"{self.task} 完了 {self._current}/{self.total} 件 ({elapsed_min} 分)"
        _append_event(
            self.data_dir,
            {
                "event": "end",
                "task": self.task,
                "current": self._current,
                "total": self.total,
                "message": summary,
            },
        )
        if self.heartbeat_mode == "slack":
            from _outreach_core.notify import post

            post(summary, level="info", thread_ts=None)
        self._sync_system_health()

    def _progress_summary_line(self) -> str | None:
        """The v22 run_progress summary for this run's data_dir, or None."""
        try:
            from _outreach_core import run_progress
            snap = run_progress.read(self.data_dir)
            if not snap:
                return None
            return run_progress.format_summary(snap)
        except Exception:  # noqa: BLE001
            return None

    def _refresh_from_log(self) -> None:
        """Pick up child-stage ticks written to current_task.jsonl by subprocesses."""
        path = current_task_path(self.data_dir)
        if not path.is_file():
            return
        try:
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if not lines:
                return
            ev = json.loads(lines[-1])
            if ev.get("task"):
                self.task = str(ev["task"])
            if ev.get("current") is not None:
                self._current = int(ev.get("current") or 0)
            if ev.get("total") is not None:
                self.total = int(ev.get("total") or self.total)
            if ev.get("message"):
                self._last_action = str(ev["message"])
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return

    def _heartbeat_loop(self) -> None:
        from _outreach_core.notify import post

        last_post = time.time()
        while not self._stop.wait(5.0):
            if not self._started_at:
                continue
            self._refresh_from_log()
            elapsed = time.time() - self._started_at
            if elapsed < self._interval:
                continue
            if time.time() - last_post < self._interval:
                continue
            post(
                compose_heartbeat_message(
                    self.task, self._current, self.total, elapsed,
                    self._last_action, self._progress_summary_line(),
                ),
                level="info",
                thread_ts=self._thread_ts,
            )
            try:
                from _outreach_core import events as ev

                if ev.get_context().data_dir:
                    ev.emit(
                        "heartbeat.tick",
                        stage=self.task,
                        payload={
                            "current": self._current,
                            "total": self.total,
                            "elapsed_sec": int(elapsed),
                        },
                    )
            except Exception:
                pass
            last_post = time.time()

    def _keepalive_loop(self) -> None:
        """Emit a stdout liveness line + refresh system_health every
        keepalive interval. Runs regardless of Slack mode. This is what keeps a
        long, legitimately-busy run from being killed as 'no output = stalled'."""
        while not self._stop.wait(self._keepalive_interval):
            if not self._started_at:
                continue
            self._refresh_from_log()
            elapsed = int(time.time() - self._started_at)
            try:
                print(
                    f"  [keepalive] {self.task} {self._current}/{self.total} · "
                    f"{elapsed}s · {self._last_action}",
                    flush=True,
                )
            except Exception:
                pass
            # Advance the run heartbeat file so the supervisor sees liveness.
            self._sync_system_health()

    def __enter__(self) -> HeartbeatSession:
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.end()
