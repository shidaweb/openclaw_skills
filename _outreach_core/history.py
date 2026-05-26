"""Append-only sent/skip history primitives (per-skill and cross-skill)."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

SKILLS_ROOT = Path(__file__).resolve().parent.parent


def skill_dirs() -> list[Path]:
    return [
        SKILLS_ROOT / "linkedin-outreach",
        SKILLS_ROOT / "jp-form-outreach",
    ]

_ID_FIELDS = ("id", "canonical_id")


def canonical_company_id(company_name: str) -> str:
    """Normalize a company display name to a stable cross-channel slug."""
    if not company_name:
        return ""
    s = unicodedata.normalize("NFKC", company_name.strip())
    s = s.lower()
    s = re.sub(r"[\s　]+", "_", s)
    s = re.sub(r"[^\w\u3040-\u30ff\u4e00-\u9fff\-]+", "", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:120]


def _load_id_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    for line in path.open():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            for key in _ID_FIELDS:
                val = entry.get(key)
                if val:
                    ids.add(str(val))
        except Exception:
            continue
    return ids


def skip_history_path(data_dir: Path) -> Path:
    return data_dir / "skip_history.jsonl"


def sent_history_path(data_dir: Path) -> Path:
    return data_dir / "sent_history.jsonl"


def load_skip_set(data_dir: Path) -> set[str]:
    return _load_id_set(skip_history_path(data_dir))


def load_sent_set(data_dir: Path) -> set[str]:
    return _load_id_set(sent_history_path(data_dir))


def load_global_exclude_set(brief_id: str | None = None) -> set[str]:
    """Exclude sent/skip ids for one brief only (§14-H: briefs do not share history)."""
    from _outreach_core.config import resolve_brief_id

    bid = resolve_brief_id(brief_id)
    s: set[str] = set()
    for d in skill_dirs():
        data = d / "data" / "briefs" / bid
        if data.is_dir():
            s |= load_sent_set(data)
            s |= load_skip_set(data)
        # Legacy flat data/ (pre-migration): only include if no brief subdir yet
        legacy = d / "data"
        brief_root = d / "data" / "briefs"
        if legacy.is_dir() and not brief_root.is_dir():
            s |= load_sent_set(legacy)
            s |= load_skip_set(legacy)
    return s


def _canonical_for_draft(d: dict[str, Any]) -> str | None:
    name = d.get("company") or d.get("name") or ""
    cid = canonical_company_id(str(name))
    return cid or None


def append_skip_history(
    skipped_drafts: list[dict[str, Any]],
    data_dir: Path,
    *,
    extra_fields: tuple[str, ...] = ("name", "company", "title", "industry"),
) -> None:
    if not skipped_drafts:
        return
    path = skip_history_path(data_dir)
    now = datetime.utcnow().isoformat() + "Z"
    with path.open("a") as f:
        for d in skipped_drafts:
            reason_full = (d.get("draft") or {}).get("body") or ""
            reason = reason_full.replace("INSUFFICIENT_DATA: ", "")[:400]
            entry: dict[str, Any] = {
                "id": d["id"],
                "skipped_at": now,
                "reason": reason,
            }
            cid = _canonical_for_draft(d)
            if cid:
                entry["canonical_id"] = cid
            for key in extra_fields:
                if d.get(key) is not None:
                    entry[key] = d.get(key)
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[skip-history] appended {len(skipped_drafts)} entries -> {path.name}")


def append_sent_history(
    sent_drafts: list[dict[str, Any]],
    data_dir: Path,
    *,
    extra_fields: tuple[str, ...] = (
        "name",
        "company",
        "title",
        "industry",
        "form_url",
    ),
) -> None:
    if not sent_drafts:
        return
    path = sent_history_path(data_dir)
    now = datetime.utcnow().isoformat() + "Z"
    with path.open("a") as f:
        for d in sent_drafts:
            entry: dict[str, Any] = {
                "id": d["id"],
                "subject": (d.get("draft") or {}).get("subject"),
                "sent_at": now,
            }
            cid = _canonical_for_draft(d)
            if cid:
                entry["canonical_id"] = cid
            for key in extra_fields:
                if d.get(key) is not None:
                    entry[key] = d.get(key)
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[sent-history] appended {len(sent_drafts)} entries -> {path.name}")


def is_excluded(lead_id: str, exclude: set[str], draft: dict[str, Any] | None = None) -> bool:
    if lead_id in exclude:
        return True
    if draft:
        cid = _canonical_for_draft(draft)
        if cid and cid in exclude:
            return True
    return False
