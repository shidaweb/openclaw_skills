"""Primary-host gating (v20) — make ONE machine the only one allowed to execute
side-effectful sends.

Why: when two machines run the same repo + OpenClaw + Slack (e.g. a Mac Mini for
production and a MacBook Pro for development), a Slack command may be delivered
to either gateway, and a stray run on the dev machine would fire REAL outreach.
The reliable routing fix is to connect only ONE gateway to Slack; this guard is
the belt-and-suspenders that makes the dev machine *refuse to send* even if it
somehow receives a command or someone runs the CLI there by accident.

Set the primary host identically on BOTH machines (pick whichever you have):
  1. env  DOORMAN_PRIMARY_HOST=<hostname>
  2. file data/primary_host   (one line: the hostname)
  3. brief yaml: execution.primary_host

Resolution order is 1 → 2 → 3. When NONE is set the guard is OFF (no
restriction) so existing single-machine setups are unaffected.

Escape hatch for intentional local testing on a non-primary machine:
  DOORMAN_FORCE_SEND=1  (bypasses the guard for that process only)

`hostname()` mirrors helpers.healthcheck (short host, no domain) so the value
you set matches what system_health/<host>.json already uses.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

SKILLS_ROOT = Path(__file__).resolve().parent.parent
PRIMARY_HOST_FILE = SKILLS_ROOT / "data" / "primary_host"


def current_host() -> str:
    """Short hostname (no domain) — same shape as helpers.healthcheck.hostname."""
    return socket.gethostname().split(".")[0]


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def configured_primary_host(config: dict[str, Any] | None = None) -> str:
    """The designated primary host, or "" when unset (guard disabled)."""
    env = os.environ.get("DOORMAN_PRIMARY_HOST", "").strip()
    if env:
        return env
    try:
        if PRIMARY_HOST_FILE.is_file():
            lines = PRIMARY_HOST_FILE.read_text(encoding="utf-8").strip().splitlines()
            if lines and lines[0].strip():
                return lines[0].strip()
    except OSError:
        pass
    exec_cfg = (config or {}).get("execution") or {}
    return str(exec_cfg.get("primary_host") or "").strip()


def force_send_enabled() -> bool:
    return os.environ.get("DOORMAN_FORCE_SEND", "").strip().lower() in ("1", "true", "yes", "on")


def is_send_allowed(
    config: dict[str, Any] | None = None,
    *,
    host: str | None = None,
) -> tuple[bool, str]:
    """(allowed, reason).

    Allowed when: no primary configured (guard off), OR the override is set, OR
    this host IS the primary. Blocked only when a primary is configured and this
    host differs from it.
    """
    primary = configured_primary_host(config)
    if not primary:
        return True, "no_primary_configured"
    if force_send_enabled():
        return True, "force_send_override"
    h = host if host is not None else current_host()
    if _norm(h) == _norm(primary):
        return True, f"host_is_primary:{h}"
    return False, f"not_primary:host={h},primary={primary}"


def _main(argv: list[str] | None = None) -> int:
    """Print this machine's send-eligibility. Exit 0 = may send, 3 = blocked.

    Run on each machine to verify the primary-host routing:
        python3 -m _outreach_core.host_role
    """
    import argparse

    ap = argparse.ArgumentParser(description="Show Doorman send-eligibility for this host.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    host = current_host()
    primary = configured_primary_host()
    allowed, reason = is_send_allowed()
    if args.json:
        import json
        print(json.dumps({"host": host, "primary": primary or None,
                          "send_allowed": allowed, "reason": reason}, ensure_ascii=False))
    else:
        print(f"host          = {host}")
        print(f"primary       = {primary or '(未設定 → ガード無効・全機で送信可)'}")
        verdict = "✅ このマシンは送信担当（実行されます）" if allowed \
            else "⛔ このマシンは送信しません（実行担当ではない）"
        print(f"send_allowed  = {allowed}  {verdict}")
        print(f"reason        = {reason}")
    return 0 if allowed else 3


if __name__ == "__main__":
    raise SystemExit(_main())
