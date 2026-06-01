"""
Content-rejection detection + body sanitization (v6 §3.6).

Some contact forms reject the inquiry body when it contains characters they
disallow — most commonly a **URL** (the CTA scheduling link), sometimes other
"特殊文字". The form responds with a validation error like:

    「問い合わせ内容に使用できない文字が使われています」
    「本文にURLは記載できません」

Production logs show this was handled manually ("URL不可のため本文からカレンダーURL
除去"). This module automates it:

  1. ``detect_content_rejection(page_text)`` — does the post-submit page show a
     disallowed-character / URL-not-allowed validation error? Classifies the
     cause as ``url`` | ``char`` | ``generic``.
  2. ``strip_urls(text)`` / ``sanitize_body(text)`` — produce a URL-free body to
     resend (the "URLは送らないルート" fallback).

Pure functions, fully unit-testable. No browser, no LLM, no network.
"""

from __future__ import annotations

import re
from typing import Any

# A URL token. The body is restricted to valid ASCII URL characters so the match
# STOPS at the first Japanese character (JP text often follows a URL with no
# space, e.g. "https://ex.com/aをご覧ください" — we must not eat "をご覧ください").
_URL_BODY = r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+"
_URL_RE = re.compile(
    r"(?:"
    r"https?://" + _URL_BODY                                  # http(s) URLs
    + r"|www\." + _URL_BODY                                   # www. URLs
    + r"|(?<![\w@.])[a-zA-Z0-9-]+\.(?:link|app|io|co|jp|com|net|org)/" + _URL_BODY
    + r")"
)

# Validation-error phrases that mean "your content has characters we won't accept".
_URL_REJECT_PATTERNS = [
    r"URL(?:を|は|が)?(?:.{0,6})?(?:入力|記載|使用|登録|含め?る?)(?:.{0,4})?(?:でき(?:ない|ません)|不可)",
    r"(?:リンク|ﾘﾝｸ)(?:を|は|が)?(?:.{0,6})?(?:入力|記載|使用)(?:.{0,4})?(?:でき(?:ない|ません)|不可)",
    r"https?://.{0,20}(?:でき(?:ない|ません)|使用不可|不可)",
    r"URLs?\s+are\s+not\s+allowed",
    r"links?\s+are\s+not\s+allowed",
]
_CHAR_REJECT_PATTERNS = [
    r"使用(?:でき|出来)(?:ない|ません)文字",
    r"使え(?:ない|ません)文字",
    r"(?:不正|無効|禁止|特殊)な?文字(?:が|を)?(?:.{0,8})?(?:含|使用|入力)",
    r"文字(?:が|は)?(?:.{0,6})?使用(?:でき|出来)(?:ない|ません)",
    r"invalid\s+characters?",
    r"使用できない文字",
]

_URL_REJECT_RE = [re.compile(p) for p in _URL_REJECT_PATTERNS]
_CHAR_REJECT_RE = [re.compile(p) for p in _CHAR_REJECT_PATTERNS]


def has_url(text: str | None) -> bool:
    if not text:
        return False
    return bool(_URL_RE.search(text))


def find_urls(text: str | None) -> list[str]:
    if not text:
        return []
    return [m.group(0) for m in _URL_RE.finditer(text)]


def detect_content_rejection(page_text: str | None) -> dict[str, Any] | None:
    """Inspect post-submit page text for a disallowed-character / URL validation
    error. Returns ``{matched, kind, evidence}`` or None.

    kind:
      "url"     … the message specifically calls out URLs / links
      "char"    … generic "使用できない文字" (URL is the usual offender → strip it)
      None      … no rejection detected
    """
    if not page_text:
        return None
    for rx in _URL_REJECT_RE:
        m = rx.search(page_text)
        if m:
            return {"matched": True, "kind": "url", "evidence": m.group(0)[:120]}
    for rx in _CHAR_REJECT_RE:
        m = rx.search(page_text)
        if m:
            return {"matched": True, "kind": "char", "evidence": m.group(0)[:120]}
    return None


def strip_urls(text: str | None) -> tuple[str, list[str]]:
    """Remove URL tokens and tidy the result. Returns (clean_text, removed_urls)."""
    if not text:
        return ("", [])
    removed = find_urls(text)
    cleaned = _URL_RE.sub("", text)
    # Tidy: drop lines that are now empty or punctuation-only (a dangling
    # "ご予約はこちら：" line left after its URL is removed is acceptable to keep,
    # but a line that became only spaces/punctuation is removed).
    lines = []
    for ln in cleaned.splitlines():
        stripped = ln.strip()
        if stripped == "" or re.fullmatch(r"[\s　:：・>＞\-—–／/]*", stripped):
            if lines and lines[-1] == "":
                continue
            lines.append("" if stripped == "" else stripped)
        else:
            lines.append(ln.rstrip())
    # Collapse 2+ consecutive blank lines into one and trim ends.
    out: list[str] = []
    for ln in lines:
        if ln == "" and out and out[-1] == "":
            continue
        out.append(ln)
    result = "\n".join(out).strip("\n")
    # Collapse stray double spaces left by inline URL removal.
    result = re.sub(r"[ \t]{2,}", " ", result)
    result = re.sub(r"[ \t]+([、。）)\]」』】])", r"\1", result)
    return (result, removed)


def sanitize_body(text: str | None, kind: str = "url") -> tuple[str, dict[str, Any]]:
    """Produce a body safe to resend. Currently URL-stripping covers both the
    ``url`` and ``char`` rejection cases (a URL is the usual offending content).

    Returns (new_text, diagnostics) where diagnostics = {changed, removed_urls, kind}.
    """
    new_text, removed = strip_urls(text)
    changed = new_text != (text or "")
    return new_text, {"changed": changed, "removed_urls": removed, "kind": kind}
