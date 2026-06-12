"""v24 — pure helpers for the closed-loop submission driver (§S3).

The legacy send path was an open-loop script: fill → click first → (assume the
confirm page appeared) → click final → verify. Page state was observed only at
the very start and the very end, so any mid-flight divergence — a validation
bounce, a native ``confirm()`` dialog silently cancelling the submit, a
single/confirm flow misclassification, a stale textarea-scoped form root on the
confirm page — surfaced minutes later as a misleading verify failure and a
needs_attention escalation.

This module provides the OBSERVE half of the new observe → classify → act loop:

- ``DIALOG_AUTOACCEPT_JS`` / ``READ_DIALOG_LOG_JS`` — arm ``window.confirm`` /
  ``window.alert`` auto-accept on the live page (CDP and Playwright both cancel
  unhandled dialogs by default, which silently aborts ``onclick="return
  confirm(...)"`` submits — the historical 「事前確認/最終確認で止まる」 class).
- ``build_probes`` / ``evidence_js`` — one-shot JS that returns everything the
  classifier needs, including whether OUR OWN submitted values appear in
  editable fields (input page) vs. echoed as plain text (confirm page).
- ``classify_send_state`` — deterministic page-state verdict:
  ``input | validation_error | confirm | done | no_form``.
- ``page_fingerprint`` — cheap transition detector so a click that did nothing
  (cancelled dialog, dead button) is caught immediately, not at verify time.

Everything here is pure (no browser I/O) and unit-testable.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SEND_STATES = ("input", "validation_error", "confirm", "done", "no_form")

# Armed once per page; navigation resets it, so the driver re-arms before every
# action. The log survives within a page so diagnostics can show WHAT the page
# asked (e.g. 「送信しますか？」) when a stall is escalated.
DIALOG_AUTOACCEPT_JS = r"""
() => {
  if (window.__ocDialogArmed) return { armed: true, already: true };
  window.__ocDialogLog = window.__ocDialogLog || [];
  const log = (kind, msg) => {
    try {
      window.__ocDialogLog.push({ kind, msg: String(msg == null ? '' : msg).slice(0, 200) });
    } catch (e) {}
  };
  const isNative = (fn) => {
    try { return /\[native code\]/.test(Function.prototype.toString.call(fn)); }
    catch (e) { return false; }
  };
  // Some JP sites define their OWN global confirm(form) that validates and
  // SUBMITS the form (maruman 2026-06-13: confirm received an HTMLFormElement).
  // Clobbering it silently breaks their submit — only replace the NATIVE
  // blocking dialog; wrap and DELEGATE a page-defined one.
  if (isNative(window.confirm)) {
    window.confirm = (msg) => { log('confirm', msg); return true; };
  } else {
    const orig = window.confirm;
    window.confirm = function (...a) { log('confirm', a[0]); return orig.apply(this, a); };
  }
  if (isNative(window.alert)) {
    window.alert = (msg) => { log('alert', msg); };
  } else {
    const origAlert = window.alert;
    window.alert = function (...a) { log('alert', a[0]); return origAlert.apply(this, a); };
  }
  // beforeunload prompts block the navigation a successful submit triggers.
  window.onbeforeunload = null;
  window.__ocDialogArmed = true;
  return { armed: true, already: false };
}
"""

READ_DIALOG_LOG_JS = "() => (window.__ocDialogLog || [])"

# innerText head is capped to match verify.PAGE_EVIDENCE_JS; probes are matched
# case-folded on the JS side to keep one evaluate round-trip per observation.
_EVIDENCE_JS_TMPL = r"""
(() => {
  const probes = __PROBES__;
  const visible = (el) => {
    if (!el) return false;
    if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') return false;
    const st = getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden';
  };
  let visibleForms = 0, visibleTextareas = 0, editableVisible = 0,
      submitControls = 0, probeFieldHits = 0;
  for (const f of document.querySelectorAll('form')) if (visible(f)) visibleForms += 1;
  const isEditableType = (el) => {
    const t = (el.type || '').toLowerCase();
    return !['hidden', 'submit', 'button', 'image', 'file', 'checkbox', 'radio'].includes(t);
  };
  const fieldValues = [];
  for (const el of document.querySelectorAll('input, textarea, select')) {
    if (!visible(el)) continue;
    if (el.tagName === 'TEXTAREA') visibleTextareas += 1;
    if (el.tagName === 'INPUT' && !isEditableType(el)) continue;
    if (el.disabled || el.readOnly) continue;
    editableVisible += 1;
    const v = String(el.value || '');
    if (v) fieldValues.push(v.toLowerCase());
  }
  for (const el of document.querySelectorAll(
    'button, input[type="submit"], input[type="button"], input[type="image"], [role="button"]'
  )) if (visible(el)) submitControls += 1;
  const text = (document.body && document.body.innerText ? document.body.innerText : '').slice(0, 16000);
  const hay = text.toLowerCase();
  let probeTextHits = 0;
  for (const p of probes) {
    const needle = String(p || '').toLowerCase();
    if (!needle) continue;
    if (hay.includes(needle)) probeTextHits += 1;
    if (fieldValues.some((v) => v.includes(needle))) probeFieldHits += 1;
  }
  return {
    url: location.href,
    title: document.title || '',
    text,
    visible_forms: visibleForms,
    visible_textareas: visibleTextareas,
    editable_visible: editableVisible,
    submit_controls: submitControls,
    probe_text_hits: probeTextHits,
    probe_field_hits: probeFieldHits,
    dialog_count: (window.__ocDialogLog || []).length,
  };
})()
"""

_WS_RE = re.compile(r"\s+")


def build_probes(sender: dict[str, Any] | None, body: str | None) -> list[str]:
    """Distinctive strings WE put into the form — the state classifier's anchor.

    On an input page they live in editable field values; on a confirm page they
    are echoed as plain text; on a done page they are usually gone. Short or
    generic values (< 4 chars) are dropped to avoid false text hits.
    """
    s = sender or {}
    cands = [
        str(s.get("email") or ""),
        str(s.get("name") or ""),
        str(s.get("company") or ""),
        str(s.get("phone") or ""),
    ]
    head = _WS_RE.sub(" ", str(body or "")).strip()[:24]
    if head:
        cands.append(head)
    out: list[str] = []
    for c in cands:
        c = c.strip()
        if len(c) >= 4 and c not in out:
            out.append(c)
    return out[:6]


def evidence_js(probes: list[str] | None) -> str:
    return _EVIDENCE_JS_TMPL.replace(
        "__PROBES__", json.dumps(list(probes or []), ensure_ascii=False)
    )


def page_fingerprint(evidence: dict[str, Any] | None) -> str:
    """Stable digest for did-the-click-do-anything detection."""
    ev = evidence or {}
    raw = "|".join(
        [
            str(ev.get("url") or ""),
            str(ev.get("visible_forms") or 0),
            str(ev.get("visible_textareas") or 0),
            str(ev.get("editable_visible") or 0),
            _WS_RE.sub(" ", str(ev.get("text") or "")[:3000]),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()


def classify_send_state(evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Deterministic verdict on WHERE the submission currently is.

    States:
      - ``done``             — success keyword / thanks URL, input form gone
      - ``validation_error`` — error keywords while an editable form is shown
      - ``confirm``          — no editable textarea; our values echoed as text
                               (not in fields) or an explicit 「送信ボタンを
                               クリック」 instruction; something clickable exists
      - ``input``            — an editable form is present
      - ``no_form``          — nothing form-like at all (error page / redirect)
    """
    from _outreach_core.submit_progress import detect_confirm_instruction
    from _outreach_core.verify import (
        FORM_ERROR_KEYWORDS,
        FORM_SUCCESS_KEYWORDS,
        _url_looks_like_success,
    )

    ev = evidence or {}
    text = str(ev.get("text") or "")
    url = str(ev.get("url") or "")
    vis_forms = int(ev.get("visible_forms") or 0)
    vis_ta = int(ev.get("visible_textareas") or 0)
    editable = int(ev.get("editable_visible") or 0)
    submits = int(ev.get("submit_controls") or 0)
    text_hits = int(ev.get("probe_text_hits") or 0)
    field_hits = int(ev.get("probe_field_hits") or 0)

    hay = text.casefold()
    success_kw = any(k.casefold() in hay for k in FORM_SUCCESS_KEYWORDS)
    error_kw = any(k.casefold() in hay for k in FORM_ERROR_KEYWORDS)
    url_ok = _url_looks_like_success(url)
    confirm_instr = detect_confirm_instruction(text)

    # An explicit 「送信ボタンをクリックしてください」 instruction outranks
    # success-LOOKING keywords: confirm pages often say 「以下の内容でお問い合わせを
    # 受け付けます」, which contains a success keyword but is NOT a done page.
    if (
        confirm_instr
        and vis_ta == 0
        and field_hits == 0
        and (vis_forms > 0 or submits > 0)
    ):
        state = "confirm"
    elif (success_kw or url_ok) and vis_ta == 0 and field_hits == 0:
        state = "done"
    elif error_kw and (editable > 0 or vis_ta > 0):
        state = "validation_error"
    elif (
        vis_ta == 0
        and field_hits == 0
        and text_hits >= 2
        and not success_kw
        and (vis_forms > 0 or submits > 0)
    ):
        state = "confirm"
    elif editable > 0 or vis_ta > 0:
        state = "input"
    else:
        state = "no_form"

    return {
        "state": state,
        "fingerprint": page_fingerprint(ev),
        "url": url,
        "visible_forms": vis_forms,
        "visible_textareas": vis_ta,
        "editable_visible": editable,
        "submit_controls": submits,
        "probe_text_hits": text_hits,
        "probe_field_hits": field_hits,
        "has_success_keyword": success_kw,
        "has_error_keyword": error_kw,
        "url_success": url_ok,
        "confirm_instruction": confirm_instr,
        "dialog_count": int(ev.get("dialog_count") or 0),
    }
