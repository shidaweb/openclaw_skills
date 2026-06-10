"""Send journal — double-send prevention + crash resume (v15 §R3).

A crash between the final submit click and verify means we do not know whether
the message went out. On the next run the target must NOT be silently retried
(double-send risk) — it goes to needs_attention for a human decision.

Protocol (append-only ``send_journal.jsonl`` in the brief's data dir):

  - just BEFORE the final submit click:   {"target_id", "ts", "phase": "submit_attempted", "form_url"}
  - after verify settles (any outcome):   {"target_id", "ts", "phase": "verified", "outcome": ...}

``should_skip_resume`` is the pure decision function: a target with a
``submit_attempted`` entry that was never followed by ``verified`` is in an
unknown state and must be skipped on resume.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

PHASE_SUBMIT_ATTEMPTED = "submit_attempted"
PHASE_VERIFIED = "verified"


def journal_path(data_dir: Path) -> Path:
    return Path(data_dir) / "send_journal.jsonl"


def append_journal(
    data_dir: Path,
    target_id: str,
    phase: str,
    **extra: Any,
) -> Path:
    """Append one journal row. Never raises (journal failure must not stop a send)."""
    path = journal_path(data_dir)
    row = {
        "target_id": str(target_id),
        "ts": datetime.utcnow().isoformat() + "Z",
        "phase": phase,
        **extra,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return path


def load_journal(data_dir: Path) -> list[dict[str, Any]]:
    path = journal_path(data_dir)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return rows
    return rows


def unverified_attempt_ids(entries: list[dict[str, Any]]) -> set[str]:
    """Target ids whose LAST journal phase is submit_attempted (no verified after).

    Order matters: a verified row closes all prior attempts for that target; a
    new submit_attempted afterwards re-opens it.
    """
    last_phase: dict[str, str] = {}
    for row in entries or []:
        tid = str(row.get("target_id") or "").strip()
        phase = str(row.get("phase") or "").strip()
        if not tid or phase not in (PHASE_SUBMIT_ATTEMPTED, PHASE_VERIFIED):
            continue
        last_phase[tid] = phase
    return {tid for tid, phase in last_phase.items() if phase == PHASE_SUBMIT_ATTEMPTED}


def should_skip_resume(entries: list[dict[str, Any]], target_id: str) -> bool:
    """True when this target has an unverified prior submit attempt (§R3)."""
    return str(target_id) in unverified_attempt_ids(entries)
