"""v32 FX4 — gateway-outage grace gate (pure decision half).

All briefs on a host share ONE OpenClaw gateway + browser. When the
watchdog force-restarts it (``launchctl kickstart -k``) — or it dies under
10×3 load — every in-flight run's tabs vanish at once. Before this gate,
each remaining target then failed in sequence (``tab not found`` /
``gateway timeout after 25000ms``), producing mass ``lead_crashed`` /
``lead_timed_out`` rows and 94 ``unverified_prior_attempt`` entries in one
production month.

The polling side effects live in jp-form-outreach/run.py
(``_wait_for_gateway``); this module owns the deterministic parts so the
signature matching and wait policy are unit-testable.

Exit-code contract: a run that gives up waiting exits
``EXIT_GATEWAY_UNAVAILABLE`` (5). The supervisor treats 5 as
terminal-without-retry — relaunching run.py cannot revive the gateway
(that is the watchdog's job), and burning the crash budget here would
exhaust it for real crashes.
"""

from __future__ import annotations

import os

# Casefolded substring signatures, confirmed against production job logs:
#   GatewayClientRequestError: tab not found: browser tab "254DF840…"
#   GatewayTransportError: gateway timeout after 25000ms
#   Error: Page closed before browser action completed.
#   browserType.connectOverCDP: Timeout 9000ms exceeded
GATEWAY_ERROR_SIGNATURES = (
    "tab not found",
    "gateway timeout after",
    "page closed before browser action",
    "connectovercdp",
    "gatewaytransporterror",
    "gatewayclientrequesterror",
    "gateway not running",
    "econnrefused",
)

# Worst-case honest recovery of the shared gateway: the watchdog's restart
# budget (3/10min) can burn in ~3 minutes, then it marks the gateway
# "abandoned" for a 30-minute cooldown before the next kickstart — ≈34
# minutes end-to-end. 45 minutes covers that with margin while still
# failing a truly dead gateway within the hour.
WAIT_SEC_DEFAULT = 2700
POLL_SEC_DEFAULT = 15

EXIT_GATEWAY_UNAVAILABLE = 5


class GatewayUnavailableError(RuntimeError):
    """Raised when the gateway stayed down past the wait budget."""


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, str(default)))
        return v if v > 0 else default
    except (ValueError, TypeError):
        return default


def wait_sec() -> int:
    return _env_int("DOORMAN_GATEWAY_WAIT_SEC", WAIT_SEC_DEFAULT)


def poll_sec() -> int:
    return _env_int("DOORMAN_GATEWAY_POLL_SEC", POLL_SEC_DEFAULT)


def is_gateway_error_text(text: object) -> bool:
    """Does this error text look like a gateway/browser-transport failure?

    Pure, casefolded substring match against the production-confirmed
    signature list. Used to route a crashed lead into the recovery wait
    instead of letting one gateway blip fail every remaining target.
    """
    hay = str(text or "").casefold()
    if not hay:
        return False
    return any(sig in hay for sig in GATEWAY_ERROR_SIGNATURES)


def should_keep_waiting(
    started_ts: float,
    now_ts: float,
    limit_sec: float | None = None,
) -> bool:
    """True while the wait budget has not been exhausted (pure)."""
    limit = wait_sec() if limit_sec is None else float(limit_sec)
    try:
        elapsed = max(0.0, float(now_ts) - float(started_ts))
    except (TypeError, ValueError):
        return False
    return elapsed < limit
