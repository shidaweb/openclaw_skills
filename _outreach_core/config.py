"""Load briefs/<id>.yaml merged with per-skill config.yaml (v4 §14)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

SKILLS_ROOT = Path(__file__).resolve().parent.parent
BRIEFS_DIR = SKILLS_ROOT / "briefs"
ACTIVE_BRIEF_FILE = BRIEFS_DIR / "_active.txt"
BRIEF_TEMPLATE = BRIEFS_DIR / "_template.yaml"


class BriefError(RuntimeError):
    """Invalid or missing brief configuration."""


def _require_yaml() -> None:
    if yaml is None:
        raise RuntimeError("pyyaml required; pip install pyyaml")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def list_brief_ids() -> list[str]:
    if not BRIEFS_DIR.is_dir():
        return []
    ids: list[str] = []
    for p in sorted(BRIEFS_DIR.glob("*.yaml")):
        if p.name.startswith("_") or ".example" in p.name:
            continue
        ids.append(p.stem)
    return ids


def resolve_brief_id(brief_id: str | None) -> str:
    """Resolve brief slug; raises BriefError if missing/invalid."""
    if brief_id and brief_id.strip():
        bid = brief_id.strip()
    elif ACTIVE_BRIEF_FILE.is_file():
        bid = ACTIVE_BRIEF_FILE.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    else:
        raise BriefError(
            "No brief selected. Create briefs/_active.txt or pass --brief <id>.\n"
            "  python3 -m _outreach_core.helpers.brief list"
        )
    if not bid:
        raise BriefError("briefs/_active.txt is empty")
    path = BRIEFS_DIR / f"{bid}.yaml"
    if not path.is_file():
        known = ", ".join(list_brief_ids()) or "(none)"
        raise BriefError(f"Unknown brief {bid!r}. Known briefs: {known}")
    return bid


def load_brief(brief_id: str | None = None) -> dict[str, Any]:
    _require_yaml()
    bid = resolve_brief_id(brief_id)
    data = yaml.safe_load((BRIEFS_DIR / f"{bid}.yaml").read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise BriefError(f"briefs/{bid}.yaml must be a mapping")
    data.setdefault("brief", {})["id"] = bid
    return data


def load_merged_config(skill_dir: Path, brief_id: str | None = None) -> dict[str, Any]:
    """
    Merge: briefs/<id>.yaml < skill_dir/config.yaml (skill wins on overlap).
    """
    _require_yaml()
    bid = resolve_brief_id(brief_id)
    brief_cfg = load_brief(bid)
    skill_cfg_path = skill_dir / "config.yaml"
    if not skill_cfg_path.exists():
        raise FileNotFoundError(skill_cfg_path)
    skill_cfg = yaml.safe_load(skill_cfg_path.read_text(encoding="utf-8")) or {}
    merged = _deep_merge(skill_cfg, brief_cfg)
    merged["_brief_id"] = bid
    return merged


def load_runtime_config(brief_id: str | None = None) -> dict[str, Any]:
    """Brief-only config (slack, heartbeat, browser) for notify/progress without skill dir."""
    return load_brief(brief_id)


def heartbeat_interval_sec(merged_config: dict[str, Any] | None = None) -> int:
    cfg = merged_config or load_runtime_config()
    hb = cfg.get("heartbeat") or {}
    try:
        return int(hb.get("interval_sec", 300))
    except (TypeError, ValueError):
        return 300


