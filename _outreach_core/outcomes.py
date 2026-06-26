"""Canonical send outcome taxonomy (v28 P1).

Raw send states live in several layers: the submission loop, verify result,
needs_attention reasons, and timeline failures. This module is the single
normalization point used by events and reports.
"""

from __future__ import annotations

from typing import Any

from _outreach_core import send_timeline as timeline_mod

SENT = "sent"
SENT_UNVERIFIED = "sent_unverified"
VALIDATION_STUCK = "validation_stuck"
SUBMIT_INEFFECTIVE = "submit_ineffective"
NO_FORM = "no_form"
RECRUIT_MISCLASS = "recruit_misclass"
LOGIN_REQUIRED = "login_required"
CAPTCHA_BLOCKED = "captcha_blocked"
MULTISTEP_TOO_DEEP = "multistep_too_deep"
SHADOW_DOM_UNSEEN = "shadow_dom_unseen"
NETWORK_ERROR = "network_error"
SKIPPED_QUALITY = "skipped_quality"
CRASHED = "crashed"
UNKNOWN = "unknown"

OUTCOMES = (
    SENT,
    SENT_UNVERIFIED,
    VALIDATION_STUCK,
    SUBMIT_INEFFECTIVE,
    NO_FORM,
    RECRUIT_MISCLASS,
    LOGIN_REQUIRED,
    CAPTCHA_BLOCKED,
    MULTISTEP_TOO_DEEP,
    SHADOW_DOM_UNSEEN,
    NETWORK_ERROR,
    SKIPPED_QUALITY,
    CRASHED,
    UNKNOWN,
)

BUCKET = {
    SENT: "confirm",
    SENT_UNVERIFIED: "confirm",
    VALIDATION_STUCK: "fill",
    SUBMIT_INEFFECTIVE: "submit",
    NO_FORM: "list",
    RECRUIT_MISCLASS: "list",
    LOGIN_REQUIRED: "list",
    CAPTCHA_BLOCKED: "submit",
    MULTISTEP_TOO_DEEP: "submit",
    SHADOW_DOM_UNSEEN: "fill",
    NETWORK_ERROR: "system",
    SKIPPED_QUALITY: "list",
    CRASHED: "system",
    UNKNOWN: "system",
}

_LABEL_JA = {
    SENT: "送信確認済み",
    SENT_UNVERIFIED: "送信操作済み・確認弱い",
    VALIDATION_STUCK: "必須/バリデーション停止",
    SUBMIT_INEFFECTIVE: "送信クリック無効",
    NO_FORM: "問い合わせフォーム不在",
    RECRUIT_MISCLASS: "採用/非問い合わせ誤分類",
    LOGIN_REQUIRED: "ログイン必須",
    CAPTCHA_BLOCKED: "CAPTCHA/Cloudflare停止",
    MULTISTEP_TOO_DEEP: "多段フォーム上限超過",
    SHADOW_DOM_UNSEEN: "Shadow DOM未検出",
    NETWORK_ERROR: "ネットワーク/処理タイムアウト",
    SKIPPED_QUALITY: "リスト品質ゲート除外",
    CRASHED: "処理クラッシュ",
    UNKNOWN: "不明",
}


def outcome_bucket(code: str) -> str:
    return BUCKET.get(str(code or ""), BUCKET[UNKNOWN])


def outcome_label_ja(code: str) -> str:
    return _LABEL_JA.get(str(code or ""), _LABEL_JA[UNKNOWN])


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold()


def _blob(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, dict):
            parts.extend(str(v) for v in value.values())
        elif isinstance(value, list):
            parts.extend(str(v) for v in value)
        elif value is not None:
            parts.append(str(value))
    return " ".join(parts).casefold()


def _outcome_from_text(text: str) -> str | None:
    t = _norm(text)
    if not t:
        return None
    if any(x in t for x in ("recruit", "採用", "求人", "career", "新卒", "中途")):
        return RECRUIT_MISCLASS
    if any(x in t for x in ("login", "ログイン", "sign in", "signin", "会員")):
        return LOGIN_REQUIRED
    if any(x in t for x in ("captcha", "recaptcha", "turnstile", "cloudflare", "bot検知")):
        return CAPTCHA_BLOCKED
    if any(x in t for x in ("wizard_too_deep", "too_deep", "multi-step", "multistep", "多段")):
        return MULTISTEP_TOO_DEEP
    if any(x in t for x in ("validation", "バリデーション", "必須", "入力エラー", "unrecoverable")):
        return VALIDATION_STUCK
    if any(x in t for x in ("ineffective", "遷移しません", "click_failed", "submit button not found", "送信ボタン")):
        return SUBMIT_INEFFECTIVE
    if any(x in t for x in ("no_form", "page_has_no_form", "フォーム要素がページに存在しません", "textarea 無し")):
        return NO_FORM
    if any(x in t for x in ("shadow", "shadow_dom")):
        return SHADOW_DOM_UNSEEN
    if any(x in t for x in ("timeout", "timed_out", "network", "net::", "err_", "応答詰まり")):
        return NETWORK_ERROR
    if any(x in t for x in ("quality", "skipped_quality", "品質")):
        return SKIPPED_QUALITY
    if any(x in t for x in ("crash", "crashed", "exception", "traceback")):
        return CRASHED
    return None


