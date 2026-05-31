"""Generic Personalize (draft) stage."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from _outreach_core import prompt as prompt_mod


def resolve_max_chars(
    lead: dict[str, Any],
    config: dict[str, Any],
    *,
    default_max: int,
    extended_max: int,
) -> int:
    """textarea maxlength > target char_limit > config.model.max_chars > default."""
    best = 0
    ff = lead.get("form_fields") or {}
    for ta in ff.get("textareas") or []:
        if not isinstance(ta, dict):
            continue
        ml = ta.get("max_length")
        if ml is not None and int(ml) > 0:
            best = max(best, int(ml))
    if best > 0:
        return min(best, extended_max)
    if lead.get("char_limit"):
        return min(int(lead["char_limit"]), extended_max)
    cfg_max = (config.get("model") or {}).get("max_chars")
    if cfg_max:
        return min(int(cfg_max), extended_max)
    return min(default_max, extended_max)


def hard_truncate_draft(draft: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Keep calendar/CTA tail lines when truncating over-limit bodies."""
    body = draft.get("body") or ""
    if len(body) <= max_chars:
        return draft
    lines = body.splitlines()
    tail: list[str] = []
    head = lines
    cal_pat = re.compile(r"(カレンダー|calendar|tenbin\.link|https?://)", re.I)
    while head and cal_pat.search(head[-1]):
        tail.insert(0, head.pop())
    keep = max_chars
    for ln in tail:
        keep -= len(ln) + 1
    keep = max(keep, max_chars // 2)
    head_text = "\n".join(head)
    if len(head_text) > keep:
        head_text = head_text[: keep - 1].rstrip() + "…"
    new_body = head_text
    if tail:
        new_body = new_body + "\n" + "\n".join(tail)
    out = dict(draft)
    out["body"] = new_body[:max_chars]
    out["_truncated"] = True
    return out


def enforce_char_limit(
    lead: dict[str, Any],
    draft: dict[str, Any],
    config: dict[str, Any],
    max_chars: int,
    *,
    oc_infer_fn: Callable[[str, str], str | None],
    label: str = "?",
) -> dict[str, Any]:
    body = (draft.get("body") or "").strip()
    if draft.get("subject") == "SKIP" or len(body) <= max_chars:
        return draft

    model = (config.get("model") or {}).get("name", "")
    compress_prompt = (
        f"以下のドラフトは {len(body)} 字で上限 {max_chars} 字を超えています。\n"
        "構造（固有事実→自己紹介→CTA→締め+URL）を保ったまま圧縮してください。\n\n"
        f"## 原文\n{body}\n\n"
        f"## 出力（厳格 JSON のみ）\n"
        f'{{"subject": "{draft.get("subject", "")}", "body": "<= {max_chars} chars>"}}\n'
    )
    print(f"[draft] ({label}) char_limit over ({len(body)}>{max_chars}), compress pass ...")
    from _outreach_core import events as ev

    ev.emit(
        "draft.over_limit",
        stage="draft",
        payload={
            "actual": len(body),
            "limit": max_chars,
            "delta_pct": round((len(body) - max_chars) * 100 / max_chars, 1),
        },
    )
    response = oc_infer_fn(compress_prompt, model)
    refined = prompt_mod.extract_first_json(response or "")
    if refined and len((refined.get("body") or "")) <= max_chars:
        refined["_compressed_from_len"] = len(body)
        ev.emit(
            "draft.compressed",
            stage="draft",
            payload={"original_chars": len(body), "new_chars": len(refined.get("body") or "")},
        )
        return refined
    out = hard_truncate_draft(draft, max_chars)
    ev.emit(
        "draft.compressed",
        stage="draft",
        outcome="truncated",
        payload={
            "original_chars": len(body),
            "new_chars": len(out.get("body") or ""),
        },
    )
    return out


def resolve_refine_enabled(
    config: dict[str, Any],
    *,
    cli_refine: bool | None = None,
    cli_no_refine: bool = False,
) -> bool:
    """CLI --refine / --no-refine override config draft.refine_default (v4: default true)."""
    if cli_no_refine:
        return False
    if cli_refine is True:
        return True
    draft_cfg = config.get("draft") or {}
    return bool(draft_cfg.get("refine_default", True))


def stage_draft(
    input_path: Path,
    out_path: Path,
    config: dict[str, Any],
    *,
    prompts_dir: Path,
    build_user_block: Callable[[dict[str, Any], int], str],
    oc_infer_fn: Callable[[str, str], str | None],
    append_skip_fn: Callable[[list[dict[str, Any]]], None],
    default_model: str,
    refine_fn: Callable[[dict[str, Any], dict[str, Any], dict[str, Any], int], dict[str, Any] | None]
    | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    skill: str | None = None,
    data_dir: Path | None = None,
    run_id: str | None = None,
    sender: dict[str, Any] | None = None,
    limit: int | None = None,
) -> None:
    from _outreach_core import events as ev

    if skill and data_dir:
        ev.configure(skill=skill, data_dir=data_dir, run_id=run_id)
    sender_cfg = sender or (config.get("sender") or {})

    with input_path.open(encoding="utf-8") as f:
        leads = [json.loads(l) for l in f]
    if limit is not None and len(leads) > limit:
        print(f"[draft] --limit {limit} applied (all {len(leads)} leads)")
        leads = leads[:limit]
    print(
        f"[draft] {len(leads)} leads to draft"
        + (" (with refine 2nd pass)" if refine_fn else "")
    )
    model_cfg = config.get("model", {}) or {}
    model = model_cfg.get("name", default_model)
    default_max = int(model_cfg.get("max_chars", 1800))
    extended_max = int(model_cfg.get("max_chars_extended", default_max))

    system_block = prompt_mod.build_system_block(config, prompts_dir)
    draft_cfg = config.get("draft") or {}
    max_parse_attempts = max(1, int(draft_cfg.get("parse_retry_max_attempts", 2)))
    per_lead_soft_timeout_sec = float(draft_cfg.get("lead_soft_timeout_sec", 90))
    existing_ids = _load_existing_draft_ids(out_path)
    if existing_ids:
        print(f"[draft] resume mode: {len(existing_ids)} existing drafts already persisted")
    new_rows: list[dict[str, Any]] = []

    for i, lead in enumerate(leads, 1):
        lead_id = str(lead.get("id", ""))
        if lead_id and lead_id in existing_ids:
            print(f"[draft] ({i}/{len(leads)}) skip existing id={lead_id}")
            continue
        max_chars = resolve_max_chars(
            lead, config, default_max=default_max, extended_max=extended_max
        )
        label = lead.get("name") or lead.get("id", "?")
        target_id = str(lead.get("id") or label)
        trace = ev.trace_dir_for(target_id) if data_dir else None

        user_block = build_user_block(lead, max_chars)
        full_prompt = system_block + user_block
        print(f"[draft] ({i}/{len(leads)}) {label} (pass 1) ...")
        if on_progress:
            on_progress(i, len(leads), f"draft {label} (pass 1)")
        ev.emit(
            "draft.requested",
            stage="draft",
            target_id=target_id,
            payload={"model": model, "max_chars": max_chars},
            trace_dir=trace,
        )
        ev.dump_trace(
            trace,
            "draft_prompt.json",
            {"system": system_block, "user": user_block},
            sender=sender_cfg,
        )
        started = time.monotonic()
        draft: dict[str, Any] | None = None
        response = ""
        for attempt in range(1, max_parse_attempts + 1):
            if (time.monotonic() - started) > per_lead_soft_timeout_sec:
                break
            response = oc_infer_fn(full_prompt, model) or ""
            ev.dump_trace(
                trace,
                f"draft_response.attempt{attempt}.json",
                {"attempt": attempt, "raw": response[:200_000]},
            )
            candidate = prompt_mod.extract_first_json(response)
            if candidate and "subject" in candidate and "body" in candidate:
                draft = candidate
                break
            if attempt < max_parse_attempts:
                print(
                    f"[draft] ({label}) parse retry {attempt}/{max_parse_attempts} "
                    "(invalid JSON shape)"
                )

        if not draft or "subject" not in draft or "body" not in draft:
            print(f"[draft] parse failed for {label}: {(response or '')[:200]}")
            timed_out = (time.monotonic() - started) > per_lead_soft_timeout_sec
            ev.emit(
                "draft.skipped",
                stage="draft",
                target_id=target_id,
                outcome="parse_error",
                payload={
                    "reason": "parse_error",
                    "attempts": max_parse_attempts,
                    "timed_out": timed_out,
                },
                trace_dir=trace,
            )
            row = {
                **lead,
                "draft": {"subject": "SKIP", "body": "parse_error"},
                "_drafted_at": datetime.utcnow().isoformat() + "Z",
            }
            _append_draft_row(out_path, row)
            if lead_id:
                existing_ids.add(lead_id)
            new_rows.append(row)
            continue

        if draft.get("subject") == "SKIP":
            ev.emit(
                "draft.skipped",
                stage="draft",
                target_id=target_id,
                outcome="skip",
                payload={"reason": (draft.get("body") or "")[:200]},
                trace_dir=trace,
            )
        elif refine_fn and draft.get("subject") != "SKIP":
            print(f"[draft] ({i}/{len(leads)}) {label} (pass 2 refine) ...")
            refined = refine_fn(lead, draft, config, max_chars)
            if refined and refined.get("body"):
                critique = (refined.get("critique") or "")[:80]
                suffix = "..." if len(refined.get("critique", "")) > 80 else ""
                print(f"          → critique: {critique}{suffix}")
                ev.dump_trace(trace, "refine_response.json", refined, sender=sender_cfg)
                ev.emit(
                    "refine.applied",
                    stage="draft",
                    target_id=target_id,
                    payload={
                        "critique_summary": (refined.get("critique") or "")[:120],
                        "changed_opener": draft.get("body", "")[:80]
                        != refined.get("body", "")[:80],
                    },
                    trace_dir=trace,
                )
                draft = {
                    "subject": refined.get("subject"),
                    "body": refined["body"],
                    "_pass1_subject": draft.get("subject"),
                    "_pass1_body": draft.get("body"),
                    "_critique": refined.get("critique"),
                }
            else:
                print("          → refine failed, keeping pass-1 draft")

        if draft.get("subject") != "SKIP":
            draft = enforce_char_limit(
                lead, draft, config, max_chars, oc_infer_fn=oc_infer_fn, label=label
            )
            body = draft.get("body") or ""
            ev.emit(
                "draft.emitted",
                stage="draft",
                target_id=target_id,
                outcome="ok",
                payload={
                    "body_chars": len(body),
                    "subject": draft.get("subject"),
                    "opener_type": ev.guess_opener_type(body),
                    "self_intro_variant": ev.guess_self_intro_variant(body),
                },
                trace_dir=trace,
            )

        row: dict[str, Any] = {
            **lead,
            "draft": draft,
            "_drafted_at": datetime.utcnow().isoformat() + "Z",
        }
        if lead.get("char_limit") or max_chars != default_max:
            row["max_chars_used"] = max_chars
        _append_draft_row(out_path, row)
        if lead_id:
            existing_ids.add(lead_id)
        new_rows.append(row)

    print(f"[draft] wrote {len(new_rows)} new drafts -> {out_path}")

    new_skips = [d for d in new_rows if (d.get("draft") or {}).get("subject") == "SKIP"]
    append_skip_fn(new_skips)


def _load_existing_draft_ids(out_path: Path) -> set[str]:
    ids: set[str] = set()
    if not out_path.is_file():
        return ids
    try:
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = row.get("id")
            if rid is not None:
                ids.add(str(rid))
    except OSError:
        return ids
    return ids


def _append_draft_row(out_path: Path, row: dict[str, Any]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
