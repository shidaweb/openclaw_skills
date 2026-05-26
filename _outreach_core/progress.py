"""Long-running task progress log + optional Slack heartbeat."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from _outreach_core.config import heartbeat_interval_sec, load_merged_config, load_sender_brief

_log = logging.getLogger(__name__)


def current_task_path(data_dir: Path) -> Path:
    return data_dir / "current_task.jsonl"


def _append_event(data_dir: Path, event: dict[str, Any]) -> None:
    path = current_task_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {**event, "ts": datetime.utcnow().isoformat() + "Z"}
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


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
    ) -> None:
        self.skill_dir = skill_dir
        self.task = task
        self.total = total
        self.heartbeat_mode = heartbeat
        self.data_dir = data_dir or (skill_dir / "data")
        self._started_at: float | None = None
        self._current = 0
        self._last_action = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        try:
            cfg = load_merged_config(skill_dir)
        except FileNotFoundError:
            cfg = load_sender_brief()
        self._interval = heartbeat_interval_sec(cfg)

    def start(self, message: str = "") -> None:
        self._started_at = time.time()
        self._last_action = message or f"{self.task} started"
        _append_event(
            self.data_dir,
            {"event": "start", "task": self.task, "total": self.total, "message": self._last_action},
        )
        if self.heartbeat_mode == "slack":
            from _outreach_core.notify import post

            post(f"[{self.task}] 開始 (全 {self.total} 件)", level="info")
            self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._thread.start()

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

    def end(self, message: str = "") -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
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

            post(summary, level="info")

    def _heartbeat_loop(self) -> None:
        from _outreach_core.notify import post

        last_post = time.time()
        while not self._stop.wait(5.0):
            if not self._started_at:
                continue
            elapsed = time.time() - self._started_at
            if elapsed < self._interval:
                continue
            if time.time() - last_post < self._interval:
                continue
            mins = int(elapsed / 60)
            post(
                f"[{self.task}] {self._current}/{self.total} 件目 · 経過 {mins} 分 · {self._last_action}",
                level="info",
            )
            last_post = time.time()

    def __enter__(self) -> HeartbeatSession:
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.end()
