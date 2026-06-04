#!/usr/bin/env python3
"""Pure, dependency-free helpers for avoiding Japanese form validation errors.

Two classes of error were observed in production (e.g. the YAMAHA form):

  1. **Furigana script mismatch** — a フリガナ/カナ field received kanji (or the
     wrong kana), so the form rejects it with "...の形式が正しくありません".
  2. **Required field left empty** — e.g. お問い合わせタイトル (subject), rejected
     with "...を入力してください".

This module provides the *deciding* logic (script detection, label
classification, correct-value selection, and parsing the form's own inline
error messages) as pure functions so they can be unit-tested without a browser.
The run.py side only does DOM I/O and calls these to decide what to fix.
"""

from __future__ import annotations

import re
from typing import Any

# --- script detection --------------------------------------------------------
_KANJI_RE = re.compile(r"[\u4E00-\u9FFF\u3400-\u4DBF\u3005\u3007]")
_KATAKANA_ONLY_RE = re.compile(r"^[\u30A0-\u30FF\uFF66-\uFF9F\s\u3000]+$")
_HIRAGANA_ONLY_RE = re.compile(r"^[\u3040-\u309F\s\u3000\u30FC\u30FB]+$")


def contains_kanji(s: str | None) -> bool:
    return bool(_KANJI_RE.search(s or ""))


def is_katakana(s: str | None) -> bool:
    s = (s or "").strip()
    return bool(s) and bool(_KATAKANA_ONLY_RE.match(s))


def is_hiragana(s: str | None) -> bool:
    s = (s or "").strip()
    return bool(s) and bool(_HIRAGANA_ONLY_RE.match(s))


def katakana_to_hiragana(s: str | None) -> str:
    out = []
    for ch in s or "":
        o = ord(ch)
        # Convert full-width katakana (ァ..ヶ) to hiragana; keep ー/・/others.
        out.append(chr(o - 0x60) if 0x30A1 <= o <= 0x30F6 else ch)
    return "".join(out)


def hiragana_to_katakana(s: str | None) -> str:
    out = []
    for ch in s or "":
        o = ord(ch)
        out.append(chr(o + 0x60) if 0x3041 <= o <= 0x3096 else ch)
    return "".join(out)


# --- label classification ----------------------------------------------------
def expected_kana_kind(label: str | None) -> str | None:
    """Return ``"katakana"`` / ``"hiragana"`` if the label denotes a furigana
    (reading) field, else ``None``.

    カタカナ markers (フリガナ/カナ/katakana) win over ひらがな markers; ``ふりがな``
    in hiragana implies a hiragana reading field.
    """
    if not label:
        return None
    low = label.lower()
    if "フリガナ" in label or "カナ" in label or "ｶﾅ" in label or "katakana" in low:
        return "katakana"
    if "ふりがな" in label or "furigana" in low or "hiragana" in low:
        return "hiragana"
    # bare "kana" token
    if re.search(r"\bkana\b", low):
        return "katakana"
    return None


def name_part(label: str | None) -> str | None:
    """Which half of a split name does this label target? ``"sei"`` (姓/セイ),
    ``"mei"`` (名/メイ), or ``None`` (full name)."""
    if not label:
        return None
    if "セイ" in label or "せい" in label or "姓" in label or "myoji" in label.lower():
        return "sei"
    if "メイ" in label or "めい" in label or "名" in label:
        return "mei"
    return None


def _split_kana(full: str, sender: dict[str, Any], part: str, kind: str) -> str:
    """Pick the sei/mei portion, preferring explicit sender fields, then a
    2-char heuristic (matches the existing convention name_kana[:2] / [2:])."""
    suffix = "_kana" if kind == "katakana" else "_furigana"
    if part == "sei":
        return str(sender.get(f"name{suffix}_sei") or full[:2] or full)
    if part == "mei":
        return str(sender.get(f"name{suffix}_mei") or full[2:] or full)
    return full


def furigana_value_for_label(label: str | None, sender: dict[str, Any]) -> str | None:
    """The correct kana string to put in a furigana field, honoring the label's
    script (katakana/hiragana) and sei/mei split. ``None`` if not a kana field."""
    kind = expected_kana_kind(label)
    if not kind:
        return None
    if kind == "katakana":
        full = str(sender.get("name_kana") or "")
        if not full and sender.get("name_furigana"):
            full = hiragana_to_katakana(str(sender["name_furigana"]))
    else:
        full = str(sender.get("name_furigana") or "")
        if not full and sender.get("name_kana"):
            full = katakana_to_hiragana(str(sender["name_kana"]))
    if not full:
        return None
    return _split_kana(full, sender, name_part(label) or "", kind)


