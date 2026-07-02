"""Generic Personalize (draft) stage."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from _outreach_core import content_guard
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
        # v31 §WS4a — cut at the last sentence boundary at or before ``keep``
        # so the body doesn't end mid-sentence right before the CTA (an
        # ungrammatical 「…ご提案が可能で…」 reads as broken Japanese). Fall
        # back to the raw char cut + … only when honoring the boundary would
        # drop more than half the budget.
        window = head_text[:keep]
        boundary = max(window.rfind(t) for t in "。．！？!?")
        if boundary >= keep // 2:
            head_text = window[: boundary + 1]
        else:
            head_text = window[: keep - 1].rstrip() + "…"
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


# --- v15 §L2: opening-sentence duplication guard -----------------------------
def normalize_opening(body: str | None) -> str:
    """First sentence of a draft, normalized for comparison (pure)."""
    text = (body or "").strip()
    if not text:
        return ""
    first_line = text.splitlines()[0]
    # First sentence = up to the first 。/．/! (Japanese copy rarely uses '.')
    m = re.split(r"[。．！!]", first_line, maxsplit=1)
    sent = m[0] if m else first_line
    # Strip whitespace and company-agnostic punctuation for a stable key
    return re.sub(r"[\s\u3000、,，・]", "", sent)


def opening_too_similar(
    body: str | None,
    recent_bodies: list[str] | None,
    *,
    min_common_prefix: int = 18,
) -> bool:
    """True when the draft's opening matches a recent send too closely (pure).

    Similar = identical normalized opening, OR a common prefix of at least
    ``min_common_prefix`` chars (catches template-y "御社の◯◯を拝見し…" reuse).
    """
    mine = normalize_opening(body)
    if not mine:
        return False
    for prev in recent_bodies or []:
        theirs = normalize_opening(prev)
        if not theirs:
            continue
        if mine == theirs:
            return True
        common = 0
        for a, b in zip(mine, theirs):
            if a != b:
                break
            common += 1
        if common >= min_common_prefix:
            return True
    return False


# --- v31 §WS4b: body-level duplication guard ---------------------------------
def _normalize_body_for_similarity(body: str | None) -> str:
    """Whole-body normalization for trigram comparison (pure)."""
    return re.sub(r"[\s　、。．，,！!？?・「」（）()]", "", (body or "").strip())


def _char_trigrams(text: str) -> set[str]:
    if len(text) < 3:
        return {text} if text else set()
    return {text[i:i + 3] for i in range(len(text) - 2)}


def body_too_similar(
    body: str | None,
    recent_bodies: list[str] | None,
    *,
    threshold: float = 0.62,
) -> bool:
    """True when the WHOLE body overlaps a recent send too much (pure).

    v31 §WS4b — ``opening_too_similar`` checks only the first sentence, so
    two drafts with distinct openers but identical middle/closing boilerplate
    passed; the template smell the guard exists to kill survived below line
    one. Char-trigram Jaccard over the normalized body catches that without
    penalizing legitimately-shared short phrases.
    """
    mine = _char_trigrams(_normalize_body_for_similarity(body))
    if not mine:
        return False
    for prev in recent_bodies or []:
        theirs = _char_trigrams(_normalize_body_for_similarity(prev))
        if not theirs:
            continue
        union = len(mine | theirs)
        if union and len(mine & theirs) / union >= threshold:
            return True
    return False


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
    recent_bodies: list[str] | None = None,
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

        # v15 §L2: opening-duplication guard — when the opener matches a recent
        # send too closely, force ONE extra refine pass so each company gets a
        # distinct opening (template smell kills reply rates). v31 §WS4b adds
        # the whole-body trigram guard: distinct openers with an identical
        # middle/closing boilerplate are the same template smell.
        if draft.get("subject") != "SKIP" and refine_fn:
            dup_reason = ""
            if opening_too_similar(draft.get("body"), recent_bodies):
                dup_reason = "opening"
            elif body_too_similar(draft.get("body"), recent_bodies):
                dup_reason = "body"
            if dup_reason:
                print(f"[draft] ({label}) {dup_reason} too similar to a recent send "
                      "— forcing refine")
                ev.emit(
                    "draft.opening_duplicate" if dup_reason == "opening"
                    else "draft.body_duplicate",
                    stage="draft",
                    target_id=target_id,
                    payload={"opening": normalize_opening(draft.get("body"))[:60]},
                    trace_dir=trace,
                )
                re_refined = refine_fn(lead, draft, config, max_chars)
                if re_refined and re_refined.get("body"):
                    draft = {
                        "subject": re_refined.get("subject") or draft.get("subject"),
                        "body": re_refined["body"],
                        "_dedup_refined": True,
                    }

        # v31 §WS4c — deterministic tone lint. A casual verb ending / NG word
        # in a Japanese B2B inquiry is a reply-rate killer no prompt fully
        # prevents; on a hit, force one refine pass naming the fragments.
        if draft.get("subject") != "SKIP" and refine_fn:
            violations = content_guard.find_tone_violations(draft.get("body"))
            if violations:
                print(f"[draft] ({label}) tone lint hit: "
                      f"{', '.join(violations[:4])} — forcing refine")
                ev.emit(
                    "draft.tone_lint",
                    stage="draft",
                    target_id=target_id,
                    payload={"violations": violations[:6]},
                    trace_dir=trace,
                )
                re_refined = refine_fn(lead, draft, config, max_chars)
                if re_refined and re_refined.get("body"):
                    if not content_guard.find_tone_violations(re_refined.get("body")):
                        draft = {
                            "subject": re_refined.get("subject") or draft.get("subject"),
                            "body": re_refined["body"],
                            "_tone_refined": True,
                        }
                    else:
                        # refine did not clean it up — keep the original and
                        # let the event flag it rather than looping.
                        print(f"[draft] ({label}) tone lint still dirty after refine")

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
