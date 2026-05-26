#!/usr/bin/env python3
"""Print latest pipeline progress from data/current_task.jsonl and JSONL counts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _skill_dir(name: str) -> Path:
    return REPO / name


def _count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.open() if line.strip())


def _last_task_events(data_dir: Path, n: int = 5) -> list[dict]:
    path = data_dir / "current_task.jsonl"
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    out: list[dict] = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def print_status(skill_name: str) -> int:
    skill = _skill_dir(skill_name)
    if not skill.is_dir():
        print(f"unknown skill: {skill_name}", file=sys.stderr)
        return 2
    data = skill / "data"
    print(f"=== {skill_name} pipeline status ===")
    print(f"  leads.jsonl:    {_count(data / 'leads.jsonl')}")
    print(f"  enriched.jsonl: {_count(data / 'enriched.jsonl')}")
    print(f"  drafts.jsonl:   {_count(data / 'drafts.jsonl')}")
    events = _last_task_events(data)
    if not events:
        print("  current_task:   (no events — run with --heartbeat auto or research.py)")
        return 0
    print("  current_task (latest):")
    for ev in events:
        ts = ev.get("ts", "?")
        task = ev.get("task", "?")
        event = ev.get("event", "?")
        cur = ev.get("current", "")
        total = ev.get("total", "")
        msg = ev.get("message", "")
        prog = f" {cur}/{total}" if cur != "" and total != "" else ""
        print(f"    [{ts}] {task} {event}{prog}: {msg}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "skill",
        nargs="?",
        default="linkedin-outreach",
        help="Skill directory name (default: linkedin-outreach)",
    )
    args = ap.parse_args()
    sys.exit(print_status(args.skill))


if __name__ == "__main__":
    main()
