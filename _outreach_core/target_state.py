"""Per-target runtime state snapshot — last-known wizard position.

v30 §WS-E. The existing :mod:`_outreach_core.send_journal` is append-only and
focuses on the safety-critical lifecycle markers (target_started /
submit_attempted / verified) used by :func:`should_skip_resume` to prevent
double-sends after a crash.

This module is the complementary observability layer: a **last-write-wins**
snapshot of the wizard's progress for one in-flight target. The snapshot
answers questions like "what wizard hop were we on?", "which button was
clicked last?", "was the wizard stuck?". It is meant to enrich the
``needs_attention`` Slack message and ``./report`` diagnostics — not to drive
resume decisions (send_journal is the truth there).

Path layout::

    data/briefs/<brief_id>/runtime/<target_id>/last_state.json

The directory is intentionally separate from the brief's top-level data files
(leads.jsonl etc.) so cleanup tools can clear runtime state safely without
risking the canonical history. Each per-target file is at most ~1 KiB.

The module is pure I/O + dataclass — no network, no browser, no LLM. It can
be unit-tested in a tmp_path fixture without any mocking.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUNTIME_DIRNAME = "runtime"
STATE_FILENAME = "last_state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def runtime_dir(data_dir: Path | str, target_id: str) -> Path:
    """Resolve the per-target runtime directory. Does NOT create it."""
    return Path(data_dir) / RUNTIME_DIRNAME / str(target_id)


def state_path(data_dir: Path | str, target_id: str) -> Path:
    return runtime_dir(data_dir, target_id) / STATE_FILENAME


@dataclass
class TargetState:
    """Last-known state for one in-flight target. Fields are intentionally
    loose strings so future phases / wizard signals can extend the schema
    without bumping a version number.
    """

    target_id: str
    run_id: str = ""
    name: str = ""
    form_url: str = ""
    phase: str = ""            # "send.opened" | "send.filled" | "send.submit_attempted" | ...
    hop: int = 0
    observation_state: str = ""  # "input" | "validation_error" | "confirm" | "done" | "no_form"
    last_button: str = ""
    same_button_count: int = 0
    wizard_stuck: str = ""     # reason code from wizard.StuckReason, if any
    captcha: str = ""          # "none" | "v3" | "v2_checkbox" | ...
    started_at: str = ""
    updated_at: str = ""
    # Arbitrary extra hints (e.g. iframe takeover src, recovery URL).
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TargetState":
        d = dict(data or {})
        extras = d.pop("extras", None) or {}
        if not isinstance(extras, dict):
            extras = {}
        return cls(
            target_id=str(d.get("target_id") or ""),
            run_id=str(d.get("run_id") or ""),
            name=str(d.get("name") or ""),
            form_url=str(d.get("form_url") or ""),
            phase=str(d.get("phase") or ""),
            hop=int(d.get("hop") or 0),
            observation_state=str(d.get("observation_state") or ""),
            last_button=str(d.get("last_button") or ""),
            same_button_count=int(d.get("same_button_count") or 0),
            wizard_stuck=str(d.get("wizard_stuck") or ""),
            captcha=str(d.get("captcha") or ""),
            started_at=str(d.get("started_at") or ""),
            updated_at=str(d.get("updated_at") or ""),
            extras=extras,
        )


def write_state(data_dir: Path | str, state: TargetState) -> Path:
    """Atomically write the state snapshot. Never raises (an I/O failure must
    not block the send loop). Returns the resolved path."""
    if not state.target_id:
        return Path(data_dir) / RUNTIME_DIRNAME
    state.updated_at = _now_iso()
    if not state.started_at:
        state.started_at = state.updated_at
    path = state_path(data_dir, state.target_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        pass
    return path


def read_state(data_dir: Path | str, target_id: str) -> TargetState | None:
    """Read a target's last-known state. Returns ``None`` when no snapshot
    exists or the file is unreadable / malformed (callers treat that as
    "no prior state")."""
    path = state_path(data_dir, target_id)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return TargetState.from_dict(data)


def clear_state(data_dir: Path | str, target_id: str) -> bool:
    """Remove the snapshot file once a target reaches a terminal verdict.
    Returns True on success, False if the file did not exist or removal
    failed (callers ignore the return — cleanup is best-effort)."""
    path = state_path(data_dir, target_id)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def list_runtime_states(data_dir: Path | str) -> list[TargetState]:
    """Enumerate every persisted target state under the brief. Used by the
    run-start banner to surface "still in runtime" targets without requiring
    the operator to grep the directory tree."""
    root = Path(data_dir) / RUNTIME_DIRNAME
    out: list[TargetState] = []
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        snap = entry / STATE_FILENAME
        if not snap.exists():
            continue
        try:
            data = json.loads(snap.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append(TargetState.from_dict(data))
    return out


def merge_update(
    data_dir: Path | str,
    target_id: str,
    *,
    run_id: str | None = None,
    name: str | None = None,
    form_url: str | None = None,
    phase: str | None = None,
    hop: int | None = None,
    observation_state: str | None = None,
    last_button: str | None = None,
    same_button_count: int | None = None,
    wizard_stuck: str | None = None,
    captcha: str | None = None,
    extras: dict[str, Any] | None = None,
) -> TargetState:
    """Convenience helper: read-modify-write. Callers pass only the fields
    that changed; everything else is preserved. Returns the merged state.

    This is the API the send loop uses most: at each checkpoint we know one
    or two fields are new (phase moved, button just clicked) and don't want
    to recompute the whole snapshot.
    """
    current = read_state(data_dir, target_id) or TargetState(target_id=target_id)
    current.target_id = target_id  # always pin (defensive vs file corruption)
    if run_id is not None:
        current.run_id = run_id
    if name is not None:
        current.name = name
    if form_url is not None:
        current.form_url = form_url
    if phase is not None:
        current.phase = phase
    if hop is not None:
        current.hop = int(hop)
    if observation_state is not None:
        current.observation_state = observation_state
    if last_button is not None:
        current.last_button = last_button
    if same_button_count is not None:
        current.same_button_count = int(same_button_count)
    if wizard_stuck is not None:
        current.wizard_stuck = wizard_stuck
    if captcha is not None:
        current.captcha = captcha
    if extras:
        merged_extras = dict(current.extras or {})
        merged_extras.update(extras)
        current.extras = merged_extras
    write_state(data_dir, current)
    return current
