"""
OpenClaw subprocess helpers (browser + infer).

Model policy (v3):
- Slack/OpenClaw agent: Opus 4.7 (configured outside this repo; not switched here).
- Python sub-tasks via oc_infer: Sonnet by default (DEFAULT_MODEL below).
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

# Sonnet — pinned default for draft / form-analyzer (prompt-cache friendly, low cost).
DEFAULT_MODEL = "claude-cli/claude-sonnet-4-6"
BROWSER_PROFILE = "openclaw"


def _run(cmd: list[str]) -> tuple[int, str, str]:
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode, res.stdout, res.stderr


def oc_browser(*args: str, profile: str = BROWSER_PROFILE) -> str | None:
    """`openclaw browser ...` returning stdout text. None on error."""
    cmd = ["openclaw", "browser", "--browser-profile", profile, *args]
    rc, out, err = _run(cmd)
    if rc != 0:
        print(f"[browser err] {' '.join(args)}: {err.strip()}", file=__import__("sys").stderr)
        return None
    return out


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
