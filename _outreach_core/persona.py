"""First-class outreach persona registry.

Personas describe *who is speaking* (sender identity and voice). Briefs describe
*what campaign is running* (offer, target, sequence). Channels describe *where
it is delivered*. Keeping these axes separate lets one Slack thread select any
valid combination without copying sender data into every campaign.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

from _outreach_core.config import BriefError, SKILLS_ROOT

PERSONAS_DIR = SKILLS_ROOT / "personas"


class PersonaError(BriefError):
    """Invalid or missing persona configuration."""


def list_persona_ids() -> list[str]:
    if not PERSONAS_DIR.is_dir():
        return []
    return [
        path.stem
        for path in sorted(PERSONAS_DIR.glob("*.yaml"))
        if not path.name.startswith("_") and ".example" not in path.name
    ]


def resolve_persona_id(
    persona_id: str | None,
    *,
    brief_config: dict[str, Any] | None = None,
    slack_channel_id: str | None = None,
    slack_thread_ts: str | None = None,
) -> str | None:
    """Resolve explicit/env/thread/default persona, returning None for legacy briefs."""
    explicit = str(persona_id or "").strip()
    if explicit:
        resolved = explicit
    else:
        env_persona = os.environ.get("DOORMAN_PERSONA_ID", "").strip()
        if env_persona:
            resolved = env_persona
        else:
            resolved = ""
            channel_id = slack_channel_id or os.environ.get("DOORMAN_SLACK_CHANNEL_ID", "").strip()
            thread_ts = slack_thread_ts or os.environ.get("DOORMAN_SLACK_THREAD_TS", "").strip()
            if channel_id and thread_ts:
                from _outreach_core.channel_state import load_thread_state

                state = load_thread_state(channel_id, thread_ts) or {}
                resolved = str(state.get("persona_id") or "").strip()
            if not resolved:
                cfg = brief_config or {}
                resolved = str(
                    cfg.get("persona_id")
                    or (cfg.get("brief") or {}).get("persona_id")
                    or ""
                ).strip()

    if not resolved:
        return None
    path = PERSONAS_DIR / f"{resolved}.yaml"
    if not path.is_file():
        known = ", ".join(list_persona_ids()) or "(none)"
        raise PersonaError(f"Unknown persona {resolved!r}. Known personas: {known}")
    return resolved


def load_persona(persona_id: str) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("pyyaml required")
    resolved = resolve_persona_id(persona_id)
    assert resolved is not None
    data = yaml.safe_load((PERSONAS_DIR / f"{resolved}.yaml").read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise PersonaError(f"personas/{resolved}.yaml must be a mapping")
    data.setdefault("persona", {})["id"] = resolved
    return data
