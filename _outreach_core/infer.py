"""
OpenClaw subprocess helpers (browser + infer).

Model policy (v3):
- Slack/OpenClaw agent: Opus 4.7 (configured outside this repo; not switched here).
- Python sub-tasks via oc_infer: Sonnet by default (DEFAULT_MODEL below).
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from _outreach_core.config import load_runtime_config

# Sonnet — pinned default for draft / form-analyzer (prompt-cache friendly, low cost).
DEFAULT_MODEL = "claude-cli/claude-sonnet-4-6"
BROWSER_PROFILE = "openclaw"


def _run(cmd: list[str], *, timeout: float | None = None) -> tuple[int, str, str]:
    # Per-subprocess hard wall: prevents a hung `openclaw infer` (or any child)
    # from stalling the whole pipeline. Override via OUTREACH_SUBPROC_TIMEOUT_SEC.
    # (root cause of pigeon_corp draft-loop hang 2026-05-31)
    if timeout is None:
        env_val = os.environ.get("OUTREACH_SUBPROC_TIMEOUT_SEC", "").strip()
        if env_val:
            try:
                timeout = float(env_val)
            except ValueError:
                timeout = 240.0
        else:
            timeout = 240.0
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired as e:
        partial_out = (e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, (bytes, bytearray)) else (e.stdout or "")) if e.stdout else ""
        partial_err = (e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, (bytes, bytearray)) else (e.stderr or "")) if e.stderr else ""
        return 124, partial_out, partial_err + f"\n[_run] timeout after {timeout}s: {' '.join(cmd[:4])}..."


def browser_headless_preference() -> bool | None:
    """
    Whether Doorman should request a headless OpenClaw browser.

    Priority: DOORMAN_BROWSER_HEADLESS → brief yaml browser.headless →
    OPENCLAW_BROWSER_HEADLESS (1/0) → None (OpenClaw default = visible).
    """
    env = os.environ.get("DOORMAN_BROWSER_HEADLESS", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    try:
        brief = load_runtime_config() or {}
    except Exception:
        brief = {}
    browser = brief.get("browser") or {}
    if "headless" in browser:
        return bool(browser.get("headless"))
    oc = os.environ.get("OPENCLAW_BROWSER_HEADLESS", "").strip()
    if oc == "1":
        return True
    if oc == "0":
        return False
    return None


def oc_browser_start(*, headless: bool | None = None, profile: str = BROWSER_PROFILE) -> bool:
    """
    Ensure the OpenClaw managed browser is running.

    headless=True → `openclaw browser start --headless` (no window; same login profile).
    If a visible browser is already running, OpenClaw keeps it (no-op). Stop first:
      openclaw browser --browser-profile openclaw stop
    """
    h = headless if headless is not None else browser_headless_preference()
    args = ["start"]
    if h is True:
        args.append("--headless")
    cmd = ["openclaw", "browser", "--browser-profile", profile, *args]
    rc, out, err = _run(cmd)
    if rc != 0:
        print(f"[browser start err] {err.strip() or out.strip()}", file=__import__("sys").stderr)
        return False
    return True


def oc_browser(*args: str, profile: str = BROWSER_PROFILE) -> str | None:
    """`openclaw browser ...` returning stdout text. None on error."""
    cmd = ["openclaw", "browser", "--browser-profile", profile, *args]
    rc, out, err = _run(cmd)
    if rc != 0:
        print(f"[browser err] {' '.join(args)}: {err.strip()}", file=__import__("sys").stderr)
        return None
    return out


def extract_json_payload(stdout: str | None) -> Any:
    """Pull the JSON object/array out of `openclaw browser --json ...` stdout,
    tolerating the 🦞 banner and box-drawing decoration lines. Pure + testable."""
    if not stdout:
        return None
    body_lines: list[str] = []
    for line in stdout.splitlines():
        s = line.strip()
        if not s or s.startswith("🦞"):
            continue
        if all(ch in "│◇└├─┃|" for ch in s):
            continue
        body_lines.append(line)
    text = "\n".join(body_lines).strip()
    if not text:
        return None
    # Find the first JSON object/array start (handles any leading prose).
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if starts:
        text = text[min(starts):]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def oc_browser_json(*args: str, profile: str = BROWSER_PROFILE) -> Any:
    """Run `openclaw browser --browser-profile <p> --json <args>` and return the
    parsed JSON (dict/list) or None. Used for tab management (open/tabs)."""
    cmd = ["openclaw", "browser", "--browser-profile", profile, "--json", *args]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[browser json err] {' '.join(args)}: {res.stderr.strip()}",
              file=__import__("sys").stderr)
        return None
    return extract_json_payload(res.stdout)


def oc_evaluate(js: str, *, profile: str = BROWSER_PROFILE) -> Any:
    """Run JS in the browser via `openclaw browser evaluate --fn`. No LLM."""
    cmd = ["openclaw", "browser", "--browser-profile", profile, "evaluate", "--fn", js]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[evaluate err] {res.stderr.strip()}", file=__import__("sys").stderr)
        return None

    body_lines: list[str] = []
    for line in res.stdout.splitlines():
        s = line.strip()
        if not s or s.startswith("🦞"):
            continue
        if all(ch in "│◇└├─┃|" for ch in s):
            continue
        body_lines.append(line)
    if not body_lines:
        return None

    text = "\n".join(body_lines).strip()
    try:
        decoded = json.loads(text)
        if isinstance(decoded, str):
            try:
                decoded = json.loads(decoded)
            except Exception:
                pass
        return decoded
    except json.JSONDecodeError as e:
        print(f"[evaluate parse err] {e}: {text[:300]}", file=__import__("sys").stderr)
        return None


def oc_infer(prompt: str, model: str = DEFAULT_MODEL) -> str | None:
    """
    `openclaw infer model run --prompt ... --model ... --json` returning the
    text output of the model. None on error.

    Default model is Sonnet (DEFAULT_MODEL). Callers must pass model explicitly
  if overriding; do not change the default to Opus.
    """
    cmd = [
        "openclaw",
        "infer",
        "model",
        "run",
        "--prompt",
        prompt,
        "--model",
        model,
        "--json",
    ]
    rc, out, err = _run(cmd)
    if rc != 0:
        print(f"[infer err] {err.strip()}", file=__import__("sys").stderr)
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        print(f"[infer json parse err] {out[:300]}", file=__import__("sys").stderr)
        return None
    if not data.get("ok"):
        print(f"[infer not ok] {data.get('error')}", file=__import__("sys").stderr)
        return None
    outputs: list[Any] = data.get("outputs") or []
    if outputs:
        first = outputs[0] if isinstance(outputs[0], dict) else {}
        for k in ("text", "content", "output_text"):
            if first.get(k):
                return str(first[k])
    return json.dumps(data, ensure_ascii=False)
