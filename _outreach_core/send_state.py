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

FORM_SUCCESS_KEYWORDS = (
    "送信完了",
    "送信を受け付け",
    "受付完了",
    "ありがとうございました",
    "お問い合わせありがとう",
    "お問い合わせを受け付け",
    "ご連絡ありがとう",
    "完了しました",
    "完了画面",
    "送信が完了",
    "送信されました",
    "送信いたしました",
    "メッセージは送信",
    "受付番号",
    "控えをお送りしました",
    "THANKS",
    "thank you",
    "thank you for",
    "successfully submitted",
    "inquiry has been received",
    "we have received",
)

FORM_ERROR_KEYWORDS = (
    "入力エラー",
    "未入力",
    "正しく入力してください",
    "入力内容に誤り",
    "エラーがあります",
    "入力内容に問題",
    "確認してもう一度",
    "送信できませんでした",
    "送信に失敗",
    "failed to send",
    "submission failed",
)

_SUCCESS_STATUS_RE = re.compile(
    r"(^|[\s_-])(sent|success|succeeded|complete|completed|submitted|thanks|thankyou|"
    r"mail[_-]?sent|mw[_-]?wp[_-]?form[_-]?complete)([\s_-]|$)",
    re.I,
)
_ERROR_STATUS_RE = re.compile(
    r"(^|[\s_-])(invalid|error|failed|failure|spam|aborted|unaccepted|"
    r"mw[_-]?wp[_-]?form[_-]?error)([\s_-]|$)",
    re.I,
)
_PRE_SUBMIT_INTRO_RE = re.compile(
    r"(お問い合わせ|お問合せ|お問い合せ)[^。\n]{0,40}"
    r"(受け付けております|受け付けています|受付しております|お受けしております)"
)
_STRONG_COMPLETION_RE = re.compile(
    r"(送信(が|を)?(完了|されました|いたしました)|受付完了|"
    r"ありがとうございました|お問い合わせありがとう|お問合せありがとう|"
    r"(お問い合わせ|お問合せ|お問い合せ)[^。\n]{0,40}"
    r"(受け付けました|受け付けいたしました|受付しました|受付いたしました)|"
    r"受付番号|控えをお送りしました|thank you|successfully submitted|"
    r"inquiry has been received|we have received)",
    re.I,
)

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
      submitControls = 0, finalSubmitControls = 0, probeFieldHits = 0;
  const collectSubmissionSignals = () => {
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
    if (document.body) statuses.push(String(document.body.className || ''));
    const joined = statuses.join(' ').toLowerCase();
    return {
      statuses,
      response_text: responseTexts.join('\n').slice(0, 600),
      sent: /\bsent\b|mail[_-]?sent|wpcf7-mail-sent-ok/.test(joined),
      invalid: /\binvalid\b|spam|failed|aborted|unaccepted/.test(joined),
      generic_sent: /(^|[\s_-])(sent|success|succeeded|complete|completed|submitted|thanks|thankyou|mw[_-]?wp[_-]?form[_-]?complete)([\s_-]|$)/i.test(joined),
      generic_invalid: /(^|[\s_-])(invalid|error|failed|failure|spam|aborted|unaccepted|mw[_-]?wp[_-]?form[_-]?error)([\s_-]|$)/i.test(joined),
    };
  };
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
  const finalSubmitRe = /送信|送る|問い合わせ|お問い合わせ|submit|send|confirm|確定|完了/i;
  for (const el of document.querySelectorAll(
    'button, input[type="submit"], input[type="button"], input[type="image"], [role="button"]'
  )) {
    if (!visible(el)) continue;
    submitControls += 1;
    const label = String(el.innerText || el.value || el.getAttribute('aria-label') || el.textContent || '');
    if (finalSubmitRe.test(label)) finalSubmitControls += 1;
  }
  const text = (document.body && document.body.innerText ? document.body.innerText : '').slice(0, 16000);
  const hay = text.toLowerCase();
  let probeTextHits = 0;
  for (const p of probes) {
    const needle = String(p || '').toLowerCase();
    if (!needle) continue;
    if (hay.includes(needle)) probeTextHits += 1;
    if (fieldValues.some((v) => v.includes(needle))) probeFieldHits += 1;
  }
  const submission = collectSubmissionSignals();
  return {
    url: location.href,
    title: document.title || '',
    text,
    visible_forms: visibleForms,
    visible_textareas: visibleTextareas,
    editable_visible: editableVisible,
    submit_controls: submitControls,
    final_submit_controls: finalSubmitControls,
    probe_text_hits: probeTextHits,
    probe_field_hits: probeFieldHits,
    dialog_count: (window.__ocDialogLog || []).length,
    cf7_statuses: submission.statuses,
    cf7_response_text: submission.response_text,
    cf7_sent: submission.sent,
    cf7_invalid: submission.invalid,
    submission_statuses: submission.statuses,
    submission_status_text: submission.response_text,
    submission_sent: submission.generic_sent,
    submission_invalid: submission.generic_invalid,
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


def _norm(text: str) -> str:
    return (text or "").casefold()


def _text_has_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    hay = _norm(text)
    return any(_norm(k) in hay for k in keywords)


def _looks_like_pre_submit_intro(text: str) -> bool:
    """Intro/gate pages often say inquiries are accepted in general."""
    if not text:
        return False
    return bool(_PRE_SUBMIT_INTRO_RE.search(text)) and not bool(
        _STRONG_COMPLETION_RE.search(text)
    )


def url_looks_like_success(url: str) -> bool:
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


def _status_blob(evidence: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("cf7_statuses", "submission_statuses"):
        val = evidence.get(key)
        if isinstance(val, list):
            parts.extend(str(x) for x in val)
        elif val:
            parts.append(str(val))
    return " ".join(parts)


def _combined_page_text(evidence: dict[str, Any]) -> str:
    return "\n".join(
        str(evidence.get(k) or "")
        for k in ("text", "submission_status_text", "cf7_response_text")
    )


def assess_submission_result(evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Score generic post-submit evidence independent of any one form library.

    This is the shared truth for both the live send-state loop and the final
    verifier. A visible leftover form is not automatically failure when a
    framework/status region explicitly says the submission was accepted.
    """
    ev = evidence or {}
    text = _combined_page_text(ev)
    status_blob = _status_blob(ev)
    visible_forms = int(ev.get("visible_forms") or 0)
    visible_textareas = int(ev.get("visible_textareas") or 0)
    submit_controls = int(ev.get("submit_controls") or 0)
    final_submit_controls = int(ev.get("final_submit_controls") or 0)
    text_hits = int(ev.get("probe_text_hits") or 0)
    field_hits = int(ev.get("probe_field_hits") or 0)
    has_visibility = any(
        key in ev
        for key in (
            "visible_forms",
            "visible_textareas",
            "editable_visible",
            "submit_controls",
            "final_submit_controls",
            "probe_field_hits",
        )
    )

    success_kw = bool(ev.get("has_success_keyword"))
    if not success_kw and text:
        success_kw = _text_has_keyword(text, FORM_SUCCESS_KEYWORDS)
    pre_submit_intro = _looks_like_pre_submit_intro(text)
    if success_kw and pre_submit_intro:
        success_kw = False
    error_kw = bool(ev.get("has_error_keyword"))
    if not error_kw and text:
        error_kw = _text_has_keyword(text, FORM_ERROR_KEYWORDS)

    url_success = bool(ev.get("url_success")) or url_looks_like_success(str(ev.get("url") or ""))
    form_gone = bool(ev.get("form_gone_visible"))
    form_still = bool(ev.get("form_still_present")) or visible_textareas > 0

    explicit_sent = (
        bool(ev.get("cf7_sent"))
        or bool(ev.get("submission_sent"))
        or bool(ev.get("sent"))
        or bool(_SUCCESS_STATUS_RE.search(status_blob))
    )
    explicit_invalid = (
        bool(ev.get("cf7_invalid"))
        or bool(ev.get("submission_invalid"))
        or bool(_ERROR_STATUS_RE.search(status_blob))
    )
    status_success_text = _text_has_keyword(str(ev.get("submission_status_text") or ev.get("cf7_response_text") or ""), FORM_SUCCESS_KEYWORDS)
    status_error_text = _text_has_keyword(str(ev.get("submission_status_text") or ev.get("cf7_response_text") or ""), FORM_ERROR_KEYWORDS)

    # v30 §WS-C — per-signal contribution breakdown. Production logs showed
    # `send_score=-8` flipping to `+11` for the same target across consecutive
    # observations (pre/post visibility data), which made the verify trace
    # impossible to interpret at a glance. Recording each contribution lets
    # ``./report`` and Slack escalations explain WHICH evidence moved the
    # score, not just the final number.
    score = 0
    signals: list[str] = []
    score_breakdown: list[dict[str, Any]] = []

    def _add(signal: str, points: int) -> None:
        nonlocal score
        score += points
        signals.append(signal)
        score_breakdown.append({"signal": signal, "points": int(points)})

    if pre_submit_intro:
        signals.append("pre_submit_intro_success_ignored")
        score_breakdown.append(
            {"signal": "pre_submit_intro_success_ignored", "points": 0}
        )
    if explicit_sent and not explicit_invalid:
        _add("explicit_sent_status", 5)
    if status_success_text and not explicit_invalid:
        _add("success_status_text", 4)
    if url_success:
        _add("success_url", 2)
    if success_kw:
        _add("success_keyword", 2)
    if (
        success_kw
        and has_visibility
        and visible_textareas == 0
        and text_hits == 0
        and field_hits == 0
        and final_submit_controls == 0
        and not explicit_invalid
    ):
        _add("success_without_pending_submit", 2)
    if form_gone:
        _add("form_gone", 2)
    if explicit_invalid:
        _add("explicit_invalid_status", -4)
    if error_kw or status_error_text:
        _add("error_keyword", -3)
    if form_still:
        _add("form_still_present", -2)
    if field_hits:
        _add("our_values_still_editable", -2)
    if final_submit_controls > 0 and not explicit_sent:
        _add("pending_submit_control", -2)

    # Confirm pages may contain "受け付けます" but still ask the user to press
    # the final submit button. Do not let those success-looking words settle ok.
    try:
        from _outreach_core.submit_progress import detect_confirm_instruction

        confirm_instruction = detect_confirm_instruction(text)
    except Exception:
        confirm_instruction = False
    if confirm_instruction and submit_controls > 0 and not explicit_sent:
        _add("confirm_instruction", -2)

    if score >= 3:
        verdict = "sent_ok"
    elif score <= -2:
        verdict = "failed"
    else:
        verdict = "uncertain"

    if verdict == "sent_ok":
        reason = next((s for s in signals if s in (
            "explicit_sent_status", "success_status_text", "success_url", "success_keyword"
        )), "sent_evidence")
    elif verdict == "failed":
        reason = next((s for s in signals if s in (
            "explicit_invalid_status", "error_keyword", "our_values_still_editable",
            "form_still_present", "pending_submit_control"
        )), "failed_evidence")
    else:
        reason = "mixed_or_insufficient_evidence"

    return {
        "verdict": verdict,
        "sent": verdict == "sent_ok",
        "failed": verdict == "failed",
        "score": score,
        "reason": reason,
        "signals": signals,
        "score_breakdown": score_breakdown,
        "has_success_keyword": success_kw,
        "has_error_keyword": error_kw,
        "url_success": url_success,
        "form_gone_visible": form_gone,
        "form_still_present": form_still,
        "explicit_sent": explicit_sent,
        "explicit_invalid": explicit_invalid,
        "final_submit_controls": final_submit_controls,
    }


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

    ev = evidence or {}
    text = str(ev.get("text") or "")
    url = str(ev.get("url") or "")
    vis_forms = int(ev.get("visible_forms") or 0)
    vis_ta = int(ev.get("visible_textareas") or 0)
    editable = int(ev.get("editable_visible") or 0)
    submits = int(ev.get("submit_controls") or 0)
    final_submits = int(ev.get("final_submit_controls") or 0)
    text_hits = int(ev.get("probe_text_hits") or 0)
    field_hits = int(ev.get("probe_field_hits") or 0)

    hay = text.casefold()
    url_ok = url_looks_like_success(url)
    cf7_sent = bool(ev.get("cf7_sent"))
    cf7_invalid = bool(ev.get("cf7_invalid"))
    submission = assess_submission_result(ev)
    success_kw = bool(submission.get("has_success_keyword"))
    error_kw = bool(submission.get("has_error_keyword"))
    confirm_instr = detect_confirm_instruction(text)

    # An explicit 「送信ボタンをクリックしてください」 instruction outranks
    # success-LOOKING keywords: confirm pages often say 「以下の内容でお問い合わせを
    # 受け付けます」, which contains a success keyword but is NOT a done page.
    if submission["sent"]:
        state = "done"
    elif (
        confirm_instr
        and vis_ta == 0
        and field_hits == 0
        and (vis_forms > 0 or submits > 0)
    ):
        state = "confirm"
    elif (
        vis_ta == 0
        and field_hits == 0
        and text_hits >= 2
        and (vis_forms > 0 or final_submits > 0 or submits > 0)
        and not url_ok
        and not cf7_sent
        and not bool(ev.get("submission_sent"))
    ):
        state = "confirm"
    elif (success_kw or url_ok) and vis_ta == 0 and field_hits == 0 and final_submits == 0:
        state = "done"
    elif (
        error_kw
        or cf7_invalid
        or bool(ev.get("submission_invalid"))
        or bool(submission.get("explicit_invalid"))
    ) and (editable > 0 or vis_ta > 0):
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
        "final_submit_controls": final_submits,
        "probe_text_hits": text_hits,
        "probe_field_hits": field_hits,
        "has_success_keyword": success_kw,
        "has_error_keyword": error_kw,
        "url_success": url_ok,
        "confirm_instruction": confirm_instr,
        "dialog_count": int(ev.get("dialog_count") or 0),
        "send_verdict": submission["verdict"],
        "send_score": submission["score"],
        "send_reason": submission["reason"],
        "send_signals": submission["signals"],
    }
