"""Shared interactive Approve-phase prompt (pick draft IDs to send)."""

from __future__ import annotations

from typing import Callable


def prompt_send_ids(
    sendable_count: int,
    not_yet_sent: list[int],
    *,
    abort_hint: str = "Aborted. Run `python run.py send --ids ...` later when ready.",
) -> set[int] | None:
    """
    Terminal prompt: all / comma IDs / skip.
    Returns 1-based indices among sendable drafts, or None if aborted.
    """
    if not not_yet_sent:
        print("All sendable drafts are already in sent_history. Nothing to send.")
        return None

    print("\nSend drafts? Pick one:")
    print(
        f"  all  → send all not-yet-sent ({len(not_yet_sent)} drafts: "
        f"{','.join(map(str, not_yet_sent))})"
    )
    print(f"  1,3  → comma-separated draft IDs (1-{sendable_count})")
    print("  n    → skip, exit")
    try:
        ans = input("→ ").strip().lower()
    except EOFError:
        print("\n(no stdin — skipping interactive send)")
        return None

    if not ans or ans in ("n", "no", "skip", "q", "quit"):
        print(abort_hint)
        return None

    if ans in ("all", "y", "yes", "a"):
        ids = set(not_yet_sent)
    else:
        try:
            ids = {int(x.strip()) for x in ans.split(",") if x.strip()}
        except ValueError:
            print(f"Could not parse '{ans}'. Aborted.")
            return None

    valid = {i for i in ids if 1 <= i <= sendable_count}
    if not valid:
        print(f"No valid IDs in {ids}. Aborted.")
        return None
    return valid


def run_after_valid_ids(
    valid: set[int],
    sendable: list,
    on_send: Callable[[set[int]], None],
) -> None:
    chosen_names = [sendable[i - 1].get("name", "?") for i in sorted(valid)]
    print(f"\nSending to: {', '.join(chosen_names)}")
    on_send(valid)
