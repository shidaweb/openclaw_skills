"""
Autonomous operation profile for the Doorman pipeline (v5 §12).

The pipeline historically runs *supervised*: each draft is previewed in Slack
for a per-item yes/no/skip, and reCAPTCHA / unexpected-field / submit-button
problems escalate to a human and **block** waiting for "進めて".

This module adds an opt-in *autonomous* profile. The bargain is explicit:

  - Quality is locked **once, upfront** (brief + target list + sample drafts are
    approved a single time). After that, the agent decides skip / screen / send
    by itself and never asks for per-item confirmation.
  - Anything that used to block-and-wait (captcha v2, wrong-form, missing submit
    button, unexpected required field) instead **auto-skips and logs** so the run
    keeps moving. Nothing waits on a human mid-run.
  - Each draft passes a deterministic-ish LLM **self-score** against the brief's
    quality bar before it is sent; below threshold → auto-skip + log.

All behaviour is config-gated under the brief's ``autonomy:`` block. The default
(``mode: supervised``) reproduces the legacy human-in-the-loop flow exactly, so
this module is purely additive and cannot regress an existing brief.

Pure Python. The only LLM touchpoint (self-score) takes an injected
``oc_infer_fn`` so it is fully unit-testable without network access.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MODE = "supervised"  # supervised | autonomous
DEFAULT_BLOCKER_ACTION = "skip_and_log"  # skip_and_log | escalate
DEFAULT_SELF_SCORE_ENABLED = True
DEFAULT_SCORE_THRESHOLD = 0.75
DEFAULT_ON_SCORE_ERROR = "send"  # send | skip (fail-open, since quality is locked upfront)
DEFAULT_UPFRONT_APPROVAL = True
DEFAULT_SAMPLE_DRAFTS = 3
DEFAULT_SELF_RESTART = True

_STATE_FILENAME = "autonomy_state.json"


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

def autonomy_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return the brief's ``autonomy`` block merged over defaults."""
    block = {}
    if isinstance(config, dict):
        raw = config.get("autonomy")
        if isinstance(raw, dict):
            block = raw

    score_raw = block.get("draft_self_score")
    score = score_raw if isinstance(score_raw, dict) else {}

    upfront_raw = block.get("upfront_approval")
    upfront = upfront_raw if isinstance(upfront_raw, dict) else {}

    return {
        "mode": (block.get("mode") or DEFAULT_MODE),
        "on_blocker": (block.get("on_blocker") or DEFAULT_BLOCKER_ACTION),
        "self_restart": _as_bool(block.get("self_restart"), DEFAULT_SELF_RESTART),
        "draft_self_score": {
            "enabled": _as_bool(score.get("enabled"), DEFAULT_SELF_SCORE_ENABLED),
            "threshold": _as_float(score.get("threshold"), DEFAULT_SCORE_THRESHOLD),
            "on_error": (score.get("on_error") or DEFAULT_ON_SCORE_ERROR),
        },
        "upfront_approval": {
            "required": _as_bool(upfront.get("required"), DEFAULT_UPFRONT_APPROVAL),
            "sample_drafts": _as_int(upfront.get("sample_drafts"), DEFAULT_SAMPLE_DRAFTS),
        },
    }


def is_autonomous(config: dict[str, Any] | None) -> bool:
    return autonomy_config(config)["mode"] == "autonomous"


def blocker_action(config: dict[str, Any] | None) -> str:
    """What to do when a mid-run blocker is hit.

    In autonomous mode the default is ``skip_and_log`` (keep moving). A brief may
    override to ``escalate`` to retain the legacy behaviour for specific blockers.
    Supervised briefs always escalate.
    """
    cfg = autonomy_config(config)
    if cfg["mode"] != "autonomous":
        return "escalate"
    return cfg["on_blocker"]


def self_score_enabled(config: dict[str, Any] | None) -> bool:
    cfg = autonomy_config(config)
    return cfg["mode"] == "autonomous" and cfg["draft_self_score"]["enabled"]


def score_threshold(config: dict[str, Any] | None) -> float:
    return autonomy_config(config)["draft_self_score"]["threshold"]


def upfront_approval_required(config: dict[str, Any] | None) -> bool:
    cfg = autonomy_config(config)
    return cfg["mode"] == "autonomous" and cfg["upfront_approval"]["required"]


def sample_draft_count(config: dict[str, Any] | None) -> int:
    return autonomy_config(config)["upfront_approval"]["sample_drafts"]


def self_restart_enabled(config: dict[str, Any] | None) -> bool:
    return autonomy_config(config)["self_restart"]


# ---------------------------------------------------------------------------
# Draft self-scoring (the autonomous replacement for human yes/no)
# ---------------------------------------------------------------------------

_SCORE_SYSTEM = (
    "あなたはアウトバウンド営業のドラフト審査官です。送信者ブリーフの品質基準に照らして"
    "1件のドラフトを採点します。評価軸: (1) 宛先企業との関連性・具体性 (2) 誇大・不適切表現の"
    "不在 (3) 文字数・トーンの適合 (4) CTA の自然さ。"
    "次の JSON だけを返答してください: "
    '{"score": 0.0-1.0, "verdict": "send"|"skip", "reason": "簡潔な理由(日本語, 80字以内)"}'
)


