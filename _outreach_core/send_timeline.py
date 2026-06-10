"""Per-target send-process timeline (v17).

The send flow has many stages — open → page state → pre-gates (同意/種別) →
fill → validation fixes → first submit → confirm page → final submit →
completion verify. Failures used to be reported only as the LAST symptom
("送信ボタンが見つからない"), hiding the real cause (e.g. the page had no form
at all, or a validation bounce emptied the form).

This module is the pure record/format layer: `stage_send` appends entries at
each checkpoint and the formatted timeline rides along on escalations, traces,
and resolver-queue messages.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Render order / labels. Unknown stages render with their raw key.
STAGE_LABELS: dict[str, str] = {
    "open": "ページを開く",
    "page_state": "ページ状態（フォーム存在）",
    "url_recovery": "URL自動リカバリ",
    "entry_click": "入口クリック（法人タブ等）",
    "pre_gates": "事前ゲート（同意・種別・ラジオ・プルダウン）",
    "fill": "フォーム入力",
    "unexpected_fields": "想定外フィールド対応",
    "validation_fix": "バリデーション修正",
    "captcha": "CAPTCHA判定",
    "first_submit": "確認/送信ボタン押下",
    "confirm_page": "確認画面",
    "final_submit": "最終送信ボタン押下",
    "verify": "完了画面の検証",
}

_OK_MARK = {True: "✓", False: "✗", None: "−"}


def add(
    timeline: list[dict[str, Any]],
    stage: str,
    ok: bool | None,
    **detail: Any,
) -> list[dict[str, Any]]:
    """Append one stage record. ``ok=None`` means informational/not applicable."""
    timeline.append({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stage": str(stage),
        "ok": ok,
        "detail": {k: v for k, v in detail.items() if v not in (None, "", [], {})},
    })
    return timeline


def _detail_str(detail: dict[str, Any], limit: int = 160) -> str:
    parts = []
    for k, v in (detail or {}).items():
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "…"
        parts.append(f"{k}={s}")
    out = ", ".join(parts)
    return out[:limit]


def format_timeline(timeline: list[dict[str, Any]] | None) -> str:
    """Human-readable multi-line timeline (Japanese) for Slack/log output."""
    rows: list[str] = []
    for i, e in enumerate(timeline or [], 1):
        if not isinstance(e, dict):
            continue
        stage = str(e.get("stage") or "?")
        label = STAGE_LABELS.get(stage, stage)
        mark = _OK_MARK.get(e.get("ok"), "−")
        d = _detail_str(e.get("detail") or {})
        rows.append(f"　{i}. {mark} {label}" + (f" — {d}" if d else ""))
    return "\n".join(rows)


def first_failure(timeline: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The first stage with ok=False — the real root cause, not the last symptom."""
    for e in timeline or []:
        if isinstance(e, dict) and e.get("ok") is False:
            return e
    return None


def failure_headline(timeline: list[dict[str, Any]] | None) -> str:
    """One-line root-cause summary, e.g. 'ページ状態（フォーム存在）で失敗'."""
    f = first_failure(timeline)
    if not f:
        return ""
    label = STAGE_LABELS.get(str(f.get("stage")), str(f.get("stage")))
    d = _detail_str(f.get("detail") or {}, limit=120)
    return f"{label}で失敗" + (f"（{d}）" if d else "")
