#!/usr/bin/env python3
"""Quality and send funnel reports from data/events.jsonl (v4 §13-E)."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
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


def _history_paths(skill: str, brief_id: str | None = None) -> tuple[Path, Path]:
    data = _skill_data_dir(skill, brief_id)
    return data / "sent_history.jsonl", data / "skip_history.jsonl"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def period_bounds(period: str, *, now: datetime | None = None) -> tuple[datetime | None, datetime | None]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if period == "all":
        return None, None
    if period == "this_week":
        # Monday 00:00 UTC
        start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) - timedelta(days=now.weekday())
        return start, now
    if period == "this_month":
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        return start, now
    if period == "last_month":
        this_month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        last_month_end = this_month_start
        last_day_prev = this_month_start - timedelta(days=1)
        last_month_start = datetime(last_day_prev.year, last_day_prev.month, 1, tzinfo=timezone.utc)
        return last_month_start, last_month_end
    raise ValueError(f"unknown period: {period}")


def _in_period(ts: datetime | None, start: datetime | None, end: datetime | None) -> bool:
    if ts is None:
        return False
    if start is not None and ts < start:
        return False
    if end is not None and ts >= end:
        return False
    return True


def _failure_reason_from_event(ev: dict[str, Any]) -> str:
    kind = str(ev.get("kind") or "")
    payload = ev.get("payload") or {}
    if kind == "send.verify.completed":
        status = str(payload.get("status") or "")
        if status == "ok":
            return ""
        return str(payload.get("reason") or status or "verify_not_ok")
    if kind == "send.first_button_missing":
        return "first submit button not found"
    if kind == "send.auto_skipped":
        return str(payload.get("reason") or "auto_skipped")
    if kind == "send.queued_for_resolver":
        return str(payload.get("reason_class") or "queued_for_resolver")
    return kind


def send_period_summary(
    sent_rows: list[dict[str, Any]],
    skip_rows: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    period: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate period send stats for Slack-readable business summaries."""
    start, end = period_bounds(period, now=now)

    sent_in = [r for r in sent_rows if _in_period(_parse_ts(r.get("sent_at")), start, end)]
    skip_in = [r for r in skip_rows if _in_period(_parse_ts(r.get("skipped_at")), start, end)]

    attempt_kinds = {"send.verify.completed", "send.first_button_missing", "send.auto_skipped", "send.queued_for_resolver"}
    attempt_events = []
    for e in events:
        ts = _parse_ts(e.get("ts"))
        if not _in_period(ts, start, end):
            continue
        if str(e.get("kind") or "") in attempt_kinds:
            attempt_events.append(e)

    success_by_id = {str(r.get("id")): r for r in sent_in if r.get("id") is not None}
    # map id -> best-known company display name
    name_by_id: dict[str, str] = {}
    for r in sent_in + skip_in:
        rid = r.get("id")
        if rid is None:
            continue
        nm = str(r.get("name") or r.get("company") or "").strip()
        if nm:
            name_by_id[str(rid)] = nm

    failure_reasons: Counter[str] = Counter()
    failed_companies: list[dict[str, Any]] = []
    attempt_ids = {str(e.get("target_id") or "") for e in attempt_events if e.get("target_id") is not None}
    for e in attempt_events:
        reason = _failure_reason_from_event(e).strip()
        if not reason:
            continue
        tid = str(e.get("target_id") or "")
        company = name_by_id.get(tid) or tid or "unknown"
        failure_reasons[reason[:120]] += 1
        failed_companies.append({"id": tid, "company": company, "reason": reason[:160]})

    # Include skip_history reasons too (for failures that never reached verify event).
    for r in skip_in:
        rid = str(r.get("id") or "")
        if rid and rid in attempt_ids:
            continue
        reason = str(r.get("reason") or "").strip()
        if not reason:
            continue
        failure_reasons[reason[:120]] += 1
        failed_companies.append(
            {
                "id": str(r.get("id") or ""),
                "company": str(r.get("name") or r.get("company") or r.get("id") or "unknown"),
                "reason": reason[:160],
            }
        )

    sent_companies = []
    for r in sent_in:
        sent_companies.append(
            {
                "id": str(r.get("id") or ""),
                "company": str(r.get("name") or r.get("company") or r.get("id") or "unknown"),
                "content": str(r.get("subject") or ""),
                "sent_at": str(r.get("sent_at") or ""),
            }
        )

    attempts = len(attempt_events) + sum(1 for r in skip_in if str(r.get("id") or "") not in attempt_ids)
    successes = len(sent_in)
    failures = max(0, attempts - successes)

    return {
        "period": period,
        "attempts": attempts,
        "successes": successes,
        "failures": failures,
        "sent_companies": sent_companies[:100],
        "failed_companies": failed_companies[:100],
        "failure_reasons": dict(failure_reasons.most_common(20)),
    }


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


