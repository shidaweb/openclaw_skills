"""Rebuild Slack-facing status from file state (v4 §14-G)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from _outreach_core import channel_state
from _outreach_core.active_run import is_lock_alive, read_lock
from _outreach_core.config import resolve_brief_id
from _outreach_core.paths import brief_data_dir

SKILLS_ROOT = channel_state.SKILLS_ROOT
SKILL_NAMES = ("jp-form-outreach", "linkedin-outreach")


def _tail_jsonl(path: Path, n: int = 5) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    out: list[dict[str, Any]] = []
    for ln in lines[-n:]:
        try:
            row = json.loads(ln)
            if isinstance(row, dict):
                out.append(row)
        except json.JSONDecodeError:
            continue
    return out


def _events_since(path: Path, hours: int = 1) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    out: list[dict[str, Any]] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        ts_s = str(ev.get("ts") or "")
        try:
            ts = datetime.fromisoformat(ts_s.replace("Z", "+00:00").replace("+00:00", ""))
        except ValueError:
            out.append(ev)
            continue
        if ts >= cutoff.replace(tzinfo=None):
            out.append(ev)
    return out[-30:]


def _open_needs_attention(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    open_rows: list[dict[str, Any]] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("status") == "open":
            open_rows.append(row)
    return open_rows


def build_status_report(
    brief_id: str | None = None,
    *,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    skill: str | None = None,
) -> str:
    """Markdown summary for OpenClaw agent / Slack."""
    thread_state: dict[str, Any] = {}
    if channel_id and thread_ts:
        thread_state = channel_state.load_thread_state(channel_id, thread_ts) or {}
    bid = resolve_brief_id(brief_id or str(thread_state.get("brief_id") or "") or None)
    lines = [f"# Doorman status — brief `{bid}`", ""]

    if channel_id:
        state = channel_state.load_state(channel_id)
        lines.append(f"## channel `{channel_id}`")
        if state:
            lines.append(f"- default_brief: {state.get('default_brief')}")
            lines.append(f"- channels: {', '.join(state.get('default_channels') or [])}")
            lines.append(f"- last_used_at: {state.get('last_used_at', '')}")
        else:
            lines.append("_No channel-wide default; a thread binding may still be active._")
        lines.append("")

    selected_channel = str(thread_state.get("channel") or "").strip()
    selected_persona = str(thread_state.get("persona_id") or "").strip()
    if thread_ts:
        lines.append(f"## thread `{thread_ts}`")
        if thread_state:
            lines.append(f"- campaign: {bid}")
            lines.append(f"- persona: {selected_persona or '(legacy inline sender)'}")
            lines.append(f"- outreach_channel: {selected_channel or '(not selected)'}")
            lines.append(f"- last_used_at: {thread_state.get('last_used_at', '')}")
        else:
            lines.append("_Thread not bound. Run `outreach bind`._")
        lines.append("")

    if skill is None:
        skill = {
            "jp_form": "jp-form-outreach",
            "linkedin": "linkedin-outreach",
        }.get(selected_channel, "jp-form-outreach")
    skill_dir = SKILLS_ROOT / skill
    data = brief_data_dir(skill_dir, bid)

    lock = read_lock(data)
    lines.append("## active_run.lock")
    if not lock:
        lines.append("_No active run._")
    else:
        alive = is_lock_alive(lock)
        lines.append(f"- run_id: {lock.get('run_id')} · stage: {lock.get('stage')} · pid: {lock.get('pid')}")
        lines.append(f"- progress: {lock.get('current_target_idx', 0)}/{lock.get('total_targets', '?')}")
        lines.append(f"- alive: {alive}")
        if lock.get("slack_thread_ts"):
            lines.append(f"- thread_ts: {lock.get('slack_thread_ts')}")
    lines.append("")

    hb = _tail_jsonl(data / "current_task.jsonl", 3)
    lines.append("## current_task (last 3)")
    if hb:
        for ev in hb:
            lines.append(
                f"- [{ev.get('event')}] {ev.get('task')}: "
                f"{ev.get('current', '?')}/{ev.get('total', '?')} — {ev.get('message', '')[:80]}"
            )
    else:
        lines.append("_Empty._")
    lines.append("")

    events = _events_since(data / "events.jsonl", hours=1)
    lines.append(f"## events (last 1h, {len(events)} rows)")
    for ev in events[-8:]:
        lines.append(f"- {ev.get('ts', '')} · {ev.get('kind', ev.get('event', '?'))}")
    if not events:
        lines.append("_No recent events._")
    lines.append("")

    na = _open_needs_attention(data / "needs_attention.jsonl")
    lines.append(f"## needs_attention (open: {len(na)})")
    for row in na[:10]:
        lines.append(
            f"- {row.get('target_id')} / {row.get('name', '?')}: "
            f"{(row.get('reason') or '')[:60]}"
        )
    if not na:
        lines.append("_None open._")

    return "\n".join(lines)
