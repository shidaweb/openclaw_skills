"""OpenClaw subprocess helpers (browser + infer)."""

from __future__ import annotations

import json
import subprocess
from typing import Any

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


def oc_infer(prompt: str, model: str = DEFAULT_MODEL) -> str | None:
    """
    `openclaw infer model run --prompt ... --model ... --json` returning the
    text output of the model. None on error.
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
