#!/usr/bin/env python3
"""Quality and send funnel reports from data/events.jsonl (v4 §13-E)."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import history
from _outreach_core.config import resolve_brief_id
from _outreach_core.events import load_events, parse_since, prune_data
from _outreach_core.paths import brief_data_dir

SKILLS_ROOT = history.SKILLS_ROOT


def _skill_data_dir(skill: str, brief_id: str | None = None) -> Path:
    bid = resolve_brief_id(brief_id)
    if skill == "jp-form-outreach":
        return brief_data_dir(SKILLS_ROOT / "jp-form-outreach", bid)
    if skill == "linkedin-outreach":
        return brief_data_dir(SKILLS_ROOT / "linkedin-outreach", bid)
    raise SystemExit(f"unknown skill: {skill}")


def _needs_path(skill: str, brief_id: str | None = None) -> Path:
    return _skill_data_dir(skill, brief_id) / "needs_attention.jsonl"


def cmd_draft_quality(args: argparse.Namespace) -> int:
    data_dir = _skill_data_dir(args.skill, getattr(args, "brief", None))
    since = parse_since(args.since)
    events = load_events(data_dir, since=since, skill=args.skill)
    emitted = [e for e in events if e.get("kind") == "draft.emitted"]
    skipped = [e for e in events if e.get("kind") == "draft.skipped"]
    over = [e for e in events if e.get("kind") == "draft.over_limit"]
    compressed = [e for e in events if e.get("kind") == "draft.compressed"]
    refined = [e for e in events if e.get("kind") == "refine.applied"]

    opener = Counter()
    for e in emitted:
        ot = (e.get("payload") or {}).get("opener_type")
        if ot:
            opener[f"型{ot}"] += 1

    lines = [
        f"# Draft Quality Report — since {args.since}",
        "",
        f"skill: {args.skill}",
        f"events (draft/refine): {len([e for e in events if str(e.get('kind','')).startswith(('draft.','refine.'))])}",
        "",
        "## outcomes",
        f"- emitted: {len(emitted)}",
        f"- skipped: {len(skipped)}",
        f"- over_limit detected: {len(over)}",
        f"- auto-compressed: {len(compressed)}",
        f"- refine applied: {len(refined)}",
        "",
    ]
    if opener:
        lines.append("## opener type distribution")
        for k, v in opener.most_common():
            lines.append(f"- {k}: {v}")
        lines.append("")

    if not emitted and not skipped:
        lines.append("_No draft events in range. Run campaign/draft with events configured._")

    out = "\n".join(lines)
    print(out)
    if args.json:
        print(json.dumps({"emitted": len(emitted), "skipped": len(skipped)}, indent=2))
    return 0


def _skip_reason_bucket(reason: str) -> str:
    """Group an auto-skip reason into a coarse bucket for the report.

    Reasons look like ``"self_score_below_threshold: ..."`` or
    ``"reCAPTCHA v2 visible (warmup insufficient)"`` — we key off a stable prefix
    so the distribution stays readable across runs."""
    r = (reason or "").strip()
    low = r.lower()
    if low.startswith("self_score"):
        return "self_score_below_threshold"
    if "recaptcha" in low or "captcha" in low:
        return "captcha_v2_visible"
    if "wrong_form" in low:
        return "wrong_form_type"
    if "first submit" in low:
        return "first_submit_not_found"
    if "confirm" in low and "submit" in low:
        return "confirm_submit_not_found"
    # fall back to the part before the first colon, else the whole string
    return (r.split(":", 1)[0] or "other")[:40] or "other"


def autonomous_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise autonomous-mode outcomes from an event stream (v5 §12).

    Pure function (no I/O) so it can be unit-tested directly. Returns ``None``
    for ``active`` when no autonomous events are present, so callers can skip the
    section entirely for supervised briefs.
    """
    scored = [e for e in events if e.get("kind") == "send.self_scored"]
    auto_skipped = [e for e in events if e.get("kind") == "send.auto_skipped"]
    awaiting = [e for e in events if e.get("kind") == "campaign.awaiting_upfront_approval"]

    if not (scored or auto_skipped or awaiting):
        return {"active": False}

    sent = sum(1 for e in scored if (e.get("payload") or {}).get("send"))
    gated = sum(1 for e in scored if not (e.get("payload") or {}).get("send"))
    errored = sum(1 for e in scored if (e.get("payload") or {}).get("errored"))
    raw_scores = [
        (e.get("payload") or {}).get("score")
        for e in scored
        if isinstance((e.get("payload") or {}).get("score"), (int, float))
    ]
    avg_score = round(sum(raw_scores) / len(raw_scores), 3) if raw_scores else None

    reasons: Counter[str] = Counter()
    for e in auto_skipped:
        reasons[_skip_reason_bucket((e.get("payload") or {}).get("reason", ""))] += 1

    return {
        "active": True,
        "self_scored": len(scored),
        "self_scored_sent": sent,
        "self_scored_gated": gated,
        "self_scored_errored": errored,
        "avg_score": avg_score,
        "auto_skipped": len(auto_skipped),
        "skip_reasons": dict(reasons.most_common()),
        "awaiting_upfront_approval": len(awaiting),
    }


