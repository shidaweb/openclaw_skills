"""
Post-send verification and needs_attention escalation.

The PRIMARY verdict is pure Python / DOM heuristics (weighted evidence score,
v15 §V1). An LLM is consulted ONLY for the uncertain middle band, via an
injected ``infer_fn`` with a verbatim-quote hallucination guard (§V2) — it is
never the final arbiter over deterministic evidence.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from _outreach_core import send_state as core_send_state

FORM_SUCCESS_KEYWORDS = core_send_state.FORM_SUCCESS_KEYWORDS
FORM_ERROR_KEYWORDS = core_send_state.FORM_ERROR_KEYWORDS

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
() => {
  const visible = (el) => {
    if (!el) return false;
    if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') return false;
    const st = getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden';
	  };
	  let visibleForms = 0, visibleTextareas = 0, editableVisible = 0,
	      submitControls = 0, finalSubmitControls = 0;
	  const statuses = [];
	  const responseTexts = [];
  const addStatus = (el) => {
    if (!el) return;
    const cls = String(el.className || '');
    const status = String(el.getAttribute && (el.getAttribute('data-status') || el.getAttribute('status')) || '');
    const aria = String(el.getAttribute && (el.getAttribute('role') || el.getAttribute('aria-live')) || '');
    if (cls || status || aria) statuses.push(`${status} ${cls} ${aria}`.trim());
  };
  for (const el of document.querySelectorAll(
    [
      'form',
      '.wpcf7 form',
      'form.wpcf7-form',
      '.wpcf7-response-output',
      '[role="alert"]',
      '[role="status"]',
      '[aria-live]',
      '.success',
      '.complete',
      '.completed',
      '.thanks',
      '.thankyou',
      '.form-success',
      '.mw_wp_form_complete',
      '.alert-success',
      '.error',
      '.form-error',
      '.mw_wp_form_error',
      '.alert-danger'
    ].join(',')
	  )) {
	    addStatus(el);
	    if (visible(el) && el.matches && !el.matches('form')) {
	      const t = String(el.textContent || '').trim();
	      if (t) responseTexts.push(t.slice(0, 300));
	    }
	  }
	  for (const f of document.querySelectorAll('form')) if (visible(f)) visibleForms++;
	  const isEditableType = (el) => {
	    const t = (el.type || '').toLowerCase();
	    return !['hidden', 'submit', 'button', 'image', 'file', 'checkbox', 'radio'].includes(t);
	  };
	  for (const el of document.querySelectorAll('input, textarea, select')) {
	    if (!visible(el)) continue;
	    if (el.tagName === 'TEXTAREA') visibleTextareas++;
	    if (el.tagName === 'INPUT' && !isEditableType(el)) continue;
	    if (el.disabled || el.readOnly) continue;
	    editableVisible++;
	  }
	  const finalSubmitRe = /送信|送る|問い合わせ|お問い合わせ|submit|send|confirm|確定|完了/i;
	  for (const el of document.querySelectorAll(
	    'button, input[type="submit"], input[type="button"], input[type="image"], [role="button"]'
	  )) {
	    if (!visible(el)) continue;
	    submitControls++;
	    const label = String(el.innerText || el.value || el.getAttribute('aria-label') || el.textContent || '');
	    if (finalSubmitRe.test(label)) finalSubmitControls++;
	  }
	  if (document.body) statuses.push(String(document.body.className || ''));
  const joined = statuses.join(' ').toLowerCase();
  const responseText = responseTexts.join('\n').slice(0, 600);
  return {
    url: location.href,
	    title: document.title || '',
	    text: (document.body && document.body.innerText ? document.body.innerText : '').slice(0, 16000),
	    visible_forms: visibleForms,
	    visible_textareas: visibleTextareas,
	    editable_visible: editableVisible,
	    submit_controls: submitControls,
	    final_submit_controls: finalSubmitControls,
	    cf7_statuses: statuses,
    cf7_response_text: responseText,
    cf7_sent: /\bsent\b|mail[_-]?sent|wpcf7-mail-sent-ok/.test(joined),
    cf7_invalid: /\binvalid\b|spam|failed|aborted|unaccepted/.test(joined),
    submission_statuses: statuses,
    submission_status_text: responseText,
    submission_sent: /(^|[\s_-])(sent|success|succeeded|complete|completed|submitted|thanks|thankyou|mw[_-]?wp[_-]?form[_-]?complete)([\s_-]|$)/i.test(joined),
    submission_invalid: /(^|[\s_-])(invalid|error|failed|failure|spam|aborted|unaccepted|mw[_-]?wp[_-]?form[_-]?error)([\s_-]|$)/i.test(joined),
  };
}
"""

