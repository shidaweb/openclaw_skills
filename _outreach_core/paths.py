"""Per-brief data and targets path resolution (v4 §14)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from _outreach_core.config import resolve_brief_id


@dataclass(frozen=True)
class SkillPaths:
    skill_dir: Path
    brief_id: str
    data_dir: Path
    targets_path: Path


def brief_data_dir(skill_dir: Path, brief_id: str) -> Path:
    return skill_dir / "data" / "briefs" / brief_id


def brief_targets_path(skill_dir: Path, brief_id: str, *, channel: str) -> Path:
    targets_dir = skill_dir / "targets"
    if channel == "linkedin":
        csv_path = targets_dir / f"{brief_id}.csv"
        if csv_path.exists():
            return csv_path
        legacy = skill_dir / "targets.csv"
        return legacy if legacy.exists() else csv_path
    yaml_path = targets_dir / f"{brief_id}.yaml"
    if yaml_path.exists():
        return yaml_path
    legacy = skill_dir / "targets.yaml"
    return legacy if legacy.exists() else yaml_path


def resolve_skill_paths(
    skill_dir: Path,
    brief_id: str | None,
    *,
    channel: str = "jp_form",
) -> SkillPaths:
    bid = resolve_brief_id(brief_id)
    data = brief_data_dir(skill_dir, bid)
    data.mkdir(parents=True, exist_ok=True)
    return SkillPaths(
        skill_dir=skill_dir,
        brief_id=bid,
        data_dir=data,
        targets_path=brief_targets_path(skill_dir, bid, channel=channel),
    )
