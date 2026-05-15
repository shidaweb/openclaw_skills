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
from typing import Any

try:
    import yaml
except ImportError:
    print("Missing dependency: pyyaml. Install with `pip install pyyaml`", file=sys.stderr)
    sys.exit(1)

SKILL_DIR = Path(__file__).resolve().parent
DATA_DIR = SKILL_DIR / "data"
PROMPTS_DIR = SKILL_DIR / "prompts"
DATA_DIR.mkdir(exist_ok=True)

DEFAULT_MODEL = "claude-cli/claude-sonnet-4-6"
BROWSER_PROFILE = "openclaw"
RATE_LIMIT_SECONDS = 4

SKIP_HISTORY_PATH = DATA_DIR / "skip_history.jsonl"
SENT_HISTORY_PATH = DATA_DIR / "sent_history.jsonl"


# ============================================================================
# History helpers (mirrors linkedin-outreach pattern)
# ============================================================================

def _load_id_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    for line in path.open():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if "id" in entry:
                ids.add(entry["id"])
        except Exception:
            continue
    return ids


def load_skip_set() -> set[str]:
    return _load_id_set(SKIP_HISTORY_PATH)


def load_sent_set() -> set[str]:
    return _load_id_set(SENT_HISTORY_PATH)


def append_skip_history(skipped_drafts: list[dict[str, Any]]) -> None:
    if not skipped_drafts:
        return
    now = datetime.utcnow().isoformat() + "Z"
    with SKIP_HISTORY_PATH.open("a") as f:
        for d in skipped_drafts:
            reason_full = (d.get("draft") or {}).get("body") or ""
            reason = reason_full.replace("INSUFFICIENT_DATA: ", "")[:400]
            entry = {
                "id": d["id"],
                "name": d.get("name"),
                "industry": d.get("industry"),
                "skipped_at": now,
                "reason": reason,
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[skip-history] appended {len(skipped_drafts)} entries -> {SKIP_HISTORY_PATH.name}")


def append_sent_history(sent_drafts: list[dict[str, Any]]) -> None:
    if not sent_drafts:
        return
    now = datetime.utcnow().isoformat() + "Z"
    with SENT_HISTORY_PATH.open("a") as f:
        for d in sent_drafts:
            entry = {
                "id": d["id"],
                "name": d.get("name"),
                "industry": d.get("industry"),
                "form_url": d.get("form_url"),
                "subject": (d.get("draft") or {}).get("subject"),
                "sent_at": now,
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[sent-history] appended {len(sent_drafts)} entries -> {SENT_HISTORY_PATH.name}")


def stage_history(action: str) -> None:
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

def _run(cmd: list[str]) -> tuple[int, str, str]:
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode, res.stdout, res.stderr


def oc_browser(*args: str, profile: str = BROWSER_PROFILE) -> str | None:
    cmd = ["openclaw", "browser", "--browser-profile", profile, *args]
    rc, out, err = _run(cmd)
    if rc != 0:
        print(f"[browser err] {' '.join(args)}: {err.strip()}", file=sys.stderr)
        return None
    return out


def oc_infer(prompt: str, model: str = DEFAULT_MODEL) -> str | None:
    cmd = [
        "openclaw", "infer", "model", "run",
        "--prompt", prompt,
        "--model", model,
        "--json",
    ]
    rc, out, err = _run(cmd)
    if rc != 0:
        print(f"[infer err] {err.strip()}", file=sys.stderr)
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        print(f"[infer json parse err] {out[:300]}", file=sys.stderr)
        return None
    if not data.get("ok"):
        print(f"[infer not ok] {data.get('error')}", file=sys.stderr)
        return None
    outputs = data.get("outputs") or []
    if outputs:
        first = outputs[0] if isinstance(outputs[0], dict) else {}
        for k in ("text", "content", "output_text"):
            if first.get(k):
                return first[k]
    return json.dumps(data, ensure_ascii=False)


def _evaluate(js: str) -> Any:
    """Run a JS function in the browser via `openclaw browser evaluate --fn`."""
    cmd = ["openclaw", "browser", "--browser-profile", BROWSER_PROFILE,
           "evaluate", "--fn", js]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[evaluate err] {res.stderr.strip()}", file=sys.stderr)
        return None

    body_lines: list[str] = []
    for line in res.stdout.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("🦞"):
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
        print(f"[evaluate parse err] {e}: {text[:300]}", file=sys.stderr)
        return None


# ============================================================================
# Stage: bootstrap (load targets from targets.yaml)
# ============================================================================

def stage_bootstrap(targets_path: Path, out_path: Path,
                    include_sent: bool = False,
                    include_dropped: bool = False,
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
        if cid in skip_ids:
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

  for (const el of document.querySelectorAll('input,textarea,select')) {
    const tag = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();
    if (type === 'hidden' || type === 'submit' || type === 'button' || type === 'image') continue;

    const name = el.name || el.id || '';
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
        name: name, label: label, required: required,
        options: Array.from(el.options).map(o => o.text).slice(0, 60)
      });
      continue;
    }
    if (tag === 'textarea') {
      result.textareas.push({
        name: name, label: label, required: required,
        max_length: maxLength, placeholder: placeholder
      });
      continue;
    }
    // Standard input
    result.inputs.push({
      name: name, label: label, required: required, type: type,
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

  return result;
}
"""


def stage_enrich(input_path: Path, out_path: Path) -> None:
    targets = [json.loads(l) for l in input_path.open()]
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
    persona = (PROMPTS_DIR / "system_persona.md").read_text()
    examples = (PROMPTS_DIR / "examples.md").read_text()
    config_str = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    return (
        "<system>\n"
        f"{persona}\n\n"
        "## Few-shot examples\n\n"
        f"{examples}\n\n"
        "## Your sender + pitch + persona configuration\n\n"
        "```yaml\n"
        f"{config_str}"
        "```\n"
        "</system>\n"
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
        "## Target\n"
        "```json\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "```\n"
        "</user>\n"
    )


def extract_first_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start: i + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


_REFINE_PROMPT_TEMPLATE = """You wrote the draft below for a Japanese B2B inquiry-form message.
Now act as a tough senior copywriter and **critique then rewrite** it.

## Target context (the company you're writing to)
```json
{target_json}```

## Your draft
```json
{draft_json}```

## Critique checklist (apply strictly)

1. Does the FIRST sentence reference a specific company fact, NOT
   start with 「お世話になります」「突然のご連絡」「初めまして」?
   → If no: REWRITE the opening.

2. Are 「拝見しました」「拝察します」「と存じます」 used more than twice combined?
   → If yes: replace at least one with natural alternatives
     (気になりました／考えています／効きそうです／今がタイミング etc.)

3. Is the self-introduction more than "肩書＋過去事例コピペ"?
   Does it bridge to a specific aspect of the target's business?
   → If no: rewrite the self-intro to bridge to the target's specific
     context (e.g. "MDOnline出身として、御社の患者×家族の二重顧客構造は…")

4. Are numbers (LTV+20%, 開封率3-5倍 etc.) used? If yes, are they tied
   to the target's specific business structure, not generic claims?
   → If generic: remove or replace with a target-specific framing.

5. Is the CTA something other than 「30分のオンラインでご相談いただけませんでしょうか」?
   Does it either (a) leave the recipient an OUT, or (b) offer specific
   value, or (c) ask a curious question?
   → If template: rewrite the CTA.

6. Are paragraphs varied in length (NOT mechanical 3-para structure)?
   → If symmetric: vary lengths.

7. Does the writing sound like a human who actually read the target's
   recent news, or like a generic LINE×CRM pitch?
   → Push for the former.

8. Body length: must be ≤ {max_chars} characters.

## Output format (STRICT JSON)

```json
{{
  "critique": "<2-4 sentence critique of the original draft, in Japanese>",
  "subject": "<refined subject or null>",
  "body": "<refined body, ≤{max_chars} chars>"
}}
```

If the original is already excellent and you'd change nothing, set
`critique` to "改善不要" and copy the original subject/body unchanged.

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

    prompt = _REFINE_PROMPT_TEMPLATE.format(
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


def stage_draft(input_path: Path, out_path: Path, config: dict[str, Any],
                 refine: bool = False) -> None:
    targets = [json.loads(l) for l in input_path.open()]
    print(f"[draft] {len(targets)} targets to draft" + (" (with --refine 2nd pass)" if refine else ""))

    model_cfg = config.get("model", {}) or {}
    model = model_cfg.get("name", DEFAULT_MODEL)
    default_max = int(model_cfg.get("max_chars", 400))
    extended_max = int(model_cfg.get("max_chars_extended", 1800))

    system_block = build_system_block(config)
    drafts: list[dict[str, Any]] = []

    for i, t in enumerate(targets, 1):
        # Per-target char limit: targets.yaml overrides config
        max_chars = int(t.get("char_limit") or default_max)
        max_chars = min(max_chars, extended_max)

        prompt = system_block + build_user_block(t, max_chars)
        print(f"[draft] ({i}/{len(targets)}) {t.get('name')} (pass 1) ...")
        response = oc_infer(prompt, model=model)
        draft = extract_first_json(response or "")
        if not draft or "subject" not in draft or "body" not in draft:
            print(f"[draft] parse failed for {t.get('name')}: {(response or '')[:200]}")
            continue

        # Second-pass refinement: critique and rewrite
        # Skip refine for SKIP outputs (no point critiquing a SKIP)
        if refine and draft.get("subject") != "SKIP":
            print(f"[draft] ({i}/{len(targets)}) {t.get('name')} (pass 2 refine) ...")
            refined = _refine_draft(t, draft, config, max_chars)
            if refined and refined.get("body"):
                critique = refined.get("critique", "")[:80]
                print(f"          → critique: {critique}{'...' if len(refined.get('critique','')) > 80 else ''}")
                # Use refined version, keep original around for debugging
                draft = {
                    "subject": refined.get("subject"),
                    "body": refined["body"],
                    "_pass1_subject": draft.get("subject"),
                    "_pass1_body": draft.get("body"),
                    "_critique": refined.get("critique"),
                }
            else:
                print(f"          → refine failed, keeping pass-1 draft")

        drafts.append({
            **t,
            "draft": draft,
            "max_chars_used": max_chars,
            "_drafted_at": datetime.utcnow().isoformat() + "Z",
        })

    with out_path.open("w") as f:
        for d in drafts:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"[draft] wrote {len(drafts)} drafts -> {out_path}")

    new_skips = [d for d in drafts if (d.get("draft") or {}).get("subject") == "SKIP"]
    append_skip_history(new_skips)


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

    print(f"\nSend drafts? Pick one:")
    print(f"  all  → send all not-yet-sent ({len(not_yet_sent)} drafts: {','.join(map(str, not_yet_sent))})")
    print(f"  1,3  → comma-separated draft IDs (1-{len(sendable)})")
    print(f"  n    → skip, exit")
    try:
        ans = input("→ ").strip().lower()
    except EOFError:
        print("\n(no stdin — skipping interactive send)")
        return

    if not ans or ans in ("n", "no", "skip", "q", "quit"):
        print("Aborted. Run `python run.py send --ids ...` later when ready.")
        return

    if ans in ("all", "y", "yes", "a"):
        ids = set(not_yet_sent)
    else:
        try:
            ids = {int(x.strip()) for x in ans.split(",") if x.strip()}
        except ValueError:
            print(f"Could not parse '{ans}'. Aborted.")
            return

    valid = {i for i in ids if 1 <= i <= len(sendable)}
    if not valid:
        print(f"No valid IDs in {ids}. Aborted.")
        return

    chosen_names = [sendable[i - 1].get("name", "?") for i in sorted(valid)]
    print(f"\nSending to: {', '.join(chosen_names)}")

    if config is None:
        # Lazy-load config when stage_preview is invoked directly
        cfg_path = SKILL_DIR / "config.yaml"
        if not cfg_path.exists():
            print(f"[preview] config.yaml not found at {cfg_path}; cannot send", file=sys.stderr)
            return
        config = yaml.safe_load(cfg_path.read_text())
    stage_send(input_path, valid, mode="interactive", config=config)


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

    if clean:
        for f in ("leads.jsonl", "enriched.jsonl", "drafts.jsonl"):
            (DATA_DIR / f).unlink(missing_ok=True)
        print(f"[campaign] cleared previous run state")

    # --- Phase 1: Pull ---
    print(f"\n{bar}\n[1/6] PULL — bootstrap targets from {targets_path.name}\n{bar}")
    stage_bootstrap(targets_path, DATA_DIR / "leads.jsonl",
                    include_sent=include_sent,
                    limit=limit,
                    only_ids=only_ids)
    leads_n = sum(1 for line in (DATA_DIR / "leads.jsonl").open() if line.strip())
    if leads_n == 0:
        print(f"\n[campaign] no targets after pull — aborting")
        return
    print(f"\n[campaign] → {leads_n} targets pulled")

    # --- Phase 2: Enrich ---
    if skip_enrich:
        # Pass-through: just copy leads.jsonl to enriched.jsonl
        print(f"\n{bar}\n[2/6] ENRICH — SKIPPED (using --skip-enrich, leads passed through)\n{bar}")
        import shutil
        shutil.copy(DATA_DIR / "leads.jsonl", DATA_DIR / "enriched.jsonl")
    else:
        print(f"\n{bar}\n[2/6] ENRICH — form structure detection\n{bar}")
        stage_enrich(DATA_DIR / "leads.jsonl", DATA_DIR / "enriched.jsonl")

    enriched_n = sum(1 for line in (DATA_DIR / "enriched.jsonl").open() if line.strip())
    if enriched_n == 0:
        print(f"\n[campaign] no enriched targets — aborting")
        return
    print(f"\n[campaign] → {enriched_n} targets enriched")

    # --- Phase 3: Personalize ---
    print(f"\n{bar}\n[3/6] PERSONALIZE — Sonnet draft (cached system prompt)\n{bar}")
    cfg_path = SKILL_DIR / "config.yaml"
    if not cfg_path.exists():
        print(f"[campaign] config.yaml not found at {cfg_path}", file=sys.stderr)
        print(f"           cp {SKILL_DIR / 'config.example.yaml'} {cfg_path}", file=sys.stderr)
        return
    cfg = yaml.safe_load(cfg_path.read_text())
    stage_draft(DATA_DIR / "enriched.jsonl", DATA_DIR / "drafts.jsonl", cfg, refine=refine)

    drafts = [json.loads(l) for l in (DATA_DIR / "drafts.jsonl").open() if l.strip()]
    sendable = [d for d in drafts if (d.get("draft") or {}).get("subject") != "SKIP"]
    skipped = len(drafts) - len(sendable)
    print(f"\n[campaign] → {len(sendable)} sendable, {skipped} SKIP "
          + f"(send rate {len(sendable) * 100 // len(drafts) if drafts else 0}%)")

    # --- Phases 4-6: Approve → Send → Log (inside preview's interactive flow) ---
    if skip_send:
        print(f"\n{bar}\n[4/6] PREVIEW — display only (send skipped)\n{bar}")
        stage_preview(DATA_DIR / "drafts.jsonl", interactive_send=False, config=cfg)
        print(f"\n[campaign] stopped at preview. To send later: python run.py send --ids ...")
        return

    print(f"\n{bar}\n[4-6/6] APPROVE → SEND → LOG (interactive)\n{bar}")
    stage_preview(DATA_DIR / "drafts.jsonl", interactive_send=True, config=cfg)


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
  const { patterns } = args;
  const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], a[role="button"]'));
  for (const pat of patterns) {
    const re = new RegExp(pat);
    for (const b of buttons) {
      const txt = ((b.textContent || b.value || '') + ' ' + (b.getAttribute('aria-label') || '')).trim();
      if (re.test(txt) && !b.disabled && b.offsetParent !== null) {
        b.click();
        return { clicked: true, text: txt.slice(0, 50) };
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


def _click_button(patterns: list[str]) -> dict[str, Any] | None:
    args = {"patterns": patterns}
    js = f"""
    (() => {{
      const fn = {_CLICK_BUTTON_BY_TEXT_JS};
      return fn({json.dumps(args, ensure_ascii=False)});
    }})()
    """
    res = _evaluate(js)
    return res if isinstance(res, dict) else None


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

    model = config.get("model", {}).get("name", DEFAULT_MODEL)
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

  // Find element by name first, then id
  const findEl = (selectorAttr) => {
    let el = document.querySelector(`[name="${selectorAttr}"]`);
    if (el) return el;
    el = document.querySelector(`#${CSS.escape(selectorAttr)}`);
    return el;
  };

  if (action === "set_text") {
    const el = findEl(name);
    if (!el) return { ok: false, reason: "element not found", name };
    setVal(el, value);
    return { ok: true, action, name, value: String(value).slice(0, 50) };
  }

  if (action === "select_option") {
    const sel = findEl(name);
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


def _apply_field_action(name: str, action: str, value: str) -> dict[str, Any] | None:
    args = {"name": name, "action": action, "value": value}
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


def fill_form_with_plan(plan: dict[str, Any], body: str) -> dict[str, Any]:
    """Apply an LLM-generated fill plan via JS. Returns diagnostics."""
    diag = {"filled": [], "errors": [], "skipped": [], "warnings": plan.get("warnings", [])}
    BODY_TOKEN = "__BODY__"

    for entry in plan.get("fields", []):
        name = entry.get("name")
        action = entry.get("action")
        value = entry.get("value", "")
        if not name or not action:
            continue
        if action == "skip":
            diag["skipped"].append(name)
            continue
        # Substitute body placeholder
        if isinstance(value, str) and BODY_TOKEN in value:
            value = value.replace(BODY_TOKEN, body)
        res = _apply_field_action(name, action, value)
        if res and res.get("ok"):
            label = res.get("value") or res.get("action")
            diag["filled"].append(f"{name}={str(label)[:30]} ({action})")
        else:
            reason = (res or {}).get("reason", "unknown")
            diag["errors"].append(f"{name} ({action}): {reason}")
        time.sleep(0.1)

    for cb_name in plan.get("checkboxes_to_check", []):
        res = _check_by_name(cb_name)
        if res and res.get("ok"):
            diag["filled"].append(f"checkbox:{cb_name}")
        else:
            diag["errors"].append(f"checkbox {cb_name}: {(res or {}).get('reason','?')}")

    return diag


def fill_form_for_target(target: dict[str, Any], config: dict[str, Any],
                         body: str) -> dict[str, Any]:
    """Fill all known sender fields + body. Returns diagnostic dict.

    Strategy:
      1. Try LLM-generated plan first (Sonnet looks at the form structure
         and outputs a precise field map)
      2. Fall back to heuristic regex patterns for any unmapped fields
    """
    sender = config.get("sender", {})

    # === Phase 1: LLM-driven fill ===
    char_limit = int(target.get("char_limit", config.get("model", {}).get("max_chars", 400)))
    plan = _llm_analyze_form(target, config, body_max_chars=char_limit)
    plan_diag = None
    if plan:
        print(f"  [fill] LLM plan: {len(plan.get('fields', []))} field actions, "
              f"{len(plan.get('checkboxes_to_check', []))} checkboxes, "
              f"flow={plan.get('next_step', '?')}")
        for w in plan.get("warnings", []):
            print(f"    ⚠ {w}")
        plan_diag = fill_form_with_plan(plan, body)
        print(f"  [fill] plan applied: {len(plan_diag['filled'])} ok, "
              f"{len(plan_diag['errors'])} errors, {len(plan_diag['skipped'])} intentionally skipped")
        for e in plan_diag["errors"][:5]:
            print(f"    ✗ {e}")
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


def stage_send(input_path: Path, ids: set[int], mode: str = "interactive",
               config: dict[str, Any] | None = None) -> None:
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
    print(f"[send] processing {len(targets)} targets · mode={mode_label}")

    sent: list[dict[str, Any]] = []
    filled_only: list[dict[str, Any]] = []

    for di, d in enumerate(targets):
        idx = sendable.index(d) + 1
        name = d.get("name", "?")
        body = d["draft"]["body"]
        form_url = d["form_url"]
        flow = d.get("flow", "single")
        captcha = (d.get("form_fields") or {}).get("has_recaptcha_v2") or d.get("captcha") == "recaptcha_v2_visible"

        print(f"\n=== [{idx}] {name} ===")
        print(f"  URL: {form_url}")
        print(f"  Flow: {flow}, captcha: {captcha and 'v2 (manual)' or 'none/v3'}, chars: {len(body)}")

        # 1. Open form
        oc_browser("open", form_url)
        time.sleep(RATE_LIMIT_SECONDS)

        # 2. Click any pre-form entry (e.g. "法人のお客様" tab)
        if d.get("entry_click_text"):
            for txt in (d["entry_click_text"] if isinstance(d["entry_click_text"], list) else [d["entry_click_text"]]):
                _click_button([re.escape(txt)])
                time.sleep(1.5)

        # 3. Fill all fields
        diagnostics = fill_form_for_target(d, config, body)
        print(f"  [send] filled: {len(diagnostics['filled'])} / unfilled: {len(diagnostics['unfilled'])} / errors: {len(diagnostics['errors'])}")
        for f in diagnostics["filled"][:8]:
            print(f"    ✓ {f}")
        if diagnostics["errors"]:
            for e in diagnostics["errors"]:
                print(f"    ✗ {e}")

        if captcha:
            print(f"  [send] ⚠ reCAPTCHA v2 detected — manual completion required")
            print(f"          Browser is left open with form filled. Complete CAPTCHA + Send manually.")
            filled_only.append(d)
            if mode == "interactive":
                ans = input("  [send] After manual submit, type 'y' to log: ").strip().lower()
                if ans == "y":
                    sent.append(d)
                    filled_only.pop()
            continue

        if mode == "fill-only":
            print(f"  [send] ✓ filled. Click 確認/送信 manually.")
            filled_only.append(d)
            continue

        # 4. Click first submit button (確認 for confirm-flow, 送信 for single)
        time.sleep(1.0)
        if flow == "confirm":
            patterns = [r"入力内容を確認", r"内容(を|の)?確認", r"確認画面", r"確認$", r"内容の確認へ"]
        else:
            patterns = [r"送信する", r"^送信$", r"submit", r"内容を送信", r"同意して.*送信"]

        click_res = _click_button(patterns)
        if not click_res or not click_res.get("clicked"):
            print(f"  [send] ⚠ first submit button not found (patterns={patterns})")
            filled_only.append(d)
            continue
        print(f"  [send] clicked: {click_res.get('text')}")
        time.sleep(3)

        # 5. If confirm flow, click final submit
        if flow == "confirm":
            if mode == "interactive":
                ans = input("  [send] Confirmation page reached. Click 送信する now? (y/N): ").strip().lower()
                if ans != "y":
                    print(f"  [send] aborted; modal left at confirm page")
                    filled_only.append(d)
                    continue
            click2 = _click_button([r"^送信する$", r"^送信$", r"この内容で送信", r"内容を送信する"])
            if not click2 or not click2.get("clicked"):
                print(f"  [send] ⚠ final submit button not found")
                filled_only.append(d)
                continue
            print(f"  [send] clicked final: {click2.get('text')}")
            time.sleep(3)

        # 6. Verify success (heuristic: URL change or success keywords)
        snap = oc_browser("snapshot")
        success_keywords = ["送信完了", "ありがとうございました", "ご連絡", "完了画面", "THANKS", "thank you"]
        success = False
        if snap:
            success = any(k in snap for k in success_keywords)

        if success:
            print(f"  [send] ✅ verified success page")
            sent.append(d)
        else:
            print(f"  [send] ⚠ no success keywords detected — verify manually")
            filled_only.append(d)

        # Rate limit
        if di < len(targets) - 1:
            print(f"  [send] sleeping 30s before next...")
            time.sleep(30)

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
        not_yet_sent = [i for i, d in enumerate(sendable, 1) if d["id"] not in sent_ids]
        if not not_yet_sent:
            print(f"[{cmd_name}] no sendable drafts left (all already in sent_history)")
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
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("bootstrap", help="(Pull) Load curated targets.yaml -> data/leads.jsonl")
    p.add_argument("--targets", default=str(SKILL_DIR / "targets.yaml"))
    p.add_argument("--out", default=str(DATA_DIR / "leads.jsonl"))
    p.add_argument("--include-sent", action="store_true",
                   help="Also include companies marked status: sent")
    p.add_argument("--include-dropped", action="store_true",
                   help="Also include companies marked status: dropped")
    p.add_argument("--limit", type=int, default=None,
                   help="Pull only the first N eligible targets (after status/history filters)")
    p.add_argument("--only", default=None,
                   help="Comma-separated target ids to restrict to (e.g. 'ikkholdings,pharmafoods')")

    p = sub.add_parser("campaign",
                        help="Run the full 6-phase outreach pipeline (pull→enrich→draft→preview+send)")
    p.add_argument("--targets", default=str(SKILL_DIR / "targets.yaml"))
    p.add_argument("--clean", action="store_true",
                   help="Wipe leads/enriched/drafts before running")
    p.add_argument("--skip-enrich", action="store_true",
                   help="Skip the enrich phase (use leads.jsonl as enriched.jsonl)")
    p.add_argument("--skip-send", action="store_true",
                   help="Stop at preview without sending (display only)")
    p.add_argument("--include-sent", action="store_true",
                   help="Also include companies marked status: sent (re-send mode)")
    p.add_argument("--refine", action="store_true",
                   help="Two-pass draft generation (critique + rewrite) for higher quality")
    p.add_argument("--limit", type=int, default=None,
                   help="Pull only the first N eligible targets (after filters)")
    p.add_argument("--only", default=None,
                   help="Comma-separated target ids to restrict to")

    p = sub.add_parser("enrich", help="Visit each form URL, capture field structure")
    p.add_argument("--in", dest="input_path", default=str(DATA_DIR / "leads.jsonl"))
    p.add_argument("--out", default=str(DATA_DIR / "enriched.jsonl"))

    p = sub.add_parser("draft", help="Generate personalized form messages")
    p.add_argument("--in", dest="input_path", default=str(DATA_DIR / "enriched.jsonl"))
    p.add_argument("--out", default=str(DATA_DIR / "drafts.jsonl"))
    p.add_argument("--config", default=str(SKILL_DIR / "config.yaml"))
    p.add_argument("--from-targets", action="store_true",
                   help="Skip enrich; draft directly from data/leads.jsonl")
    p.add_argument("--refine", action="store_true",
                   help="Two-pass: generate draft, then critique+rewrite for higher quality (~2x cost)")

    p = sub.add_parser("preview",
                        help="(Approve) Show all drafts in terminal, then prompt to send")
    p.add_argument("--in", dest="input_path", default=str(DATA_DIR / "drafts.jsonl"))
    p.add_argument("--no-send", action="store_true",
                   help="Skip the interactive send prompt at the end")

    p = sub.add_parser("send", help="Drive form fill + submit")
    p.add_argument("--in", dest="input_path", default=str(DATA_DIR / "drafts.jsonl"))
    p.add_argument("--ids", help="Comma-separated SENDABLE indices (1-based), or 'all' for every not-yet-sent draft. Required unless --all is set.")
    p.add_argument("--all", action="store_true",
                   help="Send every SENDABLE draft that's not already in sent_history (skips SKIPPED automatically)")
    p.add_argument("--config", default=str(SKILL_DIR / "config.yaml"))
    mode_group = p.add_mutually_exclusive_group()
    mode_group.add_argument("--auto-send", action="store_true",
                             help="Fill and submit without prompting")
    mode_group.add_argument("--no-confirm", action="store_true",
                             help="Fill only — you click submit manually")

    p = sub.add_parser("walk",
                        help="Walkthrough: review each SENDABLE draft one-by-one and choose send/skip/fill/quit")
    p.add_argument("--in", dest="input_path", default=str(DATA_DIR / "drafts.jsonl"))
    p.add_argument("--config", default=str(SKILL_DIR / "config.yaml"))
    p.add_argument("--default", default="send",
                   choices=["send", "skip", "fill", "quit"],
                   help="Action when user just presses Enter (default: send)")

    p = sub.add_parser("mark-sent", help="Log specific drafts to sent_history.jsonl")
    p.add_argument("--in", dest="input_path", default=str(DATA_DIR / "drafts.jsonl"))
    p.add_argument("--ids", help="Comma-separated SENDABLE indices, or 'all'")
    p.add_argument("--all", action="store_true", help="Mark every not-yet-sent SENDABLE draft as sent")

    p = sub.add_parser("history", help="View / manage skip and sent history")
    p.add_argument("action",
                   choices=["show", "purge-skip", "purge-sent", "purge-all"])

    args = ap.parse_args()

    if args.cmd == "bootstrap":
        only_ids = [x.strip() for x in args.only.split(",")] if args.only else None
        stage_bootstrap(Path(args.targets), Path(args.out),
                        include_sent=args.include_sent,
                        include_dropped=args.include_dropped,
                        limit=args.limit,
                        only_ids=only_ids)
    elif args.cmd == "campaign":
        only_ids = [x.strip() for x in args.only.split(",")] if args.only else None
        stage_campaign(
            targets_path=Path(args.targets),
            clean=args.clean,
            skip_enrich=args.skip_enrich,
            skip_send=args.skip_send,
            include_sent=args.include_sent,
            refine=args.refine,
            limit=args.limit,
            only_ids=only_ids,
        )
    elif args.cmd == "enrich":
        stage_enrich(Path(args.input_path), Path(args.out))
    elif args.cmd == "draft":
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"[draft] config not found: {config_path}", file=sys.stderr)
            print(f"        cp {SKILL_DIR / 'config.example.yaml'} {config_path}", file=sys.stderr)
            sys.exit(2)
        cfg = yaml.safe_load(config_path.read_text())
        # Default: read from enriched.jsonl. With --from-targets, read leads.jsonl instead
        in_path = Path(args.input_path)
        if args.from_targets and not in_path.exists():
            in_path = DATA_DIR / "leads.jsonl"
        stage_draft(in_path, Path(args.out), cfg, refine=args.refine)
    elif args.cmd == "preview":
        # Lazy-load config for interactive send chain
        cfg = None
        cfg_path = SKILL_DIR / "config.yaml"
        if cfg_path.exists() and not args.no_send:
            cfg = yaml.safe_load(cfg_path.read_text())
        stage_preview(Path(args.input_path),
                      interactive_send=(not args.no_send),
                      config=cfg)
    elif args.cmd == "send":
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"[send] config not found: {config_path}", file=sys.stderr)
            sys.exit(2)
        cfg = yaml.safe_load(config_path.read_text())
        ids = _resolve_ids_arg(args.ids, args.all, Path(args.input_path), cmd_name="send")
        if ids is None:
            sys.exit(2)
        if args.auto_send:
            mode = "auto"
        elif args.no_confirm:
            mode = "fill-only"
        else:
            mode = "interactive"
        stage_send(Path(args.input_path), ids, mode=mode, config=cfg)
    elif args.cmd == "walk":
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"[walk] config not found: {config_path}", file=sys.stderr)
            sys.exit(2)
        cfg = yaml.safe_load(config_path.read_text())
        stage_walkthrough(Path(args.input_path), cfg, default_action=args.default)
    elif args.cmd == "mark-sent":
        ids = _resolve_ids_arg(args.ids, args.all, Path(args.input_path), cmd_name="mark-sent")
        if ids is None:
            sys.exit(2)
        stage_mark_sent(Path(args.input_path), ids)
    elif args.cmd == "history":
        stage_history(args.action)


if __name__ == "__main__":
    main()
