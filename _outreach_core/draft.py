"""Generic Personalize (draft) stage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from _outreach_core import prompt as prompt_mod


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
) -> None:
    leads = [json.loads(l) for l in input_path.open()]
    print(
        f"[draft] {len(leads)} leads to draft"
        + (" (with refine 2nd pass)" if refine_fn else "")
    )
    model_cfg = config.get("model", {}) or {}
    model = model_cfg.get("name", default_model)
    default_max = int(model_cfg.get("max_chars", 1800))
    extended_max = int(model_cfg.get("max_chars_extended", default_max))

    system_block = prompt_mod.build_system_block(config, prompts_dir)
    drafts: list[dict[str, Any]] = []

    for i, lead in enumerate(leads, 1):
        max_chars = int(lead.get("char_limit") or default_max)
        max_chars = min(max_chars, extended_max)
        label = lead.get("name") or lead.get("id", "?")

        user_block = build_user_block(lead, max_chars)
        full_prompt = system_block + user_block
        print(f"[draft] ({i}/{len(leads)}) {label} (pass 1) ...")
        response = oc_infer_fn(full_prompt, model)
        draft = prompt_mod.extract_first_json(response or "")
        if not draft or "subject" not in draft or "body" not in draft:
            print(f"[draft] parse failed for {label}: {(response or '')[:200]}")
            continue

        if refine_fn and draft.get("subject") != "SKIP":
            print(f"[draft] ({i}/{len(leads)}) {label} (pass 2 refine) ...")
            refined = refine_fn(lead, draft, config, max_chars)
            if refined and refined.get("body"):
                critique = (refined.get("critique") or "")[:80]
                suffix = "..." if len(refined.get("critique", "")) > 80 else ""
                print(f"          → critique: {critique}{suffix}")
                draft = {
                    "subject": refined.get("subject"),
                    "body": refined["body"],
                    "_pass1_subject": draft.get("subject"),
                    "_pass1_body": draft.get("body"),
                    "_critique": refined.get("critique"),
                }
            else:
                print("          → refine failed, keeping pass-1 draft")

        row: dict[str, Any] = {
            **lead,
            "draft": draft,
            "_drafted_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        }
        if lead.get("char_limit") or default_max != extended_max:
            row["max_chars_used"] = max_chars
        drafts.append(row)

    with out_path.open("w") as f:
        for d in drafts:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"[draft] wrote {len(drafts)} drafts -> {out_path}")

    new_skips = [d for d in drafts if (d.get("draft") or {}).get("subject") == "SKIP"]
    append_skip_fn(new_skips)