def kana_field_correction(
    label: str | None, value: str | None, sender: dict[str, Any]
) -> str | None:
    """The value a furigana field *should* hold, or ``None`` if no change needed.

    Two distinct failure modes are corrected:

      - **wrong script** (kanji / hiragana in a katakana field, etc.) — always fix.
      - **wrong split** — a 姓/名 sub-field that holds the full reading instead of
        just its half (the フリガナ（名）="シダノリミチ" case). Because we know our own
        sender's reading, sei/mei sub-fields are *enforced* to the expected value
        even when the current text is otherwise valid kana.

    A bare フリガナ field (no 姓/名 marker) is only corrected on script mismatch, so
    we never fight a legitimately-different full reading.
    """
    kind = expected_kana_kind(label)
    if not kind:
        return None
    expected = furigana_value_for_label(label, sender)
    if not expected:
        return None
    v = (value or "").strip()
    part = name_part(label)
    if part in ("sei", "mei"):
        return expected if v != expected else None
    # full-name furigana field: only correct a wrong script
    if needs_kana_fix(label, v):
        return expected
    return None


def needs_kana_fix(label: str | None, value: str | None) -> bool:
    """True when a furigana field's current value is the wrong script (e.g. kanji
    leaked into a katakana field). Empty values are handled by required-checks."""
    kind = expected_kana_kind(label)
    if not kind:
        return False
    v = (value or "").strip()
    if not v:
        return False
    if contains_kanji(v):
        return True
    if kind == "katakana":
        return not is_katakana(v)
    return not is_hiragana(v)


# --- subject / title fields --------------------------------------------------
_SUBJECT_LABEL_RE = re.compile(r"件名|題名|表題|タイトル|用件|サブジェクト|subject", re.IGNORECASE)


def is_subject_label(label: str | None) -> bool:
    """Heuristic: does this label denote an inquiry subject/title field?

    Excludes the message body (本文/内容/詳細) so we never clobber the textarea.
    """
    if not label:
        return False
    if re.search(r"本文|内容|詳細|メッセージ|message|備考", label, re.IGNORECASE) and not _SUBJECT_LABEL_RE.search(label):
        return False
    return bool(_SUBJECT_LABEL_RE.search(label))


def derive_subject(draft: dict[str, Any] | None, fallback: str = "サービスのご提案") -> str:
    """A safe subject value: the draft's own subject when usable, else a neutral
    B2B-appropriate default. Capped so it fits typical title fields."""
    subject = ""
    if isinstance(draft, dict):
        subject = str(draft.get("subject") or "").strip()
    if not subject or subject.upper() == "SKIP":
        subject = fallback
    return subject[:48]


# --- parsing the form's own inline error messages ----------------------------
_QUOTE_CHARS = "「」『』\"”“'’｢｣【】［］[]<>＜＞"

_ERR_FORMAT_RE = re.compile(r"(?P<f>.+?)\s*(?:の|が)?\s*形式が正しくありません")
_ERR_REQUIRED_RE = re.compile(
    r"(?P<f>.+?)\s*(?:を|は|の|が)?\s*(?:ご)?(?:入力|選択|指定|記入|チェック)し(?:て(?:ください)?)?"
)
_ERR_REQUIRED2_RE = re.compile(r"(?P<f>.+?)\s*は\s*必須")


def _clean_field(raw: str) -> str:
    return raw.strip().strip(_QUOTE_CHARS).strip().strip("*＊").strip().strip(_QUOTE_CHARS).strip()


def parse_validation_errors(text: str | None) -> list[dict[str, str]]:
    """Extract per-field validation errors from a page's text.

    Returns a list of ``{"field": <label>, "kind": "format"|"required", "raw": ...}``.
    Recognizes the common Japanese phrasings:
      - 「X」の形式が正しくありません  → format
      - 「X」を入力してください       → required
      - Xは必須です                  → required
    """
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _ERR_FORMAT_RE.search(line)
        if m:
            field = _clean_field(m.group("f"))
            key = (field, "format")
            if field and key not in seen:
                seen.add(key)
                out.append({"field": field, "kind": "format", "raw": line})
            continue
        m = _ERR_REQUIRED_RE.search(line) or _ERR_REQUIRED2_RE.search(line)
        if m:
            field = _clean_field(m.group("f"))
            key = (field, "required")
            if field and key not in seen:
                seen.add(key)
                out.append({"field": field, "kind": "required", "raw": line})
    return out


def has_validation_errors(text: str | None) -> bool:
    return bool(parse_validation_errors(text))
