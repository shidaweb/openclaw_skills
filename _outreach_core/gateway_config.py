#!/usr/bin/env python3
"""Gateway supervision config (v14 §W5) — config-driven, dependency-minimal.

The external watchdog must keep working even if the Doorman Python venv is
broken (invariant §8.6). So this module:

- holds all defaults in code (works with zero config),
- loads overrides from a plain ``data/gateway.json`` (stdlib only) and, only
  when ``pyyaml`` happens to be importable, from a ``gateway.yaml`` /
  brief ``gateway:`` block,
- never raises on a missing/garbled config (best-effort merge), and
- exposes the gateway ``label`` and the ``health``/``start``/``restart``
  commands as rendered ``argv`` lists, so nothing is hardcoded in watchdog.py.

The hardcoded ``GATEWAY_LABEL = "ai.openclaw.gateway"`` of the old watchdog is
retired here; it lives on only as the default value.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
from pathlib import Path
from typing import Any

SKILLS_ROOT = Path(__file__).resolve().parent.parent

# launchd starts jobs with a minimal PATH (``/usr/bin:/bin:/usr/sbin:/sbin``),
# so ``openclaw`` (typically under Homebrew/npm) is NOT found and every probe
# silently fails. We augment PATH with the usual install locations so the
# supervisor can actually find/launch the gateway under launchd. This is the
# single most important "doesn't fall" fix in v14.
_EXTRA_PATH_DIRS = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)

DEFAULT_LABEL = "ai.openclaw.gateway"

# All defaults live in code so the supervisor runs even with no config file.
DEFAULTS: dict[str, Any] = {
    "label": DEFAULT_LABEL,
    "health_cmd": "openclaw health",
    "channels_cmd": "openclaw channels status --json",
    "start_cmd": "openclaw gateway start",
    # ``{uid}`` / ``{label}`` / ``$UID`` get substituted at render time.
    "restart_cmd": "launchctl kickstart -k gui/{uid}/{label}",
    "watchdog": {
        "interval_sec": 60,
        "max_restarts": 3,
        "window_min": 10,
        "dead_alert_min": 15,
        "health_timeout_sec": 20,
        "health_fail_threshold": 2,
        "channel_fail_threshold": 5,
        "wake_gap_factor": 3,
        "renotify_min": 30,
    },
    "vendor_ping": {"enabled": False, "url": ""},
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Allow either a flat block or one nested under "gateway".
                return data.get("gateway") if isinstance(data.get("gateway"), dict) else data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}


def _load_yaml_file(path: Path) -> dict[str, Any]:
    """Best-effort YAML load; silently no-ops if pyyaml is unavailable."""
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    try:
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                return data.get("gateway") if isinstance(data.get("gateway"), dict) else data
    except Exception:
        pass
    return {}


def _env_overrides() -> dict[str, Any]:
    """Environment escape hatch (highest precedence, dependency-free)."""
    out: dict[str, Any] = {}
    label = os.environ.get("DOORMAN_GATEWAY_LABEL", "").strip()
    if label:
        out["label"] = label
    for env_key, cfg_key in (
        ("DOORMAN_GATEWAY_HEALTH_CMD", "health_cmd"),
        ("DOORMAN_GATEWAY_START_CMD", "start_cmd"),
        ("DOORMAN_GATEWAY_RESTART_CMD", "restart_cmd"),
        ("DOORMAN_GATEWAY_CHANNELS_CMD", "channels_cmd"),
    ):
        val = os.environ.get(env_key, "").strip()
        if val:
            out[cfg_key] = val
    return out


def load_gateway_config(skills_root: Path | None = None) -> dict[str, Any]:
    """Merge code defaults → gateway.json → gateway.yaml → env. Never raises."""
    root = skills_root or SKILLS_ROOT
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy of defaults
    # JSON first (always loadable), then YAML can refine it, then env wins.
    cfg = _deep_merge(cfg, _load_json_file(root / "data" / "gateway.json"))
    cfg = _deep_merge(cfg, _load_yaml_file(root / "gateway.yaml"))
    cfg = _deep_merge(cfg, _load_yaml_file(root / "config" / "gateway.yaml"))
    cfg = _deep_merge(cfg, _env_overrides())
    return cfg


def _home_bin_dirs() -> list[str]:
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".npm-global", "bin"),
        os.path.join(home, "bin"),
    ]


def augmented_path() -> str:
    """A PATH that includes the current PATH plus common Homebrew/npm/user bin
    dirs, so binaries resolve even under launchd's minimal default PATH."""
    seen: list[str] = []

    def add(d: str) -> None:
        if d and d not in seen and os.path.isdir(d):
            seen.append(d)

    for d in os.environ.get("PATH", "").split(os.pathsep):
        add(d)
    for d in _EXTRA_PATH_DIRS:
        add(d)
    for d in _home_bin_dirs():
        add(d)
    return os.pathsep.join(seen)


