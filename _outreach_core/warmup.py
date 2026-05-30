"""
Browser session warm-up for reCAPTCHA v3 score improvement (v4 §11-A-8).

Strategy:
  1. Open the target site's root domain (not the form yet)
  2. Dispatch synthetic mouse movements + scroll
  3. Wait so the session accumulates "natural visitor" signals
  4. Caller then navigates to the form URL with a warm session

Pure browser + Python. No LLM, no Anthropic API, no third-party solver service.
This module only improves passthrough rates for reCAPTCHA v3 invisible. v2
visible CAPTCHA requires separate handling (escalate to human via
needs_attention).
"""

from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import urlparse


# Synthetic browser interactions: dispatched mouse movements + smooth scroll.
# reCAPTCHA Enterprise scores improve when there is non-zero engagement on
# the page before form interaction. We keep this minimal and idempotent.
WARMUP_INTERACTION_JS = r"""
() => {
  const w = window.innerWidth || 1024;
  const h = window.innerHeight || 768;

  // 1) Curved-ish mouse path (5 points)
  const points = [
    [w * 0.30, h * 0.20],
    [w * 0.50, h * 0.50],
    [w * 0.70, h * 0.40],
    [w * 0.40, h * 0.70],
    [w * 0.60, h * 0.55],
  ];
  for (const [x, y] of points) {
    try {
      const ev = new MouseEvent('mousemove', {
        clientX: x, clientY: y, bubbles: true, cancelable: true,
      });
      document.dispatchEvent(ev);
    } catch (e) { /* old browsers */ }
  }

  // 2) Smooth scroll down then back up (signals "reading")
  try {
    window.scrollTo({ top: 300, behavior: 'smooth' });
  } catch (e) { /* old browsers */ }

  // 3) Focus / blur on body to register interaction
  try {
    document.body && document.body.focus && document.body.focus();
  } catch (e) { /* ignore */ }

  return {
    ok: true,
    viewport: { w, h },
    points_dispatched: points.length,
  };
}
"""


def root_url_of(url: str) -> str:
    """Return scheme://host/ for a given URL, or the input if it can't be parsed."""
    try:
        p = urlparse(url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}/"
    except Exception:  # noqa: BLE001
        pass
    return url


def captcha_warmup_strategy(config: dict[str, Any] | None) -> str:
    cfg = (config or {}).get("captcha") or {}
    return str(cfg.get("v3_strategy") or "passthrough").strip()


def captcha_warmup_seconds(config: dict[str, Any] | None, *, default: int = 20) -> int:
    cfg = (config or {}).get("captcha") or {}
    try:
        v = int(cfg.get("warmup_sec", default))
        return max(0, min(v, 90))  # safety cap at 90 sec
    except (TypeError, ValueError):
        return default


def warmup_browser_session(
    *,
    seed_url: str,
    oc_browser_fn: Callable[..., Any],
    evaluate_fn: Callable[[str], Any],
    duration_sec: int = 20,
) -> dict[str, Any]:
    """
    Open seed_url, perform natural interactions, then return.
    The caller is expected to navigate to the actual form URL afterwards.

    duration_sec is the **total wall time budget** for warm-up. The function
    splits it roughly: open → settle → interact → settle → return.

    Returns a diagnostics dict. Never raises.
    """
    diag: dict[str, Any] = {
        "seed_url": seed_url,
        "duration_sec": duration_sec,
        "steps": [],
    }
    if duration_sec <= 0:
        diag["skipped"] = "duration_sec<=0"
        return diag

    t0 = time.time()
    try:
        oc_browser_fn("open", seed_url)
        diag["steps"].append({"action": "open_seed"})
    except Exception as exc:  # noqa: BLE001
        diag["error"] = f"open_seed: {str(exc)[:120]}"
        diag["elapsed_sec"] = round(time.time() - t0, 1)
        return diag

    # Settle after open
    initial_settle = min(4.0, duration_sec * 0.3)
    time.sleep(initial_settle)

    # Interact mid-budget
    try:
        result = evaluate_fn(WARMUP_INTERACTION_JS)
        diag["steps"].append({"action": "interact", "result": result if isinstance(result, dict) else None})
    except Exception as exc:  # noqa: BLE001
        diag["steps"].append({"action": "interact", "error": str(exc)[:120]})

    # Remaining budget — let the page "live" for the score signal
    remaining = max(0.0, duration_sec - (time.time() - t0))
    time.sleep(remaining)

    diag["elapsed_sec"] = round(time.time() - t0, 1)
    return diag


def apply_warmup_if_enabled(
    *,
    form_url: str,
    config: dict[str, Any] | None,
    oc_browser_fn: Callable[..., Any],
    evaluate_fn: Callable[[str], Any],
    emit_event: Callable[..., None] | None = None,
    stage: str = "send",
    target_id: str | None = None,
) -> dict[str, Any]:
    """
    High-level wrapper used by stage_send. Reads config and dispatches warmup
    if enabled. Returns the diag dict (or {"skipped": True} if disabled).
    """
    strategy = captcha_warmup_strategy(config)
    if strategy != "passthrough_with_warmup":
        return {"skipped": True, "strategy": strategy}

    duration = captcha_warmup_seconds(config)
    if duration <= 0:
        return {"skipped": True, "strategy": strategy, "reason": "duration_sec<=0"}

    seed = root_url_of(form_url)
    diag = warmup_browser_session(
        seed_url=seed,
        oc_browser_fn=oc_browser_fn,
        evaluate_fn=evaluate_fn,
        duration_sec=duration,
    )
    diag["strategy"] = strategy

    if emit_event:
        try:
            emit_event(
                "captcha.warmup.applied",
                stage=stage,
                target_id=target_id,
                payload={
                    "strategy": strategy,
                    "duration_sec": duration,
                    "elapsed_sec": diag.get("elapsed_sec"),
                    "seed_url": seed,
                    "error": diag.get("error"),
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return diag
