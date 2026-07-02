"""Pure helpers for submit-progress gating (v7 §A)."""

from __future__ import annotations

import re
from typing import Any

_AGREEMENT_RE = re.compile(
    r"(同意|同意する|個人情報|プライバシー|プライバシーポリシー|利用規約|取扱いについて|取り扱いについて)",
    re.I,
)
_RADIO_GATE_GROUP_RE = re.compile(
    r"(お問い合わせ種別|問合せ種別|カテゴリ|区分|お問い合わせ内容|ご相談内容|個人|法人|種別)",
    re.I,
)
_INQUIRY_TYPE_RE = re.compile(
    r"(お問い合わせ種別|問合せ種別|問い合わせ区分|カテゴリ|区分|件名|ご用件|種別|contact|subject)",
    re.I,
)
_RADIO_OPTION_POSITIVE_RE = re.compile(
    r"(法人|企業|営業|商品提案|ご提案|協業|提携|取材|その他|biz|business)",
    re.I,
)
_RADIO_OPTION_NEGATIVE_RE = re.compile(
    r"(個人|採用|応募|ir|投資家|予約|サポート|修理|返品|faq|会員|ログイン)",
    re.I,
)
_STRONG_PREFER_RE = re.compile(
    r"(法人のお客様|企業・団体|企業|法人|お取引先|お取引|お仕事のご依頼|業務提携|協業|ビジネス|OEM|卸|代理店|業務用)",
    re.I,
)
_WEAK_PREFER_RE = re.compile(
    r"(提案|取引|提携|協業|営業|business|biz|法人|企業)",
    re.I,
)
_STRONG_AVOID_RE = re.compile(
    r"(個人のお客様|商品・サービスについて|ご意見・ご感想|お客様相談|店舗について|採用|アルバイト|予約)",
    re.I,
)
_WEAK_AVOID_RE = re.compile(
    r"(個人|採用|応募|ir|投資家|株主|サポート|修理|返品|faq|会員|ログイン|忘れ物|苦情)",
    re.I,
)
_PLACEHOLDER_RE = re.compile(
    r"^(?:[-ー−‐~\s]*)?(?:以下から選択|選択してください|選択して下さい|please select|select|お選びください|指定なし).*$",
    re.I,
)
_NOISE_SUBMIT_TEXT_RE = re.compile(
    r"^(こちら|詳細|戻る|一覧|トップ|個人情報の取扱い|プライバシー|privacy|policy)$",
    re.I,
)
_FIRST_STEP_TEXT_RE = re.compile(r"(入力内容を確認|内容(を|の)?確認|確認画面|同意して次へ|^次へ$)", re.I)
_FINAL_STEP_TEXT_RE = re.compile(r"(送信|submit|完了|確定|問い合わせを送信|お問い合わせを送信)", re.I)
_ROUTE_GROUP_RE = re.compile(r"(お客様|個人|法人|企業|一般|取引|お問い合わせ対象|お問い合わせ区分)", re.I)
_SENSITIVE_DESTINATION_RE = re.compile(
    r"(お問い合わせ先|送信先|店舗|店鋪|ホテル|施設|拠点|支店|営業所|会場|校舎|教室|"
    r"location|store|hotel|branch|property)",
    re.I,
)


