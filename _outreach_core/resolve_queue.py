"""
Resolver queue + actionable escalation messages (v6 §16).

Problem (from production Slack logs): when a target hit a blocker the bot posted

    「株式会社X: 送信ボタンがDOMで特定できず停止。Slack で「x 進めて」と返すと手動再開できます」

…but replying "進めて" just retries the *same* detection that already failed, so
the human had no real action to take ("対応のしようがない"). Also each blocker felt
like it stopped the task.

This module fixes the *information* and *routing* sides:

  1. **Rich, structured diagnostics** per blocked target (enumerated buttons, URL,
     snapshot path, patterns tried) so a human OR an automated resolver can act.
  2. **Clear, honest messages** that say what was detected and what is actually
     wanted — and stop instructing the user to type "進めて" when it won't help.
  3. A durable **queue** of blocked targets that a *separate* resolver process /
     subagent drains (deep re-analysis), so the main batch never blocks on one
     hard form.

Pure file/string logic — unit-tested. The deep resolution itself (browser work)
lives in run.py; this module only stores the queue and formats messages.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_QUEUE_FILENAME = "resolve_queue.jsonl"

# Blocker reason classes a deep resolver can plausibly fix on a second, harder pass.
RESOLVABLE_REASONS = {
    "first_submit_not_found",
    "confirm_submit_not_found",
    "submit_button_not_found",
    "wrong_form_type",
}


def queue_path(data_dir: Path) -> Path:
    return Path(data_dir) / _QUEUE_FILENAME


def enqueue(data_dir: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Append a blocked-target entry. De-dupes by target_id (latest wins)."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    tid = str(entry.get("target_id") or entry.get("id") or "")
    rows = [r for r in read_queue(data_dir) if str(r.get("target_id")) != tid]
    record = dict(entry)
    record["target_id"] = tid
    record.setdefault("queued_at", datetime.now(timezone.utc).isoformat())
    record.setdefault("status", "pending")
    rows.append(record)
    _write(data_dir, rows)
    return record


def read_queue(data_dir: Path, *, status: str | None = None) -> list[dict[str, Any]]:
    path = queue_path(data_dir)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if status is None or row.get("status") == status:
            out.append(row)
    return out


def pending(data_dir: Path) -> list[dict[str, Any]]:
    return read_queue(data_dir, status="pending")


def mark(data_dir: Path, target_id: str, status: str, *, note: str = "") -> bool:
    """Update one entry's status (e.g. 'resolved' / 'skipped' / 'failed')."""
    rows = read_queue(data_dir)
    hit = False
    for r in rows:
        if str(r.get("target_id")) == str(target_id):
            r["status"] = status
            r["resolved_at"] = datetime.now(timezone.utc).isoformat()
            if note:
                r["note"] = note
            hit = True
    if hit:
        _write(Path(data_dir), rows)
    return hit


def remove(data_dir: Path, target_id: str) -> None:
    rows = [r for r in read_queue(data_dir) if str(r.get("target_id")) != str(target_id)]
    _write(Path(data_dir), rows)


def clear(data_dir: Path) -> None:
    queue_path(Path(data_dir)).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------

_REASON_LABEL = {
    "first_submit_not_found": "確認/送信ボタンを自動特定できません（最初の送信ボタン）",
    "confirm_submit_not_found": "確認画面の最終送信ボタンを自動特定できません",
    "submit_button_not_found": "送信ボタンを自動特定できません",
    "wrong_form_type": "想定と異なるフォーム種別を検出（誤フォームの可能性）",
    "page_has_no_form": "ページにフォームが存在しません（URL要再精査：リダイレクト/案内ページ/閉鎖の可能性）",
    "form_vanished_after_fill": "入力後にフォームが消失（バリデーション差し戻し/セッション切れの可能性）",
    "submit_gate_unsatisfied": "送信ゲート（同意・必須選択）が未充足のまま送信ボタンに到達できません",
    "wizard_too_deep": "多段フォームのステップ数が上限を超えました",
}


def humanize_reason(reason_class: str) -> str:
    return _REASON_LABEL.get(reason_class, reason_class)


def build_actionable_message(entry: dict[str, Any], *, auto_resolver: bool) -> str:
    """A message that states the detection detail AND what is actually wanted.

    Crucially it does NOT tell the user to type "進めて" (which only retries the
    same failing detection). It says the resolver is handling it and offers a
    real human option (skip / manual), so an unattended run needs no reply.
    """
    name = entry.get("name", "?")
    tid = entry.get("target_id", "?")
    reason_class = entry.get("reason_class", entry.get("reason", "?"))
    diag = entry.get("diagnostics") or {}
    url = diag.get("url") or entry.get("form_url") or ""
    buttons = diag.get("buttons") or []
    btn_preview = "、".join(f"「{b}」" for b in buttons[:5]) if buttons else "（候補ボタンなし）"
    snap = diag.get("snapshot_path") or ""
    shot = diag.get("screenshot_path") or ""

    lines = [
        f"⚠️ {name}（{tid}）: {humanize_reason(reason_class)}",
        f"　検知詳細: {entry.get('reason', reason_class)}",
        f"　URL: {url}",
        f"　ページ内ボタン候補（{len(buttons)}）: {btn_preview}",
    ]
    # v17: show WHERE in the send process it failed, not just the last symptom.
    timeline = diag.get("timeline")
    if isinstance(timeline, list) and timeline:
        from _outreach_core import send_timeline as tl

        headline = tl.failure_headline(timeline)
        if headline:
            lines.append(f"　根本原因: {headline}")
        lines.append("　プロセスログ:")
        lines.append(tl.format_timeline(timeline))
    if shot:
        lines.append(f"　スクリーンショット: {shot}")
    if snap:
        lines.append(f"　スナップショット: {snap}")
    if auto_resolver:
        lines.append("　→ 自動リゾルバに登録しました。本体バッチは止めず継続し、"
                     "完了後に別プロセスが深掘り再試行します（返信不要）。")
    else:
        lines.append("　→ リゾルバキューに登録しました。`run.py resolve-queue --brief <id>` "
                     "で別プロセスが深掘り再試行できます。")
    lines.append(f"　不要ならスキップ: 「{tid} skip」。手動で送るならURLを開いて確認。")
    return "\n".join(lines)


def queue_summary(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "リゾルバキューは空です。"
    by_reason: dict[str, int] = {}
    for e in entries:
        rc = e.get("reason_class", "?")
        by_reason[rc] = by_reason.get(rc, 0) + 1
    parts = "、".join(f"{humanize_reason(k)}×{v}" for k, v in by_reason.items())
    names = "、".join(e.get("name", "?") for e in entries[:10])
    return f"保留 {len(entries)} 件（{parts}）: {names}{' …' if len(entries) > 10 else ''}"


def _write(data_dir: Path, rows: list[dict[str, Any]]) -> None:
    path = queue_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
