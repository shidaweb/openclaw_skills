"""
Accurate, live reCAPTCHA classification (v6 §3.5).

Motivation (from production logs): the pipeline was reporting "reCAPTCHA / 確認待ち"
for targets that had **no captcha at all** — the real blocker was "submit button
not found". Two root causes:

  1. The escalation *message* hard-coded "reCAPTCHA" regardless of the real reason.
     (Fixed in run.py — the message now states the true reason.)
  2. Enrich-time detection (`has_recaptcha_v2`) fires when a reCAPTCHA *element
     exists in the DOM*, even if it is invisible / non-blocking (v3) or a checkbox
     that never shows a challenge. Presence ≠ blocking.

This module provides a **live, send-time** check that distinguishes:

    none                   … no reCAPTCHA / hCaptcha / Turnstile anywhere
    v3_invisible           … score-based, no widget, never blocks a submit
    v2_checkbox            … "I'm not a robot" checkbox present, no challenge shown
    v2_challenge           … an image/audio challenge popup is actually VISIBLE (blocking)
    hcaptcha               … hCaptcha widget present
    turnstile_widget       … Cloudflare Turnstile embedded on a real form (usually
                             auto/managed; non-blocking — let submit proceed)
    turnstile_challenge    … Turnstile widget showing an interactive challenge (blocking)
    turnstile_interstitial … full-page Cloudflare managed challenge / "Verify you are
                             human" interstitial; the real page is gated behind it (blocking)

Only the *blocking* kinds (``v2_challenge``, ``hcaptcha``, ``turnstile_challenge``,
``turnstile_interstitial``) should stop an autonomous submit. Everything else means:
if a submit fails, the cause is something other than a captcha (button detection,
required field, wrong form …), and must be reported as such.

We never solve or bypass a challenge. Blocking detections route to domain-level
avoidance learning (so we stop wasting attempts) and human relay / skip. The only
"don't trigger it" lever is legitimate hygiene — genuine root-domain warmup dwell
(so the persistent profile carries its own cf_clearance cookie) and per-domain
pacing — handled in warmup.py / avoidance.py, not here.

The JS constant is evaluated in the page; the Python classifiers operate on the
dict it returns, so they are fully unit-testable with synthetic inputs.
"""

from __future__ import annotations

from typing import Any


