#!/usr/bin/env python3
"""
Build a targeted Sales Navigator saved search by driving Chrome through
`openclaw browser`.

Plan:
  1. Open Sales Nav People search
  2. Type Japan/APAC keywords
  3. Add Geography: US, UK, Germany, France, Netherlands
  4. Add Industries: D2C / health / consumer / edtech-adjacent
  5. Add Seniority: CXO + VP + Director
  6. Toggle "Posted on LinkedIn" pinned filter
  7. Save the search
  8. Print the resulting URL

UI is complex and brittle. The script runs interactively — at each step it
prints what it tried and the user can recover with `Press Enter to continue`
prompts at key decision points.

Usage:
  cd ~/.openclaw/skills/linkedin-outreach
  .venv/bin/python build_search.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from typing import Iterable

PROFILE = "openclaw"
SETTLE = 1.5      # seconds to wait after a click
PAGE_LOAD = 4.0   # seconds to wait after a navigation


# --- openclaw browser helpers --------------------------------------------------

def oc(*args: str, capture: bool = True) -> str:
    cmd = ["openclaw", "browser", "--browser-profile", PROFILE, *args]
    res = subprocess.run(cmd, capture_output=capture, text=True)
    if res.returncode != 0:
        print(f"  ERR ({' '.join(args[:2])}): {res.stderr.strip()}", file=sys.stderr)
        return ""
    return res.stdout


def snap() -> str:
    return oc("snapshot")


def click(ref: str) -> None:
    if not ref:
        print("  (no ref to click)")
        return
    print(f"  click {ref}")
    oc("click", ref)
    time.sleep(SETTLE)


def type_in(ref: str, text: str, press_enter: bool = False) -> None:
    if not ref:
        print(f"  (no ref to type into '{text}')")
        return
    print(f"  type into {ref}: {text!r}{' + Enter' if press_enter else ''}")
    oc("type", ref, text)
    time.sleep(SETTLE)
    if press_enter:
        oc("press", "Enter")
        time.sleep(SETTLE)


def press(key: str) -> None:
    oc("press", key)
    time.sleep(SETTLE)


def current_url() -> str:
    """Get current URL via the browser's evaluate command."""
    out = oc("evaluate", "window.location.href")
    # `evaluate` may return JSON-quoted; strip quotes if present.
    s = out.strip()
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    return s


# --- snapshot search helpers ---------------------------------------------------

REF = re.compile(r"\[ref=(e\d+)\]")


def find_ref(snapshot: str, pattern: str, *, must_have: str | None = None) -> str | None:
    """Return ref of the first line matching `pattern` (regex). Optional
    `must_have` substring also required on the same line."""
    for line in snapshot.splitlines():
        if must_have and must_have not in line:
            continue
        if re.search(pattern, line):
            m = REF.search(line)
            if m:
                return m.group(1)
    return None


def find_all_refs(snapshot: str, pattern: str) -> list[tuple[str, str]]:
    """Return list of (ref, full_line) for every line matching pattern."""
    out: list[tuple[str, str]] = []
    for line in snapshot.splitlines():
        if re.search(pattern, line):
            m = REF.search(line)
            if m:
                out.append((m.group(1), line))
    return out


# --- filter operations ---------------------------------------------------------

def add_keyword(text: str) -> None:
    """Type into the top 'Search keywords' combobox and submit."""
    print(f"\n[1] Keywords: {text!r}")
    s = snap()
    kw_ref = find_ref(s, r'combobox\s+"Search keywords"')
    if not kw_ref:
        # Fallback: any combobox/textbox labelled Search
        kw_ref = find_ref(s, r'(combobox|textbox)\s+"Search"')
    if kw_ref:
        click(kw_ref)
        type_in(kw_ref, text, press_enter=True)
        time.sleep(PAGE_LOAD)
    else:
        print("  (could not find keyword search box; skipping)")