def _outcome_from_timeline(timeline: list[dict[str, Any]] | None) -> str | None:
    failure = timeline_mod.first_failure(timeline)
    if not failure:
        return None
    stage = _norm(failure.get("stage"))
    detail = failure.get("detail") or {}
    text = _blob(stage, detail)
    by_text = _outcome_from_text(text)
    if by_text:
        return by_text
    if stage in ("page_state", "url_recovery"):
        return NO_FORM
    if stage in ("fill", "unexpected_fields", "validation_fix"):
        return VALIDATION_STUCK
    if stage in ("first_submit", "final_submit", "confirm_page"):
        return SUBMIT_INEFFECTIVE
    if stage == "captcha":
        return CAPTCHA_BLOCKED
    if stage == "verify":
        return SENT_UNVERIFIED
    return UNKNOWN


def classify_outcome(
    *,
    result_state: str | None,
    verify_verdict: str | None,
    timeline: list[dict[str, Any]] | None,
) -> str:
    """Normalize raw send result into one canonical v28 outcome.

    Priority: strong success > timeline first failure > raw result state.
    Unknown values never leak through; they become ``UNKNOWN``.
    """
    vv = _norm(verify_verdict)
    rs = _norm(result_state)

    if vv in ("sent_ok", "strong", "sent", "ok") or rs == "sent":
        return SENT

    from_timeline = _outcome_from_timeline(timeline)
    if from_timeline:
        return from_timeline

    raw_map = {
        "sent_unverified": SENT_UNVERIFIED,
        "weak": SENT_UNVERIFIED,
        "uncertain": SENT_UNVERIFIED,
        "unknown": UNKNOWN,
        "validation_stuck": VALIDATION_STUCK,
        "validation_unrecoverable": VALIDATION_STUCK,
        "ineffective": SUBMIT_INEFFECTIVE,
        "submit_click_ineffective": SUBMIT_INEFFECTIVE,
        "click_failed": SUBMIT_INEFFECTIVE,
        "first_submit_not_found": SUBMIT_INEFFECTIVE,
        "confirm_submit_not_found": SUBMIT_INEFFECTIVE,
        "no_form": NO_FORM,
        "page_has_no_form": NO_FORM,
        "lost_form": NO_FORM,
        "form_vanished_after_fill": NO_FORM,
        "recruit_misclass": RECRUIT_MISCLASS,
        "login_required": LOGIN_REQUIRED,
        "captcha_blocked": CAPTCHA_BLOCKED,
        "cloudflare_blocking": CAPTCHA_BLOCKED,
        "wizard_too_deep": MULTISTEP_TOO_DEEP,
        "multistep_too_deep": MULTISTEP_TOO_DEEP,
        "timed_out": NETWORK_ERROR,
        "network_error": NETWORK_ERROR,
        "skipped_quality": SKIPPED_QUALITY,
        "crashed": CRASHED,
    }
    if rs in raw_map:
        return raw_map[rs]
    by_text = _outcome_from_text(rs)
    if by_text:
        return by_text
    if rs in ("done", "skipped", "queued"):
        return UNKNOWN
    return UNKNOWN


def build_target_outcome_payload(
    *,
    target: dict[str, Any],
    result: dict[str, Any] | None,
    started_at: float,
    finished_at: float,
    timeline: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Build the payload for exactly-one ``send.target_outcome`` event."""
    res = result or {}
    verify_verdict = (
        res.get("verify_verdict")
        or res.get("send_verdict")
        or res.get("verify_status")
        or res.get("status")
    )
    result_state = " ".join(
        str(x or "")
        for x in (
            res.get("outcome"),
            res.get("state"),
            res.get("status"),
            res.get("reason"),
            res.get("error"),
        )
        if x
    )
    outcome = classify_outcome(
        result_state=str(result_state or ""),
        verify_verdict=str(verify_verdict or ""),
        timeline=timeline,
    )
    root_cause = timeline_mod.failure_headline(timeline)
    if not root_cause:
        root_cause = str(res.get("reason") or res.get("error") or "")
    return {
        "outcome": outcome,
        "bucket": outcome_bucket(outcome),
        "label": outcome_label_ja(outcome),
        "company": target.get("name") or target.get("company") or target.get("id"),
        "url": target.get("form_url") or target.get("url"),
        "root_cause": root_cause[:240],
        "elapsed_sec": max(0, int(round(float(finished_at) - float(started_at)))),
        "retries": int(res.get("retries") or res.get("clicks") or 0),
        "verify_verdict": verify_verdict,
        "raw_outcome": result_state,
    }
