#!/usr/bin/env python3
"""
research.py — single-shot pipeline for 10-lead (or any N) LinkedIn outreach research.

Runs:
  1. fetch-leads from the saved Sales Nav search
  2. enrich each profile
  3. draft personalized InMails (Sonnet, cached)
  4. preview all drafts

And prints a final summary: fetched / enriched / sendable / skipped.

Usage:
  cd ~/.openclaw/skills/linkedin-outreach
  .venv/bin/python research.py                    # default: 10 leads, saved search
  .venv/bin/python research.py --limit 5
  .venv/bin/python research.py --clean            # wipe previous data first
  .venv/bin/python research.py --search-url "..." # override saved search URL
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
DATA_DIR = SKILL_DIR / "data"
PY = sys.executable          # use the same python that's running this script

# Default saved-search URL.
# Consumer Goods + Retail · headcount 11-200 · saved 2026-05-09.
# Edit if you save a new search; or pass --search-url at runtime.
DEFAULT_SEARCH_URL = (
    "https://www.linkedin.com/sales/search/people"
    "?savedSearchId=1980655852"
    "&sessionId=9CXOL16KRZK5LOrn%2F8LREQ%3D%3D"
)


def banner(title: str) -> None:
    bar = "=" * 70
    print(f"\n{bar}\n{title}\n{bar}")


def run_stage(*args: str) -> None:
    """Invoke run.py <args> via the same venv python as this script."""
    cmd = [PY, str(SKILL_DIR / "run.py"), *args]
    subprocess.run(cmd, check=True)


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.open() if line.strip())


def count_sendable_skipped(path: Path) -> tuple[int, int, list[str], list[tuple[str, str]]]:
    """Read drafts.jsonl and count sendable vs skipped, returning details."""
    sendable_names: list[str] = []
    skipped: list[tuple[str, str]] = []  # (name, reason)
    if not path.exists():
        return 0, 0, sendable_names, skipped
    for line in path.open():
        if not line.strip():
            continue
        d = json.loads(line)
        name = d.get("name") or d.get("id", "?")
        draft = d.get("draft") or {}
        if draft.get("subject") == "SKIP":
            reason = (draft.get("body") or "").replace("INSUFFICIENT_DATA: ", "")
            skipped.append((name, reason[:120] + ("…" if len(reason) > 120 else "")))
        else:
            sendable_names.append(name)
    return len(sendable_names), len(skipped), sendable_names, skipped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=10, help="Number of leads to research (default: 10)")
    ap.add_argument("--search-url", default=DEFAULT_SEARCH_URL, help="Sales Nav saved search URL")
    ap.add_argument("--clean", action="store_true", help="Wipe data/*.jsonl before running")
    ap.add_argument("--skip-preview", action="store_true", help="Skip the final preview step")
    args = ap.parse_args()

    if args.clean:
        for f in ("leads.jsonl", "enriched.jsonl", "drafts.jsonl"):
            (DATA_DIR / f).unlink(missing_ok=True)
        print("[research] cleared data/leads.jsonl, data/enriched.jsonl, data/drafts.jsonl")

    # Stage 1
    banner(f"[1/4] FETCH-LEADS  (limit={args.limit})")
    run_stage("fetch-leads", "--search-url", args.search_url, "--limit", str(args.limit))
    leads_n = count_lines(DATA_DIR / "leads.jsonl")
    print(f"\n→ {leads_n} leads in data/leads.jsonl")
    if leads_n == 0:
        print("\n[research] no leads fetched — aborting. Check sample_search.txt for parser issues.")
        sys.exit(2)

    # Stage 2
    banner("[2/4] ENRICH  (per-profile snapshot)")
    run_stage("enrich")
    enriched_n = count_lines(DATA_DIR / "enriched.jsonl")
    print(f"\n→ {enriched_n} enriched profiles in data/enriched.jsonl")

    # Stage 3
    banner("[3/4] DRAFT  (Sonnet, cached system prompt)")
    run_stage("draft")
    drafts_n = count_lines(DATA_DIR / "drafts.jsonl")
    print(f"\n→ {drafts_n} drafts in data/drafts.jsonl")

    # Stage 4
    if not args.skip_preview:
        banner("[4/4] PREVIEW")
        run_stage("preview")

    # Summary
    sendable_n, skipped_n, sendable_names, skipped = count_sendable_skipped(DATA_DIR / "drafts.jsonl")
    banner("SUMMARY")
    print(f"  Fetched:   {leads_n}")
    print(f"  Enriched:  {enriched_n}")
    print(f"  Sendable:  {sendable_n}")
    print(f"  Skipped:   {skipped_n}")
    if leads_n:
        rate = sendable_n / leads_n * 100
        print(f"  Send rate: {rate:.0f}%  ({'good' if 30 <= rate <= 80 else 'tune filters' if rate < 30 else 'check Sonnet is being honest'})")
    if sendable_names:
        print(f"\n  Sendable leads:")
        for n in sendable_names:
            print(f"    ✓ {n}")
    if skipped:
        print(f"\n  Skipped (reason):")
        for name, reason in skipped:
            print(f"    ✗ {name}: {reason}")

    print("\nNext steps:")
    print("  • Open Sales Nav for each sendable lead, manually paste the body, send.")
    print("  • Or run `python run.py send --ids 1,3,5` for the v2 semi-auto flow (when ready).")


if __name__ == "__main__":
    main()
