#!/usr/bin/env python3
"""
research.py — single-shot pipeline for jp-form-outreach.

Sister to linkedin-outreach/research.py. Runs:
  1. bootstrap from targets.yaml (Pull)
  2. enrich each form URL (Enrich)
  3. draft personalized messages (Personalize)
  4. preview all drafts (Approve, display only)

And prints a final summary: pulled / enriched / sendable / skipped.

Usage:
  cd ~/.openclaw/skills/jp-form-outreach
  .venv/bin/python research.py                          # default: all pending targets
  .venv/bin/python research.py --clean                  # wipe previous data first
  .venv/bin/python research.py --skip-enrich            # bypass form structure capture
  .venv/bin/python research.py --include-sent           # re-research already-sent companies
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
DATA_DIR = SKILL_DIR / "data"
PY = sys.executable


def banner(title: str) -> None:
    bar = "=" * 70
    print(f"\n{bar}\n{title}\n{bar}")


def run_stage(*args: str) -> None:
    cmd = [PY, str(SKILL_DIR / "run.py"), *args]
    subprocess.run(cmd, check=True)


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.open() if line.strip())


def count_sendable_skipped(path: Path) -> tuple[int, int, list[str], list[tuple[str, str]]]:
    sendable_names: list[str] = []
    skipped: list[tuple[str, str]] = []
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
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--targets", default=str(SKILL_DIR / "targets.yaml"))
    ap.add_argument("--clean", action="store_true", help="Wipe data/*.jsonl before running")
    ap.add_argument("--skip-enrich", action="store_true",
                    help="Skip the enrich phase (use leads.jsonl as enriched.jsonl)")
    ap.add_argument("--include-sent", action="store_true",
                    help="Also include companies marked status: sent")
    ap.add_argument("--skip-preview", action="store_true",
                    help="Skip the final preview step")
    args = ap.parse_args()

    if args.clean:
        for f in ("leads.jsonl", "enriched.jsonl", "drafts.jsonl"):
            (DATA_DIR / f).unlink(missing_ok=True)
        print("[research] cleared data/leads.jsonl, data/enriched.jsonl, data/drafts.jsonl")

    # Stage 1: Pull
    banner("[1/4] PULL — bootstrap from targets.yaml")
    bootstrap_args = ["bootstrap", "--targets", args.targets]
    if args.include_sent:
        bootstrap_args.append("--include-sent")
    run_stage(*bootstrap_args)
    leads_n = count_lines(DATA_DIR / "leads.jsonl")
    print(f"\n→ {leads_n} targets in data/leads.jsonl")
    if leads_n == 0:
        print("\n[research] no targets pulled — aborting.")
        sys.exit(2)

    # Stage 2: Enrich
    if args.skip_enrich:
        banner("[2/4] ENRICH — SKIPPED (--skip-enrich)")
        import shutil
        shutil.copy(DATA_DIR / "leads.jsonl", DATA_DIR / "enriched.jsonl")
    else:
        banner("[2/4] ENRICH — form structure detection")
        run_stage("enrich")
    enriched_n = count_lines(DATA_DIR / "enriched.jsonl")
    print(f"\n→ {enriched_n} enriched targets in data/enriched.jsonl")

    # Stage 3: Personalize
    banner("[3/4] DRAFT — Sonnet, cached system prompt")
    run_stage("draft")
    drafts_n = count_lines(DATA_DIR / "drafts.jsonl")
    print(f"\n→ {drafts_n} drafts in data/drafts.jsonl")

    # Stage 4: Preview (display only)
    if not args.skip_preview:
        banner("[4/4] PREVIEW")
        run_stage("preview", "--no-send")

    # Summary
    sendable_n, skipped_n, sendable_names, skipped = count_sendable_skipped(DATA_DIR / "drafts.jsonl")
    banner("SUMMARY")
    print(f"  Pulled:    {leads_n}")
    print(f"  Enriched:  {enriched_n}")
    print(f"  Sendable:  {sendable_n}")
    print(f"  Skipped:   {skipped_n}")
    if leads_n:
        rate = sendable_n / leads_n * 100
        print(f"  Send rate: {rate:.0f}%  ({'good' if 30 <= rate <= 80 else 'tune targets.yaml hooks' if rate < 30 else 'check Sonnet honesty'})")
    if sendable_names:
        print(f"\n  Sendable:")
        for n in sendable_names:
            print(f"    ✓ {n}")
    if skipped:
        print(f"\n  Skipped (reason):")
        for name, reason in skipped:
            print(f"    ✗ {name}: {reason}")

    print("\nNext steps:")
    print("  • Review + send: python run.py preview")
    print("  • Or auto-send specific IDs: python run.py send --ids 1,3,5 --auto-send")
    print("  • Or fill-only (manual click): python run.py send --ids 1 --no-confirm")


if __name__ == "__main__":
    main()
