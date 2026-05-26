---
name: jp-form-outreach
description: |
  Run a personalized B2B inquiry-form outreach pipeline against a curated list
  of Japanese mid-market companies. Use when the user wants to: prospect via
  Japanese corporate inquiry forms (`/contact/`, `/inquiry/`), submit
  personalized B2B 提案 to a curated company list, run outreach to JP companies
  whose decision-makers can't be reached via LinkedIn, or schedule recurring
  JP-form prospecting.

  The skill runs as a Python pipeline (`run.py`) with subcommands:
    bootstrap (Pull) -> enrich -> draft -> preview -> send

  In v1 it can stop at `preview` (human reviews drafts). The `send` phase
  drives the actual form (fill + click confirm + click final send) via
  openclaw browser. Sister skill to `linkedin-outreach`.
---

# jp-form-outreach

This skill drives a full Japanese B2B inquiry-form outreach pipeline using
the OpenClaw browser plugin (Chrome with the openclaw profile) and the
OpenClaw inference CLI (Sonnet for personalization, with prompt caching).

## The Outreach Pattern (canonical 6-phase contract)

This skill implements the same reusable **outreach pattern** as
`linkedin-outreach`. Same phase names, same JSONL filenames, same
skip/sent history files.

```
  ┌─────────┐   ┌─────────┐   ┌─────────────┐   ┌─────────┐   ┌──────┐   ┌─────┐
  │ 1. PULL │ → │ 2. ENRI │ → │ 3. PERSONAL │ → │ 4. APPR │ → │5.SEND│ → │6.LOG│
  │         │   │   CH    │   │     IZE     │   │   OVE   │   │      │   │     │
  └─────────┘   └─────────┘   └─────────────┘   └─────────┘   └──────┘   └─────┘
   leads.jsonl   enriched      drafts.jsonl     (interactive)  (browser)  sent_history
                  .jsonl                                                    .jsonl
```

| # | Phase | Role | Inputs | Outputs | LLM use |
|---|---|---|---|---|---|
| 1 | **Pull** | Source companies | targets.yaml (curated list) | `data/leads.jsonl` (id, name, form_url, industry, hook_context) | none |
| 2 | **Enrich** | Per-company form structure | leads.jsonl + browser | `data/enriched.jsonl` (+ form_fields, char_limit, captcha, flow) | none |
| 3 | **Personalize** | Compose | enriched.jsonl + config.yaml | `data/drafts.jsonl` (+ draft.subject/body OR SKIP) | Sonnet, cached system prompt |
| 4 | **Approve** | Human review | drafts.jsonl | selected ids | none |
| 5 | **Send** | Dispatch | form_url, body, field_map | (browser action: fill → 確認 → 送信) | none |
| 6 | **Log** | Audit trail | sent companies | `data/sent_history.jsonl` | none |

### Contract guarantees

- **Idempotent**: re-running any phase doesn't double-process. SKIP'd
  companies live in `data/skip_history.jsonl`; sent companies in
  `data/sent_history.jsonl`. Both filtered at Pull and Send phases.
- **Resumable**: each phase reads its predecessor's JSONL output.
  Delete an output file to force that phase to re-run.
- **Append-only history**: skip_history and sent_history are never
  overwritten, only extended. To "forget" use
  `python run.py history purge-skip`/`purge-sent`.
- **Cache-friendly**: phase 3's system prompt is byte-stable across all
  targets in a run (config.yaml + persona.md + examples.md), giving
  90%+ Anthropic prompt cache hits.

### One-shot runner

```bash
.venv/bin/python run.py campaign --clean --skip-send
```

This runs phases 1-3 then stops at preview (display only). Drop
`--skip-send` to chain into the interactive Approve→Send→Log loop.

### What's different from linkedin-outreach

Same pattern, different channel implementations:

| Phase | LinkedIn impl | Form-outreach impl |
|---|---|---|
| Pull | Sales Nav saved search OR CSV → leads.jsonl | Curated targets.yaml → leads.jsonl |
| Enrich | profile snapshot via openclaw browser → headline/about/recent_activity | form snapshot via openclaw browser → form_fields/char_limit/captcha/flow |
| Send | Sales Nav InMail compose (JS-driven, 2-field: subject + body) | Form auto-fill (JS-driven, N fields per company; field_map_overrides) |

Personalize / Approve / Log phases are channel-agnostic and use the
same code paths.

## When to use

