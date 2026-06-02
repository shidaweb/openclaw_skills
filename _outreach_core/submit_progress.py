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


def is_agreement_label(label: str | None) -> bool:
    text = (label or "").strip()
    if not text:
        return False
    return bool(_AGREEMENT_RE.search(text))


def should_auto_check_checkbox(box: dict[str, Any] | None) -> bool:
    if not isinstance(box, dict):
        return False
    if bool(box.get("checked")):
        return False
    label = str(box.get("label") or "")
    required = bool(box.get("required"))
    return required or is_agreement_label(label)


def pick_checkboxes_to_check(checkboxes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not isinstance(checkboxes, list):
        return []
    out: list[dict[str, Any]] = []
    for box in checkboxes:
        if should_auto_check_checkbox(box):
            out.append(box)
    return out


def pick_radio_gate_actions(groups: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Choose radio options that are likely required submit gates.

    Input schema (dict keys):
      - name: radio group name
      - label: group/fieldset label
      - required: bool
      - selected: bool
      - options: [{label, value, checked}]
    Output:
      - [{name: group_name, value: option_label_or_value}]
    """
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
        if not required and not _RADIO_GATE_GROUP_RE.search(label):
            continue
        choice = _pick_radio_option(g.get("options") or [])
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


def choose_b2b_option(options: list[Any] | None) -> dict[str, Any] | None:
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
        score = 0
        if strong_prefer:
            score += 6
        elif weak_prefer:
            score += 3
        if strong_avoid:
            score -= 8
        elif weak_avoid:
            score -= 4
        if "その他" in text:
            score += 1
            if sonota_candidate is None:
                sonota_candidate = {
                    "value": label or value,
                    "score": 1,
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
    if top["score"] < 2:
        return None
    second = scored[1] if len(scored) > 1 else None
    confidence = "high"
    if top["score"] < 5 or (second and (top["score"] - second["score"]) <= 1):
        confidence = "low"
    reason = f"score={top['score']}, prefer_hits={prefer_hits}"
    return {
        "value": top["value"],
        "score": int(top["score"]),
        "confidence": confidence,
        "reason": reason,
    }


def pick_select_gate_actions(groups: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Choose select options likely required to unblock submit."""
    if not isinstance(groups, list):
        return []
    actions: list[dict[str, str]] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        name = str(g.get("name") or g.get("id") or "").strip()
        if not name:
            continue
        if bool(g.get("selected")):
            continue
        label = str(g.get("label") or "")
        required = bool(g.get("required"))
        if not required and not _INQUIRY_TYPE_RE.search(label):
            continue
        picked = choose_b2b_option(g.get("options") or [])
        choice = str((picked or {}).get("value") or "").strip()
        if not choice:
            continue
        actions.append({"name": name, "value": choice})
    return actions


def _pick_select_option(options: list[dict[str, Any]]) -> str | None:
    picked = choose_b2b_option(options)
    if not picked:
        return None
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
        label = str(opt.get("label") or opt.get("text") or opt.get("value") or "").strip()
        value = str(opt.get("value") or opt.get("label") or opt.get("text") or "").strip()
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
