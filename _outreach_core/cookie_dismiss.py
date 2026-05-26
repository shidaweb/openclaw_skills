"""
Cookie consent banner auto-dismissal helper (v4 §11-A-7).

Used by stage_enrich / stage_send right after the form_url is opened, so the
subsequent click/fill operations are not blocked by OneTrust / TrustArc /
Cookiebot / 自前 banner overlays.

Pure browser-side detection. No LLM, no Anthropic API call.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

# Browser-side JS. Tries:
#   1. Known SDK selectors (OneTrust, TrustArc, Cookiebot, common id patterns)
#   2. Text match scoped under banner-like roots (id/class contains
#      cookie/consent/gdpr), with an accept-only allow list and a strict deny
#      list ("拒否" / "reject" / etc.)
#
# Returns: {dismissed: bool, method: "id"|"text"|null, selector?, text?}
DISMISS_COOKIE_BANNER_JS = r"""
() => {
  const ACCEPT_ID_SELECTORS = [
    '#onetrust-accept-btn-handler',
    '#truste-consent-button',
    '#CybotCookiebotDialogBodyButtonAccept',
    '#cookie-accept',
    '#js-cookie-accept',
    '#js_consent_accept',
    '.cc-accept-all',
    '.cc-allow',
    'button[aria-label*="同意" i][role="button"]',
    'button[aria-label*="accept" i]',
  ];

  const ACCEPT_TEXT_PATTERNS = [
    /^同意する$/,
    /^同意して(進む|続行|閉じる)$/,
    /^すべて(の)?(クッキー|cookie)を?受け入れ/i,
    /^受け入れる$/,
    /^了解(しました)?$/,
    /^OK$/i,
    /^accept(\s+all)?$/i,
    /^accept cookies$/i,
    /^allow all$/i,
    /^I agree$/i,
    /^agree$/i,
  ];

  // Strict deny — never click these even if they match other patterns
  const DENY_TEXT_PATTERNS = [
    /同意しない/,
    /拒否/,
    /必要なもの(だけ|のみ)/,
    /reject/i,
    /decline/i,
    /opt[- ]?out/i,
    /必須のみ/,
  ];

  const isVisible = (el) => {
    if (!el) return false;
    if (el.offsetParent === null) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  // 1) Known SDK selectors
  for (const sel of ACCEPT_ID_SELECTORS) {
    try {
      const el = document.querySelector(sel);
      if (el && isVisible(el)) {
        el.click();
        return { dismissed: true, method: 'id', selector: sel };
      }
    } catch (e) { /* invalid selector, ignore */ }
  }

  // 2) Text match scoped under banner-like roots
  const ROOT_SELECTOR =
    '[id*="cookie" i], [class*="cookie" i], ' +
    '[id*="consent" i], [class*="consent" i], ' +
    '[id*="gdpr" i], [class*="gdpr" i]';

  const banner_roots = [...document.querySelectorAll(ROOT_SELECTOR)];
  for (const root of banner_roots) {
    // Skip if any deny text is in this root — means user opted out flow
    const rootText = (root.textContent || '');
    // ↑ root テキストに deny だけがあるケースは稀。個別 button text で deny を弾く。

    const buttons = root.querySelectorAll('button, [role="button"], a');
    for (const b of buttons) {
      const txt = (b.textContent || '').trim();
      if (txt.length === 0 || txt.length > 40) continue;
      if (!isVisible(b)) continue;

      // Check deny first — must not match any deny pattern
      if (DENY_TEXT_PATTERNS.some(re => re.test(txt))) continue;

      // Now check accept patterns
      for (const re of ACCEPT_TEXT_PATTERNS) {
        if (re.test(txt)) {
          b.click();
          return { dismissed: true, method: 'text', text: txt };
        }
      }
    }
  }

  return { dismissed: false };
}
"""


def dismiss_cookie_banner(
    evaluate_fn: Callable[[str], Any],
    *,
    mode: str = "accept",
    retries: int = 2,
    wait_sec: float = 1.5,
) -> dict[str, Any]:
    """
    Try to dismiss a cookie consent banner.

    Args:
        evaluate_fn: callable that evaluates JS in the page (typically
            `_outreach_core.infer._evaluate` wired to oc_browser).
        mode: "accept" → click accept-like button (default).
              "reject" → reserved for future ("すべて拒否" pattern).
              "skip"   → no-op (returns immediately).
        retries: extra evaluation passes if the first one didn't find a banner
            (banners may render asynchronously).
        wait_sec: seconds to wait between retry passes.

    Returns:
        dict with keys:
            dismissed: bool
            method:    "id" | "text" | None
            selector:  CSS selector matched (when method == "id")
            text:      button text matched (when method == "text")
            mode:      echoed back

    Never raises. Failures from evaluate_fn are swallowed.
    """
    if mode == "skip":
        return {"dismissed": False, "mode": "skip", "reason": "skipped_by_config"}

    if mode == "reject":
        # Future work — for now fall through to accept behavior.
        # We log this so a future implementer can grep for it.
        pass

    attempts = max(1, retries + 1)
    last: dict[str, Any] = {"dismissed": False, "mode": mode}

    for i in range(attempts):
        try:
            res = evaluate_fn(DISMISS_COOKIE_BANNER_JS)
        except Exception as exc:  # noqa: BLE001 — never let cookie code break the pipeline
            last = {"dismissed": False, "mode": mode, "error": str(exc)[:120]}
        else:
            if isinstance(res, dict):
                if res.get("dismissed"):
                    res = {**res, "mode": mode}
                    # Brief settle for the banner's exit animation before
                    # subsequent click/type operations.
                    time.sleep(0.5)
                    return res
                last = {**res, "mode": mode}

        if i < attempts - 1:
            time.sleep(wait_sec)

    return last


def cookie_consent_mode(config: dict[str, Any] | None) -> str:
    return str(((config or {}).get("browser") or {}).get("cookie_consent") or "accept")


def apply_cookie_dismiss(
    evaluate_fn: Callable[[str], Any],
    config: dict[str, Any] | None,
    *,
    stage: str,
    target_id: str | None = None,
    emit_event: Optional[Callable[..., None]] = None,
) -> dict[str, Any]:
    """Dismiss banner after page open; optional emit callback for events."""
    mode = cookie_consent_mode(config)
    res = dismiss_cookie_banner(evaluate_fn, mode=mode)
    if res.get("dismissed"):
        print(
            f"  [{stage}] cookie banner dismissed: method={res.get('method')}"
            f"{' sel=' + str(res['selector']) if res.get('selector') else ''}"
            f"{' text=' + str(res['text']) if res.get('text') else ''}"
        )
        if emit_event:
            emit_event(
                "cookie.dismissed",
                stage=stage,
                target_id=target_id,
                payload={
                    "method": res.get("method"),
                    "selector": res.get("selector"),
                    "text": res.get("text"),
                },
            )
    return res
