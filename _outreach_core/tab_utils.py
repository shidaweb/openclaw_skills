"""
Pure helpers for openclaw browser tab management (v6 §17).

openclaw browser is tab-capable:
  - `open <url> --json`  → opens a NEW tab, returns {targetId, tabId, type, url, …}
  - `tabs --json`        → {"tabs": [ {targetId, tabId, type:"page"|"iframe", url, …} ]}
  - `focus <targetId>` / `close <targetId>`

We key tabs by the stable **targetId** (a long hash), not tabId ("t494", which can
be reused). `tabs` lists iframes too, so real tabs are `type == "page"`.

These functions operate on the parsed JSON payloads only — no subprocess — so they
are unit-testable against the real shapes captured from production.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


def target_id_from_open(open_payload: Any) -> str | None:
    """targetId of the tab just created by `open --json`."""
    if isinstance(open_payload, dict):
        tid = open_payload.get("targetId")
        if isinstance(tid, str) and tid:
            return tid
    return None


def page_tabs(tabs_payload: Any) -> list[dict[str, Any]]:
    """Real page tabs (excludes type=='iframe' sub-frames)."""
    if not isinstance(tabs_payload, dict):
        return []
    tabs = tabs_payload.get("tabs")
    if not isinstance(tabs, list):
        return []
    return [t for t in tabs if isinstance(t, dict) and t.get("type") == "page"]


def page_target_ids(tabs_payload: Any) -> list[str]:
    return [t["targetId"] for t in page_tabs(tabs_payload)
            if isinstance(t.get("targetId"), str)]


def is_tab_open(tabs_payload: Any, target_id: str) -> bool:
    return target_id in page_target_ids(tabs_payload)


def find_tab(tabs_payload: Any, target_id: str) -> dict[str, Any] | None:
    for t in page_tabs(tabs_payload):
        if t.get("targetId") == target_id:
            return t
    return None


def closable_overflow(tabs_payload: Any, *, protect: set[str], cap: int,
                      keep_newest: int = 1) -> list[str]:
    """Which page-tab targetIds to close to keep total ≤ cap.

    Closes oldest first (list order = open order), never touching ``protect``
    (resolver-bound tabs) nor the ``keep_newest`` most-recent tabs (the one we're
    about to / currently working in). Returns targetIds to close."""
    ids = page_target_ids(tabs_payload)
    if len(ids) <= cap:
        return []
    protected_tail = set(ids[-keep_newest:]) if keep_newest > 0 else set()
    out: list[str] = []
    # Walk oldest→newest, closing until we're within cap.
    over = len(ids) - cap
    for tid in ids:
        if over <= 0:
            break
        if tid in protect or tid in protected_tail:
            continue
        out.append(tid)
        over -= 1
    return out


def closable_overflow_owned(
    tabs_payload: Any,
    *,
    owned: set[str],
    protect: set[str],
    cap: int,
    keep_newest: int = 1,
) -> list[str]:
    """v32 FX3 — like :func:`closable_overflow`, but scoped to OUR OWN tabs.

    Multiple briefs share one browser; the old global cap made run A count
    (and close) run B's active tab — B's next evaluate then failed with
    "tab not found" → lead_crashed. Here both the candidate set and the
    overflow arithmetic use ``owned ∩ open page tabs`` only, so the cap is
    per-run and a sibling's tab is structurally unreachable. ``protect``
    (resolver-bound) and the ``keep_newest`` most-recent OWN tabs are still
    spared. An empty/stale ``owned`` set degrades to closing nothing.
    """
    ids = page_target_ids(tabs_payload)
    owned_open = [tid for tid in ids if tid in owned]
    if len(owned_open) <= cap:
        return []
    protected_tail = set(owned_open[-keep_newest:]) if keep_newest > 0 else set()
    out: list[str] = []
    over = len(owned_open) - cap
    for tid in owned_open:
        if over <= 0:
            break
        if tid in protect or tid in protected_tail:
            continue
        out.append(tid)
        over -= 1
    return out


def orphan_tab_ids(
    recorded_tab_ids: list[str] | set[str] | None,
    open_page_ids: set[str],
    resolver_tab_ids: set[str],
) -> set[str]:
    """v32 FX3 — which of a DEAD run's recorded tabs may be closed.

    Only tabs the dead run recorded as its own AND that are still open,
    MINUS its pending resolve_queue tabs — dead runs legitimately leave
    error tabs open on purpose so a later resolver pass can fix them
    in-place. Unknown tabs (not recorded by anyone) are never returned.
    """
    recorded = {str(t) for t in (recorded_tab_ids or []) if t}
    return (recorded & set(open_page_ids)) - set(resolver_tab_ids)


# Japanese second-level domains (and a few generic) for registrable-domain calc.
# Without these, "a.co.jp" and "b.co.jp" would wrongly collapse to "co.jp".
_MULTI_SLD = {"co", "or", "ne", "ac", "go", "ad", "ed", "gr", "lg", "gov", "com"}


def _host(u: str | None) -> str:
    if not u:
        return ""
    try:
        h = (urlparse(u if "://" in u else f"https://{u}").hostname or "").lower()
    except (ValueError, TypeError):
        return ""
    return h


def registrable_domain(host_or_url: str | None) -> str:
    """JP-aware registrable domain. www.park24.co.jp / ssl.park24.co.jp → park24.co.jp;
    a.co.jp → a.co.jp (NOT co.jp); example.com → example.com."""
    h = _host(host_or_url) or (host_or_url or "").lower()
    parts = [p for p in h.split(".") if p]
    if len(parts) >= 3 and parts[-1] == "jp" and parts[-2] in _MULTI_SLD:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return h


def same_site(url_a: str | None, url_b: str | None) -> bool:
    """Safety check before submitting in a focused tab: confirm the focused tab's
    URL is the same company (registrable domain) as the target's form, so we never
    submit one company's draft into another company's tab. Subdomains (www↔ssl)
    are treated as the same site."""
    ra, rb = registrable_domain(url_a), registrable_domain(url_b)
    return bool(ra) and ra == rb
