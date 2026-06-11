"""Batch backend-parity validation (v21 Phase 2).

Runs the per-URL parity probe across a LIST of URLs (default: the ones that
actually got stuck — from the brief's resolve_queue) through both browser
backends, then prints a table + an overall match/divergence summary. One command
for the operator; paste the output back for any divergence tuning.

Usage (venv python, from the repo root):

    PYTHONPATH=. jp-form-outreach/.venv/bin/python \
      -m _outreach_core.tools.backend_parity_batch --brief torana-line-crm

    # only check Playwright is alive (no gateway needed):
    ... backend_parity_batch --brief torana-line-crm --backends playwright

Exit code 0 when every URL matches (or single-backend run), 1 on any divergence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from _outreach_core import adapters
from _outreach_core.tools.backend_probe import compare_probes, run_probe


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    out.append(row)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


def collect_urls(data_dir: Path | str, *, limit: int = 20,
                 extra: list[str] | None = None) -> list[str]:
    """Stuck URLs from resolve_queue.jsonl (form_url → diagnostics.url), deduped,
    plus any explicit extras. Preserves first-seen order."""
    data_dir = Path(data_dir)
    seen: set[str] = set()
    urls: list[str] = []

    def _add(u: Any) -> None:
        u = (str(u or "")).strip()
        if u and u.startswith("http") and u not in seen:
            seen.add(u)
            urls.append(u)

    for u in (extra or []):
        _add(u)
    for row in _read_jsonl(data_dir / "resolve_queue.jsonl"):
        _add(row.get("form_url") or (row.get("diagnostics") or {}).get("url"))
    return urls[:limit] if limit and limit > 0 else urls


def run_batch(urls: list[str], backends: list[str]) -> dict[str, Any]:
    """Probe every URL through each backend (one adapter per backend, reused)."""
    built: dict[str, Any] = {}
    for b in backends:
        try:
            built[b] = adapters.make_browser(b)
        except Exception as exc:  # noqa: BLE001
            built[b] = None
            print(f"[warn] backend {b} unavailable: {str(exc)[:160]}")
    rows: list[dict[str, Any]] = []
    try:
        for url in urls:
            by_backend: dict[str, Any] = {}
            for b in backends:
                ad = built.get(b)
                by_backend[b] = (run_probe(ad, url) if ad is not None
                                 else {"backend": b, "ok": False, "error": "adapter_unavailable"})
            cmp = (compare_probes(by_backend[backends[0]], by_backend[backends[1]])
                   if len(backends) >= 2 else {"match": True, "diffs": {}, "single": True})
            rows.append({"url": url, "by_backend": by_backend, "comparison": cmp})
    finally:
        for ad in built.values():
            if ad is not None:
                try:
                    ad.shutdown()
                except Exception:  # noqa: BLE001
                    pass
    return {"rows": rows, "summary": summarize_batch(rows)}


def summarize_batch(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    match = sum(1 for r in rows if r["comparison"].get("match"))
    diverge = sum(1 for r in rows
                  if not r["comparison"].get("match") and not r["comparison"].get("single"))
    return {"total": total, "match": match, "divergence": diverge,
            "all_match": diverge == 0}


def format_batch(result: dict[str, Any], backends: list[str]) -> str:
    lines = [f"# backend parity batch — {len(result['rows'])} URLs · backends={','.join(backends)}", ""]
    for r in result["rows"]:
        cmp = r["comparison"]
        mark = "✓" if cmp.get("match") or cmp.get("single") else "✗"
        lines.append(f"{mark} {r['url']}")
        for b in backends:
            d = r["by_backend"].get(b, {})
            if d.get("ok"):
                lines.append(
                    f"      [{b}] state={d.get('page_state')} "
                    f"captcha={d.get('captcha_kind')}/{d.get('captcha_blocking')} "
                    f"ta={d.get('textareas')} btn={d.get('submit_buttons')}"
                )
            else:
                lines.append(f"      [{b}] ERROR: {d.get('error')}")
        if cmp.get("diffs"):
            for k, (va, vb) in cmp["diffs"].items():
                lines.append(f"      ↳ {k}: {va!r} vs {vb!r}")
    s = result["summary"]
    lines.append("")
    lines.append(f"== {s['match']}/{s['total']} MATCH · {s['divergence']} DIVERGENCE ==")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from _outreach_core.config import SKILLS_ROOT

    ap = argparse.ArgumentParser(description="Batch browser-backend parity check.")
    ap.add_argument("--brief", default=None)
    ap.add_argument("--skill", default="jp-form-outreach")
    ap.add_argument("--backends", default="openclaw,playwright")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--urls", default=None, help="optional file: one URL per line")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    brief = args.brief
    if not brief:
        active = SKILLS_ROOT / "briefs" / "_active.txt"
        if active.is_file():
            brief = active.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    data_dir = SKILLS_ROOT / args.skill / "data" / "briefs" / (brief or "")

    extra: list[str] = []
    if args.urls:
        try:
            extra = [l.strip() for l in Path(args.urls).read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.startswith("#")]
        except OSError:
            pass

    urls = collect_urls(data_dir, limit=args.limit, extra=extra)
    if not urls:
        print("No URLs found (resolve_queue.jsonl empty and no --urls). Nothing to check.")
        return 0

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    result = run_batch(urls, backends)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_batch(result, backends))
    return 0 if result["summary"]["all_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
