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
import json
import re
import subprocess
import sys
import time
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
from _outreach_core import resolve_queue as core_resolve_queue
from _outreach_core import tab_utils as core_tab_utils
from _outreach_core import infer as core_infer
from _outreach_core import preview as core_preview
from _outreach_core import prompt as core_prompt
from _outreach_core.config import BriefError, load_merged_config
from _outreach_core.paths import SkillPaths, resolve_skill_paths
from _outreach_core.progress import HeartbeatSession
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
DATA_DIR = SKILL_DIR / "data"
PROMPTS_DIR = SKILL_DIR / "prompts"

DEFAULT_MODEL = core_infer.DEFAULT_MODEL
BROWSER_PROFILE = core_infer.BROWSER_PROFILE
RATE_LIMIT_SECONDS = 4

SKIP_HISTORY_PATH = DATA_DIR / "skip_history.jsonl"
SENT_HISTORY_PATH = DATA_DIR / "sent_history.jsonl"


def configure_brief(brief_id: str | None, *, cmd: str = "") -> SkillPaths:
    """Resolve brief, data/briefs/<id>/, targets/<id>.yaml, and prompt overrides."""
    global _PATHS, BRIEF_ID, DATA_DIR, PROMPTS_DIR, SKIP_HISTORY_PATH, SENT_HISTORY_PATH
    _PATHS = resolve_skill_paths(SKILL_DIR, brief_id, channel="jp_form")
    BRIEF_ID = _PATHS.brief_id
    DATA_DIR = _PATHS.data_dir
    SKIP_HISTORY_PATH = DATA_DIR / "skip_history.jsonl"
    SENT_HISTORY_PATH = DATA_DIR / "sent_history.jsonl"
    try:
        cfg = load_merged_config(SKILL_DIR, BRIEF_ID)
        PROMPTS_DIR = core_prompt.resolve_prompts_dir(SKILL_DIR, cfg)
    except (FileNotFoundError, BriefError):
        PROMPTS_DIR = SKILL_DIR / "prompts"
    if cmd in ("campaign", "bootstrap", "send", "draft", "enrich", "preview"):
        print(f"[{cmd}] brief={BRIEF_ID} · skill=jp-form-outreach")
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

oc_browser = core_infer.oc_browser
oc_infer = core_infer.oc_infer


def _evaluate(js: str) -> Any:
    """Browser evaluate via _outreach_core.infer.oc_evaluate (no LLM)."""
    return core_infer.oc_evaluate(js, profile=BROWSER_PROFILE)


# ============================================================================
# Stage: bootstrap (load targets from targets.yaml)
# ============================================================================

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
    only_id_set = set(only_ids) if only_ids else None

    written: list[dict[str, Any]] = []
    filtered_sent = 0
    filtered_dropped = 0
    filtered_skip = 0
    filtered_only_ids = 0
    for c in companies:
        cid = c.get("id")
        if not cid:
            continue
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
    if drops:
        msg += f"  (filtered: {', '.join(drops)})"
    if capped:
        msg += f"  [limited to first {limit}]"
    if only_id_set:
        msg += f"  [restricted to ids: {','.join(sorted(only_id_set))}]"
    print(msg)


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
      const lbl = document.querySelector(`label[for="${el.id}"]`);
      if (lbl) return lbl.textContent.trim();
    }
    const wrap = el.closest('label');
    if (wrap) return wrap.textContent.trim();
    // Walk up the DOM looking for a sibling/preceding label-ish element
    let cur = el.parentElement;
    for (let i = 0; i < 4 && cur; i++, cur = cur.parentElement) {
      const lbl = cur.querySelector('label, .label, [class*="label" i]');
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
        value: el.value,
        label: label,
        checked: el.checked
      });
      continue;
    }
    if (type === 'checkbox') {
      result.checkboxes.push({
        name: name, label: label, value: el.value, checked: el.checked
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


def _emit_event(kind: str, *, stage: str, target_id: str | None = None, **kwargs: Any) -> None:
    from _outreach_core import events as ev

    ev.emit(kind, stage=stage, target_id=target_id, **kwargs)


_NON_CONTACT_HEADING_KW = {
    "register": ("会員登録", "新規登録", "アカウント作成", "アカウント登録", "ユーザー登録"),
    "login": ("ログイン",),
    "recruit": ("採用", "応募", "求人", "履歴書", "エントリーシート", "ES提出"),
    "reservation": ("予約フォーム", "来店予約", "ご予約", "施術予約", "見学予約"),
    "estimate_consumer": ("無料相談", "無料カウンセリング", "施術に関する相談"),
}


def _classify_form_type(
    fields: dict[str, Any], snapshot: str | None
) -> tuple[str, str | None]:
    """Classify the page as 'contact' (B2B inquiry) or one of the non-contact
    categories. Returns (kind, reason_if_non_contact).
    """
    inputs = fields.get("inputs") or []
    textareas = fields.get("textareas") or []

    for inp in inputs:
        if str(inp.get("type", "")).lower() == "password":
            return ("register", "password field present")

    name_blob = " ".join(
        str(x.get("name") or x.get("label") or "")
        for x in inputs
    ).lower()
    if "birth" in name_blob and "year" in name_blob:
        return ("register", "birth-year field present")
    if "生年月日" in name_blob:
        return ("register", "生年月日 field present")

    snap_head = (snapshot or "")[:2000]
    for kind, kws in _NON_CONTACT_HEADING_KW.items():
        for kw in kws:
            if kw in snap_head:
                if textareas:
                    continue
                return (kind, f"heading mentions '{kw}'")

    if not textareas:
        return ("unknown_no_textarea", "no textarea on page (not a typical contact form)")

    return ("contact", None)


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
    for i, t in enumerate(targets, 1):
        if not t.get("form_url"):
            print(f"[enrich] ({i}/{len(targets)}) {t.get('name')}: no form_url, skipping")
            enriched.append({**t, "_enrich_skipped": "no form_url"})
            continue

        if t.get("category") in ("b2c_only", "iframe", "site_closed"):
            cat = t.get("category")
            print(f"[enrich] ({i}/{len(targets)}) {t.get('name')}: category={cat}, skipping")
            enriched.append({**t, "_enrich_skipped": f"category={cat}"})
            continue

        print(f"[enrich] ({i}/{len(targets)}) {t.get('name')} -> {t['form_url']}")
        oc_browser("open", t["form_url"])
        time.sleep(RATE_LIMIT_SECONDS)

        from _outreach_core.cookie_dismiss import apply_cookie_dismiss

        apply_cookie_dismiss(
            _evaluate,
            config,
            stage="enrich",
            target_id=t.get("id"),
            emit_event=lambda kind, **kw: _emit_event(kind, **kw),
        )

        # Try clicking through any "法人" / "業務提携" entry-point links
        # if the target hints at it (e.g. carradanote routes via #bloc-7)
        if t.get("entry_click_text"):
            for txt in t["entry_click_text"] if isinstance(t["entry_click_text"], list) else [t["entry_click_text"]]:
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
        snap = oc_browser("snapshot")

        # Save first form snapshot for debugging
        if i == 1:
            sample = DATA_DIR / "sample_form.txt"
            if snap:
                sample.write_text(snap)
                print(f"[enrich] saved first form snapshot -> {sample}")

        form_kind, form_reason = _classify_form_type(fields, snap)
        if form_kind != "contact":
            print(
                f"[enrich] ({i}/{len(targets)}) {t.get('name')}: "
                f"NON-CONTACT form ({form_kind}: {form_reason}) — adding to skip_history"
            )
            append_skip_history([
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "industry": t.get("industry"),
                    "draft": {
                        "body": f"non_contact_form: {form_kind} ({form_reason})"
                    },
                }
            ])
            enriched.append(
                {
                    **t,
                    "_enrich_skipped": f"non_contact_form:{form_kind}",
                    "_enrich_skip_reason": form_reason,
                }
            )
            _emit_event(
                "enrich.form.skipped_non_contact",
                stage="enrich",
                target_id=str(t.get("id") or ""),
                payload={"kind": form_kind, "reason": form_reason},
            )
            continue

        if fields.get("form_root_selector"):
            t = {**t, "form_root_selector": fields["form_root_selector"]}
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
        tid = str(t.get("id") or t.get("name") or i)
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
            **t,
            "form_fields": fields,
            "_enriched_at": datetime.utcnow().isoformat() + "Z",
        }
        enriched.append(enriched_entry)

    with out_path.open("w") as f:
        for e in enriched:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"[enrich] wrote {len(enriched)} enriched targets -> {out_path}")


