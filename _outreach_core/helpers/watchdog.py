#!/usr/bin/env python3
"""External watchdog for the OpenClaw gateway (v4 §15-C, hardened in v14).

Design (v14 §0): **never fork/patch OpenClaw**. Treat the gateway as a black
box and supervise it from the OS (launchd) side. This watchdog:

- §W1 **dead → always (re)start**: if launchd is not managing the gateway (or
  the process is gone), run the configured ``start_cmd``; if it is loaded but
  *hung*, force ``restart_cmd`` (``launchctl kickstart -k``). Both are rate
  limited by a window/cap so we never restart-loop.
- §W2 **sleep/wake recovery**: if a tick arrives far later than the interval
  (laptop was asleep), take a deeper recovery path on wake.
- §W3 **self-heal**: re-load the watchdog's own launchd job if it dropped, and
  re-assert the gateway's ``KeepAlive`` if it got turned off.
- §W4 **deadman's switch**: if the gateway can't be recovered for N minutes,
  escalate to a human via Slack (and an opt-in, PII-free vendor liveness ping).
- §W5 **config-driven**: label and health/start/restart commands come from
  ``gateway_config`` — nothing hardcoded.

The decision logic is split into pure functions (``decide_action``,
``detect_wake``, ``should_escalate``, ``vendor_ping_payload`` …) that are unit
tested without touching the OS.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import gateway_config as gwcfg
from _outreach_core.config import SKILLS_ROOT
from _outreach_core.helpers.healthcheck import (
    collect_active_runs,
    heartbeat_age_seconds,
    read_health,
)

# Retained as fallback defaults; live values come from gateway_config tuning.
HEALTH_TIMEOUT_SEC = 20
HEALTH_FAIL_RESTART_THRESHOLD = 2  # consecutive failed probes before restart (~2 min)
CHANNEL_FAIL_RESTART_THRESHOLD = 5  # consecutive ticks with a stuck channel before restart
STALE_HEARTBEAT_SEC = 300  # 5 minutes
RESTART_WINDOW_MIN = 10
MAX_RESTARTS = 3
ABANDON_COOLDOWN_MIN = 30
ABANDON_NOTIFY_INTERVAL_SEC = 300
WATCHDOG_LABEL = "com.doorman.watchdog"
WATCHDOG_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.doorman.watchdog.plist"
OPENCLAW_AGENT_TIMEOUT_SECONDS = 1800
OPENCLAW_CLI_NO_OUTPUT_TIMEOUT_MS = 900_000
OPENCLAW_HEARTBEAT_EVERY = "10m"
OPENCLAW_HEARTBEAT_TIMEOUT_SECONDS = 600
OPENCLAW_CLI_BACKEND_ID = "claude-cli"
OPENCLAW_HEARTBEAT_PROMPT = (
    "現在の作業状況を日本語で1-2行だけ簡潔に報告してください。"
    "実行中の作業がなければ「待機中です」とだけ返してください。"
)
_OPENCLAW_CLI_TIMEOUT_PATTERNS = (
    "cli watchdog timeout",
    "cli subprocess: timed out",
    "no-output stall",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(ts: str) -> datetime | None:
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def watchdog_state_path(skills_root: Path | None = None) -> Path:
    root = skills_root or SKILLS_ROOT
    return root / "data" / "watchdog.state.json"


def watchdog_log_path(skills_root: Path | None = None) -> Path:
    root = skills_root or SKILLS_ROOT
    return root / "data" / "watchdog.log"


def read_state(skills_root: Path | None = None) -> dict[str, Any]:
    path = watchdog_state_path(skills_root)
    if not path.is_file():
        return {"restart_attempts": [], "abandoned_until": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("restart_attempts", [])
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"restart_attempts": [], "abandoned_until": None}


def save_state(state: dict[str, Any], skills_root: Path | None = None) -> None:
    """Persist state atomically (temp + os.replace) so a crash or power loss
    mid-write can never leave a corrupt state file that disables the watchdog."""
    path = watchdog_state_path(skills_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def openclaw_config_path() -> Path:
    return Path(os.environ.get("OPENCLAW_CONFIG", Path.home() / ".openclaw" / "openclaw.json")).expanduser()


def required_cli_backend_config(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a conservative claude-cli config with a longer no-output watchdog.

    Existing non-watchdog fields are preserved where present so local runtime
    customizations are not stomped; missing fields are filled with the known
    stream-json Claude CLI contract OpenClaw already uses.
    """
    backend = dict(existing or {})
    backend.setdefault("command", "claude")
    backend.setdefault("args", ["--print", "--output-format", "stream-json", "--verbose"])
    backend.setdefault("output", "jsonl")
    backend.setdefault("resumeOutput", "jsonl")
    backend.setdefault("jsonlDialect", "claude-stream-json")
    backend.setdefault("liveSession", "claude-stdio")
    backend.setdefault("input", "stdin")
    backend.setdefault("modelArg", "--model")
    backend.setdefault("sessionArg", "--resume")
    backend.setdefault("sessionMode", "existing")
    reliability = backend.setdefault("reliability", {})
    if not isinstance(reliability, dict):
        reliability = {}
        backend["reliability"] = reliability
    watchdog = reliability.setdefault("watchdog", {})
    if not isinstance(watchdog, dict):
        watchdog = {}
        reliability["watchdog"] = watchdog
    for mode in ("fresh", "resume"):
        block = watchdog.setdefault(mode, {})
        if not isinstance(block, dict):
            block = {}
            watchdog[mode] = block
        try:
            current = int(block.get("noOutputTimeoutMs", 0))
        except (TypeError, ValueError):
            current = 0
        if current < OPENCLAW_CLI_NO_OUTPUT_TIMEOUT_MS:
            block["noOutputTimeoutMs"] = OPENCLAW_CLI_NO_OUTPUT_TIMEOUT_MS
    return backend


