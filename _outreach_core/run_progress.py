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
HTML_FILENAME = "run_progress.html"
_REFRESH_SEC = 4  # browser auto-reload cadence while a run is active

_STAGE_LABEL = {
    "campaign": "キャンペーン",
    "list": "リスト作成",
    "fetch-leads": "リスト取得",
    "fetch-from-csv": "CSV取込",
    "enrich": "フォーム調査",
    "draft": "ドラフト作成",
    "send": "送信",
    "resolve": "リゾルバ",
    "research": "調査",
}

# outcome (from _send_one_target) → counter bucket
_OUTCOME_BUCKET = {
    "sent": "sent",
    "done": "skipped",
    "skipped": "skipped",
    "queued": "needs_attention",
    "crashed": "needs_attention",
    "timed_out": "needs_attention",
}


def progress_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / FILENAME


def html_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / HTML_FILENAME


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
        write_html(data_dir)
    except Exception:  # noqa: BLE001
        pass


def transition(
    data_dir: Path | str,
    stage: str,
    total: int,
    *,
    brief: str | None = None,
) -> None:
    """Start a subsequent phase without losing the completed phase summary.

    A campaign's send phase can finish before its resolver begins. Previously
    the progress snapshot said done throughout that resolver window, which made
    healthy runs look stopped.
    """
    try:
        previous = read(data_dir) or {}
        phases = list(previous.get("phases") or [])
        if previous.get("stage"):
            phases.append({
                "stage": previous.get("stage"),
                "total": int(previous.get("total", 0)),
                "processed": int(previous.get("processed", 0)),
                "sent": int(previous.get("sent", 0)),
                "skipped": int(previous.get("skipped", 0)),
                "needs_attention": int(previous.get("needs_attention", 0)),
                "status": previous.get("status"),
                "started_at": previous.get("started_at"),
                "finished_at": previous.get("finished_at"),
            })
        now = _now()
        _atomic_write(progress_path(data_dir), {
            "stage": stage,
            "brief": brief if brief is not None else previous.get("brief"),
            "total": int(total or 0),
            "processed": 0,
            "sent": 0,
            "skipped": 0,
            "needs_attention": 0,
            "current": None,
            "status": "running",
            "started_at": now,
            "run_started_at": previous.get("run_started_at") or previous.get("started_at") or now,
            "updated_at": now,
            "phases": phases[-12:],
        })
        write_html(data_dir)
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
        write_html(data_dir, snap)
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
        write_html(data_dir, snap)
    except Exception:  # noqa: BLE001
        pass