def cmd_send_funnel(args: argparse.Namespace) -> int:
    data_dir = _skill_data_dir(args.skill, getattr(args, "brief", None))
    since = parse_since(args.since)
    events = load_events(data_dir, since=since, skill=args.skill)
    kinds = Counter(e.get("kind") for e in events if str(e.get("kind", "")).startswith("send."))

    verify = [e for e in events if e.get("kind") == "send.verify.completed"]
    ok = sum(1 for e in verify if (e.get("payload") or {}).get("status") == "ok")
    uncertain = sum(1 for e in verify if (e.get("payload") or {}).get("status") == "uncertain")
    na = sum(1 for e in verify if (e.get("payload") or {}).get("status") == "needs_attention")
    dynamic = sum(1 for e in events if e.get("kind") == "send.fill.dynamic_required")

    lines = [
        f"# Send Funnel — since {args.since}",
        "",
        f"skill: {args.skill}",
        "",
    ]
    for kind in sorted(kinds):
        lines.append(f"- {kind}: {kinds[kind]}")
    lines.extend(
        [
            "",
            "## verify.completed",
            f"- ok: {ok}",
            f"- uncertain: {uncertain}",
            f"- needs_attention: {na}",
            f"- fill.dynamic_required: {dynamic}",
            "",
        ]
    )

    auto = autonomous_summary(events)
    if auto.get("active"):
        lines.append("## autonomous (v5)")
        if auto["awaiting_upfront_approval"]:
            lines.append(
                f"- ⏸ awaiting upfront approval: {auto['awaiting_upfront_approval']} "
                f"(approve-autonomy で送信開始)"
            )
        avg = auto["avg_score"]
        errored_n = auto["self_scored_errored"]
        errored_frag = f" / errored {errored_n}" if errored_n else ""
        lines.append(
            f"- self-scored: {auto['self_scored']} "
            f"(send {auto['self_scored_sent']} / gated {auto['self_scored_gated']}{errored_frag})"
            + (f" · avg {avg}" if avg is not None else "")
        )
        lines.append(f"- auto-skipped: {auto['auto_skipped']}")
        for reason, n in auto["skip_reasons"].items():
            lines.append(f"    · {reason}: {n}")
        lines.append("")

    if not kinds and not auto.get("active"):
        lines.append("_No send events in range._")
    print("\n".join(lines))
    return 0


