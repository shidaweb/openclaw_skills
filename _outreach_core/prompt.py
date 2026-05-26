"""Cache-stable system prompt assembly and JSON extraction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


def resolve_prompts_dir(skill_dir: Path, config: dict[str, Any]) -> Path:
    """Skill default prompts, or brief-specific override from prompts_overrides."""
    overrides = config.get("prompts_overrides") or {}
    rel = overrides.get("jp_form_system_persona") or overrides.get("linkedin_system_persona")
    if rel:
        path = skill_dir / str(rel)
        if path.is_file():
            return path.parent
    return skill_dir / "prompts"


def build_system_block(config: dict[str, Any], prompts_dir: Path) -> str:
    """
    Build the stable, cacheable system block. Byte sequence must NOT change
    across leads in the same run (lead-specific data goes in the user block).
    """
    if yaml is None:
        raise RuntimeError("pyyaml required for build_system_block")
    persona_path = prompts_dir / "system_persona.md"
    examples_path = prompts_dir / "examples.md"
    if not persona_path.is_file():
        raise FileNotFoundError(persona_path)
    persona = persona_path.read_text(encoding="utf-8")
    examples = (
        examples_path.read_text(encoding="utf-8")
        if examples_path.is_file()
        else ""
    )
    config_str = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    return (
        "<system>\n"
        f"{persona}\n\n"
        "## Few-shot examples\n\n"
        f"{examples}\n\n"
        "## Your sender + pitch + persona configuration\n\n"
        "```yaml\n"
        f"{config_str}"
        "```\n"
        "</system>\n"
    )


def extract_first_json(text: str) -> dict[str, Any] | None:
    """Pull the first {...} block out of model output, tolerant of prose."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


def run_draft_loop(
    leads: list[dict[str, Any]],
    *,
    system_block: str,
    build_user_block: Callable[[dict[str, Any], int], str],
    oc_infer_fn: Callable[[str, str], str | None],
    model: str,
    max_chars: int,
    on_lead_done: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Generic per-lead draft generation loop."""
    from datetime import datetime

    drafts: list[dict[str, Any]] = []
    for i, lead in enumerate(leads, 1):
        prompt = system_block + build_user_block(lead, max_chars)
        label = lead.get("name") or lead.get("id", "?")
        print(f"[draft] ({i}/{len(leads)}) {label} ...")
        response = oc_infer_fn(prompt, model)
        draft = extract_first_json(response or "")
        if not draft or "subject" not in draft or "body" not in draft:
            print(f"[draft] parse failed for {label}: {(response or '')[:200]}")
            continue
        row = {**lead, "draft": draft, "_drafted_at": datetime.utcnow().isoformat() + "Z"}
        if on_lead_done:
            row = on_lead_done(lead, row)
        drafts.append(row)
    return drafts
