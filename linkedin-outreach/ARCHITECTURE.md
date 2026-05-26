# linkedin-outreach — Architecture & Engineering Notes

This document describes the design, components, and data contracts of the
`linkedin-outreach` OpenClaw skill, and outlines the sibling `jp-outreach`
skill that mirrors the same architecture for Japanese-language outreach.

---

## 1. Purpose

A personalized, semi-automated outreach pipeline for high-quality, low-volume
B2B sales — currently used by Torana to reach foreign consumer-brand operators
about Japan-market entry.

Design goals:

| Goal | How achieved |
|---|---|
| Deep personalization at scale | Per-lead profile scrape + Sonnet-generated InMails grounded on real data |
| Minimize LLM credit consumption | Prompt caching, batch context, Haiku/Sonnet tiering, dedup via history |
| Human in the loop on every send | Two-stage approval: select IDs in preview → per-draft Send confirmation |
| Idempotent + resumable | All state in append-only JSONL files; can rerun any phase |
| Channel-agnostic core | Same 6-phase pattern reused for LinkedIn, forms, and JP-language outreach |
| Slack-driven UX | Skill is invokable from Slack via natural-language commands |

Operational targets:

- **LinkedIn**: 100 outreach/month with high personalization quality
- **Forms (planned `form-outreach`)**: 500/month, broader/automated
- **JP outreach (planned `jp-outreach`)**: Japanese-language variant for the
  Japanese consumer market

---

## 2. System diagram

```
┌──────────────────────────────────────────────────────────────────────────┐
│                            HUMAN OPERATOR                                │
│                       (Terminal + Slack + Chrome)                        │
└────────────────┬──────────────────────┬────────────────────┬─────────────┘
                 │                      │                    │
        invokes via Slack         runs in terminal     reviews/sends
                 │                      │                    │
                 ▼                      ▼                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                          OpenClaw Gateway                                │
│   ┌────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│   │  Slack     │  │   Agent      │  │  Browser     │  │  Inference   │  │
│   │  channel   │──│   (Claude)   │──│  plugin      │──│  (Sonnet)    │  │
│   │  plugin    │  │              │  │  (CDP→Chrome)│  │              │  │
│   └────────────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  │
└─────────────────────────┼──────────────────┼──────────────────┼─────────┘
                          │                  │                  │
                          │ reads SKILL.md   │ navigate/snapshot│ generate
                          │                  │                  │
                          ▼                  ▼                  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                  linkedin-outreach skill                                 │
│                                                                          │
│   SKILL.md  config.yaml  prompts/  run.py  research.py  build_search.py │
│                                                                          │
│   ┌──────────────────────────────────────────────────────────────────┐  │
│   │                      State (JSONL)                               │  │
│   │   leads.jsonl → enriched.jsonl → drafts.jsonl                    │  │
│   │   ─────────────────────────────────────────────────              │  │
│   │   skip_history.jsonl  (append-only audit)                        │  │
│   │   sent_history.jsonl  (append-only audit)                        │  │
│   └──────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                  LinkedIn Sales Navigator                                │
│           (driven by OpenClaw browser plugin via CDP)                    │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 3. The canonical 6-phase pattern

All outreach work in this codebase follows the same six-phase contract:

```
1. PULL  ─→  2. ENRICH  ─→  3. PERSONALIZE  ─→  4. APPROVE  ─→  5. SEND  ─→  6. LOG
```

| # | Phase | Role | Input | Output | LLM | I/O |
|---|---|---|---|---|---|---|
| 1 | **Pull** | Source leads | Sales Nav URL OR curated CSV | `leads.jsonl` | none | browser + scrape |
| 2 | **Enrich** | Add deep context | leads.jsonl | `enriched.jsonl` (+ headline, about, recent_activity, experience) | none | browser per-profile |
| 3 | **Personalize** | Compose draft | enriched.jsonl + config.yaml + prompts/ | `drafts.jsonl` (+ `draft.subject`, `draft.body`, or `SKIP`) | Sonnet w/ cached system prompt | LLM API |
| 4 | **Approve** | Human review | drafts.jsonl | set of selected lead IDs | none | terminal/Slack prompt |
| 5 | **Send** | Dispatch | profile_url + subject + body | (browser action: navigate, click Message, fill compose, click Send) | none | browser |
| 6 | **Log** | Audit trail | sent leads | append `sent_history.jsonl` | none | file |

Contracts:

- **Idempotent**: every phase reads from the previous phase's JSONL output and
  produces its own. Re-running any phase is safe.
- **Resumable**: delete an output file to force that phase to re-run.
- **Append-only history**: `skip_history.jsonl` and `sent_history.jsonl` are
  never rewritten — only extended. Both are checked at Pull and Send phases
  to prevent double-processing and double-sending.
- **Cache-friendly**: phase 3's system prompt (persona + examples + config)
  is byte-stable across all leads in a run, yielding 90%+ prompt cache hits
  on Anthropic's API.

---

## 4. Module structure

```
~/.openclaw/skills/linkedin-outreach/
├── SKILL.md                  # OpenClaw skill manifest + Slack trigger table
├── ARCHITECTURE.md           # This file
├── README.md                 # Human-facing usage doc
├── config.example.yaml       # Template for config.yaml
├── config.yaml               # User's sender / pitch / persona / model config
├── targets.csv               # Curated lead list (linkedin_url, name, company, ...)
├── targets.example.csv       # Template
├── run.py                    # All pipeline stages as subcommands
├── research.py               # Legacy thin wrapper; superseded by `run.py campaign`
├── build_search.py           # Interactive Sales Nav saved-search builder
├── install.sh                # Bootstrap script
├── prompts/
│   ├── system_persona.md     # Cacheable system prompt (Sonnet's role + rules)
│   └── examples.md           # Few-shot examples
├── data/                     # Runtime state (gitignored in practice)
│   ├── leads.jsonl
│   ├── enriched.jsonl
│   ├── drafts.jsonl
│   ├── skip_history.jsonl
│   ├── sent_history.jsonl
│   ├── sample_search.txt     # Last Sales Nav search-page snapshot (debug)
│   ├── sample_profile.txt    # Last profile-page snapshot (debug)
│   └── sample_compose.txt    # Last InMail compose-modal snapshot (debug)
└── .venv/                    # Skill-local Python virtualenv
```

---

## 5. `run.py` subcommand surface

```
run.py campaign         # one-shot pipeline (phases 1-6, interactive)
       fetch-leads      # phase 1 via Sales Nav saved search
       fetch-from-csv   # phase 1 via curated CSV
       lookup-urls      # CSV utility: fill linkedin_url from name + company
       enrich           # phase 2
       draft            # phase 3
       preview          # phase 4 (display + interactive send prompt)
       send             # phase 5 (auto / interactive / fill-only modes)
       mark-sent        # phase 6 (explicit log)
       history          # show / bootstrap / purge skip|sent histories
