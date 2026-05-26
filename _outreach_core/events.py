"""Structured event log + per-target trace dumps (v4 §13)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

EVENTS_VERSION = 1

SENDER_PII_KEYS = frozenset(
    {
        "email",
        "phone",
        "phone_hyphenated",
        "postal_code",
        "postal_code_no_hyphen",
        "name",
        "name_kana",
        "name_furigana",
        "full_address",
        "address_line",
        "building",
        "calendar_url",
    }
)


@dataclass
class EventContext:
    skill: str = ""
    data_dir: Path | None = None
    run_id: str | None = None


_ctx = EventContext()


def configure(*, skill: str, data_dir: Path, run_id: str | None = None) -> str:
    """Set active skill data dir and run_id; returns resolved run_id."""
    _ctx.skill = skill
    _ctx.data_dir = data_dir
    _ctx.run_id = run_id or datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return _ctx.run_id


def get_context() -> EventContext:
    return _ctx


def get_run_id() -> str:
    if _ctx.run_id:
        return _ctx.run_id
    return f"ad-hoc-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"


def events_path() -> Path | None:
    if not _ctx.data_dir:
        return None
    return _ctx.data_dir / "events.jsonl"


def trace_dir_for(target_id: str, *, run_id: str | None = None) -> Path | None:
    if not _ctx.data_dir:
        return None
    rid = run_id or get_run_id()
    d = _ctx.data_dir / "traces" / rid / str(target_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def redact_sender(obj: Any, sender: dict[str, Any] | None = None) -> Any:
    """Replace sender PII values with placeholders (recursive)."""
    if sender is None:
        return obj
    pii_values = {str(v) for k, v in sender.items() if k in SENDER_PII_KEYS and v}

    def _walk(x: Any) -> Any:
        if isinstance(x, dict):
            out: dict[str, Any] = {}
            for k, v in x.items():
                if k in SENDER_PII_KEYS:
                    out[k] = f"<sender.{k}>"
                else:
                    out[k] = _walk(v)
            return out
        if isinstance(x, list):
            return [_walk(i) for i in x]
        if isinstance(x, str):
            s = x
            for val in sorted(pii_values, key=len, reverse=True):
                if val and val in s:
                    s = s.replace(val, "<redacted>")
            return s
        return x

    return _walk(obj)


def dump_trace(
    trace_dir: Path | None,
    name: str,
    obj: Any,
    *,
    sender: dict[str, Any] | None = None,
) -> None:
    """Write JSON or text under trace_dir; never raises."""
    if not trace_dir:
        return
    try:
        path = trace_dir / name
        if name.endswith(".txt"):
            text = obj if isinstance(obj, str) else str(obj)
            if sender:
                for val in sender.values():
                    if isinstance(val, str) and val and val in text:
                        text = text.replace(val, "<redacted>")
            path.write_text(text[:500_000], encoding="utf-8")
        else:
            payload = redact_sender(obj, sender) if sender else obj
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except OSError as exc:
        _log.debug("dump_trace failed %s: %s", name, exc)


def emit(
    kind: str,
    *,
    stage: str,
    target_id: str | None = None,
    outcome: str | None = None,
    payload: dict[str, Any] | None = None,
    trace_dir: Path | str | None = None,
) -> None:
    """Append one event to data/events.jsonl; never raises."""
    path = events_path()
    if not path:
        return
    td = None
    if trace_dir is not None:
        td = str(trace_dir)
    row: dict[str, Any] = {
        "v": EVENTS_VERSION,
        "ts": datetime.utcnow().isoformat() + "Z",
        "run_id": get_run_id(),
        "skill": _ctx.skill or "unknown",
        "kind": kind,
        "stage": stage,
    }
    if target_id:
        row["target_id"] = target_id
    if outcome:
        row["outcome"] = outcome
    if payload:
        row["payload"] = payload
    if td:
        row["trace_dir"] = td
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        _log.debug("events.emit failed: %s", exc)


def parse_since(since: str) -> datetime:
    """Parse --since like 7d, 24h, 30m."""
    since = (since or "7d").strip().lower()
    m = re.match(r"^(\d+)(d|h|m)$", since)
    if not m:
        return datetime.utcnow() - timedelta(days=7)
    n, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        return datetime.utcnow() - timedelta(days=n)
    if unit == "h":
        return datetime.utcnow() - timedelta(hours=n)
    return datetime.utcnow() - timedelta(minutes=n)


def load_events(
    data_dir: Path,
    *,
    since: datetime | None = None,
    skill: str | None = None,
) -> list[dict[str, Any]]:
    path = data_dir / "events.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if skill and row.get("skill") != skill:
            continue
        if since:
            try:
                ts = datetime.fromisoformat(str(row.get("ts", "")).replace("Z", "+00:00"))
                if ts.replace(tzinfo=None) < since.replace(tzinfo=None):
                    continue
            except ValueError:
                pass
        rows.append(row)
    return rows


def parse_run_id_ts(run_id: str) -> datetime | None:
    try:
        return datetime.strptime(run_id, "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def prune_data(data_dir: Path, *, keep_days: int = 90, dry_run: bool = False) -> dict[str, int]:
    """
    Remove events older than keep_days and trace dirs for pruned run_ids.
    Returns counts: events_kept, events_removed, trace_dirs_removed.
    """
    cutoff = datetime.utcnow() - timedelta(days=keep_days)
    events_file = data_dir / "events.jsonl"
    kept: list[str] = []
    removed = 0
    pruned_run_ids: set[str] = set()

    if events_file.is_file():
        for line in events_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            ts_raw = str(row.get("ts", ""))
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                ts = cutoff
            if ts >= cutoff.replace(tzinfo=None):
                kept.append(line)
            else:
                removed += 1
                rid = row.get("run_id")
                if rid:
                    pruned_run_ids.add(str(rid))
        if not dry_run and removed:
            events_file.write_text(
                "\n".join(kept) + ("\n" if kept else ""),
                encoding="utf-8",
            )

    traces_root = data_dir / "traces"
    trace_dirs_removed = 0
    if traces_root.is_dir():
        for run_dir in list(traces_root.iterdir()):
            if not run_dir.is_dir():
                continue
            rid = run_dir.name
            ts = parse_run_id_ts(rid)
            remove = rid in pruned_run_ids or (ts is not None and ts < cutoff)
            if remove:
                trace_dirs_removed += 1
                if not dry_run:
                    import shutil

                    shutil.rmtree(run_dir, ignore_errors=True)

    return {
        "events_kept": len(kept),
        "events_removed": removed,
        "trace_dirs_removed": trace_dirs_removed,
    }


def guess_opener_type(body: str) -> int | None:
    """Heuristic opener type 1-4 for draft.emitted payload."""
    if not body:
        return None
    first = body.splitlines()[0] if body else body[:200]
    if "突然" in first or "恐れ入り" in first:
        return 1
    if "？" in first[:80] or "でしょうか" in first[:80]:
        return 3
    if "として" in first[:60] or "経験" in first[:40]:
        return 4
    if len(first) > 20 and not first.startswith("お世話"):
        return 2
    return 1


def guess_self_intro_variant(body: str) -> str | None:
    """Heuristic V1–V5 tag for draft.emitted (§11-B-4 / §13-C)."""
    if not body:
        return None
    if "MDOnline" in body and ("医療" in body or "患者" in body or "家族" in body):
        return "V1"
    if "ソニー" in body or "こども" in body or "サブスク" in body:
        return "V2"
    if "FC" in body or "本部" in body or "中堅" in body:
        return "V3"
    if "小さな組織" in body or "失礼" in body[:120]:
        return "V4"
    if "私自身も" in body or "運営側" in body:
        return "V5"
    return None
