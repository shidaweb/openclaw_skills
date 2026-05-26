#!/usr/bin/env python3
"""Print cross-skill sent/skip ID sets as JSON for agent context."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import history


def dump_exclude_sets() -> dict[str, list[str]]:
    linkedin_dir = history.SKILLS_ROOT / "linkedin-outreach" / "data"
    jp_dir = history.SKILLS_ROOT / "jp-form-outreach" / "data"
    linkedin_ids = sorted(
        history.load_sent_set(linkedin_dir) | history.load_skip_set(linkedin_dir)
    )
    jp_ids = sorted(history.load_sent_set(jp_dir) | history.load_skip_set(jp_dir))
    canonical: set[str] = set()
    for data_dir in (linkedin_dir, jp_dir):
        for fname in ("sent_history.jsonl", "skip_history.jsonl"):
            path = data_dir / fname
            if not path.exists():
                continue
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    cid = entry.get("canonical_id")
                    if cid:
                        canonical.add(str(cid))
                    name = entry.get("company") or entry.get("name")
                    if name:
                        canonical.add(history.canonical_company_id(str(name)))
                except Exception:
                    continue
    return {
        "linkedin": linkedin_ids,
        "jp_form": jp_ids,
        "canonical": sorted(canonical),
    }


def main() -> int:
    print(json.dumps(dump_exclude_sets(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