def add_filter_values(filter_label: str, values: Iterable[str]) -> None:
    """
    Generic flow for sidebar filters with autocomplete inputs.
    Sales Nav pattern:
      1. Click 'Expand <label> filter'
      2. The expanded panel contains a textbox for typing
      3. Type a value -> autocomplete dropdown -> click first option
      4. Repeat for each value
      5. Collapse (click expand again or move on)
    """
    print(f"\n[+] Filter '{filter_label}': {list(values)}")
    s = snap()
    expand_ref = find_ref(s, rf'Expand {re.escape(filter_label)} filter')
    if not expand_ref:
        print(f"  (could not find Expand button for {filter_label}; skipping)")
        return
    click(expand_ref)
    time.sleep(SETTLE)

    for val in values:
        s = snap()
        # Find the textbox inside the expanded filter.
        # Heuristic: look for a textbox or combobox labelled with the filter
        # name, OR for a generic text input that appeared after expansion.
        tb = (find_ref(s, rf'textbox\s+"{re.escape(filter_label)}', must_have='textbox')
              or find_ref(s, rf'combobox\s+"{re.escape(filter_label)}', must_have='combobox')
              or find_ref(s, rf'textbox\s+"Add a [^"]*{re.escape(filter_label.lower())}', must_have='textbox')
              or find_ref(s, r'textbox "[Aa]dd a', must_have='textbox'))
        if not tb:
            print(f"  (no textbox for {filter_label}; could not add {val})")
            continue
        click(tb)
        type_in(tb, val)
        # Wait for autocomplete results to appear
        time.sleep(SETTLE)
        s = snap()
        # First option in the autocomplete; LinkedIn renders these as buttons
        # with text matching the typed value.
        opt = None
        # Best: button containing the exact typed text
        for ref, line in find_all_refs(s, r'button\s+"[^"]*' + re.escape(val) + r'[^"]*"'):
            if 'cursor=pointer' in line:
                opt = ref
                break
        if not opt:
            # Fallback: any listbox option
            opt = find_ref(s, r'option', must_have='cursor=pointer')
        if opt:
            click(opt)
        else:
            print(f"  (no autocomplete option for {val})")
        time.sleep(SETTLE)


def toggle_pinned(label: str) -> None:
    """Toggle a pinned 'Recent updates' filter like 'Posted on LinkedIn'."""
    print(f"\n[*] Toggle pinned filter: {label}")
    s = snap()
    btn = find_ref(s, rf'Select {re.escape(label)} filter')
    if btn:
        click(btn)
    else:
        print(f"  (could not find pinned toggle for {label})")


# --- main flow -----------------------------------------------------------------

def main() -> None:
    print("=== Build Sales Nav targeted search ===\n")

    print("[0] Opening Sales Nav People search...")
    oc("open", "https://www.linkedin.com/sales/search/people")
    time.sleep(PAGE_LOAD)

    # NOTE: We deliberately do NOT add a "Japan/APAC/Asia" keyword filter.
    # Reasons:
    #   - Keyword-matching misses the best targets (founders who haven't
    #     written about Japan yet but would buy if asked)
    #   - It over-matches on noise ("studied abroad in Japan", "love sushi")
    #   - Quoted phrases miss near-equivalent wording
    # Instead we rely on STRUCTURAL signals: industry + title + size + activity.
    # The Sonnet pass during `draft` decides whether the lead has real Japan
    # interest from their actual profile + posts.

    # Step 1: Geography
    add_filter_values("Geography", [
        "United States",
        "United Kingdom",
        "Germany",
        "France",
        "Netherlands",
    ])

    input("\n>>> Geography applied. Press Enter to continue to Industry...")

    # Step 2: Industry — pick a few that map to D2C / health / consumer
    add_filter_values("Industry", [
        "Consumer Services",
        "Health, Wellness & Fitness",
        "Apparel & Fashion",
        "E-Learning",
        "Food & Beverages",
        "Cosmetics",
        "Sporting Goods",
        "Internet",
    ])

    input("\n>>> Industry applied. Press Enter to continue to Seniority + Title...")

    # Step 3: Seniority level — focus on decision-makers
    add_filter_values("Seniority level", [
        "CXO",
        "Owner",
        "Partner",
        "VP",
        "Director",
    ])

    # Step 4: Current job title — narrow further
    add_filter_values("Current job title", [
        "Founder",
        "CEO",
        "COO",
        "President",
        "Head of International",
        "GM International",
        "Head of Growth",
    ])

    input("\n>>> Title applied. Press Enter to continue to Company headcount...")

    # Step 5: Company headcount
    add_filter_values("Company headcount", [
        "51-200",
        "201-500",
        "501-1,000",
    ])

    input("\n>>> Headcount applied. Press Enter to toggle activity filters...")

    # Step 6: Pinned filters in "Recent updates" section
    # - "Posted on LinkedIn": ensures recent_activity material for personalization
    # - "Changed jobs": new CEOs/COOs are prime new-market-decision moments
    toggle_pinned("Posted on LinkedIn")
    toggle_pinned("Changed jobs")

    print("\n=== All filters attempted. ===")
    url = current_url()
    print(f"\nFinal Sales Nav URL:\n{url}\n")
    print("Next steps:")
    print("  1. In the browser, manually click 'Save search' and give it a name")
    print("  2. Then re-grab the URL — it'll have a savedSearchId you can reuse")
    print("  3. Run: .venv/bin/python run.py fetch-leads --search-url '<URL>' --limit 5")


if __name__ == "__main__":
    main()
