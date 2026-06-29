"""Send-stage helpers extracted from ``jp-form-outreach/run.py``.

v30 §WS-E first-cut extraction. The end-state vision is a thin
``send_pipeline`` that owns the input→fill→submit→verify state machine, but
that is a multi-week project. This module is the *seed*: it hosts small,
self-contained helpers that have a clean signature (no hidden globals) so the
existing ``run.py`` can shim into them today and the eventual full extraction
has somewhere to land.

What lives here right now:

  * :func:`try_iframe_form_takeover` — when the top-level page renders no form
    but exposes a hosted-form iframe, navigate to that iframe's src as the new
    top-level URL. Pure DI: caller passes ``open_url`` / ``emit_event`` /
    ``sleep_fn`` so the function is easy to unit-test without a browser.

Why DI rather than module-level globals: ``run.py``'s legacy structure made it
trivial to call ``oc_browser`` / ``_emit_event`` from anywhere. That convenience
is exactly what made the file 8929 lines and untestable. New helpers force the
caller to thread its I/O surface in explicitly.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from _outreach_core import contact_url as core_contact_url


def try_iframe_form_takeover(
    target: dict[str, Any],
    fields: Mapping[str, Any] | None,
    *,
    open_url: Callable[[str], Any],
    sleep_fn: Callable[[float], Any] | None = None,
    sleep_sec: float = 1.0,
    emit_event: Callable[..., Any] | None = None,
    trace_dir: Any = None,
    target_id: str = "",
) -> str | None:
    """Navigate to a hosted-form iframe src as the new top-level URL.

    Behaviour mirrors the pre-extraction ``run.py:_try_iframe_form_takeover``
    so the run-loop integration test continues to lock the same shape. The
    function is pure-DI (no hidden globals) so unit tests can wire stubs for
    ``open_url`` / ``emit_event`` / ``sleep_fn`` without monkey-patching
    ``run.py``.

    Returns the iframe src on a successful navigation, ``None`` otherwise. On
    success the target's ``form_url`` is rewritten in place so downstream
    diagnostics report the page actually being submitted to (the parent page
    URL is no longer authoritative once we've descended into the embed).

    Failure semantics: ``open_url`` raising any exception is swallowed and
    the function returns ``None``. The send loop must not abort because the
    optional takeover failed — the next gate (``page_has_no_form``) escalates
    via the existing resolver queue.
    """
    if not isinstance(fields, Mapping):
        return None
    iframes = fields.get("iframes") or []
    if not iframes:
        return None
    base_url = str(target.get("form_url") or "")
    src = core_contact_url.iframe_form_src(iframes, base_url)
    if not src:
        return None
    try:
        open_url(src)
        if sleep_fn is not None and sleep_sec > 0:
            sleep_fn(sleep_sec)
    except Exception:  # noqa: BLE001 - takeover is best-effort
        return None
    if emit_event is not None:
        try:
            emit_event(
                "send.iframe_form_takeover",
                stage="send",
                target_id=str(target_id or ""),
                payload={
                    "from": base_url[:200],
                    "to": src[:200],
                    "iframe_count": len(iframes),
                },
                trace_dir=trace_dir,
            )
        except Exception:  # noqa: BLE001 - audit emit must never abort the loop
            pass
    target["form_url"] = src
    return src