def write_html(data_dir: Path | str, snap: dict[str, Any] | None = None) -> None:
    """(Re)render the self-contained live dashboard next to the JSON."""
    try:
        if snap is None:
            snap = read(data_dir)
        html = render_html(snap)
        hp = html_path(data_dir)
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(html, encoding="utf-8")
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
    stage_label = _STAGE_LABEL.get(str(stage), str(stage))
    total = int(snap.get("total", 0))
    processed = int(snap.get("processed", 0))
    sent = int(snap.get("sent", 0))
    skipped = int(snap.get("skipped", 0))
    na = int(snap.get("needs_attention", 0))
    status = snap.get("status", "?")
    parts = [
        f"{stage_label} {processed}/{total}",
        f"送信OK {sent}",
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


# --- live HTML dashboard (pure render) ---------------------------------------
def _esc(s: Any) -> str:
    return (
        str(s if s is not None else "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_html(snap: dict[str, Any] | None, *, now: datetime | None = None) -> str:
    """Self-contained, auto-refreshing dashboard. No server / no fetch / no JS
    data load — values are embedded inline so it works straight from file://.
    Auto-reloads every few seconds ONLY while status == running."""
    if not snap:
        return (
            "<!doctype html><meta charset='utf-8'>"
            "<title>Doorman 進捗</title>"
            "<body style='font-family:sans-serif;padding:2rem;color:#444'>"
            "<h2>Doorman 進捗</h2><p>まだ実行データがありません"
            "（run_progress.json 未生成）。</p></body>"
        )
    status = str(snap.get("status", "?"))
    total = int(snap.get("total", 0))
    processed = int(snap.get("processed", 0))
    sent = int(snap.get("sent", 0))
    skipped = int(snap.get("skipped", 0))
    na = int(snap.get("needs_attention", 0))
    stage = _esc(snap.get("stage", "?"))
    pct = int(round(100 * processed / total)) if total else 0
    running = status == "running"

    started = _parse(snap.get("started_at"))
    end = _parse(snap.get("finished_at")) or (now or datetime.now(timezone.utc))
    elapsed = _fmt_dur((end - started).total_seconds()) if started else "—"
    eta = eta_seconds(snap, now=now) if running else None
    eta_str = _fmt_dur(eta) if eta is not None else "—"
    current = _esc(snap.get("current")) or "—"
    updated = _esc(snap.get("updated_at"))

    refresh = f"<meta http-equiv='refresh' content='{_REFRESH_SEC}'>" if running else ""
    badge_color = {"running": "#2563eb", "done": "#16a34a"}.get(status, "#6b7280")

    # success rate among processed (sent / processed)
    rate = int(round(100 * sent / processed)) if processed else 0

    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">{refresh}
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Doorman 進捗 — {stage}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, "Hiragino Sans", sans-serif; margin: 0;
          background: #0b1020; color: #e5e7eb; }}
  .wrap {{ max-width: 720px; margin: 0 auto; padding: 28px 20px; }}
  .head {{ display:flex; align-items:center; justify-content:space-between; }}
  h1 {{ font-size: 20px; margin: 0; }}
  .badge {{ font-size: 12px; font-weight:700; color:#fff; padding:4px 10px;
            border-radius:999px; background:{badge_color}; letter-spacing:.04em; }}
  .bar {{ height: 22px; background:#1f2937; border-radius:999px; overflow:hidden;
          margin: 18px 0 6px; }}
  .fill {{ height:100%; width:{pct}%; background:linear-gradient(90deg,#2563eb,#22d3ee);
           transition: width .4s ease; }}
  .pct {{ font-variant-numeric: tabular-nums; color:#9ca3af; font-size:13px; }}
  .cards {{ display:grid; grid-template-columns: repeat(3,1fr); gap:12px; margin:20px 0; }}
  .card {{ background:#111827; border:1px solid #1f2937; border-radius:14px; padding:14px; }}
  .card .n {{ font-size:30px; font-weight:800; font-variant-numeric:tabular-nums; }}
  .card .l {{ font-size:12px; color:#9ca3af; margin-top:2px; }}
  .sent .n {{ color:#34d399; }} .skip .n {{ color:#9ca3af; }} .na .n {{ color:#fbbf24; }}
  .meta {{ display:grid; grid-template-columns: auto 1fr; gap:6px 16px; font-size:14px;
           color:#cbd5e1; margin-top:8px; }}
  .meta dt {{ color:#94a3b8; }}
  .foot {{ color:#6b7280; font-size:12px; margin-top:22px; }}
</style></head>
<body><div class="wrap">
  <div class="head">
    <h1>Doorman 進捗 — {stage}</h1>
    <span class="badge">{_esc(status).upper()}</span>
  </div>
  <div class="bar"><div class="fill"></div></div>
  <div class="pct">{processed} / {total} 件処理（{pct}%）・送信成功率 {rate}%</div>
  <div class="cards">
    <div class="card sent"><div class="n">{sent}</div><div class="l">送信</div></div>
    <div class="card skip"><div class="n">{skipped}</div><div class="l">スキップ</div></div>
    <div class="card na"><div class="n">{na}</div><div class="l">要対応</div></div>
  </div>
  <dl class="meta">
    <dt>処理中</dt><dd>{current}</dd>
    <dt>経過</dt><dd>{elapsed}</dd>
    <dt>残り目安</dt><dd>{eta_str}</dd>
    <dt>最終更新</dt><dd>{updated}</dd>
  </dl>
  <div class="foot">{"自動更新中（" + str(_REFRESH_SEC) + "秒ごと）" if running else "実行は終了しました（自動更新停止）"} · Doorman run_progress</div>
</div></body></html>"""


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
