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
from datetime import date, timedelta
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


# Compound words that contain 名/姓 but do NOT mean a split-name field.
# Without stripping these, 氏名/お名前/会社名 were misclassified as "mei" and the
# kana guard then enforced the mei-half into a FULL-name furigana field (bug).
_NAME_COMPOUND_RE = re.compile(
    r"(氏名|名前|名称|フルネーム|件名|題名|品名|書名|署名"
    r"|会社名|法人名|団体名|企業名|店舗名|学校名|施設名|部署名|役職名"
    r"|担当者名|ご担当名|ブランド名|サイト名|サービス名|商品名|旧姓)"
)


def name_part(label: str | None) -> str | None:
    """Which half of a split name does this label target? ``"sei"`` (姓/セイ),
    ``"mei"`` (名/メイ), or ``None`` (full name / not a name part).

    Compound words (氏名, お名前, 会社名, 件名, 旧姓 …) are stripped first so a
    full-name or non-name label never matches the bare 名/姓 markers.
    """
    if not label:
        return None
    low = label.lower()
    stripped = _NAME_COMPOUND_RE.sub("", label)
    if (
        "セイ" in stripped or "せい" in stripped or "姓" in stripped
        or "苗字" in label or "名字" in label
        or re.search(r"(?:last|family)[ _-]?name|myoji|\bsei\b", low)
    ):
        return "sei"
    # re-strip 名字/苗字 so their 名 does not leak into the mei check
    stripped_mei = stripped.replace("名字", "").replace("苗字", "")
    if (
        "メイ" in stripped_mei or "めい" in stripped_mei or "名" in stripped_mei
        or re.search(r"first[ _-]?name|\bmei\b", low)
    ):
        return "mei"
    return None


_NAME_SPACE_RE = re.compile(r"[\s　]+")


def split_jp_name(full: str | None) -> tuple[str, str]:
    """(sei, mei) from a full name string.

    Whitespace (incl. 全角) is the trusted separator; without it we fall back to
    the legacy 2-char-surname heuristic. Returns ("", "") for empty input.
    """
    s = (full or "").strip()
    if not s:
        return ("", "")
    parts = [p for p in _NAME_SPACE_RE.split(s) if p]
    if len(parts) >= 2:
        return (parts[0], "".join(parts[1:]))
    if len(s) > 2:
        return (s[:2], s[2:])
    return (s, "")


def sender_name_parts(sender: dict[str, Any]) -> dict[str, str]:
    """Resolve sei/mei for kanji + katakana + hiragana readings.

    Preference per variant: explicit sender keys (``name_sei`` /
    ``name_kana_sei`` / ``name_furigana_mei`` …) → whitespace split of the full
    string → 2-char heuristic. Missing kana variants are derived from the other
    script so a brief that only carries ``name_kana`` still yields ふりがな halves.
    """
    kanji = str(sender.get("name") or "")
    kana = str(sender.get("name_kana") or "")
    hira = str(sender.get("name_furigana") or "")
    if not kana and hira:
        kana = hiragana_to_katakana(hira)
    if not hira and kana:
        hira = katakana_to_hiragana(kana)
    out: dict[str, str] = {}
    for prefix, full in (("name", kanji), ("name_kana", kana), ("name_furigana", hira)):
        sei = str(sender.get(f"{prefix}_sei") or "").strip()
        mei = str(sender.get(f"{prefix}_mei") or "").strip()
        if not (sei and mei):
            s2, m2 = split_jp_name(full)
            sei = sei or s2
            mei = mei or m2
        out[f"{prefix}_sei"] = sei
        out[f"{prefix}_mei"] = mei
    return out


def _split_kana(full: str, sender: dict[str, Any], part: str, kind: str) -> str:
    """Pick the sei/mei portion: explicit sender fields → whitespace split →
    2-char heuristic (via sender_name_parts), falling back to ``full``."""
    if part not in ("sei", "mei"):
        return full
    suffix = "_kana" if kind == "katakana" else "_furigana"
    explicit = str(sender.get(f"name{suffix}_{part}") or "").strip()
    if explicit:
        return explicit
    resolved = sender_name_parts(sender).get(f"name{suffix}_{part}", "")
    if resolved:
        return resolved
    return (full[:2] if part == "sei" else full[2:]) or full


_COMPANY_LABEL_RE = re.compile(
    r"(会社名|法人名|団体名|企業名|店舗名|貴社名|御社名|会社\(名\)|company)",
    re.IGNORECASE,
)


