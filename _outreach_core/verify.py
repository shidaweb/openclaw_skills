"""Post-send verification and needs_attention escalation."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

FORM_SUCCESS_KEYWORDS = (
    "送信完了",
    "ありがとうございました",
    "ご連絡",
    "完了画面",
    "THANKS",
    "thank you",
    "お問い合わせを受け付け",
)
FORM_ERROR_KEYWORDS = (
    "入力エラー",
    "未入力",
    "正しく入力してください",
    "必須項目",
    "入力内容に誤り",
    "error",
    "エラーがあります",
)

SCAN_REQUIRED_JS = r"""
() => {
  const empty = [];
  for (const el of document.querySelectorAll('[required]')) {
    let isEmpty = false;
    const tag = el.tagName;
    if (tag === 'SELECT') isEmpty = !el.value;
    else if (el.type === 'checkbox' || el.type === 'radio') isEmpty = !el.checked;
    else isEmpty = !(el.value || '').trim();
    if (isEmpty) {
      let label = el.name || el.id || '';
      const lab = el.labels && el.labels[0];
      if (lab) label = lab.textContent.trim() || label;
      empty.push({
        name: el.name || el.id,
        type: (el.type || tag).toLowerCase(),
        label: String(label).slice(0, 120),
      });
    }
  }
  return { empty_required: empty };
}
"""


def needs_attention_path(data_dir: Path) -> Path:
    return data_dir / "needs_attention.jsonl"


def append_needs_attention(data_dir: Path, entry: dict[str, Any]) -> Path:
    path = needs_attention_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {**entry, "recorded_at": datetime.utcnow().isoformat() + "Z", "status": "open"}
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def close_needs_attention(data_dir: Path, target_id: str, *, resolution: str) -> bool:
    path = needs_attention_path(data_dir)
    if not path.exists():
        return False
    closed = False
    out_lines: list[str] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("target_id") == target_id and entry.get("status") == "open":
            entry["status"] = "closed"
            entry["closed_at"] = datetime.utcnow().isoformat() + "Z"
            entry["resolution"] = resolution
            closed = True
        out_lines.append(json.dumps(entry, ensure_ascii=False))
    if closed:
        path.write_text("\n".join(out_lines) + ("\n" if out_lines else ""))
    return closed


def list_open_needs_attention(data_dir: Path) -> list[dict[str, Any]]:
    path = needs_attention_path(data_dir)
    if not path.exists():
        return []
    open_rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("status") == "open":
            open_rows.append(entry)
    return open_rows


def _plan_field_names(plan: dict[str, Any] | None) -> set[str]:
    if not plan:
        return set()
    names: set[str] = set()
    for field in plan.get("fields") or []:
        if isinstance(field, dict) and field.get("name"):
            names.add(str(field["name"]))
    return names


def _required_not_in_plan(target: dict[str, Any], plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    form_fields = target.get("form_fields") or {}
    planned = _plan_field_names(plan)
    unresolved: list[dict[str, Any]] = []
    for group_key, type_label in (
        ("inputs", "text"),
        ("selects", "select"),
        ("textareas", "textarea"),
    ):
        for item in form_fields.get(group_key) or []:
            if not isinstance(item, dict):
                continue
            if not item.get("required"):
                continue
            name = item.get("name") or item.get("id") or ""
            if name and name not in planned:
                unresolved.append(
                    {
                        "name": name,
                        "type": type_label,
                        "label": item.get("label") or name,
                    }
                )
    return unresolved


def verify_send_completed(
    target: dict[str, Any],
    channel: str,
    *,
    snapshot: str | None = None,
    browser_verify: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    evaluate_fn: Callable[[str], Any] | None = None,
    data_dir: Path | None = None,
    snapshot_path: Path | None = None,
) -> dict[str, Any]:
    """
    Returns dict with status: ok | uncertain | needs_attention, reason, evidence, etc.
    """
    name = target.get("name") or target.get("id", "?")
    evidence: dict[str, Any] = {}
    unresolved_fields: list[dict[str, Any]] = []

    if channel == "linkedin":
        if browser_verify and browser_verify.get("sent"):
            return {
                "status": "ok",
                "reason": browser_verify.get("reason", "compose modal closed"),
                "evidence": browser_verify,
                "snapshot_path": str(snapshot_path) if snapshot_path else None,
                "unresolved_fields": None,
            }
        return {
            "status": "uncertain",
            "reason": (browser_verify or {}).get("reason", "送信完了を確認できません"),
            "evidence": browser_verify or {},
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "unresolved_fields": None,
        }

    # jp_form (and generic form channel)
    snap = snapshot or ""
    if snap:
        evidence["has_success_keyword"] = any(k in snap for k in FORM_SUCCESS_KEYWORDS)
        evidence["has_error_keyword"] = any(k in snap for k in FORM_ERROR_KEYWORDS)
        if evidence["has_error_keyword"]:
            return {
                "status": "needs_attention",
                "reason": f"{name}: 確認画面にエラーメッセージが検出されました",
                "evidence": evidence,
                "snapshot_path": str(snapshot_path) if snapshot_path else None,
                "unresolved_fields": unresolved_fields or None,
            }

    if evaluate_fn:
        scan = evaluate_fn(SCAN_REQUIRED_JS)
        if isinstance(scan, dict):
            empty = scan.get("empty_required") or []
            evidence["empty_required"] = empty
            for item in empty:
                if isinstance(item, dict):
                    unresolved_fields.append(item)

    plan_gaps = _required_not_in_plan(target, plan or target.get("_llm_plan"))
    for g in plan_gaps:
        if g not in unresolved_fields:
            unresolved_fields.append(g)
    if plan_gaps:
        evidence["plan_gaps"] = plan_gaps

    if unresolved_fields:
        labels = ", ".join(
            f"{f.get('type', '?')}: {f.get('label') or f.get('name')}" for f in unresolved_fields[:6]
        )
        return {
            "status": "needs_attention",
            "reason": f"{name}: 想定外の必須入力項目 [{labels}]",
            "evidence": evidence,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "unresolved_fields": unresolved_fields,
        }

    if snap and any(k in snap for k in FORM_SUCCESS_KEYWORDS):
        return {
            "status": "ok",
            "reason": f"{name}: 送信完了画面を確認",
            "evidence": evidence,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "unresolved_fields": None,
        }

    if snap and not evidence.get("has_success_keyword"):
        return {
            "status": "uncertain",
            "reason": f"{name}: 送信完了画面が確認できません",
            "evidence": evidence,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "unresolved_fields": None,
        }

    if browser_verify is not None:
        ok = bool(browser_verify.get("sent"))
        return {
            "status": "ok" if ok else "uncertain",
            "reason": browser_verify.get("reason", ""),
            "evidence": browser_verify,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "unresolved_fields": None,
        }

    return {
        "status": "uncertain",
        "reason": f"{name}: 検証データ不足",
        "evidence": evidence,
        "snapshot_path": str(snapshot_path) if snapshot_path else None,
        "unresolved_fields": None,
    }


def handle_verify_result(
    target: dict[str, Any],
    result: dict[str, Any],
    data_dir: Path,
    *,
    channel: str,
) -> str:
    """
    Persist needs_attention + notify. Returns outcome: sent_ok | needs_attention | uncertain.
    """
    from _outreach_core.notify import post as notify_post

    name = target.get("name") or target.get("id", "?")
    status = result.get("status")

    if status == "ok":
        notify_post(f"{name} 送信完了", level="info")
        return "sent_ok"

    entry = {
        "target_id": target.get("id"),
        "name": name,
        "channel": channel,
        "reason": result.get("reason"),
        "unresolved_fields": result.get("unresolved_fields"),
        "snapshot_path": result.get("snapshot_path"),
        "evidence": result.get("evidence"),
    }
    append_needs_attention(data_dir, entry)

    if status == "needs_attention":
        fields = result.get("unresolved_fields") or []
        field_txt = ", ".join(
            f"{f.get('type', '?')}: {f.get('label') or f.get('name')}" for f in fields[:5]
        )
        notify_post(f"{name} 想定外の入力項目: {field_txt}", level="warn")
        return "needs_attention"

    notify_post(f"{name} 送信完了が確認できません: {result.get('reason', '')}", level="warn")
    return "uncertain"
