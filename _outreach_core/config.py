"""Merge root sender_brief.yaml with per-skill config.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

SKILLS_ROOT = Path(__file__).resolve().parent.parent
SENDER_BRIEF_PATH = SKILLS_ROOT / "sender_brief.yaml"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_sender_brief() -> dict[str, Any]:
    """Load sender_brief.yaml only (empty dict if missing)."""
    if yaml is None or not SENDER_BRIEF_PATH.exists():
        return {}
    return yaml.safe_load(SENDER_BRIEF_PATH.read_text()) or {}


def load_merged_config(skill_dir: Path) -> dict[str, Any]:
    """
    Load skill config.yaml, optionally merged with sender_brief.yaml.
    Skill values override sender_brief.
    """
    if yaml is None:
        raise RuntimeError("pyyaml required for load_merged_config")
    skill_cfg_path = skill_dir / "config.yaml"
    if not skill_cfg_path.exists():
        raise FileNotFoundError(skill_cfg_path)
    skill_cfg = yaml.safe_load(skill_cfg_path.read_text()) or {}
    brief = load_sender_brief()
    if brief:
        return _deep_merge(brief, skill_cfg)
    return skill_cfg


def heartbeat_interval_sec(merged_config: dict[str, Any] | None = None) -> int:
    cfg = merged_config or load_sender_brief()
    hb = cfg.get("heartbeat") or {}
    try:
        return int(hb.get("interval_sec", 300))
    except (TypeError, ValueError):
        return 300
