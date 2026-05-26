"""Slack channel ↔ brief binding (v4 §14-F)."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from _outreach_core.config import ACTIVE_BRIEF_FILE, BRIEFS_DIR, SKILLS_ROOT, BriefError, resolve_brief_id

CHANNEL_STATE_DIR = SKILLS_ROOT / "data" / "channel_state"


def state_path(channel_id: str) -> Path:
    cid = channel_id.strip().upper()
    if not cid.startswith("C"):
        raise ValueError(f"invalid Slack channel id: {channel_id!r}")
    CHANNEL_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return CHANNEL_STATE_DIR / f"{cid}.json"


def load_state(channel_id: str) -> dict[str, Any] | None:
    path = state_path(channel_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def save_state(channel_id: str, state: dict[str, Any]) -> Path:
    path = state_path(channel_id)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_active_brief_fallback() -> str:
    """CLI fallback when no Slack channel context."""
    if not ACTIVE_BRIEF_FILE.is_file():
        raise BriefError(
            "No brief selected. Bind a Slack channel (brief bind) or set briefs/_active.txt."
        )
    bid = ACTIVE_BRIEF_FILE.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    if not bid:
        raise BriefError("briefs/_active.txt is empty")
    if not (BRIEFS_DIR / f"{bid}.yaml").is_file():
        raise BriefError(f"Unknown brief {bid!r} in briefs/_active.txt")
    return bid


def resolve_brief_for_channel(
    slack_channel_id: str | None,
) -> tuple[str | None, list[str], bool]:
    """
    Returns (brief_id, default_channels, is_new_channel).
    is_new_channel=True when channel has no binding yet.
    """
    if not slack_channel_id or not str(slack_channel_id).strip():
        return load_active_brief_fallback(), [], False

    state = load_state(slack_channel_id)
    if state is None:
        return None, [], True

    brief = str(state.get("default_brief") or "").strip()
    channels_raw = state.get("default_channels") or []
    channels = [str(c).strip() for c in channels_raw if str(c).strip()]
    if not brief:
        return None, [], True
    return brief, channels, False


def touch_last_used(channel_id: str) -> None:
    state = load_state(channel_id)
    if not state:
        return
    state["last_used_at"] = datetime.utcnow().isoformat() + "Z"
    save_state(channel_id, state)


def bind(
    channel_id: str,
    brief_id: str,
    *,
    default_channels: list[str] | None = None,
    channel_name: str = "",
    operator_user_id: str = "",
) -> Path:
    bid = resolve_brief_id(brief_id)
    now = datetime.utcnow().isoformat() + "Z"
    existing = load_state(channel_id) or {}
    state: dict[str, Any] = {
        "channel_id": channel_id.strip().upper(),
        "channel_name": channel_name or existing.get("channel_name") or "",
        "default_brief": bid,
        "default_channels": default_channels
        if default_channels is not None
        else existing.get("default_channels") or ["jp_form", "linkedin"],
        "associated_since": existing.get("associated_since") or now,
        "last_used_at": now,
        "operator_user_id": operator_user_id or existing.get("operator_user_id") or "",
    }
    return save_state(channel_id, state)


def unbind(channel_id: str) -> bool:
    path = state_path(channel_id)
    if path.is_file():
        path.unlink()
        return True
    return False


def list_channel_bindings() -> list[dict[str, Any]]:
    if not CHANNEL_STATE_DIR.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(CHANNEL_STATE_DIR.glob("C*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(row, dict):
                out.append(row)
        except (OSError, json.JSONDecodeError):
            continue
    return out


def channels_for_brief(brief_id: str) -> list[str]:
    return [
        str(s.get("channel_id") or "")
        for s in list_channel_bindings()
        if str(s.get("default_brief") or "") == brief_id
    ]


def slack_channel_id_from_env() -> str:
    return os.environ.get("DOORMAN_SLACK_CHANNEL_ID", "").strip()
