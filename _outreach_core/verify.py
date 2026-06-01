"""
Post-send verification and needs_attention escalation.

Pure Python / DOM heuristics only — no oc_infer, no openclaw infer subprocess.
Escalation copy and user Q&A are handled by the OpenClaw agent (Opus 4.7).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

FORM_SUCCESS_KEYWORDS = (
    "送信完了",
    "送信を受け付け",
    "受付完了",
    "ありがとうございました",
    "お問い合わせありがとう",
    "お問い合わせを受け付け",
    "ご連絡ありがとう",
    "ご連絡",
    "完了しました",
    "完了画面",
    "送信が完了",
    "送信されました",
    "送信いたしました",
    "メッセージは送信",
    "THANKS",
    "thank you",
    "thank you for",
    "successfully submitted",
    "inquiry has been received",
    "we have received",
)

LINKEDIN_SUCCESS_KEYWORDS = (
    "message sent",
    "inmail sent",
    "successfully sent",
    "your message has been sent",
    "送信しました",
    "送信が完了",
    "メッセージを送信",
)

PAGE_EVIDENCE_JS = r"""
() => ({
  url: location.href,
  title: document.title || '',
  text: (document.body && document.body.innerText ? document.body.innerText : '').slice(0, 16000),
})
"""


def _norm(text: str) -> str:
    return (text or "").casefold()


def _text_has_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    hay = _norm(text)
    return any(_norm(k) in hay for k in keywords)


def _url_looks_like_success(url: str) -> bool:
    u = _norm(url)
    markers = (
        "thanks",
        "thank",
        "complete",
        "completed",
        "success",
        "done",
        "finish",
        "/thanks",
        "arigato",
        "kanryo",
    )
    return any(m in u for m in markers)
FORM_ERROR_KEYWORDS = (
    "入力エラー",
    "未入力",
    "正しく入力してください",
    "入力内容に誤り",
    "エラーがあります",
    "送信できませんでした",
    "送信に失敗",
)

PICK_TARGET_FORM_JS = r"""
() => {
  const forms = [...document.querySelectorAll('form')];
  const withTextarea = forms.filter(f => f.querySelector('textarea'));
  withTextarea.sort((a, b) =>
    b.querySelectorAll('input,select,textarea').length -
    a.querySelectorAll('input,select,textarea').length);
  return withTextarea[0] || forms[0] || document.body;
}
"""

_SCAN_REQUIRED_BODY = r"""
  const empty = [];
  for (const el of root.querySelectorAll('[required]')) {
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
"""

SCAN_REQUIRED_JS = (
    r"""
() => {
  const root = ("""
    + PICK_TARGET_FORM_JS.strip()
    + r""")();
"""
    + _SCAN_REQUIRED_BODY
    + "\n}\n"
)


def build_scan_required_js(target: dict[str, Any] | None = None) -> str:
    """Prefer enrich-time form_root_selector; fall back to textarea heuristic."""
    sel = (target or {}).get("form_root_selector")
    if not sel:
        return SCAN_REQUIRED_JS
    sel_lit = json.dumps(sel)
    return (
        r"""
() => {
  let root = document.querySelector("""
        + sel_lit
        + r""");
  if (!root) {
    root = ("""
        + PICK_TARGET_FORM_JS.strip()
        + r""")();
  }
"""
        + _SCAN_REQUIRED_BODY
        + "\n}\n"
    )


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


def _jp_form_success_confirmed(evidence: dict[str, Any]) -> bool:
    """Strong success: keyword on page AND (thanks URL OR no error banner)."""
    if not evidence.get("has_success_keyword"):
        return False
    if evidence.get("url_success"):
        return True
    return not evidence.get("has_error_keyword")


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
    verify_strict: bool = True,
) -> dict[str, Any]:
    """
    Returns dict with status: ok | uncertain | needs_attention, reason, evidence, etc.
    """
    name = target.get("name") or target.get("id", "?")
    evidence: dict[str, Any] = {}
    unresolved_fields: list[dict[str, Any]] = []

    if channel == "linkedin":
        snap = snapshot or ""
        page = browser_verify if isinstance(browser_verify, dict) else {}
        page_text = snap or str(page.get("text") or "")
        page_url = str(page.get("url") or "")
        evidence = {"browser_verify": browser_verify, "url": page_url or None}

        if page_text and _text_has_keyword(page_text, LINKEDIN_SUCCESS_KEYWORDS):
            return {
                "status": "ok",
                "reason": f"{name}: 送信成功メッセージをページ上で確認",
                "evidence": {**evidence, "matched": "linkedin_keywords"},
                "snapshot_path": str(snapshot_path) if snapshot_path else None,
                "unresolved_fields": None,
            }
        if page_url and _url_looks_like_success(page_url):
            return {
                "status": "ok",
                "reason": f"{name}: 送信後 URL を確認 ({page_url[:80]})",
                "evidence": {**evidence, "matched": "url"},
                "snapshot_path": str(snapshot_path) if snapshot_path else None,
                "unresolved_fields": None,
            }
        if browser_verify and browser_verify.get("sent"):
            return {
                "status": "ok",
                "reason": browser_verify.get("reason", "compose modal closed"),
                "evidence": evidence,
                "snapshot_path": str(snapshot_path) if snapshot_path else None,
                "unresolved_fields": None,
            }
        reason = (browser_verify or {}).get("reason", "送信完了を確認できません（モーダルが開いたまま等）")
        return {
            "status": "uncertain",
            "reason": f"{name}: {reason}",
            "evidence": evidence,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "unresolved_fields": None,
        }

    # jp_form (and generic form channel)
    snap = snapshot or ""
    page_url = ""
    if isinstance(browser_verify, dict):
        page_url = str(browser_verify.get("url") or "")
        extra = str(browser_verify.get("text") or "")
        if extra and extra not in snap:
            snap = f"{snap}\n{extra}"
    if page_url:
        evidence["url_success"] = _url_looks_like_success(page_url)
    if snap:
        evidence["has_success_keyword"] = _text_has_keyword(snap, FORM_SUCCESS_KEYWORDS)
        evidence["has_error_keyword"] = _text_has_keyword(snap, FORM_ERROR_KEYWORDS)

    # Success keyword wins over error keyword: many JP forms keep error-like
    # labels (e.g. 必須項目, 入力エラー再表示) visible alongside the success
    # confirmation message. If we see a clear "送信されました" type marker,
    # trust it. (GENDA 2026-05-31 false-positive.)
    if evidence.get("has_success_keyword"):
        return {
            "status": "ok",
            "reason": f"{name}: 送信完了画面を確認",
            "evidence": evidence,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "unresolved_fields": None,
        }

    if evidence.get("has_error_keyword"):
        return {
            "status": "needs_attention",
            "reason": f"{name}: 確認画面にエラーメッセージが検出されました",
            "evidence": evidence,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "unresolved_fields": None,
        }

    if _jp_form_success_confirmed(evidence):
        return {
            "status": "ok",
            "reason": f"{name}: 送信完了画面を確認",
            "evidence": evidence,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "unresolved_fields": None,
        }

    if verify_strict and evaluate_fn:
        scan = evaluate_fn(build_scan_required_js(target))
        if isinstance(scan, dict):
            empty = scan.get("empty_required") or []
            evidence["empty_required"] = empty
            for item in empty:
                if isinstance(item, dict):
                    unresolved_fields.append(item)

    plan_gaps = (
        _required_not_in_plan(target, plan or target.get("_llm_plan"))
        if verify_strict
        else []
    )
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

    if snap and not evidence.get("has_success_keyword"):
        hint = ""
        if page_url:
            hint = f" (url={page_url[:100]})"
        return {
            "status": "uncertain",
            "reason": f"{name}: 送信完了画面が確認できません{hint}",
            "evidence": evidence,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "unresolved_fields": None,
        }

    if browser_verify is not None and browser_verify.get("sent") is not None:
        ok = bool(browser_verify.get("sent"))
        return {
            "status": "ok" if ok else "uncertain",
            "reason": f"{name}: {browser_verify.get('reason', '')}",
            "evidence": {**evidence, "browser_verify": browser_verify},
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


def scan_empty_required(
    evaluate_fn: Callable[[str], Any],
    target: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return empty required fields inside the target form (scoped scan JS)."""
    scan = evaluate_fn(build_scan_required_js(target))
    if not isinstance(scan, dict):
        return []
    return [x for x in (scan.get("empty_required") or []) if isinstance(x, dict)]


def _field_key(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("label") or "").strip()


def scan_new_required_after_fill(
    evaluate_fn: Callable[[str], Any],
    *,
    baseline_empty_names: set[str],
    filled_names: set[str],
    target: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
  After a plan field is applied: return newly visible empty required fields
  (not in baseline, not already filled).
    """
    newly: list[dict[str, Any]] = []
    for item in scan_empty_required(evaluate_fn, target):
        key = _field_key(item)
        if not key or key in filled_names or key in baseline_empty_names:
            continue
        newly.append(item)
    return newly


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
        _emit_send_event(target, result, channel, "sent_ok")
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
        _emit_send_event(target, result, channel, "needs_attention", escalated=True)
        return "needs_attention"

    reason = str(result.get("reason") or "")
    prefix = f"{name}: "
    if reason.startswith(prefix):
        reason = reason[len(prefix) :]
    notify_post(f"{name} 送信完了が確認できません: {reason}", level="warn")
    _emit_send_event(target, result, channel, "uncertain")
    return "uncertain"


def _emit_send_event(
    target: dict[str, Any],
    result: dict[str, Any],
    channel: str,
    outcome: str,
    *,
    escalated: bool = False,
) -> None:
    try:
        from _outreach_core import events as ev

        if not ev.get_context().data_dir:
            return
        tid = str(target.get("id") or "")
        ev.emit(
            "send.verify.completed",
            stage="send",
            target_id=tid or None,
            outcome=outcome,
            payload={
                "status": result.get("status"),
                "reason": (result.get("reason") or "")[:200],
                "evidence_keys": list((result.get("evidence") or {}).keys()),
                "channel": channel,
            },
            trace_dir=result.get("snapshot_path"),
        )
        if escalated:
            ev.emit(
                "send.escalated",
                stage="send",
                target_id=tid or None,
                payload={
                    "field_count_unresolved": len(result.get("unresolved_fields") or []),
                    "slack_posted": True,
                },
            )
    except Exception:
        pass
