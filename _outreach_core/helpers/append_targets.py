#!/usr/bin/env python3
"""Append agent-curated targets to linkedin targets.csv or jp-form targets.yaml."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import history

SKILLS_ROOT = history.SKILLS_ROOT


def _read_input(path: str, fmt: str) -> list[dict[str, Any]]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text()
    if fmt == "jsonl":
        return [json.loads(line) for line in raw.splitlines() if line.strip()]
    data = json.loads(raw)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "companies" in data:
        return data["companies"]
    if isinstance(data, dict) and "targets" in data:
        return data["targets"]
    raise ValueError("expected JSON array or {companies: [...]}")


def _existing_jp_ids(yaml_path: Path) -> set[str]:
    try:
        import yaml
    except ImportError:
        raise RuntimeError("pyyaml required") from None
    if not yaml_path.exists():
        return set()
    raw = yaml.safe_load(yaml_path.read_text()) or {}
    companies = raw.get("companies") or []
    ids: set[str] = set()
    for c in companies:
        if isinstance(c, dict):
            if c.get("id"):
                ids.add(str(c["id"]))
            if c.get("name"):
                ids.add(history.canonical_company_id(str(c["name"])))
    return ids


def append_jp_form(companies: list[dict[str, Any]], yaml_path: Path) -> int:
    import yaml

    raw = yaml.safe_load(yaml_path.read_text()) if yaml_path.exists() else {}
    if raw is None:
        raw = {}
    existing = _existing_jp_ids(yaml_path)
    bucket = raw.get("companies") or []
    added = 0
    for c in companies:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        cid = c.get("id") or history.canonical_company_id(str(c["name"]))
        canon = history.canonical_company_id(str(c["name"]))
        if cid in existing or canon in existing:
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
        added += 1
    raw["companies"] = bucket
    yaml_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", required=True, choices=["linkedin", "jp_form"])
    ap.add_argument("--input", default="-")
    ap.add_argument("--format", default="jsonl", choices=["jsonl", "json"])
    ap.add_argument("--targets-path", default=None)
    args = ap.parse_args(argv)

    items = _read_input(args.input, args.format)
    if args.skill == "jp_form":
        path = Path(args.targets_path) if args.targets_path else SKILLS_ROOT / "jp-form-outreach" / "targets.yaml"
        n = append_jp_form(items, path)
        print(f"[append_targets] jp_form: added {n} → {path}")
    else:
        path = Path(args.targets_path) if args.targets_path else SKILLS_ROOT / "linkedin-outreach" / "targets.csv"
        n = append_linkedin(items, path)
        print(f"[append_targets] linkedin: added {n} → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
