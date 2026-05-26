#!/usr/bin/env python3
"""
research.py — single-shot pipeline for 10-lead (or any N) LinkedIn outreach research.

Runs:
  1. fetch-leads from the saved Sales Nav search
  2. enrich each profile
  3. draft personalized InMails (Sonnet, cached)
  4. preview all drafts (TTY only; otherwise --no-send)

Progress: data/current_task.jsonl always; Slack every ~5 min when
brief yaml webhook (optional) or OpenClaw Slack bot + session channel; heartbeat.enabled_for allows research/*.

Usage:
  cd ~/.openclaw/skills/linkedin-outreach
  .venv/bin/python research.py                    # default: 10 leads, saved search
  .venv/bin/python research.py --limit 5
  .venv/bin/python research.py --clean            # wipe previous data first
  .venv/bin/python research.py --search-url "..." # override saved search URL
  .venv/bin/python research.py --heartbeat off    # disable Slack even if webhook set
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
REPO_ROOT = SKILL_DIR.parent
DATA_DIR = SKILL_DIR / "data"
PY = sys.executable

sys.path.insert(0, str(REPO_ROOT))

from _outreach_core.infer import browser_headless_preference, oc_browser_start  # noqa: E402
from _outreach_core.notify import post, webhook_configured  # noqa: E402
from _outreach_core.progress import HeartbeatSession, resolve_heartbeat_mode  # noqa: E402

DEFAULT_SEARCH_URL = (
    "https://www.linkedin.com/sales/search/people"
    "?savedSearchId=1980655852"
    "&sessionId=9CXOL16KRZK5LOrn%2F8LREQ%3D%3D"
)


def banner(title: str) -> None:
    bar = "=" * 70
    print(f"\n{bar}\n{title}\n{bar}")


def run_stage(*args: str, heartbeat: str = "auto") -> None:
    cmd = [PY, str(SKILL_DIR / "run.py"), "--heartbeat", heartbeat, *args]
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


def _heartbeat_cli_flag(explicit: str) -> str:
    return explicit if explicit in ("slack", "off") else "auto"


def _start_background_watch() -> subprocess.Popen[str] | None:
    """Dedicated watcher: posts every ~5 min while subprocess stages run."""
    script = SKILL_DIR / "heartbeat_watch.py"
    if not script.is_file():
        return None
    return subprocess.Popen(
        [PY, str(script)],
        cwd=str(SKILL_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={**os.environ},
    )


def _stop_background_watch(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=10, help="Number of leads to research (default: 10)")
    ap.add_argument("--search-url", default=DEFAULT_SEARCH_URL, help="Sales Nav saved search URL")
    ap.add_argument("--clean", action="store_true", help="Wipe data/*.jsonl before running")
    ap.add_argument("--skip-preview", action="store_true", help="Skip the final preview step")
    ap.add_argument(
        "--heartbeat",
        choices=["auto", "slack", "off"],
        default="auto",
        help="Slack progress pings (auto=webhook + brief heartbeat.enabled_for)",
    )
    args = ap.parse_args()

    hb_explicit = None if args.heartbeat == "auto" else args.heartbeat
    hb_mode = resolve_heartbeat_mode(hb_explicit, task="research")
    hb_flag = _heartbeat_cli_flag(args.heartbeat)
    watch_proc: subprocess.Popen[str] | None = None
    session_hb = None if hb_mode == "slack" else hb_mode  # watcher handles Slack pings
    run_hb = HeartbeatSession(
        SKILL_DIR, "research", args.limit, heartbeat=session_hb, data_dir=DATA_DIR
    )

    if args.clean:
        for f in ("leads.jsonl", "enriched.jsonl", "drafts.jsonl"):
            (DATA_DIR / f).unlink(missing_ok=True)
        print("[research] cleared data/leads.jsonl, data/enriched.jsonl, data/drafts.jsonl")

    if browser_headless_preference():
        print("[research] starting OpenClaw browser headless (no window)")
        if not oc_browser_start(headless=True):
            print("[research] browser start failed — is the gateway running?", file=sys.stderr)
    elif browser_headless_preference() is False:
        oc_browser_start(headless=False)

    if hb_mode == "slack" and webhook_configured():
        watch_proc = _start_background_watch()
        post(
            f"[research] 開始 (limit={args.limit}) · 約5分ごとに進捗をこのチャンネルに投稿します",
            level="info",
        )
    elif hb_mode == "slack":
        print("[research] heartbeat=slack but Slack not configured — log only (set webhook or OpenClaw bot)")

    run_hb.start(f"リサーチ開始 (limit={args.limit})")

    try:
        banner(f"[1/4] FETCH-LEADS  (limit={args.limit})")
        run_hb.note("fetch-leads 実行中")
        run_stage("fetch-leads", "--search-url", args.search_url, "--limit", str(args.limit), heartbeat=hb_flag)
        leads_n = count_lines(DATA_DIR / "leads.jsonl")
        print(f"\n→ {leads_n} leads in data/leads.jsonl")
        run_hb.tick(min(leads_n, args.limit), f"fetch-leads 完了 ({leads_n} 件)")
        if leads_n == 0:
            print("\n[research] no leads fetched — aborting. Check sample_search.txt for parser issues.")
            run_hb.end("fetch-leads で 0 件 — 中断")
            sys.exit(2)

        banner("[2/4] ENRICH  (per-profile snapshot)")
        run_hb.note("enrich 実行中（プロフィールごとにブラウザ）")
        run_stage("enrich", heartbeat=hb_flag)
        enriched_n = count_lines(DATA_DIR / "enriched.jsonl")
        print(f"\n→ {enriched_n} enriched profiles in data/enriched.jsonl")
        run_hb.tick(enriched_n, f"enrich 完了 ({enriched_n} 件)")

        banner("[3/4] DRAFT  (Sonnet, cached system prompt)")
        run_hb.note("draft 実行中（Sonnet）")
        run_stage("draft", heartbeat=hb_flag)
        drafts_n = count_lines(DATA_DIR / "drafts.jsonl")
        print(f"\n→ {drafts_n} drafts in data/drafts.jsonl")
        run_hb.tick(drafts_n, f"draft 完了 ({drafts_n} 件)")

        if not args.skip_preview:
            banner("[4/4] PREVIEW")
            run_hb.note("preview 表示")
            preview_args = ["preview"]
            if not sys.stdin.isatty():
                preview_args.append("--no-send")
                print("[research] non-interactive: preview with --no-send (no stdin prompt)")
            run_stage(*preview_args, heartbeat=hb_flag)

        sendable_n, skipped_n, sendable_names, skipped = count_sendable_skipped(DATA_DIR / "drafts.jsonl")
        banner("SUMMARY")
        print(f"  Fetched:   {leads_n}")
        print(f"  Enriched:  {enriched_n}")
        print(f"  Sendable:  {sendable_n}")
        print(f"  Skipped:   {skipped_n}")
        if leads_n:
            rate = sendable_n / leads_n * 100
            print(
                f"  Send rate: {rate:.0f}%  "
                f"({'good' if 30 <= rate <= 80 else 'tune filters' if rate < 30 else 'check Sonnet is being honest'})"
            )
        if sendable_names:
            print("\n  Sendable leads:")
            for n in sendable_names:
                print(f"    ✓ {n}")
        if skipped:
            print("\n  Skipped (reason):")
            for name, reason in skipped:
                print(f"    ✗ {name}: {reason}")

        summary = f"research 完了: {sendable_n} sendable / {skipped_n} skip (of {leads_n} fetched)"
        run_hb.end(summary)
        if hb_mode == "slack":
            post(summary, level="info")

        print("\nNext steps:")
        print("  • Open Sales Nav for each sendable lead, manually paste the body, send.")
        print("  • Or run `python run.py send --ids 1,3,5` for the v2 semi-auto flow (when ready).")
        print("  • Status log: tail -f data/current_task.jsonl")
        print("  • Quick status: .venv/bin/python pipeline_status.py")
    except subprocess.CalledProcessError as e:
        run_hb.end(f"research 失敗 (exit {e.returncode})")
        if hb_mode == "slack":
            post(f"research パイプライン失敗 (exit {e.returncode})", level="error")
        raise
    finally:
        _stop_background_watch(watch_proc)


if __name__ == "__main__":
    main()
