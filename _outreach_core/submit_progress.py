"""Pure helpers for submit-progress gating (v7 §A)."""

from __future__ import annotations

import re
from typing import Any

_AGREEMENT_RE = re.compile(
    r"(同意|同意する|個人情報|プライバシー|プライバシーポリシー|利用規約|取扱いについて|取り扱いについて)",
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
