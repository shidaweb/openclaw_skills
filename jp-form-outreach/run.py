#!/usr/bin/env python3
"""
jp-form-outreach pipeline.

Stages: bootstrap -> enrich -> draft -> preview -> send -> mark-sent

Sister to linkedin-outreach. Same architecture (openclaw browser + infer,
JSONL state, prompt caching, skip/sent history) adapted for Japanese
B2B inquiry forms.

State files live in ./data/*.jsonl  (append-only, resumable).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _outreach_core import draft as core_draft
from _outreach_core import history as core_history
from _outreach_core import autonomy as core_autonomy
from _outreach_core import avoidance as core_avoidance
from _outreach_core import captcha as core_captcha
from _outreach_core import content_guard as core_content_guard
from _outreach_core import contact_url as core_contact_url
from _outreach_core import outcomes as core_outcomes
from _outreach_core import resolve_queue as core_resolve_queue
from _outreach_core import run_progress as core_progress
from _outreach_core import run_supervisor as core_run_supervisor
from _outreach_core import send_journal as core_send_journal
from _outreach_core import send_state as core_send_state
from _outreach_core import send_timeline as core_timeline
from _outreach_core import target_lint as core_target_lint
from _outreach_core import submit_progress as core_submit_progress
from _outreach_core import tab_utils as core_tab_utils
from _outreach_core import target_state as core_target_state
from _outreach_core import wizard as core_wizard
from _outreach_core import infer as core_infer
from _outreach_core import preview as core_preview
from _outreach_core import prompt as core_prompt
from _outreach_core.config import BriefError, load_merged_config as core_load_merged_config
from _outreach_core.paths import SkillPaths, resolve_skill_paths
from _outreach_core.progress import HeartbeatSession, resolve_heartbeat_mode
from _outreach_core.verify import (
    append_needs_attention,
    close_needs_attention,
    handle_verify_result,
    list_open_needs_attention,
    verify_send_completed,
)

try:
    import yaml
except ImportError:
    print("Missing dependency: pyyaml. Install with `pip install pyyaml`", file=sys.stderr)
    sys.exit(1)

SKILL_DIR = Path(__file__).resolve().parent
_PATHS: SkillPaths | None = None
BRIEF_ID = ""
PERSONA_ID: str | None = None
DATA_DIR = SKILL_DIR / "data"
PROMPTS_DIR = SKILL_DIR / "prompts"

DEFAULT_MODEL = core_infer.DEFAULT_MODEL
BROWSER_PROFILE = core_infer.BROWSER_PROFILE
RATE_LIMIT_SECONDS = 4
DEFAULT_PER_TARGET_TIMEOUT_SEC = 300
DEFAULT_SEND_LEAD_SOFT_TIMEOUT_SEC = DEFAULT_PER_TARGET_TIMEOUT_SEC

SKIP_HISTORY_PATH = DATA_DIR / "skip_history.jsonl"
SENT_HISTORY_PATH = DATA_DIR / "sent_history.jsonl"


def load_merged_config(skill_dir: Path, brief_id: str | None = None) -> dict[str, Any]:
    """Load this channel's campaign + selected persona configuration."""
    return core_load_merged_config(
        skill_dir,
        brief_id,
        persona_id=PERSONA_ID,
        channel="jp_form",
    )


def configure_brief(
    brief_id: str | None,
    *,
    persona_id: str | None = None,
    cmd: str = "",
) -> SkillPaths:
    """Resolve brief, data/briefs/<id>/, targets/<id>.yaml, and prompt overrides."""
    global _PATHS, BRIEF_ID, PERSONA_ID, DATA_DIR, PROMPTS_DIR, SKIP_HISTORY_PATH, SENT_HISTORY_PATH
    _PATHS = resolve_skill_paths(SKILL_DIR, brief_id, channel="jp_form")
    BRIEF_ID = _PATHS.brief_id
    PERSONA_ID = persona_id
    DATA_DIR = _PATHS.data_dir
    SKIP_HISTORY_PATH = DATA_DIR / "skip_history.jsonl"
    SENT_HISTORY_PATH = DATA_DIR / "sent_history.jsonl"
    try:
        cfg = load_merged_config(SKILL_DIR, BRIEF_ID)
        PERSONA_ID = cfg.get("_persona_id")
        PROMPTS_DIR = core_prompt.resolve_prompts_dir(SKILL_DIR, cfg, channel="jp_form")
    except (FileNotFoundError, BriefError):
        PROMPTS_DIR = SKILL_DIR / "prompts"
    if cmd in ("campaign", "bootstrap", "send", "draft", "enrich", "preview"):
        print(f"[{cmd}] brief={BRIEF_ID} · persona={PERSONA_ID or 'legacy-inline'} · channel=jp_form")
    return _PATHS


def _data_path(arg: str | None, name: str) -> Path:
    return Path(arg) if arg else DATA_DIR / name


# ============================================================================
# History helpers (mirrors linkedin-outreach pattern)
# ============================================================================

def load_skip_set() -> set[str]:
    return core_history.load_skip_set(DATA_DIR)


def load_sent_set() -> set[str]:
    return core_history.load_sent_set(DATA_DIR)


def append_skip_history(skipped_drafts: list[dict[str, Any]]) -> None:
    core_history.append_skip_history(
        skipped_drafts,
        DATA_DIR,
        extra_fields=("name", "industry"),
    )


def append_sent_history(sent_drafts: list[dict[str, Any]]) -> None:
    core_history.append_sent_history(
        sent_drafts,
        DATA_DIR,
        extra_fields=("name", "industry", "form_url"),
    )
    try:
        _sync_targets_sent_status(sent_drafts)
    except Exception as exc:  # history remains the source of truth on sync failure
        print(f"[sent-history] ⚠ targets YAML status sync failed: {exc}", file=sys.stderr)


def _sync_targets_sent_status(
    sent_drafts: list[dict[str, Any]],
    *,
    targets_path: Path | None = None,
    sent_at: str | None = None,
) -> int:
    """Atomically mirror confirmed sends into ``targets/<brief>.yaml``.

    ``sent_history.jsonl`` remains the append-only delivery source of truth, but
    keeping the curated YAML status aligned prevents operators/agents from
    reporting confirmed sends as still pending and reduces duplicate-send risk.
    """
    path = targets_path or (_PATHS.targets_path if _PATHS is not None else None)
    if not sent_drafts or path is None or not path.is_file():
        return 0
    ids = {str(d.get("id") or "") for d in sent_drafts if d.get("id") is not None}
    if not ids:
        return 0
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return 0
    companies = raw.get("companies") or []
    if not isinstance(companies, list):
        return 0
    stamp = sent_at or datetime.utcnow().isoformat() + "Z"
    changed = 0
    for company in companies:
        if not isinstance(company, dict) or str(company.get("id") or "") not in ids:
            continue
        if company.get("status") != "sent" or not company.get("sent_at"):
            company["status"] = "sent"
            company["sent_at"] = stamp
            changed += 1
    if not changed:
        return 0
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    print(f"[sent-history] synced {changed} target status(es) -> {path.name}")
    return changed


def stage_history(action: str) -> None:
    if action == "needs-attention":
        rows = list_open_needs_attention(DATA_DIR)
        print(f"needs_attention.jsonl: {len(rows)} open")
        for e in rows[-20:]:
            fields = e.get("unresolved_fields") or []
            fl = ", ".join(
                f"{f.get('label') or f.get('name')}" for f in fields[:3] if isinstance(f, dict)
            )
            print(f"  - {e.get('target_id')}: {e.get('name')} | {e.get('reason', '')[:60]} | {fl}")
        return
    if action == "show":
        skip_n = sum(1 for _ in SKIP_HISTORY_PATH.open()) if SKIP_HISTORY_PATH.exists() else 0
        sent_n = sum(1 for _ in SENT_HISTORY_PATH.open()) if SENT_HISTORY_PATH.exists() else 0
        print(f"skip_history.jsonl: {skip_n} entries  ({SKIP_HISTORY_PATH})")
        print(f"sent_history.jsonl: {sent_n} entries  ({SENT_HISTORY_PATH})")
        if sent_n:
            print("\nMost recent sends:")
            entries = [json.loads(l) for l in SENT_HISTORY_PATH.open() if l.strip()]
            for e in entries[-10:]:
                print(f"  - {e.get('name', '?'):<40} | {e.get('sent_at', '')}")
        return
    if action == "purge-skip":
        SKIP_HISTORY_PATH.unlink(missing_ok=True)
        print(f"deleted {SKIP_HISTORY_PATH}")
        return
    if action == "purge-sent":
        SENT_HISTORY_PATH.unlink(missing_ok=True)
        print(f"deleted {SENT_HISTORY_PATH}")
        return
    if action == "purge-all":
        SKIP_HISTORY_PATH.unlink(missing_ok=True)
        SENT_HISTORY_PATH.unlink(missing_ok=True)
        print(f"deleted {SKIP_HISTORY_PATH} and {SENT_HISTORY_PATH}")
        return


# ============================================================================
# OpenClaw subprocess helpers
# ============================================================================

oc_infer = core_infer.oc_infer

# v21: all browser I/O routes through the selected BrowserAdapter (default
# "openclaw" → byte-identical to the historical oc_* subprocess calls; opt into
# Playwright with DOORMAN_BROWSER_BACKEND=playwright). The call sites below
# (oc_browser / _evaluate / tab helpers) are unchanged for the rest of run.py.
from _outreach_core import adapters as core_adapters


def oc_browser(*args: str, **_kwargs: Any) -> str | None:
    """Browser verb (open/snapshot/screenshot/focus/close) via the active adapter.
    ``profile`` kwarg is accepted for call-site compatibility and ignored (the
    adapter owns the profile)."""
    return core_adapters.get_browser().browser(*args)


def _evaluate(js: str) -> Any:
    """Browser evaluate via the active BrowserAdapter (no LLM)."""
    return core_adapters.get_browser().evaluate(js)


# ============================================================================
# Stage: bootstrap (load targets from targets.yaml)
# ============================================================================

# --- v15 §L1: bootstrap-time URL validation + domain dedup -------------------
def _bootstrap_url_issue(c: dict[str, Any]) -> str | None:
    """'invalid_url' when form_url is present but malformed (no scheme/netloc)."""
    url = str(c.get("form_url") or "").strip()
    if not url:
        return None  # no URL is fine — enrich uses contact_url_candidates
    from _outreach_core.contact_url import _normalize_http_url

    return None if _normalize_http_url(url) else "invalid_url"


def _dedup_key_for_url(url: str) -> str:
    """v31 §WS1a — dedup key for one URL.

    Registrable domain, EXCEPT on hosted-form services (forms.gle, form.run,
    HubSpot, …) where different companies legitimately share the domain —
    there the full normalized URL is the key, so one Google-Forms target in
    history no longer filters every future one.
    """
    if not url:
        return ""
    if core_contact_url.is_form_service_url(url):
        from _outreach_core.contact_url import _normalize_http_url
        return _normalize_http_url(url) or ""
    return core_contact_url.registrable_domain(url)


def _bootstrap_domain(c: dict[str, Any]) -> str:
    """Dedup key for a targets.yaml row (form_url, else first candidate)."""
    url = str(c.get("form_url") or "").strip()
    if not url:
        cands = c.get("contact_url_candidates")
        if isinstance(cands, list) and cands:
            url = str(cands[0] or "").strip()
    return _dedup_key_for_url(url)


def _history_form_url_domains(exclude_ids: set[str] | None = None) -> set[str]:
    """Dedup keys of form_urls already in sent/skip history.

    ``exclude_ids`` (v31 §WS1c): rows for ids whose latest skip was transient
    (e.g. invalid_url) don't contribute — the curator fixed the URL and the
    domain must become eligible again.
    """
    domains: set[str] = set()
    for path in (SENT_HISTORY_PATH, SKIP_HISTORY_PATH):
        try:
            if not path.exists():
                continue
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if exclude_ids and str(row.get("id") or "") in exclude_ids:
                        continue
                    url = str(row.get("form_url") or "").strip()
                    if url:
                        dom = _dedup_key_for_url(url)
                        if dom:
                            domains.add(dom)
        except OSError:
            continue
    return domains


def stage_bootstrap(targets_path: Path, out_path: Path,
                    include_sent: bool = False,
                    include_dropped: bool = False,
                    include_skipped: bool = False,
                    limit: int | None = None,
                    only_ids: list[str] | None = None) -> None:
    """Read curated targets.yaml → write data/leads.jsonl.

    Args:
      limit: cap the number of eligible targets to first N (after status/history filters)
      only_ids: restrict to these specific ids (overrides limit)
    """
    if not targets_path.exists():
        print(f"[bootstrap] targets file not found: {targets_path}", file=sys.stderr)
        print(f"            cp {SKILL_DIR / 'targets.example.yaml'} {targets_path}",
              file=sys.stderr)
        sys.exit(2)

    raw = yaml.safe_load(targets_path.read_text())
    companies = raw.get("companies") or []

    sent_ids = load_sent_set()
    skip_ids = load_skip_set()
    # v31 §WS1c — ids whose LATEST skip was transient (invalid_url) become
    # eligible again: the curator fixed targets.yaml, so neither the id
    # filter nor the history-domain filter may keep excluding them.
    transient_skip_ids = core_history.load_transient_skip_ids(DATA_DIR)
    skip_ids -= transient_skip_ids
    only_id_set = set(only_ids) if only_ids else None

    # v15 §L1: domain-level dedup — against history AND within this batch.
    history_domains = _history_form_url_domains(exclude_ids=transient_skip_ids)
    seen_domains: set[str] = set()

    written: list[dict[str, Any]] = []
    filtered_sent = 0
    filtered_dropped = 0
    filtered_skip = 0
    filtered_only_ids = 0
    filtered_invalid_url = 0
    filtered_dup_domain = 0
    filtered_missing_id = 0
    lint_warnings = 0
    for row_idx, c in enumerate(companies, 1):
        cid = c.get("id") if isinstance(c, dict) else None
        if not cid:
            # v31 §WS1b — id-less rows used to vanish without a trace; the
            # curator had no way to notice a YAML indentation slip.
            filtered_missing_id += 1
            name_hint = str((c or {}).get("name") or "?") if isinstance(c, dict) else "?"
            print(f"[bootstrap] ⚠ row {row_idx} ({name_hint}): missing `id` "
                  f"— skipped (missing_id)")
            continue
        # v31 §WS1d — enum lint (warn-only; never filters).
        for warning in core_target_lint.validate_target_row(c):
            lint_warnings += 1
            print(f"[bootstrap] ⚠ {cid}: {warning}")
        status = (c.get("status") or "pending").lower()

        if only_id_set is not None and cid not in only_id_set:
            filtered_only_ids += 1
            continue
        if status == "dropped" and not include_dropped:
            filtered_dropped += 1
            continue
        if (status == "sent" or cid in sent_ids) and not include_sent:
            filtered_sent += 1
            continue
        if cid in skip_ids and not include_skipped:
            filtered_skip += 1
            continue

        # v15 §L1: URL format validation — a malformed form_url wastes a whole
        # enrich/browser cycle; record as skip so it surfaces in reports.
        issue = _bootstrap_url_issue(c)
        if issue == "invalid_url":
            filtered_invalid_url += 1
            print(f"[bootstrap] ⚠ {cid}: invalid form_url "
                  f"({str(c.get('form_url'))[:80]}) — skipped (invalid_url)")
            append_skip_history([{
                "id": cid, "name": c.get("name"),
                "draft": {"body": f"invalid_url: {str(c.get('form_url'))[:160]}"},
            }])
            continue

        # v15 §L1: registrable-domain dedup (sent/skip history + same batch).
        dom = _bootstrap_domain(c)
        if dom:
            if dom in history_domains and cid not in sent_ids and cid not in skip_ids:
                filtered_dup_domain += 1
                print(f"[bootstrap] ⚠ {cid}: domain {dom} already in "
                      f"sent/skip history — filtered (dup_domain)")
                continue
            if dom in seen_domains:
                filtered_dup_domain += 1
                print(f"[bootstrap] ⚠ {cid}: domain {dom} duplicated within "
                      f"this batch — filtered (dup_domain)")
                continue
            seen_domains.add(dom)

        c.setdefault("_loaded_at", datetime.utcnow().isoformat() + "Z")
        written.append(c)

    # Apply --limit AFTER filters
    capped = False
    if limit is not None and len(written) > limit:
        written = written[:limit]
        capped = True

    with out_path.open("w") as f:
        for c in written:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    msg = f"[bootstrap] wrote {len(written)} targets -> {out_path}"
    drops = []
    if filtered_sent: drops.append(f"{filtered_sent} sent")
    if filtered_dropped: drops.append(f"{filtered_dropped} dropped")
    if filtered_skip: drops.append(f"{filtered_skip} skipped")
    if filtered_invalid_url: drops.append(f"{filtered_invalid_url} invalid_url")
    if filtered_dup_domain: drops.append(f"{filtered_dup_domain} dup_domain")
    if filtered_missing_id: drops.append(f"{filtered_missing_id} missing_id")
    if drops:
        msg += f"  (filtered: {', '.join(drops)})"
    if lint_warnings:
        msg += f"  [{lint_warnings} lint warning(s)]"
    if capped:
        msg += f"  [limited to first {limit}]"
    if only_id_set:
        msg += f"  [restricted to ids: {','.join(sorted(only_id_set))}]"
    print(msg)
    # v31 §WS1b — a silently-shrinking list is the kind of divergence the
    # operator can only catch if it reaches Slack. Best-effort.
    if filtered_missing_id:
        try:
            from _outreach_core.notify import post as _notify_post
            _notify_post(
                f"bootstrap: targets.yaml に id 無しの行が {filtered_missing_id} 件"
                "あり読み込めませんでした。YAML のインデント/入力漏れを確認してください。",
                level="warn",
            )
        except Exception:  # noqa: BLE001 - Slack must never break bootstrap
            pass


# ============================================================================
# Stage: enrich (visit each form URL, parse field structure)
# ============================================================================

_FORM_FIELDS_JS = r"""
() => {
  const pickRoot = () => {
    const forms = [...document.querySelectorAll('form')];
    const withTextarea = forms.filter(f => f.querySelector('textarea'));
    withTextarea.sort((a, b) =>
      b.querySelectorAll('input,select,textarea').length -
      a.querySelectorAll('input,select,textarea').length);
    return withTextarea[0] || forms[0] || document.body;
  };
  const root = pickRoot();

  const getStableSelector = (el, rootEl) => {
    if (el.name) return `[name="${CSS.escape(el.name)}"]`;
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.dataset && el.dataset.qa) return `[data-qa="${el.dataset.qa}"]`;
    const path = [];
    let cur = el;
    while (cur && cur !== rootEl && cur !== document.body) {
      let part = cur.tagName.toLowerCase();
      const sibs = [...(cur.parentElement?.children || [])]
        .filter(s => s.tagName === cur.tagName);
      if (sibs.length > 1) part += `:nth-of-type(${sibs.indexOf(cur) + 1})`;
      path.unshift(part);
      cur = cur.parentElement;
    }
    return path.join(' > ');
  };

  const result = {
    inputs: [],
    selects: [],
    textareas: [],
    radios: {},
    checkboxes: [],
    has_recaptcha_v2: false,
    has_recaptcha_v3: false,
    iframes: [],
    submit_buttons: [],
    notes: []
  };

  const labelFor = (el) => {
    if (el.id) {
      try {
        const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (lbl) return lbl.textContent.trim();
      } catch (e) {}
    }
    const wrap = el.closest('label');
    if (wrap) return wrap.textContent.trim();
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return aria.trim();
    {
      let sib = el.previousSibling;
      while (sib) {
        if (sib.nodeType === 3) {
          const txt = sib.textContent.trim();
          if (txt && txt !== '-' && txt !== '−' && txt !== 'ー' && txt !== '/' && txt !== '／') {
            return txt;
          }
        } else if (sib.nodeType === 1) {
          const tag = sib.tagName;
          if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'BUTTON') break;
          if (tag !== 'BR') {
            const txt = (sib.textContent || '').trim();
            if (txt) return txt;
          }
        }
        sib = sib.previousSibling;
      }
    }
    const row = el.closest('tr, .form-row, .field, .input, .item, dl');
    if (row) {
      const lbl = row.querySelector('th, label, .label, [class*="label" i], strong, dt, .title');
      if (lbl && !lbl.contains(el)) return lbl.textContent.trim();
    }
    const tr = el.closest('tr');
    if (tr) {
      let prev = tr.previousElementSibling;
      for (let i = 0; prev && i < 3; i++) {
        if (prev.querySelector('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="image"]), textarea, select')) break;
        const txt = (prev.textContent || '').replace(/\s+/g, ' ').trim();
        if (txt) return txt;
        prev = prev.previousElementSibling;
      }
    }
    // Walk up the DOM looking for a sibling/preceding label-ish element
    let cur = el.parentElement;
    for (let i = 0; i < 4 && cur; i++, cur = cur.parentElement) {
      const lbl = cur.querySelector('label, .label, [class*="label" i], strong, th, dt');
      if (lbl && lbl !== el) return lbl.textContent.trim();
    }
    return null;
  };

  for (const el of root.querySelectorAll('input,textarea,select')) {
    const tag = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();
    if (type === 'hidden' || type === 'submit' || type === 'button' || type === 'image') continue;

    const name = el.name || el.id || '';
    const selector = getStableSelector(el, root);
    const placeholder = el.placeholder || '';
    const required = el.required || el.getAttribute('aria-required') === 'true';
    const maxLength = el.maxLength > 0 ? el.maxLength : null;
    const label = labelFor(el);

    if (type === 'radio') {
      const group = name || 'unnamed';
      result.radios[group] = result.radios[group] || [];
      result.radios[group].push({
        selector: selector,
        value: el.value,
        label: label,
        checked: el.checked,
        required: required,
        disabled: !!el.disabled
      });
      continue;
    }
    if (type === 'checkbox') {
      result.checkboxes.push({
        name: name, selector: selector, label: label, value: el.value,
        checked: el.checked, required: required, disabled: !!el.disabled
      });
      continue;
    }
    if (tag === 'select') {
      result.selects.push({
        name: name, selector: selector, label: label, required: required,
        options: Array.from(el.options).map(o => o.text).slice(0, 60)
      });
      continue;
    }
    if (tag === 'textarea') {
      result.textareas.push({
        name: name, selector: selector, label: label, required: required,
        max_length: maxLength, placeholder: placeholder
      });
      continue;
    }
    // Standard input
    result.inputs.push({
      name: name, selector: selector, label: label, required: required, type: type,
      max_length: maxLength, placeholder: placeholder
    });
  }

  // reCAPTCHA detection
  if (document.querySelector('.g-recaptcha:not([data-size="invisible"])')) {
    result.has_recaptcha_v2 = true;
  }
  if (document.querySelector('iframe[src*="recaptcha"]')
      || document.querySelector('script[src*="recaptcha"]')) {
    result.has_recaptcha_v3 = true;
  }
  // Suppress v3 if v2 is also present
  if (result.has_recaptcha_v2) result.has_recaptcha_v3 = false;

  // iframes (for forms hosted inside third-party widgets)
  for (const f of document.querySelectorAll('iframe')) {
    const src = f.src || '';
    if (!src) continue;
    if (src.includes('google.com/recaptcha')) continue;
    if (src.includes('googletagmanager')) continue;
    result.iframes.push({ src: src.split('?')[0] });
  }

  // submit-like buttons
  for (const b of document.querySelectorAll('button, input[type="submit"], [role="button"]')) {
    const txt = (b.textContent || b.value || '').trim();
    if (!txt) continue;
    if (/送信|確認|内容|入力|お問い合わせ|問合せ|submit|send/i.test(txt)) {
      result.submit_buttons.push({
        text: txt.slice(0, 40),
        disabled: !!b.disabled
      });
    }
  }

  let form_root_selector = null;
  if (root && root.tagName === 'FORM') {
    if (root.id) form_root_selector = `#${CSS.escape(root.id)}`;
    else if (root.name) form_root_selector = `form[name="${CSS.escape(root.name)}"]`;
    else {
      const forms = [...document.querySelectorAll('form')];
      const idx = forms.indexOf(root);
      if (idx >= 0) form_root_selector = `form:nth-of-type(${idx + 1})`;
    }
  }
  result.form_root_selector = form_root_selector;

  return result;
}
"""

_PAGE_LINKS_JS = r"""
() => {
  const out = [];
  for (const el of document.querySelectorAll('a[href], button, [role="button"]')) {
    const href = (el.getAttribute('href') || '').trim();
    const txt = (el.textContent || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    if (!href && !txt) continue;
    out.push({ href: href, text: txt.slice(0, 120) });
  }
  return out.slice(0, 500);
}
"""


def _emit_event(kind: str, *, stage: str, target_id: str | None = None, **kwargs: Any) -> None:
    from _outreach_core import events as ev

    ev.emit(kind, stage=stage, target_id=target_id, **kwargs)


# ---------------------------------------------------------------------------
# v25: stdout heartbeat — CLI gateway watchdog の no-output stall (180s) 対策。
# ブラウザ操作・LLM待ちなどで長時間 stdout が無音になると watchdog がプロセスを
# 殺すため、長時間コマンド実行中は定期的に生存ログを flush 付きで出力する。
# ---------------------------------------------------------------------------
_HB_STATE: dict[str, Any] = {"label": "", "t0": 0.0}


def _hb_stage(label: str) -> None:
    """現在の処理ステージをハートビート表示用に更新する。"""
    _HB_STATE["label"] = label


def _start_stdout_heartbeat(interval: float = 60.0) -> None:
    """Daemon thread that prints a liveness line every ``interval`` seconds."""
    import threading

    if _HB_STATE.get("_started"):
        return
    _HB_STATE["_started"] = True
    _HB_STATE["t0"] = time.time()

    def _beat() -> None:
        while True:
            time.sleep(interval)
            elapsed = int(time.time() - float(_HB_STATE.get("t0") or time.time()))
            label = str(_HB_STATE.get("label") or "working")
            print(f"[heartbeat] alive · {label} · elapsed={elapsed}s", flush=True)

    threading.Thread(target=_beat, daemon=True, name="stdout-heartbeat").start()


class LeadSoftTimeoutError(TimeoutError):
    """Raised when one target spends too long inside the send pipeline."""


def _send_lead_soft_timeout_sec(config: dict[str, Any] | None) -> int:
    """Per-target send timeout.

    Workaround for browser/site/CLI stalls: if one company gets wedged, route
    that company to needs_attention and continue the batch. Override with
    ``DOORMAN_SEND_LEAD_TIMEOUT_SEC`` or ``execution.per_target_timeout_sec``.
    ``send.lead_soft_timeout_sec`` is still accepted for existing briefs. Set
    <=0 to disable.
    """
    raw: Any = os.environ.get("DOORMAN_SEND_LEAD_TIMEOUT_SEC", "").strip()
    exec_cfg = (config or {}).get("execution") or {}
    send_cfg = (config or {}).get("send") or {}
    if raw == "":
        raw = exec_cfg.get("per_target_timeout_sec")
    if raw in (None, ""):
        raw = send_cfg.get("lead_soft_timeout_sec")
    if raw in (None, ""):
        return DEFAULT_SEND_LEAD_SOFT_TIMEOUT_SEC
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_SEND_LEAD_SOFT_TIMEOUT_SEC


@contextlib.contextmanager
def _lead_soft_timeout(seconds: int, *, target_id: str) -> Any:
    """Interrupt a single target if it exceeds ``seconds``.

    ``subprocess.run`` kills its child on exceptions raised while waiting, so
    this also prevents a wedged OpenClaw subprocess from surviving the timeout.
    """
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        yield
        return

    started_at = time.time()

    def _raise_timeout(_signum: int, _frame: Any) -> None:
        if not core_run_supervisor.should_abort_target(started_at, time.time(), seconds):
            return
        raise LeadSoftTimeoutError(
            f"lead_soft_timeout after {seconds}s (target_id={target_id})"
        )

    prev_handler = signal.getsignal(signal.SIGALRM)
    prev_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev_handler)
        if prev_timer and prev_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, prev_timer[0], prev_timer[1])


def _list_page_links() -> list[dict[str, str]]:
    res = _evaluate(_PAGE_LINKS_JS)
    return res if isinstance(res, list) else []


# v15 §F5: real HTTP status of the current page (null when API unavailable)
_HTTP_STATUS_JS = r"""
() => {
  try {
    const e = performance.getEntriesByType('navigation')[0];
    return (e && typeof e.responseStatus === 'number') ? e.responseStatus : null;
  } catch (err) { return null; }
}
"""

_FETCH_SITEMAP_JS = r"""
() => fetch('/sitemap.xml', {cache: 'no-store'})
        .then(r => r.ok ? r.text() : '')
        .catch(() => '')
"""


def _current_http_status() -> int | None:
    res = _evaluate(_HTTP_STATUS_JS)
    return res if isinstance(res, int) else None


def _form_fields_empty(fields: dict[str, Any] | None) -> bool:
    f = fields or {}
    return not (
        (f.get("inputs") or [])
        or (f.get("selects") or [])
        or (f.get("textareas") or [])
    )


def _sitemap_contact_candidates() -> list[str]:
    """v15 §F2: mine /sitemap.xml of the CURRENT page's origin for contact URLs."""
    try:
        xml = _evaluate(_FETCH_SITEMAP_JS)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(xml, str) or not xml.strip():
        return []
    return core_contact_url.extract_contact_urls_from_sitemap(xml)[:5]


def _seed_form_urls(target: dict[str, Any]) -> list[str]:
    seeds: list[str] = []
    direct = str(target.get("form_url") or "").strip()
    if direct:
        seeds.append(direct)
    cands = target.get("contact_url_candidates")
    if isinstance(cands, list):
        seeds.extend(str(c or "").strip() for c in cands if str(c or "").strip())
    # Same-domain guard: when form_url exists, keep only that registrable domain.
    # (For no form_url, fallback to first candidate's domain.)
    anchor = direct or (seeds[0] if seeds else "")
    if anchor:
        seeds = [s for s in seeds if core_contact_url.same_registrable_domain(s, anchor)]

    # dedupe while preserving order
    out: list[str] = []
    seen: set[str] = set()
    for s in seeds:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _build_contact_candidates(target: dict[str, Any], current_url: str, page_links: list[dict[str, str]]) -> list[str]:
    candidates: list[str] = []
    user_cands = target.get("contact_url_candidates")
    if isinstance(user_cands, list):
        candidates.extend(str(x) for x in user_cands if str(x or "").strip())
    candidates.extend(core_contact_url.contact_link_candidates(page_links, current_url))
    # v15 §F2: sitemap-mined contact URLs rank above blind common paths.
    candidates.extend(_sitemap_contact_candidates())
    candidates.extend(core_contact_url.common_contact_paths(current_url))

    uniq: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        u = core_contact_url.absolutize_url(c, current_url)
        if not u:
            continue
        if not core_contact_url.same_registrable_domain(u, current_url):
            continue
        if u == current_url or u in seen:
            continue
        seen.add(u)
        uniq.append(u)
    return uniq[:5]


def _classify_form_type(
    fields: dict[str, Any], snapshot: str | None
) -> tuple[str, str | None]:
    return core_contact_url.classify_form_type(fields, snapshot)


def _classify_form_type_v2_for_enrich(
    fields: dict[str, Any],
    snapshot: str | None,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """§F1 two-stage classify via the mockable ``_classify_form_type`` hook.

    Enrich tests patch ``_classify_form_type`` to simulate ambiguous seeds;
    production still gets LLM escalation on uncertain heuristics.
    """
    kind, reason = _classify_form_type(fields, snapshot)
    return core_contact_url._llm_escalate_classification(
        kind,
        reason,
        fields,
        snapshot,
        infer_fn=oc_infer if config else None,
        model=_form_analyzer_base_model(config) if config else "",
    )


def _enrich_one_target(
    t: dict[str, Any],
    i: int,
    total: int,
    config: dict[str, Any] | None,
    enriched: list[dict[str, Any]],
) -> dict[str, Any]:
    """Enrich a single target — extracted per-lead loop body (v15 §R1).

    Appends exactly one row to ``enriched``. Exceptions are isolated by the
    caller so one bad site cannot kill the whole enrich batch.
    """
    _hb_stage(f"enrich {t.get('name') or t.get('id')} ({i}/{total})")
    seed_urls = _seed_form_urls(t)
    if not seed_urls:
        print(f"[enrich] ({i}/{total}) {t.get('name')}: no form_url/contact_url_candidates, skipping")
        enriched.append({**t, "_enrich_skipped": "no form_url"})
        return {"outcome": "skipped"}

    # v25: form_url_locked — ユーザー確認済みURLは唯一のseedとして扱い、
    # 自動URL補正（候補巡回）を一切行わない。
    url_locked = bool(t.get("form_url_locked")) and bool(str(t.get("form_url") or "").strip())
    if url_locked:
        seed_urls = [str(t.get("form_url")).strip()]
        print(f"[enrich] ({i}/{total}) {t.get('name')}: form_url_locked — "
              f"自動URL補正を無効化 ({seed_urls[0]})")

    if t.get("category") in ("b2c_only", "iframe", "site_closed"):
        cat = t.get("category")
        print(f"[enrich] ({i}/{total}) {t.get('name')}: category={cat}, skipping")
        enriched.append({**t, "_enrich_skipped": f"category={cat}"})
        return {"outcome": "skipped"}

    from _outreach_core.cookie_dismiss import apply_cookie_dismiss

    t_work = dict(t)
    fields: dict[str, Any] = {}
    snap = ""
    form_kind = "unknown_no_textarea"
    form_reason = "no form analyzed"
    correction_attempts = 0
    corrected_emitted = False
    chosen_url = ""
    original_url = str(t.get("form_url") or "").strip()
    best_known: dict[str, Any] | None = None

    # Seed candidates: explicit form_url first, then contact_url_candidates.
    for seed_idx, seed_url in enumerate(seed_urls[:5], 1):
        print(f"[enrich] ({i}/{total}) {t.get('name')} -> seed {seed_idx}: {seed_url}")
        oc_browser("open", seed_url)
        time.sleep(RATE_LIMIT_SECONDS)
        apply_cookie_dismiss(
            _evaluate,
            config,
            stage="enrich",
            target_id=t_work.get("id"),
            emit_event=lambda kind, **kw: _emit_event(kind, **kw),
        )
        # Try clicking through any "法人" / "業務提携" entry-point links.
        if t_work.get("entry_click_text"):
            for txt in t_work["entry_click_text"] if isinstance(t_work["entry_click_text"], list) else [t_work["entry_click_text"]]:
                _evaluate(f"""
                    () => {{
                      for (const a of document.querySelectorAll('a, button')) {{
                        if ((a.textContent || '').trim().includes({json.dumps(txt)})) {{
                          a.click(); return true;
                        }}
                      }}
                      return false;
                    }}
                """)
                time.sleep(2)

        fields = _evaluate(_FORM_FIELDS_JS) or {}
        snap = oc_browser("snapshot") or ""
        # v15 §F4: SPA/lazy rendering — when nothing was collected and the page
        # body is thin, wait 3s and rescan ONCE before judging.
        if _form_fields_empty(fields) and len((snap or "").strip()) < 800:
            print(f"[enrich]    ↳ 0 fields + thin page — waiting 3s for JS render")
            time.sleep(3)
            fields = _evaluate(_FORM_FIELDS_JS) or {}
            snap = oc_browser("snapshot") or ""
        # v15 §F5: pass the real HTTP status into error-page detection.
        if core_contact_url.is_error_page(
            snap, url=seed_url, http_status=_current_http_status()
        ):
            _emit_event(
                "enrich.nav.error_page",
                stage="enrich",
                target_id=str(t_work.get("id") or ""),
                payload={"url": seed_url[:200], "where": "seed"},
            )
            print(f"[enrich]    ↳ seed error page detected, skip: {seed_url}")
            continue
        gate_adv = _advance_pre_form_phase(
            t_work, config, stage="enrich", max_rounds=2,
        )
        if gate_adv.get("advanced"):
            fields = gate_adv.get("fields") if isinstance(gate_adv.get("fields"), dict) else fields
            snap = str(gate_adv.get("snap") or snap)
            cur_after_gate = str(_evaluate("() => location.href") or "").strip()
            if cur_after_gate:
                seed_url = cur_after_gate
            print(f"[enrich]    ↳ pre-form gate advanced -> {seed_url}")
        # v15 §F1: two-stage classification — heuristics first, LLM (Sonnet)
        # only when the heuristic verdict is uncertain; deterministic fallback.
        cls = _classify_form_type_v2_for_enrich(fields, snap, config)
        form_kind, form_reason = cls["kind"], cls["reason"]
        llm_hint_url = cls.get("b2b_contact_hint_url")
        if cls.get("llm_called"):
            _emit_event(
                "enrich.classify.llm",
                stage="enrich",
                target_id=str(t_work.get("id") or ""),
                payload={
                    "kind": form_kind,
                    "src": cls.get("src"),
                    "hint_url": (llm_hint_url or "")[:200],
                },
            )
        chosen_url = seed_url
        if form_kind == "contact":
            best_known = {"url": seed_url, "fields": fields, "snap": snap}
            break

        # v15 §F3: hosted-form iframes — enter the iframe src instead of
        # skipping (form.run / HubSpot / Tayori etc., or same-domain embeds).
        ifr_src = core_contact_url.iframe_form_src(fields.get("iframes"), seed_url)
        if ifr_src:
            print(f"[enrich]    ↳ iframe form detected -> open {ifr_src}")
            oc_browser("open", ifr_src)
            time.sleep(RATE_LIMIT_SECONDS)
            f3 = _evaluate(_FORM_FIELDS_JS) or {}
            s3 = oc_browser("snapshot") or ""
            k3, r3 = _classify_form_type(f3, s3)
            _emit_event(
                "enrich.iframe.entered",
                stage="enrich",
                target_id=str(t_work.get("id") or ""),
                payload={"src": ifr_src[:200], "kind": k3},
            )
            if k3 == "contact":
                fields, snap = f3, s3
                form_kind, form_reason = k3, f"iframe:{r3 or 'contact'}"
                chosen_url = ifr_src
                best_known = {"url": ifr_src, "fields": f3, "snap": s3}
                print(f"[enrich]    ✅ iframe form is the contact form -> {ifr_src}")
                break

        if url_locked:
            # v25: ロック中は候補巡回しない — このseedの結果をそのまま採用判定へ。
            break
        links = _list_page_links()
        candidates = _build_contact_candidates(t_work, seed_url, links)
        # v15 §F1: a 法人窓口 link spotted by the LLM jumps the queue.
        if llm_hint_url:
            hint_abs = core_contact_url.absolutize_url(llm_hint_url, seed_url)
            if hint_abs and hint_abs not in candidates and hint_abs != seed_url:
                candidates = [hint_abs] + candidates
        if candidates:
            print(
                f"[enrich] ({i}/{total}) {t_work.get('name')}: "
                f"non-contact ({form_kind}) -> try correcting URL ({len(candidates)} candidates)"
            )
        for cand in candidates:
            correction_attempts += 1
            print(f"[enrich]    ↳ candidate {correction_attempts}/{len(candidates)}: {cand}")
            oc_browser("open", cand)
            time.sleep(RATE_LIMIT_SECONDS)
            apply_cookie_dismiss(
                _evaluate,
                config,
                stage="enrich",
                target_id=t_work.get("id"),
                emit_event=lambda kind, **kw: _emit_event(kind, **kw),
            )
            snap2 = oc_browser("snapshot") or ""
            if core_contact_url.is_error_page(
                snap2, url=cand, http_status=_current_http_status()
            ):
                _emit_event(
                    "enrich.nav.error_page",
                    stage="enrich",
                    target_id=str(t_work.get("id") or ""),
                    payload={"url": cand[:200], "where": "candidate"},
                )
                if best_known and best_known.get("url"):
                    oc_browser("open", str(best_known["url"]))
                    time.sleep(RATE_LIMIT_SECONDS)
                continue
            fields2 = _evaluate(_FORM_FIELDS_JS) or {}
            gate_adv2 = _advance_pre_form_phase(
                t_work, config, stage="enrich", max_rounds=2,
            )
            if gate_adv2.get("advanced"):
                fields2 = gate_adv2.get("fields") if isinstance(gate_adv2.get("fields"), dict) else fields2
                snap2 = str(gate_adv2.get("snap") or snap2)
                cur_after_gate = str(_evaluate("() => location.href") or "").strip()
                if cur_after_gate:
                    cand = cur_after_gate
                print(f"[enrich]    ↳ candidate pre-form gate advanced -> {cand}")
            kind2, reason2 = _classify_form_type(fields2, snap2)
            if kind2 == "contact":
                fields, snap = fields2, snap2
                form_kind, form_reason = kind2, reason2
                chosen_url = cand
                best_known = {"url": cand, "fields": fields2, "snap": snap2}
                _emit_event(
                    "enrich.form.url_corrected",
                    stage="enrich",
                    target_id=str(t_work.get("id") or ""),
                    payload={
                        "original_url": (seed_url or "")[:200],
                        "corrected_url": cand[:200],
                        "attempt_no": correction_attempts,
                    },
                )
                corrected_emitted = True
                print(f"[enrich]    ✅ corrected form_url -> {cand}")
                break
            if best_known and best_known.get("url"):
                oc_browser("open", str(best_known["url"]))
                time.sleep(RATE_LIMIT_SECONDS)
        if form_kind == "contact":
            break

    if form_kind != "contact" and url_locked and not _form_fields_empty(fields):
        # v25: ユーザー確認済みURLは分類器の判定より優先する（ただしフォーム
        # 要素が実在する場合のみ）。誤分類でユーザー指定窓口を捨てない。
        print(f"[enrich] ({i}/{total}) {t_work.get('name')}: "
              f"分類={form_kind} だが form_url_locked のためユーザー確認済みURLを信頼")
        chosen_url = chosen_url or seed_urls[0]
        form_kind = "contact"
        form_reason = "form_url_locked_user_confirmed"

    if form_kind != "contact" and best_known:
        chosen_url = str(best_known.get("url") or chosen_url)
        fields = dict(best_known.get("fields") or fields)
        snap = str(best_known.get("snap") or snap)
        form_kind = "contact"
        form_reason = "best_known_good_contact"

    t_work["form_url"] = chosen_url or (seed_urls[0] if seed_urls else "")
    if original_url and chosen_url and chosen_url != original_url:
        t_work["form_url_original"] = original_url
        t_work["form_url_corrected"] = True
    elif not original_url and chosen_url:
        t_work["form_url_original"] = ""
        t_work["form_url_corrected"] = True
    if t_work.get("form_url_corrected") and not corrected_emitted:
        _emit_event(
            "enrich.form.url_corrected",
            stage="enrich",
            target_id=str(t_work.get("id") or ""),
            payload={
                "original_url": original_url[:200],
                "corrected_url": str(chosen_url or "")[:200],
                "attempt_no": correction_attempts,
            },
        )

    # Save first form snapshot for debugging
    if i == 1:
        sample = DATA_DIR / "sample_form.txt"
        if snap:
            try:
                sample.write_text(snap)
                print(f"[enrich] saved first form snapshot -> {sample}")
            except OSError as exc:
                print(f"[enrich] ⚠ could not save sample snapshot ({exc}) — continuing")

    if form_kind != "contact":
        reason = form_reason or "non_contact"
        if correction_attempts:
            reason = f"{reason} (correction_attempts={correction_attempts}, all_failed)"
        print(
            f"[enrich] ({i}/{total}) {t_work.get('name')}: "
            f"NON-CONTACT form ({form_kind}: {reason}) — adding to skip_history"
        )
        append_skip_history([
            {
                "id": t_work.get("id"),
                "name": t_work.get("name"),
                "industry": t_work.get("industry"),
                "draft": {
                    "body": f"non_contact_form: {form_kind} ({reason})"
                },
            }
        ])
        enriched.append(
            {
                **t_work,
                "_enrich_skipped": f"non_contact_form:{form_kind}",
                "_enrich_skip_reason": reason,
            }
        )
        _emit_event(
            "enrich.form.skipped_non_contact",
            stage="enrich",
            target_id=str(t_work.get("id") or ""),
            payload={"kind": form_kind, "reason": reason, "correction_attempts": correction_attempts},
        )
        return {"outcome": "skipped"}

    # v25: メール確認コード(OTP)ゲート検出 — フェリシモ型の「確認コード(6桁)を
    # 送信→入力」フローはパイプラインでメールを受信できないため自動送信不可。
    # 送信ステージで「フォーム消失」として失敗する前に、enrich 段階で manual 化。
    otp = core_contact_url.detect_email_verification(snap, fields)
    if otp.get("detected"):
        reason = f"email_verification_required: {', '.join(otp.get('evidence') or [])}"
        print(
            f"[enrich] ({i}/{total}) {t_work.get('name')}: "
            f"OTP/メール確認コード方式を検出 — 自動送信不可のため manual 判定 ({reason})"
        )
        enriched.append(
            {
                **t_work,
                "status": "manual",
                "blocker": "email_verification_code",
                "_enrich_skipped": "email_verification_required",
                "_enrich_skip_reason": reason,
            }
        )
        _emit_event(
            "enrich.form.email_verification_detected",
            stage="enrich",
            target_id=str(t_work.get("id") or ""),
            payload={"evidence": otp.get("evidence"), "url": str(t_work.get("form_url") or "")[:200]},
        )
        return {"outcome": "skipped"}

    inquiry_fields = _extract_inquiry_type_fields(fields)
    if inquiry_fields:
        probe_plan = None
        if config:
            probe = {
                "id": t_work.get("id"),
                "name": t_work.get("name"),
                "form_fields": fields,
                "field_map_overrides": t_work.get("field_map_overrides", {}) or {},
            }
            probe_plan = _llm_analyze_form(probe, config, body_max_chars=400)
        sel = _summarize_inquiry_type_selection(inquiry_fields, probe_plan)
        if sel["count"] > 0:
            _emit_event(
                "enrich.inquiry_type_selected",
                stage="enrich",
                target_id=str(t_work.get("id") or ""),
                payload={
                    "count": sel["count"],
                    "items": sel["items"],
                    "confidence_counts": sel["confidence_counts"],
                    "src_counts": sel["src_counts"],
                },
            )
        llm_no_b2b, fallback_no_b2b, no_b2b = _inquiry_type_no_b2b_flags(inquiry_fields, probe_plan)
        if no_b2b:
            reason = "no_b2b_inquiry_type"
            print(
                f"[enrich] ({i}/{total}) {t_work.get('name')}: "
                f"screen_skip ({reason})"
            )
            enriched.append(
                {
                    **t_work,
                    "_enrich_skipped": "screen_skip",
                    "_enrich_skip_reason": reason,
                }
            )
            _emit_event(
                "enrich.form.screen_skipped",
                stage="enrich",
                target_id=str(t_work.get("id") or ""),
                payload={
                    "reason": reason,
                    "llm_no_b2b": llm_no_b2b,
                    "fallback_no_b2b": fallback_no_b2b,
                },
            )
            return {"outcome": "skipped"}

    if fields.get("form_root_selector"):
        t_work = {**t_work, "form_root_selector": fields["form_root_selector"]}
    field_count = (
        len(fields.get("inputs") or [])
        + len(fields.get("selects") or [])
        + len(fields.get("textareas") or [])
    )
    max_chars = 0
    for ta in fields.get("textareas") or []:
        ml = (ta or {}).get("max_length")
        if ml:
            max_chars = max(max_chars, int(ml))
    tid = str(t_work.get("id") or t_work.get("name") or i)
    trace = None
    from _outreach_core import events as ev

    if ev.get_context().data_dir:
        trace = ev.trace_dir_for(tid)
        ev.dump_trace(trace, "form_snapshot_pre.txt", snap or "", sender=None)
    _emit_event(
        "enrich.form.completed",
        stage="enrich",
        target_id=tid,
        payload={
            "field_count": field_count,
            "has_captcha": bool(fields.get("has_recaptcha_v2")),
            "detected_max_chars": max_chars or None,
        },
        trace_dir=trace,
    )
    enriched_entry = {
        **t_work,
        "form_fields": fields,
        "_enriched_at": datetime.utcnow().isoformat() + "Z",
    }
    enriched.append(enriched_entry)
    return {"outcome": "enriched"}



def stage_enrich(
    input_path: Path,
    out_path: Path,
    config: dict[str, Any] | None = None,
    limit: int | None = None,
) -> None:
    with input_path.open(encoding="utf-8") as f:
        targets = [json.loads(l) for l in f]
    if limit is not None and len(targets) > limit:
        print(f"[enrich] --limit {limit} applied (all {len(targets)} targets)")
        targets = targets[:limit]
    print(f"[enrich] {len(targets)} targets to enrich")

    enriched: list[dict[str, Any]] = []
    total = len(targets)
    for i, t in enumerate(targets, 1):
        try:
            _enrich_one_target(t, i, total, config, enriched)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 — per-lead isolation (v15 §R1)
            tb = traceback.format_exc()
            print(f"[enrich] ✗ ({i}/{total}) {t.get('name')}: crashed: {exc} — continuing",
                  file=sys.stderr)
            try:
                _emit_event(
                    "enrich.lead_crashed", stage="enrich",
                    target_id=str(t.get("id") or ""),
                    payload={"error": str(exc)[:200], "tb_tail": tb[-800:]},
                )
            except Exception:
                pass
            enriched.append({
                **t,
                "_enrich_skipped": "lead_crashed",
                "_enrich_skip_reason": str(exc)[:200],
            })

    with out_path.open("w") as f:
        for e in enriched:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"[enrich] wrote {len(enriched)} enriched targets -> {out_path}")


# ============================================================================
# Stage: draft (Claude inference per target)
# ============================================================================

def build_system_block(config: dict[str, Any]) -> str:
    return core_prompt.build_system_block(config, PROMPTS_DIR)


_INQUIRY_LABEL_RE = re.compile(
    r"(お問い合わせ種別|問合せ種別|問い合わせ区分|お問い合わせ内容|カテゴリ|区分|ご用件|種別)",
)


def _form_constraints_block(target: dict[str, Any]) -> str:
    """v15 §L2 — surface the form's hard constraints AT GENERATION TIME.

    Knowing the textarea maxlength and the inquiry-type choices up front lets
    the model write copy that fits the form, instead of being truncated later.
    """
    ff = target.get("form_fields") or {}
    lines: list[str] = []
    max_len = 0
    for ta in ff.get("textareas") or []:
        if isinstance(ta, dict) and ta.get("max_length"):
            max_len = max(max_len, int(ta["max_length"]))
    if max_len:
        lines.append(f"- 本文 textarea maxlength: {max_len} 文字（厳守）")
    for sel in (ff.get("selects") or [])[:6]:
        if not isinstance(sel, dict):
            continue
        label = str(sel.get("label") or sel.get("name") or "")
        if _INQUIRY_LABEL_RE.search(label):
            opts = [str(o) for o in (sel.get("options") or [])[:15]]
            if opts:
                lines.append(f"- お問い合わせ種別の選択肢（{label}）: {' / '.join(opts)}")
    radios = ff.get("radios") or {}
    if isinstance(radios, dict):
        for gname, opts in list(radios.items())[:4]:
            labels = [str((o or {}).get("label") or "") for o in (opts or []) if isinstance(o, dict)]
            labels = [x for x in labels if x]
            if labels and _INQUIRY_LABEL_RE.search(" ".join(labels) + gname):
                lines.append(f"- ラジオ選択肢（{gname}）: {' / '.join(labels[:10])}")
    if not lines:
        return ""
    return (
        "## Form constraints (write copy that fits this form)\n"
        + "\n".join(lines)
        + "\n\n"
    )


def build_user_block(target: dict[str, Any], max_chars: int) -> str:
    # Strip enrichment internals — pass only what the LLM needs to write copy
    payload_keys = (
        "id", "name", "industry", "founded", "category", "char_limit",
        "direct_signals", "hook_context", "hypothesized_pain",
        "field_map_overrides", "notes"
    )
    payload = {k: target[k] for k in payload_keys if k in target}
    return (
        "<user>\n"
        "Generate a personalized B2B inquiry-form message for the following target company.\n"
        f"Output strictly as JSON: {{\"subject\": \"...\", \"body\": \"...\"}}\n"
        f"`body` must be ≤ {max_chars} characters.\n"
        "If hook_context / direct_signals are too thin, output "
        "{\"subject\": \"SKIP\", \"body\": \"INSUFFICIENT_DATA: <reason>\"}.\n"
        "If category is b2c_only / iframe / site_closed, also output SKIP.\n\n"
        + _form_constraints_block(target)
        + "## Target\n"
        "```json\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "```\n"
        "</user>\n"
    )


extract_first_json = core_prompt.extract_first_json


_REFINE_PROMPT_TEMPLATE = """You wrote the draft below for a Japanese B2B inquiry-form message.
Now act as a tough senior copywriter and **critique then rewrite** it,
**strictly following the persona spec embedded below** (this is persona v6).

## Persona spec (MUST follow — this is the source of truth)

{persona}

## Target context (the company you're writing to)
```json
{target_json}```

## Your draft
```json
{draft_json}```

## Critique checklist (apply strictly — persona-v6 enforcement)

**ハード制約（違反したら必ず書き直す）:**

A. **5セクション構成の保全** — 必ず次の順で構成されているか:
   Section 1: 挨拶+名乗り（会社名・氏名・役職＋何をやっているか） /
   Section 2: 相手の現状の行動・方向性の説明（観察のみ・課題なし） /
   Section 3: その中にある課題の提示（仮説語尾） /
   Section 4: 解決策の提示+実績ブリッジ / Section 5: 定型クロージング
   → セクション欠落・順序入れ替えがあれば書き直す。特に
     (1) 文書の先頭が名乗りでない（フック・固有事実開き）、
     (2) Section 2 に課題が混ざっている、
     (3) Section 4（実績ブリッジ）と Section 5（定型クロージング）の欠落、
     を厳しく検知。

B. **Section 5 の定型文（一字一句保つ）** — 以下が **そのまま** 含まれているか:
   - 「よろしければご提案のお時間をいただけないでしょうか。」
   - 「ご多忙の折、誠に恐縮ですが、何卒よろしくお願い申し上げます。」
   - 末尾の「カレンダー：https://example.com/your-booking-url」
   → どれか欠けていたら **絶対に追加して書き直す**。URL 単独貼り（ラベルなし）も NG。
   旧定型「よろしければ一度、オンラインでご提案のお時間を頂けないでしょうか。」が
   あれば新定型に置換する。

C. **質問形 CTA 禁止（NG #1）** — 本文に疑問符（？）または「〜いただけますか」
   「〜どうされていますか」「〜聞かせてください」のような問い掛けが含まれていないか
   → 含まれていたら削除して Section 5 の定型に置換する。

D. **「どうぞ」「ぜひどうぞ」「お声がけください」などの軽い口語 CTA 禁止（NG #2）**
   → Section 5 の定型に置換。

E. **断定回避（NG #3）** — 「〜になっている」「〜が課題である」「効きます」「刺さる」等の
   断定的な指摘がないか → 「〜ではないかと感じております」「〜論点と見受けられます」等に置換。

F. **NG ジャーゴン（NG #4）** — 「両輪」「ど真ん中」「直球」「ストライク」「レバー」「穴」
   「刺さる」「効く」「食う」「眠っている」「もったいない」「手が届いていない」
   → 全て排除。「論点になり得る」「設計余地がある」等の書き言葉に。

G. **カタカナ業界語 3つ以内（NG #5）** — シナリオ／レバレッジ／クロスセル／LTV／オペレーション／
   セグメント／スキーム／ナレッジ／ナーチャリング／アジェンダ をカウントし、合計が 3 を
   超えたら和語に置換（流れ・段階的なご案内 等）。

H. **英訳調回避（NG #6）** — 「〜することができます→できます」「〜において→で」
   「〜に関して→について」「〜を有しております→がございます」「〜の観点から→の面で」
   「〜の一助となります→お役に立てれば幸いです」。

I. **一文 80字超を避ける（NG #7）** — 80字超の文が 1つでもあれば句点で分割。

J. **「拝察」1回・「拝見」2回まで（NG #8）** — 超過分は「感じております」「見受けられます」等へ。

**自由度のあるチェック（必要に応じて改善）:**

1. Section 2 の現状説明は、相手企業の固有事実（IR数値・店舗数・新サービス等）を
   1〜2点引用しているか → していなければ enriched 情報から補う。
   Section 3 の課題仮説は Section 2 の現状説明と論理的につながっているか。
2. Section 4 は「肩書＋事例コピペ」になっていないか。事例から相手企業の論点への
   ブリッジが 1文添えられているか → なければ追加。
3. 本文は max_chars={max_chars} 以内に収まっているか。超えていたら削る。

## Output format (STRICT JSON)

```json
{{
  "critique": "<2-4 sentence critique of the original draft, in Japanese — どのハード制約に違反していたかを明示>",
  "subject": "<refined subject or null>",
  "body": "<refined body, ≤{max_chars} chars, persona-v6 完全準拠>"
}}
```

ハード制約 A〜J のいずれかに違反していた場合は **必ず** rewrite してください。
本当に元案が persona-v6 完全準拠で改善余地がなければ `critique` を「改善不要」、
subject/body は元のままコピーしてください。

Output only the JSON, no prose."""


def _refine_draft(target: dict[str, Any], draft: dict[str, Any],
                   config: dict[str, Any], max_chars: int) -> dict[str, Any] | None:
    """Second-pass: critique the draft and rewrite if needed."""
    # Strip enrichment internals from target for prompt brevity
    payload_keys = (
        "id", "name", "industry", "founded", "category", "char_limit",
        "direct_signals", "hook_context", "hypothesized_pain",
        "field_map_overrides", "notes"
    )
    target_for_prompt = {k: target[k] for k in payload_keys if k in target}

    # Load persona spec so refine stays aligned with the live persona.
    # _prefer_local: use the per-client override (system_persona.local.md) when it
    # exists — the same file the draft stage uses — instead of the git template.
    from _outreach_core.prompt import resolve_prompts_dir, _prefer_local
    prompts_dir = resolve_prompts_dir(SKILL_DIR, config)
    persona_path = _prefer_local(prompts_dir, "system_persona.md")
    try:
        persona = persona_path.read_text(encoding="utf-8")
    except OSError:
        persona = "(persona spec not found — refine without explicit persona context)"

    prompt = _REFINE_PROMPT_TEMPLATE.format(
        persona=persona,
        target_json=json.dumps(target_for_prompt, ensure_ascii=False, indent=2),
        draft_json=json.dumps({"subject": draft.get("subject"),
                                "body": draft.get("body")},
                                ensure_ascii=False, indent=2),
        max_chars=max_chars,
    )
    model = config.get("model", {}).get("name", DEFAULT_MODEL)
    response = oc_infer(prompt, model=model)
    refined = extract_first_json(response or "")
    if not refined or "body" not in refined:
        return None
    return refined


def _cli_refine_enabled(
    config: dict[str, Any],
    args: argparse.Namespace,
) -> bool:
    if getattr(args, "refine_only_if_low_quality", False):
        return False
    return core_draft.resolve_refine_enabled(
        config,
        cli_refine=getattr(args, "refine", None),
        cli_no_refine=getattr(args, "no_refine", False),
    )


def stage_draft(
    input_path: Path,
    out_path: Path,
    config: dict[str, Any],
    refine: bool = False,
    run_id: str | None = None,
    limit: int | None = None,
) -> None:
    from _outreach_core import events as ev

    if not ev.get_context().data_dir:
        ev.configure(skill="jp-form-outreach", data_dir=DATA_DIR, run_id=run_id)
    refine_fn = _refine_draft if refine else None
    # Keepalive only (heartbeat=None → no Slack posts, just stdout liveness +
    # system_health). Drafting is sequential Opus calls; a single slow call could
    # otherwise leave stdout silent past a stall timeout and get the run killed.
    try:
        n_leads = sum(1 for l in input_path.open() if l.strip())
    except OSError:
        n_leads = 0
    with HeartbeatSession(SKILL_DIR, "draft", n_leads, heartbeat=None, data_dir=DATA_DIR):
        core_draft.stage_draft(
            input_path,
            out_path,
            config,
            prompts_dir=PROMPTS_DIR,
            build_user_block=build_user_block,
            oc_infer_fn=oc_infer,
            append_skip_fn=append_skip_history,
            default_model=DEFAULT_MODEL,
            refine_fn=refine_fn,
            skill="jp-form-outreach",
            data_dir=DATA_DIR,
            run_id=run_id or ev.get_run_id(),
            sender=config.get("sender"),
            limit=limit,
            recent_bodies=_recent_sent_bodies(),
        )


def _recent_sent_bodies(n: int = 20) -> list[str]:
    """Bodies of the last N sent messages (v15 §L2 opening-dedup input)."""
    if not SENT_HISTORY_PATH.exists():
        return []
    bodies: list[str] = []
    try:
        with SENT_HISTORY_PATH.open(encoding="utf-8") as f:
            rows = [l for l in f if l.strip()]
        for line in rows[-n:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            body = ((row.get("draft") or {}).get("body")) or ""
            if body:
                bodies.append(str(body))
    except OSError:
        return bodies
    return bodies


# ============================================================================
# Stage: preview
# ============================================================================

def stage_preview(input_path: Path, interactive_send: bool = True,
                   config: dict[str, Any] | None = None) -> None:
    """
    Show all drafts in terminal for review. If interactive_send=True and there
    are unsent drafts, prompt at the end for `all`/comma-IDs/`n` and chain
    into stage_send. Mirrors linkedin-outreach's preview UX (canonical
    Approve phase of the 6-phase outreach pattern).
    """
    with input_path.open(encoding="utf-8") as f:
        drafts = [json.loads(l) for l in f if l.strip()]
    skipped = [d for d in drafts if d["draft"].get("subject") == "SKIP"]
    sendable = [d for d in drafts if d["draft"].get("subject") != "SKIP"]
    sent_ids = load_sent_set()

    bar = "=" * 78
    print(f"\n{bar}\nDRAFTS PREVIEW — {len(sendable)} sendable, {len(skipped)} skipped\n{bar}")
    for i, d in enumerate(sendable, 1):
        cap = (d.get("form_fields") or {}).get("has_recaptcha_v2", False) \
              or d.get("captcha") == "recaptcha_v2_visible"
        flow = d.get("flow", "?")
        cap_flag = " [reCAPTCHA-v2]" if cap else ""
        already = " [ALREADY SENT]" if d["id"] in sent_ids else ""
        print(f"\n[{i}] {d.get('name')}  ({d.get('industry', '?')}, founded {d.get('founded', '?')}){cap_flag}{already}")
        print(f"    URL: {d.get('form_url', '')}")
        print(f"    Flow: {flow}, char_limit: {d.get('char_limit', '?')}")
        if d["draft"].get("subject"):
            print(f"    Subject: {d['draft']['subject']}")
        body = d["draft"]["body"]
        print(f"    Body ({len(body)} chars):")
        for line in body.splitlines():
            print(f"      {line}")
    if skipped:
        print(f"\n--- SKIPPED ---")
        for d in skipped:
            reason = d['draft']['body'].replace('INSUFFICIENT_DATA: ', '')[:120]
            print(f"  - {d.get('name'):<40} | {reason}")
    print(f"\n{bar}")

    if not interactive_send or not sendable:
        if sendable:
            print("To send (auto):           python run.py send --ids 1,3,5 --auto-send")
            print("To send (interactive):    python run.py send --ids 1,3,5")
            print("To fill-only (manual):    python run.py send --ids 1,3,5 --no-confirm")
        return

    # Interactive send prompt — mirrors linkedin-outreach
    not_yet_sent = [i for i, d in enumerate(sendable, 1) if d["id"] not in sent_ids]
    if not not_yet_sent:
        print("All sendable drafts are already in sent_history. Nothing to send.")
        return

    valid = core_preview.prompt_send_ids(len(sendable), not_yet_sent)
    if valid is None:
        return

    if config is None:
        try:
            config = load_merged_config(SKILL_DIR, BRIEF_ID)
        except FileNotFoundError as e:
            print(f"[preview] {e}; cannot send", file=sys.stderr)
            return

    def _do_send(ids: set[int]) -> None:
        stage_send(input_path, ids, mode="interactive", config=config)

    core_preview.run_after_valid_ids(valid, sendable, _do_send)


# ============================================================================
# Stage: campaign (one-shot full-pipeline runner)
# ============================================================================
#
# Chains the canonical 6-phase outreach pattern into a single command:
#   1. Pull        — bootstrap from targets.yaml (analog of LinkedIn's
#                    fetch-leads / fetch-from-csv)
#   2. Enrich      — visit each form URL, capture field structure
#   3. Personalize — Sonnet draft (with cached system prompt)
#   4. Approve     — preview with interactive send prompt
#   5. Send        — auto-fill form + click submit (per-draft confirm)
#   6. Log         — append to sent_history.jsonl
#
# Phases 4-6 happen inside the preview interactive flow.

def _slack_env() -> tuple[str | None, str | None]:
    import os

    ch = os.environ.get("DOORMAN_SLACK_CHANNEL_ID", "").strip() or None
    ts = os.environ.get("DOORMAN_SLACK_THREAD_TS", "").strip() or None
    return ch, ts


def _build_upfront_summary(cfg: dict[str, Any], sendable: list[dict[str, Any]]) -> str:
    """Human-readable brief + list + sample-draft summary for the one-time
    upfront approval (the single human checkpoint in autonomous mode)."""
    sender = (cfg or {}).get("sender") or {}
    n_samples = core_autonomy.sample_draft_count(cfg)
    lines = [
        f"📋 自律送信モード — 事前承認おねがいします（最初の1回だけ）",
        f"brief: {BRIEF_ID} / 送信者: {sender.get('company', '?')} {sender.get('name', '?')}",
        f"対象リスト: {len(sendable)}社",
    ]
    names = ", ".join(d.get("name", "?") for d in sendable[:12])
    if names:
        lines.append(f"  → {names}{' …' if len(sendable) > 12 else ''}")
    lines.append(f"サンプルドラフト（{min(n_samples, len(sendable))}件）:")
    for d in sendable[:n_samples]:
        dd = d.get("draft") or {}
        body = (dd.get("body") or "").strip().replace("\n", " ")
        lines.append(f"  ・{d.get('name', '?')} 「{dd.get('subject', '')}」 {body[:80]}…")
    lines.append("")
    lines.append("OK なら Slack で「承認」/ CLI: run.py approve-autonomy --brief "
                 f"{BRIEF_ID}")
    lines.append("→ 承認後は確認なしで全件 自動送信（ブロッカーは自動スキップ＋記録）。")
    return "\n".join(lines)


def _run_autonomous_send(
    drafts_path: Path,
    cfg: dict[str, Any],
    sendable: list[dict[str, Any]],
) -> dict[str, Any]:
    """Autonomous Phases 4-6.

    Gate: if upfront approval is required and not yet granted, post the brief +
    list + sample drafts ONCE and stop (no send). Once approved, send every
    not-yet-sent sendable draft hands-off (mode=auto; blockers auto-skip; each
    draft self-scored). Never prompts a human per item.
    """
    from _outreach_core.notify import post as notify_post

    bar = "=" * 70

    if core_autonomy.upfront_approval_required(cfg) and not core_autonomy.is_upfront_approved(DATA_DIR):
        summary = _build_upfront_summary(cfg, sendable)
        core_autonomy.mark_pending_approval(
            DATA_DIR,
            {"sendable": len(sendable), "brief": BRIEF_ID},
        )
        print(f"\n{bar}\n[4-6/6] AUTONOMOUS — 事前承認待ち (one-time)\n{bar}")
        print(summary)
        try:
            notify_post(summary, level="info")
        except Exception:  # noqa: BLE001
            pass
        _emit_event(
            "campaign.awaiting_upfront_approval",
            stage="campaign",
            payload={"sendable": len(sendable), "brief": BRIEF_ID},
        )
        print(f"\n[campaign] 承認後に再実行すると全件自動送信します。")
        return {"selected": 0, "sent": 0, "pending": len(sendable), "skipped": 0, "failed": 0}

    print(f"\n{bar}\n[4-6/6] AUTONOMOUS SEND — hands-off (no per-item confirm)\n{bar}")
    ids = _resolve_ids_arg(None, True, drafts_path, cmd_name="campaign")
    if not ids:
        print("[campaign] no sendable drafts left to auto-send")
        return {"selected": 0, "sent": 0, "pending": 0, "skipped": 0, "failed": 0}
    stats = stage_send(drafts_path, ids, mode="auto", config=cfg, heartbeat="auto") or {}

    # Auto-resolver pass (§16): the main batch never stopped on a hard form — it
    # queued those targets. Now that the browser is free, deep-resolve them in the
    # same hands-off run. No human reply needed.
    if core_resolve_queue.pending(DATA_DIR):
        print(f"\n{bar}\n[resolver] queued blockers → deep resolve pass\n{bar}")
        stage_resolve_queue(cfg)
    return {
        "selected": int(stats.get("selected") or len(ids)),
        "sent": int(stats.get("sent") or 0),
        "pending": int(stats.get("pending") or 0),
        "skipped": int(stats.get("skipped") or 0),
        "failed": int(stats.get("failed") or 0),
    }


def _resolver_failure_detail(
    entry: dict[str, Any],
    result: dict[str, Any] | None,
    vresult: dict[str, Any] | None,
) -> str:
    """Build a compact, evidence-backed resolver failure reason."""
    result = result or {}
    vresult = vresult or {}
    parts = [f"reason_class={entry.get('reason_class') or 'unknown'}"]
    if result.get("loop_status"):
        parts.append(f"loop_status={result['loop_status']}")
    if vresult.get("status"):
        parts.append(f"verify_status={vresult['status']}")
    errors = result.get("validation_errors") or []
    exact: list[str] = []
    for error in errors[:5]:
        if not isinstance(error, dict):
            continue
        field = str(error.get("field") or "unknown")[:60]
        kind = str(error.get("kind") or "validation")[:50]
        message = str(error.get("message") or "").strip()[:100]
        exact.append(f"{field}[{kind}]" + (f": {message}" if message else ""))
    if exact:
        parts.append("errors=" + " | ".join(exact))
    evidence = vresult.get("evidence") if isinstance(vresult.get("evidence"), dict) else {}
    verdict = evidence.get("send_verdict") or evidence.get("verdict")
    if verdict:
        parts.append(f"verdict={verdict}")
    return "; ".join(parts)


def stage_resolve_queue(config: dict[str, Any] | None = None) -> None:
    """Deep-resolve every target the main batch queued as a blocker (§16).

    For each pending target: re-open fresh and retry submit with the robust
    document-wide LLM-pick strategy (and a URL-safe body if the domain is known
    url-unfriendly). Success → sent_history; failure → skip_history. Runs as its
    own pass (and can be launched as a *separate process* via `resolve-queue`),
    so a hard form never blocks the main run or needs a human 「進めて」."""
    from _outreach_core.notify import post as notify_post

    if config is None:
        try:
            config = load_merged_config(SKILL_DIR, BRIEF_ID)
        except FileNotFoundError:
            config = {}

    queue, already_sent = core_resolve_queue.partition_pending_by_sent(
        core_resolve_queue.pending(DATA_DIR),
        load_sent_set(),
    )
    for entry in already_sent:
        tid = str(entry.get("target_id") or entry.get("id") or "")
        name = str(entry.get("name") or tid)
        core_resolve_queue.mark(
            DATA_DIR, tid, "resolved", note="already present in sent_history; duplicate blocked"
        )
        close_needs_attention(
            DATA_DIR, tid, resolution="already sent; stale resolver entry closed"
        )
        print(f"[resolver] duplicate blocked: {name} is already in sent_history")
    if not queue:
        print("[resolver] queue empty — nothing to resolve")
        return

    drafts_path = DATA_DIR / "drafts.jsonl"
    drafts_by_id: dict[str, dict[str, Any]] = {}
    if drafts_path.is_file():
        for line in drafts_path.open():
            if line.strip():
                dd = json.loads(line)
                drafts_by_id[str(dd.get("id"))] = dd

    print(f"[resolver] {len(queue)} target(s) to deep-resolve")
    core_progress.transition(DATA_DIR, "resolve", len(queue), brief=BRIEF_ID)
    hb = HeartbeatSession(SKILL_DIR, "resolve", len(queue), heartbeat="auto", data_dir=DATA_DIR)
    hb.start(f"deep-resolve {len(queue)} targets")
    resolved, skipped, skipped_details = [], [], []

    for i, entry in enumerate(queue, 1):
        tid = str(entry.get("target_id"))
        name = entry.get("name", tid)
        d = drafts_by_id.get(tid)
        if not d:
            core_resolve_queue.mark(DATA_DIR, tid, "skipped", note="draft not found")
            skipped.append(name)
            skipped_details.append(f"{name}: 下書きが見つかりません")
            core_progress.bump(DATA_DIR, outcome="skipped", name=name)
            hb.tick(i, f"{name} skipped: draft not found")
            continue

        form_url = d.get("form_url", "")
        flow = d.get("flow") or (d.get("_llm_plan") or {}).get("next_step") or "single"
        body = d["draft"]["body"]
        if core_avoidance.is_url_unfriendly(DATA_DIR, form_url) and core_content_guard.has_url(body):
            body, _ = core_content_guard.sanitize_body(body, kind="url")

        print(f"\n[resolver] ({i}/{len(queue)}) {name} — deep submit ({flow})")
        from _outreach_core import events as ev
        if not ev.get_context().data_dir:
            ev.configure(skill="jp-form-outreach", data_dir=DATA_DIR)
        trace = ev.trace_dir_for(tid)

        # Prefer in-place resolution on the KEPT-OPEN errored tab (§17): the form
        # is already filled and on its exact failed state. Only re-open fresh if
        # the tab is gone or the in-place attempt didn't submit.
        tab_id = entry.get("tab_id")
        result = None
        if tab_id and _tab_is_open(tab_id):
            print(f"  [resolver] エラータブを focus してその場で解決: {tab_id}")
            inplace = _resolve_in_open_tab(d, tab_id, config, trace=trace,
                                           flow=flow, verify_strict=True)
            if inplace.get("vresult") and inplace["vresult"].get("status") == "ok":
                result = inplace
            else:
                print(f"  [resolver] in-place 不成立 ({inplace.get('status')}) → 再オープンで再試行")
        if result is None:
            result = _deep_submit(d, body, config, trace=trace, flow=flow,
                                  verify_strict=True, iterative_fill=True)
        vres = result.get("vresult")
        outcome = (
            handle_verify_result(
                d, vres, DATA_DIR, channel="jp_form", record_attention=False
            )
            if vres
            else "failed"
        )
        if outcome == "sent_ok":
            append_sent_history([d])
            core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_SENT)
            core_resolve_queue.mark(DATA_DIR, tid, "resolved", note="deep resolver sent")
            print(f"  [resolver] ✅ {name} 送信成功")
            # v31 §WS8a — handle_verify_result no longer posts 送信完了 itself
            # (it used to double-post with _send_one_target's ✅ line), so the
            # resolver path owns its own concise per-target success line.
            try:
                from _outreach_core import notify as _notify
                _notify.post_target_event(
                    stage="resolve",
                    status="sent",
                    target=d,
                    detail={
                        "verify_status": (vres or {}).get("status"),
                        "form_url": form_url,
                    },
                )
            except Exception:  # noqa: BLE001 - Slack must never abort the loop
                pass
            resolved.append(name)
            progress_outcome = "sent"
        else:
            detail = _resolver_failure_detail(entry, result, vres)
            append_skip_history([{**d, "draft": {**d.get("draft", {}),
                                                 "body": f"RESOLVER_FAILED: {detail}"}}])
            core_resolve_queue.mark(
                DATA_DIR, tid, "skipped",
                note=f"deep resolver could not submit: {detail}"[:500],
            )
            print(f"  [resolver] ⏭ {name} 自動解決できず → skip 記録 ({detail})")
            skipped.append(name)
            label = core_resolve_queue.humanize_reason(str(entry.get("reason_class") or "unknown"))
            skipped_details.append(f"{name}: {label}")
            progress_outcome = "skipped"
        _emit_event(
            "resolver.target_outcome",
            stage="resolve",
            target_id=tid,
            outcome="sent" if outcome == "sent_ok" else "skipped",
            payload={
                "reason_class": entry.get("reason_class"),
                "loop_status": result.get("loop_status"),
                "verify_status": (vres or {}).get("status") if isinstance(vres, dict) else None,
                "validation_errors": (result.get("validation_errors") or [])[:8],
            },
            trace_dir=trace,
        )
        core_progress.bump(DATA_DIR, outcome=progress_outcome, name=name)
        if tab_id:
            _close_tab(tab_id)  # done with this target's errored tab
        hb.tick(i, f"{name} done")

    hb.end(f"resolve done · sent={len(resolved)} · skipped={len(skipped)}")
    core_progress.finish(DATA_DIR)
    def _join_preview(items: list[str], *, limit: int = 8) -> str:
        if not items:
            return "なし"
        shown = items[:limit]
        tail = f" ほか{len(items) - limit}件" if len(items) > limit else ""
        return "、".join(shown) + tail

    msg = (
        f"🔧 リゾルバ完了: 送信OK {len(resolved)} / 未送信 {len(skipped)}\n"
        f"  送信OK: {_join_preview(resolved)}\n"
        f"  未送信: {_join_preview(skipped)}\n"
        f"  理由: {_join_preview(skipped_details, limit=6)}"
    )
    if skipped:
        msg += "\n  次アクション: URL再精査または手動送信対象です。"
    print(f"\n[resolver] {msg}")
    try:
        notify_post(msg, level="info")
    except Exception:  # noqa: BLE001
        pass


def stage_campaign(
    targets_path: Path,
    clean: bool,
    skip_enrich: bool,
    skip_send: bool,
    include_sent: bool = False,
    include_skipped: bool = False,
    refine: bool = False,
    limit: int | None = None,
    only_ids: list[str] | None = None,
) -> None:
    """Run the canonical outreach pipeline end-to-end. Mirrors
    linkedin-outreach's stage_campaign for cross-skill consistency."""
    bar = "=" * 70

    from _outreach_core import events as ev
    from _outreach_core.active_run import ActiveRunError, campaign_run_lock
    from _outreach_core.channel_state import touch_last_used

    slack_ch, slack_ts = _slack_env()
    run_id = ev.configure(skill="jp-form-outreach", data_dir=DATA_DIR)
    print(f"[campaign] brief={BRIEF_ID} · skill=jp-form-outreach · run_id={run_id}")

    try:
        lock_ctx = campaign_run_lock(
            DATA_DIR,
            run_id=run_id,
            brief_id=BRIEF_ID,
            skill="jp-form-outreach",
            total_targets=limit or 0,
            slack_channel_id=slack_ch,
            slack_thread_ts=slack_ts,
        )
    except ActiveRunError as exc:
        print(f"[campaign] {exc}", file=sys.stderr)
        sys.exit(3)

    with lock_ctx:
        try:
            cfg = load_merged_config(SKILL_DIR, BRIEF_ID)
        except FileNotFoundError as e:
            print(f"[campaign] {e}", file=sys.stderr)
            print(
                f"           cp {SKILL_DIR / 'config.example.yaml'} {SKILL_DIR / 'config.yaml'}",
                file=sys.stderr,
            )
            return

        from _outreach_core.campaign import (
            CampaignContext,
            CampaignRunner,
            FunctionChannelAdapter,
            PhaseResult,
        )

        if clean:
            for f in ("leads.jsonl", "enriched.jsonl", "drafts.jsonl"):
                (DATA_DIR / f).unlink(missing_ok=True)
            print("[campaign] cleared previous run state")

        context = CampaignContext(
            brief_id=BRIEF_ID,
            persona_id=PERSONA_ID,
            channel="jp_form",
            skill="jp-form-outreach",
            data_dir=DATA_DIR,
            slack_channel_id=slack_ch,
            slack_thread_ts=slack_ts,
            run_id=run_id,
        )
        phase_state: dict[str, Any] = {}

        def do_list(_ctx: CampaignContext) -> PhaseResult:
            print(f"\n{bar}\n[1/4] LIST — bootstrap targets from {targets_path.name}\n{bar}")
            stage_bootstrap(
                targets_path,
                DATA_DIR / "leads.jsonl",
                include_sent=include_sent,
                include_skipped=include_skipped,
                limit=limit,
                only_ids=only_ids,
            )
            count = sum(1 for line in (DATA_DIR / "leads.jsonl").open() if line.strip())
            if count == 0:
                return PhaseResult("list", status="failed", detail={"reason": "no targets"})
            return PhaseResult("list", total=count, ready=count)

        def do_enrich(_ctx: CampaignContext) -> PhaseResult:
            print(f"\n{bar}\n[2/4] ENRICH — form structure detection\n{bar}")
            if skip_enrich:
                import shutil

                shutil.copy(DATA_DIR / "leads.jsonl", DATA_DIR / "enriched.jsonl")
            else:
                stage_enrich(DATA_DIR / "leads.jsonl", DATA_DIR / "enriched.jsonl", config=cfg)
            count = sum(1 for line in (DATA_DIR / "enriched.jsonl").open() if line.strip())
            if count == 0:
                return PhaseResult("enrich", status="failed", detail={"reason": "no enriched targets"})
            return PhaseResult("enrich", total=count, ready=count, status="skipped" if skip_enrich else "ok")

        def do_draft(_ctx: CampaignContext) -> PhaseResult:
            print(f"\n{bar}\n[3/4] DRAFT — personalized copy\n{bar}")
            stage_draft(DATA_DIR / "enriched.jsonl", DATA_DIR / "drafts.jsonl", cfg, refine=refine)
            drafts = [json.loads(line) for line in (DATA_DIR / "drafts.jsonl").open() if line.strip()]
            sendable = [d for d in drafts if (d.get("draft") or {}).get("subject") != "SKIP"]
            phase_state["drafts"] = drafts
            phase_state["sendable"] = sendable
            return PhaseResult(
                "draft",
                total=len(drafts),
                ready=len(sendable),
                skipped=len(drafts) - len(sendable),
            )

        def do_send(_ctx: CampaignContext) -> PhaseResult:
            sendable = phase_state.get("sendable") or []
            before = len(load_sent_set())
            before_skipped = len(load_skip_set())
            send_stats = _run_autonomous_send(DATA_DIR / "drafts.jsonl", cfg, sendable)
            sent = max(0, len(load_sent_set()) - before)
            skipped = max(0, len(load_skip_set()) - before_skipped)
            selected = int((send_stats or {}).get("selected") or 0)
            failed = int((send_stats or {}).get("failed") or 0)
            pending = max(
                0,
                selected - sent - skipped - failed,
                int((send_stats or {}).get("pending") or 0) - sent - skipped,
            )
            total = max(selected, sent + skipped + pending + failed)
            return PhaseResult(
                "send",
                total=total,
                sent=sent,
                skipped=skipped,
                pending=pending,
                failed=failed,
                status="ok",
            )

        adapter = FunctionChannelAdapter("jp_form", do_list, do_enrich, do_draft, do_send)
        send_authorized = (not skip_send) and core_autonomy.is_autonomous(cfg)
        result = CampaignRunner(context).run(
            adapter,
            stop_after="send" if send_authorized else "draft",
            replace_context=clean,
        )
        if result.status == "failed":
            raise RuntimeError(f"campaign failed in {result.stopped_after}")
        if not send_authorized:
            print(f"\n{bar}\nPREVIEW — no send authorization in this run\n{bar}")
            stage_preview(
                DATA_DIR / "drafts.jsonl",
                interactive_send=not skip_send,
                config=cfg,
            )
        if slack_ch:
            touch_last_used(slack_ch, slack_ts)


# ============================================================================
# Stage: send (drive form fill + submit)
# ============================================================================

# Generic "find field by label pattern" — used for sender info fields
# (氏名, ふりがな, 会社名, 電話, メール, etc.)
SENDER_FIELD_PATTERNS = {
    "name": [r"お?名前", r"氏名", r"name", r"担当者", r"ご担当"],
    "name_kana": [r"フリガナ", r"カナ", r"kana", r"katakana"],
    "name_furigana": [r"ふりがな", r"furigana", r"hiragana"],
    # 姓: avoid 旧姓; accept 苗字/名字/last|family name.
    "name_sei": [r"(?<!旧)姓", r"苗字", r"名字", r"last[ _-]?name", r"family[ _-]?name", r"\bsei\b"],
    # 名: must NOT match compounds (会社名, 氏名, 名前, 件名 …) — the old r"名$"
    # poured the given name into 会社名/氏名 fields. Anchor to a bare 名.
    "name_mei": [r"^名$", r"^名[\s　]*[（(※:：]", r"[（(]名[)）]", r"first[ _-]?name", r"\bmei\b"],
    "name_kana_sei": [r"セイ", r"姓.{0,6}(フリガナ|カナ)", r"(フリガナ|カナ).{0,6}姓"],
    "name_kana_mei": [r"メイ", r"[（(]名[)）].{0,6}(フリガナ|カナ)", r"(フリガナ|カナ).{0,6}[（(]名[)）]"],
    "name_furigana_sei": [r"^せい$", r"姓.{0,6}ふりがな", r"ふりがな.{0,6}姓"],
    "name_furigana_mei": [r"^めい$", r"[（(]名[)）].{0,6}ふりがな", r"ふりがな.{0,6}[（(]名[)）]"],
    "company": [r"会社名", r"法人名", r"団体名", r"貴社", r"御社", r"company"],
    "company_kana": [
        r"会社名.{0,6}(フリガナ|カナ)",
        r"法人名.{0,6}(フリガナ|カナ)",
        r"(フリガナ|カナ).{0,6}会社",
        r"company.{0,8}(kana|katakana)",
    ],
    "company_furigana": [
        r"会社名.{0,6}ふりがな",
        r"法人名.{0,6}ふりがな",
        r"ふりがな.{0,6}会社",
        r"company.{0,8}(furigana|hiragana)",
    ],
    "role": [r"役職", r"部署", r"position", r"title"],
    "email": [r"メール", r"e-?mail"],
    "email_confirm": [r"確認", r"再入力", r"confirm"],
    "phone": [r"電話", r"tel\b", r"phone"],
    "phone_part1": [r"電話番号1", r"phone1"],
    "postal_code": [r"郵便番号", r"postal", r"zip"],
    "prefecture": [r"都道府県", r"prefecture", r"都市|区分"],
    "city": [r"市区町村"],
    "address_line": [r"番地", r"町域"],
    "building": [r"建物", r"ビル", r"マンション"],
    "address_full": [r"住所"],
}


_FILL_FIELD_BY_LABEL_JS = r"""
(args) => {
  const { kind, value, patterns } = args;
  const setValue = (el, v) => {
    const proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const labelFor = (el) => {
    if (el.id) {
      try {
        const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (l) return (l.textContent || '').trim();
      } catch (e) {}
    }
    const wrap = el.closest('label');
    if (wrap) return (wrap.textContent || '').trim();
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return aria.trim();
    // Inline preceding text within the same parent — handles 「姓 [input] 名 [input]」
    {
      let sib = el.previousSibling;
      while (sib) {
        if (sib.nodeType === 3) {
          const txt = sib.textContent.trim();
          if (txt && txt !== '-' && txt !== '−' && txt !== 'ー' && txt !== '/' && txt !== '／') {
            return txt;
          }
        } else if (sib.nodeType === 1) {
          const tag = sib.tagName;
          if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'BUTTON') break;
          if (tag !== 'BR') {
            const txt = (sib.textContent || '').trim();
            if (txt) return txt;
          }
        }
        sib = sib.previousSibling;
      }
    }
    // Same-row label (th, strong, label, dt inside the row)
    const row = el.closest('tr, .form-row, .field, .input, .item, dl');
    if (row) {
      const l = row.querySelector('th, label, .label, [class*="label" i], strong, dt, .title');
      if (l && !l.contains(el)) return (l.textContent || '').trim();
    }
    // Preceding-sibling <tr> with label-only content (Onward-style)
    const tr = el.closest('tr');
    if (tr) {
      let prev = tr.previousElementSibling;
      for (let i = 0; prev && i < 3; i++) {
        if (prev.querySelector('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="image"]), textarea, select')) break;
        const txt = (prev.textContent || '').replace(/\s+/g, ' ').trim();
        if (txt) return txt;
        prev = prev.previousElementSibling;
      }
    }
    let cur = el.parentElement;
    for (let i = 0; i < 4 && cur; i++, cur = cur.parentElement) {
      const l = cur.querySelector('label, .label, [class*="label" i], strong, th, dt');
      if (l && !l.contains(el)) return (l.textContent || '').trim();
    }
    return el.placeholder || el.name || '';
  };

  const candidates = Array.from(document.querySelectorAll('input,textarea')).filter(el => {
    const t = (el.type || '').toLowerCase();
    return t !== 'hidden' && t !== 'submit' && t !== 'button' && t !== 'radio' && t !== 'checkbox';
  });

  // Try each label pattern, from most specific to least specific
  for (const pat of patterns) {
    const re = new RegExp(pat, 'i');
    for (const el of candidates) {
      if (el.value) continue; // skip already-filled
      const lbl = labelFor(el);
      if (re.test(lbl) || re.test(el.placeholder || '') || re.test(el.name || '')) {
        setValue(el, value);
        return { filled: true, kind, label: lbl, name: el.name };
      }
    }
  }
  return { filled: false, kind };
}
"""


_FILL_RADIO_BY_LABEL_JS = r"""
(args) => {
  const { value } = args;
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${el.id}"]`);
      if (l) return (l.textContent || '').trim();
    }
    const wrap = el.closest('label');
    if (wrap) return (wrap.textContent || '').trim();
    return '';
  };
  const radios = document.querySelectorAll('input[type="radio"]');
  for (const r of radios) {
    const lbl = labelFor(r);
    if (lbl.includes(value) || (r.value || '').includes(value)) {
      r.checked = true;
      r.dispatchEvent(new Event('change', { bubbles: true }));
      r.dispatchEvent(new Event('click', { bubbles: true }));
      return { selected: true, label: lbl, value: r.value };
    }
  }
  return { selected: false };
}
"""


_FILL_SELECT_BY_TEXT_JS = r"""
(args) => {
  const { value, label_pattern } = args;
  const lbl_re = label_pattern ? new RegExp(label_pattern, 'i') : null;
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${el.id}"]`);
      if (l) return (l.textContent || '').trim();
    }
    return '';
  };
  for (const sel of document.querySelectorAll('select')) {
    const lbl = labelFor(sel);
    if (lbl_re && !lbl_re.test(lbl) && !lbl_re.test(sel.name || '')) continue;
    for (const opt of sel.options) {
      if (opt.text.includes(value) || opt.value === value) {
        sel.value = opt.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        return { selected: true, label: lbl, option: opt.text };
      }
    }
  }
  return { selected: false };
}
"""


_FILL_TEXTAREA_JS = r"""
(args) => {
  const { value } = args;
  const setValue = (el, v) => {
    const proto = window.HTMLTextAreaElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  // Find the largest textarea or one labelled 'お問い合わせ内容' / '本文' / '内容'
  const tas = Array.from(document.querySelectorAll('textarea'));
  if (!tas.length) return { filled: false, reason: 'no textarea' };
  // Sort by visual size desc — biggest is usually the body
  tas.sort((a, b) => {
    const ar = a.getBoundingClientRect(); const br = b.getBoundingClientRect();
    return (br.width * br.height) - (ar.width * ar.height);
  });
  setValue(tas[0], value);
  return { filled: true, name: tas[0].name || tas[0].id };
}
"""


_LIST_CHECKBOX_GATES_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const stableSelector = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    const name = el.getAttribute('name');
    if (name) {
      const boxes = document.querySelectorAll(`input[type="checkbox"][name="${CSS.escape(name)}"]`);
      if (boxes.length === 1) return `input[type="checkbox"][name="${CSS.escape(name)}"]`;
    }
    let cur = el;
    const path = [];
    for (let depth = 0; cur && depth < 7 && cur !== document.body; depth += 1) {
      let part = cur.tagName.toLowerCase();
      if (cur.parentElement) {
        const sibs = Array.from(cur.parentElement.children).filter((x) => x.tagName === cur.tagName);
        if (sibs.length > 1) part += `:nth-of-type(${sibs.indexOf(cur) + 1})`;
      }
      path.unshift(part);
      cur = cur.parentElement;
    }
    return path.join(' > ');
  };
  const contextText = (el) => {
    // Prefer the semantic row over the nearest <td>/<div>.  In table forms the
    // 必須 marker is normally in the sibling <th>, not inside the checkbox cell.
    const row = el.closest('tr') || el.closest('fieldset') ||
      el.closest('.form-row, .form-group, .field, .input, .item, dl') ||
      el.closest('li, p, td, th, div');
    let text = row ? norm((row.textContent || '').slice(0, 240)) : '';
    // CF7 often puts the required group heading in the paragraph immediately
    // before the paragraph containing the checkbox widgets.
    let cur = el;
    for (let depth = 0; cur && depth < 6; depth += 1, cur = cur.parentElement) {
      const prev = cur.previousElementSibling;
      const prevText = prev ? norm((prev.textContent || '').slice(0, 160)) : '';
      if (prevText && /必須|required|お問い合わせ|問合せ|種別|項目/.test(prevText)) {
        text = (prevText + ' ' + text).trim();
        break;
      }
    }
    return text;
  };
  // NOTE: bare ※ is a generic footnote marker in JP forms, not a reliable
  // required indicator, so we only treat it as required when paired with 必須.
  const visuallyRequired = (el) => /必須|required|※[^]{0,12}必須/.test(contextText(el));
  const labelFor = (el) => {
    let txt = '';
    if (el.id) {
      const l = document.querySelector(`label[for="${el.id}"]`);
      if (l) txt = norm(l.textContent || '');
    }
    if (!txt) {
      const wrap = el.closest('label');
      if (wrap) txt = norm(wrap.textContent || '');
    }
    if (!txt && el.getAttribute('aria-label')) {
      txt = norm(el.getAttribute('aria-label') || '');
    }
    if (!txt && el.parentElement) {
      txt = norm(el.parentElement.textContent || '');
    }
    if (!txt && el.closest('div,li,p,td,th,fieldset')) {
      txt = norm((el.closest('div,li,p,td,th,fieldset').textContent || '').slice(0, 120));
    }
    return txt;
  };
  const groupLabelFor = (el) => {
    const fs = el.closest('fieldset');
    if (fs) {
      const legend = fs.querySelector('legend');
      if (legend) return norm(legend.textContent || '');
    }
    const tr = el.closest('tr');
    if (tr) {
      const head = tr.querySelector('th, .label, .title, dt');
      if (head) return norm(head.textContent || '');
    }
    const row = el.closest('.form-row, .form-group, .field, .input, .item, dl');
    if (row) {
      const head = row.querySelector(':scope > label, :scope > .label, :scope > .title, :scope > dt');
      if (head) return norm(head.textContent || '');
    }
    let cur = el;
    for (let depth = 0; cur && depth < 6; depth += 1, cur = cur.parentElement) {
      const prev = cur.previousElementSibling;
      const prevText = prev ? norm((prev.textContent || '').slice(0, 160)) : '';
      if (prevText && /必須|required|お問い合わせ|問合せ|種別|項目/.test(prevText)) {
        return prevText;
      }
    }
    return '';
  };
  const out = [];
  for (const cb of document.querySelectorAll('input[type="checkbox"]')) {
    const nativeRequired = Boolean(
      cb.required || cb.getAttribute('aria-required') === 'true' ||
      cb.getAttribute('data-required') === 'true'
    );
    const required = Boolean(
      nativeRequired ||
      visuallyRequired(cb)
    );
    out.push({
      name: cb.name || '',
      id: cb.id || '',
      selector: stableSelector(cb),
      label: labelFor(cb),
      group_label: groupLabelFor(cb),
      value: cb.value || '',
      checked: Boolean(cb.checked),
      required: required,
      native_required: nativeRequired,
      disabled: Boolean(cb.disabled || cb.getAttribute('aria-disabled') === 'true'),
    });
  }
  return out;
}
"""


_LIST_RADIO_GATES_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const stableSelector = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    const name = el.getAttribute('name');
    // The name-only selector is only unique when a single radio carries that
    // name. For real groups (the common case) it would match the first option,
    // and `:nth-of-type` cannot index a name-filtered NodeList — so fall through
    // to a structural path that uniquely identifies THIS option.
    if (name) {
      const radios = document.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`);
      if (radios.length === 1) {
        return `input[type="radio"][name="${CSS.escape(name)}"]`;
      }
    }
    let cur = el;
    const path = [];
    for (let depth = 0; cur && depth < 7 && cur !== document.body; depth += 1) {
      let part = cur.tagName.toLowerCase();
      if (cur.parentElement) {
        const sibs = Array.from(cur.parentElement.children).filter((x) => x.tagName === cur.tagName);
        if (sibs.length > 1) part += `:nth-of-type(${sibs.indexOf(cur) + 1})`;
      }
      path.unshift(part);
      cur = cur.parentElement;
    }
    return path.join(' > ');
  };
  const contextText = (el) => {
    const row = el.closest('fieldset, tr, dl, .form-row, .form-group, .field, .input, .item, li, p, td, th, div');
    return row ? norm((row.textContent || '').slice(0, 260)) : '';
  };
  // NOTE: bare ※ is a generic footnote marker in JP forms, not a reliable
  // required indicator, so we only treat it as required when paired with 必須.
  const visuallyRequired = (el) => /必須|required|※[^]{0,12}必須/.test(contextText(el));
  const labelForRadio = (el) => {
    let txt = '';
    if (el.id) {
      const l = document.querySelector(`label[for="${el.id}"]`);
      if (l) txt = norm(l.textContent || '');
    }
    if (!txt) {
      const wrap = el.closest('label');
      if (wrap) txt = norm(wrap.textContent || '');
    }
    if (!txt && el.parentElement) txt = norm(el.parentElement.textContent || '');
    return txt;
  };
  const groupLabel = (r) => {
    const fs = r.closest('fieldset');
    if (fs) {
      const lg = fs.querySelector('legend');
      if (lg) return norm(lg.textContent || '');
    }
    const row = r.closest('tr, dl, .form-row, .form-group, .field, .input, .item, li, p, td');
    if (row) {
      const l = row.querySelector('th, dt, .label, [class*="label" i], .title, strong');
      if (l && !l.contains(r)) return norm(l.textContent || '');
    }
    let cur = r.parentElement;
    for (let i = 0; i < 4 && cur; i++, cur = cur.parentElement) {
      const l = cur.querySelector('label, .label, [class*="label" i], .title');
      if (l && !l.contains(r)) return norm(l.textContent || '');
    }
    return '';
  };
  const map = new Map();
  const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
  for (const r of radios) {
    const name = r.name || '';
    if (!name) continue;
    const item = map.get(name) || {
      name: name,
      label: groupLabel(r),
      required: false,
      selected: false,
      options: [],
    };
    item.required = item.required || Boolean(
      r.required || r.getAttribute('aria-required') === 'true' || visuallyRequired(r)
    );
    item.selected = item.selected || Boolean(r.checked);
    item.options.push({
      selector: stableSelector(r),
      label: labelForRadio(r),
      value: r.value || '',
      checked: Boolean(r.checked),
      disabled: Boolean(r.disabled || r.getAttribute('aria-disabled') === 'true'),
    });
    map.set(name, item);
  }
  return Array.from(map.values()).slice(0, 80);
}
"""


_LIST_SELECT_GATES_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const stableSelector = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    const name = el.getAttribute('name');
    if (name) return `select[name="${CSS.escape(name)}"]`;
    let cur = el;
    const path = [];
    for (let depth = 0; cur && depth < 7 && cur !== document.body; depth += 1) {
      let part = cur.tagName.toLowerCase();
      if (cur.parentElement) {
        const sibs = Array.from(cur.parentElement.children).filter((x) => x.tagName === cur.tagName);
        if (sibs.length > 1) part += `:nth-of-type(${sibs.indexOf(cur) + 1})`;
      }
      path.unshift(part);
      cur = cur.parentElement;
    }
    return path.join(' > ');
  };
  const contextText = (el) => {
    const row = el.closest('tr, dl, .form-row, .form-group, .field, .input, .item, li, p, td, th, fieldset, div');
    return row ? norm((row.textContent || '').slice(0, 260)) : '';
  };
  // NOTE: bare ※ is a generic footnote marker in JP forms, not a reliable
  // required indicator, so we only treat it as required when paired with 必須.
  const visuallyRequired = (el) => /必須|required|※[^]{0,12}必須/.test(contextText(el));
  const labelFor = (el) => {
    let txt = '';
    if (el.id) {
      const l = document.querySelector(`label[for="${el.id}"]`);
      if (l) txt = norm(l.textContent || '');
    }
    if (!txt) {
      const row = el.closest('tr, .form-row, .field, .input, .item');
      if (row) {
        const l = row.querySelector('th, .label, label, .title, dt');
        if (l) txt = norm(l.textContent || '');
      }
    }
    if (!txt && el.parentElement) txt = norm(el.parentElement.textContent || '');
    return txt;
  };
  const out = [];
  for (const sel of Array.from(document.querySelectorAll('select'))) {
    const required = Boolean(
      sel.required ||
      sel.getAttribute('aria-required') === 'true' ||
      sel.getAttribute('data-required') === 'true' ||
      visuallyRequired(sel)
    );
    const options = Array.from(sel.options || []).map((opt) => ({
      label: norm(opt.text || ''),
      value: opt.value || '',
      selected: Boolean(opt.selected),
      disabled: Boolean(opt.disabled),
    }));
    const selected = options.some((o) => o.selected && o.value && o.label);
    out.push({
      name: sel.name || '',
      id: sel.id || '',
      selector: stableSelector(sel),
      label: labelFor(sel),
      required: required,
      selected: selected,
      disabled: Boolean(sel.disabled || sel.getAttribute('aria-disabled') === 'true'),
      options: options.slice(0, 120),
    });
  }
  return out.slice(0, 80);
}
"""


# Side-effect-free snapshot of the browser's native Constraint Validation API.
# Reading ValidityState/validationMessage does not show validation UI, but gives
# us field-level reasons that generic page-text classification cannot see.
_FORM_CONSTRAINTS_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const st = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const labelFor = (el) => {
    if (el.labels && el.labels.length) {
      const text = norm(Array.from(el.labels).map((x) => x.textContent || '').join(' '));
      if (text) return text;
    }
    const aria = norm(el.getAttribute('aria-label') || '');
    if (aria) return aria;
    const tr = el.closest('tr');
    if (tr) {
      const head = tr.querySelector('th, .label, .title, dt');
      if (head) return norm(head.textContent || '');
    }
    const row = el.closest('fieldset, .form-row, .form-group, .field, .input, .item, dl');
    if (row) {
      const head = row.querySelector('legend, :scope > label, :scope > .label, :scope > .title, :scope > dt');
      if (head) return norm(head.textContent || '');
    }
    return '';
  };
  const forms = Array.from(document.querySelectorAll('form')).filter(visible);
  let root = forms.find((f) => f.querySelector('textarea')) ||
    forms.sort((a, b) => b.querySelectorAll('input,select,textarea').length -
                         a.querySelectorAll('input,select,textarea').length)[0] || document;
  const controls = Array.from(root.querySelectorAll('input, select, textarea'));
  const invalid = [];
  for (const el of controls) {
    // Custom radio/checkbox widgets commonly hide the native input and render
    // a styled label. Hidden-by-CSS controls still participate in validation.
    if (el.disabled) continue;
    const validity = el.validity || null;
    const nativeInvalid = Boolean(el.willValidate && validity && !validity.valid);
    const ariaInvalid = el.getAttribute('aria-invalid') === 'true';
    if (!nativeInvalid && !ariaInvalid) continue;
    const reasons = [];
    if (validity) {
      for (const key of [
        'valueMissing', 'typeMismatch', 'patternMismatch', 'tooLong', 'tooShort',
        'rangeUnderflow', 'rangeOverflow', 'stepMismatch', 'badInput', 'customError'
      ]) {
        if (validity[key]) reasons.push(key);
      }
    }
    if (ariaInvalid && !reasons.length) reasons.push('ariaInvalid');
    invalid.push({
      name: el.name || el.id || '',
      id: el.id || '',
      type: (el.type || el.tagName || '').toLowerCase(),
      label: labelFor(el),
      reasons,
      message: norm(el.validationMessage || '').slice(0, 240),
      value_present: Boolean(el.type === 'checkbox' || el.type === 'radio' ? el.checked : el.value),
      visible: visible(el),
    });
  }
  return {
    form_found: root !== document,
    control_count: controls.length,
    invalid_count: invalid.length,
    valid: invalid.length === 0,
    invalid: invalid.slice(0, 40),
  };
}
"""


_CLICK_BUTTON_BY_TEXT_JS = r"""
(args) => {
  const { patterns, formRootSelector } = args;
  const selector =
    'button, input[type="submit"], input[type="button"], input[type="image"], a, [role="button"], ' +
    '[onclick], .btn, [class*="btn" i], [class*="submit" i], [class*="confirm" i]';
  let scopes = [];
  if (formRootSelector) {
    try {
      const root = document.querySelector(formRootSelector);
      if (root) scopes.push(root);
    } catch (e) {}
  }
  scopes.push(document);
  let foundButDisabled = false;
  const disabledCandidates = [];
  // Never click "go back / modify" controls from the submit cascades — actus
  // 2026-06-13: pattern "submit" matched name="submitBack" (変更する) and
  // bounced the confirm page back to input forever.
  const denyRe = /(戻る|戻り|変更する|修正する|再入力|やり直|キャンセル|cancel|前の画面|入力画面に|submit_?back|go_?back|btn_?back|\bback\b|reset)/i;
  const diagnosticMetaRe = /(submit[-_]?error|validation|invalid|response[-_]?output|alert|notice|message[-_]?error|error[-_]?message|required[-_]?message)/i;
  const diagnosticTextRe = /(未入力|入力が正しくない|入力エラー|入力内容に誤り|エラーがあります|正しく入力してください|必須項目)/i;
  const isNativeControl = (el) => {
    const tag = String(el.tagName || '').toLowerCase();
    return tag === 'button' || tag === 'input' || tag === 'a' ||
      el.getAttribute('role') === 'button' || el.hasAttribute('onclick');
  };
  const commitAndClick = (el) => {
    // Commit framework-controlled fields before submit (CF7/React/Vue sites
    // sometimes validate only after blur/change), then reproduce the pointer
    // event sequence before the single click.
    const active = document.activeElement;
    if (active && active !== document.body && typeof active.blur === 'function') active.blur();
    if (typeof el.focus === 'function') el.focus({ preventScroll: true });
    for (const type of ['pointerdown', 'mousedown', 'mouseup', 'pointerup']) {
      try { el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window })); }
      catch (e) {}
    }
    el.click();
  };
  for (const scope of scopes) {
    const buttons = Array.from(scope.querySelectorAll(selector));
    for (const pat of patterns) {
      const re = new RegExp(pat, 'i');
      for (const b of buttons) {
        // Match each attribute SEPARATELY: concatenating them broke anchored
        // patterns (^送信する$ never matched "送信する submitSend").
        const parts = [
          (b.textContent || b.value || ''),
          (b.getAttribute('aria-label') || ''),
          (b.getAttribute('title') || ''),
          (b.getAttribute('alt') || ''),
          (b.getAttribute('name') || ''),
        ].map((s) => s.replace(/\s+/g, ' ').trim());
        const txt = parts.filter(Boolean).join(' ');
        // Skip elements with too much text (likely a wrapper, not a button)
        if (txt.length > 80) continue;
        if (denyRe.test(txt)) continue;
        // Broad class selectors (.btn / [class*=submit]) are useful for finding
        // styled controls, but also match passive wrappers and checkbox labels.
        // Clicking a wrapper such as Carchs' 「この内容で送信する」 consent row
        // toggles the gate instead of submitting.  Only click semantic/actionable
        // controls; div-based controls remain supported via role/onclick.
        if (!isNativeControl(b)) continue;
        const meta = `${b.id || ''} ${b.className || ''} ${b.getAttribute('role') || ''}`;
        // [class*="submit"] also matches validation/status containers such as
        // .submit-error.  They are diagnostics, not controls; production once
        // logged their error sentence as the "clicked button".
        if (!isNativeControl(b) && (diagnosticMetaRe.test(meta) || diagnosticTextRe.test(txt))) continue;
        if (!isNativeControl(b) && b.querySelector(
          'button, input[type="submit"], input[type="button"], input[type="image"], a, [role="button"]'
        )) continue;
        if (!parts.some((p) => p && re.test(p))) continue;
        const style = window.getComputedStyle(b);
        const blocked = Boolean(
          b.disabled ||
          b.getAttribute('aria-disabled') === 'true' ||
          style.pointerEvents === 'none' ||
          style.visibility === 'hidden' ||
          style.display === 'none'
        );
        if (!blocked && b.offsetParent !== null) {
          commitAndClick(b);
          return {
            clicked: true,
            found_but_disabled: foundButDisabled,
            text: txt.slice(0, 50),
            scope: scope === document ? 'document' : 'form'
          };
        }
        foundButDisabled = true;
        disabledCandidates.push(txt.slice(0, 80));
      }
    }
  }
  return {
    clicked: false,
    found_but_disabled: foundButDisabled,
    disabled_candidates: disabledCandidates.slice(0, 8),
  };
}
"""

_PRE_FORM_ENTRY_PATTERNS = [
    r"メールフォームはこちら",
    r"お問い合わせフォームはこちら",
    r"フォームはこちら",
    r"同意してお問い合わせする",
    r"同意して.*お問い合わせ",
    r"同意して次へ",
]


def _fill_field(kind: str, value: str, patterns: list[str]) -> dict[str, Any] | None:
    args = {"kind": kind, "value": value, "patterns": patterns}
    js = f"""
    (() => {{
      const fn = {_FILL_FIELD_BY_LABEL_JS};
      return fn({json.dumps(args, ensure_ascii=False)});
    }})()
    """
    res = _evaluate(js)
    return res if isinstance(res, dict) else None


def _fill_radio(value: str) -> dict[str, Any] | None:
    args = {"value": value}
    js = f"""
    (() => {{
      const fn = {_FILL_RADIO_BY_LABEL_JS};
      return fn({json.dumps(args, ensure_ascii=False)});
    }})()
    """
    res = _evaluate(js)
    return res if isinstance(res, dict) else None


def _fill_select(value: str, label_pattern: str | None = None) -> dict[str, Any] | None:
    args = {"value": value, "label_pattern": label_pattern}
    js = f"""
    (() => {{
      const fn = {_FILL_SELECT_BY_TEXT_JS};
      return fn({json.dumps(args, ensure_ascii=False)});
    }})()
    """
    res = _evaluate(js)
    return res if isinstance(res, dict) else None


def _fill_textarea(value: str) -> dict[str, Any] | None:
    args = {"value": value}
    js = f"""
    (() => {{
      const fn = {_FILL_TEXTAREA_JS};
      return fn({json.dumps(args, ensure_ascii=False)});
    }})()
    """
    res = _evaluate(js)
    return res if isinstance(res, dict) else None


# --- v15: form-validation guardrails (furigana script + required subject) ----
# Read every visible text field with its label + current value so the pure
# `_outreach_core.form_validation` logic can decide what is wrong, then write
# corrected values back by *positional index* (stable for the lifetime of the
# page — the DOM does not change between the read and the write).
_READ_TEXT_FIELDS_JS = r"""
() => {
  const labelFor = (el) => {
    if (el.id) {
      try {
        const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (l) return (l.textContent || '').trim();
      } catch (e) {}
    }
    const wrap = el.closest('label');
    if (wrap) return (wrap.textContent || '').trim();
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return aria.trim();
    {
      let sib = el.previousSibling;
      while (sib) {
        if (sib.nodeType === 3) {
          const txt = sib.textContent.trim();
          if (txt && txt !== '-' && txt !== '−' && txt !== 'ー' && txt !== '/' && txt !== '／') {
            return txt;
          }
        } else if (sib.nodeType === 1) {
          const tag = sib.tagName;
          if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'BUTTON') break;
          if (tag !== 'BR') {
            const txt = (sib.textContent || '').trim();
            if (txt) return txt;
          }
        }
        sib = sib.previousSibling;
      }
    }
    const row = el.closest('tr, .form-row, .field, .input, .item, dl');
    if (row) {
      const l = row.querySelector('th, label, .label, [class*="label" i], strong, dt, .title');
      if (l && !l.contains(el)) return (l.textContent || '').trim();
    }
    const tr = el.closest('tr');
    if (tr) {
      let prev = tr.previousElementSibling;
      for (let i = 0; prev && i < 3; i++) {
        if (prev.querySelector('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="image"]), textarea, select')) break;
        const txt = (prev.textContent || '').replace(/\s+/g, ' ').trim();
        if (txt) return txt;
        prev = prev.previousElementSibling;
      }
    }
    let cur = el.parentElement;
    for (let i = 0; i < 4 && cur; i++, cur = cur.parentElement) {
      const l = cur.querySelector('label, .label, [class*="label" i], strong, th, dt');
      if (l && l !== el) return (l.textContent || '').trim();
    }
    return el.placeholder || el.name || '';
  };
  const isText = (el) => {
    const t = (el.type || '').toLowerCase();
    return t !== 'hidden' && t !== 'submit' && t !== 'button' && t !== 'radio'
      && t !== 'checkbox' && t !== 'file' && t !== 'image';
  };
  const els = Array.from(document.querySelectorAll('input, textarea')).filter(isText);
  return els.map((el, i) => ({
    idx: i,
    tag: el.tagName.toLowerCase(),
    type: (el.type || '').toLowerCase(),
    label: labelFor(el),
    name: el.name || el.id || '',
    value: el.value || '',
    required: !!(el.required || el.getAttribute('aria-required') === 'true'),
  }));
}
"""

_SET_TEXT_FIELDS_JS = r"""
(args) => {
  const setVal = (el, v) => {
    const proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    if (typeof el.focus === 'function') el.focus();
    if (typeof el.setCustomValidity === 'function') el.setCustomValidity('');
    setter.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
  };
  const isText = (el) => {
    const t = (el.type || '').toLowerCase();
    return t !== 'hidden' && t !== 'submit' && t !== 'button' && t !== 'radio'
      && t !== 'checkbox' && t !== 'file' && t !== 'image';
  };
  const els = Array.from(document.querySelectorAll('input, textarea')).filter(isText);
  let n = 0;
  for (const fix of (args.fixes || [])) {
    const el = els[fix.idx];
    if (el) { setVal(el, fix.value); n++; }
  }
  return { set: n };
}
"""


def _read_text_fields() -> list[dict[str, Any]]:
    res = _evaluate(_READ_TEXT_FIELDS_JS)
    return res if isinstance(res, list) else []


def _set_text_fields(fixes: list[dict[str, Any]]) -> int:
    if not fixes:
        return 0
    args = {"fixes": fixes}
    js = f"""
    (() => {{
      const fn = {_SET_TEXT_FIELDS_JS};
      return fn({json.dumps(args, ensure_ascii=False)});
    }})()
    """
    res = _evaluate(js)
    return int((res or {}).get("set", 0)) if isinstance(res, dict) else 0


def _apply_fill_guardrails(
    target: dict[str, Any],
    sender: dict[str, Any],
    body: str,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    """Deterministic post-fill corrections that prevent the two most common
    Japanese-form validation rejections (observed on the YAMAHA form):

      1. **Furigana script** — a フリガナ/カナ (or ふりがな) field that ended up with
         kanji or the wrong kana is overwritten with the correct reading, split
         姓/名 when the label asks for it.
      2. **Required subject/title** — an empty 件名/お問い合わせタイトル field is
         filled from the draft's subject (or a neutral B2B default).

    Runs after both the LLM plan and the heuristic fill, so it corrects mistakes
    regardless of which path produced them. Returns a small summary dict.
    """
    summary = {"kana_fixed": [], "postal_fixed": [], "subject_filled": None}
    try:
        from _outreach_core import form_validation as fv
    except Exception:
        return summary
    try:
        fields = _read_text_fields()
    except Exception:
        return summary
    if not fields:
        return summary

    fixes: list[dict[str, Any]] = []
    subject_value = fv.derive_subject((target.get("draft") or {}))
    subject_done = False
    for f in fields:
        label = f.get("label") or ""
        name = f.get("name") or ""
        value = f.get("value") or ""
        idx = f.get("idx")
        if idx is None:
            continue
        # Postal fields are most portable as seven ASCII digits.  LLM plans can
        # legitimately choose sender.postal_code (260-0003), but sites such as
        # KeePer reject the hyphen with 「数値を半角で入力してください」.  Normalize
        # after every fill path so target-specific overrides are unnecessary.
        if fv.is_postal_field_label(f"{label} {name}"):
            correct_postal = fv.normalize_postal_code(value)
            if correct_postal and correct_postal != value:
                fixes.append({"idx": idx, "value": correct_postal})
                summary["postal_fixed"].append(
                    f"{(label or name)[:24]}→{correct_postal}"
                )
        # (1) furigana fields — correct wrong script AND wrong sei/mei split, and
        #     fill required-but-empty kana fields. kana_field_correction returns
        #     the value to write (or None when the field is already correct).
        if fv.expected_kana_kind(label):
            correct = fv.kana_field_correction(label, value, sender)
            if not correct and (not value.strip()) and f.get("required"):
                correct = fv.furigana_value_for_label(label, sender)
            if correct and correct != value:
                fixes.append({"idx": idx, "value": correct})
                tag = "(empty)" if not value.strip() else ""
                summary["kana_fixed"].append(f"{label[:24]}→{correct}{tag}")
            continue
        # (2) required-but-empty subject/title
        if (not subject_done and not value.strip()
                and f.get("tag") == "input" and fv.is_subject_label(label)):
            fixes.append({"idx": idx, "value": subject_value})
            summary["subject_filled"] = f"{label[:24]}={subject_value}"
            subject_done = True

    if fixes:
        n = _set_text_fields(fixes)
        for entry in summary["kana_fixed"]:
            diagnostics.setdefault("filled", []).append(f"kana_guard:{entry}")
        for entry in summary["postal_fixed"]:
            diagnostics.setdefault("filled", []).append(f"postal_guard:{entry}")
        if summary["subject_filled"]:
            diagnostics.setdefault("filled", []).append(f"subject_guard:{summary['subject_filled']}")
        if n:
            diagnostics.setdefault("warnings", []).append(
                f"fill_guardrails corrected {n} field(s)"
            )
    return summary


# Phone-ish inputs with their CURRENT values — for hyphen-format toggling when
# the page complains 「電話番号を正しく入力してください」 (petline class, v25).
_PHONE_INPUTS_JS = r"""
(() => {
  const out = [];
  const phoneRe = /(電話|tel|phone|携帯|連絡先番号)/i;
  for (const el of document.querySelectorAll('input')) {
    const t = (el.type || '').toLowerCase();
    if (['hidden','submit','button','image','checkbox','radio','file'].includes(t)) continue;
    const name = el.name || el.id || '';
    let lbl = '';
    if (el.id) {
      try {
        const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (l) lbl = l.textContent || '';
      } catch (e) {}
    }
    const blob = `${name} ${el.placeholder || ''} ${lbl}`;
    if (t === 'tel' || phoneRe.test(blob)) {
      out.push({ name: name, value: String(el.value || '') });
    }
  }
  return out;
})()
"""


# Convert ASCII→full-width in inputs whose label/row context matches a zenkaku
# validation error (「住所（番地）は全角で入力してください」 — wacoal class, v26).
_FIX_ZENKAKU_JS_TMPL = r"""
(() => {
  const fields = __FIELDS__;
  const toZ = (s) => s
    .replace(/[!-~]/g, (c) => String.fromCharCode(c.charCodeAt(0) + 0xFEE0))
    .replace(/ /g, '　');
  const out = [];
  for (const el of document.querySelectorAll('input[type="text"], input:not([type]), textarea')) {
    const v = String(el.value || '');
    if (!v || !/[!-~]/.test(v)) continue;
    let ctx = (el.name || '') + ' ' + (el.placeholder || '');
    if (el.id) {
      try {
        const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (l) ctx += ' ' + (l.textContent || '');
      } catch (e) {}
    }
    const row = el.closest('tr, dl, .form-group, .form-row, li, p');
    if (row) {
      const h = row.querySelector('th, dt, label, .label, [class*="label" i]');
      if (h && !h.contains(el)) ctx += ' ' + (h.textContent || '');
    }
    ctx = ctx.replace(/\s+/g, '');
    if (!fields.some((f) => f && ctx.includes(f))) continue;
    const nv = toZ(v);
    if (nv === v) continue;
    const proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, nv);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    out.push({ name: el.name || el.id || '', value: nv.slice(0, 40) });
  }
  return out;
})()
"""


def _fix_zenkaku_errors(errors: list[dict[str, Any]]) -> list[str]:
    """ASCII→全角 conversion for fields named by zenkaku validation errors."""
    try:
        fields = [
            re.sub(r"\s+", "", str(e.get("field") or ""))
            for e in errors if e.get("kind") == "zenkaku"
        ]
        fields = [f for f in fields if len(f) >= 2]
        if not fields:
            return []
        js = _FIX_ZENKAKU_JS_TMPL.replace(
            "__FIELDS__", json.dumps(fields, ensure_ascii=False)
        )
        rows = _evaluate(js)
        return [
            f"zenkaku:{(r.get('name') or '?')[:24]}={r.get('value')}"
            for r in (rows if isinstance(rows, list) else [])
            if isinstance(r, dict)
        ]
    except Exception:  # noqa: BLE001 — recovery must not abort the send loop
        return []


_KANA_INPUTS_JS = r"""
(() => {
  const out = [];
  const kanaRe = /(フリガナ|ふりがな|カナ|かな|よみがな|読み仮名)/i;
  for (const el of document.querySelectorAll('input[type="text"], input:not([type])')) {
    const t = (el.type || '').toLowerCase();
    if (['hidden','submit','button','image','checkbox','radio','file'].includes(t)) continue;
    const name = el.name || el.id || '';
    let lbl = '';
    if (el.id) {
      try {
        const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (l) lbl = l.textContent || '';
      } catch (e) {}
    }
    const row = el.closest('tr, dl, .form-group, .form-row, li, p');
    let head = '';
    if (row) {
      const h = row.querySelector('th, dt, label, .label, [class*="label" i]');
      if (h && !h.contains(el)) head = h.textContent || '';
    }
    const blob = `${name} ${el.placeholder || ''} ${lbl} ${head}`;
    if (kanaRe.test(blob)) {
      out.push({ name: name, value: String(el.value || '') });
    }
  }
  return out;
})()
"""


def _fix_hiragana_errors(errors: list[dict[str, Any]]) -> list[str]:
    """v30 next — convert katakana in kana-class inputs to hiragana when the
    page surfaces 「ひらがなのみで入力してください」 (sunstar 2026-06-29 class).

    Returns diagnostics entries for every field rewritten. Never raises.
    Operates only when at least one ``kind=="hiragana"`` error is present so
    we never spuriously demote a legitimate katakana field.
    """
    from _outreach_core import form_validation as fv

    try:
        if not any(e.get("kind") == "hiragana" for e in errors):
            return []
        rows = _evaluate(_KANA_INPUTS_JS)
        fixed: list[str] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            cur = str(row.get("value") or "").strip()
            if not name or not cur:
                continue
            if fv.is_hiragana(cur):
                continue
            new = fv.katakana_to_hiragana(cur)
            if new == cur:
                continue
            out = _apply_field_action(name, "set_text", new)
            if out and out.get("ok"):
                fixed.append(f"hiragana:{name[:24]}={new[:24]}")
        return fixed
    except Exception:  # noqa: BLE001 — recovery must not abort the send loop
        return []


def _fix_phone_format_errors(errors: list[dict[str, Any]]) -> list[str]:
    """Toggle hyphen format on phone inputs when a format error names them.

    Returns diagnostics entries for every field rewritten. Never raises.
    """
    from _outreach_core import form_validation as fv

    try:
        if not any(
            e.get("kind") == "format" and fv.is_phone_field_label(e.get("field"))
            for e in errors
        ):
            return []
        rows = _evaluate(_PHONE_INPUTS_JS)
        fixed: list[str] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            cur = str(row.get("value") or "").strip()
            if not name or not cur:
                continue
            new = fv.toggle_phone_hyphens(cur)
            if new == cur:
                continue
            out = _apply_field_action(name, "set_text", new)
            if out and out.get("ok"):
                fixed.append(f"phone_format:{name[:24]}={new}")
        return fixed
    except Exception:  # noqa: BLE001 — recovery must not abort the send loop
        return []


def _harvest_and_fix_validation_errors(
    target: dict[str, Any],
    config: dict[str, Any],
    body: str,
    *,
    stage: str = "send",
    trace_dir: Path | None = None,
) -> dict[str, Any]:
    """After a submit click, read the page's own inline validation errors and try
    to fix them deterministically (furigana script, required subject), then report
    whether a corrective re-submit is worth attempting.

    Returns ``{"errors": [...], "fixed": <int>, "recoverable": bool}``. Never
    raises — a failure here must not abort the send loop.
    """
    out: dict[str, Any] = {"errors": [], "fixed": 0, "recoverable": False}
    try:
        from _outreach_core import form_validation as fv
        from _outreach_core.verify import PAGE_EVIDENCE_JS
    except Exception:
        return out
    try:
        ev_res = _evaluate(PAGE_EVIDENCE_JS)
        if isinstance(ev_res, dict):
            page_text = ev_res.get("text", "") or ""
            # v30 §WS-A — prefer the curated error containers (.error / [role=alert]
            # / aria-live) over body innerText so we parse only short, located
            # error messages, not body paragraphs. Fall back to innerText when
            # the curated text is empty (sites whose errors aren't tagged).
            curated = (
                ev_res.get("cf7_response_text")
                or ev_res.get("submission_status_text")
                or ""
            )
            combined = f"{curated}\n{page_text}" if curated else page_text
        else:
            combined = ""
        # The aria-snapshot tree is intentionally NOT included here. It used to
        # be concatenated for "more signal" but caused phantom required-field
        # captures from body paragraphs and row labels — see
        # form_validation._ARIA_TREE_LINE_RE and the WS-A regression tests.
    except Exception:
        return out

    errors = fv.parse_validation_errors(combined)
    out["errors"] = errors
    if not errors:
        return out

    sender = config.get("sender", {}) or {}
    diagnostics: dict[str, Any] = {"filled": [], "warnings": []}
    summary = _apply_fill_guardrails(target, sender, body, diagnostics)
    phone_fixed = _fix_phone_format_errors(errors)
    zenkaku_fixed = _fix_zenkaku_errors(errors)
    # v30 next — convert kana fields to hiragana when the page surfaces
    # 「ひらがなのみで入力してください」. Pairs with the parser's new
    # _ERR_HIRAGANA_RE so the trio (zenkaku-length, hiragana, phone-format)
    # all get actively rescued instead of looping in validation_unrecoverable.
    hiragana_fixed = _fix_hiragana_errors(errors)
    fixed = (
        len(summary.get("kana_fixed") or [])
        + len(summary.get("postal_fixed") or [])
        + (1 if summary.get("subject_filled") else 0)
        + len(phone_fixed)
        + len(zenkaku_fixed)
        + len(hiragana_fixed)
    )
    out["fixed"] = fixed
    out["phone_fixed"] = phone_fixed
    out["zenkaku_fixed"] = zenkaku_fixed
    out["hiragana_fixed"] = hiragana_fixed
    # Recoverable if we fixed something, or if all errors are kinds our guardrails
    # target (format on a kana field / a required subject). Even with fixed==0 the
    # caller may still benefit from re-clicking once after a generic re-fill.
    out["recoverable"] = fixed > 0
    if trace_dir is not None:
        try:
            _emit_event(
                f"{stage}.validation_errors",
                stage=stage,
                target_id=str(target.get("id") or target.get("name") or "?"),
                payload={
                    "errors": [{"field": e["field"][:40], "kind": e["kind"]} for e in errors][:8],
                    "fixed": fixed,
                    "kana_fixed": (summary.get("kana_fixed") or [])[:6],
                    "postal_fixed": (summary.get("postal_fixed") or [])[:6],
                    "subject_filled": summary.get("subject_filled"),
                    "phone_fixed": phone_fixed[:4],
                },
                trace_dir=trace_dir,
            )
        except Exception:
            pass
    return out


def _auto_fill_known_selects(sender: dict[str, Any]) -> list[str]:
    """Fill required / known-label <select>s via the label-aware pure chooser.

    Returns human-readable diagnostics entries for everything it set.
    """
    from _outreach_core import form_validation as fv

    res = _evaluate(_LIST_SELECT_GATES_JS)
    groups = res if isinstance(res, list) else []
    filled: list[str] = []
    for g in groups:
        if not isinstance(g, dict) or bool(g.get("selected")):
            continue
        name = str(g.get("name") or g.get("id") or "").strip()
        if not name:
            continue
        label = str(g.get("label") or "")
        required = bool(g.get("required"))
        if not required and not fv.KNOWN_CHOICE_LABEL_RE.search(label):
            continue
        choice = fv.choose_option_for_label(label, g.get("options") or [], sender)
        if not choice:
            continue
        out = _apply_field_action(name, "select_option", choice["value"])
        if out and out.get("ok"):
            filled.append(
                f"select:{(label or name)[:20]}={choice['value'][:24]} ({choice['reason']})"
            )
            time.sleep(0.15)
    return filled


def _auto_check_submit_gates() -> dict[str, Any]:
    """Check required/agreement checkboxes that often block submit progression."""
    res = _evaluate(_LIST_CHECKBOX_GATES_JS)
    checkboxes = res if isinstance(res, list) else []
    to_check = core_submit_progress.pick_checkboxes_to_check(checkboxes)
    checked_labels: list[str] = []
    checked_count = 0
    for cb in to_check:
        name = (cb.get("name") or cb.get("id") or "").strip()
        selector = str(cb.get("selector") or "").strip()
        # Prefer the exact selector even when a name exists. Checkbox groups
        # share names, and name-only lookup would always toggle option 1.
        if selector:
            name = f"selector:{selector}"
        out = _check_by_name(name) if name else None
        if not (out and out.get("ok")):
            label = (cb.get("label") or "").strip()
            out = _check_by_label(label) if label else None
        if out and out.get("ok"):
            checked_count += 1
            checked_labels.append((cb.get("label") or name or "?")[:80])
    return {
        "checked_count": checked_count,
        "checked_labels": checked_labels,
        "candidates": len(to_check),
        "total_checkboxes": len(checkboxes),
    }


def _snapshot_native_validation(
    *,
    trace_dir: Path | None = None,
    target_id: str | None = None,
    phase: str = "send",
) -> dict[str, Any]:
    """Read field-level native validity without opening browser validation UI."""
    raw = _evaluate(_FORM_CONSTRAINTS_JS)
    out = raw if isinstance(raw, dict) else {
        "form_found": False,
        "control_count": 0,
        "invalid_count": 0,
        "valid": True,
        "invalid": [],
    }
    invalid = out.get("invalid") if isinstance(out.get("invalid"), list) else []
    out["invalid"] = [x for x in invalid if isinstance(x, dict)][:40]
    out["invalid_count"] = int(out.get("invalid_count") or len(out["invalid"]))
    if out["invalid_count"] > 0:
        _emit_event(
            "send.native_validation.invalid",
            stage="send",
            target_id=target_id or "",
            payload={
                "phase": phase,
                "invalid_count": out["invalid_count"],
                "invalid": [
                    {
                        "field": str(x.get("label") or x.get("name") or x.get("id") or "?")[:80],
                        "name": str(x.get("name") or "")[:80],
                        "type": str(x.get("type") or "")[:30],
                        "reasons": list(x.get("reasons") or [])[:6],
                        "message": str(x.get("message") or "")[:160],
                    }
                    for x in out["invalid"][:12]
                ],
            },
            trace_dir=trace_dir,
        )
    return out


def _native_validation_errors(snapshot: dict[str, Any] | None) -> list[dict[str, str]]:
    """Convert native validity rows to the submission-loop error schema."""
    if not isinstance(snapshot, dict):
        return []
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in snapshot.get("invalid") or []:
        if not isinstance(row, dict):
            continue
        field = str(row.get("label") or row.get("name") or row.get("id") or "unknown")
        reasons = [str(x) for x in (row.get("reasons") or []) if str(x)]
        kind = "+".join(reasons) or "invalid"
        message = str(row.get("message") or "").strip()
        dedupe_key = (str(row.get("name") or field).strip(), kind)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        out.append({"field": field[:80], "kind": kind[:80], "message": message[:200]})
    return out


# Sender context for gate auto-fill decisions. Set once per target by
# fill_form_for_target / stage_send; read by the _auto_select_* helpers, which
# are called deep inside click-retry loops where no config is in scope.
_SENDER_CTX: dict[str, Any] = {}


def _set_sender_ctx(config: dict[str, Any] | None) -> None:
    global _SENDER_CTX
    _SENDER_CTX = dict((config or {}).get("sender") or {})


def _auto_select_submit_radios(aggressive: bool = False) -> dict[str, Any]:
    """Select likely radio gates (法人/提案系/その他) to unblock submit.

    ``aggressive=True`` (validation_error recovery): answer EVERY unselected
    group — the page itself has said a selection is missing, so the
    required-attribute / known-label gating no longer applies (kakuyasu class:
    visual 必須 mark without a DOM required attribute → gate scan saw nothing).
    """
    res = _evaluate(_LIST_RADIO_GATES_JS)
    groups = res if isinstance(res, list) else []
    if aggressive:
        actions = core_submit_progress.pick_validation_radio_actions(groups, sender=_SENDER_CTX)
    else:
        actions = core_submit_progress.pick_radio_gate_actions(groups, sender=_SENDER_CTX)
    selected_count = 0
    selected_items: list[str] = []
    for act in actions:
        out = _apply_field_action(act["name"], "select_radio", act["value"])
        if out and out.get("ok"):
            selected_count += 1
            selected_items.append(f"{act['name']}={act['value']}")
    return {
        "selected_count": selected_count,
        "selected_items": selected_items,
        "candidates": len(actions),
        "total_groups": len(groups),
    }


def _auto_select_submit_selects(aggressive: bool = False) -> dict[str, Any]:
    """Select required / known dropdown gates to unblock submit."""
    res = _evaluate(_LIST_SELECT_GATES_JS)
    groups = res if isinstance(res, list) else []
    actions = (
        core_submit_progress.pick_validation_select_actions(groups, sender=_SENDER_CTX)
        if aggressive else
        core_submit_progress.pick_select_gate_actions(groups, sender=_SENDER_CTX)
    )
    selected_count = 0
    selected_items: list[str] = []
    for act in actions:
        out = _apply_field_action(act["name"], "select_option", act["value"])
        if out and out.get("ok"):
            selected_count += 1
            selected_items.append(f"{act['name']}={act['value']}")
    return {
        "selected_count": selected_count,
        "selected_items": selected_items,
        "candidates": len(actions),
        "total_groups": len(groups),
    }


def _auto_fill_live_gates(
    *,
    phase: str,
    aggressive_radios: bool = False,
    aggressive_selects: bool = False,
    trace_dir: Path | None = None,
    target_id: str | None = None,
) -> dict[str, Any]:
    """Apply the same gate-filling pass across pre/body/confirm phases."""
    gate = _auto_check_submit_gates()
    radios = _auto_select_submit_radios(aggressive=aggressive_radios)
    selects = _auto_select_submit_selects(aggressive=aggressive_selects)
    changed = (
        int(gate.get("checked_count") or 0)
        + int(radios.get("selected_count") or 0)
        + int(selects.get("selected_count") or 0)
    )
    out = {
        "changed": changed,
        "checkboxes": gate,
        "radios": radios,
        "selects": selects,
    }
    if changed > 0:
        _emit_event(
            "send.live_gates.filled",
            stage="send" if phase in ("send", "confirm", "validation") else phase,
            target_id=target_id or "",
            payload={
                "phase": phase,
                "checkboxes": int(gate.get("checked_count") or 0),
                "radios": int(radios.get("selected_count") or 0),
                "selects": int(selects.get("selected_count") or 0),
                "aggressive_radios": bool(aggressive_radios),
                "aggressive_selects": bool(aggressive_selects),
            },
            trace_dir=trace_dir,
        )
    return out


def _snapshot_submit_gates() -> dict[str, Any]:
    boxes = _evaluate(_LIST_CHECKBOX_GATES_JS)
    radios = _evaluate(_LIST_RADIO_GATES_JS)
    selects = _evaluate(_LIST_SELECT_GATES_JS)
    box_rows = boxes if isinstance(boxes, list) else []
    radio_rows = radios if isinstance(radios, list) else []
    select_rows = selects if isinstance(selects, list) else []
    remaining = core_submit_progress.summarize_remaining_submit_gates(
        box_rows,
        radio_rows,
        select_rows,
    )
    return {
        "checkboxes": box_rows,
        "radios": radio_rows,
        "selects": select_rows,
        "remaining": remaining,
    }


def _rescan_form_fields(target: dict[str, Any]) -> dict[str, Any]:
    fresh = _evaluate(_FORM_FIELDS_JS) or {}
    if isinstance(fresh, dict):
        target["form_fields"] = fresh
        if fresh.get("form_root_selector"):
            target["form_root_selector"] = fresh["form_root_selector"]
        return fresh
    return target.get("form_fields") or {}


def _route_choice_action(
    target: dict[str, Any],
    plan: dict[str, Any] | None,
    gate_snapshot: dict[str, Any],
) -> dict[str, str] | None:
    overrides = target.get("field_map_overrides", {}) or {}
    override_value = str(
        overrides.get("route_radio")
        or overrides.get("category_radio")
        or ""
    ).strip()
    radios = gate_snapshot.get("radios") if isinstance(gate_snapshot, dict) else []
    act = core_submit_progress.pick_route_radio_action(
        radios if isinstance(radios, list) else [],
        override_value=override_value or None,
    )
    if act:
        return act
    route_choice = (plan or {}).get("route_choice")
    if isinstance(route_choice, dict):
        name = str(route_choice.get("name") or "").strip()
        value = str(route_choice.get("value") or "").strip()
        if name and value:
            return {"name": name, "value": value}
    return None


def _drive_enable_sequence(
    target: dict[str, Any],
    plan: dict[str, Any] | None,
    *,
    body: str = "",
    stage: str,
    trace_dir: Path | None = None,
    max_steps: int = 4,
    extra_patterns: list[str] | None = None,
) -> dict[str, Any]:
    """Observe->act->re-observe loop for pre-form gate activation."""
    flow = (
        target.get("flow")
        or (plan or {}).get("next_step")
        or (target.get("_llm_plan") or {}).get("next_step")
        or "single"
    )
    patterns = (
        [r"入力内容を確認", r"送信内容を確認", r"内容(を|の)?確認", r"確認画面", r"確認する", r"^次へ$"]
        if flow == "confirm"
        else [r"送信する", r"^送信$", r"submit", r"内容を送信"]
    )
    plan_first = str((plan or {}).get("first_button_pattern") or "").strip()
    if plan_first and plan_first not in patterns:
        patterns.append(plan_first)
    for pat in extra_patterns or []:
        if pat and pat not in patterns:
            patterns.append(pat)
    seq = list((plan or {}).get("enable_sequence") or [])
    seq_idx = 0

    applied: list[str] = []
    for step in range(max(1, max_steps)):
        click_state = _click_button(patterns, form_root_selector=target.get("form_root_selector"))
        if click_state and click_state.get("clicked"):
            return {
                "clicked": True,
                "steps": step + 1,
                "click_res": click_state,
                "applied": applied,
                "remaining": {"total": 0, "checkboxes": [], "radios": [], "selects": []},
            }

        snap = _snapshot_submit_gates()
        route = _route_choice_action(target, plan, snap)
        changed = 0

        if seq_idx < len(seq):
            step_item = seq[seq_idx]
            seq_idx += 1
            if isinstance(step_item, dict):
                action = str(step_item.get("action") or "").strip()
                name = str(step_item.get("name") or "").strip()
                value = str(step_item.get("value") or "").strip()
                label = str(step_item.get("label") or "").strip()
                if value == "__BODY__":
                    value = "__BODY__"
                if action == "RESCAN":
                    _rescan_form_fields(target)
                    changed += 1
                    applied.append("seq:RESCAN")
                elif action in ("select_radio", "select_option", "set_text", "click") and name:
                    if action == "click":
                        out = _click_button([value or r"確認|送信"], form_root_selector=target.get("form_root_selector"))
                        if out and out.get("clicked"):
                            return {
                                "clicked": True,
                                "steps": step + 1,
                                "click_res": out,
                                "applied": applied + [f"seq:click:{value or name}"],
                                "remaining": {"total": 0, "checkboxes": [], "radios": [], "selects": []},
                            }
                    else:
                        v = value if value != "__BODY__" else body
                        out = _apply_field_action(name, action, v, selector=name[len("selector:"):] if name.startswith("selector:") else None)
                        if out and out.get("ok"):
                            changed += 1
                            applied.append(f"seq:{action}:{name}")
                            _rescan_form_fields(target)
                            snap = _snapshot_submit_gates()
                elif action == "check" and (name or label):
                    out = _check_by_name(name) if name else None
                    if not (out and out.get("ok")) and label:
                        out = _check_by_label(label)
                    if out and out.get("ok"):
                        changed += 1
                        applied.append(f"seq:check:{name or label}")
                        _rescan_form_fields(target)
                        snap = _snapshot_submit_gates()

        if route:
            out = _apply_field_action(route["name"], "select_radio", route["value"])
            if out and out.get("ok"):
                changed += 1
                applied.append(f"route:{route['name']}={route['value']}")
                _rescan_form_fields(target)
                snap = _snapshot_submit_gates()

        inquiry = _ensure_inquiry_type_action(target, plan or {}, stage=stage, trace_dir=trace_dir)
        if int(inquiry.get("selected") or 0) > 0:
            changed += int(inquiry.get("selected") or 0)
            applied.append(f"inquiry:{int(inquiry.get('selected') or 0)}")
            _rescan_form_fields(target)
            snap = _snapshot_submit_gates()

        live = _auto_fill_live_gates(
            phase=stage,
            trace_dir=trace_dir,
            target_id=str(target.get("id") or target.get("name") or ""),
        )
        if int(live.get("changed") or 0) > 0:
            changed += int(live.get("changed") or 0)
            gate = live.get("checkboxes") or {}
            radios = live.get("radios") or {}
            selects = live.get("selects") or {}
            applied.append(
                f"gates:cb={gate.get('checked_count',0)},r={radios.get('selected_count',0)},s={selects.get('selected_count',0)}"
            )
            _rescan_form_fields(target)
            snap = _snapshot_submit_gates()

        _emit_event(
            "send.enable_sequence.step",
            stage=stage,
            target_id=str(target.get("id") or target.get("name") or ""),
            payload={
                "step": step + 1,
                "changed": changed,
                "remaining_total": int((snap.get("remaining") or {}).get("total") or 0),
            },
            trace_dir=trace_dir,
        )
        if changed <= 0:
            break

    last = _snapshot_submit_gates()
    return {
        "clicked": False,
        "steps": max(1, max_steps),
        "applied": applied,
        "remaining": last.get("remaining") or {"total": 0, "checkboxes": [], "radios": [], "selects": []},
    }


def _click_button(patterns: list[str],
                   form_root_selector: str | None = None) -> dict[str, Any] | None:
    args = {"patterns": patterns, "formRootSelector": form_root_selector}
    js = f"""
    (() => {{
      const fn = {_CLICK_BUTTON_BY_TEXT_JS};
      return fn({json.dumps(args, ensure_ascii=False)});
    }})()
    """
    res = _evaluate(js)
    return res if isinstance(res, dict) else None


def _click_button_with_gate_retry(
    patterns: list[str],
    *,
    form_root_selector: str | None = None,
    retries: int = 2,
) -> dict[str, Any] | None:
    """Try clicking submit; if blocked by gate fields, auto-fill then retry."""
    last = _click_button(patterns, form_root_selector=form_root_selector)
    if last and last.get("clicked"):
        return last
    for _ in range(max(1, retries)):
        live = _auto_fill_live_gates(phase="click_retry")
        gate = live.get("checkboxes") or {}
        radios = live.get("radios") or {}
        selects = live.get("selects") or {}
        changed = int(live.get("changed") or 0)
        if changed <= 0:
            break
        time.sleep(0.4)
        last = _click_button(patterns, form_root_selector=form_root_selector)
        if last and last.get("clicked"):
            last["gate_auto_checked"] = gate.get("checked_count", 0)
            last["gate_labels"] = gate.get("checked_labels") or []
            last["radio_auto_selected"] = radios.get("selected_count", 0)
            last["radio_items"] = radios.get("selected_items") or []
            last["select_auto_selected"] = selects.get("selected_count", 0)
            last["select_items"] = selects.get("selected_items") or []
            return last
    return last


def _try_open_pre_form_gate() -> dict[str, Any] | None:
    """Open intermediate contact-gate pages before the real textarea form."""
    return _click_button_with_gate_retry(_PRE_FORM_ENTRY_PATTERNS, retries=2)


def _advance_pre_form_phase(
    target: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    stage: str,
    trace_dir: Path | None = None,
    max_rounds: int = 3,
) -> dict[str, Any]:
    """Drive pre-form route/consent pages until the real textarea form appears.

    This deliberately runs only while the page looks like a gate (no inquiry
    textarea yet). It is used by both enrich and send so a valid contact flow is
    not discarded just because the first URL is an intermediate "法人/同意/種別"
    page.
    """
    _set_sender_ctx(config)
    tid = str(target.get("id") or target.get("name") or "")
    advanced = False
    rounds: list[dict[str, Any]] = []
    last_fields: dict[str, Any] = {}
    last_snap = ""

    for round_no in range(max(1, max_rounds)):
        last_fields = _rescan_form_fields(target)
        last_snap = oc_browser("snapshot") or ""
        page_state = core_contact_url.classify_page_form_state(last_fields, last_snap)
        kind, reason = core_contact_url.classify_form_type(last_fields, last_snap)
        has_textarea = bool(last_fields.get("textareas") or [])
        if has_textarea and page_state.get("state") == "form_ok":
            return {
                "advanced": advanced,
                "state": "form_ok",
                "fields": last_fields,
                "snap": last_snap,
                "rounds": rounds,
            }
        gate_like = (
            page_state.get("state") == "gate_like"
            or (kind == "contact" and reason == "pre_form_gate")
        )
        if not gate_like:
            break

        drive = _drive_enable_sequence(
            target,
            target.get("_llm_plan") if isinstance(target.get("_llm_plan"), dict) else None,
            body="",
            stage=stage,
            trace_dir=trace_dir,
            max_steps=3,
            extra_patterns=_PRE_FORM_ENTRY_PATTERNS + [
                r"法人.*お問い合わせ",
                r"法人.*問合せ",
                r"企業.*お問い合わせ",
                r"お問い合わせへ進む",
                r"次へ進む",
            ],
        )
        rounds.append(
            {
                "round": round_no + 1,
                "clicked": bool(drive.get("clicked")),
                "applied": (drive.get("applied") or [])[:8],
                "remaining": drive.get("remaining") or {},
            }
        )
        if drive.get("clicked"):
            advanced = True
            time.sleep(2.0)
            continue
        if not (drive.get("applied") or []):
            break
        time.sleep(0.8)

    last_fields = _rescan_form_fields(target)
    last_snap = oc_browser("snapshot") or ""
    _emit_event(
        "send.pre_form.advance" if stage == "pre_form" else "enrich.pre_form.advance",
        stage="send" if stage == "pre_form" else stage,
        target_id=tid,
        payload={
            "advanced": advanced,
            "rounds": rounds,
            "current_url": str(_evaluate("() => location.href") or "")[:200],
        },
        trace_dir=trace_dir,
    )
    return {
        "advanced": advanced,
        "state": core_contact_url.classify_page_form_state(last_fields, last_snap).get("state"),
        "fields": last_fields,
        "snap": last_snap,
        "rounds": rounds,
    }


_ENUMERATE_BUTTONS_JS = r"""
(args) => {
  const { formRootSelector, filledNames } = args;
  const selector =
    'button, input[type="submit"], input[type="button"], input[type="image"], a, [role="button"], ' +
    '[onclick], .btn, [class*="btn" i], [class*="submit" i], [class*="confirm" i]';
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const toSet = (arr) => new Set((arr || []).map((x) => String(x || '').trim()).filter(Boolean));
  const filledSet = toSet(filledNames);
  const stableSelector = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    const name = el.getAttribute('name');
    if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
    const role = el.getAttribute('role');
    if (role) return `${el.tagName.toLowerCase()}[role="${CSS.escape(role)}"]`;
    let cur = el;
    const path = [];
    for (let depth = 0; cur && depth < 6 && cur !== document.body; depth += 1) {
      let part = cur.tagName.toLowerCase();
      if (cur.parentElement) {
        const sibs = Array.from(cur.parentElement.children).filter((x) => x.tagName === cur.tagName);
        if (sibs.length > 1) part += `:nth-of-type(${sibs.indexOf(cur) + 1})`;
      }
      path.unshift(part);
      cur = cur.parentElement;
    }
    return path.join(' > ');
  };
  const visibleEnabled = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const blocked = Boolean(
      el.disabled ||
      el.getAttribute('aria-disabled') === 'true' ||
      style.pointerEvents === 'none' ||
      style.visibility === 'hidden' ||
      style.display === 'none'
    );
    return !blocked && el.offsetParent !== null;
  };
  const actionable = (el) => {
    const tag = String(el.tagName || '').toLowerCase();
    return tag === 'button' || tag === 'input' || tag === 'a' ||
      el.getAttribute('role') === 'button' || el.hasAttribute('onclick');
  };
  const hasSubmitControl = (form) => {
    if (!form) return false;
    return !!form.querySelector('button[type="submit"],input[type="submit"],input[type="image"]');
  };
  const formScore = (form) => {
    if (!form) return -9999;
    if (form.querySelector('input[type="password"]')) return -9999;
    const role = String(form.getAttribute('role') || '').toLowerCase();
    const action = String(form.getAttribute('action') || '').toLowerCase();
    if (role === 'search') return -9999;
    if (/(search|login|logout)/.test(action)) return -9999;
    let s = 0;
    if (hasSubmitControl(form)) s += 5;
    if (!form.closest('header, footer, nav')) s += 2;
    const hidden = Array.from(form.querySelectorAll('input[type="hidden"]'));
    s += Math.min(4, hidden.length);
    if (filledSet.size > 0) {
      let hits = 0;
      for (const h of hidden) {
        const nm = String(h.getAttribute('name') || '').trim();
        if (nm && filledSet.has(nm)) hits += 1;
      }
      s += Math.min(8, hits * 2);
    }
    return s;
  };
  const pickTargetForm = () => {
    if (formRootSelector) {
      try {
        const forced = document.querySelector(formRootSelector);
        if (forced && forced.tagName === 'FORM') return forced;
      } catch (e) {}
    }
    const forms = Array.from(document.querySelectorAll('form'));
    if (!forms.length) return null;
    forms.sort((a, b) => formScore(b) - formScore(a));
    const top = forms[0];
    if (!top || formScore(top) < 0) return null;
    return top;
  };
  const targetForm = pickTargetForm();
  const targetFormSig = targetForm ? stableSelector(targetForm) : '';
  let scope = document;
  if (formRootSelector) {
    try {
      const root = document.querySelector(formRootSelector);
      if (root) scope = root;
    } catch (e) {}
  }
  const buttons = Array.from(scope.querySelectorAll(selector));
  const out = [];
  buttons.forEach((b, idx) => {
    if (!actionable(b)) return;
    if (!visibleEnabled(b)) return;
    const txt = norm(
      (b.textContent || b.value || '') + ' ' +
      (b.getAttribute('aria-label') || '') + ' ' +
      (b.getAttribute('title') || '') + ' ' +
      (b.getAttribute('alt') || '') + ' ' +
      (b.getAttribute('name') || '')
    );
    const btnType = String(b.getAttribute('type') || '').toLowerCase();
    if (!txt && !['submit', 'button', 'image'].includes(btnType)) return;
    if (txt.length > 120) return;
    const className = norm(b.className || '');
    const ownerForm = b.closest('form');
    const ownerSig = ownerForm ? stableSelector(ownerForm) : '';
    const isSubmitType = (
      btnType === 'submit' || btnType === 'image' ||
      (b.tagName.toLowerCase() === 'button' && (btnType === '' || btnType === 'submit'))
    );
    out.push({
      idx,
      text: txt,
      tag: b.tagName.toLowerCase(),
      type: b.getAttribute('type') || '',
      role: b.getAttribute('role') || '',
      name: b.getAttribute('name') || '',
      id: b.id || '',
      class_name: className.slice(0, 120),
      href: b.getAttribute('href') || '',
      selector: stableSelector(b),
      is_submit_type: isSubmitType,
      in_form: Boolean(targetFormSig && ownerSig && ownerSig === targetFormSig),
      form_sig: ownerSig,
      intent_hint: norm(
        (b.getAttribute('type') || '') + ' ' +
        (b.getAttribute('onclick') || '') + ' ' +
        className
      ).slice(0, 160),
    });
  });
  return { scope: scope === document ? 'document' : 'form', target_form_sig: targetFormSig, buttons: out };
}
"""


_CLICK_BY_SELECTOR_JS = r"""
(args) => {
  const { selector } = args;
  if (!selector) return { ok: false, reason: "empty_selector" };
  let el = null;
  try {
    el = document.querySelector(selector);
  } catch (e) {
    return { ok: false, reason: "invalid_selector", selector };
  }
  if (!el) return { ok: false, reason: "not_found", selector };
  const style = window.getComputedStyle(el);
  const blocked = Boolean(
    el.disabled ||
    el.getAttribute('aria-disabled') === 'true' ||
    style.pointerEvents === 'none' ||
    style.visibility === 'hidden' ||
    style.display === 'none'
  );
  if (blocked || el.offsetParent === null) return { ok: false, reason: "blocked", selector };
  el.click();
  return {
    ok: true,
    selector,
    text: ((el.textContent || el.value || '') + ' ' + (el.getAttribute('aria-label') || '')).trim().slice(0, 80),
  };
}
"""


_SUBMIT_TARGET_FORM_JS = r"""
(args) => {
  const { formRootSelector, filledNames } = args;
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const toSet = (arr) => new Set((arr || []).map((x) => String(x || '').trim()).filter(Boolean));
  const filledSet = toSet(filledNames);
  const stableSelector = (el) => {
    if (!el) return '';
    if (el.id) return `#${CSS.escape(el.id)}`;
    const name = el.getAttribute('name');
    if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
    let cur = el;
    const path = [];
    for (let depth = 0; cur && depth < 7 && cur !== document.body; depth += 1) {
      let part = cur.tagName.toLowerCase();
      if (cur.parentElement) {
        const sibs = Array.from(cur.parentElement.children).filter((x) => x.tagName === cur.tagName);
        if (sibs.length > 1) part += `:nth-of-type(${sibs.indexOf(cur) + 1})`;
      }
      path.unshift(part);
      cur = cur.parentElement;
    }
    return path.join(' > ');
  };
  const visibleEnabled = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const blocked = Boolean(
      el.disabled ||
      el.getAttribute('aria-disabled') === 'true' ||
      style.pointerEvents === 'none' ||
      style.visibility === 'hidden' ||
      style.display === 'none'
    );
    return !blocked && el.offsetParent !== null;
  };
  const score = (form) => {
    if (!form) return -9999;
    if (form.querySelector('input[type="password"]')) return -9999;
    const role = String(form.getAttribute('role') || '').toLowerCase();
    const action = String(form.getAttribute('action') || '').toLowerCase();
    if (role === 'search') return -9999;
    if (/(search|login|logout)/.test(action)) return -9999;
    if (form.closest('header,footer,nav') && !form.closest('main,article,section')) return -5;
    let s = 0;
    const submits = Array.from(form.querySelectorAll('button[type="submit"],input[type="submit"],input[type="image"]'));
    s += submits.length > 0 ? 5 : 0;
    if (!form.closest('header, footer, nav')) s += 2;
    const hidden = Array.from(form.querySelectorAll('input[type="hidden"]'));
    s += Math.min(4, hidden.length);
    if (filledSet.size > 0) {
      let hits = 0;
      for (const h of hidden) {
        const nm = String(h.getAttribute('name') || '').trim();
        if (nm && filledSet.has(nm)) hits += 1;
      }
      s += Math.min(8, hits * 2);
    }
    return s;
  };
  let target = null;
  if (formRootSelector) {
    try {
      const forced = document.querySelector(formRootSelector);
      if (forced && forced.tagName === 'FORM') target = forced;
    } catch (e) {}
  }
  if (!target) {
    const forms = Array.from(document.querySelectorAll('form'));
    forms.sort((a, b) => score(b) - score(a));
    if (forms.length && score(forms[0]) >= 0) target = forms[0];
  }
  if (!target) return { method: "none", reason: "no_target_form", form_sig: "" };
  const formSig = stableSelector(target);
  const controls = Array.from(target.querySelectorAll('button[type="submit"],input[type="submit"],input[type="image"]'));
  const submitter = controls.find((el) => visibleEnabled(el)) || controls[0] || null;
  if (submitter && visibleEnabled(submitter)) {
    submitter.click();
    return {
      method: "click_submit",
      form_sig: formSig,
      control_text: norm((submitter.textContent || submitter.value || submitter.getAttribute('aria-label') || submitter.getAttribute('alt') || '')),
      reason: "clicked submit control",
    };
  }
  if (typeof target.requestSubmit === 'function') {
    if (submitter) target.requestSubmit(submitter);
    else target.requestSubmit();
    return {
      method: "requestSubmit",
      form_sig: formSig,
      control_text: submitter ? norm(submitter.textContent || submitter.value || '') : "",
      reason: submitter ? "requestSubmit(submitter)" : "requestSubmit()",
    };
  }
  return { method: "none", reason: "requestSubmit_unavailable", form_sig: formSig };
}
"""


def _enumerate_buttons(
    form_root_selector: str | None = None,
    *,
    filled_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    args = {"formRootSelector": form_root_selector, "filledNames": filled_names or []}
    js = f"""
    (() => {{
      const fn = {_ENUMERATE_BUTTONS_JS};
      return fn({json.dumps(args, ensure_ascii=False)});
    }})()
    """
    res = _evaluate(js)
    if isinstance(res, dict):
        return res.get("buttons") or []
    return []


def _filled_name_hints(d: dict[str, Any]) -> list[str]:
    hints: list[str] = []
    plan = d.get("_llm_plan") or {}
    for e in plan.get("fields") or []:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        if not name or name.startswith("selector:"):
            continue
        hints.append(name)
    return sorted(set(hints))[:120]


def _submit_native(
    d: dict[str, Any],
    *,
    form_root_selector: str | None = None,
) -> dict[str, Any]:
    args = {
        "formRootSelector": form_root_selector,
        "filledNames": _filled_name_hints(d),
    }
    js = f"""
    (() => {{
      const fn = {_SUBMIT_TARGET_FORM_JS};
      return fn({json.dumps(args, ensure_ascii=False)});
    }})()
    """
    res = _evaluate(js)
    out = res if isinstance(res, dict) else {}
    method = str(out.get("method") or "none")
    out["clicked"] = method in ("click_submit", "requestSubmit")
    return out


def _native_submit_diag(native: dict[str, Any] | None) -> str:
    n = native or {}
    return (
        f"native submit: method={n.get('method', 'none')}, "
        f"reason={n.get('reason', '')}, form={n.get('form_sig', '')}"
    )


def _post_form_llm_gate_action(
    target: dict[str, Any],
    config: dict[str, Any],
    *,
    stage: str,
    trace_dir: Path | None = None,
) -> dict[str, Any]:
    """Confirm-page LLM pass: detect remaining consent gates before final submit."""
    _rescan_form_fields(target)
    plan = _llm_analyze_form(
        target,
        config,
        phase="confirm",
        force_refresh=True,
        escalation_reason="post_form_confirm",
    )
    if not isinstance(plan, dict):
        return {"checked": 0, "plan": None}
    checked = 0
    for cb in plan.get("checkboxes_to_check") or []:
        cb_name = ""
        cb_label = ""
        if isinstance(cb, dict):
            cb_name = str(cb.get("name") or "").strip()
            cb_label = str(cb.get("label") or "").strip()
        else:
            cb_name = str(cb or "").strip()
            cb_label = cb_name
        res = _check_by_name(cb_name) if cb_name else None
        if not (res and res.get("ok")) and cb_label:
            res = _check_by_label(cb_label)
        if res and res.get("ok"):
            checked += 1
    _emit_event(
        "send.post_form.plan",
        stage=stage,
        target_id=str(target.get("id") or target.get("name") or ""),
        payload={
            "checked_boxes": checked,
            "has_submit_gate": bool(plan.get("submit_gate")),
            "warnings": (plan.get("warnings") or [])[:4],
        },
        trace_dir=trace_dir,
    )
    if checked > 0:
        _rescan_form_fields(target)
    return {"checked": checked, "plan": plan}


def _click_by_exact_text(text: str,
                          form_root_selector: str | None = None) -> dict[str, Any] | None:
    pat = "^" + re.escape(text).replace(r"\ ", r"\s*") + "$"
    return _click_button([pat], form_root_selector=form_root_selector)


def _click_by_selector(selector: str) -> dict[str, Any] | None:
    args = {"selector": selector}
    js = f"""
    (() => {{
      const fn = {_CLICK_BY_SELECTOR_JS};
      return fn({json.dumps(args, ensure_ascii=False)});
    }})()
    """
    res = _evaluate(js)
    if isinstance(res, dict) and res.get("ok"):
        return {
            "clicked": True,
            "text": (res.get("text") or "")[:80],
            "selector": selector,
            "scope": "selector",
        }
    return None


_FIRST_STEP_BTN_RE = re.compile(r"(入力内容を確認|内容(を|の)?確認|確認画面|同意して次へ|^次へ$)", re.I)
_FINAL_STEP_BTN_RE = re.compile(r"(送信|submit|完了|確定|問い合わせを送信|お問い合わせを送信)", re.I)
_NOISE_BTN_RE = re.compile(r"^(こちら|個人情報の取扱い|プライバシー|詳細|トップ|戻る|一覧)$", re.I)


def _looks_like_first_step_button(text: str) -> bool:
    return bool(_FIRST_STEP_BTN_RE.search((text or "").strip()))


def _looks_like_final_step_button(text: str) -> bool:
    return bool(_FINAL_STEP_BTN_RE.search((text or "").strip()))


def _infer_submit_flow_from_buttons(form_root_selector: str | None = None) -> str | None:
    buttons = _enumerate_buttons(form_root_selector=form_root_selector)
    if not buttons:
        return None
    first_like = 0
    final_like = 0
    for b in buttons:
        txt = str(b.get("text") or "")
        if _looks_like_first_step_button(txt):
            first_like += 1
        if _looks_like_final_step_button(txt):
            final_like += 1
    if first_like > 0 and final_like == 0:
        return "confirm"
    if final_like > 0 and first_like == 0:
        return "single"
    return None


def _phase_filter_submit_candidates(
    buttons: list[dict[str, Any]],
    *,
    phase: str,
) -> list[dict[str, Any]]:
    return core_submit_progress.rank_submit_candidates(buttons, phase=phase)


_FINAL_SUBMIT_PICKER_PROMPT = """あなたは日本語B2B問い合わせフォームの確認画面で「最終送信ボタン」を1つ選ぶ役割です。

## 状況
ユーザーがフォームを入力済み・「確認画面へ」ボタンを押した直後で、いま確認画面にいます。
このページ上にある全ボタンのリストから、**最終送信に該当する1個** を選んでください。

## 判断基準
- 「送信する」「上記の内容で送信」「お問い合わせを送信」など、内容を確定して送信する意図のラベル
- 「修正する」「戻る」「キャンセル」「リセット」は最終送信ではない
- ヘッダ/ナビ系（「お問い合わせ」「ログイン」「会社概要」など）は最終送信ではない
- もし最終送信ボタンがどれにも該当しない（例：確認画面に到達していない／ページ遷移失敗）なら "text" を空文字、"reason" にその旨

## ボタン一覧（JSON）
{buttons_json}

## クリック対象フェーズ
{phase_hint}

## 出力（JSONのみ）
{{"text": "<選んだボタンの text（不明なら空）>", "selector": "<選んだボタンの selector（不明なら空）>", "reason": "<簡潔な根拠>"}}
"""


def _llm_pick_final_submit(buttons: list[dict[str, Any]],
                            config: dict[str, Any],
                            *,
                            phase: str = "final") -> dict[str, Any] | None:
    if not buttons:
        return None
    if phase == "first":
        phase_hint = (
            "first: 入力画面で「確認画面へ進む/入力内容を確認する」等を選ぶ。"
            " 「送信する」は通常この段階では押さない。"
        )
    else:
        phase_hint = (
            "final: 確認画面で「送信する/上記の内容で送信/問い合わせを送信」等の最終確定のみ。"
            " 「入力内容を確認する/修正する/戻る」は選ばない。"
        )
    prompt = _FINAL_SUBMIT_PICKER_PROMPT.format(
        buttons_json=json.dumps(buttons, ensure_ascii=False, indent=2),
        phase_hint=phase_hint,
    )
    model_cfg = config.get("model", {}) or {}
    model = model_cfg.get("form_analyzer_name") or model_cfg.get("name", DEFAULT_MODEL)
    response = oc_infer(prompt, model=model)
    result = extract_first_json(response or "")
    if isinstance(result, dict) and (result.get("text") or result.get("selector")):
        return result
    return None


def _form_analyzer_base_model(config: dict[str, Any] | None) -> str:
    model_cfg = (config or {}).get("model", {}) or {}
    return str(model_cfg.get("form_analyzer_name") or model_cfg.get("name") or DEFAULT_MODEL)


def _form_analyzer_escalation_model(config: dict[str, Any] | None) -> str:
    model_cfg = (config or {}).get("model", {}) or {}
    return str(
        model_cfg.get("form_analyzer_escalation_name")
        or model_cfg.get("opus_name")
        or model_cfg.get("opus_model")
        or ""
    )


def _has_form_analyzer_escalation(config: dict[str, Any] | None) -> bool:
    base = _form_analyzer_base_model(config)
    escalated = _form_analyzer_escalation_model(config)
    return bool(escalated and escalated != base)


def _verify_model(config: dict[str, Any] | None) -> str:
    """v30 §WS-F — model used for verify-stage LLM tiebreak.

    Resolution order:

      * ``config.model.verify_name`` — brief- or persona-specific override
      * ``model.form_analyzer_name`` — legacy fallback (the verify tiebreak
        historically reused the form analyzer's base model, which conflated
        two unrelated concerns)
      * ``model.name`` — global default
      * :data:`core_infer.DEFAULT_MODEL` — Sonnet, the canonical fast judge

    Separating verify from the form analyzer matters because:

      * Verify is called once per target on the final page evidence — a fast
        Sonnet judge keeps cost predictable.
      * The form analyzer may legitimately be escalated to Opus on hard
        forms; that escalation should not bleed into verify and inflate
        every send's LLM bill.
    """
    model_cfg = (config or {}).get("model", {}) or {}
    return str(
        model_cfg.get("verify_name")
        or model_cfg.get("form_analyzer_name")
        or model_cfg.get("name")
        or DEFAULT_MODEL
    )


def _config_with_form_analyzer_model(
    config: dict[str, Any] | None,
    model_name: str,
) -> dict[str, Any]:
    cfg = dict(config or {})
    model_cfg = dict(cfg.get("model") or {})
    model_cfg["form_analyzer_name"] = model_name
    cfg["model"] = model_cfg
    return cfg


def _inquiry_guardrail_status(target: dict[str, Any]) -> dict[str, Any]:
    fields = _extract_inquiry_type_fields(target.get("form_fields") or {})
    if not fields:
        return {"has_inquiry": False, "low_confidence": False, "no_b2b": False, "reason": "none"}
    low = 0
    no_b2b = 0
    for field in fields:
        picked = core_submit_progress.choose_b2b_option(field.get("options") or [])
        if not picked:
            no_b2b += 1
            continue
        if str(picked.get("confidence") or "") == "low":
            low += 1
    return {
        "has_inquiry": True,
        "low_confidence": low > 0,
        "no_b2b": no_b2b >= len(fields),
        "reason": "low_confidence" if low > 0 else ("no_b2b" if no_b2b >= len(fields) else "ok"),
    }


def _click_submit_by_instruction(
    d: dict[str, Any],
    *,
    phase: str,
    form_root_selector: str | None = None,
) -> dict[str, Any] | None:
    """v19: click send when the PAGE TEXT explicitly says to.

    Handles confirm pages like 「上記の内容でよろしければ、送信ボタンをクリックして
    ください。」 where the instruction is the reliable signal but the button's own
    label may be generic / an image. We only act on a genuine submit-type or
    in-form control (never a nav link), and never click a 確認/次へ button in the
    final phase. Returns a click_res-like dict (with by_instruction=True) or None.
    """
    page_text = _evaluate(_PAGE_TEXT_HEAD_JS)
    page_text = page_text if isinstance(page_text, str) else ""
    if not core_submit_progress.detect_confirm_instruction(page_text):
        return None
    buttons = _enumerate_buttons(
        form_root_selector=form_root_selector,
        filled_names=_filled_name_hints(d),
    )
    ranked = _phase_filter_submit_candidates(buttons, phase=phase)
    for cand in ranked:
        if not (cand.get("is_submit_type") or cand.get("in_form")):
            continue
        txt = str(cand.get("text") or "").strip()
        sel = str(cand.get("selector") or "").strip()
        if phase == "final" and txt and _looks_like_first_step_button(txt) \
                and not _looks_like_final_step_button(txt):
            continue
        res = _click_by_selector(sel) if sel else None
        if (not res or not res.get("clicked")) and txt:
            res = _click_by_exact_text(txt, form_root_selector=form_root_selector)
        if res and res.get("clicked"):
            res["by_instruction"] = True
            res["picked"] = {"text": txt, "selector": sel,
                             "reason": "confirm_instruction_text"}
            return res
    return None


def _llm_click_submit_candidate(
    buttons: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    form_root_selector: str | None = None,
    phase: str = "final",
) -> dict[str, Any] | None:
    scoped_buttons = _phase_filter_submit_candidates(buttons, phase=phase)
    pick = _llm_pick_final_submit(scoped_buttons, config or {}, phase=phase)
    if not pick:
        return None
    selector = str(pick.get("selector") or "").strip()
    text = str(pick.get("text") or "").strip()
    # Guardrail: never use "確認/次へ" style buttons in final-submit phase.
    if phase == "final" and text and _looks_like_first_step_button(text) and not _looks_like_final_step_button(text):
        return None
    if selector:
        res = _click_by_selector(selector)
        if res and res.get("clicked"):
            if phase == "final" and _looks_like_first_step_button(str(res.get("text") or "")) and not _looks_like_final_step_button(str(res.get("text") or "")):
                return None
            res["picked"] = pick
            return res
    if text:
        res = _click_by_exact_text(text, form_root_selector=form_root_selector)
        if res and res.get("clicked"):
            res["picked"] = pick
            return res
    return None


# ============================================================================
# LLM-driven form analyzer (Sonnet plans the fill before JS executes it)
# ============================================================================
#
# Heuristic regex matching can't handle every JP form variation (custom
# labels, split address fields, multi-step radios, etc). For each target,
# we ask Sonnet to look at the actual form_fields JSON + sender info +
# overrides, and return a concrete fill plan keyed by element name/id.
# Then a small JS executor applies the plan deterministically.

_FORM_ANALYZER_PROMPT_TEMPLATE = """You are a Japanese B2B inquiry form-fill planner. Look at the form's
field structure and produce a precise JSON plan for how to fill it.

## Inputs

### Sender info
```yaml
{sender_yaml}```

### Field-level overrides (from targets.yaml — these specify which radio/select option to choose)
```yaml
{overrides_yaml}```

### Body length
The draft body will be ≤ {body_max_chars} characters. Use placeholder `__BODY__`.

### Form structure (as parsed from the DOM)
```json
{form_fields_json}```

### Phase
{phase_hint}

### Confirm-page button candidates (for final submit phase)
```json
{confirm_buttons_json}```

## Output schema (STRICT JSON)

```json
{{
  "fields": [
    {{"name": "<element name or id>", "action": "set_text" | "select_option" | "select_radio" | "skip",
      "value": "<value to set, or __BODY__ for the message body>",
      "selector": "<optional CSS selector when name/id is unavailable>",
      "reason": "<short why>"}}
  ],
  "inquiry_type_no_b2b": false,
  "route_choice": {{
    "name": "<routing radio group name>",
    "value": "<法人/企業/取引側の既存option>"
  }},
  "enable_sequence": [
    {{"action": "select_radio" | "select_option" | "set_text" | "check" | "click" | "RESCAN",
      "name": "<field name/id or selector:...>",
      "value": "<value or __BODY__>",
      "label": "<optional label hint>",
      "reason": "<short why>"}}
  ],
  "submit_gate": {{
    "blocked": true,
    "reason": "<why button is disabled>",
    "missing": ["<required gate hints>"]
  }},
  "checkboxes_to_check": [
    "<element name or id>",
    {{"name": "<element name or id>", "label": "<exact visible label text>"}}
  ],
  "first_button_pattern": "<regex for the first submit button, e.g. '入力内容を確認' or '送信'>",
  "next_step": "single" | "confirm",
  "warnings": ["<diagnostic>"]
}}
```

## Rules

1. Map each visible field to a value from sender / overrides
2. For split 姓/名 fields: use sender.name_sei / sender.name_mei when present,
   else first 2 chars of sender.name / remainder. Note: 氏名・お名前 = FULL name
   (not the 名 half); 苗字・名字 = 姓.
3. For カナ/カタカナ fields: use sender.name_kana (split: sender.name_kana_sei /
   name_kana_mei when present). カタカナ表記のラベル(フリガナ/セイ/メイ)にはカタカナを入れる.
4. For ふりがな/ひらがな fields: use sender.name_furigana (split: sender.
   name_furigana_sei / name_furigana_mei). ひらがな表記のラベル(ふりがな/せい/めい)にはひらがなを入れる.
5. For メール確認用: re-use sender.email
6. For 電話番号 split into 3 fields: split sender.phone (no hyphens) at positions 3 and 7
7. For 郵便番号 split into 2: first 3 / last 4
8. For 都道府県 select: use action="select_option", value=sender.prefecture
9. For 市区町村 / 番地 split: use sender.city + sender.address_line + sender.building
10. For body textarea: action="set_text", value="__BODY__"
11. For category/お問い合わせ種別 radios/selects:
    - If overrides.category_radio/category_select exists, use it.
    - Otherwise choose ONE existing option text that best matches B2B sales inquiry
      intent (取引/提案/協業/法人向け). Do NOT invent option texts.
    - Never choose placeholders like "選択してください", "---", empty values.
    - Avoid individual/recruit/IR/reservation/support lanes when a B2B lane exists.
12. If category options exist but no valid B2B option is available, set "inquiry_type_no_b2b": true.
13. For 性別 radios: use overrides.gender_radio if set
14. For ご希望の連絡方法 radios: use overrides.contact_method_radio
15. For 連絡可能な時間帯 radios: use overrides.contact_time_radio
16. For 同意 / プライバシー / 利用規約 / 個人情報 checkboxes:
    add to checkboxes_to_check. If name/id is unclear, include exact label text
    via {{"name":"", "label":"..."}}.
17. For optional fields like FAX, ニュースレター: action="skip".
    For 当社をどこで知ったか/きっかけ selects or radios: choose "その他" when that
    option exists (else "検索"/"Web"系), only skip if neither exists.
    For ご予算: choose "未定" or "その他". For 連絡方法: choose "メール".
18. For 従業員数 select with override.employee_count_required: use sender.employee_count_band
19. Prefer `name` as the field identifier; fall back to `id`.
    If neither exists, use "name": "selector:<css>" AND set `selector` to the same CSS selector.
20. Never emit pseudo placeholders (e.g. "<sender.name>", "<field>", "<name>") in `name`.
21. If the form has multiple submit-like buttons, the FIRST one (e.g. "入力内容を確認する") goes in `first_button_pattern`
22. If the flow is single-step (just one Send button), set next_step="single", else "confirm"
23. Add warnings for tricky cases (e.g. "postal code lookup may overwrite city field — fill postal LAST")
24. For pre-form phase, emit route_choice and enable_sequence in the order needed to activate confirm/send button.
25. If selecting route/category can change options/required fields, include RESCAN between steps.
26. For final-confirm phase, prioritize is_submit_type && in_form candidates; if uncertain, leave text empty.

Output the JSON only, no prose."""


def _llm_analyze_form(
    target: dict[str, Any],
    config: dict[str, Any],
    body_max_chars: int = 400,
    *,
    phase: str = "prefill",
    force_refresh: bool = False,
    escalation_reason: str | None = None,
) -> dict[str, Any] | None:
    """
    Use Sonnet to plan how to fill a target's form. Returns plan dict or None.
    Plan is cached on the target as `_llm_plan` once analyzed.
    """
    selected_model = _form_analyzer_base_model(config)
    plan_meta = target.get("_llm_plan_meta") or {}
    if target.get("_llm_plan") and not force_refresh and phase == "prefill":
        cached_model = str(plan_meta.get("model") or "")
        if (not cached_model) or cached_model == selected_model:
            return target["_llm_plan"]

    form_fields = target.get("form_fields") or {}
    if not form_fields or not (form_fields.get("inputs")
                                or form_fields.get("textareas")
                                or form_fields.get("selects")
                                or form_fields.get("radios")):
        return None  # No fields to analyze (e.g. iframe form)

    sender = config.get("sender", {})
    overrides = target.get("field_map_overrides", {}) or {}

    if phase == "confirm":
        phase_hint = "final-confirm phase: identify final submit and remaining consent gates."
        confirm_buttons = _enumerate_buttons(
            form_root_selector=target.get("form_root_selector"),
            filled_names=_filled_name_hints(target),
        )
    else:
        phase_hint = "prefill phase: produce B2B-safe fill + enable sequence."
        confirm_buttons = []

    prompt = _FORM_ANALYZER_PROMPT_TEMPLATE.format(
        sender_yaml=yaml.safe_dump(sender, allow_unicode=True, sort_keys=False),
        overrides_yaml=yaml.safe_dump(overrides, allow_unicode=True, sort_keys=False) if overrides else "{}\n",
        body_max_chars=body_max_chars,
        form_fields_json=json.dumps(form_fields, ensure_ascii=False, indent=2),
        phase_hint=phase_hint,
        confirm_buttons_json=json.dumps(confirm_buttons[:30], ensure_ascii=False, indent=2),
    )

    response = oc_infer(prompt, model=selected_model)
    plan = extract_first_json(response or "")
    if not plan or "fields" not in plan:
        return None
    if phase == "prefill":
        target["_llm_plan"] = plan
        target["_llm_plan_meta"] = {
            "model": selected_model,
            "escalation_reason": escalation_reason or "",
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    return plan


# JS that applies a single field action from the plan
_APPLY_PLAN_FIELD_JS = r"""
(args) => {
  const { name, action, value } = args;
  const setVal = (el, v) => {
    const proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    if (typeof el.focus === 'function') el.focus();
    // Custom validators (notably Contact Form 7) can leave a customError on
    // an element after a server bounce. Re-entry clears that stale validity;
    // the site still validates the new value on the next submit.
    if (typeof el.setCustomValidity === 'function') el.setCustomValidity('');
    setter.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
  };

  const findEl = (selectorAttr, cssSelector) => {
    if (cssSelector) {
      try {
        const byCss = document.querySelector(cssSelector);
        if (byCss) return byCss;
      } catch (e) {}
    }
    let el = null;
    try {
      el = document.querySelector(`[name="${CSS.escape(selectorAttr)}"]`);
    } catch (e) {}
    if (el) return el;
    try {
      el = document.querySelector(`#${CSS.escape(selectorAttr)}`);
    } catch (e) {}
    return el;
  };

  if (action === "set_text") {
    const el = findEl(name, args.selector || '');
    if (!el) return { ok: false, reason: "element not found", name };
    setVal(el, value);
    return { ok: true, action, name, value: String(value).slice(0, 50) };
  }

  if (action === "select_option") {
    const sel = findEl(name, args.selector || '');
    if (!sel || sel.tagName !== 'SELECT') return { ok: false, reason: "select not found", name };
    for (const opt of sel.options) {
      if (opt.text === value || opt.value === value
          || opt.text.includes(value) || opt.value.includes(value)) {
        const text = String(opt.text || '').replace(/\s+/g, ' ').trim();
        const val = String(opt.value || '').trim();
        const placeholder = !val || /^(?:[-ー−‐~\s]*)?(?:以下から選択|選択してください|選択して下さい|please select|select|お選びください|指定なし)/i.test(text);
        if (placeholder) {
          return { ok: false, reason: "placeholder option is not a selection", name, value: text };
        }
        sel.value = opt.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        return { ok: true, action, name, value: opt.text };
      }
    }
    return { ok: false, reason: "option not found", name, value };
  }

  if (action === "select_radio") {
    // For radios, "name" is the radio group name and "value" is which option
    if (args.selector) {
      let direct = null;
      try { direct = document.querySelector(args.selector); } catch (e) {}
      if (direct && direct.matches && direct.matches('input[type="radio"]')) {
        direct.checked = true;
        direct.dispatchEvent(new Event('change', { bubbles: true }));
        direct.dispatchEvent(new Event('click', { bubbles: true }));
        return { ok: true, action, name, value: direct.value || value };
      }
    }
    const radios = document.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`);
    if (!radios.length) return { ok: false, reason: "radio group not found", name };
    for (const r of radios) {
      const labelEl = r.id ? document.querySelector(`label[for="${r.id}"]`) : r.closest('label');
      const lbl = labelEl ? labelEl.textContent.trim() : '';
      if ((r.value || '').includes(value) || lbl.includes(value)) {
        r.checked = true;
        r.dispatchEvent(new Event('change', { bubbles: true }));
        r.dispatchEvent(new Event('click', { bubbles: true }));
        return { ok: true, action, name, value: lbl || r.value };
      }
    }
    return { ok: false, reason: "no radio option matched", name, value };
  }

  if (action === "skip") return { ok: true, action: "skip", name };

  return { ok: false, reason: "unknown action", action };
}
"""


# JS that checks a checkbox by name or id
_CHECK_BY_NAME_JS = r"""
(args) => {
  const { name } = args;
  let cb = null;
  if (String(name || '').startsWith('selector:')) {
    try {
      const el = document.querySelector(String(name).slice('selector:'.length));
      if (el && el.matches && el.matches('input[type="checkbox"]')) cb = el;
    } catch (e) {}
  }
  if (!cb) {
    try { cb = document.querySelector(`input[type="checkbox"][name="${CSS.escape(name)}"]`); }
    catch (e) {}
  }
  if (!cb) {
    try { cb = document.querySelector(`input[type="checkbox"]#${CSS.escape(name)}`); }
    catch (e) {}
  }
  if (!cb) return { ok: false, reason: "checkbox not found", name };
  if (cb.disabled || cb.getAttribute('aria-disabled') === 'true') {
    return { ok: false, reason: "checkbox disabled", name };
  }
  if (!cb.checked) {
    // A click toggles checked by itself.  The old sequence assigned true and
    // then dispatched click, which deterministically toggled it back to false
    // (carchs contact_us[confirm]).  Prefer a real click so framework handlers
    // observe the same transition as a user; retain a setter fallback for
    // custom widgets that cancel the click.
    cb.click();
    if (!cb.checked) {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'checked'
      ).set;
      setter.call(cb, true);
      cb.dispatchEvent(new Event('input', { bubbles: true }));
      cb.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }
  return { ok: !!cb.checked, reason: cb.checked ? '' : 'checkbox remained unchecked', name };
}
"""


# JS that checks a checkbox by its visible label text
_CHECK_BY_LABEL_JS = r"""
(args) => {
  const { label } = args;
  const cbs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
  for (const cb of cbs) {
    let txt = '';
    if (cb.id) {
      const lab = document.querySelector(`label[for="${cb.id}"]`);
      if (lab) txt = (lab.textContent || '').trim();
    }
    if (!txt && cb.parentElement) {
      txt = (cb.parentElement.textContent || '').trim();
    }
    if (!txt) {
      const aria = cb.getAttribute('aria-label');
      if (aria) txt = aria.trim();
    }
    if (txt && txt.includes(label)) {
      if (!cb.checked) {
        cb.click();
        if (!cb.checked) {
          const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'checked'
          ).set;
          setter.call(cb, true);
          cb.dispatchEvent(new Event('input', { bubbles: true }));
          cb.dispatchEvent(new Event('change', { bubbles: true }));
        }
      }
      return {
        ok: !!cb.checked,
        reason: cb.checked ? '' : 'checkbox remained unchecked',
        label,
        text: txt.slice(0, 60)
      };
    }
  }
  return { ok: false, reason: "no checkbox with label", label };
}
"""


def _apply_field_action(
    name: str, action: str, value: str, selector: str | None = None
) -> dict[str, Any] | None:
    n = str(name or "")
    s = str(selector or "")
    if n.startswith("selector:") and not s:
        s = n[len("selector:"):]
    args = {"name": n, "action": action, "value": value, "selector": s}
    js = f"""
    (() => {{
      const fn = {_APPLY_PLAN_FIELD_JS};
      return fn({json.dumps(args, ensure_ascii=False)});
    }})()
    """
    res = _evaluate(js)
    return res if isinstance(res, dict) else None


def _check_by_name(name: str) -> dict[str, Any] | None:
    args = {"name": name}
    js = f"""
    (() => {{
      const fn = {_CHECK_BY_NAME_JS};
      return fn({json.dumps(args, ensure_ascii=False)});
    }})()
    """
    res = _evaluate(js)
    return res if isinstance(res, dict) else None


def _check_by_label(label: str) -> dict[str, Any] | None:
    args = {"label": label}
    js = f"""
    (() => {{
      const fn = {_CHECK_BY_LABEL_JS};
      return fn({json.dumps(args, ensure_ascii=False)});
    }})()
    """
    res = _evaluate(js)
    return res if isinstance(res, dict) else None


def _extract_inquiry_type_fields(form_fields: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sel in form_fields.get("selects") or []:
        if not isinstance(sel, dict):
            continue
        field = {
            "kind": "select_option",
            "name": str(sel.get("name") or "").strip(),
            "label": str(sel.get("label") or ""),
            "required": bool(sel.get("required")),
            "options": [{"label": str(x), "value": str(x)} for x in (sel.get("options") or []) if str(x).strip()],
        }
        if field["name"] and core_submit_progress.is_inquiry_type_field(field):
            out.append(field)
    for name, options in (form_fields.get("radios") or {}).items():
        opts = options if isinstance(options, list) else []
        field = {
            "kind": "select_radio",
            "name": str(name or "").strip(),
            "label": str(name or ""),
            "required": False,
            "options": [
                {
                    "label": str((o or {}).get("label") or ""),
                    "value": str((o or {}).get("value") or ""),
                    "selected": bool((o or {}).get("checked")),
                }
                for o in opts if isinstance(o, dict)
            ],
        }
        if field["name"] and core_submit_progress.is_inquiry_type_field(field):
            out.append(field)
    return out


def _inquiry_type_no_b2b_flags(
    inquiry_fields: list[dict[str, Any]],
    probe_plan: dict[str, Any] | None = None,
) -> tuple[bool, bool, bool]:
    llm_no_b2b = bool((probe_plan or {}).get("inquiry_type_no_b2b"))
    fallback_no_b2b = bool(inquiry_fields) and all(
        core_submit_progress.choose_b2b_option(f.get("options") or []) is None
        for f in inquiry_fields
    )
    return llm_no_b2b, fallback_no_b2b, bool(llm_no_b2b or fallback_no_b2b)


def _summarize_inquiry_type_selection(
    inquiry_fields: list[dict[str, Any]],
    probe_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for field in inquiry_fields:
        name = str(field.get("name") or "").strip()
        action = str(field.get("kind") or "")
        if not name or not action:
            continue
        options = field.get("options") or []
        llm_entry = _plan_entry_for_name(probe_plan or {}, name, action) if probe_plan else None
        llm_value = str((llm_entry or {}).get("value") or "").strip()
        if llm_value and core_submit_progress.validate_choice(options, llm_value):
            fallback = core_submit_progress.choose_b2b_option(options)
            conf = "high"
            if fallback and str(fallback.get("value") or "") and str(fallback.get("value")) != llm_value:
                conf = "low"
            items.append({"name": name, "value": llm_value[:120], "src": "llm", "confidence": conf})
            continue
        fallback = core_submit_progress.choose_b2b_option(options)
        if fallback:
            items.append(
                {
                    "name": name,
                    "value": str(fallback.get("value") or "")[:120],
                    "src": "fallback",
                    "confidence": str(fallback.get("confidence") or "low"),
                }
            )
    conf_counts = {"high": 0, "low": 0}
    src_counts = {"llm": 0, "fallback": 0}
    for it in items:
        if it["confidence"] in conf_counts:
            conf_counts[it["confidence"]] += 1
        if it["src"] in src_counts:
            src_counts[it["src"]] += 1
    return {
        "count": len(items),
        "items": items[:8],
        "confidence_counts": conf_counts,
        "src_counts": src_counts,
    }


def _plan_entry_for_name(plan: dict[str, Any], name: str, action: str) -> dict[str, Any] | None:
    for entry in plan.get("fields") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("name") or "") != name:
            continue
        if str(entry.get("action") or "") != action:
            continue
        return entry
    return None


def _ensure_inquiry_type_action(
    target: dict[str, Any],
    plan: dict[str, Any] | None,
    *,
    stage: str,
    trace_dir: Path | None = None,
) -> dict[str, Any]:
    """Apply inquiry-type select/radio before full fill.

    LLM plan is primary. If invalid/missing, fallback to pure-function scorer.
    """
    overrides = target.get("field_map_overrides", {}) or {}
    if overrides.get("category_select") or overrides.get("category_radio"):
        return {"selected": 0, "src": "override"}
    form_fields = target.get("form_fields") or {}
    inquiry_fields = _extract_inquiry_type_fields(form_fields)
    if not inquiry_fields:
        return {"selected": 0, "src": "none", "no_b2b": False}
    chosen_count = 0
    no_b2b_hits = 0
    for field in inquiry_fields:
        fname = field["name"]
        action = field["kind"]
        options = field.get("options") or []
        llm_entry = _plan_entry_for_name(plan or {}, fname, action) if plan else None
        llm_value = str((llm_entry or {}).get("value") or "").strip()
        choice = ""
        src = ""
        confidence = "high"
        if llm_value and core_submit_progress.validate_choice(options, llm_value):
            fallback = core_submit_progress.choose_b2b_option(options)
            if fallback and str(fallback.get("value") or "") and str(fallback.get("value")) != llm_value:
                choice = str(fallback["value"])
                src = "fallback"
                confidence = "low"
            else:
                choice = llm_value
                src = "llm"
        else:
            fallback = core_submit_progress.choose_b2b_option(options)
            if fallback:
                choice = str(fallback.get("value") or "")
                src = "fallback"
                confidence = str(fallback.get("confidence") or "low")
        if not choice:
            no_b2b_hits += 1
            continue
        res = _apply_field_action(fname, action, choice)
        if not (res and res.get("ok")):
            continue
        chosen_count += 1
        tid = str(target.get("id") or target.get("name") or "")
        _emit_event(
            "send.inquiry_type",
            stage=stage,
            target_id=tid,
            payload={
                "name": fname,
                "value": choice[:120],
                "src": src,
                "confidence": confidence,
            },
            trace_dir=trace_dir,
        )
        if llm_entry is None and plan is not None:
            plan.setdefault("fields", []).append(
                {
                    "name": fname,
                    "action": action,
                    "value": choice,
                    "reason": f"inquiry_type_autoselect:{src}",
                }
            )
        elif llm_entry is not None:
            llm_entry["value"] = choice
            llm_entry["reason"] = f"inquiry_type_autoselect:{src}"
    no_b2b = bool(inquiry_fields) and (no_b2b_hits >= len(inquiry_fields))
    return {"selected": chosen_count, "src": "llm_or_fallback", "no_b2b": no_b2b}


def _required_field_rows(form_fields: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for kind in ("inputs", "textareas", "selects"):
        for row in form_fields.get(kind) or []:
            if not isinstance(row, dict):
                continue
            if not bool(row.get("required")):
                continue
            out.append(
                {
                    "type": kind[:-1],
                    "name": str(row.get("name") or ""),
                    "label": str(row.get("label") or ""),
                    "required": True,
                }
            )
    return out


def _rescan_after_inquiry_type(
    target: dict[str, Any],
    sender: dict[str, Any],
    diagnostics: dict[str, Any],
    *,
    trace_dir: Path | None = None,
) -> None:
    baseline = target.get("form_fields") or {}
    baseline_keys = {
        f"{row.get('type')}:{row.get('name') or row.get('label')}"
        for row in _required_field_rows(baseline)
    }
    fresh = _evaluate(_FORM_FIELDS_JS) or {}
    if not isinstance(fresh, dict):
        return
    new_required: list[dict[str, Any]] = []
    for row in _required_field_rows(fresh):
        key = f"{row.get('type')}:{row.get('name') or row.get('label')}"
        if key not in baseline_keys:
            new_required.append(row)
    target["form_fields"] = fresh
    if fresh.get("form_root_selector"):
        target["form_root_selector"] = fresh["form_root_selector"]
    if not new_required:
        return
    for row in new_required:
        label = str(row.get("label") or "")
        blob = f"{label} {row.get('name')}".lower()
        fill = None
        if any(k in blob for k in ("会社", "法人", "company", "団体")):
            fill = ("company", sender.get("company", ""), SENDER_FIELD_PATTERNS["company"])
        elif any(k in blob for k in ("部署", "役職", "department", "position")):
            fill = ("role", sender.get("role", ""), SENDER_FIELD_PATTERNS["role"])
        elif any(k in blob for k in ("名前", "氏名", "担当", "name")):
            fill = ("name", sender.get("name", ""), SENDER_FIELD_PATTERNS["name"])
        elif any(k in blob for k in ("メール", "mail")):
            fill = ("email", sender.get("email", ""), SENDER_FIELD_PATTERNS["email"])
        elif any(k in blob for k in ("電話", "tel", "phone")):
            fill = ("phone", sender.get("phone", ""), SENDER_FIELD_PATTERNS["phone"])
        if fill and str(fill[1]).strip():
            res = _fill_field(fill[0], str(fill[1]), fill[2])
            if res and res.get("filled"):
                diagnostics.setdefault("filled", []).append(
                    f"dynamic_required:{row.get('name') or row.get('label')}={str(fill[1])[:30]}"
                )
                continue
        diagnostics.setdefault("dynamic_escalated", []).append(row.get("name") or row.get("label") or "?")
        _escalate_dynamic_required(target, [row])
    diagnostics.setdefault("warnings", []).append(f"inquiry_rescan_new_required={len(new_required)}")
    _emit_event(
        "send.inquiry_type.rescan",
        stage="send",
        target_id=str(target.get("id") or target.get("name") or ""),
        payload={"new_required_count": len(new_required)},
        trace_dir=trace_dir,
    )


def _postal_sort_key(entry: dict[str, Any]) -> int:
    name = (entry.get("name") or "").lower()
    label = (entry.get("label") or "").lower()
    blob = name + label
    if any(k in blob for k in ("postal", "zip", "〒", "郵便")):
        return 1
    if any(k in blob for k in ("address", "addr", "都道府県", "市区", "住所", "番地")):
        return -1
    return 0


def _escalate_dynamic_required(
    target: dict[str, Any], fields: list[dict[str, Any]]
) -> None:
    tid = target.get("id", "?")
    name = target.get("name", "?")
    labels = ", ".join(
        f"{f.get('type', '?')}: {f.get('label') or f.get('name')}" for f in fields[:5]
    )
    append_needs_attention(
        DATA_DIR,
        {
            "target_id": tid,
            "name": name,
            "channel": "jp_form",
            "reason": f"動的に出現した必須項目: {labels}",
            "unresolved_fields": fields,
            "action_needed": "field_values",
        },
    )
    _emit_event(
        "send.escalated",
        stage="send",
        target_id=str(tid),
        payload={
            "field_count_unresolved": len(fields),
            "slack_posted": True,
            "reason": "dynamic_required",
        },
    )


def _apply_plan_entry(
    entry: dict[str, Any], body: str, diag: dict[str, Any]
) -> bool:
    """Apply one plan field entry. Returns True on success."""
    BODY_TOKEN = "__BODY__"
    name = str(entry.get("name") or "").strip()
    action = entry.get("action")
    value = entry.get("value", "")
    selector = str(entry.get("selector") or "").strip()
    if not action:
        return False
    if not name and selector:
        name = f"selector:{selector}"
    if not name:
        return False
    if action == "skip":
        diag["skipped"].append(name)
        return True
    if isinstance(value, str) and BODY_TOKEN in value:
        value = value.replace(BODY_TOKEN, body)
    res = _apply_field_action(name, action, value, selector=selector)
    if res and res.get("ok"):
        label = res.get("value") or res.get("action")
        diag["filled"].append(f"{name}={str(label)[:30]} ({action})")
        return True
    reason = (res or {}).get("reason", "unknown")
    diag["errors"].append(f"{name} ({action}): {reason}")
    return False


def fill_form_with_plan(
    plan: dict[str, Any],
    body: str,
    *,
    target: dict[str, Any] | None = None,
    evaluate_fn: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Apply an LLM-generated fill plan via JS. Returns diagnostics."""
    from _outreach_core.verify import scan_empty_required, scan_new_required_after_fill

    eval_fn = evaluate_fn or _evaluate
    diag: dict[str, Any] = {
        "filled": [],
        "errors": [],
        "skipped": [],
        "warnings": list(plan.get("warnings") or []),
        "dynamic_filled": [],
        "dynamic_escalated": [],
    }
    BODY_TOKEN = "__BODY__"
    fields = sorted(plan.get("fields") or [], key=_postal_sort_key)
    plan_by_name = {
        str(e["name"]): e for e in fields if e.get("name")
    }

    baseline_empty: set[str] = set()
    for item in scan_empty_required(eval_fn, target):
        key = str(item.get("name") or item.get("label") or "").strip()
        if key:
            baseline_empty.add(key)

    filled_names: set[str] = set()
    escalated_names: set[str] = set()

    for entry in fields:
        name = entry.get("name")
        if not name:
            continue
        if entry.get("action") == "skip":
            diag["skipped"].append(name)
            continue
        if isinstance(entry.get("value"), str) and BODY_TOKEN in entry["value"]:
            entry = {**entry, "value": entry["value"].replace(BODY_TOKEN, body)}
        if _apply_plan_entry(entry, body, diag):
            filled_names.add(str(name))
        time.sleep(0.1)

        time.sleep(0.3)
        for item in scan_new_required_after_fill(
            eval_fn,
            baseline_empty_names=baseline_empty,
            filled_names=filled_names,
            target=target,
        ):
            key = str(item.get("name") or item.get("label") or "").strip()
            if not key or key in escalated_names:
                continue
            baseline_empty.add(key)
            if key in plan_by_name:
                if _apply_plan_entry(plan_by_name[key], body, diag):
                    filled_names.add(key)
                    diag["dynamic_filled"].append(key)
            elif target:
                escalated_names.add(key)
                diag["dynamic_escalated"].append(key)
                _escalate_dynamic_required(target, [item])
            else:
                diag["warnings"].append(f"dynamic required {key} (no target to escalate)")

    for cb in plan.get("checkboxes_to_check", []):
        cb_name = ""
        cb_label = ""
        if isinstance(cb, dict):
            cb_name = str(cb.get("name") or "").strip()
            cb_label = str(cb.get("label") or "").strip()
        else:
            cb_name = str(cb or "").strip()
            cb_label = cb_name
        res = _check_by_name(cb_name) if cb_name else None
        if not (res and res.get("ok")) and cb_label:
            res = _check_by_label(cb_label)
        if res and res.get("ok"):
            marker = cb_name or cb_label or "?"
            diag["filled"].append(f"checkbox:{marker}")
        else:
            marker = cb_name or cb_label or "?"
            diag["errors"].append(f"checkbox {marker}: {(res or {}).get('reason','?')}")

    return diag


def _validation_errors_suggest_plan_refresh(
    validation_errors: list[dict[str, Any]] | None,
) -> bool:
    """v30 next: should we burn an extra LLM call to refresh the form plan?

    Yes ONLY when the page reported a native DOM-level reason
    (``valueMissing`` / ``patternMismatch`` / ``typeMismatch`` etc.) — those
    pin point a REAL required field the original plan didn't fill, which is
    exactly the case a fresh re-analysis can rescue. Generic 「必須項目」
    text-extracted errors are NOT a refresh trigger because they may also
    surface for fields the plan already mapped (placeholder mismatches,
    server-side re-validation noise).
    """
    if not validation_errors:
        return False
    native_kinds = {
        "valueMissing", "patternMismatch", "typeMismatch",
        "tooLong", "tooShort", "rangeUnderflow", "rangeOverflow",
        "stepMismatch", "badInput",
    }
    for err in validation_errors:
        kind = str((err or {}).get("kind") or "")
        # Native kinds may be concatenated by ``_native_validation_errors``
        # via "+" — split and check membership.
        if any(k in native_kinds for k in kind.split("+")):
            return True
    return False


def _refresh_llm_plan_and_refill(
    target: dict[str, Any],
    config: dict[str, Any],
    body: str,
    *,
    trigger_reason: str,
    trace_dir: Path | None = None,
) -> dict[str, Any]:
    """v30 next: re-snapshot the form and re-run the LLM analyzer against the
    CURRENT DOM, then re-apply the fresh plan.

    Production observation 2026-06-29: targets that hit ``submit_click_ineffective``
    in the resolver pass often had a stale ``_llm_plan`` (selectors no longer
    match the live DOM, or the original plan missed a required field that
    only became visible after a state transition). The validation_error
    handler had a "live_rescue" pass for radios/selects but no way to call
    the analyzer again. This helper fills that gap.

    Triggered AT MOST ONCE PER TARGET (``_plan_refreshed`` flag) so a
    pathological form does not burn one Opus call per validation round.

    Returns ``{"refreshed": bool, "fields": int, "filled": int, "errors": int}``.
    The caller must treat ``refreshed=False`` as "skipped" and continue with
    its existing recovery path.
    """
    if target.get("_plan_refreshed"):
        return {"refreshed": False, "reason": "already_refreshed_once"}
    tid = str(target.get("id") or target.get("name") or "?")
    print(f"  [send] LLM plan refresh: {trigger_reason} → re-analyzing form ...")
    target["_plan_refreshed"] = True

    fresh_fields = _evaluate(_FORM_FIELDS_JS) or {}
    if isinstance(fresh_fields, dict) and fresh_fields:
        target["form_fields"] = fresh_fields
        if fresh_fields.get("form_root_selector"):
            target["form_root_selector"] = fresh_fields["form_root_selector"]

    target.pop("_llm_plan", None)
    target.pop("_llm_plan_meta", None)

    # Prefer the escalation model if configured — a fresh re-analysis is the
    # right moment to spend Opus, since we know the cheaper plan was wrong.
    refresh_cfg = config
    if _has_form_analyzer_escalation(config):
        escalated_model = _form_analyzer_escalation_model(config)
        if escalated_model:
            refresh_cfg = _config_with_form_analyzer_model(config, escalated_model)

    from _outreach_core.draft import resolve_max_chars
    model_cfg = config.get("model", {}) or {}
    char_limit = resolve_max_chars(
        target, config,
        default_max=int(model_cfg.get("max_chars", 400)),
        extended_max=int(model_cfg.get("max_chars_extended", 400)),
    )
    plan = _llm_analyze_form(
        target, refresh_cfg,
        body_max_chars=char_limit,
        force_refresh=True,
        escalation_reason=f"plan_refresh:{trigger_reason}",
    )
    if not plan or not plan.get("fields"):
        _emit_event(
            "send.plan.refresh_failed",
            stage="send", target_id=tid,
            payload={"trigger": trigger_reason},
            trace_dir=trace_dir,
        )
        return {"refreshed": True, "fields": 0, "filled": 0, "errors": 0}

    target["_llm_plan"] = plan
    diag = fill_form_with_plan(plan, body, target=target, evaluate_fn=_evaluate)
    filled = len(diag.get("filled") or [])
    errors = len(diag.get("errors") or [])
    _emit_event(
        "send.plan.refreshed",
        stage="send", target_id=tid,
        payload={
            "trigger": trigger_reason,
            "fields": len(plan.get("fields") or []),
            "filled": filled,
            "errors": errors,
            "model": str((target.get("_llm_plan_meta") or {}).get("model") or ""),
        },
        trace_dir=trace_dir,
    )
    print(f"  [send] LLM plan refresh: {filled} filled / {errors} errors")
    return {"refreshed": True, "fields": len(plan.get("fields") or []),
            "filled": filled, "errors": errors}


def fill_form_for_target(
    target: dict[str, Any],
    config: dict[str, Any],
    body: str,
    *,
    trace_dir: Path | None = None,
    iterative_fill: bool = False,
) -> dict[str, Any]:
    """Fill all known sender fields + body. Returns diagnostic dict.

    Strategy:
      1. Try LLM-generated plan first (Sonnet looks at the form structure
         and outputs a precise field map)
      2. Fall back to heuristic regex patterns for any unmapped fields
    """
    sender = config.get("sender", {})
    _set_sender_ctx(config)  # gate auto-fill (radio/select) decisions need sender

    # === Phase 1: LLM-driven fill ===
    from _outreach_core.draft import resolve_max_chars

    model_cfg = config.get("model", {}) or {}
    char_limit = resolve_max_chars(
        target,
        config,
        default_max=int(model_cfg.get("max_chars", 400)),
        extended_max=int(model_cfg.get("max_chars_extended", 400)),
    )
    plan = _llm_analyze_form(target, config, body_max_chars=char_limit)
    guard = _inquiry_guardrail_status(target)
    if plan and _has_form_analyzer_escalation(config):
        if guard.get("low_confidence") or guard.get("no_b2b"):
            escalated_model = _form_analyzer_escalation_model(config)
            if escalated_model and str((target.get("_llm_plan_meta") or {}).get("model") or "") != escalated_model:
                _emit_event(
                    "send.plan.escalated",
                    stage="send",
                    target_id=str(target.get("id") or target.get("name") or ""),
                    payload={
                        "from_model": str((target.get("_llm_plan_meta") or {}).get("model") or _form_analyzer_base_model(config)),
                        "to_model": escalated_model,
                        "reason": f"inquiry_guard:{guard.get('reason')}",
                    },
                    trace_dir=trace_dir,
                )
                plan = _llm_analyze_form(
                    target,
                    _config_with_form_analyzer_model(config, escalated_model),
                    body_max_chars=char_limit,
                    force_refresh=True,
                    escalation_reason=f"inquiry_guard:{guard.get('reason')}",
                )
    plan_diag = None
    if plan:
        target["_llm_plan"] = plan
        tid = str(target.get("id") or "?")
        plan_model = str((target.get("_llm_plan_meta") or {}).get("model") or _form_analyzer_base_model(config))
        _emit_event(
            "send.plan.generated",
            stage="send",
            target_id=tid,
            payload={
                "field_count": len(plan.get("fields") or []),
                "checkboxes_count": len(plan.get("checkboxes_to_check") or []),
                "flow": plan.get("next_step"),
                "model": plan_model,
                "has_route_choice": bool(plan.get("route_choice")),
                "enable_steps": len(plan.get("enable_sequence") or []),
                "has_submit_gate": bool(plan.get("submit_gate")),
            },
            trace_dir=trace_dir,
        )
        from _outreach_core import events as ev

        if trace_dir:
            ev.dump_trace(trace_dir, "fill_plan.json", plan, sender=config.get("sender"))
        print(f"  [fill] LLM plan: {len(plan.get('fields', []))} field actions, "
              f"{len(plan.get('checkboxes_to_check', []))} checkboxes, "
              f"flow={plan.get('next_step', '?')}")
        for w in plan.get("warnings", []):
            print(f"    ⚠ {w}")
        inquiry = _ensure_inquiry_type_action(
            target,
            plan,
            stage="send",
            trace_dir=trace_dir,
        )
        plan_diag = fill_form_with_plan(plan, body, target=target, evaluate_fn=_evaluate)
        if inquiry.get("selected"):
            _rescan_after_inquiry_type(
                target,
                sender,
                plan_diag,
                trace_dir=trace_dir,
            )
        _emit_event(
            "send.fill.applied",
            stage="send",
            target_id=tid,
            payload={
                "filled": len(plan_diag.get("filled") or []),
                "errors": len(plan_diag.get("errors") or []),
                "skipped": len(plan_diag.get("skipped") or []),
                "inquiry_type_selected": int(inquiry.get("selected") or 0),
            },
            trace_dir=trace_dir,
        )
        for key in plan_diag.get("dynamic_escalated") or []:
            _emit_event(
                "send.fill.dynamic_required",
                stage="send",
                target_id=tid,
                payload={"field_label": key},
                trace_dir=trace_dir,
            )
        if trace_dir:
            ev.dump_trace(trace_dir, "fill_diagnostics.json", plan_diag)
        print(f"  [fill] plan applied: {len(plan_diag['filled'])} ok, "
              f"{len(plan_diag['errors'])} errors, {len(plan_diag['skipped'])} intentionally skipped")
        for e in plan_diag["errors"][:5]:
            print(f"    ✗ {e}")

        if iterative_fill and (
            plan_diag.get("errors") or plan_diag.get("dynamic_escalated")
        ):
            print("  [fill] iterative-fill: refreshing form_fields + re-plan ...")
            fresh = _evaluate(_FORM_FIELDS_JS) or {}
            if fresh:
                target["form_fields"] = fresh
                if fresh.get("form_root_selector"):
                    target["form_root_selector"] = fresh["form_root_selector"]
            target.pop("_llm_plan", None)
            target.pop("_llm_plan_meta", None)
            iter_cfg = config
            iter_reason = "iterative_retry"
            if _has_form_analyzer_escalation(config):
                escalated_model = _form_analyzer_escalation_model(config)
                if escalated_model:
                    iter_cfg = _config_with_form_analyzer_model(config, escalated_model)
                    iter_reason = "iterative_retry_escalated"
            plan2 = _llm_analyze_form(
                target,
                iter_cfg,
                body_max_chars=char_limit,
                escalation_reason=iter_reason,
            )
            if plan2 and plan2.get("fields"):
                already = set()
                for x in plan_diag.get("filled") or []:
                    part = str(x).split("=")[0].strip()
                    if part:
                        already.add(part)
                plan2["fields"] = [
                    f for f in plan2["fields"]
                    if f.get("name") and f["name"] not in already
                ]
                if plan2["fields"]:
                    diag2 = fill_form_with_plan(
                        plan2, body, target=target, evaluate_fn=_evaluate
                    )
                    plan_diag["filled"] = (plan_diag.get("filled") or []) + (
                        diag2.get("filled") or []
                    )
                    plan_diag["errors"] = (plan_diag.get("errors") or []) + (
                        diag2.get("errors") or []
                    )
                    _emit_event(
                        "send.fill.iterative_pass",
                        stage="send",
                        target_id=tid,
                        payload={"fields": len(plan2["fields"])},
                        trace_dir=trace_dir,
                    )
    else:
        print(f"  [fill] no LLM plan available (form_fields missing or analyzer failed) — using heuristics only")

    # If plan filled all body and most required fields, we're done. Otherwise
    # run heuristics as backup for anything missed.

    return _heuristic_fill_fallback(target, config, body, plan_diag)


def _heuristic_fill_fallback(target: dict[str, Any], config: dict[str, Any],
                              body: str, plan_diag: dict[str, Any] | None) -> dict[str, Any]:
    """Original heuristic-pattern fill, used as backup after LLM plan."""
    sender = config.get("sender", {})
    overrides = target.get("field_map_overrides", {}) or {}
    # Seed diagnostics with LLM plan's results
    diagnostics = {
        "filled": list((plan_diag or {}).get("filled", [])),
        "unfilled": [],
        "errors": list((plan_diag or {}).get("errors", [])),
        "warnings": list((plan_diag or {}).get("warnings", [])),
        "skipped": list((plan_diag or {}).get("skipped", [])),
        "llm_plan_used": plan_diag is not None,
    }

    # Phone format (default no-hyphen)
    phone_format = overrides.get("phone_format", "no_hyphen")
    phone_value = sender["phone_hyphenated"] if phone_format == "hyphenated" else sender["phone"]

    # Postal code format
    postal_format = overrides.get("postal_format", "no_hyphen")
    postal_value = sender["postal_code_no_hyphen"] if postal_format == "no_hyphen" else sender["postal_code"]

    # v15 §S2: derive split address parts when the brief only carries a single
    # address string (pure heuristic split; explicit sender keys always win).
    from _outreach_core import form_validation as fv

    addr_split = fv.split_jp_address(
        str(sender.get("full_address") or sender.get("address") or "")
    )
    sender_prefecture = sender.get("prefecture") or addr_split["prefecture"]
    sender_city = sender.get("city") or addr_split["city"]
    sender_address_line = sender.get("address_line") or addr_split["address_line"]
    sender_building = sender.get("building") or addr_split["building"]
    sender_full_address = str(
        sender.get("full_address") or sender.get("address") or ""
    )

    # Sender field fill (multi-shot for split fields).
    # Order matters: kana/ふりがな variants fill BEFORE kanji splits and full-name
    # patterns so a 「姓（フリガナ）」 field is taken by the kana pattern first and
    # never receives kanji. Split values prefer explicit sender keys, then a
    # whitespace split, then the legacy 2-char heuristic (fv.sender_name_parts).
    name_parts = fv.sender_name_parts(sender)
    fills = [
        # Kana / ふりがな splits first (most specific labels)
        ("name_furigana_sei", name_parts["name_furigana_sei"], SENDER_FIELD_PATTERNS["name_furigana_sei"]),
        ("name_furigana_mei", name_parts["name_furigana_mei"], SENDER_FIELD_PATTERNS["name_furigana_mei"]),
        ("name_kana_sei", name_parts["name_kana_sei"], SENDER_FIELD_PATTERNS["name_kana_sei"]),
        ("name_kana_mei", name_parts["name_kana_mei"], SENDER_FIELD_PATTERNS["name_kana_mei"]),
        # Company-kana / company-furigana BEFORE bare 「フリガナ」 patterns so
        # 「会社名（フリガナ）」 fields are taken by company_kana, not name_kana.
        ("company_kana", sender.get("company_kana", ""), SENDER_FIELD_PATTERNS["company_kana"]),
        ("company_furigana", sender.get("company_furigana", ""), SENDER_FIELD_PATTERNS["company_furigana"]),
        # Full kana fields before kanji fulls (お名前（フリガナ） must get kana)
        ("name_kana", sender["name_kana"], SENDER_FIELD_PATTERNS["name_kana"]),
        ("name_furigana", sender["name_furigana"], SENDER_FIELD_PATTERNS["name_furigana"]),
        # Kanji splits
        ("name_sei", name_parts["name_sei"], SENDER_FIELD_PATTERNS["name_sei"]),
        ("name_mei", name_parts["name_mei"], SENDER_FIELD_PATTERNS["name_mei"]),
        # Then full-name fallbacks
        ("name", sender["name"], SENDER_FIELD_PATTERNS["name"]),
        ("company", sender["company"], SENDER_FIELD_PATTERNS["company"]),
        ("role", sender["role"], SENDER_FIELD_PATTERNS["role"]),
        ("email", sender["email"], SENDER_FIELD_PATTERNS["email"]),
        ("email_confirm", sender["email"], SENDER_FIELD_PATTERNS["email_confirm"]),
        ("phone", phone_value, SENDER_FIELD_PATTERNS["phone"]),
        ("postal_code", postal_value, SENDER_FIELD_PATTERNS["postal_code"]),
        ("city", f"{sender_city}{sender_address_line}", SENDER_FIELD_PATTERNS["city"]),
        ("address_line", sender_address_line, SENDER_FIELD_PATTERNS["address_line"]),
        ("building", sender_building, SENDER_FIELD_PATTERNS["building"]),
        ("address_full", sender_full_address, SENDER_FIELD_PATTERNS["address_full"]),
    ]
    for kind, value, patterns in fills:
        if not value:
            continue
        res = _fill_field(kind, value, patterns)
        if res and res.get("filled"):
            diagnostics["filled"].append(f"{kind}={value[:30]} (label={res.get('label')})")
            time.sleep(0.2)
        else:
            diagnostics["unfilled"].append(kind)

    # v15 §S2: date fields (ご希望日 etc.) → 7 business days out, ISO format.
    date_value = fv.default_date_value()
    date_res = _fill_field(
        "contact_date", date_value,
        [r"希望日", r"予定日", r"ご都合", r"日程", r"実施日"],
    )
    if date_res and date_res.get("filled"):
        diagnostics["filled"].append(f"contact_date={date_value}")

    # Prefecture select (separate handling)
    pref_res = _fill_select(sender_prefecture, label_pattern=r"都道府県|prefecture")
    if pref_res and pref_res.get("selected"):
        diagnostics["filled"].append(f"prefecture={sender_prefecture}")

    # Known-label selects (従業員数, 業種, きっかけ, 予算, 連絡方法 …): answer every
    # required or recognizable dropdown deterministically via the pure chooser,
    # instead of leaving them for the submit-gate retry loop.
    for entry in _auto_fill_known_selects(sender):
        diagnostics["filled"].append(entry)

    # Apply overrides
    if overrides.get("category_radio"):
        rres = _fill_radio(overrides["category_radio"])
        if rres and rres.get("selected"):
            diagnostics["filled"].append(f"category_radio={overrides['category_radio']}")
        else:
            diagnostics["errors"].append(f"category_radio not found: {overrides['category_radio']}")
    if overrides.get("category_select"):
        sres = _fill_select(overrides["category_select"])
        if sres and sres.get("selected"):
            diagnostics["filled"].append(f"category_select={overrides['category_select']}")
        else:
            diagnostics["errors"].append(f"category_select not found: {overrides['category_select']}")
    else:
        inquiry = _ensure_inquiry_type_action(target, None, stage="send")
        if inquiry.get("selected"):
            diagnostics["filled"].append(
                f"inquiry_type=auto(src=fallback, count={int(inquiry.get('selected') or 0)})"
            )
            _rescan_after_inquiry_type(target, sender, diagnostics)
        elif inquiry.get("no_b2b"):
            diagnostics["warnings"].append("no_b2b_inquiry_type")
    if overrides.get("gender_radio"):
        rres = _fill_radio(overrides["gender_radio"])
        if rres and rres.get("selected"):
            diagnostics["filled"].append(f"gender_radio={overrides['gender_radio']}")
    if overrides.get("contact_method_radio"):
        rres = _fill_radio(overrides["contact_method_radio"])
        if rres and rres.get("selected"):
            diagnostics["filled"].append(f"contact_method={overrides['contact_method_radio']}")
    if overrides.get("contact_time_radio"):
        rres = _fill_radio(overrides["contact_time_radio"])
        if rres and rres.get("selected"):
            diagnostics["filled"].append(f"contact_time={overrides['contact_time_radio']}")
    if overrides.get("consultation_radio"):
        rres = _fill_radio(overrides["consultation_radio"])
        if rres and rres.get("selected"):
            diagnostics["filled"].append(f"consultation={overrides['consultation_radio']}")
    if overrides.get("referral_source_radio"):
        rres = _fill_radio(overrides["referral_source_radio"])
        if rres and rres.get("selected"):
            diagnostics["filled"].append(f"referral_source={overrides['referral_source_radio']}")
    if overrides.get("employee_count_required"):
        band = str(sender.get("employee_count_band") or "10")
        sres = _fill_select(band, label_pattern=r"従業員")
        if sres and sres.get("selected"):
            diagnostics["filled"].append(f"employee_count={band}")
    extra_cb_labels = overrides.get("extra_checkboxes_by_label") or []
    if isinstance(extra_cb_labels, str):
        extra_cb_labels = [extra_cb_labels]
    for lab in extra_cb_labels:
        cres = _check_by_label(lab)
        if cres and cres.get("ok"):
            diagnostics["filled"].append(f"checkbox_label={lab}")
        else:
            diagnostics["errors"].append(f"checkbox_label not found: {lab}")

    # Body textarea
    bres = _fill_textarea(body)
    if bres and bres.get("filled"):
        diagnostics["filled"].append(f"body ({len(body)} chars)")
    else:
        diagnostics["errors"].append("body textarea fill failed")

    # Submit-gate controls (required / privacy agreement / inquiry route).
    live_gates = _auto_fill_live_gates(
        phase="send",
        target_id=str(target.get("id") or target.get("name") or ""),
    )
    gate = live_gates.get("checkboxes") or {}
    radios = live_gates.get("radios") or {}
    selects = live_gates.get("selects") or {}
    if gate.get("checked_count", 0) > 0:
        diagnostics["filled"].append(
            f"gate_checkboxes={gate.get('checked_count')} ({', '.join((gate.get('checked_labels') or [])[:2])})"
        )
    if radios.get("selected_count", 0) > 0:
        diagnostics["filled"].append(
            f"gate_radios={radios.get('selected_count')} ({', '.join((radios.get('selected_items') or [])[:2])})"
        )
    if selects.get("selected_count", 0) > 0:
        diagnostics["filled"].append(
            f"gate_selects={selects.get('selected_count')} ({', '.join((selects.get('selected_items') or [])[:2])})"
        )

    # v15 guardrails: correct furigana script + fill a required subject/title
    # before any submit, so the form never bounces us with a format/required error.
    guard = _apply_fill_guardrails(target, sender, body, diagnostics)
    if guard.get("kana_fixed"):
        print(f"  [fill] kana-guard: {', '.join(guard['kana_fixed'][:3])}")
    if guard.get("postal_fixed"):
        print(f"  [fill] postal-guard: {', '.join(guard['postal_fixed'][:3])}")
    if guard.get("subject_filled"):
        print(f"  [fill] subject-guard: {guard['subject_filled']}")

    return diagnostics


_EXPLICIT_WRONG_FORM_WARNING_RE = re.compile(
    r"(wrong[_ -]?form|non[_ -]?contact|"
    r"(?:会員登録|アカウント作成|ログイン|採用|応募|求人|予約)(?:専用フォーム|フォーム専用|専用|のみ)|"
    r"B2B(?:提案|問い合わせ).*(?:不適切|不可)|"
    r"(?:問い合わせ|お問い合わせ)フォームではない)",
    re.IGNORECASE,
)
_WRONG_FORM_SKIPPED_KW = (
    "login_pass", "password", "passwd",
    "birth_date", "birthday", "birth_year",
    "applicant", "resume", "shokureki", "gakureki", "keireki",
    "学歴", "職歴", "保有資格", "希望勤務地", "希望年収",
    "予約日", "希望日", "希望時間",
)


def _detect_wrong_form_type(diag: dict[str, Any]) -> str | None:
    """If the filled form looks like a non-B2B form (registration / job application
    / reservation), return a short reason string. Else None.
    """
    warnings = diag.get("warnings") or []
    skipped = diag.get("skipped") or []

    # LLM warnings are free-form cautions.  A single incidental word such as
    # 「予約日時フィールド」 or a navigation mention of 「採用」 is not evidence
    # that the whole contact form is the wrong type (JMS/KeePer false positives).
    # Abort only when the warning explicitly classifies the destination as a
    # dedicated/non-contact form.  Structured page classification already ran
    # before this guard and remains the primary wrong-form gate.
    for w in warnings:
        w_str = str(w)
        if _EXPLICIT_WRONG_FORM_WARNING_RE.search(w_str):
            return f"explicit wrong-form warning: {w_str[:120]}"

    suspect_hits: list[str] = []
    for s in skipped:
        s_low = str(s).lower()
        for kw in _WRONG_FORM_SKIPPED_KW:
            if kw.lower() in s_low:
                suspect_hits.append(str(s))
                break
    if len(suspect_hits) >= 2:
        return f"skipped {len(suspect_hits)} non-B2B required fields: {suspect_hits[:4]}"

    return None


def _detect_empty_submission_risk(diag: dict[str, Any]) -> str | None:
    """Block submit/verify when nothing was actually filled.

    Some guide/gate pages contain generic success words such as "完了" while
    exposing no usable inquiry form. If body fill failed and we filled zero
    fields, any later "success" signal is almost certainly page-copy noise.
    """
    errors = [str(e) for e in (diag.get("errors") or [])]
    filled = [str(f) for f in (diag.get("filled") or [])]
    if any("body textarea fill failed" in e for e in errors) and not filled:
        return "本文を入力できず、入力済み項目も0件のため送信成功判定へ進めません"
    return None


def _escalate_await_proceed(target: dict[str, Any], reason: str) -> None:
    """Record needs_attention + Slack notify; browser stays open for resolve --action proceed."""
    tid = target.get("id", "?")
    name = target.get("name", "?")
    short = _humanize_blocker_reason(reason)
    append_needs_attention(
        DATA_DIR,
        {
            "target_id": tid,
            "name": name,
            "channel": "jp_form",
            "reason": reason,
            "action_needed": "proceed",
            "slack_message": (
                f"{name}: {short}。Slack で「{tid} 進めて」と返すと手動再開できます。\n"
                f"詳細: {reason}"
            ),
        },
    )
    _emit_event(
        "send.escalated",
        stage="send",
        target_id=str(tid),
        payload={
            "field_count_unresolved": 0,
            "slack_posted": True,
            "reason": reason[:160],
            "reason_class": _classify_blocker_reason(reason),
        },
    )


def _classify_blocker_reason(reason: str) -> str:
    """Map a free-text blocker reason to a stable class for analytics/messaging."""
    r = (reason or "").lower()
    if "recaptcha" in r or "captcha" in r:
        return "captcha"
    if "submit button not found" in r or "final submit" in r:
        return "submit_button_not_found"
    if "wrong_form" in r or "wrong form" in r:
        return "wrong_form_type"
    if "dynamic required" in r or "想定外" in reason:
        return "unexpected_required_field"
    return "other"


def _humanize_blocker_reason(reason: str) -> str:
    """Short, truthful Japanese label for Slack — never says 'reCAPTCHA' unless真に captcha."""
    return {
        "captcha": "reCAPTCHA（可視チャレンジ）で停止",
        "submit_button_not_found": "送信ボタンがDOMで特定できず停止（captchaではない）",
        "wrong_form_type": "想定と異なるフォーム種別を検出して中断",
        "unexpected_required_field": "想定外の必須項目があり停止",
        "other": "送信フローで停止",
    }[_classify_blocker_reason(reason)]


def _auto_skip_and_log(target: dict[str, Any], reason: str) -> None:
    """Autonomous-mode replacement for ``_escalate_await_proceed``.

    Instead of leaving a browser open and blocking on a human "進めて", this
    records the target in skip_history (so it is excluded on the next run),
    auto-closes any matching needs_attention, and emits an event — then the
    caller simply ``continue``s. Nothing waits on a human.
    """
    tid = target.get("id", "?")
    name = target.get("name", "?")
    skip_copy = dict(target)
    skip_copy["draft"] = {
        **(target.get("draft") or {}),
        "body": f"AUTONOMOUS_SKIP: {reason}",
    }
    append_skip_history([skip_copy])
    try:
        close_needs_attention(DATA_DIR, str(tid), resolution=f"auto_skip: {reason}"[:120])
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass
    print(f"  [send] ⏭ auto-skip & log: {name} — {reason}")
    _emit_event(
        "send.auto_skipped",
        stage="send",
        target_id=str(tid),
        payload={"reason": reason[:160]},
    )
    # v30 §WS-E — drop the runtime snapshot for a definitively skipped
    # target. send_journal still records the lifecycle; this only clears
    # the diagnostic "last position" file so list_runtime_states reflects
    # in-flight targets only.
    try:
        core_target_state.clear_state(DATA_DIR, str(tid))
    except Exception:  # noqa: BLE001
        pass
    # v30 §WS-D — surface auto-skips to Slack so the thread stops looking
    # silent when several targets are skipped in a row. Best-effort; the
    # decision is already persisted to skip_history above.
    try:
        from _outreach_core import notify as _notify
        _notify.post_target_event(
            stage="send",
            status="skipped",
            target=target,
            detail={"reason": reason[:160]},
        )
    except Exception:  # noqa: BLE001 - Slack must never abort the loop
        pass


def _config_with_warmup_sec(config: dict[str, Any] | None, warm_sec: int) -> dict[str, Any]:
    """Shallow copy of config with captcha.warmup_sec overridden (adaptive warmup)."""
    cfg = dict(config or {})
    captcha_block = dict(cfg.get("captcha") or {})
    captcha_block["warmup_sec"] = warm_sec
    cfg["captcha"] = captcha_block
    return cfg


def _config_force_cf_warmup(config: dict[str, Any] | None, warm_sec: int) -> dict[str, Any]:
    """Config that FORCES a genuine root-domain warmup dwell, for domains known
    to be Cloudflare-gated. This is legitimate hygiene: the persistent browser
    profile loads the root domain for real and dwells so it carries its own
    cf_clearance cookie before we navigate to the form — we never solve or
    bypass the challenge. Min 20s dwell so clearance can actually be issued."""
    cfg = dict(config or {})
    captcha_block = dict(cfg.get("captcha") or {})
    captcha_block["v3_strategy"] = "passthrough_with_warmup"
    captcha_block["warmup_sec"] = max(20, int(warm_sec or 0))
    cfg["captcha"] = captcha_block
    return cfg


def _live_captcha_state() -> dict[str, Any]:
    """Evaluate the LIVE page for an actually-visible/blocking reCAPTCHA.
    Distinguishes a real v2 challenge from a non-blocking v3/checkbox, so a
    submit-button failure is never mislabeled as 'reCAPTCHA'."""
    try:
        raw = _evaluate(core_captcha.LIVE_CAPTCHA_JS)
    except Exception:  # noqa: BLE001 - detection must never crash a send
        raw = None
    return core_captcha.classify_live_state(raw)


def _combine_page_evidence_text(snapshot: str | None, page_evidence: dict[str, Any] | None) -> str:
    """Merge browser snapshot text with structured post-submit evidence."""
    combined = snapshot or ""
    if isinstance(page_evidence, dict):
        combined = (
            f"{combined}\n"
            f"{page_evidence.get('text', '')}\n"
            f"{page_evidence.get('cf7_response_text', '')}\n"
            f"{page_evidence.get('submission_status_text', '')}\n"
            f"{page_evidence.get('url', '')}"
        )
    return combined


def _blocker_diagnostics(d: dict[str, Any], *, trace: Any = None) -> dict[str, Any]:
    """Capture actionable diagnostics at a blocker: the LIVE buttons on the page,
    current URL, and a saved snapshot path. This is what lets a human or the deep
    resolver actually act (the candidate buttons usually contain the real submit
    target that no regex matched)."""
    diag: dict[str, Any] = {
        "buttons": [], "url": d.get("form_url", ""), "snapshot_path": "", "screenshot_path": "",
    }
    try:
        btns = _enumerate_buttons(form_root_selector=d.get("form_root_selector"))
        diag["buttons"] = [b.get("text", "") for b in (btns or []) if b.get("text")][:20]
    except Exception:  # noqa: BLE001
        pass
    try:
        from _outreach_core.verify import PAGE_EVIDENCE_JS
        pe = _evaluate(PAGE_EVIDENCE_JS)
        if isinstance(pe, dict) and pe.get("url"):
            diag["url"] = pe.get("url")
    except Exception:  # noqa: BLE001
        pass
    try:
        snap = oc_browser("snapshot") or ""
        if snap.strip():
            p = DATA_DIR / f"resolve_snapshot_{d.get('id', d.get('name', 'x'))}.txt"
            p.write_text(snap, encoding="utf-8")
            diag["snapshot_path"] = str(p)
    except Exception:  # noqa: BLE001
        pass
    # Visual evidence: full-page screenshot of the errored form (active tab = the
    # failing page at this moment, so no tab-routing risk). openclaw returns the
    # saved path as "MEDIA:<path>".
    shot = _capture_screenshot()
    if shot:
        diag["screenshot_path"] = shot
    return diag


def _capture_screenshot() -> str:
    """Full-page screenshot of the current (active) tab. Returns the saved path
    parsed from openclaw's 'MEDIA:<path>' output, or '' on failure."""
    try:
        out = oc_browser("screenshot", "--full-page") or ""
        m = re.search(r"MEDIA:(\S+)", out)
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return ""


# --- Tab management (v6 §17) -------------------------------------------------
# openclaw's `open` already creates a NEW tab. These helpers TRACK the tab's
# stable targetId so we can close it on success, keep it open on error (evidence
# + in-place resolution), and cap total tabs. All degrade gracefully: if the id
# can't be captured, behaviour falls back to today's active-tab flow.

def _tab_isolation_enabled(config: dict[str, Any] | None) -> bool:
    br = (config or {}).get("browser") or {}
    val = br.get("tab_isolation")
    if val is None:
        return True  # default ON
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(val)


def _open_tab(url: str) -> str | None:
    """Open url in a new tab; return its targetId (or None → caller falls back)."""
    try:
        payload = core_adapters.get_browser().browser_json("open", url)
        return core_tab_utils.target_id_from_open(payload)
    except Exception:  # noqa: BLE001
        return None


def _focus_tab(target_id: str) -> bool:
    if not target_id:
        return False
    try:
        return oc_browser("focus", target_id) is not None
    except Exception:  # noqa: BLE001
        return False


def _close_tab(target_id: str | None) -> None:
    if not target_id:
        return
    try:
        oc_browser("close", target_id)
    except Exception:  # noqa: BLE001
        pass


def _close_tab_safely(target_id: str | None) -> None:
    """Crash-path tab cleanup (v15 §R1) — must never raise."""
    try:
        _close_tab(target_id)
    except Exception:  # noqa: BLE001
        pass


def _list_tabs_payload() -> Any:
    try:
        return core_adapters.get_browser().browser_json("tabs")
    except Exception:  # noqa: BLE001
        return None


def _tab_is_open(target_id: str) -> bool:
    return core_tab_utils.is_tab_open(_list_tabs_payload(), target_id)


def _enforce_tab_cap(protect: set[str], cap: int = 12) -> None:
    """Close oldest page tabs beyond ``cap`` except protected (resolver-bound) and
    the newest. Keeps the browser from accumulating unbounded tabs over a batch."""
    try:
        payload = _list_tabs_payload()
        for tid in core_tab_utils.closable_overflow(payload, protect=protect, cap=cap):
            _close_tab(tid)
    except Exception:  # noqa: BLE001
        pass


_PAGE_TEXT_HEAD_JS = r"""
() => (document.body && document.body.innerText ? document.body.innerText : '').slice(0, 4000)
"""


def _try_iframe_form_takeover(
    d: dict[str, Any],
    fields: dict[str, Any] | None,
    trace: Any,
    tid: str,
) -> str | None:
    """v30 §WS-B / §WS-E — thin shim over
    :func:`_outreach_core.send_pipeline.try_iframe_form_takeover` so the
    existing call site stays intact while the underlying logic now lives in
    a unit-testable module without hidden globals.

    Behaviour is identical to the pre-extraction version: when the top-level
    page has no form but a hosted-form iframe is present, navigate to the
    iframe src and let the send pipeline operate against that page directly.
    Returns the iframe src on success, ``None`` otherwise.
    """
    from _outreach_core import send_pipeline as _sp

    def _open(src: str) -> None:
        print(f"  [send] ↳ iframe-hosted form 検出 → open {src}")
        oc_browser("open", src)

    return _sp.try_iframe_form_takeover(
        d, fields,
        open_url=_open,
        sleep_fn=time.sleep,
        sleep_sec=float(RATE_LIMIT_SECONDS),
        emit_event=_emit_event,
        trace_dir=trace,
        target_id=tid,
    )


def _assess_page_and_recover(
    d: dict[str, Any],
    timeline: list[dict[str, Any]],
    trace: Any,
) -> tuple[bool, dict[str, Any]]:
    """v17 URL精査: classify the current page's form state BEFORE filling.

    empty_render → wait 3s + rescan once (JS-rendered pages).
    no_form / error_page → ONE recovery hop: follow the page's own contact-ish
    links (guide pages like ain_holdings link to the real form). On success the
    target's form_url is updated in place and an event is emitted.

    Returns (ok, state_dict).
    """
    tid = str(d.get("id") or d.get("name") or "?")

    def _scan() -> tuple[dict[str, Any], dict[str, Any], str]:
        fields = _rescan_form_fields(d)
        text = _evaluate(_PAGE_TEXT_HEAD_JS)
        text_s = text if isinstance(text, str) else ""
        return (
            core_contact_url.classify_page_form_state(fields, text_s),
            fields if isinstance(fields, dict) else {},
            text_s,
        )

    state, page_fields, page_text = _scan()
    # v30 §WS-B — progressive poll for client-rendered (SPA) forms. The legacy
    # single 3s wait was a magic number that fired too early for Medley /
    # ROXX class pages where React + code-splitting takes 5–8 seconds to
    # populate the DOM. Poll at 1.5s + 3s (cumulative 4.5s); stop as soon as a
    # form materialises. Total worst case ≈ 5s, equal to the legacy budget
    # but with a chance to succeed earlier and a chance to succeed at all on
    # slower SPAs.
    if state.get("state") == "empty_render":
        for wait_sec in (1.5, 3.0):
            time.sleep(wait_sec)
            state, page_fields, page_text = _scan()
            if state.get("state") != "empty_render":
                _emit_event(
                    "send.client_rendered.rescued",
                    stage="send", target_id=tid,
                    payload={
                        "state": state.get("state"),
                        "wait_sec": float(wait_sec),
                    },
                    trace_dir=trace,
                )
                break
        else:
            # v30 §WS-B — if the form never rendered, look for a hosted-form
            # iframe (HubSpot / formrun / Tayori / Salesforce / same-domain
            # embed). LegalOn-class: the form lives on lp.legalforce-cloud.com
            # inside the parent /contact/ page. We don't drive the iframe in
            # place — instead, navigate to the iframe src as a top-level page
            # so the rest of the send pipeline operates without iframe scoping.
            iframe_src = _try_iframe_form_takeover(d, page_fields, trace, tid)
            if iframe_src:
                state, page_fields, page_text = _scan()
    ok = state.get("state") in ("form_ok", "gate_like")
    core_timeline.add(
        timeline, "page_state", ok,
        state=state.get("state"),
        inputs=state.get("inputs"),
        textareas=state.get("textareas"),
        buttons=state.get("submit_buttons"),
    )
    if ok:
        # v25: OTP gate — メール確認コード方式は記入前に検知して中断する。
        # (確認コード未入力のまま進むと確認画面でリセット＝「フォーム消失」)
        otp = core_contact_url.detect_email_verification(page_text, page_fields)
        if otp.get("detected"):
            core_timeline.add(
                timeline, "otp_gate", False, evidence=otp.get("evidence"),
            )
            _emit_event(
                "send.otp_gate_detected", stage="send", target_id=tid,
                payload={"evidence": otp.get("evidence")}, trace_dir=trace,
            )
            print(f"  [send] ⚠ OTPゲート検出（メール確認コード方式）— 自動送信不可: "
                  f"{', '.join(otp.get('evidence') or [])}")
            return False, {**state, "state": "otp_gate"}
        return True, state

    # v25: ユーザー確認済みURLはリカバリで勝手に差し替えない（フェリシモで
    # IRフォームへ自動補正された事故の再発防止）。
    if d.get("form_url_locked"):
        core_timeline.add(timeline, "url_recovery", None, skipped="form_url_locked")
        _emit_event(
            "send.url_recovery_skipped_locked", stage="send", target_id=tid,
            payload={"form_url": str(d.get("form_url") or "")[:200]}, trace_dir=trace,
        )
        print("  [send] ✗ URLリカバリ抑止: form_url_locked（ユーザー確認済みURLのため差し替え禁止）")
        return False, state

    # Recovery hop: the page itself usually links to the real form.
    cur_url = str(_evaluate("() => location.href") or d.get("form_url") or "")
    cands = core_contact_url.contact_link_candidates(_list_page_links(), cur_url)
    tried: list[str] = []
    for cand in cands[:2]:
        tried.append(cand)
        _evaluate(f"() => {{ location.href = {json.dumps(cand)}; return true; }}")
        time.sleep(2.5)
        st2, fields2, text2 = _scan()
        if st2.get("state") in ("form_ok", "gate_like"):
            # v25: リカバリ先のフォーム種別を再分類し、IR/採用/ログイン等の
            # 非contactフォームへの誤着地（フェリシモIRフォーム事故）を拒否。
            kind2, reason2 = core_contact_url.classify_form_type(fields2, text2)
            if kind2 not in ("contact", "unknown_no_textarea"):
                _emit_event(
                    "send.url_recovery_rejected", stage="send", target_id=tid,
                    payload={"to": cand[:200], "kind": kind2, "reason": reason2},
                    trace_dir=trace,
                )
                print(f"  [send] ✗ リカバリ先を不採用 (form_type={kind2}): {cand}")
                continue
            otp2 = core_contact_url.detect_email_verification(text2, fields2)
            if otp2.get("detected"):
                _emit_event(
                    "send.url_recovery_rejected", stage="send", target_id=tid,
                    payload={"to": cand[:200], "kind": "otp_gate",
                             "evidence": otp2.get("evidence")},
                    trace_dir=trace,
                )
                print(f"  [send] ✗ リカバリ先を不採用 (OTPゲート): {cand}")
                continue
            old_url = d.get("form_url")
            d["form_url"] = cand
            _emit_event(
                "send.url_recovered", stage="send", target_id=tid,
                payload={"from": old_url, "to": cand, "state": st2.get("state"),
                         "form_type": kind2},
                trace_dir=trace,
            )
            core_timeline.add(timeline, "url_recovery", True, to=cand)
            print(f"  [send] ✓ URLリカバリ: {old_url} → {cand} (form_type={kind2})")
            return True, st2
    core_timeline.add(
        timeline, "url_recovery", (False if cands else None),
        candidates=len(cands), tried=tried or None,
    )
    return False, state


def _queue_for_resolver(
    d: dict[str, Any],
    reason_class: str,
    reason: str,
    *,
    trace: Any = None,
    autonomous: bool,
    tab_id: str | None = None,
) -> None:
    """Non-blocking blocker handling: capture diagnostics, enqueue the target for
    the deep resolver, append needs_attention, and post a CLEAR actionable
    message (no misleading 「進めて」). The main batch keeps moving. The errored
    tab (``tab_id``) is left OPEN so the resolver can focus it in place (§17)."""
    tid = str(d.get("id") or d.get("name") or "?")
    name = d.get("name", "?")
    diag = _blocker_diagnostics(d, trace=trace)
    # v17: attach the stage timeline so the escalation shows WHERE the process
    # actually failed (e.g. ページ状態で失敗 = URL problem), not the last symptom.
    tl = d.get("_send_timeline")
    if isinstance(tl, list) and tl:
        diag["timeline"] = tl
        try:
            from _outreach_core import events as _ev
            _ev.dump_trace(trace, "send_timeline.json", tl)
        except Exception:  # noqa: BLE001
            pass
    entry = {
        "target_id": tid,
        "name": name,
        "channel": "jp_form",
        "reason_class": reason_class,
        "reason": reason,
        "form_url": d.get("form_url", ""),
        "flow": d.get("flow"),
        "tab_id": tab_id,
        "diagnostics": diag,
    }
    core_resolve_queue.enqueue(DATA_DIR, entry)
    # v30 §WS-F — emit BOTH the legacy text and the structured action list so
    # the OpenClaw Slack bot can render buttons (URL/retry/skip) once its end
    # is updated. Existing consumers that only read ``slack_message`` keep
    # working unchanged.
    actionable = core_resolve_queue.build_actionable_payload(entry, auto_resolver=autonomous)
    slack_message = actionable["text"]
    slack_actions = actionable["actions"]
    append_needs_attention(
        DATA_DIR,
        {
            "target_id": tid,
            "name": name,
            "channel": "jp_form",
            "reason": reason,
            "reason_class": reason_class,
            "action_needed": "auto_resolve",
            "buttons": diag.get("buttons"),
            "snapshot_path": diag.get("snapshot_path"),
            "slack_message": slack_message,
            "slack_actions": slack_actions,
        },
    )
    _emit_event(
        "send.queued_for_resolver", stage="send", target_id=tid,
        payload={"reason_class": reason_class, "buttons": diag.get("buttons"), "reason": reason[:160]},
        trace_dir=trace,
    )


def _handle_blocker(
    target: dict[str, Any],
    reason: str,
    *,
    autonomous: bool,
) -> None:
    """Route a mid-run blocker by autonomy policy: auto-skip (autonomous) or
    escalate-and-wait (supervised / brief override)."""
    if autonomous:
        _auto_skip_and_log(target, reason)
    else:
        _escalate_await_proceed(target, f"awaiting_user_proceed: {reason}")


def _resolve_in_open_tab(
    d: dict[str, Any],
    tab_id: str,
    config: dict[str, Any],
    *,
    trace: Any,
    flow: str,
    verify_strict: bool,
) -> dict[str, Any]:
    """Resolve a blocker on the ALREADY-OPEN errored tab (§17): focus it, confirm
    it's the right company (same_site guard — never submit into the wrong tab),
    then hunt the submit button on the preserved DOM and verify. Returns
    {"vresult", "page_text", "status"}. The form is already filled — no re-fill."""
    from _outreach_core.verify import PAGE_EVIDENCE_JS

    if not _focus_tab(tab_id):
        return {"vresult": None, "page_text": "", "status": "focus_failed"}
    time.sleep(1.0)
    pe = _evaluate(PAGE_EVIDENCE_JS)
    cur_url = pe.get("url") if isinstance(pe, dict) else ""
    if not core_tab_utils.same_site(cur_url, d.get("form_url", "")):
        # Safety: focused tab is not this company's form — do NOT submit.
        return {"vresult": None, "page_text": "", "status": "site_mismatch", "cur_url": cur_url}

    # v24 §S3: hunt the submit on the preserved DOM with the closed-loop driver
    # (observes the LIVE state — the page may be an input form, a validation
    # bounce, or already a confirm page). mode="retry" → no double journaling.
    _arm_dialog_autoaccept()
    resolve_timeline: list[dict[str, Any]] = []
    subres = _submission_loop(
        d, config, str((d.get("draft") or {}).get("body") or ""),
        flow=flow, mode="retry", trace=trace,
        tid=str(d.get("id") or d.get("name") or "?"),
        timeline=resolve_timeline,
    )
    if subres.get("status") == "click_failed" and int(subres.get("clicks") or 0) == 0:
        return {"vresult": None, "page_text": "", "status": "no_button"}
    time.sleep(2)
    page_evidence = _evaluate(PAGE_EVIDENCE_JS)
    snap = oc_browser("snapshot")
    combined = _combine_page_evidence_text(
        snap, page_evidence if isinstance(page_evidence, dict) else None
    )
    vresult = verify_send_completed(
        d, "jp_form", snapshot=combined,
        browser_verify=page_evidence if isinstance(page_evidence, dict) else None,
        plan=d.get("_llm_plan"), evaluate_fn=_evaluate, data_dir=DATA_DIR,
        snapshot_path=None, verify_strict=verify_strict,
    )
    return {"vresult": vresult, "page_text": combined, "status": "attempted"}


def _deep_submit(
    d: dict[str, Any],
    body: str,
    config: dict[str, Any],
    *,
    trace: Any,
    flow: str,
    verify_strict: bool,
    iterative_fill: bool,
) -> dict[str, Any]:
    """Open the form fresh and run a robust fill→submit→verify for ``body``.

    Submit detection is regex-first then **LLM-pick over document-wide buttons**
    (the strategy that recovers cases like パーク24/王将 where the real button is
    「お問い合わせを送信する」 but no regex matched). Used both by the §3.6 URL
    fallback and by the deep resolver pass (§16). Returns {"vresult", "page_text"}.
    """
    sanitized_body = body
    from _outreach_core import events as ev
    from _outreach_core.cookie_dismiss import apply_cookie_dismiss
    from _outreach_core.verify import PAGE_EVIDENCE_JS

    form_url = d["form_url"]
    tid = str(d.get("id") or d.get("name") or "?")

    before_url = ""
    try:
        before_evidence = _evaluate(PAGE_EVIDENCE_JS)
        if isinstance(before_evidence, dict):
            before_url = str(before_evidence.get("url") or "")
    except Exception:  # noqa: BLE001
        before_url = ""

    oc_browser("open", form_url)
    time.sleep(RATE_LIMIT_SECONDS)
    try:
        after_open_evidence = _evaluate(PAGE_EVIDENCE_JS)
        after_open_url = (
            str(after_open_evidence.get("url") or "")
            if isinstance(after_open_evidence, dict)
            else ""
        )
    except Exception:  # noqa: BLE001
        after_open_url = ""
    if (
        before_url
        and after_open_url
        and after_open_url == before_url
        and not core_tab_utils.same_site(after_open_url, form_url)
    ):
        _emit_event(
            "resolver.open_stale_page",
            stage="resolve",
            target_id=tid,
            payload={"form_url": form_url, "current_url": after_open_url},
            trace_dir=trace,
        )
        return {
            "vresult": None,
            "page_text": "",
            "loop_status": "open_failed_stale_page",
            "cur_url": after_open_url,
        }
    apply_cookie_dismiss(
        _evaluate, config, stage="send", target_id=tid,
        emit_event=lambda kind, **kw: _emit_event(kind, trace_dir=trace, **kw),
    )
    _arm_dialog_autoaccept()
    if d.get("entry_click_text"):
        for txt in (d["entry_click_text"] if isinstance(d["entry_click_text"], list) else [d["entry_click_text"]]):
            _click_button([re.escape(txt)])
            time.sleep(1.5)
    gate = _try_open_pre_form_gate()
    if gate and gate.get("clicked"):
        time.sleep(1.5)

    fill_form_for_target(d, config, sanitized_body, trace_dir=trace, iterative_fill=iterative_fill)
    time.sleep(1.0)

    # v24 §S3: the deep retry drives the SAME closed-loop state machine as the
    # main send path — re-running the old open-loop script here was why retries
    # deterministically hit the same wall. mode="retry" → no double journaling.
    inferred_flow = _infer_submit_flow_from_buttons(form_root_selector=d.get("form_root_selector"))
    if inferred_flow and inferred_flow != flow:
        flow = inferred_flow
    deep_timeline: list[dict[str, Any]] = []
    subres = _submission_loop(
        d, config, sanitized_body,
        flow=flow, mode="retry", trace=trace, tid=tid, timeline=deep_timeline,
    )
    if subres.get("status") == "click_failed" and int(subres.get("clicks") or 0) == 0:
        return {"vresult": None, "page_text": "", "loop_status": subres.get("status")}

    time.sleep(2)
    page_evidence = _evaluate(PAGE_EVIDENCE_JS)
    snap = oc_browser("snapshot")
    combined = _combine_page_evidence_text(
        snap, page_evidence if isinstance(page_evidence, dict) else None
    )
    vresult = verify_send_completed(
        d, "jp_form", snapshot=combined,
        browser_verify=page_evidence if isinstance(page_evidence, dict) else None,
        plan=d.get("_llm_plan"), evaluate_fn=_evaluate, data_dir=DATA_DIR,
        snapshot_path=None, verify_strict=verify_strict,
    )
    return {"vresult": vresult, "page_text": combined}


# v15 §S1 — multi-step wizard generalization (single/confirm = special cases)
MAX_FORM_STEPS = 4

_WIZARD_CLICK_PATTERNS = [
    r"入力内容を確認", r"送信内容を確認", r"内容(を|の)?確認", r"確認画面",
    r"確認する", r"確認$", r"同意して次へ", r"^次へ$", r"次のステップへ",
    r"^送信する$", r"^送信$", r"この内容で送信", r"内容を送信する",
    r"上記の内容で送信", r"submit", r"確定",
]


def _advance_wizard_steps(
    d: dict[str, Any],
    config: dict[str, Any],
    body: str,
    *,
    trace: Any,
    steps_done: int,
) -> dict[str, Any]:
    """Drive remaining wizard steps after the known single/confirm clicks (§S1).

    Loop (max MAX_FORM_STEPS total): success keyword / form visibly gone →
    stop (verify decides); otherwise re-fill required fields (existing fill +
    guardrails) and click a next/confirm/submit button. Returns
    {"too_deep": bool, "extra_steps": int} — too_deep means the form is STILL
    an input step after the cap, i.e. needs_attention (`wizard_too_deep`).
    """
    from _outreach_core.verify import FORM_VISIBILITY_JS, PAGE_EVIDENCE_JS

    def _still_input_step() -> bool:
        page = _evaluate(PAGE_EVIDENCE_JS)
        vis = _evaluate(FORM_VISIBILITY_JS)
        page_text = page.get("text", "") if isinstance(page, dict) else ""
        visible_textareas = (
            int(vis.get("visible_textareas") or 0) if isinstance(vis, dict) else 0
        )
        return core_submit_progress.wizard_should_continue(page_text, visible_textareas)

    extra = 0
    while steps_done + extra < MAX_FORM_STEPS:
        if not _still_input_step():
            return {"too_deep": False, "extra_steps": extra}
        print(f"  [send] wizard: still an input step after {steps_done + extra} "
              f"click(s) — fill + advance")
        _heuristic_fill_fallback(d, config, body, None)
        time.sleep(0.5)
        cr = _click_button_with_gate_retry(
            _WIZARD_CLICK_PATTERNS, form_root_selector=d.get("form_root_selector")
        )
        if not cr or not cr.get("clicked"):
            # Nothing clickable — let verify/resolver judge the page as-is.
            return {"too_deep": False, "extra_steps": extra, "stalled": True}
        extra += 1
        _emit_event(
            "send.wizard_step",
            stage="send",
            target_id=str(d.get("id") or d.get("name") or ""),
            payload={"step": steps_done + extra, "clicked": (cr.get("text") or "")[:60]},
            trace_dir=trace,
        )
        time.sleep(3)
    return {"too_deep": _still_input_step(), "extra_steps": extra}


# ============================================================================
# v24 §S3 — closed-loop submission driver (observe → classify → act → re-observe)
# ============================================================================
# Replaces the open-loop "click first → assume confirm page → click final"
# script. The live page is observed before EVERY action and the action is
# chosen from the observed state — never from the enrich-time flow guess.

def _arm_dialog_autoaccept() -> None:
    """Auto-accept native JS dialogs (confirm/alert) on the CURRENT page.

    CDP and Playwright both dismiss unhandled dialogs, so a submit button
    guarded by ``onclick="return confirm('送信しますか？')"`` silently cancels —
    the click reports success, the page never changes, and the target lands in
    needs_attention. Must be re-armed after every navigation."""
    try:
        _evaluate(core_send_state.DIALOG_AUTOACCEPT_JS)
    except Exception:  # noqa: BLE001 — arming must never break a send
        pass


def _read_dialog_log() -> list[dict[str, Any]]:
    try:
        res = _evaluate(core_send_state.READ_DIALOG_LOG_JS)
        return res if isinstance(res, list) else []
    except Exception:  # noqa: BLE001
        return []


def _observe_send_state(sender: dict[str, Any], body: str) -> dict[str, Any]:
    """One evaluate round-trip → classified live page state (see send_state)."""
    probes = core_send_state.build_probes(sender, body)
    js = core_send_state.evidence_js(probes)
    raw = _evaluate(js)
    if not isinstance(raw, dict):
        # Transient: the page may be mid-navigation when we probe. One retry —
        # a FAILED observation must not masquerade as state=no_form, which the
        # loop would escalate as lost_form (v25).
        time.sleep(2.5)
        raw = _evaluate(js)
    obs = core_send_state.classify_send_state(raw if isinstance(raw, dict) else {})
    if not isinstance(raw, dict):
        obs["observe_failed"] = True
    return obs


_FIRST_CLICK_PATTERNS = [
    r"入力内容を確認", r"送信内容を確認", r"内容(を|の)?確認",
    r"確認画面", r"確認する", r"確認$", r"内容の確認へ",
    r"同意して次へ", r"^次へ$", r"次のステップへ",
]
_FINAL_CLICK_PATTERNS = [
    r"^送信する$", r"^送信$", r"この内容で送信",
    r"内容を送信する", r"上記の内容で送信", r"^回答送信$",
    r"^送る$", r"submit", r"完了", r"確定",
    r"内容で.*送信", r"^以上の内容", r"内容を以上で",
    r"上記内容を送信", r"問い合わせを送信", r"お問い合わせを送信",
    r"送信する", r"内容を送信", r"同意して.*送信",
]


def _click_phase_submit(
    d: dict[str, Any],
    config: dict[str, Any],
    *,
    phase: str,
    trace: Any,
    tid: str,
    scope_to_form: bool = True,
    extra_patterns: list[str] | None = None,
) -> dict[str, Any]:
    """One full click cascade for the OBSERVED phase ('first' | 'final').

    Order: phase regexes → opposite-phase regexes (rescues single/confirm
    misclassification, e.g. flow=single but the page wants 「確認」) → LLM picker
    over enumerated buttons → page-instruction click → native submit.
    ``scope_to_form=False`` drops the (possibly stale) textarea-derived form
    root — required on confirm pages, which usually have no textarea.
    Returns {"clicked", "click_res", "noise_only", "native", "found_but_disabled"}.
    """
    primary = list(_FIRST_CLICK_PATTERNS if phase == "first" else _FINAL_CLICK_PATTERNS)
    alternate = list(_FINAL_CLICK_PATTERNS if phase == "first" else _FIRST_CLICK_PATTERNS)
    for p in extra_patterns or []:
        if p and p not in primary:
            primary.append(p)
    root = d.get("form_root_selector") if scope_to_form else None

    res = _click_button_with_gate_retry(primary, form_root_selector=root)
    if not (res and res.get("clicked")):
        alt = _click_button_with_gate_retry(alternate, form_root_selector=root)
        if alt and alt.get("clicked"):
            alt["phase_mismatch"] = True
            res = alt

    noise_only = False
    native: dict[str, Any] | None = None
    if not (res and res.get("clicked")):
        buttons = _enumerate_buttons(form_root_selector=root, filled_names=_filled_name_hints(d))
        if buttons:
            ranked = _phase_filter_submit_candidates(buttons, phase=phase)
            noise_only = len(ranked) == 0
            pick = _llm_click_submit_candidate(
                buttons, config or {}, form_root_selector=root, phase=phase,
            )
            if pick and pick.get("clicked"):
                res = pick
                picked = pick.get("picked") or {}
                _emit_event(
                    f"send.{'first' if phase == 'first' else 'final'}.llm_pick",
                    stage="send", target_id=tid,
                    payload={
                        "picked_text": (picked.get("text") or "")[:120],
                        "picked_selector": (picked.get("selector") or "")[:160],
                        "clicked": True,
                        "candidates": len(buttons),
                    },
                    trace_dir=trace,
                )
    if not (res and res.get("clicked")):
        instr = _click_submit_by_instruction(d, phase=phase, form_root_selector=root)
        if instr and instr.get("clicked"):
            res = instr
            _emit_event(
                f"send.{'first' if phase == 'first' else 'final'}.instruction_click",
                stage="send", target_id=tid,
                payload={"text": (instr.get("text") or "")[:120]}, trace_dir=trace,
            )
    if not (res and res.get("clicked")):
        native = _submit_native(d, form_root_selector=root)
        if native.get("clicked"):
            res = {
                "clicked": True,
                "text": f"[native:{native.get('method')}]",
                "native_submit_method": native.get("method"),
            }
    return {
        "clicked": bool(res and res.get("clicked")),
        "click_res": res,
        "noise_only": noise_only,
        "native": native,
        "found_but_disabled": bool(res and res.get("found_but_disabled")),
    }


def _submission_loop(
    d: dict[str, Any],
    config: dict[str, Any],
    send_body: str,
    *,
    flow: str,
    mode: str,
    trace: Any,
    tid: str,
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    """Drive the already-filled live form to completion as a state machine.

    Per round: arm dialog auto-accept → observe/classify the page → act on the
    OBSERVED state → detect whether anything actually changed. Terminal result::

        {"status": "done" | "click_failed" | "validation_stuck" | "ineffective"
                   | "lost_form" | "too_deep",
         "state": <last observed state>, "phase": <last click phase>,
         "clicks": n, "journal_attempted": bool, "obs": <last observation>,
         "dialogs": [...], "noise_only": bool, "native": {...}}

    The caller maps failure statuses onto the existing escalation taxonomy and
    ALWAYS runs verify after "done"/"too_deep" — the loop never declares a send
    successful by itself.
    """
    sender = (config or {}).get("sender") or {}
    plan = d.get("_llm_plan") or {}
    journal = {"attempted": False}

    def _journal_submit_attempt() -> None:
        if mode in ("auto", "interactive") and not journal["attempted"]:
            core_send_journal.append_journal(
                DATA_DIR, tid, core_send_journal.PHASE_SUBMIT_ATTEMPTED,
                form_url=d.get("form_url"),
            )
            journal["attempted"] = True

    def _result(status: str, obs: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
        return {
            "status": status,
            "obs": obs or {},
            "state": (obs or {}).get("state"),
            "clicks": clicks,
            "journal_attempted": journal["attempted"],
            "dialogs": _read_dialog_log()[:6],
            **extra,
        }

    last_fp: str | None = None
    no_progress = 0
    validation_rounds = 0
    clicks = 0
    confirm_seen = False
    confirm_gate_done = False
    input_rescue_done = False
    last_validation_errors: list[dict[str, str]] = []
    phase = "first" if flow == "confirm" else "final"
    # v30 §WS-A — runs alongside the legacy counters so the same-button-streak
    # gate (Fujisoft 「次へ」 ×3 / SUPER STUDIO 「内容確認へ」 ×3) has a chance to
    # fire before the per-iteration ineffective check. Legacy counters remain
    # the primary fingerprint/validation gates for now.
    wizard_state = core_wizard.WizardState()
    wizard_cfg = core_wizard.WizardConfig()

    for step in range(MAX_FORM_STEPS + 2):
        _arm_dialog_autoaccept()
        obs = _observe_send_state(sender, send_body)
        state = obs["state"]
        _emit_event(
            "send.state.observed", stage="send", target_id=tid,
            payload={
                "step": step, "state": state,
                "visible_textareas": obs.get("visible_textareas"),
                "editable_visible": obs.get("editable_visible"),
                "probe_text_hits": obs.get("probe_text_hits"),
                "probe_field_hits": obs.get("probe_field_hits"),
                "dialog_count": obs.get("dialog_count"),
                "send_verdict": obs.get("send_verdict"),
                "send_score": obs.get("send_score"),
                "send_reason": obs.get("send_reason"),
                "send_signals": obs.get("send_signals"),
            },
            trace_dir=trace,
        )
        core_timeline.add(
            timeline, "live_state", state not in ("no_form",),
            step=step, state=state,
        )
        # v30 §WS-A — pure state update + stuck check. Returns early ONLY for
        # the same-button-clicks gate, which the legacy counters did not
        # catch (Fujisoft 「次へ」×3 with fingerprint flicker). The other stuck
        # codes are also produced (REASON_NO_PROGRESS, REASON_MAX_HOPS,
        # REASON_VALIDATION_UNRECOVERABLE) but fall through to the existing
        # legacy gates so the behaviour for well-behaved forms is unchanged.
        core_wizard.bump_after_observation(
            wizard_state,
            observation_state=state,
            fingerprint=obs.get("fingerprint"),
        )
        # v30 §WS-E — persist a last-known-position snapshot so a crashed
        # process leaves a breadcrumb on disk: when the next run scans
        # data/briefs/<id>/runtime/ it can see "this target was at wizard
        # hop=N, observation_state=input, last_button=次へ when the previous
        # run died". send_journal still owns the safety-critical
        # double-send decision; this is purely diagnostic enrichment.
        try:
            from _outreach_core import events as _ev_mod  # local — avoid cycle
            core_target_state.merge_update(
                DATA_DIR, tid,
                run_id=_ev_mod.get_run_id(),
                name=str(d.get("name") or ""),
                form_url=str(d.get("form_url") or ""),
                phase="send.observed",
                hop=wizard_state.hop,
                observation_state=state,
                same_button_count=wizard_state.same_button_count,
                last_button=wizard_state.last_button_text or "",
            )
        except Exception:  # noqa: BLE001 - snapshot is best-effort
            pass
        wizard_stuck = core_wizard.compute_stuck_reason(wizard_state, wizard_cfg)
        if (
            wizard_stuck is not None
            and wizard_stuck.code == core_wizard.REASON_SAME_BUTTON
            and state not in ("done", "no_form")
        ):
            print(f"  [send] ⚠ wizard stuck: {wizard_stuck.detail}")
            _emit_event(
                "send.wizard.stuck", stage="send", target_id=tid,
                payload={
                    "reason": wizard_stuck.code,
                    "detail": wizard_stuck.detail,
                    "hop": wizard_state.hop,
                    "same_button_count": wizard_state.same_button_count,
                    "last_button_text": wizard_state.last_button_text,
                    "observation_state": state,
                },
                trace_dir=trace,
            )
            # v30 §WS-E — pin the stuck reason in the runtime snapshot so
            # the next run / report can show "this target hit
            # same_button_repeated" without re-reading events.jsonl.
            try:
                core_target_state.merge_update(
                    DATA_DIR, tid,
                    phase="send.wizard_stuck",
                    wizard_stuck=wizard_stuck.code,
                )
            except Exception:  # noqa: BLE001
                pass
            return _result(
                "ineffective", obs, phase=phase,
                wizard_stuck=wizard_stuck.as_payload(),
            )

        if state == "done":
            return _result("done", obs)
        if state == "no_form":
            # A click may have navigated to an interstitial; treat the FIRST
            # no_form sighting after a click as transient, the second as lost.
            if clicks > 0 and no_progress == 0:
                no_progress += 1
                time.sleep(3)
                continue
            return _result("lost_form", obs)

        fp = obs["fingerprint"]
        if last_fp is not None and fp == last_fp:
            no_progress += 1
            # Silent-bounce rescue (v26, wacoal class): the server re-rendered
            # the input page with inline errors our keywords don't recognize
            # (e.g. 「住所（番地）は全角で入力してください」), so state stays
            # "input" and the validation branch never fires. ONE harvest+fix
            # pass before declaring the click ineffective.
            if no_progress == 1 and state == "input" and not input_rescue_done:
                input_rescue_done = True
                rescue = _harvest_and_fix_validation_errors(
                    d, config, send_body, stage="send", trace_dir=trace,
                )
                native = _snapshot_native_validation(
                    trace_dir=trace, target_id=tid, phase="silent_bounce",
                )
                native_errors = _native_validation_errors(native)
                harvested_errors = [
                    {
                        "field": str(e.get("field") or "unknown")[:80],
                        "kind": str(e.get("kind") or "validation")[:80],
                        "message": str(e.get("message") or "")[:200],
                    }
                    for e in (rescue.get("errors") or []) if isinstance(e, dict)
                ]
                last_validation_errors = native_errors or harvested_errors
                # A server-side/custom validator can silently return to the
                # input page without error keywords. Treat that bounce as
                # evidence to retry newly revealed radio/select gates once.
                live_rescue = _auto_fill_live_gates(
                    phase="silent_bounce",
                    aggressive_radios=True,
                    aggressive_selects=True,
                    trace_dir=trace,
                    target_id=tid,
                )
                changed = int(rescue.get("fixed") or 0) + int(live_rescue.get("changed") or 0)
                if changed:
                    fixes = (
                        (rescue.get("zenkaku_fixed") or [])
                        + (rescue.get("phone_fixed") or [])
                    )
                    print(f"  [send] silent-bounce rescue: fixed={changed} "
                          f"{', '.join(fixes[:4])}")
                    core_timeline.add(
                        timeline, "input_rescue", True,
                        fixed=int(rescue.get("fixed") or 0),
                        gates=int(live_rescue.get("changed") or 0),
                        invalid_fields=len(native_errors),
                    )
            if no_progress >= 2:
                native = _snapshot_native_validation(
                    trace_dir=trace, target_id=tid, phase="stuck",
                )
                exact_errors = _native_validation_errors(native) or last_validation_errors
                if exact_errors:
                    return _result(
                        "validation_stuck", obs,
                        errors=exact_errors[:12],
                        native_validation=native,
                    )
                return _result("ineffective", obs, phase=phase)
        else:
            no_progress = 0
        last_fp = fp

        if state == "validation_error":
            validation_rounds += 1
            vfix = _harvest_and_fix_validation_errors(
                d, config, send_body, stage="send", trace_dir=trace,
            )
            native = _snapshot_native_validation(
                trace_dir=trace, target_id=tid, phase="validation_error",
            )
            native_errors = _native_validation_errors(native)
            errs = native_errors or (vfix.get("errors") or [])
            last_validation_errors = [
                {
                    "field": str(e.get("field") or "unknown")[:80],
                    "kind": str(e.get("kind") or "validation")[:80],
                    "message": str(e.get("message") or "")[:200],
                }
                for e in errs if isinstance(e, dict)
            ]
            if errs:
                err_fields = ", ".join(f"{e['field'][:16]}[{e['kind']}]" for e in errs[:4])
                print(f"  [send] ⚠ validation errors: {err_fields} → fixed={vfix.get('fixed')}")
            core_timeline.add(
                timeline, "validation_fix", bool(vfix.get("fixed")),
                round=validation_rounds,
                fixed=vfix.get("fixed"),
                errors=last_validation_errors[:8] or None,
            )
            # v30 next: re-analyze the form when validation_error suggests the
            # cached LLM plan was wrong. Two firing paths (both gated by
            # ``_plan_refreshed`` for one-shot idempotency):
            #
            #   * **Native validity** (valueMissing / patternMismatch / ...)
            #     fires on its FIRST appearance regardless of round number.
            #     Pilot 2026-06-29 (kagome) showed the native error surfacing
            #     only on round 2 because the form's first bounce contained
            #     only server-side text-extracted complaints — gating to
            #     round 1 missed it.
            #   * **Text-only required** that never escalates to native fires
            #     on round 2+ as the last-ditch attempt before
            #     ``validation_rounds > 2`` aborts the wizard. Pilot
            #     (sunstar) showed text-only "全角64文字以内で[required]"
            #     loops that never produce a native kind.
            should_refresh = not d.get("_plan_refreshed") and (
                _validation_errors_suggest_plan_refresh(errs)
                or (validation_rounds >= 2 and bool(errs))
            )
            if should_refresh:
                trigger_kind = (errs[0] or {}).get("kind") if errs else "n/a"
                _refresh_llm_plan_and_refill(
                    d, config, send_body,
                    trigger_reason=(
                        f"validation_round{validation_rounds}:{trigger_kind}"
                    ),
                    trace_dir=trace,
                )
            # Beyond kana/subject guardrails, retry the generic gate auto-fill —
            # dynamically-revealed required selects/radios live here. Radios run
            # AGGRESSIVE here (v25): the validation bounce itself is the evidence
            # that an unselected group is required, even without a DOM required
            # attribute or a known label (kakuyasu 「酒屋はありますか？」 class).
            live_rescue = _auto_fill_live_gates(
                phase="validation",
                aggressive_radios=True,
                aggressive_selects=True,
                trace_dir=trace,
                target_id=tid,
            )
            radio_rescue = live_rescue.get("radios") or {}
            if radio_rescue.get("selected_count"):
                print(f"  [send] validation radio rescue: "
                      f"{', '.join(radio_rescue.get('selected_items') or [])[:120]}")
            select_rescue = live_rescue.get("selects") or {}
            if select_rescue.get("selected_count"):
                print(f"  [send] validation select rescue: "
                      f"{', '.join(select_rescue.get('selected_items') or [])[:120]}")
            if validation_rounds > 2:
                return _result(
                    "validation_stuck", obs,
                    errors=last_validation_errors[:8],
                )
            phase = "first" if (flow == "confirm" and obs.get("visible_textareas")) else "final"
        elif state == "confirm":
            phase = "final"
            # Empty-echo guard (v25, baycrews class): a confirm page that echoes
            # NONE of our probe values means the form data did not survive the
            # POST (JS-state form, session loss). Clicking final here would send
            # an EMPTY inquiry — escalate instead.
            if (
                int(obs.get("probe_text_hits") or 0) == 0
                and int(obs.get("probe_field_hits") or 0) == 0
            ):
                return _result("confirm_empty_echo", obs)
            if not confirm_seen:
                confirm_seen = True
                _emit_event(
                    "send.confirm.reached", stage="send", target_id=tid,
                    payload={"wait_user_ms": 0, "observed": True}, trace_dir=trace,
                )
                core_timeline.add(timeline, "confirm_page", True)
            if not confirm_gate_done:
                confirm_gate_done = True
                gate_res = _auto_fill_live_gates(
                    phase="confirm",
                    trace_dir=trace,
                    target_id=tid,
                )
                llm_gate = {"checked": 0}
                remaining = _snapshot_submit_gates().get("remaining") or {}
                if int(remaining.get("total") or 0) > 0:
                    llm_gate = _post_form_llm_gate_action(
                        d, config, stage="send", trace_dir=trace,
                    )
                changed = int(gate_res.get("changed") or 0) + int(llm_gate.get("checked") or 0)
                if changed > 0:
                    print(f"  [send] confirm gate filled: {changed}")
                    core_timeline.add(timeline, "confirm_gate", True, changed=changed)
                    time.sleep(0.8)
                    continue
        else:  # input
            phase = "first" if flow == "confirm" else "final"

        # Journal before ANY click (§R3). The flow belief can be wrong: on a
        # misclassified single-flow form a phase="first" cascade falls through
        # to the final-pattern alternates / native submit and REALLY submits.
        # A false "attempted" (crash before a harmless first click) costs one
        # human check on resume; a missed journal risks a double-send.
        _journal_submit_attempt()

        # First action on the input page gets the plan-driven enable sequence
        # (route radios / consent gates / RESCAN steps) before the cascade.
        cl: dict[str, Any] | None = None
        if clicks == 0 and state == "input":
            drive = _drive_enable_sequence(
                d, plan, body=send_body, stage="send", trace_dir=trace, max_steps=4,
            )
            if drive.get("clicked"):
                cl = {
                    "clicked": True,
                    "click_res": {**(drive.get("click_res") or {}),
                                  "enable_steps": int(drive.get("steps") or 0)},
                    "noise_only": False, "native": None, "found_but_disabled": False,
                }
                _emit_event(
                    "send.enable_sequence.completed", stage="send", target_id=tid,
                    payload={"steps": int(drive.get("steps") or 0),
                             "applied": (drive.get("applied") or [])[:8]},
                    trace_dir=trace,
                )
            else:
                d["_drive_remaining"] = drive.get("remaining") or {}
        if cl is None:
            cl = _click_phase_submit(
                d, config, phase=phase, trace=trace, tid=tid,
                scope_to_form=(state != "confirm"),
                extra_patterns=(
                    [str(plan.get("first_button_pattern"))]
                    if phase == "first" and plan.get("first_button_pattern") else None
                ),
            )
        if not cl["clicked"]:
            return _result(
                "click_failed", obs, phase=phase,
                noise_only=cl["noise_only"], native=cl["native"],
                found_but_disabled=cl["found_but_disabled"],
                disabled_candidates=((cl.get("click_res") or {}).get("disabled_candidates") or [])[:3],
            )

        clicks += 1
        click_res = cl["click_res"] or {}
        btn_text = (click_res.get("text") or "")[:80]
        # v30 §WS-A — track which button we just clicked so a same-button
        # streak (3+ clicks of e.g. 「次へ」 without an observation-state
        # transition) can be detected on the next loop iteration.
        core_wizard.record_click(wizard_state, btn_text)
        print(f"  [send] clicked ({phase}): {btn_text}")
        core_timeline.add(
            timeline, "final_submit" if phase == "final" else "first_submit", True,
            button=btn_text[:40], native=click_res.get("native_submit_method"),
            state=state,
        )
        _emit_event(
            "send.final.clicked" if phase == "final" else "send.button.clicked",
            stage="send", target_id=tid,
            payload={
                "pattern_matched": not click_res.get("phase_mismatch", False),
                "text": btn_text,
                "observed_state": state,
                "radio_auto_selected": int(click_res.get("radio_auto_selected") or 0),
                "select_auto_selected": int(click_res.get("select_auto_selected") or 0),
                "native_submit_method": click_res.get("native_submit_method"),
            },
            trace_dir=trace,
        )
        time.sleep(5 if phase == "final" else 3)

    final_obs = _observe_send_state(sender, send_body)
    if final_obs["state"] == "done":
        return _result("done", final_obs)
    if final_obs["state"] in ("input", "validation_error"):
        return _result("too_deep", final_obs)
    # confirm/no_form after the cap — let verify judge the page as-is.
    return _result("done", final_obs, capped=True)


def _refresh_last_verify(d: dict[str, Any], vresult: dict[str, Any] | None) -> None:
    """v31 §WS8b — cache the LATEST verify verdict/status on the target.

    The per-target Slack ✅ line and _send_one_target's return payload read
    ``_last_verify_*``; before this helper only the first verify pass wrote
    them, so a URL-fallback / analyzer-escalation retry that succeeded still
    reported the first pass's failed status.
    """
    if not vresult:
        return
    verify_evidence = vresult.get("evidence") or {}
    d["_last_verify_verdict"] = (
        verify_evidence.get("send_verdict") or vresult.get("status")
    )
    d["_last_verify_status"] = vresult.get("status")


def _send_one_target(
    d: dict[str, Any],
    *,
    di: int,
    idx: int,
    mode: str,
    config: dict[str, Any],
    verify_strict: bool,
    iterative_fill: bool,
    autonomous: bool,
    score_on: bool,
    tab_isolation: bool,
    resolver_tab_ids: set[str],
    sent: list[dict[str, Any]],
    filled_only: list[dict[str, Any]],
) -> dict[str, Any]:
    """Send to a single target — extracted per-lead loop body (v15 §R1).

    Mutates ``sent`` / ``filled_only`` / ``resolver_tab_ids`` in place
    (mechanical extraction). Exceptions are isolated by the caller, so one
    company can no longer kill the whole batch.
    """
    name = d.get("name", "?")
    _hb_stage(f"send {name} (#{idx})")
    body = d["draft"]["body"]
    form_url = d["form_url"]
    flow = d.get("flow") or (d.get("_llm_plan") or {}).get("next_step") or "single"
    captcha = (d.get("form_fields") or {}).get("has_recaptcha_v2") or d.get("captcha") == "recaptcha_v2_visible"

    # v17: per-target stage timeline. Every checkpoint of the send process
    # (open → page state → gates → fill → validation → submit → verify) records
    # here so escalations report the FIRST failing stage, not the last symptom.
    timeline: list[dict[str, Any]] = []
    d["_send_timeline"] = timeline

    # Proactive URL-strip (§3.6): if this domain is known to reject URLs in the
    # body, send the URL-free variant from the start (no wasted first attempt).
    send_body = body
    url_stripped = False
    if core_avoidance.is_url_unfriendly(DATA_DIR, form_url) and core_content_guard.has_url(body):
        send_body, _diag = core_content_guard.sanitize_body(body, kind="url")
        url_stripped = True
        print(f"  [send] このドメインはURL不可既知 → 本文からURL除去して送信 "
              f"(removed {len(_diag.get('removed_urls') or [])})")

    print(f"\n=== [{idx}] {name} ===")
    print(f"  URL: {form_url}")
    print(f"  Flow: {flow}, captcha: {captcha and 'v2 (manual)' or 'none/v3'}, chars: {len(send_body)}"
          + (" [URL除去済]" if url_stripped else ""))

    tid = str(d.get("id") or name)
    from _outreach_core import events as ev

    if not ev.get_context().data_dir:
        ev.configure(skill="jp-form-outreach", data_dir=DATA_DIR)
    trace = ev.trace_dir_for(tid)

    # Autonomous self-score gate (the per-item replacement for human yes/no).
    # Runs BEFORE opening the browser so weak drafts cost no page load.
    # Quality is locked upfront; this is the secondary guard against a weak
    # draft slipping through. Below threshold → skip & log, no human ask.
    if autonomous and score_on and mode in ("auto", "interactive"):
        decision = core_autonomy.self_score_draft(d, config, oc_infer_fn=oc_infer)
        sc = decision.get("score")
        print(f"  [send] self-score: {sc if sc is not None else 'n/a'} → "
              f"{'send' if decision['send'] else 'SKIP'} ({decision['reason']})")
        _emit_event(
            "send.self_scored",
            stage="send",
            target_id=tid,
            payload={
                "score": sc,
                "send": decision["send"],
                "errored": decision.get("errored", False),
                "reason": (decision.get("reason") or "")[:160],
            },
            trace_dir=trace,
        )
        if not decision["send"]:
            _auto_skip_and_log(d, f"self_score_below_threshold: {decision['reason']}")
            return {"outcome": "skipped"}

    # Avoidance learning: if this domain has repeatedly shown a *visible*
    # captcha challenge even after warmup, stop wasting attempts on it.
    dstatus = core_avoidance.domain_status(DATA_DIR, form_url, config)
    if dstatus["unviable"]:
        print(f"  [send] ⏭ domain captcha-unviable "
              f"({dstatus['captcha_blocks']}/{dstatus['attempts']} blocked) → route/skip")
        _emit_event(
            "send.domain_unviable", stage="send", target_id=tid,
            payload=dstatus, trace_dir=trace,
        )
        core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_SKIPPED)
        if autonomous:
            _auto_skip_and_log(
                d, f"domain_captcha_unviable: {dstatus['captcha_blocks']}/{dstatus['attempts']} blocked"
            )
        else:
            _handle_blocker(d, "domain repeatedly captcha-blocked", autonomous=False)
        return {"outcome": "skipped"}

    # 0. reCAPTCHA v3 warm-up (§11-A-8 + §3.5 avoidance): visit root domain +
    #    dispatch natural interactions before navigating to the form. Warmup
    #    duration is ADAPTIVE — bumped for domains previously challenged.
    from _outreach_core.warmup import apply_warmup_if_enabled

    warm_sec = core_avoidance.recommended_warmup_sec(DATA_DIR, form_url, config)
    # Cloudflare-gated domains (learned): force a genuine root-domain warmup
    # dwell so the persistent profile carries its own cf_clearance — reduces the
    # chance Turnstile re-triggers, without solving/bypassing anything.
    if core_avoidance.is_cloudflare_domain(DATA_DIR, form_url):
        warm_config = _config_force_cf_warmup(config, warm_sec)
        print(f"  [send] cf-gated domain既知 → root warmup を強制 ({max(20, warm_sec)}s, cf_clearance自然取得)")
    else:
        warm_config = _config_with_warmup_sec(config, warm_sec)
    warmup_diag = apply_warmup_if_enabled(
        form_url=form_url,
        config=warm_config,
        oc_browser_fn=oc_browser,
        evaluate_fn=_evaluate,
        emit_event=_emit_event,
        stage="send",
        target_id=tid,
    )
    if not warmup_diag.get("skipped"):
        print(
            f"  [send] reCAPTCHA v3 warmup: "
            f"{warmup_diag.get('elapsed_sec')}s on {warmup_diag.get('seed_url')}"
        )

    # 1. Open form — in its own tracked tab (§17). Keep total tabs bounded;
    #    error tabs (resolver-bound) are protected from the cap.
    t0 = time.time()
    cur_tab_id: str | None = None
    if tab_isolation:
        _enforce_tab_cap(protect=resolver_tab_ids)
        cur_tab_id = _open_tab(form_url)
        if cur_tab_id:
            _focus_tab(cur_tab_id)
    if not cur_tab_id:
        oc_browser("open", form_url)  # fallback (also opens a tab)
    d["_send_tab_id"] = cur_tab_id  # crash-path cleanup handle (v15 §R1)
    time.sleep(RATE_LIMIT_SECONDS)
    _emit_event(
        "send.opened",
        stage="send",
        target_id=tid,
        payload={"url": form_url, "tab_id": cur_tab_id,
                 "time_ms": int((time.time() - t0) * 1000)},
        trace_dir=trace,
    )
    from _outreach_core.cookie_dismiss import apply_cookie_dismiss

    apply_cookie_dismiss(
        _evaluate,
        config,
        stage="send",
        target_id=tid,
        emit_event=lambda kind, **kw: _emit_event(kind, trace_dir=trace, **kw),
    )
    # v24: accept native confirm()/alert() from the very first interaction —
    # entry clicks and pre-form gates can also be dialog-guarded.
    _arm_dialog_autoaccept()

    pre_snap = oc_browser("snapshot")
    ev.dump_trace(trace, "form_snapshot_pre.txt", pre_snap or "")

    # v17: record where we actually landed — redirects to a different page
    # (e.g. ain_holdings → アイン薬局 site) are a URL problem, not a form problem.
    final_url = str(_evaluate("() => location.href") or "")
    redirected = bool(
        final_url
        and core_contact_url._normalize_http_url(final_url)
        != core_contact_url._normalize_http_url(form_url)
    )
    core_timeline.add(
        timeline, "open", True,
        url=form_url,
        final_url=(final_url if redirected else None),
        redirected=(redirected or None),
    )

    # 1b. Early Cloudflare / bot-detection check (v18). A full-page Turnstile
    # "Verify you are human" interstitial gates the real form and has zero form
    # controls — without this it would be misread as page_has_no_form. We do NOT
    # solve it: a MANAGED challenge often auto-clears on a real browser profile,
    # so we wait once; if it's still blocking we learn the domain (so next time
    # gets a genuine root-warmup dwell) and route to human relay / skip.
    cap_landing = _live_captcha_state()
    if cap_landing.get("cloudflare"):
        core_avoidance.mark_cloudflare(DATA_DIR, form_url)
    if cap_landing.get("blocking"):
        # Managed interstitials clear themselves on a trusted session — give it
        # one genuine wait (no interaction injection), then re-check.
        if cap_landing.get("kind") == "turnstile_interstitial":
            print("  [send] Cloudflare managed challenge検出 → 自動クリア待機（最大12s）")
            for _ in range(4):
                time.sleep(3)
                cap_landing = _live_captcha_state()
                if not cap_landing.get("blocking"):
                    print("  [send] ✓ challenge自動クリア → 続行")
                    break
    if cap_landing.get("blocking"):
        label = core_captcha.reason_label(cap_landing)
        core_timeline.add(timeline, "captcha", False, kind=cap_landing.get("kind"))
        print(f"  [send] ⚠ {label} — 突破せず記録/迂回（人手 or skip）")
        core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_CAPTCHA_BLOCKED)
        filled_only.append(d)
        if cur_tab_id:
            resolver_tab_ids.add(cur_tab_id)
        if autonomous:
            _auto_skip_and_log(d, f"cloudflare_blocking: {cap_landing.get('kind')} ({label})")
        else:
            _queue_for_resolver(
                d, "cloudflare_challenge",
                f"Cloudflare bot検知をブロッキングで検出 ({cap_landing.get('kind')}) — "
                f"突破不可。手動でチャレンジ通過後に送信が必要",
                trace=trace, autonomous=False, tab_id=cur_tab_id,
            )
        return {"outcome": "skipped", "reason": "cloudflare_blocking"}

    # 2. Click any pre-form entry (e.g. "法人のお客様" tab)
    if d.get("entry_click_text"):
        for txt in (d["entry_click_text"] if isinstance(d["entry_click_text"], list) else [d["entry_click_text"]]):
            cr = _click_button([re.escape(txt)])
            _emit_event(
                "send.entry_clicked",
                stage="send",
                target_id=tid,
                payload={"text": txt, "success": bool(cr and cr.get("clicked"))},
                trace_dir=trace,
            )
            time.sleep(1.5)
    gate = _try_open_pre_form_gate()
    if gate and gate.get("clicked"):
        _emit_event(
            "send.pre_form_gate_clicked",
            stage="send",
            target_id=tid,
            payload={
                "patterns": _PRE_FORM_ENTRY_PATTERNS,
                "checked_boxes": gate.get("checked_boxes", 0),
                "radio_auto_selected": gate.get("radio_auto_selected", 0),
                "select_auto_selected": gate.get("select_auto_selected", 0),
            },
            trace_dir=trace,
        )
        time.sleep(1.5)
    if d.get("entry_click_text") or (gate and gate.get("clicked")):
        core_timeline.add(
            timeline, "entry_click", None,
            entry=d.get("entry_click_text"),
            gate_clicked=bool(gate and gate.get("clicked")),
        )

    pre_adv = _advance_pre_form_phase(
        d, config, stage="pre_form", trace_dir=trace, max_rounds=3,
    )
    if pre_adv.get("advanced"):
        _emit_event(
            "send.pre_form_advanced",
            stage="send",
            target_id=tid,
            payload={
                "state": pre_adv.get("state"),
                "rounds": (pre_adv.get("rounds") or [])[:4],
            },
            trace_dir=trace,
        )
        core_timeline.add(
            timeline, "pre_form_advanced", True,
            state=pre_adv.get("state"),
        )
        time.sleep(1.0)

    # v17 URL精査: verify the page actually carries a form BEFORE filling.
    # Catches redirects, contact GUIDE pages, expired sessions — the production
    # cases that used to surface later as "送信ボタンが見つからない（候補0）".
    page_ok, page_state = _assess_page_and_recover(d, timeline, trace)
    if not page_ok and page_state.get("state") == "otp_gate":
        # v25: メール確認コード方式は人手でも回避不能（メール受信が必要）。
        # リゾルバ再試行は無駄なので即スキップ＋manual 判定で記録する。
        print("  [send] ⚠ OTP_GATE — メール確認コード方式のため自動送信不可 → skip (manual対応)")
        d["status"] = "manual"
        d["blocker"] = "email_verification_code"
        _auto_skip_and_log(d, "email_verification_code: メール確認コード方式のため自動送信不可")
        return {"outcome": "skipped", "reason": "otp_gate"}
    if not page_ok:
        print(f"  [send] ⚠ PAGE_HAS_NO_FORM — state={page_state.get('state')} "
              f"(inputs={page_state.get('inputs')}, buttons={page_state.get('submit_buttons')})")
        filled_only.append(d)
        core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_WRONG_FORM)
        if cur_tab_id:
            resolver_tab_ids.add(cur_tab_id)
        _queue_for_resolver(
            d, "page_has_no_form",
            (
                f"フォーム要素がページに存在しません (state={page_state.get('state')}, "
                f"inputs={page_state.get('inputs')}, textareas={page_state.get('textareas')}, "
                f"buttons={page_state.get('submit_buttons')}"
                + (f", 最終URL={final_url}" if redirected else "")
                + ") — URLの再精査が必要です"
            ),
            trace=trace, autonomous=autonomous, tab_id=cur_tab_id,
        )
        return {"outcome": "queued", "reason": "page_has_no_form"}

    # 3. Fill all fields
    diagnostics = fill_form_for_target(
        d, config, send_body, trace_dir=trace, iterative_fill=iterative_fill
    )
    core_timeline.add(
        timeline, "fill", not diagnostics.get("errors"),
        filled=len(diagnostics.get("filled") or []),
        unfilled=len(diagnostics.get("unfilled") or []),
        errors=(diagnostics.get("errors") or [])[:3] or None,
    )
    print(f"  [send] filled: {len(diagnostics['filled'])} / unfilled: {len(diagnostics['unfilled'])} / errors: {len(diagnostics['errors'])}")
    for f in diagnostics["filled"][:8]:
        print(f"    ✓ {f}")
    if diagnostics["errors"]:
        for e in diagnostics["errors"]:
            print(f"    ✗ {e}")

    empty_submission_risk = _detect_empty_submission_risk(diagnostics)
    if empty_submission_risk:
        print(f"  [send] ⚠ EMPTY_SUBMISSION_RISK — {empty_submission_risk}")
        filled_only.append(d)
        core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_WRONG_FORM)
        if cur_tab_id:
            resolver_tab_ids.add(cur_tab_id)
        _queue_for_resolver(
            d,
            "body_not_filled",
            empty_submission_risk,
            trace=trace,
            autonomous=autonomous,
            tab_id=cur_tab_id,
        )
        return {"outcome": "queued", "reason": "body_not_filled"}

    wrong_form_reason = _detect_wrong_form_type(diagnostics)
    if wrong_form_reason:
        print(f"  [send] ⚠ WRONG_FORM_TYPE detected — {wrong_form_reason}")
        print(f"          aborting submit; browser left open for manual review")
        filled_only.append(d)
        _emit_event(
            "send.wrong_form_type",
            stage="send",
            target_id=tid,
            payload={"reason": wrong_form_reason},
            trace_dir=trace,
        )
        core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_WRONG_FORM)
        if cur_tab_id:
            resolver_tab_ids.add(cur_tab_id)
        _queue_for_resolver(
            d, "wrong_form_type",
            f"WRONG_FORM_TYPE_DETECTED — {wrong_form_reason}",
            trace=trace, autonomous=autonomous, tab_id=cur_tab_id,
        )
        return {"outcome": "skipped"}

    # Live captcha re-check (§3.5): the enrich-time flag only says a widget
    # EXISTS; here we verify whether a challenge is actually VISIBLE/blocking
    # right now. This is the fix for the production false positives — a missing
    # submit button or non-blocking v3/checkbox must NOT be reported as
    # "reCAPTCHA". Presence ≠ blocking.
    cap_state = _live_captcha_state()
    _emit_event(
        "send.captcha_check", stage="send", target_id=tid,
        payload={
            "enrich_flag": bool(captcha),
            "live_kind": cap_state["kind"],
            "blocking": cap_state["blocking"],
            "requires_human": cap_state.get("requires_human", False),
            "response_token_present": cap_state.get("response_token_present", False),
            "counts": cap_state.get("counts", {}),
        },
        trace_dir=trace,
    )
    captcha_deferred = core_captcha.should_defer_submit(cap_state)
    if captcha and not captcha_deferred:
        print(f"  [send] captcha 再確認: enrich={captcha} だが live={cap_state['kind']} "
              f"（非ブロッキング）→ 続行")
    core_timeline.add(
        timeline, "captcha", (not captcha_deferred),
        kind=cap_state.get("kind"), blocking=cap_state["blocking"],
        requires_human=cap_state.get("requires_human"),
    )
    if cap_state.get("cloudflare"):
        core_avoidance.mark_cloudflare(DATA_DIR, form_url)
    if captcha_deferred:
        label = core_captcha.reason_label(cap_state)
        reason_kind = "captcha_blocking" if cap_state.get("blocking") else "captcha_human_required"
        print(f"  [send] ⚠ {label} — response token未取得のため送信せず退避")
        core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_CAPTCHA_BLOCKED)
        filled_only.append(d)
        if autonomous:
            _auto_skip_and_log(d, f"{reason_kind}: {cap_state['kind']} ({label})")
        else:
            _escalate_await_proceed(d, f"captcha human action required: {cap_state['kind']}")
        return {"outcome": "skipped", "reason": reason_kind}

    if mode == "fill-only":
        print(f"  [send] ✓ filled. Click 確認/送信 manually.")
        filled_only.append(d)
        return {"outcome": "skipped"}

    # 4-5. Closed-loop submission (v24 §S3). The legacy linear script (click
    # first → assume confirm page reached → click final → wizard catch-up) is
    # replaced by a state machine that observes the LIVE page before every
    # action and picks the action from the observed state — see _submission_loop.
    time.sleep(1.0)
    plan = d.get("_llm_plan") or {}
    plan_flow = plan.get("next_step")
    if plan_flow and plan_flow != flow:
        flow = plan_flow
    inferred_flow = _infer_submit_flow_from_buttons(form_root_selector=d.get("form_root_selector"))
    if inferred_flow and inferred_flow != flow:
        flow = inferred_flow

    subres = _submission_loop(
        d, config, send_body,
        flow=flow, mode=mode, trace=trace, tid=tid, timeline=timeline,
    )
    journal_attempted = bool(subres.get("journal_attempted"))
    status = str(subres.get("status") or "")
    sub_state = str(subres.get("state") or "")
    sub_obs = subres.get("obs") or {}

    def _close_journal(outcome: str) -> None:
        if journal_attempted:
            core_send_journal.append_journal(
                DATA_DIR, tid, core_send_journal.PHASE_VERIFIED, outcome=outcome
            )

    if status != "done":
        filled_only.append(d)
        if cur_tab_id:
            resolver_tab_ids.add(cur_tab_id)

        if status == "click_failed" and sub_state == "confirm":
            if trace:
                ev.dump_trace(trace, "form_snapshot_confirm.txt", oc_browser("snapshot") or "")
            print("  [send] ⚠ confirm page reached but final submit not clickable")
            core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_SUBMIT_NOT_FOUND)
            _queue_for_resolver(
                d, "confirm_submit_not_found",
                (
                    "confirm-page final submit not found; "
                    f"noise_only_candidates={bool(subres.get('noise_only'))}; "
                    f"{_native_submit_diag(subres.get('native'))}"
                ),
                trace=trace, autonomous=autonomous, tab_id=cur_tab_id,
            )
            _close_journal("no_click" if int(subres.get("clicks") or 0) == 0 else "not_confirmed")
            return {"outcome": "skipped"}

        if status == "click_failed":
            print(f"  [send] ⚠ submit button not found (observed state={sub_state})")
            if subres.get("found_but_disabled"):
                print(f"  [send]    ↳ matched but disabled: {subres.get('disabled_candidates') or []}")
            if subres.get("native"):
                print(f"  [send]    ↳ {_native_submit_diag(subres.get('native'))}")
            remaining = d.get("_drive_remaining") or {}
            core_timeline.add(
                timeline, "first_submit", False,
                flow=flow, page_state_now=sub_state,
                found_but_disabled=bool(subres.get("found_but_disabled")) or None,
            )
            _emit_event(
                "send.first_button_missing", stage="send", target_id=tid,
                payload={
                    "flow": flow,
                    "observed_state": sub_state,
                    "found_but_disabled": bool(subres.get("found_but_disabled")),
                    "native_submit_method": (subres.get("native") or {}).get("method"),
                    "native_submit_reason": (subres.get("native") or {}).get("reason"),
                    "noise_only_candidates": bool(subres.get("noise_only")),
                    "remaining_gate_total": int(remaining.get("total") or 0),
                },
                trace_dir=trace,
            )
            core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_SUBMIT_NOT_FOUND)
            _queue_for_resolver(
                d,
                (
                    "submit_gate_unsatisfied" if int(remaining.get("total") or 0) > 0
                    else "first_submit_not_found"
                ),
                (
                    f"submit button not found (flow={flow}, observed_state={sub_state}); "
                    f"noise_only_candidates={bool(subres.get('noise_only'))}; "
                    f"remaining_gates={json.dumps(remaining, ensure_ascii=False)[:240]}; "
                    f"{_native_submit_diag(subres.get('native'))}"
                ),
                trace=trace, autonomous=autonomous, tab_id=cur_tab_id,
            )
            _close_journal("no_click" if int(subres.get("clicks") or 0) == 0 else "not_confirmed")
            return {"outcome": "skipped"}

        if status == "validation_stuck":
            field_parts: list[str] = []
            for e in (subres.get("errors") or [])[:5]:
                message = str(e.get("message") or "").strip()
                part = f"{e.get('field')}[{e.get('kind')}]"
                if message:
                    part += f": {message[:100]}"
                field_parts.append(part)
            fields = ", ".join(field_parts) or "項目を特定できませんでした"
            print(f"  [send] ⚠ validation errors persist after auto-fix — {fields}")
            core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_WRONG_FORM)
            _queue_for_resolver(
                d, "validation_unrecoverable",
                f"想定外の必須項目/バリデーションを自動修復できません: {fields}",
                trace=trace, autonomous=autonomous, tab_id=cur_tab_id,
            )
            _close_journal("validation_bounced")
            return {"outcome": "skipped"}

        if status == "ineffective":
            dialogs = subres.get("dialogs") or []
            dlg_note = (
                "; dialogs=" + json.dumps(dialogs[:3], ensure_ascii=False)
                if dialogs else ""
            )
            print("  [send] ⚠ clicks register but the page never changes — escalating")
            core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_SUBMIT_NOT_FOUND)
            _queue_for_resolver(
                d, "submit_click_ineffective",
                (
                    f"クリックは成立するがページが遷移しません (observed_state={sub_state})"
                    f"{dlg_note}"
                ),
                trace=trace, autonomous=autonomous, tab_id=cur_tab_id,
            )
            _close_journal("not_confirmed")
            return {"outcome": "skipped"}

        if status == "lost_form":
            print("  [send] ⚠ form disappeared mid-flight (session expiry / redirect)")
            core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_WRONG_FORM)
            _queue_for_resolver(
                d, "form_vanished_after_fill",
                (
                    f"フォームが送信途中で消失しました (clicks={subres.get('clicks')}, "
                    f"url={sub_obs.get('url', '')})"
                ),
                trace=trace, autonomous=autonomous, tab_id=cur_tab_id,
            )
            _close_journal("no_click" if int(subres.get("clicks") or 0) == 0 else "not_confirmed")
            return {"outcome": "skipped"}

        if status == "confirm_empty_echo":
            print("  [send] ⚠ confirm page reached but our values are NOT echoed "
                  "— possible empty submission, escalating (no final click)")
            core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_WRONG_FORM)
            _queue_for_resolver(
                d, "confirm_empty_echo",
                (
                    "確認ページに入力値が反映されていません（空送信の恐れがあるため"
                    f"最終送信せず退避; url={sub_obs.get('url', '')}）"
                ),
                trace=trace, autonomous=autonomous, tab_id=cur_tab_id,
            )
            _close_journal("not_confirmed")
            return {"outcome": "skipped"}

        # too_deep — still an input step after the click cap (§S1 successor).
        print(f"  [send] ⚠ wizard too deep (>{MAX_FORM_STEPS} steps) — needs attention")
        _emit_event(
            "send.wizard_too_deep", stage="send", target_id=tid,
            payload={"max_steps": MAX_FORM_STEPS, "clicks": int(subres.get("clicks") or 0)},
            trace_dir=trace,
        )
        _queue_for_resolver(
            d, "wizard_too_deep",
            f"multi-step form exceeded {MAX_FORM_STEPS} steps",
            trace=trace, autonomous=autonomous, tab_id=cur_tab_id,
        )
        _close_journal("wizard_too_deep")
        return {"outcome": "skipped"}

    # 6. Verify send (keywords, required fields, plan gaps)
    time.sleep(2)
    ev.dump_trace(trace, "form_snapshot_post.txt", oc_browser("snapshot") or "")
    from _outreach_core.verify import PAGE_EVIDENCE_JS

    page_evidence = _evaluate(PAGE_EVIDENCE_JS)
    snap = oc_browser("snapshot")
    snap_path = DATA_DIR / f"verify_snapshot_{d.get('id', di)}.txt"
    combined = _combine_page_evidence_text(
        snap, page_evidence if isinstance(page_evidence, dict) else None
    )
    if combined.strip():
        snap_path.write_text(combined, encoding="utf-8")

    # v15 §V1: ALWAYS persist post-submit evidence (URL+title+text head) so a
    # sent_ok can be audited later for false positives — not only on failure.
    if isinstance(page_evidence, dict):
        ev.dump_trace(
            trace, "post_submit_evidence.txt",
            f"url: {page_evidence.get('url', '')}\n"
            f"title: {page_evidence.get('title', '')}\n\n"
            f"cf7_sent: {page_evidence.get('cf7_sent', '')}\n"
            f"cf7_invalid: {page_evidence.get('cf7_invalid', '')}\n"
            f"cf7_statuses: {page_evidence.get('cf7_statuses', '')}\n"
            f"cf7_response_text: {page_evidence.get('cf7_response_text', '')}\n\n"
            f"submission_sent: {page_evidence.get('submission_sent', '')}\n"
            f"submission_invalid: {page_evidence.get('submission_invalid', '')}\n"
            f"submission_statuses: {page_evidence.get('submission_statuses', '')}\n"
            f"submission_status_text: {page_evidence.get('submission_status_text', '')}\n\n"
            f"{(page_evidence.get('text') or '')[:4000]}",
        )

    if mode in ("auto", "interactive"):
        if d.get("_llm_plan"):
            ev.dump_trace(trace, "fill_plan.json", d["_llm_plan"], sender=config.get("sender"))
        vresult = verify_send_completed(
            d,
            "jp_form",
            snapshot=combined,
            browser_verify=page_evidence if isinstance(page_evidence, dict) else None,
            plan=d.get("_llm_plan"),
            evaluate_fn=_evaluate,
            data_dir=DATA_DIR,
            snapshot_path=snap_path if combined.strip() else None,
            verify_strict=verify_strict,
            # v15 §V2 / v30 §WS-F: LLM tiebreak ONLY for the uncertain middle
            # band. The verify model is read from a dedicated ``model.verify_name``
            # config key so the form analyzer's potentially-Opus escalation does
            # not bleed into the verify path.
            infer_fn=lambda p, m: oc_infer(p, m or core_infer.DEFAULT_MODEL),
            tiebreak_model=_verify_model(config),
        )
        _refresh_last_verify(d, vresult)
        ev.dump_trace(trace, "verify_evidence.json", vresult.get("evidence") or {})
        outcome = handle_verify_result(d, vresult, DATA_DIR, channel="jp_form")
        core_timeline.add(
            timeline, "verify", (outcome == "sent_ok"),
            status=vresult.get("status"),
            reason=(vresult.get("reason") or "")[:80],
        )
        ev.dump_trace(trace, "send_timeline.json", timeline)
        print("  [send] プロセスログ:\n" + core_timeline.format_timeline(timeline))
        if outcome == "sent_ok":
            print(f"  [send] ✅ {vresult.get('reason')}")
            core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_SENT)
            if tab_isolation:
                _close_tab(cur_tab_id)  # success → close the tab
            sent.append(d)
        else:
            # Content-rejection fallback (§3.6): the form rejected the body for
            # disallowed characters / a URL. Retry ONCE with the URL removed
            # ("URLは送らないルート"). Learn the domain so next time we pre-strip.
            rej = core_content_guard.detect_content_rejection(combined)
            if (rej and not url_stripped and core_content_guard.has_url(send_body)
                    and mode in ("auto", "interactive")):
                print(f"  [send] ⚠ コンテンツ拒否を検知 "
                      f"({rej['kind']}: {rej['evidence']}) → URL除去して再送")
                core_avoidance.mark_url_unfriendly(DATA_DIR, form_url)
                core_avoidance.record_outcome(
                    DATA_DIR, form_url, core_avoidance.OUTCOME_CONTENT_REJECTED)
                sanitized, sdiag = core_content_guard.sanitize_body(send_body, kind=rej["kind"])
                _emit_event(
                    "send.content_rejected", stage="send", target_id=tid,
                    payload={
                        "kind": rej["kind"],
                        "evidence": rej["evidence"],
                        "removed_urls": sdiag.get("removed_urls"),
                    },
                    trace_dir=trace,
                )
                retry = _deep_submit(
                    d, sanitized, config, trace=trace, flow=flow,
                    verify_strict=verify_strict, iterative_fill=iterative_fill,
                )
                # v31 §WS8b — refresh the cached verdict from the retry so the
                # per-target ✅ line / return payload reflect the attempt that
                # actually settled the target, not the first failed pass.
                _refresh_last_verify(d, retry.get("vresult"))
                outcome2 = (
                    handle_verify_result(d, retry["vresult"], DATA_DIR, channel="jp_form")
                    if retry.get("vresult") else "failed"
                )
                if outcome2 == "sent_ok":
                    print(f"  [send] ✅ URL除去で再送成功")
                    core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_SENT)
                    if tab_isolation:
                        _close_tab(cur_tab_id)
                    _emit_event("send.url_fallback_ok", stage="send", target_id=tid,
                                payload={"removed_urls": sdiag.get("removed_urls")}, trace_dir=trace)
                    sent.append(d)
                else:
                    print(f"  [send] ⚠ URL除去再送も不成立 — filled_only")
                    filled_only.append(d)
            else:
                escalated_ok = False
                if mode in ("auto", "interactive") and _has_form_analyzer_escalation(config):
                    escalated_model = _form_analyzer_escalation_model(config)
                    cur_model = str(
                        (d.get("_llm_plan_meta") or {}).get("model")
                        or _form_analyzer_base_model(config)
                    )
                    if escalated_model and cur_model != escalated_model:
                        print(
                            f"  [send] verify failed → form analyzer escalation "
                            f"{cur_model} -> {escalated_model}"
                        )
                        _emit_event(
                            "send.verify.escalation_retry",
                            stage="send",
                            target_id=tid,
                            payload={
                                "from_model": cur_model,
                                "to_model": escalated_model,
                                "verify_status": vresult.get("status"),
                                "verify_reason": (vresult.get("reason") or "")[:160],
                            },
                            trace_dir=trace,
                        )
                        d.pop("_llm_plan", None)
                        d.pop("_llm_plan_meta", None)
                        esc_cfg = _config_with_form_analyzer_model(config, escalated_model)
                        retry = _deep_submit(
                            d, send_body, esc_cfg, trace=trace, flow=flow,
                            verify_strict=verify_strict, iterative_fill=True,
                        )
                        # v31 §WS8b — same stale-verdict fix as the URL-fallback
                        # retry above.
                        _refresh_last_verify(d, retry.get("vresult"))
                        outcome2 = (
                            handle_verify_result(d, retry["vresult"], DATA_DIR, channel="jp_form")
                            if retry.get("vresult") else "failed"
                        )
                        if outcome2 == "sent_ok":
                            print(f"  [send] ✅ escalated analyzer retry succeeded")
                            core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_SENT)
                            if tab_isolation:
                                _close_tab(cur_tab_id)
                            _emit_event(
                                "send.verify.escalation_ok",
                                stage="send",
                                target_id=tid,
                                payload={"model": escalated_model},
                                trace_dir=trace,
                            )
                            sent.append(d)
                            escalated_ok = True
                        else:
                            _emit_event(
                                "send.verify.escalation_failed",
                                stage="send",
                                target_id=tid,
                                payload={"model": escalated_model},
                                trace_dir=trace,
                            )
                if not escalated_ok:
                    print(f"  [send] ⚠ verify: {vresult.get('status')} — {vresult.get('reason')}")
                    filled_only.append(d)
    else:
        filled_only.append(d)
    # v15 §R3: close the journal — verify has settled (any outcome). A target
    # whose journal stays open (submit_attempted only) is treated as a possible
    # double-send on the next run and routed to needs_attention.
    if journal_attempted:
        final_outcome = "sent_ok" if any(x is d for x in sent) else "not_confirmed"
        core_send_journal.append_journal(
            DATA_DIR, tid, core_send_journal.PHASE_VERIFIED, outcome=final_outcome
        )
    # v30 §WS-E — clear the runtime snapshot once a verdict settles. The
    # send_journal still preserves the safety-critical lifecycle history,
    # but the per-target last_state.json is purely "what was the target
    # doing right now" and should not linger across runs once we have a
    # final answer.
    try:
        core_target_state.clear_state(DATA_DIR, tid)
    except Exception:  # noqa: BLE001
        pass
    # v30 §WS-D — concise per-target verdict for the Slack thread. ``sent``
    # ones land green, anything else (filled_only, not_confirmed) lands as
    # filled_only so the operator knows the verify step did not certify it.
    was_sent = any(x is d for x in sent)
    try:
        from _outreach_core import notify as _notify
        # v31 §WS8e — idx/total come from the target's _batch_idx/_batch_total
        # fallback inside post_target_event so [i/N] finally renders in
        # production (the old idx=sendable-index / total=None pair never did).
        _notify.post_target_event(
            stage="send",
            status="sent" if was_sent else "filled_only",
            target=d,
            detail={
                "verify_status": d.get("_last_verify_status"),
                "verify_reason": (
                    (d.get("_last_verify_verdict") or "")
                    if was_sent else None
                ),
                "form_url": d.get("form_url"),
            },
        )
    except Exception:  # noqa: BLE001 - Slack must never abort the loop
        pass
    return {
        "outcome": "sent" if was_sent else "done",
        "verify_verdict": d.get("_last_verify_verdict"),
        "verify_status": d.get("_last_verify_status"),
    }



def stage_send(
    input_path: Path,
    ids: set[int],
    mode: str = "interactive",
    config: dict[str, Any] | None = None,
    heartbeat: str | None = None,
    verify_strict: bool = True,
    iterative_fill: bool = False,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "selected": 0,
        "sent": 0,
        "pending": 0,
        "skipped": 0,
        "failed": 0,
        "unverified": 0,
        "interrupted": 0,
        "stopped": False,
    }
    if not config:
        print("[send] missing config", file=sys.stderr)
        return stats

    # v20: primary-host guard. On a non-primary machine (e.g. the dev MacBook)
    # refuse to actually SUBMIT, so a stray command never fires real outreach.
    # fill-only never submits, so it's allowed everywhere for local testing.
    from _outreach_core import host_role
    if mode in ("auto", "interactive"):
        allowed, reason = host_role.is_send_allowed(config)
        if not allowed:
            host = host_role.current_host()
            primary = host_role.configured_primary_host(config)
            msg = (
                f"⏭ 送信をスキップしました（このホストは実行担当ではありません）。\n"
                f"　このマシン: {host} / 実行担当(primary): {primary}\n"
                f"　送信は {primary} 側で実行されます。開発機からの誤送信を防ぐためのガードです。\n"
                f"　意図的にこのマシンで送るなら DOORMAN_FORCE_SEND=1 を付けて実行してください。"
            )
            print(f"[send] BLOCKED by primary-host guard: {reason}")
            try:
                from _outreach_core.notify import post as _notify_post
                _notify_post(msg, level="warn")
            except Exception:  # noqa: BLE001
                pass
            return stats

    with input_path.open(encoding="utf-8") as f:
        drafts = [json.loads(l) for l in f if l.strip()]
    sendable = [d for d in drafts if d.get("draft", {}).get("subject") != "SKIP"]
    if not sendable:
        print("[send] no sendable drafts")
        return stats

    targets = [d for i, d in enumerate(sendable, 1) if i in ids]
    if not targets:
        print(f"[send] no matching ids; max={len(sendable)}")
        return stats
    stats["selected"] = len(targets)

    sent_ids = load_sent_set()
    pre_filtered = [d for d in targets if d["id"] in sent_ids]
    if pre_filtered:
        names = ", ".join(d.get("name", "?") for d in pre_filtered)
        print(f"[send] ⚠ skipping {len(pre_filtered)} already in sent_history: {names}")
        targets = [d for d in targets if d["id"] not in sent_ids]
        stats["selected"] = len(targets)
        if not targets:
            return stats

    # v15 §R3: resume guard — a target whose journal shows submit_attempted
    # without verified crashed mid-submit last run. Double-send risk → do NOT
    # auto-send; route to needs_attention for a human decision.
    journal_entries = core_send_journal.load_journal(DATA_DIR)
    unverified = core_send_journal.unverified_attempt_ids(journal_entries)
    flagged = [d for d in targets if str(d.get("id")) in unverified]
    if flagged:
        stats["unverified"] = len(flagged)
        stats["pending"] += len(flagged)
        open_unverified_attention = {
            str(row.get("target_id") or "")
            for row in list_open_needs_attention(DATA_DIR)
            if "unverified_prior_attempt" in str(row.get("reason") or "")
        }
        names = ", ".join(d.get("name", "?") for d in flagged)
        print(f"[send] ⚠ {len(flagged)} target(s) with UNVERIFIED prior submit "
              f"attempt → needs_attention (二重送信防止): {names}")
        for d in flagged:
            if str(d.get("id") or "") not in open_unverified_attention:
                append_needs_attention(DATA_DIR, {
                    "target_id": d.get("id"),
                    "name": d.get("name"),
                    "channel": "jp_form",
                    "reason": ("unverified_prior_attempt: 前回runが最終送信クリック後・"
                               "verify前に異常終了。二重送信防止のため自動送信を停止。"
                               "送信履歴(メール受信等)を確認してから判断してください"),
                })
                open_unverified_attention.add(str(d.get("id") or ""))
            _emit_event(
                "send.unverified_prior_attempt", stage="send",
                target_id=str(d.get("id") or ""), payload={},
            )
        targets = [d for d in targets if str(d.get("id")) not in unverified]
        if not targets:
            return stats

    # Resume guard for parent/process deaths before final submit. This is
    # lower-risk than submit_attempted, but automatically retrying the same
    # page can produce an endless stall/restart loop. Park it for review and
    # keep the campaign moving to the next company.
    interrupted = core_send_journal.interrupted_pre_submit_ids(journal_entries)
    interrupted_targets = [d for d in targets if str(d.get("id")) in interrupted]
    if interrupted_targets:
        stats["interrupted"] = len(interrupted_targets)
        stats["pending"] += len(interrupted_targets)
        open_interrupted_attention = {
            str(row.get("target_id") or "")
            for row in list_open_needs_attention(DATA_DIR)
            if "interrupted_before_submit" in str(row.get("reason") or "")
        }
        names = ", ".join(d.get("name", "?") for d in interrupted_targets)
        print(
            f"[send] ⚠ {len(interrupted_targets)} target(s) interrupted before "
            f"final submit → needs_attention/resume skip: {names}"
        )
        for d in interrupted_targets:
            tid = str(d.get("id") or "")
            if tid not in open_interrupted_attention:
                append_needs_attention(DATA_DIR, {
                    "target_id": d.get("id"),
                    "name": d.get("name"),
                    "channel": "jp_form",
                    "form_url": d.get("form_url") or d.get("url"),
                    "reason_class": "interrupted_before_submit",
                    "action_needed": "manual_review_or_retry",
                    "reason": (
                        "interrupted_before_submit: 前回runがこの会社の処理中に停止しました。"
                        "最終送信クリック前のチェックポイントなので送信済み扱いにはしませんが、"
                        "同じ会社で再停止ループするのを避けるため自動処理から退避しました。"
                    ),
                })
                open_interrupted_attention.add(tid)
            core_send_journal.append_journal(
                DATA_DIR,
                tid,
                core_send_journal.PHASE_TARGET_FINISHED,
                outcome="interrupted_before_submit_skipped_on_resume",
                form_url=d.get("form_url") or d.get("url"),
            )
            _emit_event(
                "send.interrupted_before_submit_resume_skip",
                stage="send",
                target_id=tid,
                payload={},
            )
        targets = [d for d in targets if str(d.get("id")) not in interrupted]
        if not targets:
            return stats

    mode_label = {
        "interactive": "interactive (prompts after fill)",
        "auto": "AUTO (no prompts)",
        "fill-only": "fill-only (no submit click)",
    }.get(mode, mode)
    autonomous = core_autonomy.is_autonomous(config)
    score_on = core_autonomy.self_score_enabled(config)
    if autonomous:
        thr = core_autonomy.score_threshold(config)
        print(f"[send] processing {len(targets)} targets · mode={mode_label} · AUTONOMOUS"
              + (f" · self-score≥{thr:.2f}" if score_on else " · self-score off")
              + " · blockers→auto-skip")
    else:
        print(f"[send] processing {len(targets)} targets · mode={mode_label}")

    lead_timeout_sec = _send_lead_soft_timeout_sec(config)
    if lead_timeout_sec > 0:
        print(f"[send] lead soft-timeout: {lead_timeout_sec}s/target")
    else:
        print("[send] lead soft-timeout: disabled")

    sent: list[dict[str, Any]] = []
    filled_only: list[dict[str, Any]] = []
    tab_isolation = _tab_isolation_enabled(config)
    resolver_tab_ids: set[str] = set()  # error tabs kept open for the resolver
    # Resolve to a concrete mode ("slack"/None). Previously the raw arg (default
    # None) was passed straight through, so the Slack progress loop never started
    # unless the caller said --heartbeat slack → progress was silent in the
    # thread. resolve_heartbeat_mode honors the brief's heartbeat.enabled_for and
    # turns on Slack posting whenever the bot/webhook is configured.
    hb_mode = resolve_heartbeat_mode(heartbeat, task="send")
    hb = HeartbeatSession(SKILL_DIR, "send", len(targets), heartbeat=hb_mode, data_dir=DATA_DIR)
    hb.start(f"send {len(targets)} targets")
    core_progress.start(DATA_DIR, "send", len(targets))

    # v27: inbound thread control. A human reply in the progress thread can stop
    # the batch between targets. Disabled (no-op) when Slack isn't configured.
    from _outreach_core import thread_control
    stop_watcher = thread_control.ThreadStopWatcher.from_env(config=config)
    stopped_by_user = False

    try:
        for di, d in enumerate(targets):
            # Checkpoint at the safe boundary (between targets, never mid-submit).
            stop, why = stop_watcher.should_stop()
            if stop:
                stopped_by_user = True
                msg = (
                    f"🛑 スレッドの指示により処理を停止しました"
                    f"（{di}/{len(targets)} 件処理済み、残り {len(targets) - di} 件は未処理）。\n"
                    f"　指示: {why}\n　再開するには再度 send を実行してください。"
                )
                print(f"  [send] 🛑 stop requested via Slack thread: {why}")
                try:
                    from _outreach_core.notify import post as _notify_post
                    _notify_post(msg, level="warn",
                                 thread_ts=stop_watcher.thread_ts or None)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    _emit_event(
                        "send.thread_stop", stage="send",
                        payload={"processed": di, "remaining": len(targets) - di,
                                 "reason": str(why)[:200]},
                    )
                except Exception:  # noqa: BLE001
                    pass
                break
            idx = sendable.index(d) + 1
            tid = str(d.get("id") or d.get("name", "?"))
            # v31 §WS8e — batch position for the Slack [i/N] prefix. The
            # journal keeps the sendable-wide ``idx`` above; the Slack feed
            # wants "position within THIS batch", which is di/len(targets).
            # post_target_event falls back to these keys when its idx/total
            # args are None, so every call site (sent/skip/timeout) renders
            # a consistent [i/N] without threading two more parameters.
            d["_batch_idx"] = di + 1
            d["_batch_total"] = len(targets)
            target_started_at = time.time()
            _hb_stage(f"send {di + 1}/{len(targets)} {d.get('name', tid)}")
            print(
                f"  [send] ▶ {di + 1}/{len(targets)} {d.get('name', tid)} "
                f"(id={tid})",
                flush=True,
            )
            core_send_journal.append_journal(
                DATA_DIR,
                tid,
                core_send_journal.PHASE_TARGET_STARTED,
                idx=idx,
                name=d.get("name"),
                form_url=d.get("form_url") or d.get("url"),
            )
            try:
                with _lead_soft_timeout(lead_timeout_sec, target_id=tid):
                    result = _send_one_target(
                        d, di=di, idx=idx, mode=mode, config=config,
                        verify_strict=verify_strict, iterative_fill=iterative_fill,
                        autonomous=autonomous, score_on=score_on,
                        tab_isolation=tab_isolation, resolver_tab_ids=resolver_tab_ids,
                        sent=sent, filled_only=filled_only,
                    )
            except KeyboardInterrupt:
                raise
            except LeadSoftTimeoutError as exc:
                print(
                    f"  [send] ⚠ lead timed out after {lead_timeout_sec}s: "
                    f"{d.get('name', tid)} — continuing with next target",
                    file=sys.stderr,
                )
                try:
                    _emit_event(
                        "send.target_timeout",
                        stage="send",
                        target_id=tid,
                        outcome=core_outcomes.NETWORK_ERROR,
                        payload={
                            "timeout_sec": lead_timeout_sec,
                            "elapsed_sec": int(max(0, time.time() - target_started_at)),
                            "error": str(exc)[:200],
                        },
                    )
                    _emit_event(
                        "send.lead_timed_out",
                        stage="send",
                        target_id=tid,
                        payload={
                            "timeout_sec": lead_timeout_sec,
                            "error": str(exc)[:200],
                        },
                    )
                    append_needs_attention(DATA_DIR, {
                        "target_id": d.get("id"),
                        "name": d.get("name"),
                        "channel": "jp_form",
                        "form_url": d.get("form_url") or d.get("url"),
                        "reason_class": "target_timeout",
                        "action_needed": "manual_verify",
                        "reason": (
                            f"target_timeout/lead_soft_timeout: 1社処理が{lead_timeout_sec}秒を超過。"
                            "ブラウザ/サイト応答詰まりの可能性があるため自動処理を退避しました。"
                        ),
                    })
                    # v30 §WS-D — concise per-target one-liner for the thread.
                    # The append_needs_attention call above already triggers a
                    # verbose post_problem; this is the lightweight progress
                    # marker so the operator sees the timeout at the right
                    # position in the per-target feed.
                    from _outreach_core import notify as _notify
                    _notify.post_target_event(
                        stage="send", status="timeout",
                        target=d,
                        detail={
                            "reason_class": "target_timeout",
                            "elapsed_sec": int(max(0, time.time() - target_started_at)),
                        },
                    )
                except Exception:
                    pass
                _close_tab_safely(d.get("_send_tab_id"))
                result = {"outcome": "timed_out", "reason": str(exc)}
            except Exception as exc:  # noqa: BLE001 — per-lead isolation (v15 §R1)
                tb = traceback.format_exc()
                print(f"  [send] ✗ lead crashed: {exc} — continuing with next target",
                      file=sys.stderr)
                try:
                    _emit_event(
                        "send.lead_crashed", stage="send", target_id=tid,
                        payload={"error": str(exc)[:200], "tb_tail": tb[-800:]},
                    )
                    append_needs_attention(DATA_DIR, {
                        "target_id": d.get("id"), "name": d.get("name"),
                        "channel": "jp_form",
                        "reason": f"lead_crashed: {str(exc)[:160]}",
                    })
                except Exception:
                    pass
                _close_tab_safely(d.get("_send_tab_id"))
                result = {"outcome": "crashed", "error": str(exc)[:200]}
            try:
                payload = core_outcomes.build_target_outcome_payload(
                    target=d,
                    result=result,
                    started_at=target_started_at,
                    finished_at=time.time(),
                    timeline=d.get("_send_timeline") or [],
                )
                _emit_event(
                    "send.target_outcome",
                    stage="send",
                    target_id=tid,
                    outcome=payload["outcome"],
                    payload=payload,
                )
            except Exception:  # noqa: BLE001 - observability must not break send
                pass
            try:
                core_send_journal.append_journal(
                    DATA_DIR,
                    tid,
                    core_send_journal.PHASE_TARGET_FINISHED,
                    outcome=str((result or {}).get("outcome") or "done"),
                    form_url=d.get("form_url") or d.get("url"),
                )
            except Exception:  # noqa: BLE001 - resume bookkeeping must not break send
                pass
            d.pop("_send_tab_id", None)
            hb.tick(di + 1, f"{d.get('name', '?')} · {result.get('outcome', 'done')}")
            _hb_stage(f"send {di + 1}/{len(targets)} done")
            core_progress.bump(DATA_DIR, outcome=result.get("outcome"),
                               name=d.get("name"))

            if di < len(targets) - 1:
                print(f"  [send] sleeping 30s before next...")
                time.sleep(30)
    finally:
        # §R1 acceptance: hb.end always runs; partial successes are persisted
        # even when the loop dies mid-batch.
        _end_note = "stopped by user · " if stopped_by_user else ""
        hb.end(f"send {_end_note}done · sent={len(sent)} · pending={len(filled_only)}")
        core_progress.finish(DATA_DIR, status="stopped" if stopped_by_user else "done")
        if sent:
            append_sent_history(sent)
    _done_label = "stopped" if stopped_by_user else "done"
    print(f"\n[send] {_done_label} · sent={len(sent)} · filled-only={len(filled_only)}")
    if filled_only:
        names = ", ".join(d.get("name", "?") for d in filled_only)
        print(f"[send] not auto-logged: {names}")
        ids_str = ",".join(str(sendable.index(d) + 1) for d in filled_only)
        print(f"[send] If you completed any of those manually:")
        print(f"      python run.py mark-sent --ids {ids_str}")
    stats["sent"] = len(sent)
    stats["pending"] = (
        int(stats.get("unverified") or 0)
        + int(stats.get("interrupted") or 0)
        + len(filled_only)
    )
    stats["stopped"] = stopped_by_user
    return stats


def _resolve_ids_arg(ids_str: str | None, use_all: bool,
                      input_path: Path, cmd_name: str = "send") -> set[int] | None:
    """
    Parse --ids and --all options. Returns:
      - set of 1-based indices (among SENDABLE drafts, excluding SKIP and
        already-sent), or
      - None on parse error (caller exits)

    Accepts:
      --ids 1,3,5      → {1, 3, 5}
      --ids all        → every not-yet-sent SENDABLE draft
      --all            → same as --ids all
    """
    if not ids_str and not use_all:
        print(f"[{cmd_name}] specify --ids 1,3 or --ids all (or --all)", file=sys.stderr)
        return None

    select_all = use_all or (ids_str and ids_str.strip().lower() == "all")

    if select_all:
        if not input_path.exists():
            print(f"[{cmd_name}] {input_path} not found; nothing to send", file=sys.stderr)
            return None
        drafts = [json.loads(l) for l in input_path.open() if l.strip()]
        sendable = [d for d in drafts if d.get("draft", {}).get("subject") != "SKIP"]
        sent_ids = load_sent_set()
        skip_ids = load_skip_set()
        not_yet_sent = [
            i for i, d in enumerate(sendable, 1)
            if d["id"] not in sent_ids and d["id"] not in skip_ids
        ]
        if not not_yet_sent:
            print(f"[{cmd_name}] no sendable drafts left (all already in sent_history or skip_history)")
            return set()
        print(f"[{cmd_name}] --all → {len(not_yet_sent)} drafts: {','.join(map(str, not_yet_sent))}")
        return set(not_yet_sent)

    try:
        return {int(x.strip()) for x in ids_str.split(",") if x.strip()}
    except ValueError:
        print(f"[{cmd_name}] could not parse --ids '{ids_str}'", file=sys.stderr)
        return None


def stage_walkthrough(input_path: Path, config: dict[str, Any],
                       default_action: str = "send") -> None:
    """
    Walk through each SENDABLE not-yet-sent draft one at a time. For each,
    show preview and prompt:
      [s]end + auto-submit + log
      s[k]ip + log to skip_history (will be filtered next bootstrap)
      [f]ill-only (no submit, manual)
      [q]uit (exit walkthrough)
      (Enter) → take default_action

    Skipped drafts are appended to skip_history.jsonl with
    reason="user_skip_at_walkthrough" so they're auto-filtered on the next
    `bootstrap` run. Use `python run.py history purge-skip` to undo.
    """
    drafts = [json.loads(l) for l in input_path.open()]
    sendable = [d for d in drafts if d.get("draft", {}).get("subject") != "SKIP"]
    sent_ids = load_sent_set()
    skip_ids = load_skip_set()

    queue = [(i, d) for i, d in enumerate(sendable, 1)
             if d["id"] not in sent_ids and d["id"] not in skip_ids]

    if not queue:
        print("[walk] no drafts left (all in sent_history or skip_history)")
        print("       to retry: python run.py history purge-skip / purge-sent")
        return

    bar = "=" * 78
    print(f"\n{bar}\nWALKTHROUGH — {len(queue)} drafts to review\n{bar}")
    print("Per draft, choose:")
    print("  s  → send + auto-submit + log to sent_history")
    print("  k  → skip this company (log to skip_history; filtered next bootstrap)")
    print("  f  → fill the form only, you submit manually")
    print("  q  → quit walkthrough")
    print(f"  (Enter to default: {default_action})")

    user_skipped: list[dict[str, Any]] = []
    sent_count = 0
    fill_count = 0

    for j, (idx, d) in enumerate(queue, 1):
        name = d.get("name", "?")
        subject = d["draft"].get("subject") or "(none)"
        body = d["draft"]["body"]
        cap = (d.get("form_fields") or {}).get("has_recaptcha_v2", False) \
              or d.get("captcha") == "recaptcha_v2_visible"
        cap_flag = " [reCAPTCHA-v2]" if cap else ""
        flow = d.get("flow", "?")

        print(f"\n{bar}")
        print(f"[{j}/{len(queue)}] {name}  ({d.get('industry', '?')}, founded {d.get('founded', '?')}){cap_flag}")
        print(f"  URL: {d.get('form_url', '')}")
        print(f"  Flow: {flow}, char_limit: {d.get('char_limit', '?')}")
        print(f"  Subject: {subject}")
        print(f"  Body ({len(body)} chars):")
        for line in body.splitlines():
            print(f"    {line}")

        try:
            ans = input(f"\n  Send this? [y]es=send / [n]o=skip / [f]ill-only / [q]uit (default={default_action}) → ").strip().lower()
        except EOFError:
            print("\n  (no stdin) — exiting walkthrough")
            break

        if ans == "":
            ans = default_action[0]  # 's' / 'k' / 'f' / 'q'

        if ans in ("q", "quit", "exit"):
            print("  → quit")
            break

        if ans in ("n", "no", "k", "skip"):
            user_skipped.append(d)
            print(f"  → No / skipped & queued for skip_history")
            continue

        if ans in ("f", "fill", "fill-only"):
            stage_send(input_path, {idx}, mode="fill-only", config=config)
            fill_count += 1
            continue

        if ans in ("y", "yes", "s", "send"):
            stage_send(input_path, {idx}, mode="auto", config=config)
            sent_count += 1
            continue

        print(f"  unrecognized '{ans}' — moving to next (no action taken)")

    # Persist user skips
    if user_skipped:
        now = datetime.utcnow().isoformat() + "Z"
        with SKIP_HISTORY_PATH.open("a") as f:
            for d in user_skipped:
                entry = {
                    "id": d["id"],
                    "name": d.get("name"),
                    "industry": d.get("industry"),
                    "skipped_at": now,
                    "reason": "user_skip_at_walkthrough",
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"\n[walk] logged {len(user_skipped)} user-skips to skip_history.jsonl")

    print(f"\n{bar}\nWALKTHROUGH SUMMARY")
    print(f"  sent (auto-submitted):   {sent_count}")
    print(f"  filled-only (manual):    {fill_count}")
    print(f"  user-skipped:            {len(user_skipped)}")
    print(f"  remaining in queue:      {len(queue) - sent_count - fill_count - len(user_skipped)}")
    print(bar)


def stage_resolve_proceed(
    target_id: str,
    config: dict[str, Any],
    *,
    drafts_path: Path | None = None,
) -> None:
    """Resume send after user completed CAPTCHA or confirm page (browser still open)."""
    path = drafts_path or (DATA_DIR / "drafts.jsonl")
    if not path.exists():
        print(f"[resolve] {path} not found", file=sys.stderr)
        return

    drafts = [json.loads(line) for line in path.open() if line.strip()]
    sendable = [d for d in drafts if (d.get("draft") or {}).get("subject") != "SKIP"]
    d = next((x for x in sendable if x.get("id") == target_id), None)
    if not d:
        print(f"[resolve] target_id {target_id} not in sendable drafts", file=sys.stderr)
        return

    name = d.get("name", "?")
    flow = d.get("flow", "single")
    print(f"[resolve] proceed → {name} (flow={flow})")

    patterns = [r"^送信する$", r"^送信$", r"この内容で送信", r"内容を送信する", r"submit"]
    click_res = _click_button_with_gate_retry(patterns)
    if not click_res or not click_res.get("clicked"):
        print("[resolve] ⚠ submit button not found on current page", file=sys.stderr)
        _escalate_await_proceed(d, "awaiting_user_proceed: submit button not found on resume")
        return

    print(f"[resolve] clicked: {click_res.get('text')}")
    time.sleep(5)

    from _outreach_core.verify import PAGE_EVIDENCE_JS

    page_evidence = _evaluate(PAGE_EVIDENCE_JS)
    snap = oc_browser("snapshot")
    combined = _combine_page_evidence_text(
        snap, page_evidence if isinstance(page_evidence, dict) else None
    )
    snap_path = DATA_DIR / f"verify_snapshot_{d.get('id', 0)}.txt"
    if combined.strip():
        snap_path.write_text(combined, encoding="utf-8")

    vresult = verify_send_completed(
        d,
        "jp_form",
        snapshot=combined,
        browser_verify=page_evidence if isinstance(page_evidence, dict) else None,
        plan=d.get("_llm_plan"),
        evaluate_fn=_evaluate,
        data_dir=DATA_DIR,
        snapshot_path=snap_path if combined.strip() else None,
    )
    outcome = handle_verify_result(d, vresult, DATA_DIR, channel="jp_form")
    if outcome == "sent_ok":
        append_sent_history([d])
        close_needs_attention(DATA_DIR, target_id, resolution="proceed: verified ok")
        _emit_event(
            "send.resolved",
            stage="send",
            target_id=target_id,
            outcome="ok",
            payload={"previous_status": "awaiting_user_proceed", "new_status": "ok"},
        )
        print(f"[resolve] ✅ {vresult.get('reason')}")
        # v31 §WS8a — handle_verify_result no longer posts 送信完了; this
        # proceed path owns its own per-target success line.
        try:
            from _outreach_core import notify as _notify
            _notify.post_target_event(
                stage="resolve",
                status="sent",
                target=d,
                detail={
                    "verify_status": vresult.get("status"),
                    "form_url": d.get("form_url") or d.get("url"),
                },
            )
        except Exception:  # noqa: BLE001 - Slack must never abort the flow
            pass
    else:
        print(f"[resolve] ⚠ verify: {vresult.get('status')} — {vresult.get('reason')}")


def stage_resolve(
    target_id: str,
    fields: dict[str, str],
    config: dict[str, Any],
    *,
    drafts_path: Path | None = None,
) -> None:
    """Apply field overrides from Slack/user and retry send for one target."""
    path = drafts_path or (DATA_DIR / "drafts.jsonl")
    if not path.exists():
        print(f"[resolve] {path} not found", file=sys.stderr)
        return

    drafts = [json.loads(line) for line in path.open() if line.strip()]
    sendable = [d for d in drafts if (d.get("draft") or {}).get("subject") != "SKIP"]
    found_idx = next((i for i, d in enumerate(sendable, 1) if d.get("id") == target_id), None)
    if found_idx is None:
        print(f"[resolve] target_id {target_id} not in sendable drafts", file=sys.stderr)
        return

    for d in drafts:
        if d.get("id") == target_id:
            overrides = dict(d.get("field_map_overrides") or {})
            overrides.update(fields)
            d["field_map_overrides"] = overrides
            d.pop("_llm_plan", None)

    with path.open("w") as f:
        for d in drafts:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    close_needs_attention(DATA_DIR, target_id, resolution=f"overrides: {fields}")
    print(f"[resolve] updated overrides for {target_id}: {fields}")
    stage_send(path, {found_idx}, mode="auto", config=config, heartbeat=None)


def stage_mark_sent(input_path: Path, ids: set[int]) -> None:
    drafts = [json.loads(l) for l in input_path.open()]
    sendable = [d for d in drafts if d.get("draft", {}).get("subject") != "SKIP"]
    sent = [d for i, d in enumerate(sendable, 1) if i in ids]
    if not sent:
        print(f"[mark-sent] no matches for ids {sorted(ids)}")
        return
    append_sent_history(sent)
    for d in sent:
        close_needs_attention(
            DATA_DIR,
            str(d.get("id") or ""),
            resolution="mark-sent: user confirmed sent",
        )
    print(f"[mark-sent] logged {len(sent)}: " + ", ".join(d.get("name", "?") for d in sent))


# ============================================================================
# CLI
# ============================================================================

# ---------------------------------------------------------------------------
# v25: 決定の永続化 — Slack等でユーザーと合意した内容（確定フォームURL等）を
# brief 単位で decisions.jsonl に記録し、別プロセス/再実行でも再質問しない。
# ---------------------------------------------------------------------------
def _decisions_path() -> Path:
    return DATA_DIR / "decisions.jsonl"


def record_decision(action: str, target_id: str, payload: dict[str, Any]) -> None:
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "brief": BRIEF_ID,
        "action": action,
        "target_id": target_id,
        **payload,
    }
    path = _decisions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _update_leads_jsonl(target_id: str, updates: dict[str, Any]) -> bool:
    """leads.jsonl に pin-url の変更を即時反映する（再bootstrap不要にする）。"""
    leads = DATA_DIR / "leads.jsonl"
    if not leads.exists():
        return False
    rows: list[str] = []
    hit = False
    for line in leads.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            rows.append(line)
            continue
        if str(row.get("id")) == target_id:
            row.update(updates)
            hit = True
        rows.append(json.dumps(row, ensure_ascii=False))
    if hit:
        leads.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return hit


def stage_pin_url(args: Any) -> None:
    """ユーザー確認済みフォームURLをターゲットへ固定（form_url_locked）。"""
    tp = _PATHS.targets_path if _PATHS else None
    if not tp or not tp.exists():
        print(f"[pin-url] targets file not found: {tp}", file=sys.stderr)
        sys.exit(2)
    if not args.unlock and not args.url:
        print("[pin-url] --url が必要です（解除時のみ --unlock 単独可）", file=sys.stderr)
        sys.exit(2)
    raw = yaml.safe_load(tp.read_text()) or {}
    companies = raw.get("companies") if isinstance(raw, dict) else None
    if not isinstance(companies, list):
        print("[pin-url] companies キーが見つかりません", file=sys.stderr)
        sys.exit(2)
    hit = next((c for c in companies if str(c.get("id")) == args.id), None)
    if hit is None:
        print(f"[pin-url] id={args.id} が targets に見つかりません", file=sys.stderr)
        sys.exit(2)
    old_url = str(hit.get("form_url") or "")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = tp.parent / f"{tp.stem}.backup.pin-url.{stamp}{tp.suffix}"
    backup.write_text(tp.read_text())
    if args.unlock:
        hit["form_url_locked"] = False
        updates: dict[str, Any] = {"form_url_locked": False}
        action = "unpin_url"
        print(f"[pin-url] {args.id}: ロック解除（自動URL補正を再許可）")
    else:
        hit["form_url"] = args.url
        hit["form_url_locked"] = True
        note_line = f"{datetime.now().date().isoformat()} pin-url: {args.url}"
        if args.note:
            note_line += f" — {args.note}"
        prev = str(hit.get("notes") or "").strip()
        hit["notes"] = (prev + "\n" if prev else "") + note_line
        updates = {"form_url": args.url, "form_url_locked": True}
        action = "pin_url"
        print(f"[pin-url] {args.id}: form_url を固定")
        print(f"  old: {old_url or '(なし)'}")
        print(f"  new: {args.url} (locked)")
    tp.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))
    print(f"  backup: {backup.name}")
    if _update_leads_jsonl(args.id, updates):
        print("  leads.jsonl にも反映済み（再bootstrap不要）")
    record_decision(action, args.id, {
        "url": args.url or "",
        "old_url": old_url,
        "note": args.note or "",
        "source": "cli",
    })
    print(f"  decision を記録 -> {_decisions_path().name}")


def stage_decisions(args: Any) -> None:
    """記録済みユーザー決定の一覧表示。"""
    path = _decisions_path()
    if not path.exists():
        print(f"[decisions] まだ決定記録がありません: {path}")
        return
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if args.id and str(rec.get("target_id")) != args.id:
            continue
        n += 1
        note = f" — {rec.get('note')}" if rec.get("note") else ""
        print(f"  {rec.get('ts')} [{rec.get('action')}] {rec.get('target_id')}: "
              f"{rec.get('url') or rec.get('old_url') or ''}{note}")
    print(f"[decisions] {n} 件")


def main() -> None:
    ap = argparse.ArgumentParser(prog="jp-form-outreach", description=__doc__)
    brief_parent = argparse.ArgumentParser(add_help=False)
    brief_parent.add_argument(
        "--brief",
        default=argparse.SUPPRESS,
        help="Brief id (default: briefs/_active.txt)",
    )
    brief_parent.add_argument(
        "--persona",
        default=argparse.SUPPRESS,
        help="Persona id (default: brief/thread binding)",
    )
    ap.add_argument(
        "--brief",
        default=None,
        help="Brief id (default: briefs/_active.txt or DOORMAN_SLACK_CHANNEL_ID)",
    )
    ap.add_argument(
        "--persona",
        default=None,
        help="Persona id (default: brief/thread binding)",
    )
    ap.add_argument(
        "--slack-channel-id",
        default=None,
        help="Slack channel id for brief resolution (sets DOORMAN_SLACK_CHANNEL_ID)",
    )
    ap.add_argument(
        "--slack-thread-ts",
        default=None,
        help="Slack thread ts for heartbeat replies (sets DOORMAN_SLACK_THREAD_TS)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser(
        "bootstrap",
        parents=[brief_parent],
        help="(Pull) Load curated targets/<brief>.yaml -> data/briefs/<brief>/leads.jsonl",
    )
    p.add_argument("--targets", default=None, help="Targets YAML (default: targets/<brief>.yaml)")
    p.add_argument("--out", default=None, help="Output leads.jsonl (default: data/briefs/<brief>/)")
    p.add_argument("--include-sent", action="store_true",
                   help="Also include companies marked status: sent")
    p.add_argument("--include-dropped", action="store_true",
                   help="Also include companies marked status: dropped")
    p.add_argument("--include-skipped", action="store_true",
                   help="Also include companies present in skip_history.jsonl (for re-test)")
    p.add_argument("--limit", type=int, default=None,
                   help="Pull only the first N eligible targets (after status/history filters)")
    p.add_argument("--only", default=None,
                   help="Comma-separated target ids to restrict to (e.g. 'ikkholdings,pharmafoods')")

    p = sub.add_parser(
        "campaign",
        parents=[brief_parent],
        help="Run the full 6-phase outreach pipeline (pull→enrich→draft→preview+send)",
    )
    p.add_argument("--targets", default=None)
    p.add_argument("--clean", action="store_true",
                   help="Wipe leads/enriched/drafts before running")
    p.add_argument("--skip-enrich", action="store_true",
                   help="Skip the enrich phase (use leads.jsonl as enriched.jsonl)")
    p.add_argument("--skip-send", action="store_true",
                   help="Stop at preview without sending (display only)")
    p.add_argument("--include-sent", action="store_true",
                   help="Also include companies marked status: sent (re-send mode)")
    p.add_argument("--include-skipped", action="store_true",
                   help="Also include companies present in skip_history.jsonl (for explicit retry)")
    refine_grp = p.add_mutually_exclusive_group()
    refine_grp.add_argument(
        "--refine",
        action="store_true",
        default=None,
        help="Two-pass draft (critique + rewrite). Default: config draft.refine_default (true)",
    )
    refine_grp.add_argument("--no-refine", action="store_true", help="Disable refine pass")
    p.add_argument(
        "--refine-only-if-low-quality",
        action="store_true",
        help="Agent-led: draft all, then refine only low-rated ids (see SKILL.md quality gate)",
    )
    p.add_argument("--limit", type=int, default=None,
                   help="Pull only the first N eligible targets (after filters)")
    p.add_argument("--only", default=None,
                   help="Comma-separated target ids to restrict to")

    p = sub.add_parser("enrich", parents=[brief_parent], help="Visit each form URL, capture field structure")
    p.add_argument("--in", dest="input_path", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--limit", type=int, default=None,
                   help="Process only the first N rows from input JSONL")

    p = sub.add_parser("draft", parents=[brief_parent], help="Generate personalized form messages")
    p.add_argument("--in", dest="input_path", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--limit", type=int, default=None,
                   help="Process only the first N rows from input JSONL")
    p.add_argument("--config", default=str(SKILL_DIR / "config.yaml"))
    p.add_argument("--from-targets", action="store_true",
                   help="Skip enrich; draft directly from data/leads.jsonl")
    draft_refine_grp = p.add_mutually_exclusive_group()
    draft_refine_grp.add_argument(
        "--refine",
        action="store_true",
        default=None,
        help="Two-pass critique+rewrite (default: config draft.refine_default)",
    )
    draft_refine_grp.add_argument("--no-refine", action="store_true", help="Single-pass draft only")
    p.add_argument(
        "--refine-only-if-low-quality",
        action="store_true",
        help="Single-pass draft; agent re-runs --refine for low-rated ids (SKILL.md)",
    )

    p = sub.add_parser(
        "preview",
        parents=[brief_parent],
        help="(Approve) Show all drafts in terminal, then prompt to send",
    )
    p.add_argument("--in", dest="input_path", default=None)
    p.add_argument("--no-send", action="store_true",
                   help="Skip the interactive send prompt at the end")

    p = sub.add_parser("send", parents=[brief_parent], help="Drive form fill + submit")
    p.add_argument("--in", dest="input_path", default=None)
    p.add_argument("--ids", help="Comma-separated SENDABLE indices (1-based), or 'all' for every not-yet-sent draft. Required unless --all is set.")
    p.add_argument("--all", action="store_true",
                   help="Send every SENDABLE draft that's not already in sent_history (skips SKIPPED automatically)")
    p.add_argument("--config", default=str(SKILL_DIR / "config.yaml"))
    mode_group = p.add_mutually_exclusive_group()
    mode_group.add_argument("--auto-send", action="store_true",
                             help="Fill and submit without prompting")
    mode_group.add_argument("--no-confirm", action="store_true",
                             help="Fill only — you click submit manually")
    p.add_argument(
        "--heartbeat",
        default=None,
        choices=["slack", "auto"],
        help="Progress to Slack: slack=webhook only, auto=webhook or OpenClaw botToken (~5 min)",
    )
    p.add_argument(
        "--verify-strict",
        choices=["true", "false"],
        default="true",
        help="false = success markers only (skip empty-required scan)",
    )
    p.add_argument(
        "--iterative-fill",
        action="store_true",
        help="On fill errors, refresh DOM and run a second LLM fill plan",
    )

    p = sub.add_parser(
        "resolve",
        parents=[brief_parent],
        help="Resolve needs_attention with field overrides or resume send",
    )
    p.add_argument("--target-id", required=True)
    p.add_argument(
        "--action",
        default="fields",
        choices=["fields", "proceed"],
        help="fields: apply --field overrides and retry send; proceed: click submit on open browser",
    )
    p.add_argument("--field", action="append", default=[], help="key=value (repeatable)")
    p.add_argument("--config", default=str(SKILL_DIR / "config.yaml"))

    p = sub.add_parser(
        "walk",
        parents=[brief_parent],
        help="Walkthrough: review each SENDABLE draft one-by-one and choose send/skip/fill/quit",
    )
    p.add_argument("--in", dest="input_path", default=None)
    p.add_argument("--config", default=str(SKILL_DIR / "config.yaml"))
    p.add_argument("--default", default="send",
                   choices=["send", "skip", "fill", "quit"],
                   help="Action when user just presses Enter (default: send)")

    p = sub.add_parser("mark-sent", parents=[brief_parent], help="Log specific drafts to sent_history.jsonl")
    p.add_argument("--in", dest="input_path", default=None)
    p.add_argument("--ids", help="Comma-separated SENDABLE indices, or 'all'")
    p.add_argument("--all", action="store_true", help="Mark every not-yet-sent SENDABLE draft as sent")

    p = sub.add_parser("history", parents=[brief_parent], help="View / manage skip and sent history")
    p.add_argument(
        "action",
        choices=["show", "needs-attention", "purge-skip", "purge-sent", "purge-all"],
    )

    p = sub.add_parser(
        "approve-autonomy",
        parents=[brief_parent],
        help="One-time approval that unlocks hands-off autonomous sending for this brief",
    )
    p.add_argument("--note", default="", help="Optional note recorded with the approval")
    p.add_argument("--revoke", action="store_true",
                   help="Revoke approval (return brief to pre-approval / supervised gating)")

    p = sub.add_parser(
        "autonomy-status",
        parents=[brief_parent],
        help="Show the autonomy mode + upfront-approval state for this brief",
    )

    p = sub.add_parser(
        "resolve-queue",
        parents=[brief_parent],
        help="Deep-resolve queued blocker targets (separate pass/process; no human 進めて needed)",
    )
    p.add_argument("--status", action="store_true",
                   help="Just show the queue summary, do not resolve")

    p = sub.add_parser(
        "pin-url",
        parents=[brief_parent],
        help="v25: ユーザー確認済みフォームURLを固定し自動URL補正を禁止 (form_url_locked)",
    )
    p.add_argument("--id", required=True, help="Target id (targets/<brief>.yaml の companies[].id)")
    p.add_argument("--url", default="", help="確定フォームURL")
    p.add_argument("--note", default="", help="決定メモ（Slackで合意した内容など）")
    p.add_argument("--unlock", action="store_true", help="ロック解除（自動補正を再許可）")

    p = sub.add_parser(
        "decisions",
        parents=[brief_parent],
        help="v25: このbriefで記録されたユーザー決定 (decisions.jsonl) を表示",
    )
    p.add_argument("--id", default=None, help="Target id で絞り込み")

    args = ap.parse_args()
    import os

    if getattr(args, "slack_channel_id", None):
        os.environ["DOORMAN_SLACK_CHANNEL_ID"] = args.slack_channel_id
    if getattr(args, "slack_thread_ts", None):
        os.environ["DOORMAN_SLACK_THREAD_TS"] = args.slack_thread_ts
    brief_id = getattr(args, "brief", None)
    try:
        configure_brief(
            brief_id,
            persona_id=getattr(args, "persona", None),
            cmd=args.cmd,
        )
    except BriefError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    # v25: パイプ接続時のブロックバッファリングで出力が滞留すると watchdog の
    # no-output stall 判定を誘発するため、行バッファリングへ強制切替。
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
        sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - non-reconfigurable streams
        pass
    # v25: 長時間コマンドでは stdout ハートビートを常時出力（180s stall 対策）。
    if args.cmd in ("enrich", "send", "campaign", "resolve-queue", "draft"):
        _hb_stage(f"{args.cmd} 起動")
        _start_stdout_heartbeat(interval=60.0)

    if args.cmd == "bootstrap":
        only_ids = [x.strip() for x in args.only.split(",")] if args.only else None
        stage_bootstrap(
            Path(args.targets) if args.targets else _PATHS.targets_path,
            _data_path(args.out, "leads.jsonl"),
                        include_sent=args.include_sent,
                        include_dropped=args.include_dropped,
                        include_skipped=args.include_skipped,
                        limit=args.limit,
                        only_ids=only_ids)
    elif args.cmd == "campaign":
        only_ids = [x.strip() for x in args.only.split(",")] if args.only else None
        try:
            cfg = load_merged_config(SKILL_DIR, BRIEF_ID)
        except FileNotFoundError:
            cfg = {}
        refine = _cli_refine_enabled(cfg, args)
        if getattr(args, "refine_only_if_low_quality", False):
            print("[campaign] --refine-only-if-low-quality: pass-1 draft only; "
                  "agent scores drafts then run.py draft --refine for low ids")
        stage_campaign(
            targets_path=Path(args.targets) if args.targets else _PATHS.targets_path,
            clean=args.clean,
            skip_enrich=args.skip_enrich,
            skip_send=args.skip_send,
            include_sent=args.include_sent,
            include_skipped=args.include_skipped,
            refine=refine,
            limit=args.limit,
            only_ids=only_ids,
        )
    elif args.cmd == "enrich":
        try:
            enrich_cfg = load_merged_config(SKILL_DIR, BRIEF_ID)
        except FileNotFoundError as e:
            print(f"[enrich] {e}", file=sys.stderr)
            sys.exit(2)
        stage_enrich(
            _data_path(args.input_path, "leads.jsonl"),
            _data_path(args.out, "enriched.jsonl"),
            config=enrich_cfg,
            limit=args.limit,
        )
    elif args.cmd == "draft":
        try:
            cfg = load_merged_config(SKILL_DIR, BRIEF_ID)
        except FileNotFoundError as e:
            print(f"[draft] {e}", file=sys.stderr)
            sys.exit(2)
        in_path = _data_path(args.input_path, "enriched.jsonl")
        if args.from_targets and not in_path.exists():
            in_path = DATA_DIR / "leads.jsonl"
        refine = _cli_refine_enabled(cfg, args)
        if getattr(args, "refine_only_if_low_quality", False):
            print("[draft] pass-1 only (--refine-only-if-low-quality). "
                  "Re-run with --refine for low-rated target ids after agent review.")
        stage_draft(
            in_path,
            _data_path(args.out, "drafts.jsonl"),
            cfg,
            refine=refine,
            limit=args.limit,
        )
    elif args.cmd == "preview":
        cfg = None
        if not args.no_send:
            try:
                cfg = load_merged_config(SKILL_DIR, BRIEF_ID)
            except FileNotFoundError:
                cfg = None
        stage_preview(
            _data_path(args.input_path, "drafts.jsonl"),
            interactive_send=(not args.no_send),
            config=cfg,
        )
    elif args.cmd == "send":
        try:
            cfg = load_merged_config(SKILL_DIR, BRIEF_ID)
        except FileNotFoundError as e:
            print(f"[send] {e}", file=sys.stderr)
            sys.exit(2)
        drafts_path = _data_path(args.input_path, "drafts.jsonl")
        ids = _resolve_ids_arg(args.ids, args.all, drafts_path, cmd_name="send")
        if ids is None:
            sys.exit(2)
        if args.auto_send:
            mode = "auto"
        elif args.no_confirm:
            mode = "fill-only"
        else:
            mode = "interactive"
        from _outreach_core import events as ev

        if not ev.get_context().data_dir:
            ev.configure(skill="jp-form-outreach", data_dir=DATA_DIR)
        stage_send(
            drafts_path,
            ids,
            mode=mode,
            config=cfg,
            heartbeat=args.heartbeat,
            verify_strict=str(args.verify_strict).lower() == "true",
            iterative_fill=bool(getattr(args, "iterative_fill", False)),
        )
    elif args.cmd == "resolve":
        try:
            cfg = load_merged_config(SKILL_DIR, BRIEF_ID)
        except FileNotFoundError as e:
            print(f"[resolve] {e}", file=sys.stderr)
            sys.exit(2)
        if args.action == "proceed":
            stage_resolve_proceed(args.target_id, cfg)
        else:
            fields: dict[str, str] = {}
            for item in args.field:
                if "=" not in item:
                    print(f"[resolve] bad --field {item!r}, want key=value", file=sys.stderr)
                    sys.exit(2)
                k, v = item.split("=", 1)
                fields[k.strip()] = v.strip()
            if not fields:
                print("[resolve] --action fields requires at least one --field key=value", file=sys.stderr)
                sys.exit(2)
            stage_resolve(args.target_id, fields, cfg)
    elif args.cmd == "walk":
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"[walk] config not found: {config_path}", file=sys.stderr)
            sys.exit(2)
        cfg = yaml.safe_load(config_path.read_text())
        stage_walkthrough(_data_path(args.input_path, "drafts.jsonl"), cfg, default_action=args.default)
    elif args.cmd == "mark-sent":
        drafts_path = _data_path(args.input_path, "drafts.jsonl")
        ids = _resolve_ids_arg(args.ids, args.all, drafts_path, cmd_name="mark-sent")
        if ids is None:
            sys.exit(2)
        stage_mark_sent(drafts_path, ids)
    elif args.cmd == "history":
        stage_history(args.action)
    elif args.cmd == "approve-autonomy":
        if args.revoke:
            core_autonomy.revoke_upfront_approval(DATA_DIR, by="cli")
            print(f"[autonomy] brief={BRIEF_ID} approval REVOKED — "
                  f"次回 campaign は再び事前承認待ちになります")
        else:
            state = core_autonomy.mark_upfront_approved(DATA_DIR, by="cli", note=args.note)
            if state.get("_was_already_approved"):
                print(f"[autonomy] brief={BRIEF_ID} は既に承認済みです（変更なし）")
            else:
                print(f"[autonomy] brief={BRIEF_ID} 承認 ✅ — "
                      f"以降の campaign は確認なしで全件自動送信します")
    elif args.cmd == "resolve-queue":
        if getattr(args, "status", False):
            print(core_resolve_queue.queue_summary(core_resolve_queue.pending(DATA_DIR)))
        else:
            try:
                cfg = load_merged_config(SKILL_DIR, BRIEF_ID)
            except FileNotFoundError:
                cfg = {}
            stage_resolve_queue(cfg)
    elif args.cmd == "pin-url":
        stage_pin_url(args)
    elif args.cmd == "decisions":
        stage_decisions(args)
    elif args.cmd == "autonomy-status":
        try:
            cfg = load_merged_config(SKILL_DIR, BRIEF_ID)
        except FileNotFoundError:
            cfg = {}
        ac = core_autonomy.autonomy_config(cfg)
        state = core_autonomy.read_autonomy_state(DATA_DIR)
        print(f"brief={BRIEF_ID}")
        print(f"  mode:            {ac['mode']}")
        print(f"  on_blocker:      {ac['on_blocker']}")
        print(f"  self_score:      enabled={ac['draft_self_score']['enabled']} "
              f"threshold={ac['draft_self_score']['threshold']} "
              f"on_error={ac['draft_self_score']['on_error']}")
        print(f"  upfront_approval: required={ac['upfront_approval']['required']} "
              f"sample_drafts={ac['upfront_approval']['sample_drafts']}")
        print(f"  self_restart:    {ac['self_restart']}")
        print(f"  approved:        {state.get('approved')}"
              + (f" (at {state.get('approved_at')}, by {state.get('approved_by')})"
                 if state.get('approved') else ""))
        if state.get("pending"):
            print(f"  pending:         {state['pending'].get('at')} "
                  f"{state['pending'].get('summary')}")


if __name__ == "__main__":
    main()
