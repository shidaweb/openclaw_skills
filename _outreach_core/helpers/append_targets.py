#!/usr/bin/env python3
"""Append agent-curated targets to linkedin targets.csv or jp-form targets.yaml."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import history

SKILLS_ROOT = history.SKILLS_ROOT


def _read_input(path: str, fmt: str) -> tuple[str, list[dict[str, Any]]]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text()
    if fmt == "jsonl":
        return raw, [json.loads(line) for line in raw.splitlines() if line.strip()]
    data = json.loads(raw)
    if isinstance(data, list):
        return raw, data
    if isinstance(data, dict) and "companies" in data:
        return raw, data["companies"]
    if isinstance(data, dict) and "targets" in data:
        return raw, data["targets"]
    raise ValueError("expected JSON array or {companies: [...]}")


def _existing_jp_ids(yaml_path: Path) -> tuple[set[str], set[str]]:
    try:
        import yaml
    except ImportError:
        raise RuntimeError("pyyaml required") from None
    if not yaml_path.exists():
        return set(), set()
    raw = yaml.safe_load(yaml_path.read_text()) or {}
    companies = raw.get("companies") or []
    ids: set[str] = set()
    loose: set[str] = set()
    for c in companies:
        if isinstance(c, dict):
            if c.get("id"):
                sid = str(c["id"])
                ids.add(sid)
                lk = history.canonical_company_key(sid)
                if lk:
                    loose.add(lk)
            if c.get("name"):
                name = str(c["name"])
                ids.add(history.canonical_company_id(name))
                lk = history.canonical_company_key(name)
                if lk:
                    loose.add(lk)
    return ids, loose


def append_jp_form(
    companies: list[dict[str, Any]],
    yaml_path: Path,
    *,
    exclude_ids: set[str] | None = None,
) -> int:
    import yaml

    raw = yaml.safe_load(yaml_path.read_text()) if yaml_path.exists() else {}
    if raw is None:
        raw = {}
    existing, existing_loose = _existing_jp_ids(yaml_path)
    excluded = exclude_ids or set()
    excluded_loose = {
        history.canonical_company_key(x) for x in excluded if str(x or "").strip()
    }
    bucket = raw.get("companies") or []
    added = 0
    skipped_dup = 0
    skipped_excluded = 0
    for c in companies:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        cid = c.get("id") or history.canonical_company_id(str(c["name"]))
        canon = history.canonical_company_id(str(c["name"]))
        loose = history.canonical_company_key(str(c.get("id") or c["name"]))
        if cid in excluded or canon in excluded or (loose and loose in excluded_loose):
            skipped_excluded += 1
            continue
        if cid in existing or canon in existing or (loose and loose in existing_loose):
            skipped_dup += 1
            continue
        row = {
            "id": cid,
            "name": c["name"],
            "industry": c.get("industry", ""),
            "founded": c.get("founded", ""),
            "status": c.get("status", "pending"),
            "category": c.get("category", "b2b_form"),
            "form_url": c.get("form_url", ""),
            "hook_context": c.get("hook_context") or c.get("hook_seed", ""),
            "notes": c.get("notes") or c.get("why_fit", ""),
        }
        if isinstance(c.get("contact_url_candidates"), list):
            row["contact_url_candidates"] = [str(x) for x in c.get("contact_url_candidates") if str(x or "").strip()]
        if c.get("field_map_overrides"):
            row["field_map_overrides"] = c["field_map_overrides"]
        bucket.append(row)
        existing.add(str(cid))
        existing.add(canon)
        if loose:
            existing_loose.add(loose)
        added += 1
    raw["companies"] = bucket
    yaml_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    if skipped_dup or skipped_excluded:
        print(
            f"[append_targets] jp_form dedup: skipped duplicate={skipped_dup}, excluded(sent/skip)={skipped_excluded}",
            file=sys.stderr,
        )
    return added


def append_linkedin(companies: list[dict[str, Any]], csv_path: Path) -> int:
    existing: set[str] = set()
    rows: list[dict[str, str]] = []
    fieldnames = ["linkedin_url", "name", "company", "note"]
    if csv_path.exists():
        with csv_path.open(newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or fieldnames
            for row in reader:
                rows.append(row)
                co = row.get("company") or row.get("name") or ""
                if co:
                    existing.add(history.canonical_company_id(co))
    added = 0
    for c in companies:
        if not isinstance(c, dict):
            continue
        company = c.get("company") or c.get("name") or ""
        if not company:
            continue
        canon = history.canonical_company_id(str(company))
        if canon in existing:
            continue
        rows.append(
            {
                "linkedin_url": c.get("linkedin_url", ""),
                "name": c.get("contact_name", c.get("name", "")),
                "company": company if c.get("company") else c.get("name", ""),
                "note": (c.get("hook_seed") or c.get("notes") or c.get("why_fit") or "")[:200],
            }
        )
        existing.add(canon)
        added += 1
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    return added


def _save_raw_ingest(skill: str, raw: str) -> Path | None:
    if not raw.strip():
        return None
    base = SKILLS_ROOT / ("jp-form-outreach" if skill == "jp_form" else "linkedin-outreach")
    intake_dir = base / "data" / "intake"
    intake_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    out = intake_dir / f"ingest_{stamp}.json"
    out.write_text(raw, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", required=True, choices=["linkedin", "jp_form"])
    ap.add_argument("--input", default="-")
    ap.add_argument("--format", default="jsonl", choices=["jsonl", "json"])
    ap.add_argument("--targets-path", default=None)
    ap.add_argument("--brief", default=None, help="brief id for sent/skip exclude filtering")
    ap.add_argument("--no-save-raw", action="store_true", help="do not persist raw ingestion payload")
    args = ap.parse_args(argv)

    raw, items = _read_input(args.input, args.format)
    if not args.no_save_raw:
        saved = _save_raw_ingest(args.skill, raw)
        if saved:
            print(f"[append_targets] saved raw ingest -> {saved}", file=sys.stderr)
    if args.skill == "jp_form":
        path = Path(args.targets_path) if args.targets_path else SKILLS_ROOT / "jp-form-outreach" / "targets.yaml"
        exclude = history.load_global_exclude_set(args.brief)
        n = append_jp_form(items, path, exclude_ids=exclude)
        print(f"[append_targets] jp_form: added {n} → {path}")
    else:
        path = Path(args.targets_path) if args.targets_path else SKILLS_ROOT / "linkedin-outreach" / "targets.csv"
        n = append_linkedin(items, path)
        print(f"[append_targets] linkedin: added {n} → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