Trigger this skill when the user asks for any of:
- "Run JP form outreach" / "国内営業フォーム回して"
- "Send inquiry forms to my target list" / "問い合わせフォーム送って"
- "Generate personalized form messages for [N] JP companies"
- "Show me the current form drafts" / "今のフォーム下書き見せて"
- "Schedule weekly JP form prospecting"

Do NOT use for:
- Sales/contact automation in non-JP markets (LinkedIn / cold email instead)
- Form submissions on consumer-facing pages (use case is B2B inquiry only)

## How to interpret common Slack/chat requests

| User intent (en or ja) | Command |
|---|---|
| **"Run the campaign" / "キャンペーン回して"** | `cd ~/.openclaw/skills/jp-form-outreach && .venv/bin/python run.py campaign --clean` |
| **"Campaign without sending" / "送らずに draft まで"** | `… run.py campaign --clean --skip-send` |
| **"Skip enrich, just draft from targets" / "form構造取らずに draft"** | `… run.py campaign --clean --skip-enrich --skip-send` |
| "Show me the drafts" / "ドラフト見せて" | `… run.py preview --no-send` (display only) |
| "Review and send" / "確認して送って" | `… run.py preview` (prompts at end: all / IDs / n) → chains into send |
| "How many sendable?" | Count `drafts.jsonl` entries where `draft.subject != "SKIP"` |
| "Tighten the persona" / "もっと絞って" | Edit `config.yaml` `target_persona` section, then re-run `run.py draft` |
| "Add a new target" | Append to `targets.yaml`, then `run.py bootstrap` |
| **"Send draft 1" / "1番送って"** | (see Send flow below — TWO-STEP confirmation in chat) |
| **"How many already sent?"** | `cat ~/.openclaw/skills/jp-form-outreach/data/sent_history.jsonl \| wc -l` |
| **"Show sent history" / "送信履歴"** | `… run.py history show` |
| **"Build target list" / "リスト作って"** | List build flow（§下記） |
| **"進捗どう？"** | `tail -n 20 data/current_task.jsonl` を要約 |
| **needs_attention への回答** | `… run.py resolve --target-id <id> --field 業界=その他` |
| **"全部止めて"** | `pkill -f "run.py send"` |

## Send flow (with Slack confirmation)

When the user says "send draft N" or "Nを送って":

**Step 1 — preview & ask** (do NOT run send yet)

1. Read `data/drafts.jsonl`, identify the Nth SENDABLE draft (skip
   entries where `subject == "SKIP"`)
2. Reply in Slack with a preview, like:

   > **Draft 1 — 株式会社○○ (industry: edtech, founded 2008)**
   > URL: https://corp.example.jp/contact/
   > Flow: confirm (2-step)
   > Captcha: none
   > Body: [first 250 chars]…
   >
   > フォームに入力→「入力内容を確認する」→「送信する」を実行します。よろしいですか？(yes/no)

**Step 2 — execute on confirmation**

If the user replies "yes" / "はい" / "送って" / "ok":

```bash
cd ~/.openclaw/skills/jp-form-outreach && .venv/bin/python run.py send --ids N --auto-send
```

The script will:
- Open the company's `form_url`
- Apply `entry_click_text` (e.g. "法人のお客様" tab) if specified
- Fill all sender fields (name / kana / 会社 / phone / email / 住所…) via
  label-pattern matching
- Apply `field_map_overrides` (category radio, select dropdown, gender,
  contact_method, etc.)
- Fill the largest textarea with the draft body
- Check the agreement checkbox (label match: 同意 / プライバシー / etc.)
- For `flow: confirm` — click "確認" → wait → click "送信する"
- For `flow: single` — click "送信する" once
- Verify success page (heuristic: 送信完了 / ありがとうございました / 完了画面 / THANKS)
- Append to `data/sent_history.jsonl`

Reply in Slack with the result:
- ✅ Sent and logged → "○○宛のフォーム送信完了。記録しました。"
- ⚠ Captcha v2 detected → "reCAPTCHA v2 visible — fill-only mode に
  フォールバックしました。手動でCAPTCHA→送信してください"
- ⚠ Final submit button not found → "確認画面で『送信する』が見つから
  ない。data/sample_form.txt を確認します"
- ⚠ No success keywords detected → "送信完了の確認ができません。
  手動確認してください"

If the user replies "no" / "いいえ" / "やめて":

Reply: "送信は中止しました。" Do not run send.

## Other modes (manual / mixed)