def openclaw_runtime_patch_needed(cfg: dict[str, Any]) -> bool:
    defaults = ((cfg.get("agents") or {}).get("defaults") or {})
    try:
        if int(defaults.get("timeoutSeconds", 0)) < OPENCLAW_AGENT_TIMEOUT_SECONDS:
            return True
    except (TypeError, ValueError):
        return True
    backend = ((defaults.get("cliBackends") or {}).get(OPENCLAW_CLI_BACKEND_ID) or {})
    required = required_cli_backend_config(backend if isinstance(backend, dict) else {})
    if backend != required:
        return True
    hb = defaults.get("heartbeat") or {}
    if not isinstance(hb, dict):
        return True
    if hb.get("every") != OPENCLAW_HEARTBEAT_EVERY:
        return True
    try:
        if int(hb.get("timeoutSeconds", 0)) < OPENCLAW_HEARTBEAT_TIMEOUT_SECONDS:
            return True
    except (TypeError, ValueError):
        return True
    if int(hb.get("ackMaxChars", 0) or 0) < 180:
        return True
    if not str(hb.get("prompt") or "").strip():
        return True
    return False


def apply_openclaw_runtime_patch(path: Path | None = None) -> bool:
    """Self-heal OpenClaw runtime settings that prevent long turns from living.

    Returns True only when the config was changed. Invalid/missing configs are
    left alone so the watchdog never replaces a human-owned file with guesses.
    """
    cfg_path = path or openclaw_config_path()
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict) or not openclaw_runtime_patch_needed(data):
        return False
    agents = data.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = {}
        data["agents"] = agents
    defaults = agents.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}
        agents["defaults"] = defaults
    try:
        current_timeout = int(defaults.get("timeoutSeconds", 0))
    except (TypeError, ValueError):
        current_timeout = 0
    if current_timeout < OPENCLAW_AGENT_TIMEOUT_SECONDS:
        defaults["timeoutSeconds"] = OPENCLAW_AGENT_TIMEOUT_SECONDS
    cli_backends = defaults.setdefault("cliBackends", {})
    if not isinstance(cli_backends, dict):
        cli_backends = {}
        defaults["cliBackends"] = cli_backends
    existing_backend = cli_backends.get(OPENCLAW_CLI_BACKEND_ID)
    cli_backends[OPENCLAW_CLI_BACKEND_ID] = required_cli_backend_config(
        existing_backend if isinstance(existing_backend, dict) else {}
    )
    heartbeat = defaults.setdefault("heartbeat", {})
    if not isinstance(heartbeat, dict):
        heartbeat = {}
        defaults["heartbeat"] = heartbeat
    heartbeat["every"] = OPENCLAW_HEARTBEAT_EVERY
    try:
        hb_timeout = int(heartbeat.get("timeoutSeconds", 0))
    except (TypeError, ValueError):
        hb_timeout = 0
    if hb_timeout < OPENCLAW_HEARTBEAT_TIMEOUT_SECONDS:
        heartbeat["timeoutSeconds"] = OPENCLAW_HEARTBEAT_TIMEOUT_SECONDS
    if int(heartbeat.get("ackMaxChars", 0) or 0) < 180:
        heartbeat["ackMaxChars"] = 180
    heartbeat.setdefault("includeReasoning", False)
    heartbeat.setdefault("prompt", OPENCLAW_HEARTBEAT_PROMPT)
    tmp = cfg_path.with_name(cfg_path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, cfg_path)
    return True


def _openclaw_log_candidates() -> list[Path]:
    candidates: list[Path] = []
    tmp_dir = Path(os.environ.get("OPENCLAW_LOG_DIR", "/tmp/openclaw")).expanduser()
    try:
        candidates.extend(sorted(tmp_dir.glob("openclaw-*.log"), key=lambda p: p.stat().st_mtime, reverse=True))
    except OSError:
        pass
    candidates.append(Path.home() / ".openclaw" / "logs" / "gateway.err.log")
    return candidates