```

### Key flags

- `--clean` (campaign) — wipe leads/enriched/drafts before run
- `--skip-lookup` (campaign) — bypass the lookup-urls sub-phase
- `--skip-send` (campaign) — stop at preview; no interactive send
- `--no-send` (preview) — display-only; no send prompt
- `--auto-send` (send) — fill + click Send + log, no per-draft prompt
- `--no-confirm` (send) — fill only; human clicks Send manually
- `--ignore-skip-history` (fetch-*) — bypass dedup against past SKIPs

---

## 6. Browser automation: OpenClaw + JS evaluation

The OpenClaw `browser` plugin drives Chrome via CDP. Two interaction modes
are used:

### 6.1 Snapshot-based (for small DOM regions)

```bash
openclaw browser --browser-profile openclaw snapshot
```

Returns an accessibility-tree text representation with refs like `[ref=e123]`.
Subsequent `click`, `type` commands target those refs.

**Limitation**: snapshot output is truncated around 600 lines. Anything past
that (modals appended to `<body>` after a long page) is invisible.

### 6.2 JS-based (for full-DOM access)

For elements that fall beyond snapshot truncation (Sales Nav's InMail compose
modal, the search results list, etc.), we use `evaluate --fn`:

```bash
openclaw browser --browser-profile openclaw evaluate --fn '() => { ... return ...; }'
```

The function runs in the page context with full DOM access. We use this for:

- **`LEADS_JS_EXTRACTOR`** — extract all visible lead cards from a Sales Nav
  results page (bypasses snapshot truncation that limited us to ~3 leads).
- **`_FILL_COMPOSE_JS`** — find and fill the Subject + Body inputs of the
  InMail compose modal using React-aware setter dispatch:
  ```js
  const setter = Object.getOwnPropertyDescriptor(
    el.tagName === 'TEXTAREA'
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype,
    'value'
  ).set;
  setter.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  ```
- **`_CLICK_SEND_JS`** — locate the Send button inside the compose modal
  (anchored on the subject input's `[role="dialog"]` ancestor) and click it.
- **`_VERIFY_SENT_JS`** — confirm the modal closed after click (= sent).

### 6.3 Selector conventions for Sales Nav

Confirmed and used in the codebase:

| Element | Selector |
|---|---|
| Subject input | `input[id^="compose-form-subject"]` or `input[aria-label="Subject (required)"]` |
| Body textarea | `textarea[id^="compose-form-text"]` or `textarea[name="message"]` |
| Send button | `button` with text matching `/^(Send\|Send InMail\|Send message\|送信)$/i` inside `[role="dialog"]` ancestor of subject |
| Search-result lead | `a[href*="/sales/lead/"]` (filter out `Go to ` decorative links) |
| Company link | `a[href*="/sales/company/"]` |

---

## 7. Prompt design (phase 3)

### 7.1 Caching strategy

Sonnet is invoked once per lead. The prompt is structured as:

```
<system>
{prompts/system_persona.md}          ← stable across leads
{prompts/examples.md}                 ← stable
{config.yaml as YAML}                 ← stable (sender, pitch, persona)
</system>

