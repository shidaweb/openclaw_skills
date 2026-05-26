---
name: linkedin-outreach
description: |
  Run a personalized LinkedIn InMail outreach pipeline against a Sales Navigator
  saved search. Use when the user wants to: prospect on LinkedIn, send InMails
  via Sales Nav, run outreach to a saved-search list, generate personalized
  LinkedIn messages at scale, or schedule recurring LinkedIn outreach.

  The skill runs as a Python pipeline (`run.py`) with subcommands:
    fetch-leads -> enrich -> draft -> preview -> send

  In v1 it stops at `preview` (human reviews drafts and copy-pastes the
  approved ones into LinkedIn). Auto-send is added in v2 once parsing is
  stable.
---

# linkedin-outreach

This skill drives a full LinkedIn InMail outreach pipeline using the OpenClaw
browser plugin (Chrome with the user's signed-in profile) and the OpenClaw
inference CLI (Sonnet for personalization, with prompt caching).

## The Outreach Pattern (canonical 6-phase contract)

This skill implements a reusable **outreach pattern** that any future
outreach skill (form-outreach, email-outreach, etc.) should mirror.

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
| 1 | **Pull** | Source leads | Sales Nav saved search URL OR curated CSV | `data/leads.jsonl` (id, name, profile_url) | none |
| 2 | **Enrich** | Per-lead deep context | leads.jsonl + browser | `data/enriched.jsonl` (+ headline/about/recent_activity) | none |
| 3 | **Personalize** | Compose | enriched.jsonl + config.yaml | `data/drafts.jsonl` (+ draft.subject/body OR SKIP) | Sonnet, cached system prompt |
| 4 | **Approve** | Human review | drafts.jsonl | selected ids | none |
| 5 | **Send** | Dispatch | profile_url, subject, body | (browser action) | none |
| 6 | **Log** | Audit trail | sent leads | `data/sent_history.jsonl` | none |

### Contract guarantees

- **Idempotent**: re-running any phase doesn't double-process. SKIP'd leads are
  remembered in `data/skip_history.jsonl`; sent leads in `data/sent_history.jsonl`.
  Both are checked at Pull and Send phases.
- **Resumable**: each phase reads its predecessor's JSONL output. Delete an
  output file to force that phase to re-run.
- **Append-only history**: skip_history and sent_history are never overwritten,
  only extended. To "forget" a lead use `run.py history purge-skip` or
  `purge-sent`.
- **Cache-friendly**: phase 3's system prompt is byte-stable across all leads
  in a run (config.yaml + persona.md + examples.md), giving 90%+ Anthropic
  prompt cache hits.

### One-shot runner

```bash
.venv/bin/python run.py campaign --input targets.csv --limit 5 --clean
```

This runs phases 1-6 in sequence. Phase 4-6 happen inside `preview`'s
interactive prompt (`all` / IDs / `n`).

### Reusing the pattern in new skills

When building a new outreach skill (e.g., `form-outreach`), keep:

- The same 6 phase names and JSONL filenames
- The same skip_history / sent_history files
- The same config.yaml shape (sender / pitch / target_persona / personalization)
- The same `prompts/system_persona.md` + `prompts/examples.md` structure

Only the implementations of Pull / Enrich / Send change per channel:

| Phase | LinkedIn impl | Form-outreach impl (planned) |
|---|---|---|
| Pull | Sales Nav search OR CSV | Curated company list (CSV) |
| Enrich | profile snapshot via openclaw browser | company About/News fetch via openclaw browser/tavily |
| Send | Sales Nav InMail compose (JS-driven) | Form auto-fill (per-domain mapping cached) |

The Personalize / Approve / Log phases are channel-agnostic and can be
shared as a library between skills.

## When to use

Trigger this skill when the user asks for any of:
- "Run LinkedIn outreach" / "LinkedIn 営業" / "LinkedIn のリサーチ"
- "Research [N] leads" / "10件リサーチ" / "5件だけ"
- "Send InMails to my Sales Nav search"
- "Generate personalized LinkedIn messages for [search]"
- "Show me the latest LinkedIn drafts" / "今のドラフト見せて"
- "Schedule weekly LinkedIn prospecting"

## How to interpret common Slack/chat requests

The fastest path is to call `research.py`, which chains fetch-leads → enrich →
draft → preview into one command.

| User intent (en or ja) | Command |
|---|---|
| **"Run the campaign" / "キャンペーン回して"** | `cd ~/.openclaw/skills/linkedin-outreach && .venv/bin/python run.py campaign --input targets.csv --limit 5 --clean` |
| **"Campaign without sending" / "送らずに draft まで"** | `… run.py campaign --input targets.csv --limit 5 --skip-send` |
| **"Run from saved search not CSV" / "保存検索でcampaign"** | `… run.py campaign --search-url "<url>" --limit 10 --clean` |
| "Run a 10-lead research" / "10件リサーチして" | `cd ~/.openclaw/skills/linkedin-outreach && .venv/bin/python research.py --clean --limit 10` |
| "Just 5 this time" / "5件だけ" | `… research.py --clean --limit 5` |
| "Show me the drafts" / "ドラフト見せて" | `… run.py preview --no-send` (display only, no prompt) |
| "Review and send" / "確認して送って" | `… run.py preview` (prompts at end: all / IDs / n) — chains into send |
| "How many sendable?" | Count `drafts.jsonl` entries where `draft.subject != "SKIP"` |
| "Use a different saved search" | Pass `--search-url "..."` to research.py |
| "Tighten the persona" / "もっと絞って" | Edit `config.yaml` `target_persona` section, then re-run `run.py draft` |
| **"Send draft 1" / "1番送って"** | (see Send flow below — TWO-STEP confirmation in chat) |
| **"How many already sent?"** | `cat ~/.openclaw/skills/linkedin-outreach/data/sent_history.jsonl \| wc -l` |
| **"Show skip history" / "スキップ履歴"** | `… run.py history show` |
| **"Look up LinkedIn URLs" / "URL自動取得して" / "name+companyからURL埋めて"** | `… run.py lookup-urls --input targets.csv` |
| **"First N URLs only" / "Tier 1だけ自動で"** | `… run.py lookup-urls --input targets.csv --limit 5` |
| **"Build target list" / "リスト作って"** | List build flow（§下記）— WebSearch + `dump_exclude_set` + `append_targets` |
| **"進捗どう？" / "今何してる？"** | `tail -n 20 data/current_task.jsonl` を要約して返す |
| **"<会社>に <値> で送って"** (needs_attention 応答) | `… run.py resolve --target-id <id> --field key=value` |
| **"全部止めて"** | `pkill -f "run.py send"` + 必要なら webhook で中断通知 |

## Send flow (v2 — full automation with Slack confirmation)

When the user says "send draft N" or "Nを送って":

**Step 1 — preview & ask** (do NOT run send yet)

1. Read `data/drafts.jsonl`, identify the Nth SENDABLE draft (skip entries where `subject == "SKIP"`)
2. Reply in Slack with a preview, like:

   > **Draft 1 — Adrian Trzaskus (Bluprintx COO)**
   > Subject: Tokyo trip — Japan market timing?
   > Body: [first 200 chars]…
   >
   > Sales Navで件名・本文を入力し、Sendボタンを押します。よろしいですか？(yes/no)

**Step 2 — execute on confirmation**

If the user replies "yes" / "はい" / "送って" / "ok":

```bash
cd ~/.openclaw/skills/linkedin-outreach && .venv/bin/python run.py send --ids N --auto-send
```

The script will:
- Open Sales Nav profile
- Click Message button
- Type subject + body into the InMail compose modal
- **Click the Send button automatically**
- Verify the modal closed (= sent)
- Append to `data/sent_history.jsonl`

Reply in Slack with the result:
- ✅ Sent and logged → "Adrian宛のInMail送信完了。記録しました。"
- ⚠ Send button not found → "compose modalが想定構造と違います。data/sample_compose.txt を確認します"
- ⚠ Modal still open after click → "送信できなかった可能性。手動確認してください"

If the user replies "no" / "いいえ" / "やめて":

Reply: "送信は中止しました。" Do not run send.

## Other modes (manual / mixed)

| Mode | Command | Behavior |
|---|---|---|
| Auto-send (Slack normal) | `run.py send --ids N --auto-send` | fill + click Send + log |
| Fill only (paranoid) | `run.py send --ids N --no-confirm` | fill, stop. Human clicks Send. Use `mark-sent` after |
| Interactive (terminal) | `run.py send --ids N` | fill, prompt "y/N" in terminal, then Send if yes |
| Manual log | `run.py mark-sent --ids N` | append to sent_history without sending |

## Sending multiple

> "1番と3番送って" / "Send 1 and 3"

After the user confirms with "yes":

```bash
.venv/bin/python run.py send --ids 1,3 --auto-send
```

The script rate-limits between sends (~30s pause) to look human and avoid LinkedIn spam detection.

When responding in Slack, prefer to:
1. Run the command,
2. Then give a short bullet summary in the chat (sendable count, top 3 names, send rate),
3. Attach the full preview output as a thread reply if needed.

## Direct CLI usage

```bash
cd ~/.openclaw/skills/linkedin-outreach

# One-shot: full pipeline (recommended)
.venv/bin/python research.py --clean --limit 10

# Or stage-by-stage
.venv/bin/python run.py fetch-leads --search-url "<sales nav search url>" --limit 10
.venv/bin/python run.py enrich
.venv/bin/python run.py draft
.venv/bin/python run.py preview

# (v2) Send approved drafts via browser automation
# .venv/bin/python run.py send --ids 1,3,5,7
```

## Configuration

Before first run, copy `config.example.yaml` to `config.yaml` and fill in
the pitch / persona / value props. This file becomes the cached system
prompt — keep it stable across runs to maximize cache hits.

## Cost profile

With Sonnet and prompt caching, expect ~$0.005 per drafted InMail
(system prompt ~95% cached, output ~500 tokens). 100 InMails/month
runs at well under $1 in API equivalent — most cost is on the
Sales Nav subscription itself, not the LLM.

## List build flow (agent-led)

User 例: 「EdTech 中堅 10 社、フォームと LinkedIn に振り分けてリスト化」

1. `cat ~/.openclaw/skills/sender_brief.yaml` で送信者文脈
2. `python3 -m _outreach_core.helpers.dump_exclude_set` で除外 ID（JSON）
3. WebSearch で実在企業を抽出（PR TIMES / IR / 公式で検証。**捏造禁止**）
4. 候補を Slack で表提示 → ユーザー確認
5. OK 後:
   ```bash
   echo '<JSON array>' | python3 -m _outreach_core.helpers.append_targets --skill linkedin --input - --format jsonl
   echo '<JSON array>' | python3 -m _outreach_core.helpers.append_targets --skill jp_form --input - --format jsonl
   ```
6. enrich → draft → preview に進むか確認

## Send verification & escalation

`run.py send --ids N --auto-send` 後、各件は自動 verify されます。

Slack（incoming webhook 経由）のサイン:
- ✅ `<会社名>` 送信完了 → `sent_history.jsonl` 記録済
- ⚠️ 送信完了が確認できません → 手動確認をユーザーに依頼
- ⚠️ 想定外の入力項目 → `run.py resolve --target-id <id> --field ...`

`data/needs_attention.jsonl` に保留。一覧: `run.py history needs-attention`

## Heartbeat behavior

長時間 send では `--heartbeat slack` を付与（webhook URL 設定時のみ 5 分毎に状況投稿）:

```bash
.venv/bin/python run.py send --ids 1,2,3 --auto-send --heartbeat slack
```

未指定時は従来どおり（バックグラウンドスレッドなし）。

## Notes

- Browser profile: `openclaw` (independent Chrome, login persists in `~/.openclaw`)
- Models: `claude-cli/claude-sonnet-4-6` by default (configurable)
- State files in `data/*.jsonl` are append-only and resumable
- Rate limiting: `fetch-leads` and `enrich` sleep between page loads