# ============================================================================
# Stage: draft (Claude inference per target)
# ============================================================================

def build_system_block(config: dict[str, Any]) -> str:
    return core_prompt.build_system_block(config, PROMPTS_DIR)


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
        "## Target\n"
        "```json\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "```\n"
        "</user>\n"
    )


extract_first_json = core_prompt.extract_first_json


_REFINE_PROMPT_TEMPLATE = """You wrote the draft below for a Japanese B2B inquiry-form message.
Now act as a tough senior copywriter and **critique then rewrite** it,
**strictly following the persona spec embedded below** (this is persona v3).

## Persona spec (MUST follow — this is the source of truth)

{persona}

## Target context (the company you're writing to)
```json
{target_json}```

## Your draft
```json
{draft_json}```

## Critique checklist (apply strictly — persona-v3 enforcement)

**ハード制約（違反したら必ず書き直す）:**

A. **4セクション構成の保全** — 必ず次の順で構成されているか:
   Section 1: 挨拶+自己紹介 / Section 2: 課題仮説 / Section 3: 解決策+実績 / Section 4: 定型クロージング
   → セクション欠落・順序入れ替えがあれば書き直す。特に Section 3（弊社実績への
     ブリッジ）と Section 4（定型クロージング）の欠落を厳しく検知。

B. **Section 4 の定型文（一字一句保つ）** — 以下が **そのまま** 含まれているか:
   - 「よろしければ一度、オンラインでご提案のお時間を頂けないでしょうか。」
   - 「ご多忙の折、誠に恐縮ですが、何卒よろしくお願い申し上げます。」
   - 末尾の「カレンダー：https://tenbin.link/book/u-1302066f5d4f/torana-norimitsu-shida」
   → どれか欠けていたら **絶対に追加して書き直す**。URL 単独貼り（ラベルなし）も NG。

C. **質問形 CTA 禁止（NG #1）** — 本文に疑問符（？）または「〜いただけますか」
   「〜どうされていますか」「〜聞かせてください」のような問い掛けが含まれていないか
   → 含まれていたら削除して Section 4 の定型に置換する。

D. **「どうぞ」「ぜひどうぞ」「お声がけください」などの軽い口語 CTA 禁止（NG #2）**
   → Section 4 の定型に置換。

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

1. Section 2 の課題仮説は、相手企業の固有事実（IR数値・店舗数・新サービス等）を
   1〜2点引用しているか → していなければ enriched 情報から補う。
2. Section 3 は「肩書＋事例コピペ」になっていないか。事例から相手企業の論点への
   ブリッジが 1文添えられているか → なければ追加。
3. 本文は max_chars={max_chars} 以内に収まっているか。超えていたら削る。

## Output format (STRICT JSON)

```json
{{
  "critique": "<2-4 sentence critique of the original draft, in Japanese — どのハード制約に違反していたかを明示>",
  "subject": "<refined subject or null>",
  "body": "<refined body, ≤{max_chars} chars, persona-v3 完全準拠>"
}}
```

ハード制約 A〜J のいずれかに違反していた場合は **必ず** rewrite してください。
本当に元案が persona-v3 完全準拠で改善余地がなければ `critique` を「改善不要」、
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

    # Load persona spec so refine stays aligned with v3
    from _outreach_core.prompt import resolve_prompts_dir
    prompts_dir = resolve_prompts_dir(SKILL_DIR, config)
    persona_path = prompts_dir / "system_persona.md"
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
        )


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
    drafts = [json.loads(l) for l in input_path.open()]
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
) -> None:
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
        return

    print(f"\n{bar}\n[4-6/6] AUTONOMOUS SEND — hands-off (no per-item confirm)\n{bar}")
    ids = _resolve_ids_arg(None, True, drafts_path, cmd_name="campaign")
    if not ids:
        print("[campaign] no sendable drafts left to auto-send")
        return
    stage_send(drafts_path, ids, mode="auto", config=cfg, heartbeat="auto")

    # Auto-resolver pass (§16): the main batch never stopped on a hard form — it
    # queued those targets. Now that the browser is free, deep-resolve them in the
    # same hands-off run. No human reply needed.
    if core_resolve_queue.pending(DATA_DIR):
        print(f"\n{bar}\n[resolver] queued blockers → deep resolve pass\n{bar}")
        stage_resolve_queue(cfg)


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

    queue = core_resolve_queue.pending(DATA_DIR)
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
    hb = HeartbeatSession(SKILL_DIR, "resolve", len(queue), heartbeat="auto", data_dir=DATA_DIR)
    hb.start(f"deep-resolve {len(queue)} targets")
    resolved, skipped = [], []

    for i, entry in enumerate(queue, 1):
        tid = str(entry.get("target_id"))
        name = entry.get("name", tid)
        d = drafts_by_id.get(tid)
        if not d:
            core_resolve_queue.mark(DATA_DIR, tid, "skipped", note="draft not found")
            skipped.append(name)
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
        outcome = handle_verify_result(d, vres, DATA_DIR, channel="jp_form") if vres else "failed"
        if outcome == "sent_ok":
            append_sent_history([d])
            core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_SENT)
            core_resolve_queue.mark(DATA_DIR, tid, "resolved", note="deep resolver sent")
            print(f"  [resolver] ✅ {name} 送信成功")
            resolved.append(name)
        else:
            append_skip_history([{**d, "draft": {**d.get("draft", {}),
                                                 "body": f"RESOLVER_FAILED: {entry.get('reason_class')}"}}])
            core_resolve_queue.mark(DATA_DIR, tid, "skipped", note="deep resolver could not submit")
            print(f"  [resolver] ⏭ {name} 自動解決できず → skip 記録")
            skipped.append(name)
        if tab_id:
            _close_tab(tab_id)  # done with this target's errored tab
        hb.tick(i, f"{name} done")

    hb.end(f"resolve done · sent={len(resolved)} · skipped={len(skipped)}")
    msg = (f"🔧 リゾルバ完了: 自動送信 {len(resolved)} / スキップ {len(skipped)}\n"
           f"  送信: {('、'.join(resolved)) or 'なし'}\n"
           f"  スキップ: {('、'.join(skipped)) or 'なし'}")
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

        if clean:
            for f in ("leads.jsonl", "enriched.jsonl", "drafts.jsonl"):
                (DATA_DIR / f).unlink(missing_ok=True)
            print(f"[campaign] cleared previous run state")

        # --- Phase 1: Pull ---
        print(f"\n{bar}\n[1/6] PULL — bootstrap targets from {targets_path.name}\n{bar}")
        stage_bootstrap(
            targets_path,
            DATA_DIR / "leads.jsonl",
            include_sent=include_sent,
            limit=limit,
            only_ids=only_ids,
        )
        leads_n = sum(1 for line in (DATA_DIR / "leads.jsonl").open() if line.strip())
        if leads_n == 0:
            print(f"\n[campaign] no targets after pull — aborting")
            return
        print(f"\n[campaign] → {leads_n} targets pulled")

        # --- Phase 2: Enrich ---
        if skip_enrich:
            print(f"\n{bar}\n[2/6] ENRICH — SKIPPED (using --skip-enrich, leads passed through)\n{bar}")
            import shutil

            shutil.copy(DATA_DIR / "leads.jsonl", DATA_DIR / "enriched.jsonl")
        else:
            print(f"\n{bar}\n[2/6] ENRICH — form structure detection\n{bar}")
            stage_enrich(DATA_DIR / "leads.jsonl", DATA_DIR / "enriched.jsonl", config=cfg)

        enriched_n = sum(1 for line in (DATA_DIR / "enriched.jsonl").open() if line.strip())
        if enriched_n == 0:
            print(f"\n[campaign] no enriched targets — aborting")
            return
        print(f"\n[campaign] → {enriched_n} targets enriched")

        # --- Phase 3: Personalize ---
        print(f"\n{bar}\n[3/6] PERSONALIZE — Opus draft (cached system prompt)\n{bar}")
        stage_draft(DATA_DIR / "enriched.jsonl", DATA_DIR / "drafts.jsonl", cfg, refine=refine)

        drafts = [json.loads(l) for l in (DATA_DIR / "drafts.jsonl").open() if l.strip()]
        sendable = [d for d in drafts if (d.get("draft") or {}).get("subject") != "SKIP"]
        skipped = len(drafts) - len(sendable)
        print(
            f"\n[campaign] → {len(sendable)} sendable, {skipped} SKIP "
            + f"(send rate {len(sendable) * 100 // len(drafts) if drafts else 0}%)"
        )

        # --- Phases 4-6: Approve → Send → Log ---
        if skip_send:
            print(f"\n{bar}\n[4/6] PREVIEW — display only (send skipped)\n{bar}")
            stage_preview(DATA_DIR / "drafts.jsonl", interactive_send=False, config=cfg)
            print(f"\n[campaign] stopped at preview. To send later: python run.py send --ids ...")
            return

        # --- Autonomous path: lock quality once upfront, then run hands-off ---
        if core_autonomy.is_autonomous(cfg):
            _run_autonomous_send(DATA_DIR / "drafts.jsonl", cfg, sendable)
            if slack_ch:
                touch_last_used(slack_ch)
            return

        print(f"\n{bar}\n[4-6/6] APPROVE → SEND → LOG (interactive)\n{bar}")
        stage_preview(DATA_DIR / "drafts.jsonl", interactive_send=True, config=cfg)

        if slack_ch:
            touch_last_used(slack_ch)


# ============================================================================
# Stage: send (drive form fill + submit)
# ============================================================================

# Generic "find field by label pattern" — used for sender info fields
# (氏名, ふりがな, 会社名, 電話, メール, etc.)
SENDER_FIELD_PATTERNS = {
    "name": [r"お?名前", r"氏名", r"name", r"担当者", r"ご担当"],
    "name_kana": [r"フリガナ", r"カナ", r"kana", r"katakana"],
    "name_furigana": [r"ふりがな", r"furigana", r"hiragana"],
    "name_sei": [r"姓"],
    "name_mei": [r"名$"],
    "name_kana_sei": [r"セイ"],
    "name_kana_mei": [r"メイ"],
    "company": [r"会社名", r"法人名", r"団体名", r"貴社", r"御社", r"company"],
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
      const l = document.querySelector(`label[for="${el.id}"]`);
      if (l) return (l.textContent || '').trim();
    }
    const wrap = el.closest('label');
    if (wrap) return (wrap.textContent || '').trim();
    let cur = el.parentElement;
    for (let i = 0; i < 4 && cur; i++, cur = cur.parentElement) {
      const l = cur.querySelector('label, .label');
      if (l) return (l.textContent || '').trim();
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


_CHECK_AGREEMENT_JS = r"""
() => {
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${el.id}"]`);
      if (l) return (l.textContent || '').trim();
    }
    const wrap = el.closest('label');
    if (wrap) return (wrap.textContent || '').trim();
    return '';
  };
  for (const cb of document.querySelectorAll('input[type="checkbox"]')) {
    const lbl = labelFor(cb);
    if (/同意|プライバシー|利用規約|個人情報|送信する$/i.test(lbl)) {
      if (!cb.checked) {
        cb.checked = true;
        cb.dispatchEvent(new Event('change', { bubbles: true }));
        cb.dispatchEvent(new Event('click', { bubbles: true }));
      }
      return { checked: true, label: lbl };
    }
  }
  return { checked: false };
}
"""


_CLICK_BUTTON_BY_TEXT_JS = r"""
(args) => {
  const { patterns, formRootSelector } = args;
  const selector = 'button, input[type="submit"], input[type="button"], a, [role="button"]';
  let scopes = [];
  if (formRootSelector) {
    try {
      const root = document.querySelector(formRootSelector);
      if (root) scopes.push(root);
    } catch (e) {}
  }
  scopes.push(document);
  for (const scope of scopes) {
    const buttons = Array.from(scope.querySelectorAll(selector));
    for (const pat of patterns) {
      const re = new RegExp(pat);
      for (const b of buttons) {
        const rawTxt = ((b.textContent || b.value || '') + ' ' + (b.getAttribute('aria-label') || '')).trim();
        const txt = rawTxt.replace(/\s+/g, ' ');
        // Skip elements with too much text (likely a wrapper, not a button)
        if (txt.length > 80) continue;
        if (re.test(txt) && !b.disabled && b.offsetParent !== null) {
          b.click();
          return { clicked: true, text: txt.slice(0, 50), scope: scope === document ? 'document' : 'form' };
        }
      }
    }
  }
  return { clicked: false };
}
"""


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


def _check_agreement() -> dict[str, Any] | None:
    res = _evaluate(_CHECK_AGREEMENT_JS)
    return res if isinstance(res, dict) else None


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


_ENUMERATE_BUTTONS_JS = r"""
(args) => {
  const { formRootSelector } = args;
  const selector = 'button, input[type="submit"], input[type="button"], a, [role="button"]';
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
    if (b.disabled || b.offsetParent === null) return;
    const rawTxt = ((b.textContent || b.value || '') + ' ' + (b.getAttribute('aria-label') || '')).trim();
    const txt = rawTxt.replace(/\s+/g, ' ');
    if (!txt || txt.length > 80) return;
    out.push({
      idx,
      text: txt,
      tag: b.tagName.toLowerCase(),
      type: b.getAttribute('type') || '',
      role: b.getAttribute('role') || '',
      href: b.getAttribute('href') || '',
    });
  });
  return { scope: scope === document ? 'document' : 'form', buttons: out };
}
"""


def _enumerate_buttons(form_root_selector: str | None = None) -> list[dict[str, Any]]:
    args = {"formRootSelector": form_root_selector}
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


def _click_by_exact_text(text: str,
                          form_root_selector: str | None = None) -> dict[str, Any] | None:
    pat = "^" + re.escape(text).replace(r"\ ", r"\s*") + "$"
    return _click_button([pat], form_root_selector=form_root_selector)


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

## 出力（JSONのみ）
{{"text": "<選んだボタンの text フィールドをそのまま>", "reason": "<簡潔な根拠>"}}
"""


