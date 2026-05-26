"""One-way Slack notifications via incoming webhook (not OpenClaw plugin)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from _outreach_core.config import load_sender_brief

_log = logging.getLogger(__name__)

_LEVEL_PREFIX = {
    "info": "ℹ️",
    "warn": "⚠️",
    "error": "❌",
}


def _webhook_url() -> str:
    brief = load_sender_brief()
    slack = brief.get("slack") or {}
    return str(slack.get("incoming_webhook_url") or "").strip()


def post(text: str, *, level: str = "info", thread_ts: str | None = None) -> bool:
    """
    POST to Slack incoming webhook. Never raises.
    thread_ts is ignored (incoming webhooks cannot thread); kept for API compat.
    """
    _ = thread_ts
    url = _webhook_url()
    if not url:
        return False

    prefix = _LEVEL_PREFIX.get(level, "")
    body_text = f"{prefix} {text}".strip() if prefix else text
    payload: dict[str, Any] = {"text": body_text}

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
            _log.debug("notify.post attempt %s failed: %s", attempt + 1, e)
    return False