<user>
{instructions: produce JSON {subject, body}}
{lead data as JSON, indented}
</user>
```

The system block is byte-identical across all leads in a run, so Anthropic's
prompt cache hits for ~90% of input tokens after the first call. Observed
cache hit rates in practice: 43–97%.

### 7.2 Output schema

Strict JSON. The model must output one of:

```json
{"subject": "<8-12 char ja / 5-9 word en>", "body": "<1800 chars max>"}
```

or, for out-of-scope leads:

```json
{"subject": "SKIP", "body": "INSUFFICIENT_DATA: <reason>"}
```

The skill enforces this contract via `extract_first_json` and treats SKIP
entries as auto-deduped (logged into `skip_history.jsonl`).

### 7.3 Personalization rules

Encoded in `prompts/system_persona.md` and `config.yaml.personalization`:

- Must reference at least one specific, non-trivial fact from the lead's
  profile, recent activity, or company.
- Avoid template phrases ("I came across your profile and...").
- Self-intro ≤ 2 sentences.
- Sign-off: first name only.
- Out-of-scope leads MUST be SKIP'd, not faked.

---

## 8. History & deduplication

Two append-only JSONL files prevent re-processing:

### 8.1 `skip_history.jsonl`

Populated automatically when phase 3 emits a SKIP draft. Contains:

```json
{"id": "<slug>", "name": "...", "company": "...", "title": "...", 
 "skipped_at": "...Z", "reason": "..."}
