"""Read Slack delivery settings from OpenClaw (~/.openclaw/openclaw.json + sessions)."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from _outreach_core.config import load_sender_brief

_log = logging.getLogger(__name__)

_CHANNEL_RE = re.compile(r"^channel:(C[A-Z0-9]+)$", re.IGNORECASE)


def openclaw_home() -> Path:
    return Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw")).expanduser()


def openclaw_config_path() -> Path:
    return Path(os.environ.get("OPENCLAW_CONFIG", openclaw_home() / "openclaw.json")).expanduser()


def load_openclaw_config() -> dict[str, Any]:
    path = openclaw_config_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text()) or {}
    except (OSError, json.JSONDecodeError) as e:
        _log.debug("load_openclaw_config failed: %s", e)
        return {}


def _slack_section(cfg: dict[str, Any]) -> dict[str, Any]:
    channels = cfg.get("channels") or {}
    slack = channels.get("slack") or {}
    if isinstance(slack, dict):
        return slack
    return {}


def slack_bot_token() -> str:
    """Bot token from OpenClaw channels.slack (same as Socket Mode plugin)."""
    slack = _slack_section(load_openclaw_config())
    token = slack.get("botToken") or slack.get("token") or ""
    if isinstance(token, str) and token.strip() and not token.startswith("__OPENCLAW"):
        return token.strip()
    # Multi-account layout: channels.slack.accounts.default.botToken
    accounts = slack.get("accounts") or {}
    if isinstance(accounts, dict):
        for acc in accounts.values():
            if not isinstance(acc, dict):
                continue
            t = acc.get("botToken") or acc.get("token") or ""
            if isinstance(t, str) and t.strip() and not t.startswith("__OPENCLAW"):
                return t.strip()
    return ""


def _channel_from_to_field(to_value: str) -> str:
    m = _CHANNEL_RE.match((to_value or "").strip())
    return m.group(1).upper() if m else ""


def slack_channel_id_from_sessions(agent_id: str = "main") -> str:
    """
    Most recently updated Slack channel session (e.g. #general).
    Matches OpenClaw session keys like agent:main:slack:channel:c09d38ugjtc.
    """
    path = openclaw_home() / "agents" / agent_id / "sessions" / "sessions.json"
    if not path.is_file():
        return ""
    try:
        sessions = json.loads(path.read_text()) or {}
    except (OSError, json.JSONDecodeError):
        return ""
    best_ts = -1
    best_channel = ""
    for _key, sess in sessions.items():
        if not isinstance(sess, dict) or sess.get("channel") != "slack":
            continue
        to = (
            sess.get("lastTo")
            or (sess.get("deliveryContext") or {}).get("to")
            or (sess.get("origin") or {}).get("to")
            or ""
        )
        cid = _channel_from_to_field(str(to))
        if not cid:
            native = (sess.get("origin") or {}).get("nativeChannelId")
            if isinstance(native, str) and native.startswith("C"):
                cid = native.upper()
        if not cid:
            continue
        ts = int(sess.get("updatedAt") or 0)
        if ts >= best_ts:
            best_ts = ts
            best_channel = cid
    return best_channel


def resolve_slack_channel_id() -> str:
    """
    Priority: sender_brief slack.channel_id → OpenClaw slack session →
    channels.slack.defaultChannelId in openclaw.json.
    """
    brief = load_sender_brief()
    slack = brief.get("slack") or {}
    explicit = str(slack.get("channel_id") or "").strip()
    if explicit:
        return explicit.upper() if explicit.startswith("C") else explicit

    from_sessions = slack_channel_id_from_sessions()
    if from_sessions:
        return from_sessions

    oc_slack = _slack_section(load_openclaw_config())
    default = str(oc_slack.get("defaultChannelId") or oc_slack.get("channelId") or "").strip()
    return default.upper() if default.startswith("C") else default


def openclaw_slack_ready() -> bool:
    return bool(slack_bot_token() and resolve_slack_channel_id())