def inquiry_type_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise v10 inquiry-type auto-selection quality/volume."""
    send_selected = [e for e in events if e.get("kind") == "send.inquiry_type"]
    enrich_selected = [e for e in events if e.get("kind") == "enrich.inquiry_type_selected"]
    no_b2b = [
        e for e in events
        if e.get("kind") == "enrich.form.screen_skipped"
        and str((e.get("payload") or {}).get("reason") or "") == "no_b2b_inquiry_type"
    ]
    conf = Counter()
    src = Counter()
    for e in send_selected:
        p = e.get("payload") or {}
        c = str(p.get("confidence") or "").strip().lower()
        s = str(p.get("src") or "").strip().lower()
        if c:
            conf[c] += 1
        if s:
            src[s] += 1
    for e in enrich_selected:
        p = e.get("payload") or {}
        for k, v in ((p.get("confidence_counts") or {}).items()):
            conf[str(k)] += int(v or 0)
        for k, v in ((p.get("src_counts") or {}).items()):
            src[str(k)] += int(v or 0)
    return {
        "selected_send": len(send_selected),
        "selected_enrich": len(enrich_selected),
        "no_b2b_inquiry_type": len(no_b2b),
        "confidence_counts": dict(conf.most_common()),
        "source_counts": dict(src.most_common()),
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

    iq = inquiry_type_summary(events)
    if iq["selected_send"] or iq["selected_enrich"] or iq["no_b2b_inquiry_type"]:
        lines.append("## inquiry type (v10)")
        lines.append(
            f"- selected: send={iq['selected_send']} / enrich={iq['selected_enrich']}"
        )
        lines.append(f"- no_b2b_inquiry_type: {iq['no_b2b_inquiry_type']}")
        if iq["confidence_counts"]:
            parts = ", ".join(f"{k}:{v}" for k, v in iq["confidence_counts"].items())
            lines.append(f"- confidence: {parts}")
        if iq["source_counts"]:
            parts = ", ".join(f"{k}:{v}" for k, v in iq["source_counts"].items())
            lines.append(f"- source: {parts}")
        lines.append("")

    if not kinds and not auto.get("active"):
        lines.append("_No send events in range._")
    print("\n".join(lines))
    return 0


def _write_send_summary_snapshot(data_dir: Path, payload: dict[str, Any]) -> None:
    path = data_dir / "send_summary_latest.json"
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _render_send_summary(summary: dict[str, Any], *, skill: str, brief: str | None) -> str:
    lines = [
        f"# Send Summary — {summary['period']}",
        "",
        f"skill: {skill}",
        f"brief: {brief or '(active)'}",
        "",
        "## counts",
        f"- attempts: {summary['attempts']}",
        f"- successes: {summary['successes']}",
        f"- failures: {summary['failures']}",
        "",
    ]
    lines.append("## successes (company / content)")
    if summary["sent_companies"]:
        for r in summary["sent_companies"][:20]:
            lines.append(f"- {r['company']} / {r['content']}")
    else:
        lines.append("_No successful sends in period._")
    lines.append("")

    lines.append("## failure reasons")
    if summary["failure_reasons"]:
        for reason, n in summary["failure_reasons"].items():
            lines.append(f"- {reason}: {n}")
    else:
        lines.append("_No failure reasons in period._")
    lines.append("")

    lines.append("## failures (company / reason)")
    if summary["failed_companies"]:
        for r in summary["failed_companies"][:20]:
            lines.append(f"- {r['company']} / {r['reason']}")
    else:
        lines.append("_No failures in period._")
    lines.append("")
    return "\n".join(lines)


def cmd_send_summary(args: argparse.Namespace) -> int:
    data_dir = _skill_data_dir(args.skill, getattr(args, "brief", None))
    brief = getattr(args, "brief", None)
    sent_path, skip_path = _history_paths(args.skill, brief)
    sent_rows = _read_jsonl(sent_path)
    skip_rows = _read_jsonl(skip_path)
    events = load_events(data_dir, since=None, skill=args.skill)

    periods = ["this_week", "this_month", "last_month", "all"] if args.all_periods else [args.period]
    out_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "skill": args.skill,
        "brief": resolve_brief_id(brief),
        "periods": {},
    }
    blocks: list[str] = []
    for p in periods:
        summary = send_period_summary(sent_rows, skip_rows, events, period=p)
        out_payload["periods"][p] = summary
        blocks.append(_render_send_summary(summary, skill=args.skill, brief=brief))
    print("\n\n".join(blocks))
    _write_send_summary_snapshot(data_dir, out_payload)
    if args.json:
        print(json.dumps(out_payload, ensure_ascii=False, indent=2))
    return 0


def research_quality_summary(
    events: list[dict[str, Any]],
    sent_rows: list[dict[str, Any]],
    *,
    since: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate research quality KPIs (v8 §R4)."""
    enrich_completed = 0
    non_contact = 0
    corrected = 0
    correction_attempted = 0
    by_kind: Counter[str] = Counter()
    success_sent = 0

    for e in events:
        kind = str(e.get("kind") or "")
        if kind == "enrich.form.completed":
            enrich_completed += 1
        elif kind == "enrich.form.skipped_non_contact":
            non_contact += 1
            p = e.get("payload") or {}
            by_kind[str(p.get("kind") or "unknown")] += 1
            if int(p.get("correction_attempts") or 0) > 0:
                correction_attempted += 1
        elif kind == "enrich.form.url_corrected":
            corrected += 1
            correction_attempted += 1

    for r in sent_rows:
        ts = _parse_ts(r.get("sent_at"))
        if since and (ts is None or ts < since.replace(tzinfo=timezone.utc)):
            continue
        success_sent += 1

    enrich_attempts = enrich_completed + non_contact
    form_url_valid_rate = (enrich_completed / enrich_attempts) if enrich_attempts else 0.0
    wrong_url_rate = (non_contact / enrich_attempts) if enrich_attempts else 0.0
    correction_success_rate = (corrected / correction_attempted) if correction_attempted else 0.0
    hit_rate = (success_sent / enrich_attempts) if enrich_attempts else 0.0
    return {
        "enrich_attempts": enrich_attempts,
        "contact_classified": enrich_completed,
        "non_contact": non_contact,
        "non_contact_by_kind": dict(by_kind.most_common()),
        "correction_attempted": correction_attempted,
        "correction_succeeded": corrected,
        "form_url_valid_rate": round(form_url_valid_rate, 4),
        "wrong_url_rate": round(wrong_url_rate, 4),
        "correction_success_rate": round(correction_success_rate, 4),
        "hit_rate": round(hit_rate, 4),
        "sent_successes": success_sent,
    }


