"""One-way Slack notifications: incoming webhook or OpenClaw bot (chat.postMessage)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from _outreach_core.config import load_runtime_config
from _outreach_core.openclaw_slack import (
    openclaw_slack_ready,
    resolve_slack_channel_id,
    slack_bot_token,
)

_log = logging.getLogger(__name__)

_LEVEL_PREFIX = {
    "info": "ℹ️",
    "warn": "⚠️",
    "error": "❌",
}


def _webhook_url() -> str:
    brief = load_runtime_config()
    slack = brief.get("slack") or {}
    return str(slack.get("incoming_webhook_url") or "").strip()


def webhook_configured() -> bool:
    return bool(_webhook_url()) or openclaw_slack_ready()


def _format_text(text: str, *, level: str) -> str:
    prefix = _LEVEL_PREFIX.get(level, "")
    return f"{prefix} {text}".strip() if prefix else text


def _post_webhook(text: str) -> bool:
    url = _webhook_url()
    if not url:
        return False
    payload: dict[str, Any] = {"text": text}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            _log.debug("notify.webhook attempt %s failed: %s", attempt + 1, e)
    return False


def _post_slack_api(text: str, *, thread_ts: str | None) -> bool:
    token = slack_bot_token()
    channel = resolve_slack_channel_id()
    if not token or not channel:
        return False
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if body.get("ok"):
                    return True
                _log.debug("chat.postMessage error: %s", body.get("error"))
                return False
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            _log.debug("notify.slack_api attempt %s failed: %s", attempt + 1, e)
    return False


def post(text: str, *, level: str = "info", thread_ts: str | None = None) -> bool:
    """
    Post a one-way status line to Slack. Never raises.

    Delivery order:
    1. briefs/<id>.yaml slack.incoming_webhook_url (if set)
    2. OpenClaw channels.slack.botToken + channel from sessions/config
    """
    body_text = _format_text(text, level=level)
    ok = False
    if _webhook_url():
        ok = _post_webhook(body_text)
    elif openclaw_slack_ready():
        ok = _post_slack_api(body_text, thread_ts=thread_ts)
    try:
        from _outreach_core import events as ev

        if ev.get_context().data_dir:
            import hashlib

            ev.emit(
                "slack.notified",
                stage="notify",
                outcome="ok" if ok else "failed",
                payload={
                    "level": level,
                    "text_hash": hashlib.sha256(body_text.encode()).hexdigest()[:16],
                    "ok": ok,
                },
            )
    except Exception:
        pass
    return ok