def is_company_label(label: str | None) -> bool:
    """Whether the label denotes a company-name kana field (not a personal name)."""
    if not label:
        return False
    return bool(_COMPANY_LABEL_RE.search(label))


def furigana_value_for_label(label: str | None, sender: dict[str, Any]) -> str | None:
    """The correct kana string to put in a furigana field, honoring the label's
    script (katakana/hiragana) and sei/mei split. ``None`` if not a kana field.

    Company-name kana fields (会社名（フリガナ）/法人名カナ etc.) are routed to
    ``sender.company_kana`` / ``sender.company_furigana`` instead of the personal
    name reading; without this routing the kana-guard would overwrite the
    company-kana field with the担当者のカナ.
    """
    kind = expected_kana_kind(label)
    if not kind:
        return None
    if is_company_label(label):
        if kind == "katakana":
            val = str(sender.get("company_kana") or "")
            if not val and sender.get("company_furigana"):
                val = hiragana_to_katakana(str(sender["company_furigana"]))
        else:
            val = str(sender.get("company_furigana") or "")
            if not val and sender.get("company_kana"):
                val = katakana_to_hiragana(str(sender["company_kana"]))
        return val or None
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
    # Company-name kana field: we know the canonical value — always enforce it,
    # even when the current value is otherwise valid kana (e.g. シダノリミツ from
    # an earlier mis-fill).
    if is_company_label(label):
        return expected if v != expected else None
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
# 「電話番号を正しく入力してください」 (petline 2026-06-12) — a FORMAT complaint,
# not a required-field one. Must be tried before _ERR_REQUIRED_RE, which would
# otherwise mis-capture 「電話番号を正しく」 as a required field name.
_ERR_FORMAT2_RE = re.compile(
    r"(?P<f>.+?)\s*(?:を|は|の|が)?\s*正しく\s*(?:ご)?(?:入力|記入|指定)"
)
_ERR_REQUIRED_RE = re.compile(
    r"(?P<f>.+?)\s*(?:を|は|の|が)?\s*(?:ご)?(?:入力|選択|指定|記入|チェック)し(?:て(?:ください)?)?"
)
_ERR_REQUIRED2_RE = re.compile(r"(?P<f>.+?)\s*は\s*必須")
# 「住所（番地）は全角で入力してください」 (wacoal 2026-06-13) — a zenkaku-format
# bounce. Checked before _ERR_REQUIRED_RE, which would swallow it as required.
# Bare parenthesized hints 「（全角で入力してください）」 (petline static text)
# yield a junk 1-char field and are filtered by the length guard below.
_ERR_ZENKAKU_RE = re.compile(r"(?P<f>.+?)\s*(?:は|を)?\s*全角で(?:ご)?(?:入力|記入)")
# 「数値を半角で入力してください」 (keeper_giken 2026-06-19).  This is a
# format complaint, not a missing-value complaint.  It must run before the
# generic required matcher below, whose trailing 「入力してください」 otherwise
# misclassifies the message as ``required``.
_ERR_HANKAKU_NUMERIC_RE = re.compile(
    r"(?P<f>.*?)\s*数値を半角で(?:ご)?(?:入力|記入)"
)
# v30 next — three production format-class messages mis-classified as required
# (sunstar 2026-06-29). All run BEFORE _ERR_REQUIRED_RE so the trailing
# 「入力してください」 catch-all does not swallow them.
#
#   * 「X は全角64文字以内で入力してください」 → zenkaku (existing fixer)
#   * 「X はひらがなのみで入力してください」    → hiragana (new fixer)
#   * 「X は電話番号形式で入力してください」    → format with phone-y field
# Field prefix is OPTIONAL — production pilot redux #3 (sunstar 2026-06-29)
# showed pages emit bare "全角64文字以内で入力してください" with no preceding
# label, so the parser must still classify those as zenkaku rather than
# letting them fall through to _ERR_REQUIRED_RE. When the prefix is missing
# the captured field defaults to a generic placeholder downstream.
_ERR_ZENKAKU_LENGTH_RE = re.compile(
    r"(?P<f>.*?)\s*(?:は|を)?\s*全角\d+文字(?:以内|まで|程度)?で(?:ご)?(?:入力|記入)"
)
_ERR_HIRAGANA_RE = re.compile(
    r"(?P<f>.*?)\s*(?:は|を)?\s*ひらがな(?:のみ)?で(?:ご)?(?:入力|記入)"
)
_ERR_PHONE_FORMAT_RE = re.compile(
    r"(?P<f>.*?)\s*(?:は|を)?\s*電話番号(?:形式)?で(?:ご)?(?:入力|記入)"
)
_JP_WORD_RE = re.compile(r"[ぁ-んァ-ヶ一-龠a-zA-Z0-9]")