```

Checked at phase 1 (Pull) — any lead whose `id` is in this file is filtered
out before enrich, saving the cost of profile scrape + draft generation.

### 8.2 `sent_history.jsonl`

Populated when phase 5 successfully clicks Send (or via `mark-sent` for
manual confirmations). Contains:

```json
{"id": "<slug>", "name": "...", "company": "...", "subject": "...", "sent_at": "...Z"}
```

Checked at three points:
- Phase 1 (Pull): excluded from leads.jsonl
- Phase 4 (Preview): marked `[ALREADY SENT]` and excluded from `all` default
- Phase 5 (Send): final guard before browser action

### 8.3 Management

```bash
run.py history show           # summary + last 5 skips
run.py history bootstrap      # import existing drafts.jsonl SKIPs
run.py history purge-skip     # delete skip_history.jsonl
run.py history purge-sent     # delete sent_history.jsonl
```

---

## 9. Slack integration

The OpenClaw Slack channel plugin connects via Socket Mode (no public webhook
required). The main agent reads `SKILL.md` and maps natural-language requests
to subcommands:

| Slack message (en / ja) | Resolves to |
|---|---|
| "Run the campaign" / "キャンペーン回して" | `run.py campaign --input targets.csv --clean` |
| "Send draft 1" / "1番送って" | (1) preview draft, ask user confirm in chat → (2) `run.py send --ids 1 --auto-send` |
| "Mark 1 as sent" / "1番送信完了" | `run.py mark-sent --ids 1` |
| "Show skip history" / "スキップ履歴" | `run.py history show` |
| "Look up LinkedIn URLs" | `run.py lookup-urls --input targets.csv` |

The Slack flow always uses two-stage confirmation: the agent posts a draft
preview to chat, waits for user "yes/no", and only then dispatches the
auto-send.

Security note: the agent runs with `groupPolicy="open"` by default, which
should be tightened to `allowlist` with specific channel IDs for production
use, especially given filesystem and process tools are exposed.

---

## 10. Cost profile

Per drafted InMail with Sonnet + prompt caching:

| Component | Tokens | Approximate cost (on direct Anthropic API) |
|---|---|---|
| Cached system prompt (read) | ~2000 | $0.0006 |
| Variable input (lead JSON) | ~1000 | $0.003 |
| Output (subject + body or SKIP) | ~400 | $0.006 |
| **Per lead** | | **~$0.01** |

100 InMails/month ≈ **$1 in API cost** (and zero on Claude CLI subscription,
which is the current routing). Browser automation has no per-action cost;
the dominant operational cost is Sales Navigator subscription and human
review time, not LLM compute.

---

## 11. JP outreach — planned sibling skill

`jp-outreach` is a planned skill for Japanese-language outreach to Japanese
companies. It mirrors `linkedin-outreach`'s architecture exactly, with only
the channel-specific phases (Pull / Enrich / Send) re-implemented per
channel.

### 11.1 Channel matrix

| Phase | linkedin-outreach (current) | jp-outreach (planned) | form-outreach (planned) |
|---|---|---|---|
| Pull | Sales Nav search OR curated CSV | Crunchbase-JP / Wantedly / curated JP company list | Curated 企業リスト CSV (Japan or global) |
| Enrich | LinkedIn profile snapshot | Wantedly profile / company site / press releases (Japanese-language) | Company About / 最近のpress (Tavily fetch) |
| Personalize | Sonnet (en), Torana ops pitch | Sonnet (ja), Japanese-localized pitch (MDオンライン, or other JP product line) | Sonnet (any), form-specific value prop |
| Approve | Same | Same | Same |
| Send | Sales Nav InMail compose | LinkedIn JP DM / Wantedly message / Email (himalaya skill) | Form auto-fill + submit |
| Log | sent_history.jsonl | sent_history.jsonl | sent_history.jsonl |

### 11.2 Shared core (implemented)

Non-channel primitives live in `~/.openclaw/skills/_outreach_core/`:

```
_outreach_core/
├── history.py       # skip/sent JSONL, load_global_exclude_set(), canonical_id
├── infer.py         # oc_infer / oc_browser
├── prompt.py        # build_system_block, extract_first_json
├── draft.py         # stage_draft (generic Personalize)
├── preview.py       # interactive Approve prompt helpers
├── approve.py       # Slack pre-send approval
├── config.py        # load_merged_config() + sender_brief.yaml
├── verify.py          # post-send verification, needs_attention.jsonl
├── notify.py          # Slack incoming webhook (one-way)
├── progress.py        # current_task.jsonl + optional heartbeat
└── helpers/
    ├── dump_exclude_set.py
    ├── append_targets.py
    └── backfill_canonical_ids.py