| Mode | Command | Behavior |
|---|---|---|
| Auto-send (Slack normal) | `run.py send --ids N --auto-send` | fill + 確認 + 送信 + log |
| Fill only (paranoid) | `run.py send --ids N --no-confirm` | fill, stop. Human clicks 確認/送信. Use `mark-sent` after |
| Interactive (terminal) | `run.py send --ids N` | fill, prompt "y/N" in terminal, then proceed if yes |
| Manual log | `run.py mark-sent --ids N` | append to sent_history without sending |

## Sending multiple

> "1番と3番送って" / "Send 1 and 3"

After the user confirms with "yes":

```bash
.venv/bin/python run.py send --ids 1,3 --auto-send
```

The script rate-limits between sends (~30s pause) to look human.

## Direct CLI usage

```bash
cd ~/.openclaw/skills/jp-form-outreach

# One-shot: full pipeline (recommended)
.venv/bin/python run.py campaign --clean

# Or stage-by-stage
.venv/bin/python run.py bootstrap                 # Pull
.venv/bin/python run.py enrich                    # Enrich
.venv/bin/python run.py draft                     # Personalize
.venv/bin/python run.py preview                   # Approve (interactive)

# Send approved drafts
.venv/bin/python run.py send --ids 1,3,5 --auto-send
```

## List build flow (agent-led)

（linkedin-outreach/SKILL.md と同手順。`append_targets --skill jp_form` / `linkedin` を使い分け）

**禁止:** 実在しない企業の捏造。PR TIMES / IR / 公式サイトで検証してから採用。

## Send verification & escalation

`run.py send --ids N --auto-send` 後に verify。想定外の必須フィールドは `needs_attention` へ。

```bash
.venv/bin/python run.py history needs-attention
.venv/bin/python run.py resolve --target-id <id> --field 業界=その他 --field 紹介者=なし
```

Webhook 通知（`sender_brief.yaml` の `slack.incoming_webhook_url`）:
- ✅ 送信完了
- ⚠️ 完了画面未確認 / 想定外フィールド

## Heartbeat behavior

```bash
.venv/bin/python run.py send --ids all --auto-send --heartbeat slack
```

`--heartbeat slack` 未指定時は従来どおり（5 分毎投稿なし）。

## needs_attention の取り扱い

1. Slack でユーザーに不足フィールドの値を聞く
2. `run.py resolve --target-id ... --field key=value` で overrides 更新 → 自動再 send
3. 解決後 `history needs-attention` で open が減っていることを確認

## Configuration

Before first run:

```bash
cp config.example.yaml config.yaml      # edit sender / pitch
cp targets.example.yaml targets.yaml    # edit / extend (22 pre-loaded)
```

`config.yaml` becomes the cached system prompt — keep it stable across
runs to maximize cache hits. `targets.yaml` is the human-curated source
of truth for who to contact.

### targets.yaml shape (per-company)

```yaml
companies:
  - id: yarukiswitch_hd                    # stable slug, used as dedup key
    name: 株式会社やる気スイッチグループHD
    industry: edtech_fc
    founded: 1989
    status: pending                        # pending / sent / blocked / manual / linkedin / dropped
    category: b2b_form                     # b2b_form / b2c_only / iframe / site_closed
    form_url: https://...
    char_limit: 400                        # form's body maxlength
    flow: confirm                          # single (1-click) / confirm (2-step)
    captcha: none                          # none / recaptcha_v2_visible / recaptcha_v3_invisible
    direct_signals:                        # specific facts the LLM can quote
      - "スクールIE 1,200教室超"
      - "グループ売上578億円"
    hook_context: |                        # 1-3 sentence narrative
      FC型こども学習事業、本部主導CRM未整備...
    hypothesized_pain:                     # CRM gaps the LLM can address
      - "教室ごとにLINE分散、本部主導の統一運用なし"
    field_map_overrides:                   # per-form non-standard fields
      category_radio: "その他"
      address_required: true
```

## Cost profile

With Sonnet and prompt caching, expect ~$0.005 per drafted form
message (system prompt ~95% cached, output ~500 tokens). Same as
linkedin-outreach.

## Notes

- Browser profile: `openclaw` (independent Chrome, no login needed for
  most JP corporate inquiry forms)
- Models: `claude-cli/claude-sonnet-4-6` by default (configurable)
- State files in `data/*.jsonl` are append-only and resumable
- Rate limiting: enrich and send sleep between page loads / sends
- Forms hosted in iframes (kintone / BowNow / Microsoft Forms) flagged
  as `category: iframe` and skipped — handle manually for now