def _llm_pick_final_submit(buttons: list[dict[str, Any]],
                            config: dict[str, Any]) -> dict[str, Any] | None:
    if not buttons:
        return None
    prompt = _FINAL_SUBMIT_PICKER_PROMPT.format(
        buttons_json=json.dumps(buttons, ensure_ascii=False, indent=2),
    )
    model_cfg = config.get("model", {}) or {}
    model = model_cfg.get("form_analyzer_name") or model_cfg.get("name", DEFAULT_MODEL)
    response = oc_infer(prompt, model=model)
    result = extract_first_json(response or "")
    if isinstance(result, dict) and result.get("text"):
        return result
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

## Output schema (STRICT JSON)

```json
{{
  "fields": [
    {{"name": "<element name or id>", "action": "set_text" | "select_option" | "select_radio" | "skip",
      "value": "<value to set, or __BODY__ for the message body>",
      "reason": "<short why>"}}
  ],
  "checkboxes_to_check": ["<element name or id>"],
  "first_button_pattern": "<regex for the first submit button, e.g. '入力内容を確認' or '送信'>",
  "next_step": "single" | "confirm",
  "warnings": ["<diagnostic>"]
}}
```

## Rules

1. Map each visible field to a value from sender / overrides
2. For split 姓/名 fields: name_sei = first 2 chars of sender.name, name_mei = remainder
3. For カナ/カタカナ fields: use sender.name_kana split similarly if needed
4. For ふりがな/ひらがな fields: use sender.name_furigana
5. For メール確認用: re-use sender.email
6. For 電話番号 split into 3 fields: split sender.phone (no hyphens) at positions 3 and 7
7. For 郵便番号 split into 2: first 3 / last 4
8. For 都道府県 select: use action="select_option", value=sender.prefecture
9. For 市区町村 / 番地 split: use sender.city + sender.address_line + sender.building
10. For body textarea: action="set_text", value="__BODY__"
11. For category/お問い合わせ種別 radios: use overrides.category_radio
12. For category selects: use overrides.category_select
13. For 性別 radios: use overrides.gender_radio if set
14. For ご希望の連絡方法 radios: use overrides.contact_method_radio
15. For 連絡可能な時間帯 radios: use overrides.contact_time_radio
16. For 同意 / プライバシー / 利用規約 / 個人情報 checkboxes: add to checkboxes_to_check
17. For optional fields like FAX, ニュースレター, 当社をどこで知ったか when no override: action="skip"
18. For 従業員数 select with override.employee_count_required: use sender.employee_count_band
19. Prefer `name` attribute as the field identifier; fall back to `id`
20. If the form has multiple submit-like buttons, the FIRST one (e.g. "入力内容を確認する") goes in `first_button_pattern`
21. If the flow is single-step (just one Send button), set next_step="single", else "confirm"
22. Add warnings for tricky cases (e.g. "postal code lookup may overwrite city field — fill postal LAST")

