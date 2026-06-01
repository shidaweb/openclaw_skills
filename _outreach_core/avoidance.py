"""
reCAPTCHA avoidance engine (v6 §3.5) — form channel only.

Full-autonomy stance: we **do not solve** a shown challenge. We pour effort into
*not triggering one*, and we *learn* which domains are unwinnable so we stop
wasting attempts (and accumulating low scores) on them.

Three pure, testable concerns live here:

  1. **Domain learning** — per-domain outcome stats (sent / captcha_blocked /
     submit_not_found …). A domain that keeps showing a *visible* challenge after
     warmup is marked ``unviable`` → the send flow routes it elsewhere or skips.
  2. **Adaptive warmup** — a domain that was challenged before gets a longer warm
     dwell on the next attempt (more engagement = higher v3 score).
  3. **Human-like pacing** — typing/inter-action delay ranges and a send window,
     so automated fills look like a real low-volume sender (which, on-device, is
     exactly what they are).

The actual browser steps live in ``warmup.py`` / ``run.py``; this module decides
*how long*, *how fast*, and *whether to bother*. No LLM, no network.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MIN_ATTEMPTS = 3        # need at least this many tries before judging a domain
DEFAULT_MIN_CAPTCHA_BLOCKS = 2  # ...and at least this many captcha blocks
DEFAULT_CAPTCHA_RATE = 0.6      # ...and this share of attempts blocked → unviable
DEFAULT_BASE_WARMUP_SEC = 20
DEFAULT_MAX_WARMUP_SEC = 90
DEFAULT_WARMUP_BUMP_SEC = 25    # added per prior captcha block on the domain
DEFAULT_TYPING_DELAY_MS = (40, 140)   # per-character think time
DEFAULT_FIELD_PAUSE_MS = (350, 1200)  # pause between fields
DEFAULT_SEND_WINDOW = (9, 19)   # business hours (local), inclusive-exclusive

_STATS_FILENAME = "avoidance_stats.json"

# Outcomes recorded per attempt.
OUTCOME_SENT = "sent"
OUTCOME_CAPTCHA_BLOCKED = "captcha_blocked"
OUTCOME_SUBMIT_NOT_FOUND = "submit_not_found"
OUTCOME_WRONG_FORM = "wrong_form"
OUTCOME_CONTENT_REJECTED = "content_rejected"
OUTCOME_SKIPPED = "skipped"
OUTCOME_ERROR = "error"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def avoidance_config(config: dict[str, Any] | None) -> dict[str, Any]:
    block = {}
    if isinstance(config, dict):
        raw = config.get("avoidance")
        if isinstance(raw, dict):
            block = raw
    learn = block.get("learning") if isinstance(block.get("learning"), dict) else {}
    warm = block.get("warmup") if isinstance(block.get("warmup"), dict) else {}
    pace = block.get("pacing") if isinstance(block.get("pacing"), dict) else {}
    return {
        "enabled": _as_bool(block.get("enabled"), True),
        "learning": {
            "min_attempts": _as_int(learn.get("min_attempts"), DEFAULT_MIN_ATTEMPTS),
            "min_captcha_blocks": _as_int(learn.get("min_captcha_blocks"), DEFAULT_MIN_CAPTCHA_BLOCKS),
            "captcha_rate": _as_float(learn.get("captcha_rate"), DEFAULT_CAPTCHA_RATE),
        },
        "warmup": {
            "base_sec": _as_int(warm.get("base_sec"), DEFAULT_BASE_WARMUP_SEC),
            "max_sec": _as_int(warm.get("max_sec"), DEFAULT_MAX_WARMUP_SEC),
            "bump_sec": _as_int(warm.get("bump_sec"), DEFAULT_WARMUP_BUMP_SEC),
        },
        "pacing": {
            "typing_delay_ms": _as_pair(pace.get("typing_delay_ms"), DEFAULT_TYPING_DELAY_MS),
            "field_pause_ms": _as_pair(pace.get("field_pause_ms"), DEFAULT_FIELD_PAUSE_MS),
            "send_window": _as_pair(pace.get("send_window"), DEFAULT_SEND_WINDOW),
        },
    }


# ---------------------------------------------------------------------------
# Domain keying
# ---------------------------------------------------------------------------

def domain_of(url: str | None) -> str:
    """Registrable-ish host key (lowercased, leading 'www.' stripped)."""
    if not url:
        return ""
    try:
        host = urlparse(url if "://" in url else f"https://{url}").hostname or ""
    except (ValueError, TypeError):
        return ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


# ---------------------------------------------------------------------------
# Stats persistence + learning
# ---------------------------------------------------------------------------

def stats_path(data_dir: Path) -> Path:
    return Path(data_dir) / _STATS_FILENAME


def read_stats(data_dir: Path) -> dict[str, Any]:
    path = stats_path(data_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _write_stats(data_dir: Path, stats: dict[str, Any]) -> None:
    path = stats_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")


def _blank_domain() -> dict[str, Any]:
    return {
        "attempts": 0,
        "sent": 0,
        "captcha_blocked": 0,
        "submit_not_found": 0,
        "wrong_form": 0,
        "content_rejected": 0,
        "error": 0,
        "skipped": 0,
        "url_unfriendly": False,
        "last_outcome": None,
        "last_at": None,
    }


def record_outcome(data_dir: Path, url_or_domain: str, outcome: str) -> dict[str, Any]:
    """Record one attempt outcome for a domain. Returns the domain's row."""
    domain = domain_of(url_or_domain) or (url_or_domain or "").strip().lower()
    if not domain:
        return _blank_domain()
    stats = read_stats(data_dir)
    row = stats.get(domain) or _blank_domain()
    # 'skipped' is a routing decision, not a delivery attempt → don't inflate attempts.
    if outcome != OUTCOME_SKIPPED:
        row["attempts"] = int(row.get("attempts", 0)) + 1
    if outcome in row:
        row[outcome] = int(row.get(outcome, 0)) + 1
    row["last_outcome"] = outcome
    row["last_at"] = datetime.now(timezone.utc).isoformat()
    stats[domain] = row
    _write_stats(data_dir, stats)
    return row


