#!/usr/bin/env python3
"""Emit enrich.research.completed after agent-led WebSearch (v4 §13-C / B-2)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import history
from _outreach_core import events as ev

SKILLS_ROOT = history.SKILLS_ROOT


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Record enrich-research completion in events.jsonl"
    )
    ap.add_argument("--skill", default="jp-form-outreach")
    ap.add_argument("--target-id", action="append", default=[], help="Repeatable")
    ap.add_argument("--signals-count", type=int, default=0)
    ap.add_argument("--hook-chars", type=int, default=0)
    ap.add_argument(
        "--from-jsonl",
        default=None,
        help="Read target ids from enriched/leads jsonl (uses direct_signals length)",
    )
    args = ap.parse_args()

    if args.skill == "jp-form-outreach":
        data_dir = SKILLS_ROOT / "jp-form-outreach" / "data"
    elif args.skill == "linkedin-outreach":
        data_dir = SKILLS_ROOT / "linkedin-outreach" / "data"
    else:
        raise SystemExit(f"unknown skill: {args.skill}")

    ev.configure(skill=args.skill, data_dir=data_dir)

    target_ids = list(args.target_id)
    if args.from_jsonl:
        path = Path(args.from_jsonl)
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            tid = row.get("id")
            if not tid:
                continue
            signals = row.get("direct_signals") or []
            n_sig = len(signals) if isinstance(signals, list) else 0
            hook = row.get("hook_context") or ""
            ev.emit(
                "enrich.research.completed",
                stage="enrich",
                target_id=str(tid),
                payload={
                    "signals_count": n_sig,
                    "hook_context_chars": len(str(hook)),
                },
            )
            print(f"  recorded {tid} signals={n_sig} hook={len(str(hook))} chars")
        return

    if not target_ids:
        print("Specify --target-id (repeatable) or --from-jsonl path", file=sys.stderr)
        sys.exit(2)

    for tid in target_ids:
        ev.emit(
            "enrich.research.completed",
            stage="enrich",
            target_id=tid,
            payload={
                "signals_count": args.signals_count,
                "hook_context_chars": args.hook_chars,
            },
        )
        print(f"  recorded {tid}")


if __name__ == "__main__":
    main()
