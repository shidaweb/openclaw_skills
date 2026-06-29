"""Multi-step form wizard state machine — pure helpers (v30 §WS-A).

The send loop in ``jp-form-outreach/run.py`` traverses an input → (validation) →
confirm → done state machine, tracking several ad-hoc counters
(``no_progress``, ``validation_rounds``, ``clicks``, ``last_fp``) inline. Three
production failure modes were not caught by those counters in isolation:

  * Fujisoft 2026-06-29: same "次へ" button clicked 3 times with no progress —
    the wizard kept clicking because each click slightly changed the page
    fingerprint (validation error flicker), so ``no_progress`` never reached 2.
  * SUPER STUDIO 2026-06-27: same "内容確認へ" clicked 3 times — same shape.
  * MIL 2026-06-27: same "送信する" clicked 3 times in the resolver pass.

This module exposes:

  * :class:`WizardConfig` — limits (``max_hops``, ``max_same_button_clicks``,
    ``max_no_progress``, ``max_validation_rounds``). Defaults match the
    historical tolerance so introducing the gate doesn't tighten behaviour for
    well-behaved forms.
  * :class:`WizardState` — counters mutated by :func:`bump_after_observation`.
  * :class:`StuckReason` — terminal verdict.
  * :func:`compute_stuck_reason` — pure check: state + config → stuck reason or
    ``None``. The send loop calls this each iteration as an additional exit.
  * :func:`bump_after_observation` — pure update applied per iteration.
  * :func:`next_phase` — pure resolver for the next click phase.

All functions are dependency-free for unit testing without a browser.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

# Phase the wizard is about to act on (which button cascade to fire).
PHASE_FIRST = "first"
PHASE_FINAL = "final"

# Internal stuck reason codes — distinct so the controller can distinguish
# "same button N times" (button-loop) from "fingerprint unchanged" (effect-loop)
# from "validation can't be fixed". Each maps to the historical ``reason_class``
# string via :func:`reason_class_for` so report dashboards stay coherent.
REASON_MAX_HOPS = "max_hops"
REASON_SAME_BUTTON = "same_button_repeated"
REASON_NO_PROGRESS = "no_observable_progress"
REASON_VALIDATION_UNRECOVERABLE = "validation_unrecoverable"

# Mapping to the historical reason_class strings written to
# needs_attention.jsonl. Keep new wizard codes routed to the existing report
# buckets to avoid breaking `./report needs-attention` dashboards.
_REASON_CLASS_BY_CODE = {
    REASON_MAX_HOPS: "wizard_too_deep",
    REASON_SAME_BUTTON: "submit_click_ineffective",
    REASON_NO_PROGRESS: "submit_click_ineffective",
    REASON_VALIDATION_UNRECOVERABLE: "validation_unrecoverable",
}


def reason_class_for(code: str) -> str:
    """Map a wizard reason code to the legacy ``reason_class`` written to the
    needs_attention queue. Unknown codes pass through unchanged."""
    return _REASON_CLASS_BY_CODE.get(code, code)


@dataclass(frozen=True)
class WizardConfig:
    """Tunable limits for the wizard's stuck detection.

    Defaults match the historical run-loop tolerances. They are intentionally
    permissive: most real forms transition cleanly in one or two hops, and the
    point of these gates is to STOP the rare pathological cases rather than to
    tighten the happy path.
    """

    # Total state-machine iterations before we give up. A confirm flow uses 2
    # hops (input → confirm → done); a single-page flow uses 1. We leave
    # headroom for one re-click after a transient validation rescue.
    max_hops: int = 4

    # Consecutive clicks on the same button text. After this many clicks
    # without a phase change, the wizard is wedged: clicking again is unlikely
    # to help and risks duplicate-send anxiety.
    max_same_button_clicks: int = 3

    # Consecutive observations with the same page fingerprint (i.e. nothing
    # visibly changed). A silent re-render counts as one; two means the click
    # had no effect.
    max_no_progress: int = 2

    # Times the page returns to ``validation_error`` with our auto-fix failing
    # to lower the count. After this many we escalate rather than thrash.
    max_validation_rounds: int = 2


@dataclass
class WizardState:
    """Mutable counters tracked across one target's wizard run.

    Update via :func:`bump_after_observation` and :func:`record_click`. Direct
    field mutation is supported (the send loop edits some fields inline) but
    the helpers are preferred for testability.
    """

    hop: int = 0
    no_progress: int = 0
    validation_rounds: int = 0
    last_fingerprint: Optional[str] = None
    last_button_text: Optional[str] = None
    same_button_count: int = 0
    confirm_seen: bool = False
    last_observation_state: Optional[str] = None

    def snapshot(self) -> "WizardState":
        """Defensive copy for tests / event payloads."""
        return replace(self)


@dataclass(frozen=True)
class StuckReason:
    """Terminal verdict from :func:`compute_stuck_reason`."""

    code: str
    detail: str

    def as_payload(self) -> dict[str, str]:
        return {"reason": self.code, "detail": self.detail}


def bump_after_observation(
    state: WizardState,
    *,
    observation_state: str,
    fingerprint: Optional[str],
) -> WizardState:
    """Update counters from a fresh page observation. Returns the same state
    object for chaining; callers may also rely on in-place mutation.

    ``observation_state`` is the live page verdict (``input`` |
    ``validation_error`` | ``confirm`` | ``done`` | ``no_form``). The hop count
    advances on every observation past the first; the fingerprint is compared
    to the previous one to bump ``no_progress``; a ``validation_error`` state
    bumps ``validation_rounds``.
    """

    state.hop += 1
    if observation_state == "validation_error":
        state.validation_rounds += 1
    if fingerprint is not None:
        if state.last_fingerprint is not None and fingerprint == state.last_fingerprint:
            state.no_progress += 1
        else:
            state.no_progress = 0
        state.last_fingerprint = fingerprint
    # v30 §WS-A — the same-button streak is cleared ONLY when the observation
    # state truly transitions (e.g. input → confirm or input → done). It is
    # deliberately NOT cleared on a mere fingerprint flicker: the production
    # Fujisoft / SUPER STUDIO bugs were exactly the case where the page
    # fingerprint shifted (validation error rendered, server-side echo) but
    # the state stayed "input" — clearing the streak there would defeat the
    # stuck guard.
    prev = state.last_observation_state
    if (
        observation_state
        and prev is not None
        and observation_state != prev
        and observation_state not in ("no_form",)
    ):
        state.same_button_count = 0
    state.last_observation_state = observation_state
    return state


def record_click(state: WizardState, button_text: Optional[str]) -> WizardState:
    """Track which submit button we just clicked. Repeated clicks on the SAME
    button text (after normalisation) increment ``same_button_count``."""

    norm = _normalise_button(button_text)
    if norm and norm == state.last_button_text:
        state.same_button_count += 1
    else:
        state.same_button_count = 1 if norm else 0
        state.last_button_text = norm
    return state


def reset_progress_counters(state: WizardState) -> WizardState:
    """Called when the page TRULY transitioned (state machine made progress).
    The send loop has historically reset ``no_progress`` to 0 on fingerprint
    change; clearing ``same_button_count`` here mirrors that semantics so a
    legitimate phase change doesn't carry stale stuck signals into the next
    phase.
    """
    state.same_button_count = 0
    state.no_progress = 0
    return state


def compute_stuck_reason(
    state: WizardState, config: WizardConfig
) -> Optional[StuckReason]:
    """Return a :class:`StuckReason` when the wizard should stop, else ``None``.

    Checked in priority order:

      1. ``validation_rounds`` exceeded — auto-fix isn't working.
      2. ``same_button_count`` exceeded — same button clicked too many times.
      3. ``no_progress`` exceeded — fingerprint stuck for too many observations.
      4. ``hop`` exceeded — total iteration cap.

    Order matters because the most specific reason gives the best Slack
    message; falling back to ``max_hops`` for a problem caused by a wedged
    button would lose the diagnostic.
    """

    if state.validation_rounds > config.max_validation_rounds:
        return StuckReason(
            code=REASON_VALIDATION_UNRECOVERABLE,
            detail=(
                f"validation_error returned {state.validation_rounds}x; "
                f"auto-fix could not lower the count "
                f"(limit={config.max_validation_rounds})"
            ),
        )
    if state.same_button_count >= config.max_same_button_clicks:
        return StuckReason(
            code=REASON_SAME_BUTTON,
            detail=(
                f"button {state.last_button_text!r} clicked "
                f"{state.same_button_count}x in a row without phase change "
                f"(limit={config.max_same_button_clicks})"
            ),
        )
    if state.no_progress >= config.max_no_progress:
        return StuckReason(
            code=REASON_NO_PROGRESS,
            detail=(
                f"page fingerprint unchanged for {state.no_progress} "
                f"consecutive observations (limit={config.max_no_progress})"
            ),
        )
    if state.hop > config.max_hops:
        return StuckReason(
            code=REASON_MAX_HOPS,
            detail=(
                f"wizard exceeded {config.max_hops} hops without reaching "
                f"a terminal state"
            ),
        )
    return None


def next_phase(
    *,
    flow: str,
    observation_state: str,
    confirm_seen: bool,
    has_visible_textarea: bool,
) -> str:
    """Pure resolver for the click phase to use on the upcoming click.

    Rules (mirror the historical run.py logic at the time of extraction):

      * ``single`` flow → always ``final``.
      * ``confirm`` flow on a ``validation_error`` state → ``first`` if a
        textarea is still visible (we're still on input), else ``final``.
      * ``confirm`` flow on a ``confirm`` observation → ``final``.
      * ``confirm`` flow on ``input`` → ``first`` until we've seen the confirm
        page once; after that ``final`` (re-entering input mid-flight is rare
        and usually means a validation bounce).
    """

    if flow != "confirm":
        return PHASE_FINAL
    if observation_state == "confirm":
        return PHASE_FINAL
    if observation_state == "validation_error":
        return PHASE_FIRST if has_visible_textarea else PHASE_FINAL
    if confirm_seen:
        return PHASE_FINAL
    return PHASE_FIRST


def _normalise_button(text: Optional[str]) -> Optional[str]:
    """Collapse whitespace and trim so 「次へ」 / 「次へ 」 / 「次 へ」 compare
    equal. Returns ``None`` for empty input so an unidentified click does not
    accidentally extend a same-button streak."""
    if text is None:
        return None
    cleaned = " ".join(str(text).split()).strip()
    return cleaned or None
