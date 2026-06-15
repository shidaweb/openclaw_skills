"""Inbound Slack thread control (v27): let a human stop a running campaign by
replying in the run's progress thread.

The rest of the Slack integration is one-way (``notify.post``). This adds the
*read* side. Between each target the send loop calls
``ThreadStopWatcher.should_stop()``, which pulls new human replies in the run's
thread via ``conversations.replies`` and asks the LLM whether the latest
instruction means "stop now". A keyword fast-path covers the obvious cases
without an LLM call.

Design constraints:
  * **Fail safe.** Any missing token / scope / network / LLM error → returns
    "don't stop", so a transient Slack problem can never halt a live run.
  * **Only react to replies that arrive after the run started** (baseline ts),
    and never to the bot's own progress posts (they carry ``bot_id``).
  * **Cheap.** The LLM is only consulted when a *new human reply* exists and the
    keyword fast-path didn't already decide — normally zero LLM calls per check.

Requires the bot token to have conversations history read scope
(``channels:history`` / ``groups:history``) on the run's channel. Without it the
watcher disables itself (and says so once in the thread).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

_log = logging.getLogger(__name__)

# Lowercased substrings that unambiguously mean "stop". Japanese characters are
# unaffected by ``.lower()`` so a single lowercased compare covers both scripts.
_STOP_KEYWORDS: tuple[str, ...] = (
    "停止", "止めて", "止める", "とめて", "やめて", "やめろ", "中止", "中断",
    "ストップ", "キャンセル",
    "stop", "abort", "halt", "cancel",
)

_SLACK_REPLIES_URL = "https://slack.com/api/conversations.replies"


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def _to_float_ts(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def filter_new_human_replies(
    messages: list[dict[str, Any]] | None,
    *,
    after_ts: float,
    bot_user_id: str = "",
) -> list[dict[str, Any]]:
    """Human replies strictly newer than ``after_ts``, oldest-first.

    Excludes the thread root, the bot's own posts (``bot_id`` present or matching
    ``bot_user_id``), system subtypes (joins/edits), and empty text.
    """
    out: list[dict[str, Any]] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        ts = _to_float_ts(m.get("ts"))
        if ts is None or ts <= after_ts:
            continue
        if m.get("bot_id"):
            continue
        if m.get("subtype"):  # channel_join, bot_message edits, etc.
            continue
        user = str(m.get("user") or "")
        if bot_user_id and user == bot_user_id:
            continue
        text = str(m.get("text") or "").strip()
        if not text:
            continue
        out.append({"ts": ts, "text": text, "user": user})
    out.sort(key=lambda x: x["ts"])
    return out


def keyword_stop(text: str) -> bool:
    """True when ``text`` contains an unambiguous stop keyword."""
    t = (text or "").lower()
    return any(k in t for k in _STOP_KEYWORDS)


def parse_stop_decision(raw: str | None) -> dict[str, Any]:
    """Lenient parse of the LLM's JSON verdict → ``{'stop': bool, 'reason': str}``.

    Tolerates code fences / surrounding prose by extracting the first {...} block.
    Unparseable → ``{'stop': False, 'reason': 'unparsed'}`` (fail safe).
    """
    if not raw:
        return {"stop": False, "reason": "empty"}
    text = str(raw).strip()
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        start = text.find("{")
        end = text.rfind("}")
        obj = None
        if 0 <= start < end:
            try:
                obj = json.loads(text[start : end + 1])
            except (ValueError, TypeError):
                obj = None
    if not isinstance(obj, dict):
        return {"stop": False, "reason": "unparsed"}
    stop = obj.get("stop")
    if isinstance(stop, str):
        stop = stop.strip().lower() in ("true", "yes", "1", "stop")
    return {"stop": bool(stop), "reason": str(obj.get("reason") or "")[:160]}


def _build_stop_prompt(text: str) -> str:
    return (
        "あなたは実行中の営業フォーム自動送信バッチの制御アシスタントです。\n"
        "担当者がSlackスレッドにLive返信しました。この返信が『今すぐバッチ処理を"
        "停止してほしい』という指示かどうかを判定してください。\n"
        "停止指示の例: 「止めて」「一旦ストップ」「今日はもうやめて」など。\n"
        "停止でない例: 質問・感想・雑談・特定の1社だけ直す依頼など。\n"
        "次のJSONのみを出力: {\"stop\": true|false, \"reason\": \"簡潔な理由\"}\n\n"
        f"返信本文:\n{text[:800]}"
    )


def interpret_stop(
    text: str,
    *,
    infer: Callable[[str], str | None] | None = None,
) -> tuple[bool, str]:
    """Decide whether a single reply means stop. Keyword fast-path → LLM.

    ``infer`` is a ``prompt -> text`` callable (defaults to ``infer.oc_infer``),
    injectable for tests. Any LLM error is treated as "don't stop".
    """
    if keyword_stop(text):
        return True, "keyword"
    if infer is None:
        try:
            from _outreach_core.infer import oc_infer as infer  # type: ignore
        except Exception:  # noqa: BLE001
            return False, "no_infer"
    try:
        raw = infer(_build_stop_prompt(text))
    except Exception as exc:  # noqa: BLE001
        _log.debug("interpret_stop infer error: %s", exc)
        return False, "infer_error"
    decision = parse_stop_decision(raw)
    return bool(decision.get("stop")), str(decision.get("reason") or "llm")


# ---------------------------------------------------------------------------
# Slack read + watcher
# ---------------------------------------------------------------------------

def _fetch_replies(
    *, channel: str, thread_ts: str, token: str, oldest: float, limit: int = 50,
) -> tuple[list[dict[str, Any]] | None, str]:
    """Call conversations.replies. Returns (messages|None, error_str)."""
    params = urllib.parse.urlencode(
        {
            "channel": channel,
            "ts": thread_ts,
            "oldest": f"{oldest:.6f}",
            "inclusive": "false",
            "limit": str(limit),
        }
    )
    req = urllib.request.Request(
        f"{_SLACK_REPLIES_URL}?{params}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return None, f"network:{exc}"
    if not body.get("ok"):
        return None, str(body.get("error") or "slack_not_ok")
    msgs = body.get("messages")
    return (msgs if isinstance(msgs, list) else []), ""


class ThreadStopWatcher:
    """Watches the run's Slack thread for a human stop instruction."""

    def __init__(
        self,
        *,
        channel: str,
        thread_ts: str,
        token: str,
        baseline_ts: float | None = None,
        bot_user_id: str = "",
        infer: Callable[[str], str | None] | None = None,
        fetcher: Callable[..., tuple[list[dict[str, Any]] | None, str]] | None = None,
    ) -> None:
        self.channel = channel
        self.thread_ts = thread_ts
        self.token = token
        self.enabled = bool(channel and thread_ts and token)
        self._last_ts = baseline_ts if baseline_ts is not None else time.time()
        self._bot_user_id = bot_user_id
        self._infer = infer
        self._fetch = fetcher or _fetch_replies
        self._warned_scope = False

    @classmethod
    def from_env(cls, config: dict[str, Any] | None = None) -> "ThreadStopWatcher":
        """Build from OpenClaw Slack config + DOORMAN_SLACK_THREAD_TS env.

        Returns a disabled watcher (no-op) when anything required is missing, so
        callers never have to special-case the un-configured path.
        """
        import os

        try:
            from _outreach_core.openclaw_slack import (
                resolve_slack_channel_id,
                slack_bot_token,
            )

            token = slack_bot_token()
            channel = resolve_slack_channel_id()
        except Exception:  # noqa: BLE001
            token, channel = "", ""
        thread_ts = os.environ.get("DOORMAN_SLACK_THREAD_TS", "").strip()
        return cls(channel=channel, thread_ts=thread_ts, token=token)

    def should_stop(self) -> tuple[bool, str | None]:
        """(stop, reason). Safe to call frequently; no-op when disabled."""
        if not self.enabled:
            return False, None
        messages, err = self._fetch(
            channel=self.channel,
            thread_ts=self.thread_ts,
            token=self.token,
            oldest=self._last_ts,
        )
        if err:
            # Most likely missing_scope or a transient network blip. Don't halt
            # the run; surface a missing-scope problem to the thread once.
            if (not self._warned_scope) and ("scope" in err or "not_in_channel" in err):
                self._warned_scope = True
                self._notify(
                    "⚠ スレッド返信の受信に必要な権限がありません"
                    f"（conversations.replies: {err}）。停止制御は無効のまま続行します。"
                )
            _log.debug("should_stop fetch error: %s", err)
            return False, None
        new = filter_new_human_replies(
            messages, after_ts=self._last_ts, bot_user_id=self._bot_user_id
        )
        if not new:
            return False, None
        # Advance the cursor so we don't re-evaluate these next time.
        self._last_ts = new[-1]["ts"]
        for m in reversed(new):  # newest instruction wins
            stop, reason = interpret_stop(m["text"], infer=self._infer)
            if stop:
                return True, f"{reason}: {m['text'][:80]}"
        return False, None

    def _notify(self, text: str) -> None:
        try:
            from _outreach_core.notify import post

            post(text, level="warn", thread_ts=self.thread_ts)
        except Exception:  # noqa: BLE001
            pass