# Confirmation-page instruction text. Many JP forms render a sentence telling
# the human to click send to finalize — "上記の内容でよろしければ送信ボタンを
# クリックしてください" — instead of (or alongside) a regex-matchable button. Used
# as a STRONG "this is the final-submit page" signal to confidently click send.
_CONFIRM_PRE_RE = re.compile(
    r"(上記の?内容|ご記入(の)?内容|入力(された)?内容|ご入力内容|下記の?内容|"
    r"内容をご確認|ご確認の上|ご確認のうえ|お間違い|よろしければ|よろしけれ|"
    r"確認の上|間違いがなけれ|問題なけれ)",
)
_CONFIRM_SEND_RE = re.compile(
    r"(送信|送信ボタン|お送り|送付|確定|完了)(する)?[」』]?\s*(ボタン)?\s*"
    r"(を|に)?\s*(押して|押下|クリック|タップ|してください|して下さい|"
    r"ください|下さい|お願い|送信)",
)
# A bare imperative "送信ボタンを押してください" without the 内容 preamble.
# (する)?[」』]? tolerates quoted button-label citations — 「この内容で送信する」
# ボタンを押してください (baycrews 2026-06-12) — where the verb and ボタン are
# separated by する and a closing quote.
_CLICK_SEND_RE = re.compile(
    r"(送信|確定|完了)(する)?[」』]?(ボタン)?(を)?(押して|押下|クリックして|タップして)",
)


def detect_confirm_instruction(page_text: str | None) -> bool:
    """True if the page TEXT instructs the user to click send to finalize.

    Two shapes:
      1. "上記の内容で…よろしければ…送信ボタンをクリック" (content-confirm preamble + send)
      2. "送信ボタンを押してください" (bare imperative)
    This is a page-level signal, independent of whether any button's own label
    matched the submit regexes — it lets us click a generically-labelled (or
    image) send button with confidence on a confirm page.
    """
    t = (page_text or "").replace("\n", " ")
    if not t:
        return False
    if _CLICK_SEND_RE.search(t):
        return True
    # preamble + a send verb within a reasonable window
    m = _CONFIRM_PRE_RE.search(t)
    if m and _CONFIRM_SEND_RE.search(t[m.start(): m.start() + 120]):
        return True
    return False


def wizard_should_continue(page_text: str | None, visible_textareas: int) -> bool:
    """v15 §S1 — should the wizard loop take another step?

    Continue only while the page shows NO success keyword AND a visible
    textarea remains (we are still on an input step of a multi-step form).
    """
    from _outreach_core.verify import FORM_SUCCESS_KEYWORDS  # no import cycle

    hay = (page_text or "").casefold()
    if any(k.casefold() in hay for k in FORM_SUCCESS_KEYWORDS):
        return False
    return int(visible_textareas or 0) > 0


def is_agreement_label(label: str | None) -> bool:
    text = (label or "").strip()
    if not text:
        return False
    return bool(_AGREEMENT_RE.search(text))


_NEWSLETTER_RE = re.compile(
    r"(メルマガ|メールマガジン|ニュースレター|案内を希望|配信)",
    re.I,
)


def is_newsletter_label(label: str | None) -> bool:
    return bool(_NEWSLETTER_RE.search((label or "").strip()))


def should_auto_check_checkbox(box: dict[str, Any] | None) -> bool:
    if not isinstance(box, dict):
        return False
    if bool(box.get("checked")):
        return False
    label = str(box.get("label") or "")
    required = bool(box.get("required"))
    # v15 §S2: never opt the sender into newsletters — an optional
    # newsletter/配信 checkbox is left unchecked even if it pattern-matches.
    if not required and is_newsletter_label(label):
        return False
    return required or is_agreement_label(label)


# v30 §WS-A — concept-separated checkbox pickers.
#
# Previously a single ``pick_checkboxes_to_check`` collapsed three semantically
# distinct decisions:
#
#   (a) Consent / agreement boxes  — 個人情報の取扱いに同意, 利用規約 …
#   (b) Inquiry-type checkbox GROUPS — pick exactly one safe option
#   (c) Standalone required boxes  — a lone required checkbox without a group
#
# Mixing them made unit-testing each rule independently awkward and meant a
# brief-specific tweak to one concept could not be expressed without touching
# the others. The three helpers below capture each concept in isolation; the
# original ``pick_checkboxes_to_check`` is now a thin facade that concatenates
# their outputs (preserving every existing call site).


