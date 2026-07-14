"""Load briefs/<id>.yaml merged with per-skill config.yaml (v4 §14)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

SKILLS_ROOT = Path(__file__).resolve().parent.parent
BRIEFS_DIR = SKILLS_ROOT / "briefs"
ACTIVE_BRIEF_FILE = BRIEFS_DIR / "_active.txt"
BRIEF_TEMPLATE = BRIEFS_DIR / "_template.yaml"


class BriefError(RuntimeError):
    """Invalid or missing brief configuration."""


def _require_yaml() -> None:
    if yaml is None:
        raise RuntimeError("pyyaml required; pip install pyyaml")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def list_brief_ids() -> list[str]:
    if not BRIEFS_DIR.is_dir():
        return []
    ids: list[str] = []
    for p in sorted(BRIEFS_DIR.glob("*.yaml")):
        if p.name.startswith("_") or ".example" in p.name:
            continue
        ids.append(p.stem)
    return ids


def resolve_brief_id(brief_id: str | None) -> str:
    """Resolve brief slug; raises BriefError if missing/invalid."""
    if brief_id and brief_id.strip():
        bid = brief_id.strip()
    else:
        ch = os.environ.get("DOORMAN_SLACK_CHANNEL_ID", "").strip()
        if ch:
            from _outreach_core.channel_state import resolve_brief_for_channel

            thread_ts = os.environ.get("DOORMAN_SLACK_THREAD_TS", "").strip() or None
            resolved, _channels, is_new = resolve_brief_for_channel(ch, thread_ts)
            if is_new or not resolved:
                raise BriefError(
                    f"Slack channel {ch} is not bound to a brief.\n"
                    f"  ~/.openclaw/skills/brief bind --channel-id {ch} --brief <id>\n"
                    "  Or complete Slack onboarding (§14-N in SKILL.md)."
                )
            bid = resolved
        elif ACTIVE_BRIEF_FILE.is_file():
            bid = ACTIVE_BRIEF_FILE.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        else:
            raise BriefError(
                "No brief selected. Pass --brief <id>, bind a Slack channel, or set briefs/_active.txt.\n"
                "  ~/.openclaw/skills/brief list"
            )
    if not bid:
        raise BriefError("briefs/_active.txt is empty")
    path = BRIEFS_DIR / f"{bid}.yaml"
    if not path.is_file():
        known = ", ".join(list_brief_ids()) or "(none)"
        raise BriefError(f"Unknown brief {bid!r}. Known briefs: {known}")
    return bid


def load_brief(brief_id: str | None = None) -> dict[str, Any]:
    _require_yaml()
    bid = resolve_brief_id(brief_id)
    data = yaml.safe_load((BRIEFS_DIR / f"{bid}.yaml").read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise BriefError(f"briefs/{bid}.yaml must be a mapping")
    data.setdefault("brief", {})["id"] = bid
    return data


def load_merged_config(
    skill_dir: Path,
    brief_id: str | None = None,
    *,
    persona_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    """
    Merge skill defaults -> campaign brief -> persona.

    Persona is last so an explicitly selected speaker can be reused across
    campaigns without stale inline sender/tone fields winning. Legacy briefs
    without persona_id continue to work unchanged.
    """
    _require_yaml()
    bid = resolve_brief_id(brief_id)
    brief_cfg = load_brief(bid)
    skill_cfg_path = skill_dir / "config.yaml"
    if not skill_cfg_path.exists():
        raise FileNotFoundError(skill_cfg_path)
    skill_cfg = yaml.safe_load(skill_cfg_path.read_text(encoding="utf-8")) or {}
    merged = _deep_merge(skill_cfg, brief_cfg)
    from _outreach_core.persona import load_persona, resolve_persona_id

    pid = resolve_persona_id(
        persona_id,
        brief_config=brief_cfg,
        slack_channel_id=os.environ.get("DOORMAN_SLACK_CHANNEL_ID", "").strip() or None,
        slack_thread_ts=os.environ.get("DOORMAN_SLACK_THREAD_TS", "").strip() or None,
    )
    if pid:
        merged = _deep_merge(merged, load_persona(pid))
    merged["_brief_id"] = bid
    merged["_persona_id"] = pid
    if channel:
        merged["_channel"] = channel
    return merged


def load_runtime_config(brief_id: str | None = None) -> dict[str, Any]:
    """Brief-only config (slack, heartbeat, browser) for notify/progress without skill dir.

    Best-effort BY CONTRACT: every caller is observability plumbing
    (heartbeat mode, notify webhook, keepalive interval), and a missing or
    mistyped brief must degrade to defaults there — never crash a run.
    Callers that REQUIRE a brief use load_brief/load_merged_config, which
    still raise BriefError loudly.
    """
    try:
        return load_brief(brief_id)
    except BriefError:
        return {}


def heartbeat_interval_sec(merged_config: dict[str, Any] | None = None) -> int:
    cfg = merged_config or load_runtime_config()
    hb = cfg.get("heartbeat") or {}
    try:
        return int(hb.get("interval_sec", 600))
    except (TypeError, ValueError):
        return 600