# v30 §WS-A — Playwright aria-snapshot tree leakage filter.
#
# Production runs (Fujisoft 2026-06-29, SUPER STUDIO / MIL 2026-06-27) fed the
# regex parser a string containing the aria-snapshot tree (e.g.
# 「- text: …検索エンジンに入力した…」, 「- row "氏名 必須" [ref=eXX]」). Body text
# inside `text:` nodes triggered the 〜してください regex, producing phantom
# "required" fields that the resolver could not match to any real input, so the
# wizard looped clicking 「次へ」 indefinitely.
#
# Two complementary guards:
#   1. _ARIA_TREE_LINE_RE skips structural tree lines that have no concrete
#      error content (a node descriptor like `- generic [ref=e42]` on its own).
#   2. _FIELD_LOOKS_LIKE_TREE_LEAK_RE rejects a captured field name whose head
#      looks like an aria-tree role token leaking through ("text: …",
#      'row "…', 'cell "…' etc.). Real form labels never start with those.
#
# We keep tree lines that DO contain a real inline error after the role prefix
# (e.g. `- generic [ref=e386]: 数値を半角で入力してください。`) — see
# test_hankaku_numeric_is_format_not_required.
_ARIA_TREE_ROLE_WORDS = (
    "text|row|cell|generic|paragraph|listitem|list|link|rowheader|rowgroup"
    "|table|img|textbox|checkbox|radio|combobox|searchbox|navigation|main"
    "|article|banner|contentinfo|complementary|heading|status|button"
    "|tabpanel|tablist|tab|dialog|menu|menuitem|menubar|switch|slider"
)
# A *bare* tree node line — no inline content after the descriptor. These are
# scaffolding (subtree headers / refs). We also catch URL leak lines.
_ARIA_TREE_LINE_RE = re.compile(
    rf"^\s*-\s+(?:{_ARIA_TREE_ROLE_WORDS})\b"
    rf'(?:\s+"[^"]*")?\s*(?:\[[^\]]*\])?\s*:?\s*$'
)
_ARIA_URL_LINE_RE = re.compile(r"^\s*-?\s*/url:\s*\S")
# A captured field name that starts with an aria-tree role token followed by
# the syntactic separator the snapshot uses (":", `"`, `[`, ` `) is a leak.
_FIELD_LOOKS_LIKE_TREE_LEAK_RE = re.compile(
    rf"^(?:{_ARIA_TREE_ROLE_WORDS})\s*(?::|\"|\[|$)"
)

# Real field labels are short. Privacy-policy paragraphs that leak through as
# "field names" are typically >30 chars because the regex captured a sentence
# fragment. Cap defensively so body-text false positives don't survive even if
# the leak filter misses a novel snapshot shape.
_FIELD_MAX_LEN = 30


def _clean_field(raw: str) -> str:
    s = raw.strip().strip(_QUOTE_CHARS).strip().strip("*＊").strip()
    # v30 §WS-A — leading list-bullet from aria-snapshot lines ("- text: …",
    # "- row \"…") must be stripped so the tree-leak detector can see the role
    # token at the start of the captured field.
    s = re.sub(r"^-+\s+", "", s).strip()
    return s.strip(_QUOTE_CHARS).strip()


# v30 next — broken-form-structure detection.
#
# A poorly-implemented JS framework can stringify HTMLInputElement / Object
# references straight into the `name` attribute, producing values like
# "[object HTMLInputElement]". The form's submit handler then never finds
# its own fields. Production observation 2026-06-30 (株式会社テンダ): the
# legacy path attempted fill + submit and finally timed out at 300s. We
# detect this early so the target skips immediately with a precise reason
# instead of burning the timeout budget.
_BROKEN_NAME_RE = re.compile(r"^\[object\s+\w+\]$")


def field_name_is_broken(name: Any) -> bool:
    """True when ``name`` looks like a JS object stringified into the DOM
    (`"[object HTMLInputElement]"` etc.). Empty / missing names are NOT
    treated as broken — they fall through to the existing label-based fill
    heuristics.
    """
    if not name:
        return False
    return bool(_BROKEN_NAME_RE.match(str(name).strip()))