def build_score_prompt(draft: dict[str, Any], config: dict[str, Any] | None) -> str:
    sender = (config or {}).get("sender") or {}
    pitch = (config or {}).get("pitch") or {}
    d = draft.get("draft") or {}
    parts = [
        _SCORE_SYSTEM,
        "",
        "## 送信者ブリーフ(要約)",
        f"- 送信者: {sender.get('company', '?')} / {sender.get('name', '?')}",
        f"- 提案: {(config or {}).get('product', {}).get('one_liner', '')}",
        f"- 課題: {pitch.get('problem', '')[:200]}",
        "",
        "## 宛先",
        f"- 会社: {draft.get('name', '?')} ({draft.get('industry', '?')})",
        f"- シグナル: {draft.get('direct_signals', draft.get('signals', ''))}",
        f"- 文字数上限: {draft.get('char_limit', '不明')}",
        "",
        "## ドラフト",
        f"件名: {d.get('subject', '')}",
        f"本文({len(d.get('body', ''))}字):",
        d.get("body", ""),
        "",
        "JSON のみで採点を返してください。",
    ]
    return "\n".join(parts)


def parse_score_response(text: str | None) -> dict[str, Any] | None:
    """Extract the score JSON from an LLM response, tolerating prose/code fences."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "score" not in obj:
        return None
    score = _as_float(obj.get("score"), None)
    if score is None:
        return None
    score = max(0.0, min(1.0, score))
    verdict = obj.get("verdict")
    if verdict not in ("send", "skip"):
        verdict = None
    return {
        "score": score,
        "verdict": verdict,
        "reason": str(obj.get("reason") or "")[:200],
    }


def self_score_draft(
    draft: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    oc_infer_fn: Callable[..., str | None],
    model: str | None = None,
) -> dict[str, Any]:
    """Score one draft. Returns a decision dict:

        {"send": bool, "score": float|None, "reason": str, "errored": bool}

    Never raises: on any inference/parse failure it falls back to the brief's
    ``on_error`` policy (default ``send`` — quality is already locked upfront).
    """
    cfg = autonomy_config(config)
    threshold = cfg["draft_self_score"]["threshold"]
    on_error = cfg["draft_self_score"]["on_error"]
    prompt = build_score_prompt(draft, config)

    raw = None
    try:
        raw = oc_infer_fn(prompt, model=model) if model else oc_infer_fn(prompt)
    except Exception as exc:  # noqa: BLE001 - scorer must never crash a run
        return {
            "send": (on_error == "send"),
            "score": None,
            "reason": f"score_error: {exc}"[:200],
            "errored": True,
        }

    parsed = parse_score_response(raw)
    if parsed is None:
        return {
            "send": (on_error == "send"),
            "score": None,
            "reason": "score_unparseable",
            "errored": True,
        }

    send = should_send(parsed["score"], config)
    # An explicit verdict can only make the decision *more* conservative.
    if parsed.get("verdict") == "skip":
        send = False
    return {
        "send": send,
        "score": parsed["score"],
        "reason": parsed["reason"] or f"score={parsed['score']:.2f} thr={threshold:.2f}",
        "errored": False,
    }


def should_send(score: float | None, config: dict[str, Any] | None) -> bool:
    if score is None:
        return autonomy_config(config)["draft_self_score"]["on_error"] == "send"
    return score >= score_threshold(config)


# ---------------------------------------------------------------------------
# Upfront one-time approval state (the single human touchpoint)
# ---------------------------------------------------------------------------

def autonomy_state_path(data_dir: Path) -> Path:
    return Path(data_dir) / _STATE_FILENAME


def read_autonomy_state(data_dir: Path) -> dict[str, Any]:
    path = autonomy_state_path(data_dir)
    if not path.exists():
        return {"approved": False, "pending": None, "history": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("approved", False)
            data.setdefault("pending", None)
            data.setdefault("history", [])
            return data
    except (ValueError, OSError):
        pass
    return {"approved": False, "pending": None, "history": []}


def _write_state(data_dir: Path, state: dict[str, Any]) -> None:
    path = autonomy_state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def is_upfront_approved(data_dir: Path) -> bool:
    return bool(read_autonomy_state(data_dir).get("approved"))


def mark_pending_approval(data_dir: Path, summary: dict[str, Any]) -> None:
    """Record that a campaign produced a list + sample drafts and is waiting for
    the one-time human go-ahead. Does not flip ``approved``."""
    state = read_autonomy_state(data_dir)
    state["pending"] = {
        "at": _now(),
        "summary": summary,
    }
    _write_state(data_dir, state)


def mark_upfront_approved(data_dir: Path, *, by: str = "user", note: str = "") -> dict[str, Any]:
    """Flip the brief into approved-autonomous. Idempotent."""
    state = read_autonomy_state(data_dir)
    already = bool(state.get("approved"))
    state["approved"] = True
    state["approved_at"] = state.get("approved_at") or _now()
    state["approved_by"] = by
    if note:
        state["approved_note"] = note
    history = state.get("history") or []
    history.append({"at": _now(), "event": "approved", "by": by, "note": note})
    state["history"] = history[-50:]
    state["pending"] = None
    _write_state(data_dir, state)
    state["_was_already_approved"] = already
    return state


def revoke_upfront_approval(data_dir: Path, *, by: str = "user") -> None:
    """Return the brief to pre-approval (e.g. after a major brief/list change)."""
    state = read_autonomy_state(data_dir)
    state["approved"] = False
    state["pending"] = None
    history = state.get("history") or []
    history.append({"at": _now(), "event": "revoked", "by": by})
    state["history"] = history[-50:]
    _write_state(data_dir, state)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_bool(val: Any, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(val)


def _as_float(val: Any, default: float | None) -> float | None:
    try:
        if val is None:
            return default
        return float(val)
    except (ValueError, TypeError):
        return default


def _as_int(val: Any, default: int) -> int:
    try:
        if val is None:
            return default
        return int(val)
    except (ValueError, TypeError):
        return default
