"""Live run-progress snapshot (v22).

`HeartbeatSession` already pushes per-lead ticks to current_task.jsonl + Slack
(liveness). What was missing is a single, instantly-readable CURRENT-STATE file
so a viewer (the Slack agent, ./report progress, a Cowork artifact) can answer
"where are we right now" without replaying the event stream.

This module owns ``data/<...>/run_progress.json`` — one small JSON object,
atomically overwritten on each update. It is best-effort: every function must
swallow its own errors so progress bookkeeping can never break a send batch.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FILENAME = "run_progress.json"

# outcome (from _send_one_target) → counter bucket
_OUTCOME_BUCKET = {
    "sent": "sent",
    "done": "skipped",
    "skipped": "skipped",
    "queued": "needs_attention",
    "crashed": "needs_attention",
}


def progress_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / FILENAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".rp_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass


def read(data_dir: Path | str) -> dict[str, Any] | None:
    try:
        p = progress_path(data_dir)
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def start(data_dir: Path | str, stage: str, total: int, *, brief: str | None = None) -> None:
    try:
        _atomic_write(progress_path(data_dir), {
            "stage": stage,
            "brief": brief,
            "total": int(total or 0),
            "processed": 0,
            "sent": 0,
            "skipped": 0,
            "needs_attention": 0,
            "current": None,
            "status": "running",
            "started_at": _now(),
            "updated_at": _now(),
        })
    except Exception:  # noqa: BLE001
        pass


def bump(data_dir: Path | str, *, outcome: str | None, name: str | None = None) -> None:
    """Record one finished lead. ``outcome`` is the _send_one_target outcome."""
    try:
        snap = read(data_dir) or {}
        snap["processed"] = int(snap.get("processed", 0)) + 1
        bucket = _OUTCOME_BUCKET.get(str(outcome or "").strip(), "skipped")
        snap[bucket] = int(snap.get(bucket, 0)) + 1
        snap["current"] = name
        snap["last_outcome"] = outcome
        snap["updated_at"] = _now()
        _atomic_write(progress_path(data_dir), snap)
    except Exception:  # noqa: BLE001
        pass


def set_current(data_dir: Path | str, name: str | None) -> None:
    try:
        snap = read(data_dir) or {}
        snap["current"] = name
        snap["updated_at"] = _now()
        _atomic_write(progress_path(data_dir), snap)
    except Exception:  # noqa: BLE001
        pass


def finish(data_dir: Path | str, *, status: str = "done") -> None:
    try:
        snap = read(data_dir) or {}
        snap["status"] = status
        snap["current"] = None
        snap["updated_at"] = _now()
        snap["finished_at"] = _now()
        _atomic_write(progress_path(data_dir), snap)
    except Exception:  # noqa: BLE001
        pass


# --- formatting (pure) -------------------------------------------------------
def _parse(ts: Any) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _fmt_dur(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def eta_seconds(snap: dict[str, Any], *, now: datetime | None = None) -> float | None:
    """Naive ETA: elapsed/processed × remaining. None when not computable."""
    started = _parse(snap.get("started_at"))
    processed = int(snap.get("processed", 0))
    total = int(snap.get("total", 0))
    if not started or processed <= 0 or total <= processed:
        return None
    now = now or datetime.now(timezone.utc)
    elapsed = (now - started).total_seconds()
    per = elapsed / processed
    return per * (total - processed)


def format_summary(snap: dict[str, Any] | None, *, now: datetime | None = None) -> str:
    if not snap:
        return "進捗データなし（run_progress.json が見つかりません）"
    stage = snap.get("stage", "?")
    total = int(snap.get("total", 0))
    processed = int(snap.get("processed", 0))
    sent = int(snap.get("sent", 0))
    skipped = int(snap.get("skipped", 0))
    na = int(snap.get("needs_attention", 0))
    status = snap.get("status", "?")
    parts = [
        f"{stage} {processed}/{total}",
        f"送信 {sent}",
        f"スキップ {skipped}",
        f"要対応 {na}",
    ]
    started = _parse(snap.get("started_at"))
    if started:
        end = _parse(snap.get("finished_at")) or (now or datetime.now(timezone.utc))
        parts.append(f"経過 {_fmt_dur((end - started).total_seconds())}")
    if status == "running":
        eta = eta_seconds(snap, now=now)
        if eta is not None:
            parts.append(f"残り目安 {_fmt_dur(eta)}")
        if snap.get("current"):
            parts.append(f"処理中: {snap['current']}")
    else:
        parts.append(f"状態: {status}")
    return " · ".join(parts)


# --- standalone reader (for the Slack agent / CLI) ---------------------------
def _main(argv: list[str] | None = None) -> int:
    import argparse

    from _outreach_core.config import SKILLS_ROOT

    ap = argparse.ArgumentParser(description="Print live run progress.")
    ap.add_argument("--brief", default=None)
    ap.add_argument("--skill", default="jp-form-outreach")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    brief = args.brief
    if not brief:
        active = SKILLS_ROOT / "briefs" / "_active.txt"
        if active.is_file():
            brief = active.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    data_dir = SKILLS_ROOT / args.skill / "data" / "briefs" / (brief or "")
    snap = read(data_dir)
    if args.json:
        print(json.dumps(snap or {}, ensure_ascii=False, indent=2))
    else:
        print(format_summary(snap))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