def form_has_broken_structure(fields: dict[str, Any] | None) -> bool:
    """True when at least one input/textarea/select in ``fields`` carries a
    broken object-stringification name. Even one such field is enough to
    abort: the page's submit handler typically can't address ANY of its
    fields once this pattern surfaces.
    """
    if not isinstance(fields, dict):
        return False
    for group in ("inputs", "textareas", "selects"):
        for item in fields.get(group) or []:
            if not isinstance(item, dict):
                continue
            if field_name_is_broken(item.get("name")):
                return True
    return False


def _is_field_capture_valid(field: str) -> bool:
    """Reject captured 'field' strings that came from aria-snapshot leakage or
    are too long to plausibly be a form label."""
    if not field:
        return False
    if len(field) > _FIELD_MAX_LEN:
        return False
    if _FIELD_LOOKS_LIKE_TREE_LEAK_RE.match(field):
        return False
    return True


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
        # v30 §WS-A — drop pure aria-snapshot scaffolding lines. Lines that have
        # real inline content after the role descriptor still flow through
        # (e.g. `- generic [ref=e386]: 数値を半角で入力してください。`).
        if _ARIA_TREE_LINE_RE.match(line) or _ARIA_URL_LINE_RE.match(line):
            continue
        m = _ERR_FORMAT_RE.search(line) or _ERR_FORMAT2_RE.search(line)
        if m:
            field = _clean_field(m.group("f"))
            if not _is_field_capture_valid(field):
                continue
            key = (field, "format")
            if key not in seen:
                seen.add(key)
                out.append({"field": field, "kind": "format", "raw": line})
            continue
        m = _ERR_ZENKAKU_RE.search(line)
        if m:
            field = _clean_field(m.group("f"))
            # length/content guard: drop bare hints like 「（全角で入力してください）」
            if (
                len(field) >= 2
                and _JP_WORD_RE.search(field)
                and _is_field_capture_valid(field)
            ):
                key = (field, "zenkaku")
                if key not in seen:
                    seen.add(key)
                    out.append({"field": field, "kind": "zenkaku", "raw": line})
            continue
        m = _ERR_HANKAKU_NUMERIC_RE.search(line)
        if m:
            field = _clean_field(m.group("f")) or "数値"
            # The hankaku-numeric rule legitimately allows aria-tree decorations
            # to lead the line (e.g. `- generic [ref=eN]:` before 「数値を…」).
            # We only reject if the cleaned field is itself a tree leak. The
            # default 「数値」 fallback is always acceptable.
            if field != "数値" and not _is_field_capture_valid(field):
                field = "数値"
            key = (field, "hankaku_numeric")
            if key not in seen:
                seen.add(key)
                out.append({"field": field, "kind": "hankaku_numeric", "raw": line})
            continue
        m = _ERR_ZENKAKU_LENGTH_RE.search(line)
        if m:
            field = _clean_field(m.group("f"))
            if not field or not _is_field_capture_valid(field):
                field = "全角文字"  # generic fallback for prefix-less messages
            elif len(field) < 2 or not _JP_WORD_RE.search(field):
                field = "全角文字"
            key = (field, "zenkaku")
            if key not in seen:
                seen.add(key)
                out.append({"field": field, "kind": "zenkaku", "raw": line})
            continue
        m = _ERR_HIRAGANA_RE.search(line)
        if m:
            field = _clean_field(m.group("f"))
            if not field or not _is_field_capture_valid(field):
                field = "ふりがな"
            elif len(field) < 2 or not _JP_WORD_RE.search(field):
                field = "ふりがな"
            key = (field, "hiragana")
            if key not in seen:
                seen.add(key)
                out.append({"field": field, "kind": "hiragana", "raw": line})
            continue
        m = _ERR_PHONE_FORMAT_RE.search(line)
        if m:
            field = _clean_field(m.group("f"))
            if not field or not _is_field_capture_valid(field):
                field = "電話番号"
            key = (field, "format")
            if key not in seen:
                seen.add(key)
                out.append({"field": field, "kind": "format", "raw": line})
            continue
        m = _ERR_REQUIRED_RE.search(line) or _ERR_REQUIRED2_RE.search(line)
        if m:
            field = _clean_field(m.group("f"))
            if not _is_field_capture_valid(field):
                continue
            key = (field, "required")
            if key not in seen:
                seen.add(key)
                out.append({"field": field, "kind": "required", "raw": line})
    return out


