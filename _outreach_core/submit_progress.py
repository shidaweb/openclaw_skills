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
_RADIO_OPTION_POSITIVE_RE = re.compile(
    r"(法人|企業|営業|商品提案|ご提案|協業|提携|取材|その他|biz|business)",
    re.I,
)
_RADIO_OPTION_NEGATIVE_RE = re.compile(
    r"(個人|採用|応募|ir|投資家|予約|サポート|修理|返品|faq|会員|ログイン)",
    re.I,
)


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
    best: tuple[int, str] | None = None
    for o in options:
        if not isinstance(o, dict):
            continue
        if bool(o.get("checked")):
            continue
        label = str(o.get("label") or "")
        value = str(o.get("value") or "")
        text = f"{label} {value}".strip()
        if not text:
            continue
        score = 0
        if _RADIO_OPTION_POSITIVE_RE.search(text):
            score += 2
        if _RADIO_OPTION_NEGATIVE_RE.search(text):
            score -= 4
        # "その他" is often safe when no explicit business option exists.
        if "その他" in text:
            score += 1
        if best is None or score > best[0]:
            best = (score, label or value)
    if not best:
        return None
    # avoid forcing clearly negative options
    if best[0] < 0:
        return None
    return best[1]