# v15 §V3 — form presence must be judged by VISIBILITY, not DOM existence.
# A form hidden with display:none while a "確認中…" spinner runs must not be
# counted as "gone" (false sent_ok) nor a hidden leftover as "still present".
FORM_VISIBILITY_JS = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') return false;
    const st = getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden';
  };
	  let visibleForms = 0, visibleTextareas = 0, editableVisible = 0,
	      submitControls = 0, finalSubmitControls = 0;
	  for (const f of document.querySelectorAll('form')) if (visible(f)) visibleForms++;
	  const isEditableType = (el) => {
	    const t = (el.type || '').toLowerCase();
	    return !['hidden', 'submit', 'button', 'image', 'file', 'checkbox', 'radio'].includes(t);
	  };
	  for (const el of document.querySelectorAll('input, textarea, select')) {
	    if (!visible(el)) continue;
	    if (el.tagName === 'TEXTAREA') visibleTextareas++;
	    if (el.tagName === 'INPUT' && !isEditableType(el)) continue;
	    if (el.disabled || el.readOnly) continue;
	    editableVisible++;
	  }
	  const finalSubmitRe = /送信|送る|問い合わせ|お問い合わせ|submit|send|confirm|確定|完了/i;
	  for (const el of document.querySelectorAll(
	    'button, input[type="submit"], input[type="button"], input[type="image"], [role="button"]'
	  )) {
	    if (!visible(el)) continue;
	    submitControls++;
	    const label = String(el.innerText || el.value || el.getAttribute('aria-label') || el.textContent || '');
	    if (finalSubmitRe.test(label)) finalSubmitControls++;
	  }
  const statuses = [];
  for (const el of document.querySelectorAll(
    'form, .wpcf7 form, form.wpcf7-form, [role="alert"], [role="status"], [aria-live], .success, .complete, .completed, .thanks, .thankyou, .form-success, .mw_wp_form_complete, .alert-success, .error, .form-error, .mw_wp_form_error, .alert-danger'
  )) {
    const cls = String(el.className || '');
    const status = String(el.getAttribute && (el.getAttribute('data-status') || el.getAttribute('status')) || '');
    const aria = String(el.getAttribute && (el.getAttribute('role') || el.getAttribute('aria-live')) || '');
    if (cls || status || aria) statuses.push(`${status} ${cls} ${aria}`.trim());
  }
  if (document.body) statuses.push(String(document.body.className || ''));
  const joined = statuses.join(' ').toLowerCase();
  return {
	    visible_forms: visibleForms,
	    visible_textareas: visibleTextareas,
	    editable_visible: editableVisible,
	    submit_controls: submitControls,
	    final_submit_controls: finalSubmitControls,
	    cf7_statuses: statuses,
    cf7_sent: /\bsent\b|mail[_-]?sent|wpcf7-mail-sent-ok/.test(joined),
    cf7_invalid: /\binvalid\b|spam|failed|aborted|unaccepted/.test(joined),
    submission_statuses: statuses,
    submission_sent: /(^|[\s_-])(sent|success|succeeded|complete|completed|submitted|thanks|thankyou|mw[_-]?wp[_-]?form[_-]?complete)([\s_-]|$)/i.test(joined),
    submission_invalid: /(^|[\s_-])(invalid|error|failed|failure|spam|aborted|unaccepted|mw[_-]?wp[_-]?form[_-]?error)([\s_-]|$)/i.test(joined),
  };
}
"""


def _norm(text: str) -> str:
    return (text or "").casefold()


def _text_has_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    hay = _norm(text)
    return any(_norm(k) in hay for k in keywords)


def _url_looks_like_success(url: str) -> bool:
    return core_send_state.url_looks_like_success(url)

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
    try:
        from _outreach_core.notify import post_problem

        post_problem("needs_attention", row, row)
    except Exception:
        pass
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
    """Strong success according to the shared send-state verdict."""
    return bool(core_send_state.assess_submission_result(evidence).get("sent"))


# --- v15 §V1: weighted evidence scoring (pure) -------------------------------
def score_send_evidence(evidence: dict[str, Any]) -> int:
    """Weighted score over post-submit signals (§V1 table).

    Delegates to ``send_state`` so CF7, MW WP Form, aria-live alerts, generic
    success/error blocks, URL transitions, and visible-form residue are judged
    by one shared policy.
    """
    return int(core_send_state.assess_submission_result(evidence).get("score") or 0)


def verdict_from_score(score: int) -> str:
    """≥3 → sent_ok, ≤−2 → failed, otherwise uncertain (§V1)."""
    if score >= 3:
        return "sent_ok"
    if score <= -2:
        return "failed"
    return "uncertain"


def _record_submission_result(evidence: dict[str, Any]) -> dict[str, Any]:
    """Attach the shared send-state verdict fields to evidence and return it."""
    result = core_send_state.assess_submission_result(evidence)
    evidence["score"] = result["score"]
    evidence["send_verdict"] = result["verdict"]
    evidence["send_reason"] = result["reason"]
    evidence["send_signals"] = result["signals"]
    evidence["has_success_keyword"] = result["has_success_keyword"]
    evidence["has_error_keyword"] = result["has_error_keyword"]
    evidence["url_success"] = result["url_success"]
    evidence["form_gone_visible"] = result["form_gone_visible"]
    evidence["form_still_present"] = result["form_still_present"]
    evidence["explicit_sent"] = result["explicit_sent"]
    evidence["explicit_invalid"] = result["explicit_invalid"]
    return result


# --- v15 §V2: LLM tiebreak for uncertain verdicts (with hallucination guard) -
def llm_tiebreak_verdict(
    page_text: str,
    page_url: str,
    *,
    infer_fn: Callable[[str, str], str | None],
    model: str = "",
) -> dict[str, Any] | None:
    """Ask the LLM ONLY for uncertain cases; the primary verdict stays deterministic.

    Returns {"verdict": "sent"|"not_sent"|"unclear", "quote": ...} or None on
    any failure. A quote that does not literally appear in the page text is
    rejected (hallucination guard).
    """
    prompt = (
        "A Japanese inquiry form was just submitted in a browser. Based ONLY on "
        "the page evidence below, decide whether the submission completed.\n\n"
        f"## URL\n{page_url[:300]}\n\n"
        f"## Page text (head)\n{(page_text or '')[:4000]}\n\n"
        "## Output — STRICT JSON only\n"
        '{"verdict": "sent|not_sent|unclear", "quote": "<verbatim sentence from '
        'the page text that supports your verdict>"}\n'
        "If nothing on the page clearly indicates success or failure, answer unclear.\n"
    )
    try:
        raw = infer_fn(prompt, model)
    except Exception:
        return None
    return parse_llm_tiebreak(raw, page_text)


def parse_llm_tiebreak(raw: str | None, page_text: str) -> dict[str, Any] | None:
    """Parse + guard the tiebreak JSON (pure). Quote must appear in page text."""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in ("sent", "not_sent", "unclear"):
        return None
    quote = str(data.get("quote") or "").strip()
    if verdict in ("sent", "not_sent"):
        # Hallucination guard: the supporting quote must literally exist on the
        # page (whitespace-normalized). No quote → downgrade to unclear.
        norm = lambda s: re.sub(r"\s+", "", s or "")  # noqa: E731
        if not quote or norm(quote) not in norm(page_text):
            return None
    return {"verdict": verdict, "quote": quote}


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
    infer_fn: Callable[[str, str], str | None] | None = None,
    tiebreak_model: str = "",
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
        cf7_extra = str(browser_verify.get("cf7_response_text") or "")
        submission_extra = str(browser_verify.get("submission_status_text") or "")
        if extra and extra not in snap:
            snap = f"{snap}\n{extra}"
        if cf7_extra and cf7_extra not in snap:
            snap = f"{snap}\n{cf7_extra}"
        if submission_extra and submission_extra not in snap:
            snap = f"{snap}\n{submission_extra}"
        for key in (
            "visible_forms",
            "visible_textareas",
            "editable_visible",
            "submit_controls",
            "final_submit_controls",
            "probe_text_hits",
            "probe_field_hits",
            "form_gone_visible",
            "form_still_present",
            "has_success_keyword",
            "has_error_keyword",
            "cf7_sent",
            "cf7_invalid",
            "cf7_statuses",
            "cf7_response_text",
            "submission_sent",
            "submission_invalid",
            "submission_statuses",
            "submission_status_text",
        ):
            if key in browser_verify:
                evidence[key] = browser_verify.get(key)
    if page_url:
        evidence["url"] = page_url
        evidence["url_success"] = _url_looks_like_success(page_url)
    if snap:
        evidence["text"] = snap[:8000]
        evidence["has_success_keyword"] = _text_has_keyword(snap, FORM_SUCCESS_KEYWORDS)
        evidence["has_error_keyword"] = _text_has_keyword(snap, FORM_ERROR_KEYWORDS)

    submission = _record_submission_result(evidence)

    # Guardrail: if an explicit input error banner is present and we don't also
    # have strong success evidence, treat as failure (Benesse false-positive).
    if (
        evidence.get("has_error_keyword")
        and not submission["sent"]
        and not submission.get("explicit_sent")
    ):
        return {
            "status": "needs_attention",
            "reason": f"{name}: 確認画面にエラーメッセージが検出されました",
            "evidence": evidence,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "unresolved_fields": None,
        }

    if submission["sent"]:
        return {
            "status": "ok",
            "reason": f"{name}: 送信完了を確認 ({submission['reason']}, score={submission['score']})",
            "evidence": evidence,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "unresolved_fields": None,
        }

    # v15 §V3: form visibility signals (visibility-based, not DOM existence)
    if evaluate_fn:
        vis = evaluate_fn(FORM_VISIBILITY_JS)
        if isinstance(vis, dict):
            visible_forms = int(vis.get("visible_forms") or 0)
            visible_textareas = int(vis.get("visible_textareas") or 0)
            editable_visible = int(vis.get("editable_visible") or 0)
            submit_controls = int(vis.get("submit_controls") or 0)
            final_submit_controls = int(vis.get("final_submit_controls") or 0)
            evidence["visible_forms"] = visible_forms
            evidence["visible_textareas"] = visible_textareas
            evidence["editable_visible"] = editable_visible
            evidence["submit_controls"] = submit_controls
            evidence["final_submit_controls"] = final_submit_controls
            evidence["form_gone_visible"] = (
                visible_forms == 0 and visible_textareas == 0
            )
            evidence["form_still_present"] = visible_textareas > 0
            for key in (
                "cf7_sent",
                "cf7_invalid",
                "cf7_statuses",
                "submission_sent",
                "submission_invalid",
                "submission_statuses",
                "submission_status_text",
                "editable_visible",
                "submit_controls",
                "final_submit_controls",
            ):
                if key in vis and key not in evidence:
                    evidence[key] = vis.get(key)

    # v15 §V1: weighted score settles cases keywords alone could not.
    submission = _record_submission_result(evidence)
    score = int(submission["score"])
    if submission["verdict"] == "sent_ok":
        return {
            "status": "ok",
            "reason": f"{name}: 送信完了をスコア判定で確認 (score={score})",
            "evidence": evidence,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "unresolved_fields": None,
        }
    if submission["verdict"] == "failed":
        return {
            "status": "needs_attention",
            "reason": f"{name}: 送信失敗の証拠が優勢 (score={score})",
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
        # v15 §V2: uncertain ONLY → LLM tiebreak (primary verdict stays
        # deterministic; quote must literally appear on the page).
        if infer_fn:
            tb = llm_tiebreak_verdict(
                snap, page_url, infer_fn=infer_fn, model=tiebreak_model
            )
            evidence["llm_tiebreak"] = tb
            if tb and tb["verdict"] == "sent":
                return {
                    "status": "ok",
                    "reason": f"{name}: LLM tiebreak=sent 「{tb['quote'][:60]}」",
                    "evidence": evidence,
                    "snapshot_path": str(snapshot_path) if snapshot_path else None,
                    "unresolved_fields": None,
                }
            if tb and tb["verdict"] == "not_sent":
                return {
                    "status": "needs_attention",
                    "reason": f"{name}: LLM tiebreak=not_sent 「{tb['quote'][:60]}」",
                    "evidence": evidence,
                    "snapshot_path": str(snapshot_path) if snapshot_path else None,
                    "unresolved_fields": None,
                }
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
    record_attention: bool = True,
) -> str:
    """
    Persist needs_attention + notify unless record_attention is false.
    Returns outcome: sent_ok | needs_attention | uncertain.
    """
    from _outreach_core.notify import post as notify_post

    name = target.get("name") or target.get("id", "?")
    status = result.get("status")

    if status == "ok":
        notify_post(f"{name} 送信完了", level="info")
        _emit_send_event(target, result, channel, "sent_ok")
        return "sent_ok"

    if record_attention:
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
        _emit_send_event(target, result, channel, "needs_attention", escalated=True)
        return "needs_attention"

    reason = str(result.get("reason") or "")
    prefix = f"{name}: "
    if reason.startswith(prefix):
        reason = reason[len(prefix) :]
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
        evidence = result.get("evidence") or {}
        draft = target.get("draft") or {}
        ev.emit(
            "send.verify.completed",
            stage="send",
            target_id=tid or None,
            outcome=outcome,
            payload={
                "status": result.get("status"),
                "reason": (result.get("reason") or "")[:200],
                "evidence_keys": list(evidence.keys()),
                "send_verdict": evidence.get("send_verdict"),
                "send_score": evidence.get("score"),
                "send_reason": evidence.get("send_reason"),
                "send_signals": evidence.get("send_signals") or [],
                "name": target.get("name") or target.get("company"),
                "subject": draft.get("subject") or target.get("subject") or "",
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