def has_validation_errors(text: str | None) -> bool:
    return bool(parse_validation_errors(text))


def to_zenkaku(value: str | None) -> str:
    """Convert ASCII chars (digits, letters, symbols, space) to full-width.

    For 「全角で入力してください」 bounces (wacoal 番地/建物名 class). Non-ASCII
    chars (kana/kanji, existing full-width) pass through unchanged, so the
    conversion is idempotent and safe to apply to mixed strings.
    """
    out: list[str] = []
    for ch in value or "":
        o = ord(ch)
        if o == 0x20:
            out.append("　")
        elif 0x21 <= o <= 0x7E:
            out.append(chr(o + 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


_POSTAL_FIELD_RE = re.compile(r"(郵便番号|postal(?:code)?|post[_ -]?code|zip(?:code)?)", re.IGNORECASE)
_FULLWIDTH_DIGIT_TRANS = str.maketrans("０１２３４５６７８９", "0123456789")


def is_postal_field_label(label: str | None) -> bool:
    """Whether a field label/name denotes a postal code input."""
    return bool(_POSTAL_FIELD_RE.search((label or "").strip()))


def normalize_postal_code(value: str | None) -> str:
    """Return a JP postal code as seven ASCII digits when safely possible.

    Inquiry forms disagree about whether ``260-0003`` is accepted, while the
    digits-only representation is accepted by the common seven-digit validators.
    The function is content-preserving: values that do not normalize to exactly
    seven digits are returned unchanged.
    """
    original = (value or "").strip()
    if not original:
        return original
    ascii_value = original.translate(_FULLWIDTH_DIGIT_TRANS)
    digits = re.sub(r"[^0-9]", "", ascii_value)
    return digits if len(digits) == 7 else original


_PHONE_FIELD_RE = re.compile(r"(電話|TEL|tel|phone|携帯|連絡先番号)", re.IGNORECASE)


def is_phone_field_label(label: str | None) -> bool:
    return bool(_PHONE_FIELD_RE.search((label or "").strip()))


def toggle_phone_hyphens(value: str | None) -> str:
    """Flip a JP phone number between hyphenated and digits-only form.

    Sites disagree on format — petline demands ハイフンなし while others demand
    ハイフンあり — and both forms are valid representations of the sender's own
    number, so toggling is always content-preserving. Hyphenated → digits only;
    digits only → 3-4-4 (11 digits, mobile/0120…) or 2-4-4 (10 digits). Values
    that don't look like a JP phone number are returned unchanged.
    """
    v = (value or "").strip()
    if not v:
        return v
    digits = re.sub(r"[-‐－ー\s().（）]", "", v)
    if not digits.isdigit() or not 10 <= len(digits) <= 11:
        return v
    if digits != v:  # had separators → strip them
        return digits
    if len(digits) == 11:  # 090-1234-5678 / 0120-345-678 style → 3-4-4
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    return f"{digits[:2]}-{digits[2:6]}-{digits[6:]}"  # 10 digits → 2-4-4


# --- v15 §S2: special field value helpers ------------------------------------
def default_date_value(today: date | None = None) -> str:
    """ISO date 7 business days (Mon–Fri) from today — for 希望日/date fields."""
    d = today or date.today()
    remaining = 7
    while remaining > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            remaining -= 1
    return d.isoformat()


# --- label-aware option choice for selects / radios ---------------------------
# Labels we know how to answer deterministically even when the group is not an
# inquiry-type field. Used by submit gates and the heuristic fill pass.
KNOWN_CHOICE_LABEL_RE = re.compile(
    r"(都道府県|prefecture|従業員|社員数|会社規模|企業規模|業種|業界|industry"
    r"|きっかけ|お?知りになった|知った|経由|媒体|どちらで|予算|budget"
    r"|連絡方法|返信方法|ご返答|回答方法|連絡手段|性別|役職|職種)",
    re.IGNORECASE,
)


def choose_option_for_label(
    label: str | None,
    options: list[Any] | None,
    sender: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """Pick the most appropriate option for a select/radio given its LABEL.

    Pure decision function. Known label categories get a deterministic answer
    (都道府県→sender's prefecture, 従業員数→smallest band, きっかけ→その他, 連絡方法→
    メール, …); anything else falls back to the B2B-preference scorer and finally
    to neutral options (その他/未定). Returns ``{"value", "reason"}`` or ``None``
    when no safe choice exists (caller should escalate, not guess).
    """
    from _outreach_core import submit_progress as sp  # lazy: avoid import cycle

    lab = (label or "").strip()
    opts = sp._normalize_options(options or [])
    usable = [
        o for o in opts
        if not o.get("disabled") and not o.get("selected")
        and not sp._is_placeholder_option(o)
    ]
    if not usable:
        return None

    def _find(*keywords: str, reason: str) -> dict[str, str] | None:
        for o in usable:
            text = f"{o.get('label') or ''} {o.get('value') or ''}".lower()
            if any(k.lower() in text for k in keywords if k):
                return {"value": str(o.get("label") or o.get("value")), "reason": reason}
        return None

    s = sender or {}
    if re.search(r"都道府県|prefecture|地域|エリア", lab, re.IGNORECASE):
        pref = str(s.get("prefecture") or "").strip()
        return _find(pref, reason="sender_prefecture") if pref else None
    if re.search(r"従業員|社員数|会社規模|企業規模|人数規模", lab):
        return (
            _find("10名以下", "10人以下", "1〜10", "1～10", "1-10", "10未満", "〜10名",
                  reason="employee_smallest_band")
            or _find("50名以下", "11〜", "11～", "〜50", reason="employee_small_band")
            or {"value": str(usable[0].get("label") or usable[0].get("value")),
                "reason": "employee_first_band"}
        )
    if re.search(r"業種|業界|industry", lab, re.IGNORECASE):
        kws = str(s.get("industry_keywords") or "サービス 小売 通信販売 EC IT").split()
        for kw in kws:
            hit = _find(kw, reason=f"industry:{kw}")
            if hit:
                return hit
        return _find("その他", reason="industry_other")
    if re.search(r"きっかけ|お?知りになった|知った|経由|媒体|どちらで", lab):
        return (
            _find("その他", reason="referral_other")
            or _find("検索", "web", "サイト", "インターネット", reason="referral_web")
        )
    if re.search(r"予算|budget", lab, re.IGNORECASE):
        return (
            _find("未定", reason="budget_undecided")
            or _find("その他", "わからない", "決まっていない", reason="budget_other")
        )
    if re.search(r"連絡方法|返信方法|ご返答|回答方法|連絡手段", lab):
        return _find("メール", "mail", reason="contact_by_email")
    if re.search(r"性別", lab):
        g = str(s.get("gender") or "").strip()
        return _find(g, reason="sender_gender") if g else None
    if re.search(r"役職|職種", lab):
        return (
            _find("経営", "役員", "代表", "経営者", reason="role_executive")
            or _find("その他", reason="role_other")
        )
    # Unknown / inquiry-type-ish label → B2B preference scorer, then neutral.
    picked = sp.choose_b2b_option(options or [])
    if picked and str(picked.get("value") or "").strip():
        return {"value": str(picked["value"]),
                "reason": f"b2b:{picked.get('reason', '')}"}
    return (
        _find("その他", reason="generic_other")
        or _find("未定", "該当なし", reason="generic_undecided")
    )


_PREF_RE = re.compile(
    r"^(東京都|北海道|(?:京都|大阪)府|.{2,3}県)"
)
_CITY_RE = re.compile(
    # 市/区/郡+町村: greedy enough for compound names like 千葉市中央区
    r"^((?:.+?郡.+?[町村])|(?:.+?市.+?区)|(?:.+?[市区町村]))"
)


def split_jp_address(addr: str | None) -> dict[str, str]:
    """Split a single-line Japanese address into prefecture/city/line/building.

    Pure heuristic for forms that demand split address fields when the brief
    only has one string. Building = trailing token after whitespace when it
    contains ビル/階/号室/マンション etc.
    """
    out = {"prefecture": "", "city": "", "address_line": "", "building": ""}
    s = (addr or "").strip().replace("　", " ")
    if not s:
        return out
    m = _PREF_RE.match(s)
    if m:
        out["prefecture"] = m.group(1)
        s = s[m.end():].strip()
    m = _CITY_RE.match(s)
    if m:
        out["city"] = m.group(1)
        s = s[m.end():].strip()
    # Trailing building part: last whitespace-separated token with a building marker
    parts = s.split(" ")
    if len(parts) > 1 and re.search(r"(ビル|タワー|マンション|ハイツ|階|F$|号室|号館)", parts[-1]):
        out["building"] = parts[-1]
        s = " ".join(parts[:-1]).strip()
    out["address_line"] = s
    return out