def cmd_research_quality(args: argparse.Namespace) -> int:
    data_dir = _skill_data_dir(args.skill, getattr(args, "brief", None))
    since = parse_since(args.since)
    events = load_events(data_dir, since=since, skill=args.skill)
    sent_path, _skip_path = _history_paths(args.skill, getattr(args, "brief", None))
    sent_rows = _read_jsonl(sent_path)
    summary = research_quality_summary(events, sent_rows, since=since)
    lines = [
        f"# Research Quality — since {args.since}",
        "",
        f"skill: {args.skill}",
        "",
        "## KPIs",
        f"- enrich attempts: {summary['enrich_attempts']}",
        f"- form_url valid rate: {summary['form_url_valid_rate']:.2%}",
        f"- wrong-url rate: {summary['wrong_url_rate']:.2%}",
        f"- correction success rate: {summary['correction_success_rate']:.2%}",
        f"- hit rate (sent/new targets): {summary['hit_rate']:.2%}",
        f"- sent successes: {summary['sent_successes']}",
        "",
        "## non-contact categories",
    ]
    if summary["non_contact_by_kind"]:
        for k, v in summary["non_contact_by_kind"].items():
            lines.append(f"- {k}: {v}")
    else:
        lines.append("_No non-contact skips in range._")
    print("\n".join(lines))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
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

    p = sub.add_parser("research-quality", help="Research/list quality KPIs for form_url accuracy")
    _add_brief_arg(p)
    p.add_argument("--since", default="7d")
    p.add_argument("--skill", default="jp-form-outreach")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser(
        "send-summary",
        help="Period summary: attempts/success/failure + company/content + failure reasons",
    )
    _add_brief_arg(p)
    p.add_argument("--skill", default="jp-form-outreach")
    p.add_argument(
        "--period",
        choices=["this_week", "this_month", "last_month", "all"],
        default="this_month",
    )
    p.add_argument("--all-periods", action="store_true")
    p.add_argument("--json", action="store_true")

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
    if args.cmd == "research-quality":
        sys.exit(cmd_research_quality(args))
    if args.cmd == "send-summary":
        sys.exit(cmd_send_summary(args))
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
