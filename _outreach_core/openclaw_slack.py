"""Read Slack delivery settings from OpenClaw (~/.openclaw/openclaw.json + sessions)."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _outreach_core.config import load_runtime_config

_log = logging.getLogger(__name__)

_CHANNEL_RE = re.compile(r"^channel:(C[A-Z0-9]+)$", re.IGNORECASE)
_THREAD_KEY_RE = re.compile(r":thread:([0-9]+(?:\.[0-9]+)?)$", re.IGNORECASE)


@dataclass(frozen=True)
class SlackDeliveryContext:
    """A channel/thread pair recovered from one OpenClaw Slack session."""

    channel_id: str = ""
    thread_ts: str = ""
    source: str = "none"
    updated_at_ms: int = 0


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


def _session_thread_ts(key: str, sess: dict[str, Any]) -> str:
    for value in (
        (sess.get("deliveryContext") or {}).get("threadId"),
        (sess.get("origin") or {}).get("threadId"),
        sess.get("lastThreadId"),
    ):
        if value not in (None, ""):
            return str(value).strip()
    match = _THREAD_KEY_RE.search(key)
    return match.group(1) if match else ""


def slack_delivery_context_from_sessions(
    agent_id: str = "main",
    *,
    thread_max_age_sec: int | None = None,
) -> SlackDeliveryContext:
    """
    Return the most recently updated Slack session as one atomic route.

    The channel and thread must come from the same session. A stale thread is
    discarded so a manually launched job cannot accidentally reply to an old
    conversation. Set ``thread_max_age_sec=0`` to disable the age check.
    """
    path = openclaw_home() / "agents" / agent_id / "sessions" / "sessions.json"
    if not path.is_file():
        return SlackDeliveryContext()
    try:
        sessions = json.loads(path.read_text()) or {}
    except (OSError, json.JSONDecodeError):
        return SlackDeliveryContext()
    best_ts = -1
    best = SlackDeliveryContext()
    for key, sess in sessions.items():
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
            best = SlackDeliveryContext(
                channel_id=cid,
                thread_ts=_session_thread_ts(str(key), sess),
                source="openclaw_session",
                updated_at_ms=ts,
            )

    if not best.thread_ts:
        return best
    if thread_max_age_sec is None:
        raw = os.environ.get("DOORMAN_SLACK_CONTEXT_MAX_AGE_SEC", "900").strip()
        try:
            thread_max_age_sec = max(0, int(float(raw)))
        except (TypeError, ValueError):
            thread_max_age_sec = 900
    if thread_max_age_sec > 0 and best.updated_at_ms > 0:
        age_ms = max(0, int(time.time() * 1000) - best.updated_at_ms)
        if age_ms > thread_max_age_sec * 1000:
            return SlackDeliveryContext(
                channel_id=best.channel_id,
                source=f"{best.source}_stale_thread",
                updated_at_ms=best.updated_at_ms,
            )
    return best


def slack_channel_id_from_sessions(agent_id: str = "main") -> str:
    """Most recently updated Slack channel session (e.g. #general)."""
    return slack_delivery_context_from_sessions(agent_id).channel_id


def resolve_slack_delivery_context(
    *,
    channel_id: str | None = None,
    thread_ts: str | None = None,
) -> SlackDeliveryContext:
    """Resolve an explicit/env/session/config Slack route without mixing sessions."""
    explicit_channel = str(channel_id or "").strip()
    env_channel = os.environ.get("DOORMAN_SLACK_CHANNEL_ID", "").strip()
    explicit_thread = str(thread_ts or "").strip()
    env_thread = os.environ.get("DOORMAN_SLACK_THREAD_TS", "").strip()
    session = slack_delivery_context_from_sessions()

    selected_channel = explicit_channel or env_channel
    source = "argument" if explicit_channel else "environment" if env_channel else ""
    if not selected_channel:
        brief = load_runtime_config()
        slack = brief.get("slack") or {}
        selected_channel = str(slack.get("channel_id") or "").strip()
        if selected_channel:
            source = "brief"
    if not selected_channel and session.channel_id:
        selected_channel = session.channel_id
        source = session.source
    if not selected_channel:
        oc_slack = _slack_section(load_openclaw_config())
        selected_channel = str(
            oc_slack.get("defaultChannelId") or oc_slack.get("channelId") or ""
        ).strip()
        if selected_channel:
            source = "openclaw_config"

    selected_thread = explicit_thread or env_thread
    if explicit_thread:
        source = f"{source or 'argument'}+argument_thread"
    elif env_thread:
        source = f"{source or 'environment'}+environment_thread"
    elif (
        session.thread_ts
        and session.channel_id
        and session.channel_id.upper() == selected_channel.upper()
    ):
        selected_thread = session.thread_ts
        source = session.source

    if selected_channel.startswith("C"):
        selected_channel = selected_channel.upper()
    return SlackDeliveryContext(
        channel_id=selected_channel,
        thread_ts=selected_thread,
        source=source or "none",
        updated_at_ms=session.updated_at_ms if source.startswith("openclaw_session") else 0,
    )


def resolve_slack_channel_id() -> str:
    """
    Priority: DOORMAN_SLACK_CHANNEL_ID → brief yaml slack.channel_id →
    OpenClaw slack session → channels.slack.defaultChannelId in openclaw.json.
    """
    return resolve_slack_delivery_context().channel_id


def resolve_slack_thread_ts() -> str:
    """Current Slack thread from explicit environment or a fresh OpenClaw session."""
    return resolve_slack_delivery_context().thread_ts


def openclaw_slack_ready() -> bool:
    return bool(slack_bot_token() and resolve_slack_channel_id())
