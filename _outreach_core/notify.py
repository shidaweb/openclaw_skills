"""One-way Slack notifications: incoming webhook or OpenClaw bot (chat.postMessage)."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, MutableMapping
from typing import Any

from _outreach_core.config import load_runtime_config
from _outreach_core.openclaw_slack import (
    openclaw_slack_ready,
    resolve_slack_channel_id,
    slack_bot_token,
)

_log = logging.getLogger(__name__)
_LAST_DELIVERY_ERROR = ""
_LAST_DELIVERY_ROUTE = ""

_LEVEL_PREFIX = {
    "info": "ℹ️",
    "warn": "⚠️",
    "error": "❌",
}

PROBLEM_DEDUP_SEC = 600
_PROBLEM_RECENT: dict[str, float] = {}

_REASON_LABELS = {
    "target_timeout": "1社処理がタイムアウトしました",
    "lead_soft_timeout": "1社処理が規定時間を超えました",
    "manual_verify": "目視確認が必要です",
    "unverified_prior_attempt": "前回runが最終送信クリック後・検証前に停止しました",
    "interrupted_before_submit": "前回runが最終送信クリック前に停止しました",
    "validation_unrecoverable": "必須項目/バリデーションを自動修復できません",
    "submit_not_found": "確認/送信ボタンを自動特定できません",
    "no_form": "ページにフォームが見つかりません",
    "page_has_no_form": "フォーム未検出（URL再精査が必要）",
    "form_vanished_after_fill": "入力後にフォームが消失しました",
    "first_submit_not_found": "最初の送信ボタンを自動特定できません",
    "confirm_submit_not_found": "確認画面の最終送信ボタンを自動特定できません",
    "submit_button_not_found": "送信ボタンを自動特定できません",
    "wrong_form_type": "想定と異なるフォーム種別を検出しました",
    "submit_gate_unsatisfied": "同意・必須選択などの送信条件が未充足です",
    "wizard_too_deep": "多段フォームのステップ数が上限を超えました",
    "non_contact_form": "問い合わせフォームではないページを検出しました",
    "unknown_no_textarea": "問い合わせ本文欄を確認できません",
    "email_verification_code": "メール確認コード方式のため自動送信できません",
    "open_failed_stale_page": "ページを開けず、前のページが残っています",
    "empty_render": "ページにフォーム要素が表示されていません",
    "captcha_blocking": "CAPTCHAが送信をブロックしています",
    "cloudflare_blocking": "Cloudflare等の保護画面で停止しました",
    "run_give_up_stalled": "runの停止検知が再試行上限に達しました",
    "run_give_up_crash": "runの異常終了が再試行上限に達しました",
}

_ACTION_LABELS = {
    "manual_verify": "送信済みか目視確認してください。",
    "manual_review_or_retry": "対象を目視確認し、必要なら手動再実行してください。",
    "field_values": "needs_attention の未解決項目に入力値を指定してください。",
    "proceed": "対象IDを指定して手動再開するか、画面状態を確認してください。",
    "auto_resolve": "自動リゾルバで再試行します。失敗時はURL再精査または手動送信をしてください。",
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


def _set_last_delivery(*, route: str = "", error: str = "") -> None:
    global _LAST_DELIVERY_ERROR, _LAST_DELIVERY_ROUTE
    _LAST_DELIVERY_ROUTE = route
    _LAST_DELIVERY_ERROR = error


def last_delivery_error() -> str:
    return _LAST_DELIVERY_ERROR


def last_delivery_route() -> str:
    return _LAST_DELIVERY_ROUTE


def _post_webhook(text: str) -> bool:
    url = _webhook_url()
    if not url:
        _set_last_delivery(route="webhook", error="webhook_not_configured")
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
                ok = 200 <= resp.status < 300
                if not ok:
                    _set_last_delivery(route="webhook", error=f"http_status:{resp.status}")
                return ok
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            _set_last_delivery(route="webhook", error=f"{type(e).__name__}:{e}")
            _log.debug("notify.webhook attempt %s failed: %s", attempt + 1, e)
    return False


def _post_slack_api(
    text: str,
    *,
    thread_ts: str | None,
    channel_id: str | None = None,
) -> bool:
    token = slack_bot_token()
    channel = str(channel_id or resolve_slack_channel_id()).strip()
    if not token or not channel:
        missing = []
        if not token:
            missing.append("token")
        if not channel:
            missing.append("channel")
        _set_last_delivery(route="slack_api", error="missing_" + "_".join(missing))
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
                err = str(body.get("error") or "unknown_error")
                _set_last_delivery(route="slack_api", error=err)
                _log.debug("chat.postMessage error: %s", err)
                return False
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            _set_last_delivery(route="slack_api", error=f"{type(e).__name__}:{e}")
            _log.debug("notify.slack_api attempt %s failed: %s", attempt + 1, e)
    return False


def _problem_dedup_sec() -> int:
    raw = os.environ.get("DOORMAN_PROBLEM_DEDUP_SEC", "").strip()
    if not raw:
        return PROBLEM_DEDUP_SEC
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return PROBLEM_DEDUP_SEC


def _recent_problem_seen(
    key: str,
    now: float,
    *,
    recent: Mapping[str, float] | None = None,
    window_sec: int | None = None,
) -> bool:
    """Pure duplicate check for problem notifications."""
    if not key:
        return False
    window = _problem_dedup_sec() if window_sec is None else int(window_sec)
    if window <= 0:
        return False
    store = recent if recent is not None else _PROBLEM_RECENT
    last = store.get(key)
    return last is not None and max(0.0, float(now) - float(last)) < window


def _mark_problem_seen(
    key: str,
    now: float,
    *,
    recent: MutableMapping[str, float] | None = None,
    window_sec: int | None = None,
) -> None:
    if not key:
        return
    window = _problem_dedup_sec() if window_sec is None else int(window_sec)
    store = recent if recent is not None else _PROBLEM_RECENT
    if window > 0:
        stale = [k for k, ts in store.items() if max(0.0, float(now) - float(ts)) >= window]
        for k in stale[:200]:
            store.pop(k, None)
    store[key] = float(now)


def _target_value(target: Mapping[str, Any] | None, *keys: str) -> str:
    if not isinstance(target, Mapping):
        return ""
    for key in keys:
        value = target.get(key)
        if value not in (None, "", [], {}):
            return str(value)
    return ""


def _problem_key(kind: str, target: Mapping[str, Any] | None) -> str:
    tid = _target_value(target, "target_id", "id")
    if not tid:
        tid = _target_value(target, "name", "company", "url", "form_url") or "unknown"
    return f"{str(kind or 'problem').strip()}:{tid}"


def _detail_value(detail: Mapping[str, Any] | str | None, *keys: str) -> str:
    if isinstance(detail, Mapping):
        for key in keys:
            value = detail.get(key)
            if value not in (None, "", [], {}):
                return str(value)
    if isinstance(detail, str):
        return detail
    return ""


def _humanize_code(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    head = text.split(":", 1)[0].strip()
    label = _REASON_LABELS.get(text) or _REASON_LABELS.get(head)
    if label and ":" in text:
        rest = text.split(":", 1)[1].strip()
        return f"{label}: {rest}" if rest else label
    return label or text


def _next_action(target: Mapping[str, Any] | None, detail: Mapping[str, Any] | str | None) -> str:
    explicit = _detail_value(detail, "next_action", "action")
    if explicit:
        return _ACTION_LABELS.get(explicit, _humanize_code(explicit))
    action_needed = _target_value(target, "action_needed")
    if action_needed in _ACTION_LABELS:
        return _ACTION_LABELS[action_needed]
    if action_needed:
        return f"{_humanize_code(action_needed)}を確認してください。"
    return "needs_attention を確認し、必要なら目視確認・手動再開してください。"


def format_problem_message(
    kind: str,
    target: Mapping[str, Any] | None,
    detail: Mapping[str, Any] | str | None = None,
) -> str:
    """Build a compact human problem report for Slack."""
    custom = _target_value(target, "slack_message") or _detail_value(detail, "slack_message")
    if custom:
        return custom
    company = _target_value(target, "name", "company", "target_id", "id") or "不明"
    url = (
        _target_value(target, "form_url", "url")
        or _detail_value(detail, "form_url", "url")
        or "-"
    )
    root = (
        _detail_value(detail, "root_cause", "reason_class", "reason", "error")
        or _target_value(target, "root_cause", "reason_class", "reason", "error")
        or str(kind or "problem")
    )
    root = _humanize_code(root)
    kind_label = {
        "needs_attention": "要確認",
        "target_timeout": "1社タイムアウト",
        "run_give_up_stalled": "run stall 再試行上限",
        "run_give_up_crash": "run crash 再試行上限",
    }.get(str(kind or ""), str(kind or "problem"))
    return "\n".join([
        f"問題検知: {kind_label}",
        f"会社: {company}",
        f"URL: {url}",
        f"根本原因: {root}",
        f"次アクション: {_next_action(target, detail)}",
    ])


def _emit_notify_event(kind: str, *, outcome: str, payload: dict[str, Any]) -> None:
    try:
        from _outreach_core import events as ev

        if ev.get_context().data_dir:
            ev.emit(kind, stage="notify", outcome=outcome, payload=payload)
    except Exception:
        pass


def post_problem(
    kind: str,
    target: Mapping[str, Any] | None,
    detail: Mapping[str, Any] | str | None = None,
    *,
    thread_ts: str | None = None,
    channel_id: str | None = None,
) -> bool:
    """Post a deduplicated warn/error problem report to Slack. Never raises."""
    import hashlib

    _set_last_delivery()
    now = time.time()
    dedup_key = _problem_key(kind, target)
    if _recent_problem_seen(dedup_key, now):
        _emit_notify_event(
            "slack.problem_deduped",
            outcome="skipped",
            payload={"kind": kind, "dedup_key_hash": hashlib.sha256(dedup_key.encode()).hexdigest()[:16]},
        )
        return False
    _mark_problem_seen(dedup_key, now)

    level = "error" if str(kind or "").startswith("run_give_up") else "warn"
    if isinstance(detail, Mapping) and detail.get("level"):
        level = str(detail.get("level"))
    body_text = _format_text(format_problem_message(kind, target, detail), level=level)
    desired_thread_ts = (
        thread_ts
        or os.environ.get("DOORMAN_SLACK_THREAD_TS", "").strip()
        or None
    )

    ok = False
    route = "none"
    try:
        # If a thread is requested, prefer the bot even when a webhook is set:
        # incoming webhooks cannot reply into an existing thread.
        if desired_thread_ts and openclaw_slack_ready():
            route = "bot_thread"
            ok = _post_slack_api(
                body_text,
                thread_ts=desired_thread_ts,
                channel_id=channel_id,
            )
        if not ok and desired_thread_ts and openclaw_slack_ready():
            route = "bot_channel_fallback"
            ok = _post_slack_api(
                _format_text(
                    "（スレッド投稿に失敗したためチャンネル投稿）\n"
                    + format_problem_message(kind, target, detail),
                    level=level,
                ),
                thread_ts=None,
                channel_id=channel_id,
            )
        if not ok and not desired_thread_ts and openclaw_slack_ready():
            route = "bot"
            ok = _post_slack_api(body_text, thread_ts=None, channel_id=channel_id)
        if not ok and _webhook_url():
            route = "webhook_fallback" if not desired_thread_ts else "webhook_channel_fallback"
            text = body_text
            if desired_thread_ts:
                text = _format_text(
                    "（スレッド投稿に失敗したためwebhookで通知）\n"
                    + format_problem_message(kind, target, detail),
                    level=level,
                )
            ok = _post_webhook(text)
    except Exception:
        _set_last_delivery(route=route, error="unexpected_exception")
        ok = False

    _emit_notify_event(
        "slack.problem_notified",
        outcome="ok" if ok else "failed",
        payload={
            "kind": kind,
            "target_id": _target_value(target, "target_id", "id") or None,
            "level": level,
            "route": route,
            "thread": bool(desired_thread_ts),
            "text_hash": hashlib.sha256(body_text.encode()).hexdigest()[:16],
            "ok": ok,
            "error": last_delivery_error() or None,
        },
    )
    if ok:
        _set_last_delivery(route=route, error="")
    return ok


def post(
    text: str,
    *,
    level: str = "info",
    thread_ts: str | None = None,
    channel_id: str | None = None,
) -> bool:
    """
    Post a one-way status line to Slack. Never raises.

    Bot delivery is preferred because it can target the originating channel
    and thread. Incoming webhooks are only a channel-level fallback.
    """
    _set_last_delivery()
    body_text = _format_text(text, level=level)
    desired_thread_ts = (
        thread_ts
        or os.environ.get("DOORMAN_SLACK_THREAD_TS", "").strip()
        or None
    )
    ok = False
    route = "none"
    try:
        if desired_thread_ts and openclaw_slack_ready():
            route = "bot_thread"
            ok = _post_slack_api(
                body_text,
                thread_ts=desired_thread_ts,
                channel_id=channel_id,
            )
        if not ok and desired_thread_ts and openclaw_slack_ready():
            route = "bot_channel_fallback"
            ok = _post_slack_api(
                _format_text(
                    "（スレッド投稿に失敗したためチャンネル投稿）\n" + text,
                    level=level,
                ),
                thread_ts=None,
                channel_id=channel_id,
            )
        if not ok and not desired_thread_ts and openclaw_slack_ready():
            route = "bot"
            ok = _post_slack_api(body_text, thread_ts=None, channel_id=channel_id)
        if not ok and _webhook_url():
            route = "webhook_fallback" if not desired_thread_ts else "webhook_channel_fallback"
            text_to_send = body_text
            if desired_thread_ts:
                text_to_send = _format_text(
                    "（スレッド投稿に失敗したためwebhookで通知）\n" + text,
                    level=level,
                )
            ok = _post_webhook(text_to_send)
    except Exception:
        _set_last_delivery(route=route, error="unexpected_exception")
        ok = False
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
                    "route": route,
                    "thread": bool(desired_thread_ts),
                    "channel": bool(channel_id or resolve_slack_channel_id()),
                    "text_hash": hashlib.sha256(body_text.encode()).hexdigest()[:16],
                    "ok": ok,
                    "error": last_delivery_error() or None,
                },
            )
    except Exception:
        pass
    if ok:
        _set_last_delivery(route=route, error="")
    return ok