def cmd_needs_attention(args: argparse.Namespace) -> int:
    path = _needs_path(args.skill, getattr(args, "brief", None))
    if not path.is_file():
        print(f"# needs_attention Report\n\nNo file: {path}")
        return 0
    open_rows: list[dict[str, Any]] = []
    closed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("status") == "open":
            open_rows.append(row)
        elif row.get("status") == "closed":
            closed += 1
    print(f"# needs_attention Report — {args.skill}\n")
    print(f"open: {len(open_rows)} / closed: {closed}\n")
    if open_rows:
        print("## open")
        for r in open_rows[:20]:
            print(
                f"- {r.get('target_id')} / {r.get('name')} / "
                f"{(r.get('reason') or '')[:80]}"
            )
    else:
        print("_No open entries._")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    data_dir = _skill_data_dir(args.skill, getattr(args, "brief", None))
    traces_root = data_dir / "traces"
    if not traces_root.is_dir():
        print(f"No traces dir: {traces_root}")
        return 0

    candidates: list[Path] = []
    if args.run_id:
        p = traces_root / args.run_id / args.target_id
        if p.is_dir():
            candidates.append(p)
    else:
        for run_dir in sorted(traces_root.iterdir(), reverse=True):
            if not run_dir.is_dir():
                continue
            p = run_dir / args.target_id
            if p.is_dir():
                candidates.append(p)
                break

    if not candidates:
        print(f"No trace for target_id={args.target_id}")
        return 0

    td = candidates[0]
    print(f"# inspect {args.target_id}\n\ntrace_dir: {td}\n")
    for fp in sorted(td.iterdir()):
        print(f"## {fp.name}")
        try:
            text = fp.read_text(encoding="utf-8")
            preview = "\n".join(text.splitlines()[:20])
            print(preview)
            if len(text.splitlines()) > 20:
                print(f"... ({len(text.splitlines())} lines total)")
        except OSError as exc:
            print(f"(read error: {exc})")
        print()
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    data_dir = _skill_data_dir(args.skill, getattr(args, "brief", None))
    stats = prune_data(data_dir, keep_days=args.keep, dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"# prune {args.skill} (keep {args.keep}d)\n")
    print(f"{prefix}events kept: {stats['events_kept']}")
    print(f"{prefix}events removed: {stats['events_removed']}")
    print(f"{prefix}trace run dirs removed: {stats['trace_dirs_removed']}")
    return 0


def cmd_improvements(args: argparse.Namespace) -> int:
    """Merged brief for Slack '今週の改善ポイント'."""
    since = args.since
    print(f"# Improvement brief — since {since}\n")
    print("## Draft quality\n")
    cmd_draft_quality(argparse.Namespace(since=since, skill=args.skill, json=False))
    print("\n## Send funnel\n")
    cmd_send_funnel(argparse.Namespace(since=since, skill=args.skill))
    print("\n## needs_attention\n")
    cmd_needs_attention(argparse.Namespace(skill=args.skill))
    return 0


def _add_brief_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--brief", default=None, help="Brief id (default: briefs/_active.txt)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Doorman events report (v4 §13)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("draft-quality")
    _add_brief_arg(p)
    p.add_argument("--since", default="7d")
    p.add_argument("--skill", default="jp-form-outreach")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("send-funnel")
    _add_brief_arg(p)
    p.add_argument("--since", default="7d")
    p.add_argument("--skill", default="jp-form-outreach")

    p = sub.add_parser("needs-attention")
    _add_brief_arg(p)
    p.add_argument("--skill", default="jp-form-outreach")

    p = sub.add_parser("inspect")
    _add_brief_arg(p)
    p.add_argument("--target-id", required=True)
    p.add_argument("--run-id", default=None)
    p.add_argument("--skill", default="jp-form-outreach")

    p = sub.add_parser("prune", help="Delete events/traces older than --keep days")
    _add_brief_arg(p)
    p.add_argument("--keep", type=int, default=90)
    p.add_argument("--skill", default="jp-form-outreach")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("improvements", help="draft-quality + send-funnel + needs-attention")
    _add_brief_arg(p)
    p.add_argument("--since", default="7d")
    p.add_argument("--skill", default="jp-form-outreach")

    args = ap.parse_args()
    if args.cmd == "draft-quality":
        sys.exit(cmd_draft_quality(args))
    if args.cmd == "send-funnel":
        sys.exit(cmd_send_funnel(args))
    if args.cmd == "needs-attention":
        sys.exit(cmd_needs_attention(args))
    if args.cmd == "inspect":
        sys.exit(cmd_inspect(args))
    if args.cmd == "prune":
        sys.exit(cmd_prune(args))
    if args.cmd == "improvements":
        sys.exit(cmd_improvements(args))
    ap.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