# Evaluated in the page. Inspects the LIVE DOM for the reCAPTCHA iframes Google
# actually injects, and whether a challenge popup is visible right now.
LIVE_CAPTCHA_JS = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') return false;
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) return false;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity || '1') < 0.1) return false;
    return true;
  };

  const q = (sel) => Array.from(document.querySelectorAll(sel));

  // Anchor iframe = the "I'm not a robot" checkbox widget.
  const anchors = q('iframe[src*="recaptcha/api2/anchor"], iframe[src*="recaptcha/enterprise/anchor"]');
  // Bframe = the image/audio challenge popup that appears when score is low.
  const bframes = q('iframe[src*="recaptcha/api2/bframe"], iframe[src*="recaptcha/enterprise/bframe"]');
  const hcap = q('iframe[src*="hcaptcha.com"]');

  // v3 / invisible: api.js?render= script, or explicit invisible widget, with no anchor.
  const v3Scripts = q('script[src*="recaptcha/api.js?render="], script[src*="recaptcha/enterprise.js?render="]');
  const invisibleWidget = q('.g-recaptcha[data-size="invisible"]');
  const visibleWidget = q('.g-recaptcha:not([data-size="invisible"])');

  // --- Cloudflare Turnstile / managed challenge -----------------------------
  // Embedded widget: explicit .cf-turnstile container, the response input, or
  // the Turnstile iframe.
  const tsWidgets = q('.cf-turnstile, [data-sitekey][data-callback], input[name="cf-turnstile-response"]');
  const tsIframes = q('iframe[src*="challenges.cloudflare.com"]');
  // Full-page MANAGED challenge interstitial: CF injects these on the gating page.
  const cfChallengeEls = q(
    '#challenge-running, #cf-challenge-running, #challenge-stage, #challenge-form, ' +
    '#cf-please-wait, #trk_jschal_js, script[src*="/cdn-cgi/challenge-platform/"]'
  );
  const bodyText = (document.body && document.body.innerText ? document.body.innerText : '').slice(0, 4000);
  const cfInterstitialText = /(Verify you are human|Checking if the site connection is secure|needs to review the security of your connection|お使いのブラウザを確認しています|セキュリティ.*接続を確認|あなたが人間であることを確認)/i.test(bodyText);

  // Does the page carry a real, fillable form right now? (textarea or >2 text
  // inputs) — used to tell an embedded Turnstile widget apart from a full-page
  // interstitial that REPLACES the form.
  const textInputs = q('input').filter((el) => {
    const t = (el.type || '').toLowerCase();
    return t !== 'hidden' && t !== 'submit' && t !== 'button' && t !== 'image' && t !== 'checkbox' && t !== 'radio';
  });
  const hasForm = q('textarea').length > 0 || textInputs.length > 2;

  const tsIframeVisible = tsIframes.some(visible);
  const tsPresent = tsWidgets.length > 0 || tsIframes.length > 0;
  const cfChallengePresent = cfChallengeEls.length > 0 || cfInterstitialText;

  const challengeVisible = bframes.some(visible);
  const checkboxPresent = anchors.length > 0 || visibleWidget.length > 0;
  const hcaptchaPresent = hcap.length > 0;

  let kind = 'none';
  // Order matters: a full-page CF interstitial gates everything → check first.
  if (cfChallengePresent && !hasForm) kind = 'turnstile_interstitial';
  else if (challengeVisible) kind = 'v2_challenge';
  else if (hcaptchaPresent) kind = 'hcaptcha';
  else if (tsPresent && tsIframeVisible && !hasForm) kind = 'turnstile_challenge';
  else if (tsPresent) kind = 'turnstile_widget';
  else if (cfChallengePresent) kind = 'turnstile_interstitial';
  else if (checkboxPresent) kind = 'v2_checkbox';
  else if (v3Scripts.length > 0 || invisibleWidget.length > 0) kind = 'v3_invisible';

  return {
    kind,
    checkbox_present: checkboxPresent,
    challenge_visible: challengeVisible,
    hcaptcha_present: hcaptchaPresent,
    turnstile_present: tsPresent,
    cf_challenge_present: cfChallengePresent,
    has_form: hasForm,
    counts: {
      anchor: anchors.length,
      bframe: bframes.length,
      bframe_visible: bframes.filter(visible).length,
      hcaptcha: hcap.length,
      v3_script: v3Scripts.length,
      invisible_widget: invisibleWidget.length,
      visible_widget: visibleWidget.length,
      turnstile_widget: tsWidgets.length,
      turnstile_iframe: tsIframes.length,
      turnstile_iframe_visible: tsIframes.filter(visible).length,
      cf_challenge_el: cfChallengeEls.length,
    },
  };
}
"""


_BLOCKING_KINDS = {"v2_challenge", "hcaptcha", "turnstile_challenge", "turnstile_interstitial"}

# Kinds that mean "this domain is gated by Cloudflare" — used to enable genuine
# root-domain warmup dwell + pacing next time (legitimate hygiene, not bypass).
_CLOUDFLARE_KINDS = {"turnstile_widget", "turnstile_challenge", "turnstile_interstitial"}


def classify_live_state(raw: Any) -> dict[str, Any]:
    """Normalize the dict returned by LIVE_CAPTCHA_JS into a stable shape.

    Returns: {kind, present, blocking, checkbox_present, challenge_visible, counts}
    On a missing/garbage payload (eval failed), returns a conservative "unknown"
    state that is **not** treated as a captcha blocker (so a non-captcha failure
    is never mislabeled as reCAPTCHA).
    """
    if not isinstance(raw, dict):
        return {
            "kind": "unknown",
            "present": False,
            "blocking": False,
            "checkbox_present": False,
            "challenge_visible": False,
            "turnstile_present": False,
            "cf_challenge_present": False,
            "cloudflare": False,
            "counts": {},
        }
    kind = raw.get("kind") or "none"
    checkbox_present = bool(raw.get("checkbox_present"))
    challenge_visible = bool(raw.get("challenge_visible"))
    present = kind not in ("none", "unknown")
    return {
        "kind": kind,
        "present": present,
        "blocking": is_blocking(kind, challenge_visible=challenge_visible),
        "checkbox_present": checkbox_present,
        "challenge_visible": challenge_visible,
        "turnstile_present": bool(raw.get("turnstile_present")),
        "cf_challenge_present": bool(raw.get("cf_challenge_present")),
        "cloudflare": is_cloudflare(kind) or bool(raw.get("cf_challenge_present")),
        "counts": raw.get("counts") or {},
    }


def is_cloudflare(kind: str) -> bool:
    """Whether the detected kind indicates a Cloudflare-gated domain (any
    Turnstile/managed-challenge variant)."""
    return kind in _CLOUDFLARE_KINDS


def is_blocking(kind: str, *, challenge_visible: bool = False, block_on_checkbox: bool = False) -> bool:
    """Whether a captcha kind should stop an autonomous submit.

    Only a *visible challenge* (or hCaptcha) blocks by default. A v2 checkbox does
    not block unless ``block_on_checkbox`` is set (some sites require a click; we
    leave that policy to the caller). v3 invisible never blocks.
    """
    if kind in _BLOCKING_KINDS:
        return True
    if challenge_visible:
        return True
    if block_on_checkbox and kind == "v2_checkbox":
        return True
    return False


def reason_label(state: dict[str, Any]) -> str:
    """Human-readable, *truthful* label for logs/Slack."""
    kind = state.get("kind", "none")
    return {
        "v2_challenge": "reCAPTCHA v2 画像チャレンジが表示（ブロッキング）",
        "v2_checkbox": "reCAPTCHA v2 チェックボックスあり（チャレンジ非表示）",
        "v3_invisible": "reCAPTCHA v3（不可視・非ブロッキング）",
        "hcaptcha": "hCaptcha 検出",
        "turnstile_interstitial": "Cloudflare 全画面チャレンジ（Verify you are human・ブロッキング）",
        "turnstile_challenge": "Cloudflare Turnstile インタラクティブチャレンジ表示（ブロッキング）",
        "turnstile_widget": "Cloudflare Turnstile ウィジェットあり（フォーム上・非ブロッキング）",
        "none": "captcha なし",
        "unknown": "captcha 判定不能（live eval 失敗）",
    }.get(kind, kind)