def _tail_lines(path: Path, max_bytes: int = 512_000) -> list[str]:
    try:
        with path.open("rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes), os.SEEK_SET)
            except OSError:
                pass
            return f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _line_timestamp(line: str, path: Path) -> datetime | None:
    try:
        data = json.loads(line)
        if isinstance(data, dict):
            ts = data.get("time") or (data.get("_meta") or {}).get("date")
            if ts:
                return _parse_ts(str(ts))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    head = line.split(" ", 1)[0]
    if head.startswith("20"):
        parsed = _parse_ts(head)
        if parsed:
            return parsed
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None


def _is_cli_timeout_line(line: str) -> bool:
    lower = line.lower()
    return any(pattern in lower for pattern in _OPENCLAW_CLI_TIMEOUT_PATTERNS)


def latest_cli_backend_timeout(
    *,
    log_paths: list[Path] | None = None,
    now_epoch: float | None = None,
    lookback_sec: int = 900,
) -> dict[str, Any] | None:
    """Return the newest recent OpenClaw CLI no-output timeout, if any."""
    now = now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp()
    latest: dict[str, Any] | None = None
    for path in log_paths or _openclaw_log_candidates():
        for line in _tail_lines(path):
            if not _is_cli_timeout_line(line):
                continue
            ts = _line_timestamp(line, path)
            if not ts:
                continue
            epoch = ts.astimezone(timezone.utc).timestamp()
            if now - epoch > lookback_sec:
                continue
            item = {
                "epoch": epoch,
                "ts": ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "path": str(path),
                "marker": f"{path}:{int(epoch)}:{line[:180]}",
            }
            if latest is None or item["epoch"] > latest["epoch"]:
                latest = item
    return latest


def _run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """subprocess.run with binary resolution + PATH-augmented env (§ launchd PATH).

    Ensures ``openclaw`` / ``launchctl`` are found even when launchd hands us its
    minimal default PATH, which otherwise makes every probe silently no-op.
    """
    return subprocess.run(gwcfg.resolve_argv(argv), env=gwcfg.augmented_env(), **kwargs)


def append_log(message: str, skills_root: Path | None = None) -> None:
    path = watchdog_log_path(skills_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{_utc_now()} {message}\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


# --------------------------------------------------------------------------- #
# Pure decision functions (unit-tested, no OS side effects).                    #
# --------------------------------------------------------------------------- #
def decide_action(
    *,
    loaded: bool,
    healthy: bool | None,
    health_fail_streak: int,
    health_fail_threshold: int,
    can_restart: bool,
    woke_from_sleep: bool = False,
) -> str:
    """Decide what to do this tick (pure). Returns one of:

    - ``"start"``     gateway not registered/dead → run ``start_cmd``
    - ``"kickstart"`` loaded but hung → force ``restart_cmd``
    - ``"wait"``      unhealthy but still under the failure threshold
    - ``"abandoned"`` out of restart budget → escalate, don't act
    - ``"ok"``        healthy (or no probe evidence)

    ``health_fail_streak`` is the *already-incremented* consecutive failure
    count for this tick. On wake we skip the patient ``wait`` and act now.
    """
    if not loaded:
        return "start" if can_restart else "abandoned"
    if healthy or healthy is None:
        return "ok"
    if not woke_from_sleep and health_fail_streak < health_fail_threshold:
        return "wait"
    return "kickstart" if can_restart else "abandoned"


def detect_wake(
    *,
    now_epoch: float,
    last_tick_epoch: float | None,
    interval_sec: int,
    factor: int = 3,
) -> bool:
    """True when this tick arrived ``> interval_sec * factor`` after the last —
    i.e. launchd ticks were skipped because the machine was asleep."""
    if last_tick_epoch is None or interval_sec <= 0:
        return False
    return (now_epoch - last_tick_epoch) > interval_sec * max(1, factor)


def should_escalate(
    *,
    abandoned: bool,
    last_ok_epoch: float | None,
    now_epoch: float,
    dead_alert_min: int,
    last_escalation_epoch: float | None,
    renotify_sec: int,
) -> bool:
    """Deadman's switch (pure): escalate when recovery has failed (``abandoned``)
    and the gateway has not been healthy for ``dead_alert_min`` minutes, with a
    re-notify floor so we don't spam."""
    if not abandoned:
        return False
    if last_ok_epoch is not None and (now_epoch - last_ok_epoch) < dead_alert_min * 60:
        return False
    if last_escalation_epoch is None:
        return True
    return (now_epoch - last_escalation_epoch) >= renotify_sec


def vendor_ping_payload(*, install_id: str, status: str, now_iso: str) -> dict[str, Any]:
    """Build the opt-in vendor liveness ping body. Liveness only — **no PII**,
    no hostname, no user, no outreach data; just an opaque install id + status."""
    return {
        "install_id": str(install_id),
        "status": str(status),
        "ts": str(now_iso),
        "schema": "doorman.liveness.v1",
    }


def should_reload_watchdog(loaded: bool) -> bool:
    return not loaded


def should_reassert_keepalive(keepalive: bool | None) -> bool:
    return keepalive is False


def watchdog_liveness(
    last_tick_epoch: float | None,
    now_epoch: float,
    interval_sec: int,
    factor: int = 4,
) -> str:
    """Is the watchdog itself still ticking? (pure)

    Returns ``"ok"`` when the last tick is recent, ``"stale"`` when it is older
    than ``interval_sec * factor`` (the watchdog job likely died/unloaded), or
    ``"unknown"`` when no tick has ever been recorded."""
    if last_tick_epoch is None or interval_sec <= 0:
        return "unknown"
    return "ok" if (now_epoch - last_tick_epoch) <= interval_sec * max(1, factor) else "stale"


def overall_status(summary: dict[str, Any]) -> str:
    """Roll a status summary up into ``ok`` | ``degraded`` | ``down`` (pure)."""
    if not summary.get("gateway_loaded"):
        return "down"
    if summary.get("abandoned"):
        return "down"
    if not summary.get("gateway_healthy"):
        return "degraded"
    if summary.get("watchdog_liveness") == "stale":
        return "degraded"
    if summary.get("channels_down"):
        return "degraded"
    return "ok"


def stale_active_runs(
    runs: list[dict[str, Any]] | None,
    *,
    threshold_sec: int,
    fallback_age_sec: int | None = None,
) -> list[dict[str, Any]]:
    """Return only runs whose own activity signal is stale (pure).

    Older callers/tests may not provide ``activity_age_sec``; in that case the
    host heartbeat age remains a conservative fallback.
    """
    stale: list[dict[str, Any]] = []
    for run in runs or []:
        age = run.get("activity_age_sec")
        if age is None:
            age = fallback_age_sec
        try:
            if age is not None and int(age) >= int(threshold_sec):
                stale.append({**run, "activity_age_sec": int(age)})
        except (TypeError, ValueError):
            continue
    return stale


# --------------------------------------------------------------------------- #
# OS command layer (mocked in tests).                                          #
# --------------------------------------------------------------------------- #
def is_gateway_loaded(cfg: dict[str, Any] | None = None) -> bool:
    """True when launchd has the gateway job registered (managing it)."""
    label = gwcfg.label(cfg)
    try:
        out = _run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def is_gateway_healthy(cfg: dict[str, Any] | None = None) -> bool:
    """True when the health command responds successfully within the timeout.

    If the health command is not on PATH we return True (no evidence) so we
    never restart blindly.
    """
    cfg = cfg or gwcfg.load_gateway_config()
    timeout = int(gwcfg.watchdog_tuning(cfg).get("health_timeout_sec", HEALTH_TIMEOUT_SEC))
    try:
        out = _run(
            gwcfg.health_argv(cfg),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return out.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        # Health binary genuinely not found even after PATH augmentation: we
        # have no evidence the gateway is down, so don't restart blindly.
        return True


def configured_but_down_channels(cfg: dict[str, Any] | None = None) -> list[str]:
    """Channels that are configured but not running (alive-but-disconnected)."""
    cfg = cfg or gwcfg.load_gateway_config()
    timeout = int(gwcfg.watchdog_tuning(cfg).get("health_timeout_sec", HEALTH_TIMEOUT_SEC))
    try:
        out = _run(
            gwcfg.channels_argv(cfg),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if out.returncode != 0:
            return []
        data = json.loads(out.stdout)
        channels = data.get("channels") or {}
        down: list[str] = []
        for cid, info in channels.items():
            if isinstance(info, dict) and info.get("configured") and not info.get("running"):
                down.append(str(cid))
        return down
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return []


def restart_gateway(cfg: dict[str, Any] | None = None) -> bool:
    """Force launchd to kill and relaunch the gateway job (hung recovery)."""
    try:
        _run(
            gwcfg.restart_argv(cfg),
            capture_output=True,
            timeout=15,
            check=False,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def start_gateway(cfg: dict[str, Any] | None = None) -> bool:
    """Start the gateway when it is dead/unregistered (§W1).

    Tries the configured ``start_cmd`` first; if that errors, falls back to
    ``launchctl kickstart`` so a registered-but-stopped job still comes back.
    """
    cfg = cfg or gwcfg.load_gateway_config()
    started = False
    try:
        out = _run(
            gwcfg.start_argv(cfg),
            capture_output=True,
            timeout=30,
            check=False,
        )
        started = out.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        started = False
    if not started:
        # best-effort fallback: kickstart a (possibly registered) job
        return restart_gateway(cfg)
    return True


def is_watchdog_loaded(label: str = WATCHDOG_LABEL) -> bool:
    try:
        out = _run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def reload_watchdog(plist: Path = WATCHDOG_PLIST) -> bool:
    """Re-load the watchdog's own launchd job if it dropped (§W3, idempotent)."""
    if not plist.is_file():
        return False
    try:
        _run(["launchctl", "load", str(plist)], capture_output=True, timeout=10, check=False)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def watchdog_template_path(skills_root: Path | None = None) -> Path:
    root = skills_root or SKILLS_ROOT
    return root / "scripts" / "com.doorman.watchdog.plist.template"


def render_watchdog_plist(skills_root: Path | None = None) -> str:
    """Render the watchdog LaunchAgent plist from the template, baking in the
    SKILLS dir, this Python, and a PATH that can actually find ``openclaw``."""
    root = skills_root or SKILLS_ROOT
    tpl = watchdog_template_path(root).read_text(encoding="utf-8")
    return (
        tpl.replace("{{SKILLS_DIR}}", str(root))
        .replace("{{PYTHON3}}", sys.executable)
        .replace("{{PATH}}", gwcfg.augmented_path())
    )


def ensure_watchdog_installed(skills_root: Path | None = None, plist: Path = WATCHDOG_PLIST) -> bool:
    """Recovery: make sure the watchdog LaunchAgent exists AND is loaded.

    Stronger than ``reload_watchdog``: if the plist file itself was deleted, this
    regenerates it from the template before loading, so the supervisor can heal
    even after someone removes its launchd config. Best-effort; never raises.
    """
    try:
        if not plist.is_file():
            content = render_watchdog_plist(skills_root)
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_text(content, encoding="utf-8")
        if not is_watchdog_loaded():
            try:
                _run(["launchctl", "load", str(plist)], capture_output=True, timeout=10, check=False)
            except (OSError, subprocess.TimeoutExpired):
                return False
        return True
    except OSError:
        return False


def gateway_keepalive_state(cfg: dict[str, Any] | None = None) -> bool | None:
    """Best-effort read of the gateway job's ``KeepAlive`` flag via
    ``launchctl print``. Returns True/False, or None when undeterminable."""
    label = gwcfg.label(cfg)
    try:
        uid = gwcfg._uid()
        out = _run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if out.returncode != 0:
            return None
        text = out.stdout.lower()
        if "keepalive" not in text:
            return None
        for line in text.splitlines():
            if "keepalive" in line:
                if "true" in line or "= 1" in line or "enabled" in line:
                    return True
                if "false" in line or "= 0" in line or "disabled" in line:
                    return False
        return None
    except (OSError, subprocess.TimeoutExpired):
        return None


def reassert_gateway_keepalive(cfg: dict[str, Any] | None = None) -> bool:
    """Try to turn ``KeepAlive`` back on for the gateway job (best-effort).

    We do not own the gateway plist (black box), so this is opportunistic; if it
    fails, §W1's active start/restart still covers a dead gateway.
    """
    label = gwcfg.label(cfg)
    try:
        uid = gwcfg._uid()
        out = _run(
            ["launchctl", "kickstart", f"gui/{uid}/{label}"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return out.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def notify_slack(text: str, *, level: str = "warn") -> bool:
    try:
        from _outreach_core.notify import post

        post(text, level=level)
        return True
    except Exception:
        return False


def send_vendor_ping(cfg: dict[str, Any], payload: dict[str, Any]) -> bool:
    """POST the (opt-in) liveness payload. Disabled by default; never raises."""
    vp = gwcfg.vendor_ping_config(cfg)
    if not vp.get("enabled") or not str(vp.get("url") or "").strip():
        return False
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            str(vp["url"]).strip(),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Rate limiting / abandon book-keeping.                                        #
# --------------------------------------------------------------------------- #
def _recent_restart_count(state: dict[str, Any], *, within_minutes: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
    n = 0
    for item in state.get("restart_attempts") or []:
        ts = _parse_ts(str(item.get("ts", "")))
        if ts and ts.astimezone(timezone.utc) >= cutoff:
            n += 1
    return n


def can_restart(state: dict[str, Any], tuning: dict[str, Any] | None = None) -> bool:
    window = int((tuning or {}).get("window_min", RESTART_WINDOW_MIN))
    max_r = int((tuning or {}).get("max_restarts", MAX_RESTARTS))
    abandoned = state.get("abandoned_until")
    if abandoned:
        until = _parse_ts(str(abandoned))
        if until and datetime.now(timezone.utc) < until.astimezone(timezone.utc):
            return False
    return _recent_restart_count(state, within_minutes=window) < max_r


def record_restart(state: dict[str, Any], outcome: str, tuning: dict[str, Any] | None = None) -> None:
    window = int((tuning or {}).get("window_min", RESTART_WINDOW_MIN))
    max_r = int((tuning or {}).get("max_restarts", MAX_RESTARTS))
    attempts = list(state.get("restart_attempts") or [])
    attempts.append({"ts": _utc_now(), "outcome": outcome})
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window)
    attempts = [
        a
        for a in attempts
        if (ts := _parse_ts(str(a.get("ts", "")))) and ts.astimezone(timezone.utc) >= cutoff
    ]
    state["restart_attempts"] = attempts
    if len(attempts) >= max_r:
        state["abandoned_until"] = (
            datetime.now(timezone.utc) + timedelta(minutes=ABANDON_COOLDOWN_MIN)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Self-heal + escalation orchestration.                                        #
# --------------------------------------------------------------------------- #
def _self_heal(cfg: dict[str, Any], root: Path) -> None:
    """§W3: re-load the watchdog if it dropped; re-assert gateway KeepAlive.
    Best-effort; never raises into the tick."""
    try:
        if apply_openclaw_runtime_patch():
            append_log("self-heal: re-applied OpenClaw runtime reliability config", root)
    except Exception as exc:  # pragma: no cover - defensive
        append_log(f"self-heal openclaw config error: {exc!s}", root)
    try:
        if should_reload_watchdog(is_watchdog_loaded()):
            ensure_watchdog_installed(root)
            append_log("self-heal: re-ensured watchdog launchd job", root)
    except Exception as exc:  # pragma: no cover - defensive
        append_log(f"self-heal watchdog error: {exc!s}", root)
    try:
        ka = gateway_keepalive_state(cfg)
        if should_reassert_keepalive(ka):
            reassert_gateway_keepalive(cfg)
            append_log("self-heal: re-asserted gateway KeepAlive", root)
    except Exception as exc:  # pragma: no cover - defensive
        append_log(f"self-heal keepalive error: {exc!s}", root)


def _escalate(
    state: dict[str, Any],
    cfg: dict[str, Any],
    tuning: dict[str, Any],
    root: Path,
    now_epoch: float,
) -> None:
    """§W4: notify a human + opt-in vendor ping when recovery has failed."""
    dead_alert_min = int(tuning.get("dead_alert_min", 15))
    renotify_sec = int(tuning.get("renotify_min", 30)) * 60
    if not should_escalate(
        abandoned=True,
        last_ok_epoch=state.get("last_ok_epoch"),
        now_epoch=now_epoch,
        dead_alert_min=dead_alert_min,
        last_escalation_epoch=state.get("last_escalation_epoch"),
        renotify_sec=renotify_sec,
    ):
        return
    notify_slack(
        f"🚨 gateway を {dead_alert_min} 分以上復帰できません（自動再起動の上限超過）。"
        "手動での確認・再起動をお願いします。",
        level="error",
    )
    payload = vendor_ping_payload(
        install_id=gwcfg.install_id(root),
        status="dead",
        now_iso=_utc_now(),
    )
    send_vendor_ping(cfg, payload)
    state["last_escalation_epoch"] = now_epoch
    state["escalated"] = True
    append_log("escalated: gateway dead beyond dead_alert_min", root)


def _maybe_notify_recovered(state: dict[str, Any], cfg: dict[str, Any], root: Path, now_epoch: float) -> None:
    """Send a single 'recovered' notice after a prior escalation (flap-safe)."""
    if state.get("escalated"):
        notify_slack("✅ gateway が復帰しました。", level="info")
        send_vendor_ping(
            cfg,
            vendor_ping_payload(
                install_id=gwcfg.install_id(root), status="recovered", now_iso=_utc_now()
            ),
        )
        state["escalated"] = False
        state["last_escalation_epoch"] = None
        append_log("recovered notice sent", root)


def tick(skills_root: Path | None = None) -> str:
    """One watchdog check. Returns: ok | restarted | started | abandoned | stuck | error.
    Never raises to caller (launchd safety)."""
    root = skills_root or SKILLS_ROOT
    try:
        cfg = gwcfg.load_gateway_config(root)
        tuning = gwcfg.watchdog_tuning(cfg)
        state = read_state(root)
        now = datetime.now(timezone.utc)
        now_epoch = now.timestamp()

        # §W6 (v30): refresh data/system_health/<host>.json on every tick.
        # The launchd watchdog runs every ~60s independently of any operator
        # activity; refreshing the heartbeat here means an idle but healthy
        # host appears "alive within 60s" instead of going dark for hours
        # between Slack commands. Production observation 2026-06-30:
        # MacMiniHome's heartbeat was 9h stale despite the watchdog being
        # actively running — that's exactly the gap this closes.
        try:
            from _outreach_core.helpers import healthcheck as _hc
            _hc.write_heartbeat(root)
        except Exception:
            # Heartbeat refresh is best-effort; never block tick on it.
            pass

        # §W2: detect a wake-from-sleep gap before overwriting last_tick_epoch.
        woke = detect_wake(
            now_epoch=now_epoch,
            last_tick_epoch=state.get("last_tick_epoch"),
            interval_sec=int(tuning.get("interval_sec", 60)),
            factor=int(tuning.get("wake_gap_factor", 3)),
        )
        state["last_tick_epoch"] = now_epoch
        if woke:
            append_log("woke_from_sleep detected — deep recovery path", root)

        # §W3: keep ourselves and the gateway's KeepAlive healthy.
        _self_heal(cfg, root)

        loaded = is_gateway_loaded(cfg)
        healthy = is_gateway_healthy(cfg) if loaded else False

        if loaded and not healthy:
            streak = int(state.get("health_fail_streak", 0)) + 1
        else:
            streak = 0
        state["health_fail_streak"] = streak

        action = decide_action(
            loaded=loaded,
            healthy=healthy if loaded else False,
            health_fail_streak=streak,
            health_fail_threshold=int(tuning.get("health_fail_threshold", HEALTH_FAIL_RESTART_THRESHOLD)),
            can_restart=can_restart(state, tuning),
            woke_from_sleep=woke,
        )

        if action == "start":
            notify_slack(
                "⚠️ gateway が起動していません（launchd 未管理/プロセス死）。起動を試みます。",
                level="error",
            )
            start_gateway(cfg)
            record_restart(state, "start", tuning)
            save_state(state, root)
            append_log("started gateway (was not loaded)", root)
            return "started"

        if action == "wait":
            save_state(state, root)
            append_log(f"gateway unhealthy streak={streak} (waiting)", root)
            return "stuck"

        if action == "kickstart":
            notify_slack(
                f"⚠️ gateway が応答しません (連続 {streak} 回"
                f"{'・スリープ復帰' if woke else ''})。強制再起動します。",
                level="error",
            )
            restart_gateway(cfg)
            record_restart(state, "kickstart", tuning)
            state["health_fail_streak"] = 0
            save_state(state, root)
            append_log(f"restarted gateway (unhealthy streak={streak} woke={woke})", root)
            return "restarted"

        if action == "abandoned":
            record_restart(state, "abandoned", tuning)
            _escalate(state, cfg, tuning, root, now_epoch)
            save_state(state, root)
            append_log("abandoned (gateway dead/unhealthy beyond budget)", root)
            return "abandoned"

        # action == "ok": gateway is responsive.
        state["last_ok_epoch"] = now_epoch
        _maybe_notify_recovered(state, cfg, root, now_epoch)

        # A configured channel (e.g. Slack) can be stuck disconnected while the
        # gateway itself is healthy. Force-restart after a sustained streak.
        down = configured_but_down_channels(cfg)
        if down:
            ch_streak = int(state.get("channel_fail_streak", 0)) + 1
            state["channel_fail_streak"] = ch_streak
            threshold = int(tuning.get("channel_fail_threshold", CHANNEL_FAIL_RESTART_THRESHOLD))
            if ch_streak < threshold and not woke:
                save_state(state, root)
                append_log(f"channel down {down} streak={ch_streak} (waiting)", root)
                return "stuck"
            if can_restart(state, tuning):
                notify_slack(
                    f"⚠️ チャンネル {down} が gateway 生存中に切断したままです。"
                    "gateway を再起動して再接続します。",
                    level="error",
                )
                restart_gateway(cfg)
                record_restart(state, "channel-restart", tuning)
                state["channel_fail_streak"] = 0
                save_state(state, root)
                append_log(f"restarted gateway (channel down {down} streak={ch_streak})", root)
                return "restarted"
            record_restart(state, "abandoned", tuning)
            _escalate(state, cfg, tuning, root, now_epoch)
            save_state(state, root)
            append_log(f"abandoned (channel down {down})", root)
            return "abandoned"
        if state.get("channel_fail_streak"):
            state["channel_fail_streak"] = 0

        cli_timeout = latest_cli_backend_timeout(now_epoch=now_epoch)
        if cli_timeout and cli_timeout.get("marker") != state.get("last_cli_backend_timeout_marker"):
            state["last_cli_backend_timeout_marker"] = cli_timeout.get("marker")
            state["last_cli_backend_timeout_epoch"] = cli_timeout.get("epoch")
            if can_restart(state, tuning):
                notify_slack(
                    "⚠️ OpenClawの応答生成が無出力timeoutで止まりました。"
                    "gatewayを再起動して、次の依頼に応答できる状態へ戻します。",
                    level="warn",
                )
                restart_gateway(cfg)
                record_restart(state, "cli-backend-timeout", tuning)
                save_state(state, root)
                append_log(f"restarted gateway (cli backend timeout {cli_timeout.get('ts')})", root)
                return "restarted"
            notify_slack(
                "⚠️ OpenClawの応答生成timeoutを検知しましたが、短時間の再起動上限に達しています。"
                "少し待ってから再度確認します。",
                level="warn",
            )
            save_state(state, root)
            append_log(f"cli backend timeout detected but restart budget exhausted {cli_timeout.get('ts')}", root)
            return "stuck"

        health = read_health(root)
        age = heartbeat_age_seconds(health)
        active = collect_active_runs(root)
        stale_runs = stale_active_runs(
            active,
            threshold_sec=STALE_HEARTBEAT_SEC,
            fallback_age_sec=age,
        )
        if stale_runs:
            last = _parse_ts(str(state.get("last_stuck_notify") or ""))
            if last is None or (now - last.astimezone(timezone.utc)).total_seconds() >= ABANDON_NOTIFY_INTERVAL_SEC:
                labels = ", ".join(
                    f"{r.get('run_id') or r.get('brief_id') or '?'}={r.get('activity_age_sec')}s"
                    for r in stale_runs[:3]
                )
                notify_slack(
                    "⚠️ gateway は応答していますが、実行中 run 固有の進捗が"
                    f"更新されていません ({labels})。タスクが詰まっている可能性があります。",
                    level="warn",
                )
                state["last_stuck_notify"] = _utc_now()
            save_state(state, root)
            append_log(
                f"stuck run activity stale_runs={len(stale_runs)}/{len(active)}",
                root,
            )
            return "stuck"

        if state.get("last_stuck_notify"):
            state["last_stuck_notify"] = None
        save_state(state, root)
        append_log("tick ok", root)
        return "ok"
    except Exception as exc:
        append_log(f"tick error: {exc!s}", root)
        return "error"


def _abandoned_now(state: dict[str, Any]) -> bool:
    ab = state.get("abandoned_until")
    if not ab:
        return False
    until = _parse_ts(str(ab))
    return bool(until and datetime.now(timezone.utc) < until.astimezone(timezone.utc))


def status_summary(skills_root: Path | None = None) -> dict[str, Any]:
    """Gather a single, consolidated health picture (observability).

    Answers "is it up, is the watchdog itself alive, and when was it last OK?"
    in one structure that both the CLI and Slack can render.
    """
    root = skills_root or SKILLS_ROOT
    cfg = gwcfg.load_gateway_config(root)
    tuning = gwcfg.watchdog_tuning(cfg)
    state = read_state(root)
    now = datetime.now(timezone.utc).timestamp()

    loaded = is_gateway_loaded(cfg)
    healthy = bool(is_gateway_healthy(cfg)) if loaded else False
    last_tick = state.get("last_tick_epoch")
    last_ok = state.get("last_ok_epoch")
    channels = configured_but_down_channels(cfg) if (loaded and healthy) else []
    cli_timeout = latest_cli_backend_timeout(now_epoch=now, lookback_sec=24 * 60 * 60)

    summary: dict[str, Any] = {
        "gateway_label": gwcfg.label(cfg),
        "gateway_loaded": loaded,
        "gateway_healthy": healthy,
        "watchdog_loaded": is_watchdog_loaded(),
        "watchdog_liveness": watchdog_liveness(
            last_tick, now, int(tuning.get("interval_sec", 60)), 4
        ),
        "watchdog_last_tick_age_sec": int(now - last_tick) if last_tick else None,
        "last_ok_age_sec": int(now - last_ok) if last_ok else None,
        "recent_restarts": _recent_restart_count(
            state, within_minutes=int(tuning.get("window_min", RESTART_WINDOW_MIN))
        ),
        "abandoned": _abandoned_now(state),
        "channels_down": channels,
        "cli_backend_last_timeout_age_sec": (
            int(now - cli_timeout["epoch"]) if cli_timeout else None
        ),
    }
    summary["status"] = overall_status(summary)
    return summary


def _age(sec: int | None) -> str:
    if sec is None:
        return "なし"
    if sec < 90:
        return f"{sec}秒前"
    if sec < 5400:
        return f"{sec // 60}分前"
    return f"{sec // 3600}時間前"


def format_status_summary(summary: dict[str, Any]) -> str:
    """Human/Slack-friendly rendering of ``status_summary`` (pure)."""
    icon = {"ok": "✅", "degraded": "⚠️", "down": "🚨"}.get(summary.get("status", ""), "❓")
    verdict = {"ok": "正常", "degraded": "劣化", "down": "ダウン"}.get(
        summary.get("status", ""), "不明"
    )
    yn = lambda b: "✅" if b else "❌"  # noqa: E731
    live_icon = {"ok": "✅", "stale": "🚨", "unknown": "❓"}.get(
        summary.get("watchdog_liveness", ""), "❓"
    )
    lines = [
        f"{icon} Doorman gateway: {verdict}",
        f"  • gateway: 起動 {yn(summary.get('gateway_loaded'))} / "
        f"応答 {yn(summary.get('gateway_healthy'))} ({summary.get('gateway_label')})",
        f"  • watchdog: 起動 {yn(summary.get('watchdog_loaded'))} / "
        f"監視 {live_icon} (最終tick {_age(summary.get('watchdog_last_tick_age_sec'))})",
        f"  • 最終正常: {_age(summary.get('last_ok_age_sec'))} / "
        f"直近再起動: {summary.get('recent_restarts', 0)}回"
        f"{' / ABANDONED' if summary.get('abandoned') else ''}",
    ]
    if summary.get("cli_backend_last_timeout_age_sec") is not None:
        lines.append(
            f"  • CLI無出力timeout: {_age(summary.get('cli_backend_last_timeout_age_sec'))}"
        )
    down = summary.get("channels_down") or []
    if down:
        lines.append(f"  • 切断中チャンネル: {', '.join(down)}")
    return "\n".join(lines)


def cmd_tick(args: argparse.Namespace) -> int:
    outcome = tick(Path(args.skills_root) if args.skills_root else None)
    print(outcome)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.skills_root) if args.skills_root else None
    summary = status_summary(root)
    if getattr(args, "json", False):
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(format_status_summary(summary))
    if getattr(args, "notify", False):
        level = {"ok": "info", "degraded": "warn", "down": "error"}.get(summary["status"], "warn")
        notify_slack(format_status_summary(summary), level=level)
    # non-zero exit when not healthy, so scripts/monitors can branch on it
    return 0 if summary["status"] == "ok" else 1


def cmd_ensure(args: argparse.Namespace) -> int:
    root = Path(args.skills_root) if args.skills_root else None
    ok = ensure_watchdog_installed(root)
    print("ensured" if ok else "failed")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Doorman watchdog (v4 §15-C / v14 §W1-W5)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tick", help="Run one watchdog check")
    p.add_argument("--skills-root", default=None)
    ps = sub.add_parser("status", help="Consolidated gateway+watchdog health")
    ps.add_argument("--skills-root", default=None)
    ps.add_argument("--json", action="store_true", help="Emit JSON")
    ps.add_argument("--notify", action="store_true", help="Also post the summary to Slack")
    pe = sub.add_parser("ensure", help="Reinstall+load the watchdog LaunchAgent if missing")
    pe.add_argument("--skills-root", default=None)
    args = ap.parse_args()
    if args.cmd == "tick":
        sys.exit(cmd_tick(args))
    if args.cmd == "status":
        sys.exit(cmd_status(args))
    if args.cmd == "ensure":
        sys.exit(cmd_ensure(args))
    ap.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
