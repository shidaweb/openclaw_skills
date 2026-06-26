"""Resolve orthogonal campaign, persona, and delivery channel selections."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from _outreach_core.config import BriefError, load_brief, resolve_brief_id
from _outreach_core.persona import resolve_persona_id

CHANNEL_TO_SKILL = {
    "jp_form": "jp-form-outreach",
    "linkedin": "linkedin-outreach",
}


def normalize_channel(channel: str | None) -> str | None:
    value = str(channel or "").strip().lower().replace("-", "_")
    aliases = {"form": "jp_form", "jpform": "jp_form", "linked_in": "linkedin"}
    value = aliases.get(value, value)
    if not value:
        return None
    if value not in CHANNEL_TO_SKILL:
        raise BriefError(f"Unknown outreach channel {channel!r}; use jp_form or linkedin")
    return value


@dataclass(frozen=True)
class OutreachRoute:
    brief_id: str
    persona_id: str | None
    channel: str
    skill: str
    source: str


def resolve_route(
    *,
    brief_id: str | None = None,
    persona_id: str | None = None,
    channel: str | None = None,
    slack_channel_id: str | None = None,
    slack_thread_ts: str | None = None,
) -> OutreachRoute:
    slack_channel = slack_channel_id or os.environ.get("DOORMAN_SLACK_CHANNEL_ID", "").strip()
    thread_ts = slack_thread_ts or os.environ.get("DOORMAN_SLACK_THREAD_TS", "").strip()
    thread_state: dict[str, Any] = {}
    if slack_channel and thread_ts:
        from _outreach_core.channel_state import load_thread_state

        thread_state = load_thread_state(slack_channel, thread_ts) or {}

    bid = resolve_brief_id(brief_id or str(thread_state.get("brief_id") or "") or None)
    brief = load_brief(bid)
    pid = resolve_persona_id(
        persona_id or str(thread_state.get("persona_id") or "") or None,
        brief_config=brief,
        slack_channel_id=slack_channel or None,
        slack_thread_ts=thread_ts or None,
    )

    explicit_channel = normalize_channel(channel)
    if explicit_channel:
        selected = explicit_channel
        source = "explicit"
    else:
        selected = normalize_channel(str(thread_state.get("channel") or "") or None)
        source = "thread" if selected else ""
        if not selected and slack_channel:
            from _outreach_core.channel_state import load_state

            channel_state = load_state(slack_channel) or {}
            defaults = [normalize_channel(x) for x in channel_state.get("default_channels") or []]
            defaults = [x for x in defaults if x]
            if len(defaults) == 1:
                selected = defaults[0]
                source = "slack_channel"
        if not selected:
            desired = [normalize_channel(x) for x in brief.get("desired_channels") or []]
            desired = [x for x in desired if x]
            if len(desired) == 1:
                selected = desired[0]
                source = "brief"
        if not selected:
            raise BriefError(
                "Outreach channel is ambiguous. Select jp_form or linkedin explicitly "
                "or bind it to this Slack thread."
            )

    return OutreachRoute(
        brief_id=bid,
        persona_id=pid,
        channel=selected,
        skill=CHANNEL_TO_SKILL[selected],
        source=source,
    )