def augmented_env() -> dict[str, str]:
    """``os.environ`` with PATH augmented for subprocess calls."""
    env = dict(os.environ)
    env["PATH"] = augmented_path()
    return env


def resolve_argv(argv: list[str]) -> list[str]:
    """Resolve ``argv[0]`` to an absolute path via the augmented PATH.

    Returns argv unchanged if the binary can't be found (the caller's existing
    error handling then applies). This keeps the config accessors returning the
    plain command (good for tests) while the OS layer resolves at call time.
    """
    if not argv:
        return argv
    exe = shutil.which(argv[0], path=augmented_path())
    return [exe, *argv[1:]] if exe else list(argv)


def _uid() -> int:
    try:
        return os.getuid()  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - non-POSIX
        return 0


def render_cmd(template: str, *, label: str, uid: int | None = None) -> list[str]:
    """Render a command template into an argv list.

    Substitutes ``{label}`` / ``$LABEL`` and ``{uid}`` / ``$UID`` so configs can
    use either brace or shell-style placeholders.
    """
    u = str(_uid() if uid is None else uid)
    s = (
        template.replace("{label}", label)
        .replace("$LABEL", label)
        .replace("{uid}", u)
        .replace("$UID", u)
    )
    try:
        return shlex.split(s)
    except ValueError:
        return s.split()


def label(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or load_gateway_config()
    return str(cfg.get("label") or DEFAULT_LABEL)


def health_argv(cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = cfg or load_gateway_config()
    return render_cmd(str(cfg.get("health_cmd") or DEFAULTS["health_cmd"]), label=label(cfg))


def channels_argv(cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = cfg or load_gateway_config()
    return render_cmd(str(cfg.get("channels_cmd") or DEFAULTS["channels_cmd"]), label=label(cfg))


def start_argv(cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = cfg or load_gateway_config()
    return render_cmd(str(cfg.get("start_cmd") or DEFAULTS["start_cmd"]), label=label(cfg))


def restart_argv(cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = cfg or load_gateway_config()
    return render_cmd(str(cfg.get("restart_cmd") or DEFAULTS["restart_cmd"]), label=label(cfg))


def watchdog_tuning(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or load_gateway_config()
    base = dict(DEFAULTS["watchdog"])
    base.update({k: v for k, v in (cfg.get("watchdog") or {}).items() if v is not None})
    return base


def vendor_ping_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or load_gateway_config()
    vp = dict(DEFAULTS["vendor_ping"])
    vp.update({k: v for k, v in (cfg.get("vendor_ping") or {}).items() if v is not None})
    return vp


def install_id(skills_root: Path | None = None) -> str:
    """Stable, non-PII install identifier for vendor liveness pings.

    Random UUID persisted under ``data/install_id``. Contains no hostname, user,
    or any personal data — purely an opaque token to correlate pings.
    """
    root = skills_root or SKILLS_ROOT
    path = root / "data" / "install_id"
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError:
        pass
    import uuid

    new_id = uuid.uuid4().hex
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id + "\n", encoding="utf-8")
    except OSError:
        pass
    return new_id
