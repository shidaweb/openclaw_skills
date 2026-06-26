"""Cache-stable system prompt assembly and JSON extraction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


def _prefer_local(prompts_dir: Path, name: str) -> Path:
    """Prefer a gitignored per-client override (e.g. system_persona.local.md) over
    the shared, neutral template that ships in git. This is how each client keeps
    its OWN persona / few-shot examples (sender identity, case studies, calendar
    URL) locally without ever committing them. Falls back to the shared file."""
    stem, _, ext = name.rpartition(".")
    local = prompts_dir / f"{stem}.local.{ext}"
    if local.is_file():
        return local
    return prompts_dir / name


def _override_value(config: dict[str, Any], suffix: str, channel: str | None = None) -> Any:
    overrides = config.get("prompts_overrides") or {}
    selected = channel or config.get("_channel")
    if selected:
        return overrides.get(f"{selected}_{suffix}")
    # Legacy callers without channel context retain the old fallback order.
    return overrides.get(f"jp_form_{suffix}") or overrides.get(f"linkedin_{suffix}")


def resolve_prompts_dir(
    skill_dir: Path,
    config: dict[str, Any],
    channel: str | None = None,
) -> Path:
    """Skill default prompts, or brief-specific override from prompts_overrides."""
    rel = _override_value(config, "system_persona", channel)
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
    persona_override = _override_value(config, "system_persona")
    examples_override = _override_value(config, "examples")
    if persona_override:
        persona_path = prompts_dir / Path(str(persona_override)).name
    else:
        persona_path = _prefer_local(prompts_dir, "system_persona.md")
    if examples_override:
        examples_path = prompts_dir / Path(str(examples_override)).name
    elif persona_override:
        # A campaign-specific persona must not accidentally inherit unrelated
        # global few-shots (the Tenbin run inherited a CellCloud example).
        examples_path = prompts_dir / "__no_campaign_examples__.md"
    else:
        examples_path = _prefer_local(prompts_dir, "examples.md")
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
    """Pull the first JSON object out of model output, tolerant of prose/fences."""
    if not text:
        return None
    text = _strip_outer_code_fence(text)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        candidate = _extract_balanced_json(text, i, ch)
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    return item
    return None


def _strip_outer_code_fence(text: str) -> str:
    """Remove a single outer ```...``` wrapper if present."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3:
        return stripped
    first = lines[0].strip()
    last = lines[-1].strip()
    if not first.startswith("```") or last != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _extract_balanced_json(text: str, start: int, opener: str) -> str | None:
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
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