Output the JSON only, no prose."""


def _llm_analyze_form(target: dict[str, Any], config: dict[str, Any],
                       body_max_chars: int = 400) -> dict[str, Any] | None:
    """
    Use Sonnet to plan how to fill a target's form. Returns plan dict or None.
    Plan is cached on the target as `_llm_plan` once analyzed.
    """
    if target.get("_llm_plan"):
        return target["_llm_plan"]

    form_fields = target.get("form_fields") or {}
    if not form_fields or not (form_fields.get("inputs")
                                or form_fields.get("textareas")
                                or form_fields.get("selects")):
        return None  # No fields to analyze (e.g. iframe form)

    sender = config.get("sender", {})
    overrides = target.get("field_map_overrides", {}) or {}

    prompt = _FORM_ANALYZER_PROMPT_TEMPLATE.format(
        sender_yaml=yaml.safe_dump(sender, allow_unicode=True, sort_keys=False),
        overrides_yaml=yaml.safe_dump(overrides, allow_unicode=True, sort_keys=False) if overrides else "{}\n",
        body_max_chars=body_max_chars,
        form_fields_json=json.dumps(form_fields, ensure_ascii=False, indent=2),
    )

    model_cfg = config.get("model", {}) or {}
    model = model_cfg.get("form_analyzer_name") or model_cfg.get("name", DEFAULT_MODEL)
    response = oc_infer(prompt, model=model)
    plan = extract_first_json(response or "")
    if not plan or "fields" not in plan:
        return None
    target["_llm_plan"] = plan
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
    setter.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };

  const findEl = (selectorAttr, cssSelector) => {
    if (cssSelector) {
      try {
        const byCss = document.querySelector(cssSelector);
        if (byCss) return byCss;
      } catch (e) {}
    }
    let el = document.querySelector(`[name="${selectorAttr}"]`);
    if (el) return el;
    el = document.querySelector(`#${CSS.escape(selectorAttr)}`);
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
        sel.value = opt.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        return { ok: true, action, name, value: opt.text };
      }
    }
    return { ok: false, reason: "option not found", name, value };
  }

  if (action === "select_radio") {
    // For radios, "name" is the radio group name and "value" is which option
    const radios = document.querySelectorAll(`input[type="radio"][name="${name}"]`);
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
  let cb = document.querySelector(`input[type="checkbox"][name="${name}"]`);
  if (!cb) cb = document.querySelector(`input[type="checkbox"]#${CSS.escape(name)}`);
  if (!cb) return { ok: false, reason: "checkbox not found", name };
  if (!cb.checked) {
    cb.checked = true;
    cb.dispatchEvent(new Event('change', { bubbles: true }));
    cb.dispatchEvent(new Event('click', { bubbles: true }));
  }
  return { ok: true, name };
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
        cb.checked = true;
        cb.dispatchEvent(new Event('change', { bubbles: true }));
        cb.dispatchEvent(new Event('click', { bubbles: true }));
      }
      return { ok: true, label, text: txt.slice(0, 60) };
    }
  }
  return { ok: false, reason: "no checkbox with label", label };
}
"""


