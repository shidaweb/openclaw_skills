#!/usr/bin/env python3
"""
Poll data/current_task.jsonl and post Doorman progress to Slack every ~5 minutes.

Use when a long pipeline runs in subprocesses (research.py stages) or when the
OpenClaw agent orchestrates multiple shell commands manually.

  cd ~/.openclaw/skills/linkedin-outreach
  nohup ../.venv/bin/python heartbeat_watch.py >> /tmp/doorman-hb.log 2>&1 &
  # ... run pipeline ...
  kill %1

Or one-shot status (agent cron / user asks 進捗):
  python heartbeat_watch.py linkedin-outreach --once
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from _outreach_core.config import heartbeat_interval_sec, load_sender_brief  # noqa: E402
from _outreach_core.notify import post, webhook_configured  # noqa: E402
from _outreach_core.progress import current_task_path  # noqa: E402


def _skill_dir(name: str) -> Path:
    return REPO / name


def _read_last_event(data_dir: Path) -> dict | None:
    path = current_task_path(data_dir)
    if not path.is_file():
        return None
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return None


def _format_status(ev: dict) -> str:
    task = ev.get("task", "?")
    cur = ev.get("current", "")
    tot = ev.get("total", "")
    msg = ev.get("message", "")
    prog = ""
    if cur != "" and tot != "":
        prog = f" {cur}/{tot}"
    return f"[Doorman/{task}]{prog} · {msg}"


def _should_stop(ev: dict | None, idle_sec: float, last_change: float) -> bool:
    if ev and ev.get("event") == "end" and ev.get("task") in (
        "research",
        "campaign",
        "send",
        "fetch-leads",
        "enrich",
        "draft",
    ):
        return True
    if idle_sec > 0 and (time.time() - last_change) > idle_sec:
        return True
    return False


def run_watch(
    skill_name: str,
    *,
    interval_sec: int,
    poll_sec: int = 30,
    once: bool = False,
    idle_timeout_sec: int = 7200,
) -> int:
    if not webhook_configured():
        print(
            "[heartbeat_watch] Slack not configured (OpenClaw botToken or webhook). "
            "No posts sent.",
            file=sys.stderr,
        )
        return 2

    data_dir = _skill_dir(skill_name) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    last_post = 0.0
    last_change = time.time()
    last_body = ""

    while True:
        ev = _read_last_event(data_dir)
        if ev:
            body = _format_status(ev)
            if body != last_body:
                last_change = time.time()
                last_body = body
            now = time.time()
            if once or (now - last_post >= interval_sec and body):
                if post(body, level="info"):
                    print(f"[heartbeat_watch] posted: {body[:120]}")
                else:
                    print("[heartbeat_watch] post failed", file=sys.stderr)
                last_post = now

            if once:
                return 0
            if _should_stop(ev, idle_timeout_sec, last_change):
                print(f"[heartbeat_watch] stop ({ev.get('event')} / {ev.get('task')})")
                return 0

        if once:
            if not ev:
                post(
                    f"[Doorman/{skill_name}] 進捗ログなし "
                    "(パイプライン未開始 or --heartbeat off)",
                    level="info",
                )
            return 0

        time.sleep(poll_sec)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "skill",
        nargs="?",
        default="linkedin-outreach",
        help="Skill directory name (default: linkedin-outreach)",
    )
    ap.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Seconds between Slack posts (default: sender_brief heartbeat.interval_sec)",
    )
    ap.add_argument(
        "--once",
        action="store_true",
        help="Post current status once and exit (for agent 進捗どう？)",
    )
    ap.add_argument(
        "--poll",
        type=int,
        default=30,
        help="How often to read current_task.jsonl (default: 30)",
    )
    args = ap.parse_args()
    interval = args.interval or heartbeat_interval_sec(load_sender_brief())
    sys.exit(
        run_watch(
            args.skill,
            interval_sec=interval,
            poll_sec=args.poll,
            once=args.once,
        )
    )


if __name__ == "__main__":
    main()