def domain_status(data_dir: Path, url_or_domain: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verdict for a domain: is it captcha-unviable, and why."""
    cfg = avoidance_config(config)["learning"]
    domain = domain_of(url_or_domain) or (url_or_domain or "").strip().lower()
    row = read_stats(data_dir).get(domain) or _blank_domain()
    attempts = int(row.get("attempts", 0))
    blocks = int(row.get("captcha_blocked", 0))
    rate = (blocks / attempts) if attempts else 0.0
    unviable = (
        attempts >= cfg["min_attempts"]
        and blocks >= cfg["min_captcha_blocks"]
        and rate >= cfg["captcha_rate"]
    )
    return {
        "domain": domain,
        "attempts": attempts,
        "captcha_blocks": blocks,
        "captcha_rate": round(rate, 3),
        "unviable": unviable,
        "last_outcome": row.get("last_outcome"),
    }


# ---------------------------------------------------------------------------
# URL-unfriendly domain learning (§3.6) — forms that reject URLs in the body
# ---------------------------------------------------------------------------

def mark_url_unfriendly(data_dir: Path, url_or_domain: str) -> None:
    """Remember that this domain rejects URLs in the inquiry body, so future
    sends pre-strip the URL instead of wasting a first attempt."""
    domain = domain_of(url_or_domain) or (url_or_domain or "").strip().lower()
    if not domain:
        return
    stats = read_stats(data_dir)
    row = stats.get(domain) or _blank_domain()
    row["url_unfriendly"] = True
    stats[domain] = row
    _write_stats(data_dir, stats)


def is_url_unfriendly(data_dir: Path, url_or_domain: str) -> bool:
    domain = domain_of(url_or_domain) or (url_or_domain or "").strip().lower()
    row = read_stats(data_dir).get(domain) or {}
    return bool(row.get("url_unfriendly"))


# ---------------------------------------------------------------------------
# Adaptive warmup
# ---------------------------------------------------------------------------

def recommended_warmup_sec(data_dir: Path, url_or_domain: str, config: dict[str, Any] | None = None) -> int:
    """Base warmup, bumped for each prior captcha block on this domain (capped)."""
    warm = avoidance_config(config)["warmup"]
    row = read_stats(data_dir).get(domain_of(url_or_domain) or "") or {}
    blocks = int(row.get("captcha_blocked", 0))
    sec = warm["base_sec"] + warm["bump_sec"] * blocks
    return max(0, min(warm["max_sec"], sec))


# ---------------------------------------------------------------------------
# Human-like pacing
# ---------------------------------------------------------------------------

def typing_delay_ms(config: dict[str, Any] | None = None) -> int:
    lo, hi = avoidance_config(config)["pacing"]["typing_delay_ms"]
    return random.randint(int(lo), int(max(lo, hi)))


def field_pause_ms(config: dict[str, Any] | None = None) -> int:
    lo, hi = avoidance_config(config)["pacing"]["field_pause_ms"]
    return random.randint(int(lo), int(max(lo, hi)))


def within_send_window(config: dict[str, Any] | None = None, now: datetime | None = None) -> bool:
    """True if the local hour is inside the configured business-hours window.
    A window of (0, 24) (or start>=end misconfig) means 'always'."""
    start, end = avoidance_config(config)["pacing"]["send_window"]
    start, end = int(start), int(end)
    if start <= 0 and end >= 24:
        return True
    if start >= end:
        return True  # misconfigured → don't block sending
    hour = (now or datetime.now()).hour
    return start <= hour < end


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _as_bool(val: Any, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(val)


def _as_int(val: Any, default: int) -> int:
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _as_float(val: Any, default: float) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _as_pair(val: Any, default: tuple) -> tuple:
    if isinstance(val, (list, tuple)) and len(val) == 2:
        try:
            return (type(default[0])(val[0]), type(default[1])(val[1]))
        except (ValueError, TypeError):
            return default
    return default