def _iter_actionable(checkboxes: list[dict[str, Any]] | None):
    """Yield checkboxes that pass :func:`should_auto_check_checkbox`."""
    if not isinstance(checkboxes, list):
        return
    for box in checkboxes:
        if should_auto_check_checkbox(box):
            yield box


def pick_consent_actions(
    checkboxes: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Consent / agreement checkboxes (privacy policy, terms of use).

    Identified by :func:`is_agreement_label`. Always a single-checkbox commit
    — never grouped because each agreement is independent.
    """
    return [
        box
        for box in _iter_actionable(checkboxes)
        if is_agreement_label(str(box.get("label") or ""))
    ]


def pick_inquiry_type_actions(
    checkboxes: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Inquiry-type checkbox GROUPS — pick exactly one safe option per group.

    A shared visual 必須 marker on a group ("以下から選択してください") means the
    operator must commit at least one option, not all. Checking every option
    fabricates preferences and can itself fail validation, so we route the
    group's options through :func:`choose_b2b_option` and pick the single
    safest one.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for box in _iter_actionable(checkboxes):
        label = str(box.get("label") or "")
        if is_agreement_label(label):
            continue
        group_label = str(box.get("group_label") or "").strip()
        if not group_label:
            continue
        grouped.setdefault(group_label, []).append(box)
    out: list[dict[str, Any]] = []
    for boxes in grouped.values():
        if len(boxes) == 1:
            out.append(boxes[0])
            continue
        picked = _pick_checkbox_group_candidate(boxes)
        if picked is not None:
            out.append(picked)
    return out


def pick_required_checkbox_actions(
    checkboxes: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Standalone required checkboxes that aren't agreements and aren't part
    of a multi-option group. A lone required box (e.g. 「キャンペーンに参加する」)
    falls here.
    """
    return [
        box
        for box in _iter_actionable(checkboxes)
        if not is_agreement_label(str(box.get("label") or ""))
        and not str(box.get("group_label") or "").strip()
    ]


def pick_checkboxes_to_check(
    checkboxes: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Backward-compatible facade — concatenates consent + inquiry-type group
    + standalone required picks in that order. Existing call sites keep
    working without change."""
    return (
        pick_consent_actions(checkboxes)
        + pick_inquiry_type_actions(checkboxes)
        + pick_required_checkbox_actions(checkboxes)
    )


def _pick_checkbox_group_candidate(
    boxes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    options = [
        {
            "label": str(box.get("label") or ""),
            "value": str(box.get("value") or ""),
            "selected": bool(box.get("checked")),
            "disabled": bool(box.get("disabled")),
        }
        for box in boxes
    ]
    picked = choose_b2b_option(options)
    wanted = str((picked or {}).get("value") or "").strip()
    if not wanted:
        wanted = _pick_neutral_or_first_option(options, allow_first=False) or ""
    if not wanted:
        return None
    for box in boxes:
        if bool(box.get("disabled")) or bool(box.get("checked")):
            continue
        if wanted in {
            str(box.get("label") or "").strip(),
            str(box.get("value") or "").strip(),
        }:
            return box
    return None


def pick_radio_gate_actions(
    groups: list[dict[str, Any]] | None,
    sender: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Choose radio options that are likely required submit gates.

    Input schema (dict keys):
      - name: radio group name
      - label: group/fieldset label
      - required: bool
      - selected: bool
      - options: [{label, value, checked}]
    Output:
      - [{name: group_name, value: option_label_or_value}]

    Label-aware first (性別/連絡方法/きっかけ… via form_validation.
    choose_option_for_label), then the B2B-preference picker.
    """
    from _outreach_core import form_validation as fv  # lazy: avoid import cycle

    if not isinstance(groups, list):
        return []
    actions: list[dict[str, str]] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        name = str(g.get("name") or "").strip()
        if not name:
            continue
        if bool(g.get("selected")):
            continue
        label = str(g.get("label") or "")
        required = bool(g.get("required"))
        if (
            not required
            and not _RADIO_GATE_GROUP_RE.search(label)
            and not fv.KNOWN_CHOICE_LABEL_RE.search(label)
        ):
            continue
        labelled = fv.choose_option_for_label(label, g.get("options") or [], sender)
        choice = str((labelled or {}).get("value") or "").strip() or _pick_radio_option(
            g.get("options") or []
        )
        if not choice:
            continue
        actions.append({"name": name, "value": choice})
    return actions


def _pick_radio_option(options: list[dict[str, Any]]) -> str | None:
    normalized: list[dict[str, Any]] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        normalized.append(
            {
                "label": option.get("label"),
                "value": option.get("value"),
                "selected": bool(option.get("checked")),
                "disabled": bool(option.get("disabled")),
            }
        )
    picked = choose_b2b_option(normalized)
    if not picked:
        for opt in normalized:
            if bool(opt.get("selected")) or bool(opt.get("disabled")):
                continue
            if _is_placeholder_option(opt):
                continue
            label = str(opt.get("label") or "")
            value = str(opt.get("value") or "")
            if "その他" in f"{label} {value}":
                return label or value
        return None
    choice = str(picked.get("value") or "").strip()
    if not choice:
        return None
    return choice


# Validation-rescue option preference: when the page has ALREADY bounced us
# with 「選択してください」, an unanswered radio group must get SOME answer.
# Prefer neutral/negative options (no false claims like 取引実績あり or
# メルマガ希望); fall back to the first enabled option.
_NEUTRAL_OPTION_RE = re.compile(
    r"(その他|該当しない|該当なし|未定|検討中|特になし|^ない$|^無し$|^なし$|いいえ|希望しない)",
)


def pick_validation_radio_actions(
    groups: list[dict[str, Any]] | None,
    sender: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Aggressive radio rescue for the validation_error state (kakuyasu class).

    Unlike :func:`pick_radio_gate_actions`, this targets EVERY unselected
    group — the page's own validation error is the evidence that a selection
    is missing, so required-attribute / known-label gating no longer applies.
    Choice order: label-aware chooser → B2B preference → neutral/negative
    option → first enabled non-placeholder option.
    """
    from _outreach_core import form_validation as fv  # lazy: avoid import cycle

    if not isinstance(groups, list):
        return []
    actions: list[dict[str, str]] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        name = str(g.get("name") or "").strip()
        if not name or bool(g.get("selected")):
            continue
        label = str(g.get("label") or "")
        options = g.get("options") or []
        labelled = fv.choose_option_for_label(label, options, sender)
        choice = str((labelled or {}).get("value") or "").strip() or _pick_radio_option(options)
        if not choice:
            normalized = _normalize_options(options)
            neutral = None
            first_ok = None
            for opt in normalized:
                if bool(opt.get("selected")) or bool(opt.get("disabled")):
                    continue
                if _is_placeholder_option(opt):
                    continue
                olabel = str(opt.get("label") or "").strip()
                ovalue = str(opt.get("value") or "").strip()
                if not olabel and not ovalue:
                    continue
                if first_ok is None:
                    first_ok = opt
                if neutral is None and (
                    _NEUTRAL_OPTION_RE.search(olabel) or _NEUTRAL_OPTION_RE.search(ovalue)
                ):
                    neutral = opt
            picked = neutral or first_ok
            if picked:
                choice = str(picked.get("label") or picked.get("value") or "").strip()
        if not choice:
            continue
        actions.append({"name": name, "value": choice})
    return actions


def _pick_neutral_or_first_option(
    options: list[Any] | None,
    *,
    allow_first: bool = False,
) -> str | None:
    """Pick a safe neutral option, or the first enabled option when allowed.

    Normal pre-submit filling should not guess arbitrary values for unknown
    required controls. After the page has already bounced with a validation
    error, however, choosing the first non-placeholder option is often the only
    way to progress through custom required selects/radios that expose no useful
    label.
    """
    normalized = _normalize_options(options or [])
    first_ok: dict[str, Any] | None = None
    for opt in normalized:
        if bool(opt.get("selected")) or bool(opt.get("disabled")):
            continue
        if _is_placeholder_option(opt):
            continue
        label = str(opt.get("label") or "").strip()
        value = str(opt.get("value") or "").strip()
        if not label and not value:
            continue
        if first_ok is None:
            first_ok = opt
        if _NEUTRAL_OPTION_RE.search(label) or _NEUTRAL_OPTION_RE.search(value):
            return label or value
    if allow_first and first_ok:
        return str(first_ok.get("label") or first_ok.get("value") or "").strip() or None
    return None


def is_inquiry_type_field(field: dict[str, Any] | None) -> bool:
    if not isinstance(field, dict):
        return False
    name = str(field.get("name") or field.get("id") or "")
    label = str(field.get("label") or "")
    blob = f"{name} {label}".strip()
    if not blob:
        return False
    return bool(_INQUIRY_TYPE_RE.search(blob))


def validate_choice(options: list[Any] | None, chosen: str | None) -> bool:
    selected = (chosen or "").strip()
    if not selected:
        return False
    candidates = _normalize_options(options or [])
    for opt in candidates:
        if bool(opt.get("disabled")):
            continue
        label = str(opt.get("label") or "").strip()
        value = str(opt.get("value") or "").strip()
        if _is_placeholder_option(opt):
            continue
        if selected in {label, value}:
            return True
    return False


# v30 §WS-A — B2B option scoring config.
#
# The +6 / +3 / -8 / -4 / sonota / floor numbers were hard-coded inside
# choose_b2b_option, so a brief that wanted to soften the bias (e.g. ECC, where
# the historically-strong "資料請求" avoid keyword is the right answer) had no
# legitimate way to override without forking the module. These constants are
# the legacy defaults; callers may pass a partial overlay via ``scores=`` or
# ``brief_b2b_scoring(...)``.
B2B_DEFAULT_SCORES: dict[str, int | float] = {
    "strong_prefer": 6,
    "weak_prefer": 3,
    "strong_avoid": -8,
    "weak_avoid": -4,
    "sonota_bonus": 1,
    # Decision floors — a top score below ``min_top_score`` is rejected so we
    # never pick a "least bad" option that nothing actually preferred.
    "min_top_score": 2,
    # Confidence demotion: top scores below this OR within ``confidence_gap``
    # of the second option are flagged "low" so the caller can escalate.
    "high_confidence_floor": 5,
    "confidence_gap": 1,
}


def b2b_scores_from_config(
    config: Any,
    *,
    defaults: dict[str, int | float] | None = None,
) -> dict[str, int | float]:
    """Merge ``defaults`` with the brief / persona overlay at
    ``b2b_scoring`` (top-level) or ``send.b2b_scoring``. Missing keys keep
    their default value; unknown keys are ignored (so a yaml typo cannot
    silently change behaviour). Non-numeric overlay values are ignored.
    """
    base = dict(defaults or B2B_DEFAULT_SCORES)
    if not isinstance(config, dict):
        return base
    overlays: list[Any] = []
    overlays.append(config.get("b2b_scoring"))
    send_block = config.get("send")
    if isinstance(send_block, dict):
        overlays.append(send_block.get("b2b_scoring"))
    for overlay in overlays:
        if not isinstance(overlay, dict):
            continue
        for key, value in overlay.items():
            if key not in base:
                continue
            if isinstance(value, bool):  # bool is a subclass of int — reject
                continue
            if isinstance(value, (int, float)):
                base[key] = value
    return base


def choose_b2b_option(
    options: list[Any] | None,
    *,
    scores: dict[str, int | float] | None = None,
) -> dict[str, Any] | None:
    """Pick the best B2B-aligned option from a select/radio/checkbox group.

    The scoring weights default to :data:`B2B_DEFAULT_SCORES` but callers may
    pass a brief-specific overlay via ``scores`` (already merged by
    :func:`b2b_scores_from_config`). All numeric thresholds are read from the
    same mapping so a partial overlay can soften or sharpen any axis without
    forking the module.
    """
    s = dict(scores or B2B_DEFAULT_SCORES)
    candidates = _normalize_options(options or [])
    scored: list[dict[str, Any]] = []
    prefer_hits = 0
    sonota_candidate: dict[str, Any] | None = None
    for opt in candidates:
        if bool(opt.get("selected")) or bool(opt.get("disabled")):
            continue
        if _is_placeholder_option(opt):
            continue
        label = str(opt.get("label") or "").strip()
        value = str(opt.get("value") or "").strip()
        text = f"{label} {value}".strip()
        if not text:
            continue
        strong_prefer = bool(_STRONG_PREFER_RE.search(text))
        weak_prefer = bool(_WEAK_PREFER_RE.search(text))
        strong_avoid = bool(_STRONG_AVOID_RE.search(text))
        weak_avoid = bool(_WEAK_AVOID_RE.search(text))
        score: float = 0
        if strong_prefer:
            score += s["strong_prefer"]
        elif weak_prefer:
            score += s["weak_prefer"]
        if strong_avoid:
            score += s["strong_avoid"]
        elif weak_avoid:
            score += s["weak_avoid"]
        if "その他" in text:
            score += s["sonota_bonus"]
            if sonota_candidate is None:
                sonota_candidate = {
                    "value": label or value,
                    "score": int(s["sonota_bonus"]),
                    "confidence": "low",
                    "reason": "fallback_sonota",
                }
        if strong_prefer or weak_prefer:
            prefer_hits += 1
        scored.append(
            {
                "value": label or value,
                "score": score,
                "strong_prefer": strong_prefer,
                "strong_avoid": strong_avoid,
                "label": label,
            }
        )
    if not scored:
        return None
    if prefer_hits <= 0:
        return sonota_candidate
    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[0]
    if top["score"] < s["min_top_score"]:
        return None
    second = scored[1] if len(scored) > 1 else None
    confidence = "high"
    if (
        top["score"] < s["high_confidence_floor"]
        or (second and (top["score"] - second["score"]) <= s["confidence_gap"])
    ):
        confidence = "low"
    reason = f"score={int(top['score'])}, prefer_hits={prefer_hits}"
    return {
        "value": top["value"],
        "score": int(top["score"]),
        "confidence": confidence,
        "reason": reason,
    }


def pick_select_gate_actions(
    groups: list[dict[str, Any]] | None,
    sender: dict[str, Any] | None = None,
    *,
    aggressive: bool = False,
) -> list[dict[str, str]]:
    """Choose select options likely required to unblock submit.

    Handles required selects plus any select whose label we know how to answer
    (お問い合わせ種別, 都道府県, 従業員数, きっかけ, 予算, 連絡方法 …). Label-aware
    choice first, then the B2B-preference picker.

    ``aggressive=True`` is for validation recovery: if a required/unknown
    dropdown still has no semantic answer, pick the first enabled non-placeholder
    option rather than looping forever on the same validation bounce.
    """
    from _outreach_core import form_validation as fv  # lazy: avoid import cycle

    if not isinstance(groups, list):
        return []
    actions: list[dict[str, str]] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        name = str(g.get("name") or g.get("id") or "").strip()
        if not name:
            continue
        if bool(g.get("disabled")):
            continue
        if bool(g.get("selected")):
            continue
        label = str(g.get("label") or "")
        required = bool(g.get("required"))
        if (
            not required
            and not _INQUIRY_TYPE_RE.search(label)
            and not fv.KNOWN_CHOICE_LABEL_RE.search(label)
        ):
            continue
        labelled = fv.choose_option_for_label(label, g.get("options") or [], sender)
        choice = str((labelled or {}).get("value") or "").strip()
        if not choice:
            picked = choose_b2b_option(g.get("options") or [])
            choice = str((picked or {}).get("value") or "").strip()
        if not choice:
            # A validation bounce proves the field is required, but does not
            # authorize us to invent a destination. Picking the first hotel or
            # store would silently route the inquiry to the wrong property.
            destination_sensitive = bool(
                _SENSITIVE_DESTINATION_RE.search(f"{name} {label}".strip())
            )
            choice = _pick_neutral_or_first_option(
                g.get("options") or [],
                allow_first=bool(aggressive and required and not destination_sensitive),
            ) or ""
        if not choice:
            continue
        actions.append({"name": name, "value": choice})
    return actions


def pick_validation_select_actions(
    groups: list[dict[str, Any]] | None,
    sender: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Aggressive select rescue for pages that already emitted validation."""
    return pick_select_gate_actions(groups, sender=sender, aggressive=True)


def _pick_select_option(options: list[dict[str, Any]]) -> str | None:
    picked = choose_b2b_option(options)
    if not picked:
        return _pick_neutral_or_first_option(options, allow_first=False)
    choice = str(picked.get("value") or "").strip()
    return choice or None


def _is_placeholder_option(option: dict[str, Any]) -> bool:
    label = str(option.get("label") or "").strip()
    value = str(option.get("value") or "").strip()
    if not value:
        return True
    if _PLACEHOLDER_RE.match(label):
        return True
    if _PLACEHOLDER_RE.match(value):
        return True
    return False


def _normalize_options(options: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for opt in options:
        if isinstance(opt, str):
            out.append(
                {
                    "label": opt,
                    "value": opt,
                    "selected": False,
                    "disabled": False,
                }
            )
            continue
        if not isinstance(opt, dict):
            continue
        # v31 §WS3a — the enrich extractor now emits compact {t, v} dicts
        # (text + submitted value); older jsonl rows carry plain strings or
        # {label/text/value} shapes, all of which must keep working.
        label = str(
            opt.get("label") or opt.get("text") or opt.get("t")
            or opt.get("value") or opt.get("v") or ""
        ).strip()
        value = str(
            opt.get("value") or opt.get("v") or opt.get("label")
            or opt.get("text") or opt.get("t") or ""
        ).strip()
        out.append(
            {
                "label": label,
                "value": value,
                "selected": bool(opt.get("selected") or opt.get("checked")),
                "disabled": bool(opt.get("disabled")),
            }
        )
    return out


def is_noise_submit_text(text: str | None) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _NOISE_SUBMIT_TEXT_RE.search(t):
        return True
    if t == "こちら":
        return True
    return False


def rank_submit_candidates(
    candidates: list[dict[str, Any]] | None,
    *,
    phase: str = "final",
) -> list[dict[str, Any]]:
    """Rank submit candidates by structure-first heuristics.

    Priority:
      1) is_submit_type && in_form
      2) text hints matching phase
      3) non-noise text links/buttons
    If every candidate is noise (and non-submit), return [].
    """
    if not isinstance(candidates, list):
        return []
    phase_val = (phase or "final").strip().lower()

    def _score(c: dict[str, Any]) -> int:
        txt = str(c.get("text") or "").strip()
        tag = str(c.get("tag") or "").lower()
        href = str(c.get("href") or "").strip()
        in_form = bool(c.get("in_form"))
        is_submit_type = bool(c.get("is_submit_type"))
        s = 0
        if is_submit_type and in_form:
            s += 12
        elif is_submit_type:
            s += 7
        elif in_form:
            s += 3
        if phase_val == "final":
            if _FINAL_STEP_TEXT_RE.search(txt):
                s += 5
            if _FIRST_STEP_TEXT_RE.search(txt) and not _FINAL_STEP_TEXT_RE.search(txt):
                s -= 4
        else:
            if _FIRST_STEP_TEXT_RE.search(txt):
                s += 5
            if _FINAL_STEP_TEXT_RE.search(txt) and not _FIRST_STEP_TEXT_RE.search(txt):
                s -= 3
        if is_noise_submit_text(txt):
            s -= 6
        if tag == "a" and href and href != "#" and not href.lower().startswith("javascript:"):
            s -= 2
        if not txt and not is_submit_type:
            s -= 2
        return s

    norm: list[dict[str, Any]] = [c for c in candidates if isinstance(c, dict)]
    if not norm:
        return []
    ranked = sorted(norm, key=_score, reverse=True)
    # if everything looks like noisy non-submit links, force native fallback.
    if all(
        (not bool(c.get("is_submit_type"))) and is_noise_submit_text(str(c.get("text") or ""))
        for c in ranked
    ):
        return []
    filtered = [c for c in ranked if _score(c) >= 0]
    return (filtered or ranked)[:30]


def pick_route_radio_action(
    groups: list[dict[str, Any]] | None,
    override_value: str | None = None,
) -> dict[str, str] | None:
    """Pick a route-choice radio (personal/company toggle), preferring B2B side."""
    if not isinstance(groups, list):
        return None
    if override_value:
        ov = str(override_value).strip()
        if not ov:
            return None
        for g in groups:
            if not isinstance(g, dict):
                continue
            name = str(g.get("name") or "").strip()
            if not name or bool(g.get("selected")):
                continue
            options = _normalize_options(g.get("options") or [])
            for opt in options:
                if bool(opt.get("selected")) or bool(opt.get("disabled")):
                    continue
                label = str(opt.get("label") or "").strip()
                value = str(opt.get("value") or "").strip()
                if ov in {label, value}:
                    return {"name": name, "value": label or value}
    best: dict[str, str] | None = None
    best_score = -9999
    for g in groups:
        if not isinstance(g, dict):
            continue
        name = str(g.get("name") or "").strip()
        if not name or bool(g.get("selected")):
            continue
        label = str(g.get("label") or "")
        blob = f"{name} {label}"
        options = _normalize_options(g.get("options") or [])
        if len(options) < 2 and not _ROUTE_GROUP_RE.search(blob):
            continue
        picked = choose_b2b_option(options)
        if not picked:
            continue
        score = int(picked.get("score") or 0)
        if _ROUTE_GROUP_RE.search(blob):
            score += 2
        if score > best_score:
            best_score = score
            best = {"name": name, "value": str(picked.get("value") or "")}
    return best


def summarize_remaining_submit_gates(
    checkboxes: list[dict[str, Any]] | None,
    radios: list[dict[str, Any]] | None,
    selects: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Return unresolved gate candidates after auto-fix attempts."""
    boxes = checkboxes if isinstance(checkboxes, list) else []
    radio_groups = radios if isinstance(radios, list) else []
    select_groups = selects if isinstance(selects, list) else []
    unresolved_boxes = []
    for b in boxes:
        if not isinstance(b, dict):
            continue
        if bool(b.get("checked")):
            continue
        if should_auto_check_checkbox(b):
            unresolved_boxes.append(str(b.get("label") or b.get("name") or b.get("id") or "?"))
    unresolved_radios = []
    for g in radio_groups:
        if not isinstance(g, dict):
            continue
        if bool(g.get("selected")):
            continue
        picked = _pick_radio_option(g.get("options") or [])
        if picked:
            unresolved_radios.append(f"{g.get('name') or '?'}={picked}")
    unresolved_selects = []
    for g in select_groups:
        if not isinstance(g, dict):
            continue
        if bool(g.get("selected")):
            continue
        picked = _pick_select_option(g.get("options") or [])
        if picked:
            unresolved_selects.append(f"{g.get('name') or g.get('id') or '?'}={picked}")
    return {
        "checkboxes": unresolved_boxes[:12],
        "radios": unresolved_radios[:12],
        "selects": unresolved_selects[:12],
        "total": len(unresolved_boxes) + len(unresolved_radios) + len(unresolved_selects),
    }