def _apply_field_action(
    name: str, action: str, value: str, selector: str | None = None
) -> dict[str, Any] | None:
    args = {"name": name, "action": action, "value": value, "selector": selector or ""}
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
    from _outreach_core.notify import post as notify_post

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
    notify_post(f"{name} 想定外の必須項目（動的）: {labels}", level="warn")
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
    name = entry.get("name")
    action = entry.get("action")
    value = entry.get("value", "")
    selector = entry.get("selector")
    if not name or not action:
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

    for cb_name in plan.get("checkboxes_to_check", []):
        res = _check_by_name(cb_name)
        if res and res.get("ok"):
            diag["filled"].append(f"checkbox:{cb_name}")
        else:
            diag["errors"].append(f"checkbox {cb_name}: {(res or {}).get('reason','?')}")

    return diag


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
    plan_diag = None
    if plan:
        target["_llm_plan"] = plan
        tid = str(target.get("id") or "?")
        _emit_event(
            "send.plan.generated",
            stage="send",
            target_id=tid,
            payload={
                "field_count": len(plan.get("fields") or []),
                "checkboxes_count": len(plan.get("checkboxes_to_check") or []),
                "flow": plan.get("next_step"),
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
        plan_diag = fill_form_with_plan(plan, body, target=target, evaluate_fn=_evaluate)
        _emit_event(
            "send.fill.applied",
            stage="send",
            target_id=tid,
            payload={
                "filled": len(plan_diag.get("filled") or []),
                "errors": len(plan_diag.get("errors") or []),
                "skipped": len(plan_diag.get("skipped") or []),
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
            plan2 = _llm_analyze_form(target, config, body_max_chars=char_limit)
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

    # Sender field fill (multi-shot for split fields)
    fills = [
        # Try split-name first
        ("name_sei", sender["name"][:2], SENDER_FIELD_PATTERNS["name_sei"]),
        ("name_mei", sender["name"][2:], SENDER_FIELD_PATTERNS["name_mei"]),
        ("name_kana_sei", sender["name_kana"][:2], SENDER_FIELD_PATTERNS["name_kana_sei"]),
        ("name_kana_mei", sender["name_kana"][2:], SENDER_FIELD_PATTERNS["name_kana_mei"]),
        # Then full-name fallbacks
        ("name", sender["name"], SENDER_FIELD_PATTERNS["name"]),
        ("name_kana", sender["name_kana"], SENDER_FIELD_PATTERNS["name_kana"]),
        ("name_furigana", sender["name_furigana"], SENDER_FIELD_PATTERNS["name_furigana"]),
        ("company", sender["company"], SENDER_FIELD_PATTERNS["company"]),
        ("role", sender["role"], SENDER_FIELD_PATTERNS["role"]),
        ("email", sender["email"], SENDER_FIELD_PATTERNS["email"]),
        ("email_confirm", sender["email"], SENDER_FIELD_PATTERNS["email_confirm"]),
        ("phone", phone_value, SENDER_FIELD_PATTERNS["phone"]),
        ("postal_code", postal_value, SENDER_FIELD_PATTERNS["postal_code"]),
        ("city", f"{sender['city']}{sender['address_line']}", SENDER_FIELD_PATTERNS["city"]),
        ("address_line", sender["address_line"], SENDER_FIELD_PATTERNS["address_line"]),
        ("building", sender["building"], SENDER_FIELD_PATTERNS["building"]),
        ("address_full", sender["full_address"], SENDER_FIELD_PATTERNS["address_full"]),
    ]
    for kind, value, patterns in fills:
        res = _fill_field(kind, value, patterns)
        if res and res.get("filled"):
            diagnostics["filled"].append(f"{kind}={value[:30]} (label={res.get('label')})")
            time.sleep(0.2)
        else:
            diagnostics["unfilled"].append(kind)

    # Prefecture select (separate handling)
    pref_res = _fill_select(sender["prefecture"], label_pattern=r"都道府県|prefecture")
    if pref_res and pref_res.get("selected"):
        diagnostics["filled"].append(f"prefecture={sender['prefecture']}")

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
        sres = _fill_select(sender["employee_count_band"], label_pattern=r"従業員")
        if sres and sres.get("selected"):
            diagnostics["filled"].append(f"employee_count={sender['employee_count_band']}")
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

    # Agreement checkbox
    cres = _check_agreement()
    if cres and cres.get("checked"):
        diagnostics["filled"].append(f"agreement_checkbox ({cres.get('label')[:30]})")

    return diagnostics


_WRONG_FORM_WARN_KW = (
    "会員登録", "アカウント作成", "新規登録", "ログイン",
    "採用", "応募", "求人", "履歴書",
    "予約", "予約フォーム", "資料請求",
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

    for w in warnings:
        w_str = str(w)
        for kw in _WRONG_FORM_WARN_KW:
            if kw in w_str:
                return f"warning mentions '{kw}'"

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


def _escalate_await_proceed(target: dict[str, Any], reason: str) -> None:
    """Record needs_attention + Slack notify; browser stays open for resolve --action proceed."""
    from _outreach_core.notify import post as notify_post

    tid = target.get("id", "?")
    name = target.get("name", "?")
    append_needs_attention(
        DATA_DIR,
        {
            "target_id": tid,
            "name": name,
            "channel": "jp_form",
            "reason": reason,
            "action_needed": "proceed",
        },
    )
    # Truthful messaging: state the ACTUAL blocker reason. Previously this always
    # said "reCAPTCHA / 確認待ち" even when the real cause was a missing submit
    # button or wrong form — which made non-captcha failures look like captcha
    # detections (see production logs / オリヒロ false positive).
    short = _humanize_blocker_reason(reason)
    notify_post(
        f"⚠️ {name}: {short}。Slack で「{tid} 進めて」と返すと手動再開できます。\n  詳細: {reason}",
        level="warn",
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


def _config_with_warmup_sec(config: dict[str, Any] | None, warm_sec: int) -> dict[str, Any]:
    """Shallow copy of config with captcha.warmup_sec overridden (adaptive warmup)."""
    cfg = dict(config or {})
    captcha_block = dict(cfg.get("captcha") or {})
    captcha_block["warmup_sec"] = warm_sec
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
        payload = core_infer.oc_browser_json("open", url)
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


def _list_tabs_payload() -> Any:
    try:
        return core_infer.oc_browser_json("tabs")
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
    from _outreach_core.notify import post as notify_post

    tid = str(d.get("id") or d.get("name") or "?")
    name = d.get("name", "?")
    diag = _blocker_diagnostics(d, trace=trace)
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
        },
    )
    try:
        notify_post(core_resolve_queue.build_actionable_message(entry, auto_resolver=autonomous),
                    level="warn")
    except Exception:  # noqa: BLE001
        pass
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

    patterns = [
        r"^送信する$", r"^送信$", r"この内容で送信", r"内容を送信する", r"送信する",
        r"問い合わせを送信", r"お問い合わせを送信", r"上記の内容で送信", r"submit", r"完了", r"確定",
        r"入力内容を確認", r"内容(を|の)?確認", r"確認する",
    ]
    cr = _click_button(patterns, form_root_selector=d.get("form_root_selector"))
    if not cr or not cr.get("clicked"):
        buttons = _enumerate_buttons()
        if buttons:
            pick = _llm_pick_final_submit(buttons, config or {})
            if pick and pick.get("text"):
                cr = _click_by_exact_text(pick["text"])
    if not cr or not cr.get("clicked"):
        return {"vresult": None, "page_text": "", "status": "no_button"}
    time.sleep(3)
    # If this was an input page (confirm flow), a confirm page may now appear.
    if flow == "confirm":
        c2 = _click_button([
            r"^送信する$", r"^送信$", r"この内容で送信", r"内容を送信する",
            r"submit", r"完了", r"確定", r"問い合わせを送信",
        ])
        if c2 and c2.get("clicked"):
            time.sleep(5)
    time.sleep(2)
    page_evidence = _evaluate(PAGE_EVIDENCE_JS)
    snap = oc_browser("snapshot")
    combined = snap or ""
    if isinstance(page_evidence, dict):
        combined = f"{combined}\n{page_evidence.get('text', '')}\n{page_evidence.get('url', '')}"
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

    oc_browser("open", form_url)
    time.sleep(RATE_LIMIT_SECONDS)
    apply_cookie_dismiss(
        _evaluate, config, stage="send", target_id=tid,
        emit_event=lambda kind, **kw: _emit_event(kind, trace_dir=trace, **kw),
    )
    if d.get("entry_click_text"):
        for txt in (d["entry_click_text"] if isinstance(d["entry_click_text"], list) else [d["entry_click_text"]]):
            _click_button([re.escape(txt)])
            time.sleep(1.5)

    fill_form_for_target(d, config, sanitized_body, trace_dir=trace, iterative_fill=iterative_fill)
    time.sleep(1.0)

    plan = d.get("_llm_plan") or {}
    if flow == "confirm":
        patterns = [r"入力内容を確認", r"内容(を|の)?確認", r"確認する", r"確認$", r"同意して次へ", r"^次へ$"]
    else:
        patterns = [r"送信する", r"^送信$", r"submit", r"内容を送信"]
    if plan.get("first_button_pattern"):
        patterns = patterns + [plan["first_button_pattern"]]
    cr = _click_button(patterns, form_root_selector=d.get("form_root_selector"))
    if not cr or not cr.get("clicked"):
        buttons = _enumerate_buttons(form_root_selector=d.get("form_root_selector"))
        if buttons:
            pick = _llm_pick_final_submit(buttons, config or {})
            if pick and pick.get("text"):
                cr = _click_by_exact_text(pick["text"])
    if not cr or not cr.get("clicked"):
        return {"vresult": None, "page_text": ""}
    time.sleep(3)

    if flow == "confirm":
        c2 = _click_button([
            r"^送信する$", r"^送信$", r"この内容で送信", r"内容を送信する",
            r"上記の内容で送信", r"submit", r"完了", r"確定", r"問い合わせを送信",
        ])
        if not c2 or not c2.get("clicked"):
            buttons = _enumerate_buttons()
            if buttons:
                pick = _llm_pick_final_submit(buttons, config or {})
                if pick and pick.get("text"):
                    _click_by_exact_text(pick["text"])
        time.sleep(5)

    time.sleep(2)
    page_evidence = _evaluate(PAGE_EVIDENCE_JS)
    snap = oc_browser("snapshot")
    combined = snap or ""
    if isinstance(page_evidence, dict):
        combined = f"{combined}\n{page_evidence.get('text', '')}\n{page_evidence.get('url', '')}"
    vresult = verify_send_completed(
        d, "jp_form", snapshot=combined,
        browser_verify=page_evidence if isinstance(page_evidence, dict) else None,
        plan=d.get("_llm_plan"), evaluate_fn=_evaluate, data_dir=DATA_DIR,
        snapshot_path=None, verify_strict=verify_strict,
    )
    return {"vresult": vresult, "page_text": combined}


def stage_send(
    input_path: Path,
    ids: set[int],
    mode: str = "interactive",
    config: dict[str, Any] | None = None,
    heartbeat: str | None = None,
    verify_strict: bool = True,
    iterative_fill: bool = False,
) -> None:
    if not config:
        print("[send] missing config", file=sys.stderr)
        return

    drafts = [json.loads(l) for l in input_path.open()]
    sendable = [d for d in drafts if d.get("draft", {}).get("subject") != "SKIP"]
    if not sendable:
        print("[send] no sendable drafts")
        return

    targets = [d for i, d in enumerate(sendable, 1) if i in ids]
    if not targets:
        print(f"[send] no matching ids; max={len(sendable)}")
        return

    sent_ids = load_sent_set()
    pre_filtered = [d for d in targets if d["id"] in sent_ids]
    if pre_filtered:
        names = ", ".join(d.get("name", "?") for d in pre_filtered)
        print(f"[send] ⚠ skipping {len(pre_filtered)} already in sent_history: {names}")
        targets = [d for d in targets if d["id"] not in sent_ids]
        if not targets:
            return

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

    sent: list[dict[str, Any]] = []
    filled_only: list[dict[str, Any]] = []
    tab_isolation = _tab_isolation_enabled(config)
    resolver_tab_ids: set[str] = set()  # error tabs kept open for the resolver
    hb = HeartbeatSession(SKILL_DIR, "send", len(targets), heartbeat=heartbeat, data_dir=DATA_DIR)
    hb.start(f"send {len(targets)} targets")

    for di, d in enumerate(targets):
        idx = sendable.index(d) + 1
        name = d.get("name", "?")
        body = d["draft"]["body"]
        form_url = d["form_url"]
        flow = d.get("flow") or (d.get("_llm_plan") or {}).get("next_step") or "single"
        captcha = (d.get("form_fields") or {}).get("has_recaptcha_v2") or d.get("captcha") == "recaptcha_v2_visible"

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
                continue

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
            continue

        # 0. reCAPTCHA v3 warm-up (§11-A-8 + §3.5 avoidance): visit root domain +
        #    dispatch natural interactions before navigating to the form. Warmup
        #    duration is ADAPTIVE — bumped for domains previously challenged.
        from _outreach_core.warmup import apply_warmup_if_enabled

        warm_sec = core_avoidance.recommended_warmup_sec(DATA_DIR, form_url, config)
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

        pre_snap = oc_browser("snapshot")
        ev.dump_trace(trace, "form_snapshot_pre.txt", pre_snap or "")

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

        # 3. Fill all fields
        diagnostics = fill_form_for_target(
            d, config, send_body, trace_dir=trace, iterative_fill=iterative_fill
        )
        print(f"  [send] filled: {len(diagnostics['filled'])} / unfilled: {len(diagnostics['unfilled'])} / errors: {len(diagnostics['errors'])}")
        for f in diagnostics["filled"][:8]:
            print(f"    ✓ {f}")
        if diagnostics["errors"]:
            for e in diagnostics["errors"]:
                print(f"    ✗ {e}")

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
            continue

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
                "counts": cap_state.get("counts", {}),
            },
            trace_dir=trace,
        )
        if captcha and not cap_state["blocking"]:
            print(f"  [send] captcha 再確認: enrich={captcha} だが live={cap_state['kind']} "
                  f"（非ブロッキング）→ 続行")
        if cap_state["blocking"]:
            label = core_captcha.reason_label(cap_state)
            print(f"  [send] ⚠ {label} — 突破せず迂回/記録（完全自律・人手なし方針）")
            core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_CAPTCHA_BLOCKED)
            filled_only.append(d)
            if autonomous:
                _auto_skip_and_log(d, f"captcha_blocking: {cap_state['kind']} ({label})")
            else:
                _escalate_await_proceed(d, f"reCAPTCHA visible challenge: {cap_state['kind']}")
            continue

        if mode == "fill-only":
            print(f"  [send] ✓ filled. Click 確認/送信 manually.")
            filled_only.append(d)
            continue

        # 4. Click first submit button (確認 for confirm-flow, 送信 for single)
        time.sleep(1.0)
        plan = d.get("_llm_plan") or {}
        plan_flow = plan.get("next_step")
        if plan_flow and plan_flow != flow:
            flow = plan_flow
        if flow == "confirm":
            patterns = [
                r"入力内容を確認", r"送信内容を確認", r"内容(を|の)?確認",
                r"確認画面", r"確認する", r"確認$", r"内容の確認へ",
                r"同意して次へ", r"^次へ$", r"次のステップへ",
            ]
        else:
            patterns = [r"送信する", r"^送信$", r"submit", r"内容を送信", r"同意して.*送信"]
        plan_first = plan.get("first_button_pattern")
        if plan_first and plan_first not in patterns:
            patterns = patterns + [plan_first]

        click_res = _click_button(patterns, form_root_selector=d.get("form_root_selector"))
        if not click_res or not click_res.get("clicked"):
            # Strengthened detection (§3.5): before declaring failure, enumerate
            # the form's buttons and let the LLM pick the most likely confirm/submit
            # control — the same fallback the FINAL submit already had. Many
            # "first submit not found" cases (e.g. オリヒロ) were mislabeled as
            # reCAPTCHA when the button simply didn't match the regex list.
            print(f"  [send] first submit button regex miss — LLM picker fallback")
            buttons = _enumerate_buttons(form_root_selector=d.get("form_root_selector"))
            if buttons:
                pick = _llm_pick_final_submit(buttons, config or {})
                if pick and pick.get("text"):
                    print(f"  [send] LLM picked first: {pick.get('text')} ({pick.get('reason','')[:60]})")
                    click_res = _click_by_exact_text(pick["text"])
                    _emit_event(
                        "send.first.llm_pick", stage="send", target_id=tid,
                        payload={
                            "picked_text": pick.get("text"),
                            "clicked": bool(click_res and click_res.get("clicked")),
                            "candidates": len(buttons),
                        },
                        trace_dir=trace,
                    )
        if not click_res or not click_res.get("clicked"):
            print(f"  [send] ⚠ first submit button not found (patterns={patterns})")
            filled_only.append(d)
            _emit_event(
                "send.first_button_missing",
                stage="send",
                target_id=tid,
                payload={"patterns": patterns, "flow": flow},
                trace_dir=trace,
            )
            core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_SUBMIT_NOT_FOUND)
            if cur_tab_id:
                resolver_tab_ids.add(cur_tab_id)
            _queue_for_resolver(
                d, "first_submit_not_found",
                f"first submit button not found (flow={flow})",
                trace=trace, autonomous=autonomous, tab_id=cur_tab_id,
            )
            continue
        print(f"  [send] clicked: {click_res.get('text')}")
        _emit_event(
            "send.button.clicked",
            stage="send",
            target_id=tid,
            payload={
                "pattern_matched": True,
                "text": (click_res.get("text") or "")[:80],
            },
            trace_dir=trace,
        )
        time.sleep(3)

        # 5. If confirm flow, click final submit
        if flow == "confirm":
            _emit_event(
                "send.confirm.reached",
                stage="send",
                target_id=tid,
                payload={"wait_user_ms": 0},
                trace_dir=trace,
            )
            click2 = _click_button([
                r"^送信する$", r"^送信$", r"この内容で送信",
                r"内容を送信する", r"上記の内容で送信", r"^回答送信$",
                r"^送る$", r"submit", r"完了", r"確定",
                r"内容で.*送信", r"^以上の内容", r"内容を以上で",
                r"上記内容を送信", r"問い合わせを送信", r"お問い合わせを送信",
            ])
            if not click2 or not click2.get("clicked"):
                print(f"  [send] ⚠ final submit not matched by patterns — falling back to LLM picker")
                buttons = _enumerate_buttons()
                if buttons:
                    pick = _llm_pick_final_submit(buttons, config or {})
                    if pick and pick.get("text"):
                        print(f"  [send] LLM picked: {pick.get('text')} ({pick.get('reason','')[:60]})")
                        click2 = _click_by_exact_text(pick["text"])
                        _emit_event(
                            "send.final.llm_pick", stage="send", target_id=tid,
                            payload={
                                "picked_text": pick.get("text"),
                                "clicked": bool(click2 and click2.get("clicked")),
                                "candidates": len(buttons),
                            },
                            trace_dir=trace,
                        )
                if not click2 or not click2.get("clicked"):
                    print(f"  [send] ⚠ final submit still not clickable after LLM fallback — awaiting proceed")
                    if trace:
                        ev.dump_trace(
                            trace, "form_snapshot_confirm.txt",
                            oc_browser("snapshot") or "",
                        )
                    filled_only.append(d)
                    core_avoidance.record_outcome(DATA_DIR, form_url, core_avoidance.OUTCOME_SUBMIT_NOT_FOUND)
                    if cur_tab_id:
                        resolver_tab_ids.add(cur_tab_id)
                    _queue_for_resolver(
                        d, "confirm_submit_not_found",
                        "confirm-page final submit not found",
                        trace=trace, autonomous=autonomous, tab_id=cur_tab_id,
                    )
                    continue
            print(f"  [send] clicked final: {click2.get('text')}")
            _emit_event(
                "send.final.clicked",
                stage="send",
                target_id=tid,
                payload={"text": (click2.get("text") or "")[:80]},
                trace_dir=trace,
            )
            time.sleep(5)

        # 6. Verify send (keywords, required fields, plan gaps)
        time.sleep(2)
        ev.dump_trace(trace, "form_snapshot_post.txt", oc_browser("snapshot") or "")
        from _outreach_core.verify import PAGE_EVIDENCE_JS

        page_evidence = _evaluate(PAGE_EVIDENCE_JS)
        snap = oc_browser("snapshot")
        snap_path = DATA_DIR / f"verify_snapshot_{d.get('id', di)}.txt"
        combined = snap or ""
        if isinstance(page_evidence, dict):
            combined = f"{combined}\n{page_evidence.get('text', '')}\n{page_evidence.get('url', '')}"
        if combined.strip():
            snap_path.write_text(combined, encoding="utf-8")

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
            )
            ev.dump_trace(trace, "verify_evidence.json", vresult.get("evidence") or {})
            outcome = handle_verify_result(d, vresult, DATA_DIR, channel="jp_form")
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
                    print(f"  [send] ⚠ verify: {vresult.get('status')} — {vresult.get('reason')}")
                    filled_only.append(d)
        else:
            filled_only.append(d)

        hb.tick(di + 1, f"{name} · verify done")

        if di < len(targets) - 1:
            print(f"  [send] sleeping 30s before next...")
            time.sleep(30)

    hb.end(f"send done · sent={len(sent)} · pending={len(filled_only)}")
    if sent:
        append_sent_history(sent)
    print(f"\n[send] done · sent={len(sent)} · filled-only={len(filled_only)}")
    if filled_only:
        names = ", ".join(d.get("name", "?") for d in filled_only)
        print(f"[send] not auto-logged: {names}")
        ids_str = ",".join(str(sendable.index(d) + 1) for d in filled_only)
        print(f"[send] If you completed any of those manually:")
        print(f"      python run.py mark-sent --ids {ids_str}")


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
    click_res = _click_button(patterns)
    if not click_res or not click_res.get("clicked"):
        print("[resolve] ⚠ submit button not found on current page", file=sys.stderr)
        _escalate_await_proceed(d, "awaiting_user_proceed: submit button not found on resume")
        return

    print(f"[resolve] clicked: {click_res.get('text')}")
    time.sleep(5)

    from _outreach_core.verify import PAGE_EVIDENCE_JS

    page_evidence = _evaluate(PAGE_EVIDENCE_JS)
    snap = oc_browser("snapshot")
    combined = snap or ""
    if isinstance(page_evidence, dict):
        combined = f"{combined}\n{page_evidence.get('text', '')}\n{page_evidence.get('url', '')}"
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
    print(f"[mark-sent] logged {len(sent)}: " + ", ".join(d.get("name", "?") for d in sent))


# ============================================================================
# CLI
# ============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(prog="jp-form-outreach", description=__doc__)
    brief_parent = argparse.ArgumentParser(add_help=False)
    brief_parent.add_argument(
        "--brief",
        default=None,
        help="Brief id (default: briefs/_active.txt)",
    )
    ap.add_argument(
        "--brief",
        default=None,
        help="Brief id (default: briefs/_active.txt or DOORMAN_SLACK_CHANNEL_ID)",
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

    args = ap.parse_args()
    import os

    if getattr(args, "slack_channel_id", None):
        os.environ["DOORMAN_SLACK_CHANNEL_ID"] = args.slack_channel_id
    if getattr(args, "slack_thread_ts", None):
        os.environ["DOORMAN_SLACK_THREAD_TS"] = args.slack_thread_ts
    brief_id = getattr(args, "brief", None)
    try:
        configure_brief(brief_id, cmd=args.cmd)
    except BriefError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

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
