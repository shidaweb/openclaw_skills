#!/usr/bin/env python3
"""
linkedin-outreach pipeline.

Stages: fetch-leads -> enrich -> draft -> preview -> (v2) send

All Claude calls go through `openclaw infer model run` so prompt caching
(94-97% hit rate observed) applies automatically when the system prompt
stays stable.

State files live in ./data/*.jsonl  (append-only, resumable).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _outreach_core import draft as core_draft
from _outreach_core import history as core_history
from _outreach_core import infer as core_infer
from _outreach_core import preview as core_preview
from _outreach_core import prompt as core_prompt
from _outreach_core.config import load_merged_config
from _outreach_core.progress import HeartbeatSession
from _outreach_core.verify import (
    close_needs_attention,
    handle_verify_result,
    list_open_needs_attention,
    verify_send_completed,
)

try:
    import yaml
except ImportError:
    print("Missing dependency: pyyaml. Install with `pip install pyyaml`", file=sys.stderr)
    sys.exit(1)

SKILL_DIR = Path(__file__).resolve().parent
DATA_DIR = SKILL_DIR / "data"
PROMPTS_DIR = SKILL_DIR / "prompts"
DATA_DIR.mkdir(exist_ok=True)

DEFAULT_MODEL = core_infer.DEFAULT_MODEL
BROWSER_PROFILE = core_infer.BROWSER_PROFILE
RATE_LIMIT_SECONDS = 4  # between page loads, to look human

SKIP_HISTORY_PATH = DATA_DIR / "skip_history.jsonl"
SENT_HISTORY_PATH = DATA_DIR / "sent_history.jsonl"


# ============================================================================
# Skip / sent history
# ============================================================================
#
# Lead IDs (Sales Nav slugs / public-profile slugs) are stable per-person, so
# we maintain two append-only logs to avoid re-processing:
#
#   data/skip_history.jsonl  — leads previously SKIP'd by Sonnet for being
#                              out-of-scope or insufficient data. Filter at
#                              fetch / enrich stages to save credits.
#   data/sent_history.jsonl  — leads that have already been contacted.
#                              Filtered out so we don't double-message.
#
# To force a retry of skipped leads (e.g. after persona changes), pass
# `--ignore-skip-history` to fetch-leads / fetch-from-csv, or just delete
# the skip_history.jsonl file.

def load_skip_set() -> set[str]:
    return core_history.load_skip_set(DATA_DIR)


def load_sent_set() -> set[str]:
    return core_history.load_sent_set(DATA_DIR)


def append_skip_history(skipped_drafts: list[dict[str, Any]]) -> None:
    """Append newly-SKIPped drafts to skip_history.jsonl."""
    core_history.append_skip_history(
        skipped_drafts,
        DATA_DIR,
        extra_fields=("name", "company", "title"),
    )


def stage_history(action: str) -> None:
    """View or purge skip/sent history."""
    if action == "needs-attention":
        rows = list_open_needs_attention(DATA_DIR)
        print(f"needs_attention.jsonl: {len(rows)} open")
        for e in rows[-20:]:
            print(f"  - {e.get('target_id')}: {e.get('name')} | {e.get('reason', '')[:80]}")
        return
    if action == "show":
        skip_n = sum(1 for _ in SKIP_HISTORY_PATH.open()) if SKIP_HISTORY_PATH.exists() else 0
        sent_n = sum(1 for _ in SENT_HISTORY_PATH.open()) if SENT_HISTORY_PATH.exists() else 0
        print(f"skip_history.jsonl: {skip_n} entries  ({SKIP_HISTORY_PATH})")
        print(f"sent_history.jsonl: {sent_n} entries  ({SENT_HISTORY_PATH})")
        if skip_n:
            print("\nMost recent skips:")
            entries = [json.loads(l) for l in SKIP_HISTORY_PATH.open() if l.strip()]
            for e in entries[-5:]:
                print(f"  - {e.get('name', '?'):<30} | {(e.get('reason') or '')[:80]}")
        return
    if action == "bootstrap":
        drafts_path = DATA_DIR / "drafts.jsonl"
        if not drafts_path.exists():
            print(f"no drafts.jsonl yet at {drafts_path}; run a draft pass first")
            return
        drafts = [json.loads(l) for l in drafts_path.open() if l.strip()]
        new_skips = [d for d in drafts if (d.get("draft") or {}).get("subject") == "SKIP"]
        existing = load_skip_set()
        new_only = [d for d in new_skips if d["id"] not in existing]
        append_skip_history(new_only)
        print(f"bootstrap: scanned {len(drafts)} drafts, {len(new_skips)} SKIPs found, {len(new_only)} new entries added")
        return
    if action == "purge-skip":
        SKIP_HISTORY_PATH.unlink(missing_ok=True)
        print(f"deleted {SKIP_HISTORY_PATH}")
        return
    if action == "purge-sent":
        SENT_HISTORY_PATH.unlink(missing_ok=True)
        print(f"deleted {SENT_HISTORY_PATH}")
        return
    if action == "purge-all":
        SKIP_HISTORY_PATH.unlink(missing_ok=True)
        SENT_HISTORY_PATH.unlink(missing_ok=True)
        print(f"deleted {SKIP_HISTORY_PATH} and {SENT_HISTORY_PATH}")
        return


def append_sent_history(sent_drafts: list[dict[str, Any]]) -> None:
    """Append sent drafts to sent_history.jsonl (called from stage_send)."""
    core_history.append_sent_history(
        sent_drafts,
        DATA_DIR,
        extra_fields=("name", "company", "title"),
    )


oc_browser = core_infer.oc_browser
oc_infer = core_infer.oc_infer


# ============================================================================
# Snapshot parsing (heuristic — iterate against real Sales Nav output)
# ============================================================================

REF_RE = re.compile(r"\[ref=(e\d+)\]")
LINK_NAME_RE = re.compile(r'link\s+"([^"]+)"\s+\[ref=(e\d+)\]\s+\[cursor=pointer\]:\s*$')
SALES_LEAD_URL_RE = re.compile(r'/url:\s*(/sales/lead/[^\s"]+)')
SALES_COMPANY_URL_RE = re.compile(r'/url:\s*/sales/company/')
TEXT_LINE_RE = re.compile(r'^\s*-\s*text:\s*(.+?)\s*$')
GENERIC_TEXT_RE = re.compile(r'^\s*-\s*generic\s+\[ref=\w+\]:\s+(.+?)\s*$')
COMPANY_LINK_RE = re.compile(r'^\s*-\s*link\s+"([^"]+)"\s+\[ref=\w+\]\s+\[cursor=pointer\]:\s*$')


def _looks_like_location(s: str) -> bool:
    """Heuristic: does this string look like a 'City, Region, Country' address?"""
    if "," not in s or len(s) > 80:
        return False
    parts = [p.strip() for p in s.split(",")]
    if not (1 < len(parts) <= 4):
        return False
    bad = ("connection", "role", "profile", "recent", "in role", "in company",
           "post", "linkedin", "about:", "shared")
    if any(b in s.lower() for b in bad):
        return False
    return all(2 < len(p) < 40 for p in parts)


def parse_search_results(snapshot: str) -> list[dict[str, Any]]:
    """
    Extract leads from a Sales Nav saved-search snapshot.

    Anchor: a `link "<Name>" [ref=eN] [cursor=pointer]:` line whose VERY NEXT
    line is `- /url: /sales/lead/<slug>?...`. We then scan forward up to ~80
    lines collecting title / company / location / tenure / about for that
    lead, stopping when we hit the next /sales/lead/ anchor.

    Validated against an actual Sales Nav saved-search snapshot.
    """
    leads: list[dict[str, Any]] = []
    lines = snapshot.splitlines()
    seen_slugs: set[str] = set()

    for i, line in enumerate(lines):
        m = LINK_NAME_RE.search(line)
        if not m:
            continue
        name = m.group(1).strip()
        # Skip "Go to X's profile" decorative anchors — only the bare-name
        # link is followed directly by the /sales/lead/ URL.
        if name.startswith("Go to ") or "profile" in name.lower():
            continue
        if i + 1 >= len(lines):
            continue
        url_match = SALES_LEAD_URL_RE.search(lines[i + 1])
        if not url_match:
            continue

        url_path = url_match.group(1)
        slug = url_path.split("/sales/lead/")[1].split("?")[0]
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        ref = m.group(2)

        title: str | None = None
        company: str | None = None
        location: str | None = None
        tenure: str | None = None
        about: str | None = None
        message_ref: str | None = None

        for j in range(i + 2, min(i + 80, len(lines))):
            ctx = lines[j]

            # Stop when we hit the next lead anchor (lead URL but not a company URL)
            if (SALES_LEAD_URL_RE.search(ctx)
                and not SALES_COMPANY_URL_RE.search(ctx)
                and j > i + 5):
                break

            # Title: first long `text:` line within this lead block
            if title is None:
                tm = TEXT_LINE_RE.match(ctx)
                if tm:
                    cand = tm.group(1).strip()
                    if (len(cand) > 8
                        and "connection" not in cand.lower()
                        and "in role" not in cand
                        and "in company" not in cand
                        and not cand.startswith("·")
                        and cand not in ("|",)):
                        title = cand

            # Company: link "X" line followed immediately by /url: /sales/company/
            if company is None:
                cm = COMPANY_LINK_RE.match(ctx)
                if cm and j + 1 < len(lines) and SALES_COMPANY_URL_RE.search(lines[j + 1]):
                    company = cm.group(1).strip()

            # Tenure
            if tenure is None and "in role" in ctx:
                tem = re.search(r"text:\s*(.+?in role)", ctx)
                if tem:
                    tenure = tem.group(1).strip()

            # Location
            if location is None:
                lm = GENERIC_TEXT_RE.match(ctx)
                if lm and _looks_like_location(lm.group(1)):
                    location = lm.group(1).strip()

            # About: scan a few lines after `term ...: "About:"`
            if about is None and "About:" in ctx:
                for k in range(j + 1, min(j + 10, len(lines))):
                    am = TEXT_LINE_RE.match(lines[k])
                    if am:
                        about = am.group(1).strip()
                        break

            # Capture the "Message X" button ref for v2 send
            if message_ref is None and "Message " in ctx:
                rm = re.search(r'button\s+"Message [^"]+"\s+\[ref=(e\d+)\]', ctx)
                if rm:
                    message_ref = rm.group(1)

        url = f"https://www.linkedin.com{url_path.split('?')[0]}"

        leads.append({
            "id": slug,
            "name": name,
            "title": title,
            "company": company,
            "location": location,
            "tenure": tenure,
            "about_snippet": about,
            "profile_url": url,
            "_message_ref": message_ref,
            "_link_ref": ref,
            "_source": "sales_nav_search",
            "_fetched_at": datetime.utcnow().isoformat() + "Z",
        })

    return leads


def parse_profile(snapshot: str) -> dict[str, Any]:
    """
    Extract profile signals from a Sales Nav lead-page snapshot.

    Targets:
      - headline: the curated tagline under the name heading (e.g.
        "Executive Coach & Author | Identity Clarity for Overlooked Leaders")
      - about: the full About-section text (truncated by LinkedIn at "Show more")
      - current_role_description: bullet/text content under "Current role"
      - recent_activity: list of {type, time, text} for the most recent posts
      - experience: compact list of past role headings (top 3)

    Validated against an actual Sales Nav lead-page snapshot.
    """
    out: dict[str, Any] = {
        "headline": None,
        "about": None,
        "current_role_description": None,
        "recent_activity": [],
        "experience": [],
    }
    lines = snapshot.splitlines()
    GENERIC_LINE = re.compile(r"^\s*-\s*generic\s+\[ref=\w+\]:\s+(.+?)\s*$")
    TEXT_LINE = re.compile(r"^\s*-\s*text:\s*(.+?)\s*$")
    HEADING_LV2 = re.compile(r'heading "([^"]+)" \[level=2\]')

    # 1. Headline — scan past `heading "<NAME>" [level=1]` for the first
    # generic-text line with a substantial tagline (skip "2nd", "First time
    # view", "Last active", profile picture labels, etc.).
    for i, line in enumerate(lines):
        if not re.search(r'heading "[^"]+" \[level=1\] \[ref=\w+\]', line):
            continue
        for j in range(i + 1, min(i + 18, len(lines))):
            gm = GENERIC_LINE.match(lines[j])
            if not gm:
                continue
            cand = gm.group(1).strip()
            low = cand.lower()
            if (len(cand) > 25
                and not cand.endswith((":", ","))
                and "connection" not in low
                and "last active" not in low
                and "profile" not in low
                and "first time" not in low):
                out["headline"] = cand
                break
        if out["headline"]:
            break

    # 2. About — first text-line within ~20 lines after `heading "About" [level=1]`.
    for i, line in enumerate(lines):
        if re.search(r'heading "About" \[level=1\]', line):
            for j in range(i + 1, min(i + 20, len(lines))):
                tm = TEXT_LINE.match(lines[j])
                if tm:
                    out["about"] = tm.group(1).strip()
                    break
            break

    # 3. Current role description — text lines under `heading "Current role"`,
    # stop at next level-2 heading. Skip date strings, "at", and connector text.
    for i, line in enumerate(lines):
        if not re.search(r'heading "Current role" \[level=2\]', line):
            continue
        collected: list[str] = []
        for j in range(i + 1, min(i + 50, len(lines))):
            if HEADING_LV2.search(lines[j]):
                break
            tm = TEXT_LINE.match(lines[j])
            if not tm:
                continue
            cand = tm.group(1).strip()
            if (len(cand) > 8
                and cand != "at"
                and cand.lower() not in ("also worked at",)
                and not re.match(r"^\d+\s+(years?|yrs?|months?|mos?)\s*$", cand)
                and "Present" not in cand):
                collected.append(cand)
        if collected:
            out["current_role_description"] = " ".join(collected)
        break

    # 4. Recent activity — within `region "Recent activity on LinkedIn"`.
    in_region = False
    region_indent = -1
    current: dict[str, Any] | None = None
    for line in lines:
        if 'region "Recent activity on LinkedIn"' in line and not in_region:
            in_region = True
            region_indent = len(line) - len(line.lstrip())
            continue
        if not in_region:
            continue
        # Exit region when we encounter another region/heading at <= indent
        cur_indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if stripped and cur_indent <= region_indent and (
            stripped.startswith("- region") or stripped.startswith("- separator")
        ):
            if current and current.get("text"):
                out["recent_activity"].append(current)
            current = None
            in_region = False
            continue
        if "article [ref=" in line:
            if current and current.get("text"):
                out["recent_activity"].append(current)
            current = {"type": None, "time": None, "text": None}
            continue
        if current is None:
            continue
        if current["type"] is None:
            hm = re.search(r'heading "([^"]+(?:posted|shared|reshared)[^"]*)" \[level=4\]', line, re.IGNORECASE)
            if hm:
                current["type"] = hm.group(1).strip()
        if current["time"] is None:
            tm = re.search(r"time \[ref=\w+\]:\s*(.+)", line)
            if tm:
                current["time"] = tm.group(1).strip()
        if current["text"] is None:
            gm = GENERIC_LINE.match(line)
            if gm:
                cand = gm.group(1).strip()
                low = cand.lower()
                if (len(cand) > 30
                    and "reaction" not in low
                    and "comment" not in low
                    and not cand.startswith("·")
                    and not cand.endswith(",")):
                    current["text"] = cand
    if current and current.get("text"):
        out["recent_activity"].append(current)
    out["recent_activity"] = out["recent_activity"][:3]

    # 5. Experience — role headings under "<Name>'s experience" / "<Name>' experience"
    # (handles both possessive forms; LinkedIn drops the trailing 's' for names ending in s).
    for i, line in enumerate(lines):
        if not re.search(r'heading "[^"]+ experience" \[level=2\]', line):
            continue
        for j in range(i + 1, min(i + 100, len(lines))):
            if re.search(r'heading "(Education|Volunteering|Skills|Languages|Recommendations)" \[level=2\]', lines[j]):
                break
            hm = HEADING_LV2.search(lines[j])
            if hm and "experience" not in hm.group(1).lower():
                out["experience"].append(hm.group(1).strip())
        break
    out["experience"] = out["experience"][:3]
    return out


# ============================================================================
# Stage: fetch-leads
# ============================================================================
#
# Strategy: OpenClaw's `snapshot` truncates output around ~600 lines (=~3
# Sales Nav lead cards). To get more, we use `evaluate` to run JavaScript
# directly in the page DOM. JS sees all loaded leads regardless of snapshot
# size limits. We only need {id, name, profile_url} from search results;
# headline / about / activity all come from the per-profile `enrich` stage.

LEADS_JS_EXTRACTOR = r"""
() => {
  const leads = [];
  const seen = new Set();
  const cleanName = (s) => {
    return s
      .replace(/\s+(is\s+(reachable|online|away)|was\s+last\s+active.+|recently\s+active|now)\s*$/i, '')
      .replace(/\s+(reachable|online|away)\s*$/i, '')
      .trim();
  };
  const links = document.querySelectorAll('a[href*="/sales/lead/"]');
  for (const a of links) {
    const m = a.href.match(/\/sales\/lead\/([^?]+)/);
    if (!m) continue;
    const slug = m[1];
    if (seen.has(slug)) continue;
    const rawTxt = (a.textContent || '').trim();
    if (!rawTxt || rawTxt.startsWith('Go to ') || /profile/i.test(rawTxt) || rawTxt.length > 120) continue;
    const txt = cleanName(rawTxt);
    if (!txt) continue;
    seen.add(slug);
    const card = a.closest('li');
    let title = null, company = null, location = null, tenure = null, about = null;
    if (card) {
      const text = (card.innerText || '').replace(/\s+/g, ' ').trim();
      const compLink = card.querySelector('a[href*="/sales/company/"]');
      if (compLink) company = compLink.textContent.trim();
      const tenMatch = text.match(/(\d+\s+(?:years?|months?|yrs?|mos?)(?:\s+\d+\s+(?:months?|mos?))?\s+in\s+role)/i);
      if (tenMatch) tenure = tenMatch[1];
      const locMatch = text.match(/([A-Z][\w\sÀ-ſ\.\-']+(?:,\s*[\w\sÀ-ſ\.\-']+){1,3})\s+\d+\s+(?:year|month|day|yr|mo)/);
      if (locMatch) location = locMatch[1].trim();
      const aboutMatch = text.match(/About:\s*([^…]{20,200})/);
      if (aboutMatch) about = aboutMatch[1].trim();
    }
    leads.push({
      id: slug,
      name: txt,
      title: title,
      company: company,
      location: location,
      tenure: tenure,
      about_snippet: about,
      profile_url: a.href.split('?')[0],
    });
  }
  return leads;
}
"""




def _evaluate(js: str) -> Any:
    """Browser evaluate via _outreach_core.infer.oc_evaluate (no LLM)."""
    return core_infer.oc_evaluate(js, profile=BROWSER_PROFILE)


def _scroll_page(steps: int = 8, px_per_step: int = 600) -> None:
    """Scroll the page down a few times to trigger any lazy-loaded leads."""
    for _ in range(steps):
        _evaluate(f"() => window.scrollBy(0, {px_per_step})")
        time.sleep(0.4)
    time.sleep(0.5)


def _extract_leads_from_dom() -> list[dict[str, Any]]:
    """Run the JS extractor and return the leads list (empty on any error)."""
    result = _evaluate(LEADS_JS_EXTRACTOR)
    if isinstance(result, list):
        return result
    return []


def stage_fetch_leads(search_url: str, limit: int, out_path: Path,
                       ignore_skip_history: bool = False) -> None:
    print(f"[fetch-leads] navigating to {search_url}")
    oc_browser("open", search_url)
    time.sleep(RATE_LIMIT_SECONDS)

    skip_ids = set() if ignore_skip_history else load_skip_set()
    sent_ids = load_sent_set()
    if skip_ids or sent_ids:
        print(f"[fetch-leads] excluding {len(skip_ids)} SKIPped + {len(sent_ids)} sent leads from history")

    all_leads: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    page = 1
    while len(all_leads) < limit and page <= 8:
        # Make sure all lead cards on this page are loaded into the DOM
        _scroll_page(steps=10)

        page_leads = _extract_leads_from_dom()
        new = [l for l in page_leads
               if l["id"] not in seen_ids
               and l["id"] not in skip_ids
               and l["id"] not in sent_ids]
        skipped_by_history = len([l for l in page_leads
                                   if l["id"] not in seen_ids
                                   and (l["id"] in skip_ids or l["id"] in sent_ids)])
        for l in new:
            seen_ids.add(l["id"])
        msg = f"[fetch-leads] page {page}: extracted {len(page_leads)} from DOM, {len(new)} new"
        if skipped_by_history:
            msg += f" ({skipped_by_history} filtered by history)"
        msg += f" (total {len(all_leads) + len(new)})"
        print(msg)
        all_leads.extend(new)

        if len(all_leads) >= limit:
            break
        if not new and not skipped_by_history:
            print("[fetch-leads] no new leads on this page; stopping")
            break

        # Advance to next page via URL parameter (Sales Nav supports &page=N)
        page += 1
        next_url = search_url + f"&page={page}" if "&page=" not in search_url else re.sub(r"&page=\d+", f"&page={page}", search_url)
        print(f"[fetch-leads] navigating to page {page}: {next_url}")
        oc_browser("open", next_url)
        time.sleep(RATE_LIMIT_SECONDS)

    all_leads = all_leads[:limit]
    now = datetime.utcnow().isoformat() + "Z"
    for lead in all_leads:
        lead.setdefault("_source", "sales_nav_search")
        lead.setdefault("_fetched_at", now)

    with out_path.open("w") as f:
        for lead in all_leads:
            f.write(json.dumps(lead, ensure_ascii=False) + "\n")

    # Save a sample snapshot of the last page for debugging.
    sample_path = DATA_DIR / "sample_search.txt"
    last_snap = oc_browser("snapshot")
    if last_snap:
        sample_path.write_text(last_snap)

    print(f"[fetch-leads] wrote {len(all_leads)} leads -> {out_path}")


# ============================================================================
# Stage: fetch-from-csv
# ============================================================================

def stage_fetch_from_csv(csv_path: Path, out_path: Path,
                          ignore_skip_history: bool = False) -> None:
    """
    Read leads from a CSV file. Supports two URL formats:
      - Sales Nav:  https://www.linkedin.com/sales/lead/<slug>?...
      - Public:     https://www.linkedin.com/in/<slug>/

    For Sales Nav URLs, enrich works as-is. For public URLs, we navigate
    to the Sales Nav search-by-name URL to redirect into Sales Nav, then
    enrich. (LinkedIn auto-resolves /in/<slug> to a Sales Nav lead page
    when you're signed in to Sales Nav.)

    Required CSV column: linkedin_url (or profile_url, or url)
    Optional columns:    name, title, company, location, note
    """
    import csv as csvlib

    skip_ids = set() if ignore_skip_history else load_skip_set()
    sent_ids = load_sent_set()

    leads: list[dict[str, Any]] = []
    filtered_by_history = 0
    with csv_path.open(newline="") as f:
        reader = csvlib.DictReader(f)
        for row in reader:
            # Defensive: merge stray extra columns (None key) into the last field
            row.pop(None, None)
            url = (row.get("linkedin_url") or row.get("profile_url") or row.get("url") or "").strip()
            if not url:
                continue

            # Derive a stable id (slug) and a usable profile_url
            if "/sales/lead/" in url:
                slug = url.split("/sales/lead/")[1].split("?")[0]
                profile_url = url.split("?")[0]
                source = "csv_sales_nav"
            elif "/in/" in url:
                slug = "in/" + url.split("/in/")[1].split("/")[0].split("?")[0]
                # Send Sales Nav users into the Sales Nav redirect path so
                # parse_profile (which expects Sales Nav DOM) works.
                public_slug = url.split("/in/")[1].split("/")[0].split("?")[0]
                profile_url = f"https://www.linkedin.com/sales/people/{public_slug},NAME_SEARCH"
                source = "csv_public"
            else:
                slug = url
                profile_url = url
                source = "csv_other"

            if slug in skip_ids or slug in sent_ids:
                filtered_by_history += 1
                continue

            leads.append({
                "id": slug,
                "name": (row.get("name") or "").strip() or None,
                "title": (row.get("title") or "").strip() or None,
                "company": (row.get("company") or "").strip() or None,
                "location": (row.get("location") or "").strip() or None,
                "tenure": None,
                "about_snippet": (row.get("note") or "").strip() or None,
                "profile_url": profile_url,
                "_source": source,
                "_fetched_at": datetime.utcnow().isoformat() + "Z",
            })

    with out_path.open("w") as f:
        for lead in leads:
            f.write(json.dumps(lead, ensure_ascii=False) + "\n")
    msg = f"[fetch-from-csv] wrote {len(leads)} leads -> {out_path}"
    if filtered_by_history:
        msg += f"  ({filtered_by_history} filtered by skip/sent history)"
    print(msg)


# ============================================================================
# Stage: lookup-urls (auto-resolve LinkedIn URLs from name + company)
# ============================================================================

_LOOKUP_FIRST_LEAD_JS = r"""
() => {
  const links = document.querySelectorAll('a[href*="/sales/lead/"]');
  const seen = new Set();
  const cleanName = (s) => s.replace(/\s+(is\s+(reachable|online|away)|was\s+last\s+active.+|recently\s+active)\s*$/i, '').trim();
  for (const a of links) {
    const m = a.href.match(/\/sales\/lead\/([^?]+)/);
    if (!m) continue;
    const slug = m[1];
    if (seen.has(slug)) continue;
    const raw = (a.textContent || '').trim();
    if (!raw || raw.startsWith('Go to ') || /profile/i.test(raw) || raw.length > 120) continue;
    seen.add(slug);
    const name = cleanName(raw);
    // Get company from same lead card
    const card = a.closest('li');
    let company = null;
    if (card) {
      const compLink = card.querySelector('a[href*="/sales/company/"]');
      if (compLink) company = compLink.textContent.trim();
    }
    return { url: a.href.split('?')[0], name, company };
  }
  return null;
}
"""


def stage_lookup_urls(csv_path: Path, output_path: Path | None,
                       limit: int | None, overwrite: bool, dry_run: bool) -> None:
    """
    For each CSV row with name + company but no linkedin_url, search
    Sales Nav by '"<name>" "<company>"' keywords and write the first lead's
    URL back into the linkedin_url column.

    Args:
      csv_path: input CSV (must have linkedin_url, name, company columns)
      output_path: write result to this path; if None, overwrites input
      limit: process at most N rows (None = all)
      overwrite: if True, also re-resolve rows that already have linkedin_url
      dry_run: show what would be searched without actually navigating
    """
    import csv as csvlib

    if not csv_path.exists():
        print(f"[lookup-urls] CSV not found: {csv_path}", file=sys.stderr)
        return

    with csv_path.open(newline="") as f:
        reader = csvlib.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    if not rows:
        print("[lookup-urls] empty CSV")
        return

    if "linkedin_url" not in fieldnames:
        print("[lookup-urls] CSV missing required column 'linkedin_url'", file=sys.stderr)
        return

    # Defensive: if rows have extra columns (because a note had unquoted commas),
    # DictReader stores them under the None key. Merge those back into the last
    # field (note) so downstream writing doesn't blow up.
    last_field = fieldnames[-1] if fieldnames else "note"
    repaired = 0
    for row in rows:
        extras = row.pop(None, None)
        if extras:
            extra_str = ",".join(str(x) for x in extras if x is not None)
            cur = row.get(last_field) or ""
            row[last_field] = (cur + "," + extra_str).strip(",")
            repaired += 1
    if repaired:
        print(f"[lookup-urls] repaired {repaired} rows with unquoted commas (merged back into '{last_field}')")

    candidates: list[dict[str, Any]] = []
    for r in rows:
        name = (r.get("name") or "").strip()
        company = (r.get("company") or "").strip()
        existing_url = (r.get("linkedin_url") or "").strip()
        if not name or not company:
            continue
        if existing_url and not overwrite:
            continue
        candidates.append(r)

    if limit is not None:
        candidates = candidates[:limit]

    if not candidates:
        print("[lookup-urls] no rows need URL lookup (use --overwrite to re-resolve filled rows)")
        return

    print(f"[lookup-urls] {len(candidates)} executive(s) to look up"
          + (" [DRY RUN]" if dry_run else ""))
    for r in candidates:
        print(f"  - {r['name']} @ {r['company']}")
    if dry_run:
        return

    found = 0
    not_found: list[str] = []

    for i, r in enumerate(candidates, 1):
        name = r["name"].strip()
        company = r["company"].strip()
        # Build a precise keyword query: quoted name + quoted company
        query = f'"{name}" "{company}"'
        encoded = urllib.parse.quote(query)
        search_url = f"https://www.linkedin.com/sales/search/people?keywords={encoded}"

        print(f"\n[lookup-urls] ({i}/{len(candidates)}) {name} @ {company}")
        oc_browser("open", search_url)
        time.sleep(RATE_LIMIT_SECONDS)
        _scroll_page(steps=3)

        result = _evaluate(_LOOKUP_FIRST_LEAD_JS)
        if not isinstance(result, dict) or not result.get("url"):
            # Retry once with name only, in case the company filter is too narrow
            fallback_query = f'"{name}"'
            print(f"  [lookup-urls] not found with name+company; retrying with name only")
            oc_browser("open",
                       f"https://www.linkedin.com/sales/search/people?keywords={urllib.parse.quote(fallback_query)}")
            time.sleep(RATE_LIMIT_SECONDS)
            _scroll_page(steps=3)
            result = _evaluate(_LOOKUP_FIRST_LEAD_JS)

        if isinstance(result, dict) and result.get("url"):
            r["linkedin_url"] = result["url"]
            found_name = result.get("name", "?")
            found_company = result.get("company") or "?"
            match_company = company.lower() in found_company.lower() if found_company != "?" else False
            tag = "✓" if match_company else "?"
            print(f"  {tag} {found_name} (company found: {found_company}) → {result['url']}")
            found += 1
        else:
            print(f"  ✗ no result")
            not_found.append(f"{name} @ {company}")

        # Rate limit between searches
        time.sleep(2.0)

    out = output_path or csv_path
    with out.open("w", newline="") as f:
        # QUOTE_ALL guarantees commas/special chars inside any field stay safe
        # going forward, so this kind of corruption can't recur.
        writer = csvlib.DictWriter(
            f, fieldnames=fieldnames,
            extrasaction="ignore",
            quoting=csvlib.QUOTE_ALL,
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (v if v is not None else "") for k, v in row.items()})

    print(f"\n[lookup-urls] done · found {found}/{len(candidates)} · wrote {out}")
    if not_found:
        print(f"[lookup-urls] not found: {', '.join(not_found)}")


# ============================================================================
# Stage: campaign (one-shot full-pipeline runner)
# ============================================================================
#
# Chains the canonical 6-phase outreach pattern into a single command:
#   1. Pull        — fetch-from-csv (and lookup-urls if needed) OR fetch-leads
#   2. Enrich      — per-profile snapshot
#   3. Personalize — Sonnet draft (with cached system prompt)
#   4. Approve     — preview with interactive send prompt
#   5. Send        — auto-fill compose + click Send (per-draft confirm)
#   6. Log         — append to sent_history.jsonl
#
# Phases 4-6 happen inside the preview interactive flow.

def stage_campaign(
    csv_input: Path | None,
    search_url: str | None,
    limit: int,
    clean: bool,
    skip_lookup: bool,
    skip_send: bool,
) -> None:
    """Run the canonical outreach pipeline end-to-end."""
    bar = "=" * 70

    if clean:
        for f in ("leads.jsonl", "enriched.jsonl", "drafts.jsonl"):
            (DATA_DIR / f).unlink(missing_ok=True)
        print(f"[campaign] cleared previous run state")

    # --- Phase 1: Pull ---
    print(f"\n{bar}\n[1/6] PULL — gather leads\n{bar}")
    if csv_input:
        # Auto-resolve missing URLs from CSV before fetching
        if not skip_lookup:
            print(f"\n[campaign] (1a) lookup-urls — auto-fill missing linkedin_url from name+company")
            stage_lookup_urls(csv_input, output_path=None, limit=limit,
                               overwrite=False, dry_run=False)
        print(f"\n[campaign] (1b) fetch-from-csv")
        stage_fetch_from_csv(csv_input, DATA_DIR / "leads.jsonl",
                              ignore_skip_history=False)
    elif search_url:
        print(f"\n[campaign] (1) fetch-leads from Sales Nav saved search")
        stage_fetch_leads(search_url, limit, DATA_DIR / "leads.jsonl",
                           ignore_skip_history=False)
    else:
        print("[campaign] need --input <csv> or --search-url <url>", file=sys.stderr)
        return

    leads_n = sum(1 for line in (DATA_DIR / "leads.jsonl").open() if line.strip())
    if leads_n == 0:
        print(f"\n[campaign] no leads after pull — aborting")
        return
    print(f"\n[campaign] → {leads_n} leads pulled")

    # --- Phase 2: Enrich ---
    print(f"\n{bar}\n[2/6] ENRICH — profile detail + recent activity\n{bar}")
    stage_enrich(DATA_DIR / "leads.jsonl", DATA_DIR / "enriched.jsonl")

    enriched_n = sum(1 for line in (DATA_DIR / "enriched.jsonl").open() if line.strip())
    if enriched_n == 0:
        print(f"\n[campaign] no enriched profiles — aborting")
        return
    print(f"\n[campaign] → {enriched_n} profiles enriched")

    # --- Phase 3: Personalize ---
    print(f"\n{bar}\n[3/6] PERSONALIZE — Sonnet draft (cached system prompt)\n{bar}")
    try:
        cfg = load_merged_config(SKILL_DIR)
    except FileNotFoundError as e:
        print(f"[campaign] {e}", file=sys.stderr)
        return
    stage_draft(DATA_DIR / "enriched.jsonl", DATA_DIR / "drafts.jsonl", cfg)

    # Quick stats
    drafts = [json.loads(l) for l in (DATA_DIR / "drafts.jsonl").open() if l.strip()]
    sendable = [d for d in drafts if (d.get("draft") or {}).get("subject") != "SKIP"]
    skipped = len(drafts) - len(sendable)
    print(f"\n[campaign] → {len(sendable)} sendable, {skipped} SKIP "
          + f"(send rate {len(sendable) * 100 // len(drafts) if drafts else 0}%)")

    # --- Phases 4-6: Approve → Send → Log (inside preview's interactive flow) ---
    if skip_send:
        print(f"\n{bar}\n[4/6] PREVIEW — display only (send skipped)\n{bar}")
        stage_preview(DATA_DIR / "drafts.jsonl", interactive_send=False)
        print(f"\n[campaign] stopped at preview. To send later: python run.py send --ids ...")
        return

    print(f"\n{bar}\n[4-6/6] APPROVE → SEND → LOG (interactive)\n{bar}")
    stage_preview(DATA_DIR / "drafts.jsonl", interactive_send=True)


# ============================================================================
# Stage: enrich
# ============================================================================

def stage_enrich(input_path: Path, out_path: Path) -> None:
    leads = [json.loads(l) for l in input_path.open()]
    print(f"[enrich] {len(leads)} leads to enrich")
    enriched: list[dict[str, Any]] = []
    for i, lead in enumerate(leads, 1):
        print(f"[enrich] ({i}/{len(leads)}) {lead.get('name') or lead['id']}")
        oc_browser("open", lead["profile_url"])
        time.sleep(RATE_LIMIT_SECONDS)
        snap = oc_browser("snapshot")
        if snap is None:
            print(f"[enrich] failed snapshot for {lead['id']}, skipping")
            continue
        profile = parse_profile(snap)
        enriched.append({**lead, **profile, "_enriched_at": datetime.utcnow().isoformat() + "Z"})
        # Save sample for first lead
        if i == 1:
            (DATA_DIR / "sample_profile.txt").write_text(snap)

    with out_path.open("w") as f:
        for lead in enriched:
            f.write(json.dumps(lead, ensure_ascii=False) + "\n")
    print(f"[enrich] wrote {len(enriched)} enriched leads -> {out_path}")


# ============================================================================
# Stage: draft
# ============================================================================

def build_system_block(config: dict[str, Any]) -> str:
    """Cache-stable system block (delegates to _outreach_core.prompt)."""
    return core_prompt.build_system_block(config, PROMPTS_DIR)


def build_user_block(lead: dict[str, Any], max_chars: int) -> str:
    return (
        "<user>\n"
        "Generate a personalized InMail for the following LinkedIn lead.\n"
        f"Output strictly as JSON: {{\"subject\": \"...\", \"body\": \"...\"}}\n"
        f"`body` must be ≤ {max_chars} characters.\n"
        "If profile data is too thin to ground a real personal observation, "
        "output {\"subject\": \"SKIP\", \"body\": \"INSUFFICIENT_DATA: <reason>\"}.\n\n"
        "## Lead\n"
        "```json\n"
        f"{json.dumps(lead, ensure_ascii=False, indent=2)}\n"
        "```\n"
        "</user>\n"
    )


extract_first_json = core_prompt.extract_first_json


def stage_draft(input_path: Path, out_path: Path, config: dict[str, Any]) -> None:
    core_draft.stage_draft(
        input_path,
        out_path,
        config,
        prompts_dir=PROMPTS_DIR,
        build_user_block=build_user_block,
        oc_infer_fn=oc_infer,
        append_skip_fn=append_skip_history,
        default_model=DEFAULT_MODEL,
    )


# ============================================================================
# Stage: preview
# ============================================================================

def stage_preview(input_path: Path, interactive_send: bool = True) -> None:
    drafts = [json.loads(l) for l in input_path.open()]
    skipped = [d for d in drafts if d["draft"].get("subject") == "SKIP"]
    sendable = [d for d in drafts if d["draft"].get("subject") != "SKIP"]
    sent_ids = load_sent_set()

    bar = "=" * 70
    print(f"\n{bar}\nDRAFTS PREVIEW — {len(sendable)} sendable, {len(skipped)} skipped\n{bar}")
    for i, d in enumerate(sendable, 1):
        already = " [ALREADY SENT]" if d["id"] in sent_ids else ""
        print(f"\n[{i}] {d.get('name') or d['id']}{already}")
        print(f"    Profile: {d.get('profile_url', '')}")
        print(f"    Headline: {d.get('headline', '-')}")
        print(f"    Subject: {d['draft']['subject']}")
        print(f"    Body ({len(d['draft']['body'])} chars):")
        for line in d["draft"]["body"].splitlines():
            print(f"      {line}")
    if skipped:
        print(f"\n--- SKIPPED (insufficient data) ---")
        for d in skipped:
            print(f"  - {d.get('name') or d['id']}: {d['draft']['body']}")
    print(f"\n{bar}")

    if not interactive_send or not sendable:
        if sendable:
            print("Auto-send: python run.py send --ids 1,3,5")
        return

    # Interactive send prompt
    not_yet_sent = [i for i, d in enumerate(sendable, 1) if d["id"] not in sent_ids]
    if not not_yet_sent:
        print("All sendable drafts are already in sent_history. Nothing to send.")
        return

    valid = core_preview.prompt_send_ids(len(sendable), not_yet_sent)
    if valid is None:
        return
    core_preview.run_after_valid_ids(
        valid, sendable, lambda ids: stage_send(input_path, ids, mode="interactive")
    )


# ============================================================================
# Stage: send (v2 stub — opens Sales Nav messaging UI for the chosen lead)
# ============================================================================

def _find_button_ref_by_text(snapshot: str, text_contains: str) -> str | None:
    """Find a button ref whose accessible text contains a substring."""
    pattern = re.compile(rf'button\s+"[^"]*{re.escape(text_contains)}[^"]*"\s+\[ref=(e\d+)\]')
    for line in snapshot.splitlines():
        m = pattern.search(line)
        if m:
            return m.group(1)
    return None


# ----------------------------------------------------------------------------
# JS-based compose modal interaction (snapshot-truncation-proof)
# ----------------------------------------------------------------------------
# Sales Nav's InMail compose modal is appended to <body> after a long page,
# so snapshot truncation hides it. We use evaluate() to interact with the
# form directly via CSS selectors, bypassing the snapshot path entirely.

_FILL_COMPOSE_JS = r"""
() => {
  const setReactValue = (el, value) => {
    const proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };

  // Sales Nav InMail compose modal — confirmed selectors:
  //   Subject: input id starting with "compose-form-subject", aria-label "Subject (required)"
  //   Body:    textarea id starting with "compose-form-text", name="message"
  // Both elements have parent[role="dialog"] but the compose modal is NOT a
  // <div role="dialog"> — it's a different element type (likely a web
  // component or section). Querying by element id/name is the most reliable.
  const subj = document.querySelector(
    'input[id^="compose-form-subject"], '
    + 'input[aria-label="Subject (required)"], '
    + 'input[aria-label*="Subject" i]'
  );

  const bodyEl = document.querySelector(
    'textarea[id^="compose-form-text"], '
    + 'textarea[name="message"], '
    + 'textarea[aria-label*="Type your message" i]'
  );

  const subjectVal = __SUBJECT__;
  const bodyVal = __BODY__;

  if (subj) setReactValue(subj, subjectVal);
  if (bodyEl) setReactValue(bodyEl, bodyVal);

  return {
    subjectFound: !!subj,
    bodyFound: !!bodyEl,
    subjectInfo: subj ? (subj.getAttribute('aria-label') || subj.id || '?') : null,
    bodyInfo: bodyEl ? (bodyEl.getAttribute('aria-label') || bodyEl.id || '?') : null,
    composeOpen: !!subj
  };
}
"""

_CLICK_SEND_JS = r"""
() => {
  // Find the compose subject input — it's the anchor for the compose modal
  const subj = document.querySelector(
    'input[id^="compose-form-subject"], input[aria-label="Subject (required)"]'
  );
  if (!subj) return { clicked: false, reason: 'compose modal not open (no subject input)' };

  // Walk up to find the [role="dialog"] container
  const container = subj.closest('[role="dialog"]') || subj.closest('form') || subj.parentElement;
  if (!container) return { clicked: false, reason: 'compose container not found' };

  const matchSendText = (s) => /^(Send|Send InMail|Send message|Submit|送信)$/i.test((s || '').trim());

  const buttons = Array.from(container.querySelectorAll('button'));
  let btn = buttons.find(b =>
    matchSendText(b.textContent) && !b.disabled && b.offsetParent !== null
  );
  if (!btn) {
    btn = buttons.find(b => {
      const al = b.getAttribute('aria-label') || '';
      return matchSendText(al) && !b.disabled && b.offsetParent !== null;
    });
  }

  if (btn) {
    btn.click();
    return { clicked: true, label: (btn.textContent || btn.getAttribute('aria-label') || '').trim() };
  }

  // Diagnostic: list all buttons in container so we can see what we have
  const candidates = buttons.slice(0, 10).map(b => ({
    text: (b.textContent || '').trim().slice(0, 30),
    aria: b.getAttribute('aria-label'),
    disabled: b.disabled
  }));
  return { clicked: false, reason: 'no Send button matched', candidates };
}
"""

_VERIFY_SENT_JS = r"""
() => {
  const subj = document.querySelector(
    'input[id^="compose-form-subject"], input[aria-label="Subject (required)"]'
  );
  // Compose modal closes after successful send → subject input is gone
  if (!subj) return { sent: true, reason: 'compose modal closed' };
  return { sent: false, reason: 'compose modal still open' };
}
"""


def _fill_compose_via_js(subject: str, body: str) -> dict[str, Any] | None:
    """Fill Subject + Body in the currently-open compose modal via JS."""
    js = (_FILL_COMPOSE_JS
          .replace("__SUBJECT__", json.dumps(subject))
          .replace("__BODY__", json.dumps(body)))
    res = _evaluate(js)
    return res if isinstance(res, dict) else None


def _click_send_via_js() -> dict[str, Any] | None:
    res = _evaluate(_CLICK_SEND_JS)
    return res if isinstance(res, dict) else None


def _verify_sent_via_js() -> dict[str, Any] | None:
    res = _evaluate(_VERIFY_SENT_JS)
    return res if isinstance(res, dict) else None


def _find_send_button_ref(snapshot: str) -> str | None:
    """
    Find the Send button ref inside an open InMail compose modal.

    Sales Nav's send button is usually labelled "Send" (English) or "送信"
    (Japanese), rendered as a `button` with cursor=pointer. Some variants
    label it "Send InMail" or "Send message".
    """
    for line in snapshot.splitlines():
        m = re.search(
            r'button\s+"(?:Send(?:\s+InMail)?(?:\s+message)?|送信)"\s+\[ref=(e\d+)\]',
            line,
        )
        if m and "cursor=pointer" in line and "[disabled]" not in line:
            return m.group(1)
    return None


def _find_message_compose_fields(snapshot: str) -> tuple[str | None, str | None]:
    """
    From a Sales Nav InMail / message-compose modal snapshot, find the refs
    for (subject_field, body_field).

    Heuristics (Sales Nav's modal structure as of 2026.5):
      - Subject: textbox labelled "Subject" or "InMail subject"
      - Body: textbox/textarea labelled "Type a message" / "Body" /
              "Write a message", or a contenteditable in the modal.
    """
    subject_ref = None
    body_ref = None
    for line in snapshot.splitlines():
        if subject_ref is None:
            m = re.search(
                r'(?:textbox|combobox)\s+"(?:[^"]*[Ss]ubject[^"]*|InMail[^"]*subject[^"]*)"\s+\[ref=(e\d+)\]',
                line,
            )
            if m:
                subject_ref = m.group(1)
        if body_ref is None:
            m = re.search(
                r'(?:textbox|textarea)\s+"(?:[^"]*[Tt]ype a message[^"]*|[^"]*[Mm]essage body[^"]*|[^"]*[Ww]rite[^"]*|[^"]*[Bb]ody[^"]*)"\s+\[ref=(e\d+)\]',
                line,
            )
            if m:
                body_ref = m.group(1)
    return subject_ref, body_ref


def stage_send(
    input_path: Path,
    ids: set[int],
    mode: str = "interactive",
    heartbeat: str | None = None,
) -> None:
    """
    Drive the Sales Nav InMail compose UI for the given draft IDs.

    Modes:
      "interactive" — fill, then prompt in terminal "send? y/N",
                      click Send if yes, log to sent_history.
      "auto"        — fill, click Send, log. No prompts. Used by Slack agent
                      after it has confirmed with the user via chat.
      "fill-only"   — fill the compose modal and stop. Human clicks Send
                      manually. Use `mark-sent` afterwards to log.

    Per-lead steps:
      1. Open profile URL.
      2. Click the "Message <Name>" button.
      3. Snapshot the compose modal (saved to data/sample_compose.txt on
         first run for parser iteration).
      4. Find Subject + Body fields, type both.
      5. Depending on mode, click Send or stop.

    Args:
      ids: 1-based indices among SENDABLE drafts (SKIPs not counted).
    """
    drafts = [json.loads(l) for l in input_path.open()]
    sendable = [d for d in drafts if d.get("draft", {}).get("subject") != "SKIP"]
    if not sendable:
        print("[send] no sendable drafts in input; nothing to do")
        return

    targets = [d for i, d in enumerate(sendable, 1) if i in ids]
    if not targets:
        print(f"[send] none of ids {sorted(ids)} match sendable drafts (max={len(sendable)})")
        return

    # Guard against duplicate sends — refuse to re-send anyone already in sent_history.
    sent_ids = load_sent_set()
    pre_filtered = [d for d in targets if d["id"] in sent_ids]
    if pre_filtered:
        names = ", ".join(d.get("name", "?") for d in pre_filtered)
        print(f"[send] ⚠ skipping {len(pre_filtered)} lead(s) already in sent_history: {names}")
        print(f"[send]    delete data/sent_history.jsonl entries to override")
        targets = [d for d in targets if d["id"] not in sent_ids]
        if not targets:
            print(f"[send] nothing left to send")
            return

    mode_label = {
        "interactive": "interactive (will prompt for confirmation in terminal)",
        "auto": "AUTO (will click Send without prompting)",
        "fill-only": "fill-only (no Send click, you do it)",
    }.get(mode, mode)
    print(f"[send] preparing to fill+act on {len(targets)} InMail compose modals · mode={mode_label}")

    sent: list[dict[str, Any]] = []
    filled_only: list[dict[str, Any]] = []
    hb = HeartbeatSession(SKILL_DIR, "send", len(targets), heartbeat=heartbeat, data_dir=DATA_DIR)
    hb.start(f"send {len(targets)} InMails")
    for di, d in enumerate(targets):
        is_last = di == len(targets) - 1
        idx = sendable.index(d) + 1
        name = d.get("name", "?")
        subject = d["draft"]["subject"]
        body = d["draft"]["body"]
        profile_url = d["profile_url"]

        print(f"\n=== [{idx}] {name} ===")
        print(f"  Subject ({len(subject)} ch): {subject}")
        print(f"  Body    ({len(body)} ch)")

        # 1. Navigate to profile
        oc_browser("open", profile_url)
        time.sleep(RATE_LIMIT_SECONDS)

        # 2. Find and click Message button
        profile_snap = oc_browser("snapshot")
        if not profile_snap:
            print(f"  [send] failed snapshot for {name}; skipping")
            continue

        msg_ref = (d.get("_message_ref")
                   or _find_button_ref_by_text(profile_snap, f"Message {name.split()[0]}")
                   or _find_button_ref_by_text(profile_snap, "Message"))
        if not msg_ref:
            print(f"  [send] could not locate Message button on profile; skipping")
            continue
        print(f"  [send] clicking Message button (ref={msg_ref})")
        oc_browser("click", msg_ref)
        time.sleep(2.5)

        # 3. Snapshot the compose modal
        compose_snap = oc_browser("snapshot")
        if not compose_snap:
            print(f"  [send] failed compose snapshot; skipping")
            continue

        # Save first compose snapshot for parser iteration
        sample_compose = DATA_DIR / "sample_compose.txt"
        if not sample_compose.exists():
            sample_compose.write_text(compose_snap)
            print(f"  [send] saved first compose snapshot -> {sample_compose}")

        # 4. Fill Subject + Body via JS (snapshot truncation-proof)
        fill_res = _fill_compose_via_js(subject, body)
        if not fill_res:
            print(f"  [send] ⚠ JS fill returned no result; aborting this lead")
            continue
        if not fill_res.get("subjectFound") or not fill_res.get("bodyFound"):
            print(f"  [send] could not locate compose fields via JS:")
            print(f"         subject={fill_res.get('subjectFound')} ({fill_res.get('subjectInfo')})")
            print(f"         body   ={fill_res.get('bodyFound')} ({fill_res.get('bodyInfo')})")
            print(f"         compose modal open: {fill_res.get('composeOpen')}")
            if not fill_res.get('composeOpen'):
                print(f"  [send] Compose modal did not open — Message button click may have failed.")
            else:
                print(f"  [send] Compose IS open but selectors didn't match — DOM structure may have changed.")
            print(f"  [send] ----- copy this for manual paste -----")
            print(f"  Subject: {subject}")
            print(f"  Body:\n{body}")
            print(f"  ---------------------")
            if mode == "interactive":
                ans = input("  [send] After manual paste & send, type 'y' to log: ").strip().lower()
                if ans == "y":
                    sent.append(d)
            continue

        print(f"  [send] ✓ filled via JS · subject:{fill_res.get('subjectInfo')} · body:{fill_res.get('bodyInfo')}")
        time.sleep(1.0)  # let React validate

        # 5. Decide whether to click Send
        if mode == "fill-only":
            print(f"  [send] ✓ filled. Review and click Send manually.")
            filled_only.append(d)
            continue

        if mode == "interactive":
            ans = input("  [send] Click Send now? (y/N): ").strip().lower()
            if ans != "y":
                print(f"  [send] aborted — modal left filled, will not log.")
                filled_only.append(d)
                continue

        # mode == "auto" OR interactive-yes → click Send via JS
        send_res = _click_send_via_js()
        if not send_res or not send_res.get("clicked"):
            print(f"  [send] ⚠ Send button not found via JS (dialog={send_res and send_res.get('dialogPresent')})")
            print(f"  [send] Modal left filled. Inspect manually.")
            filled_only.append(d)
            continue

        print(f"  [send] clicked Send button: {send_res.get('label')}")
        time.sleep(3.0)

        # 6. Verify
        browser_verify = _verify_sent_via_js()
        vresult = verify_send_completed(
            d,
            "linkedin",
            browser_verify=browser_verify,
            data_dir=DATA_DIR,
        )
        outcome = handle_verify_result(d, vresult, DATA_DIR, channel="linkedin")
        if outcome != "sent_ok":
            print(f"  [send] ⚠ {vresult.get('status')}: {vresult.get('reason')}")
            filled_only.append(d)
            hb.tick(sendable.index(d) + 1, f"{name} verify {vresult.get('status')}")
            continue

        print(f"  [send] ✅ sent ({vresult.get('reason')}).")
        sent.append(d)
        hb.tick(sendable.index(d) + 1, f"{name} sent")

        # Rate limit between sends — look human, avoid LinkedIn spam detection
        if not is_last:
            delay = 30
            print(f"  [send] sleeping {delay}s before next send...")
            time.sleep(delay)

    hb.end(f"send done · sent={len(sent)} · pending={len(filled_only)}")
    if sent:
        append_sent_history(sent)
    print(f"\n[send] done · sent={len(sent)} · filled-only={len(filled_only)}")
    if filled_only and mode != "fill-only":
        names = ", ".join(d.get("name", "?") for d in filled_only)
        print(f"[send] not auto-logged (Send not confirmed): {names}")
        print(f"[send] If you DID send any of those manually, run:")
        ids_str = ",".join(str(sendable.index(d) + 1) for d in filled_only)
        print(f"      .venv/bin/python run.py mark-sent --ids {ids_str}")


def stage_resolve(target_id: str, fields: dict[str, str], config: dict[str, Any]) -> None:
    """Retry send after needs_attention (fields kept for logging; LinkedIn uses re-send)."""
    _ = fields, config
    path = DATA_DIR / "drafts.jsonl"
    if not path.exists():
        print(f"[resolve] {path} not found", file=sys.stderr)
        return
    sendable = [
        json.loads(line)
        for line in path.open()
        if line.strip() and json.loads(line).get("draft", {}).get("subject") != "SKIP"
    ]
    found_idx = next((i for i, d in enumerate(sendable, 1) if d.get("id") == target_id), None)
    if not found_idx:
        print(f"[resolve] {target_id} not in sendable drafts", file=sys.stderr)
        return
    close_needs_attention(DATA_DIR, target_id, resolution="retry send")
    print(f"[resolve] retrying send for {target_id} (draft #{found_idx})")
    stage_send(path, {found_idx}, mode="auto", heartbeat=None)


def stage_mark_sent(input_path: Path, ids: set[int]) -> None:
    """Append specified sendable drafts to sent_history.jsonl without browser action."""
    drafts = [json.loads(l) for l in input_path.open()]
    sendable = [d for d in drafts if d.get("draft", {}).get("subject") != "SKIP"]
    sent = [d for i, d in enumerate(sendable, 1) if i in ids]
    if not sent:
        print(f"[mark-sent] no matching sendable drafts for ids {sorted(ids)}")
        return
    append_sent_history(sent)
    print(f"[mark-sent] logged {len(sent)} drafts to sent_history.jsonl: "
          + ", ".join(d.get("name", "?") for d in sent))


# ============================================================================
# CLI
# ============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(prog="linkedin-outreach", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch-leads", help="Scrape Sales Nav saved search")
    p.add_argument("--search-url", required=True, help="Sales Nav saved search URL")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--out", default=str(DATA_DIR / "leads.jsonl"))
    p.add_argument("--ignore-skip-history", action="store_true",
                   help="Don't filter out leads in data/skip_history.jsonl")

    p = sub.add_parser("fetch-from-csv", help="Read curated leads from a CSV file")
    p.add_argument("--input", required=True, help="CSV file with at minimum a linkedin_url column")
    p.add_argument("--out", default=str(DATA_DIR / "leads.jsonl"))
    p.add_argument("--ignore-skip-history", action="store_true",
                   help="Don't filter out leads in data/skip_history.jsonl")

    p = sub.add_parser("campaign", help="Run the full 6-phase outreach pipeline (pull→enrich→draft→preview+send)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help="CSV file (Pull phase: fetch-from-csv with optional lookup-urls)")
    src.add_argument("--search-url", help="Sales Nav saved search URL (Pull phase: fetch-leads)")
    p.add_argument("--limit", type=int, default=10, help="Number of leads to process")
    p.add_argument("--clean", action="store_true", help="Wipe leads/enriched/drafts before running")
    p.add_argument("--skip-lookup", action="store_true",
                   help="Skip the lookup-urls sub-phase (use CSV linkedin_url as-is)")
    p.add_argument("--skip-send", action="store_true",
                   help="Stop at preview without sending (display only)")

    p = sub.add_parser("lookup-urls", help="Auto-fill linkedin_url in CSV by searching Sales Nav")
    p.add_argument("--input", required=True, help="CSV with name + company columns")
    p.add_argument("--out", default=None, help="Output CSV (default: overwrite input)")
    p.add_argument("--limit", type=int, default=None, help="Only process first N rows")
    p.add_argument("--overwrite", action="store_true",
                   help="Re-resolve rows that already have linkedin_url")
    p.add_argument("--dry-run", action="store_true", help="Show targets without searching")

    p = sub.add_parser("history", help="View / manage skip and sent history")
    p.add_argument(
        "action",
        choices=["show", "needs-attention", "bootstrap", "purge-skip", "purge-sent", "purge-all"],
        help=(
            "show | needs-attention | bootstrap | purge-skip | purge-sent | purge-all"
        ),
    )

    p = sub.add_parser("enrich", help="Open each profile and capture detail")
    p.add_argument("--in", dest="input_path", default=str(DATA_DIR / "leads.jsonl"))
    p.add_argument("--out", default=str(DATA_DIR / "enriched.jsonl"))

    p = sub.add_parser("draft", help="Generate personalized InMails")
    p.add_argument("--in", dest="input_path", default=str(DATA_DIR / "enriched.jsonl"))
    p.add_argument("--out", default=str(DATA_DIR / "drafts.jsonl"))
    p.add_argument("--config", default=str(SKILL_DIR / "config.yaml"))

    p = sub.add_parser("preview", help="Show all drafts in terminal for review (then prompt to send)")
    p.add_argument("--in", dest="input_path", default=str(DATA_DIR / "drafts.jsonl"))
    p.add_argument("--no-send", action="store_true",
                   help="Skip the interactive send prompt at the end")

    p = sub.add_parser("send", help="Drive Sales Nav to fill compose modal and send InMail")
    p.add_argument("--in", dest="input_path", default=str(DATA_DIR / "drafts.jsonl"))
    p.add_argument("--ids", required=True, help="Comma-separated SENDABLE draft indices (1-based, SKIPs not counted)")
    mode_group = p.add_mutually_exclusive_group()
    mode_group.add_argument("--auto-send", action="store_true",
                             help="Fill and click Send without prompting. Used by Slack agent AFTER user has confirmed.")
    mode_group.add_argument("--no-confirm", action="store_true",
                             help="Fill the compose modal and stop — human clicks Send manually.")
    p.add_argument(
        "--heartbeat",
        default=None,
        choices=["slack"],
        help="Post progress to Slack incoming webhook every ~5 min",
    )

    p = sub.add_parser("resolve", help="Close needs_attention and retry send")
    p.add_argument("--target-id", required=True)
    p.add_argument("--field", action="append", default=[], help="key=value (jp_form overrides)")

    p = sub.add_parser("mark-sent", help="Log specific drafts to sent_history.jsonl")
    p.add_argument("--in", dest="input_path", default=str(DATA_DIR / "drafts.jsonl"))
    p.add_argument("--ids", required=True, help="Comma-separated SENDABLE draft indices to mark as sent")

    args = ap.parse_args()

    if args.cmd == "campaign":
        stage_campaign(
            csv_input=Path(args.input) if args.input else None,
            search_url=args.search_url,
            limit=args.limit,
            clean=args.clean,
            skip_lookup=args.skip_lookup,
            skip_send=args.skip_send,
        )
    elif args.cmd == "fetch-leads":
        stage_fetch_leads(args.search_url, args.limit, Path(args.out),
                          ignore_skip_history=args.ignore_skip_history)
    elif args.cmd == "fetch-from-csv":
        stage_fetch_from_csv(Path(args.input), Path(args.out),
                             ignore_skip_history=args.ignore_skip_history)
    elif args.cmd == "lookup-urls":
        stage_lookup_urls(
            Path(args.input),
            Path(args.out) if args.out else None,
            args.limit,
            args.overwrite,
            args.dry_run,
        )
    elif args.cmd == "history":
        stage_history(args.action)
    elif args.cmd == "enrich":
        stage_enrich(Path(args.input_path), Path(args.out))
    elif args.cmd == "draft":
        try:
            cfg = load_merged_config(SKILL_DIR)
        except FileNotFoundError as e:
            print(f"[draft] {e}", file=sys.stderr)
            sys.exit(2)
        stage_draft(Path(args.input_path), Path(args.out), cfg)
    elif args.cmd == "preview":
        stage_preview(Path(args.input_path), interactive_send=not args.no_send)
    elif args.cmd == "send":
        ids = {int(x) for x in args.ids.split(",") if x.strip()}
        if args.auto_send:
            mode = "auto"
        elif args.no_confirm:
            mode = "fill-only"
        else:
            mode = "interactive"
        stage_send(Path(args.input_path), ids, mode=mode, heartbeat=args.heartbeat)
    elif args.cmd == "resolve":
        fields: dict[str, str] = {}
        for item in args.field:
            if "=" in item:
                k, v = item.split("=", 1)
                fields[k.strip()] = v.strip()
        try:
            cfg = load_merged_config(SKILL_DIR)
        except FileNotFoundError:
            cfg = {}
        stage_resolve(args.target_id, fields, cfg)
    elif args.cmd == "mark-sent":
        ids = {int(x) for x in args.ids.split(",") if x.strip()}
        stage_mark_sent(Path(args.input_path), ids)


if __name__ == "__main__":
    main()