```

`linkedin-outreach` and `jp-form-outreach` import the core via `sys.path` and
keep channel-specific Pull, Enrich, and Send in each `run.py`.

### 11.3 JP-specific design notes

When `jp-outreach` is implemented:

- **Language toggle**: `config.yaml` already has `model.language` (`ja|en`).
  System prompt and few-shot examples need a ja-localized variant in
  `prompts/system_persona_ja.md` and `prompts/examples_ja.md`.
- **Tone**: Japanese B2B outreach is meaningfully more formal than US/EU.
  敬語 baseline, no first-name-only sign-off (full name + title + company).
- **Address forms**: include `〒` postal code + company HQ in signature when
  appropriate; greetings like 「お世話になっております」are expected.
- **Subject conventions**: 「ご相談」「ご提案」「【ご案内】」-style prefixes
  signal intent type, can improve open rates.
- **JP CTA**: instead of a Tenbin link only, also offer a Japanese-language
  fallback (e.g., a TimeRex / Spir link).
- **Compliance**: 特定電子メール法 — for true B2B email outreach, include
  unsubscribe contact + sender's business address. The `prompts/` config
  must enforce this in the system prompt.

### 11.4 Reference targets for jp-outreach

The Japanese ICP (different from linkedin-outreach's foreign-brand-entering-JP
focus) is — depending on product line — something like:

- **For MDオンライン (telemedicine)**: 中小規模クリニック・病院・医療法人の
  理事長 / 院長 / 事務長
- **For Torana Operation Expert services in JP**: 日本の事業会社で海外SaaS/D2C
  と契約している事業責任者（逆方向のオペレーション支援）

These would live in a separate `targets.csv` under
`~/.openclaw/skills/jp-outreach/`.

---

## 12. Known issues / TODO

| Issue | Impact | Mitigation / plan |
|---|---|---|
| Sales Nav person-search returns noisy results (B2B SaaS, coaches, NPOs mixed with consumer brands) | Bad-fit leads waste enrich+draft cycles | Curated CSV is primary lead source; Sales Nav saved search is fallback |
| `lookup-urls` can hit same-name different-person (Sam Corcos → Evan Baehr) | Bad URL gets stored; enrich runs against wrong person; draft generates against wrong content; Sonnet usually SKIPs but not always | Add company-confidence threshold; on `?` match (company mismatch), leave URL empty and require human resolution |
| CSV manual edits sometimes break quoting | Subsequent runs read garbled fields | Always write back with `QUOTE_ALL`; tolerate broken reads by merging stray columns into `note` |
| Sales Nav InMail compose modal selectors hardcoded to current DOM | LinkedIn UI change will break send | Selectors centralized in `_FILL_COMPOSE_JS` / `_CLICK_SEND_JS`; one-place update on UI change |
| `groupPolicy="open"` in Slack | Prompt-injection risk through any Slack message | OpenClaw plugin allowlist; Python only posts via incoming webhook |
| Send-history dedup is by Sales Nav slug | If a lead's slug changes (rare), they could be re-sent | Acceptable given LinkedIn slug stability; monitor for false negatives |
| Cross-channel duplicate detection (slug mismatch) | Same company may appear under different `id` values | `canonical_id` on history entries + `load_global_exclude_set()` for list_builder |

---

## 13. Operational runbook (TL;DR)

### Daily / weekly cadence

```bash
# Daily quick check
cd ~/.openclaw/skills/linkedin-outreach
.venv/bin/python run.py history show

# Weekly campaign (Top 12 + Tier B + Tier C, dedup on already-sent)
.venv/bin/python run.py campaign --input targets.csv --limit 32 --clean
# → prompt: send all / IDs / skip
# → per-draft: Click Send? (y/N)
# → sent_history auto-updated
```

### When a campaign yields 0 leads

Means everything in targets.csv is in sent_history or skip_history. Time to
add fresh executives:

1. Identify next 10 named executives (or ask Claude/agent to curate)
2. Append to targets.csv
3. Re-run campaign

### When `lookup-urls` finds the wrong person

Mark the row's `linkedin_url` as empty in CSV, re-run with `--overwrite` if
needed, or manually paste the correct Sales Nav lead URL.

### When Sonnet's SKIPs feel too strict / too lenient

Edit `prompts/system_persona.md` (strictness rules) or `config.yaml`
(`target_persona` + `personalization.avoid`). Re-run `run.py draft` to
regenerate drafts on existing enriched.jsonl.

---

## 14. Versioning + contracts

This skill currently lives at `~/.openclaw/skills/linkedin-outreach/`.
A clean v1 contract that future skills should adhere to:

- JSONL files use the field set documented in section 3.
- `id` is a stable per-person identifier (Sales Nav slug or
  `"in/" + public-profile-slug`).
- `sent_history.jsonl` and `skip_history.jsonl` are sacred — never rewritten,
  only appended.
- All LLM calls go through `openclaw infer model run --model <model>` for
  unified cost tracking and provider routing.
- Browser commands use `--browser-profile openclaw` (independent Chrome
  profile with persistent LinkedIn login).

---

End of document.
