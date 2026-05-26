#!/usr/bin/env python3
"""One-shot backfill of canonical_id on sent_history.jsonl and skip_history.jsonl."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import history

HISTORY_FILES = ("sent_history.jsonl", "skip_history.jsonl")


def backfill_file(path: Path) -> int:
    if not path.exists():
        return 0
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    updated = 0
    out_lines: list[str] = []
    for line in lines:
        entry = json.loads(line)
        if entry.get("canonical_id"):
            out_lines.append(line)
            continue
        name = entry.get("company") or entry.get("name") or ""
        cid = history.canonical_company_id(str(name))
        if cid:
            entry["canonical_id"] = cid
            updated += 1
        out_lines.append(json.dumps(entry, ensure_ascii=False))
    if updated:
        path.write_text("\n".join(out_lines) + ("\n" if out_lines else ""))
    return updated


def main() -> int:
    total = 0
    for skill_dir in history.SKILL_DIRS:
        data = skill_dir / "data"
        for fname in HISTORY_FILES:
            p = data / fname
            n = backfill_file(p)
            if n:
                print(f"  {p}: backfilled {n} entries")
            total += n
    print(f"[backfill] done, {total} entries updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
