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

## Auto-acknowledge (MANDATORY, 最優先ルール)

非自明なリクエスト（fetch-leads / campaign / draft / send / preview / status 確認 など）
を Slack で受けたら、**他のどの SKILL.md 規約より先に**、agent は 5 秒以内に必ず
thread 返信する:

```
👍 受信しました。<brief_id> · <skill> で進めます。
推定所要 ~X 分です。
```

- ack 投稿は **thread 内**で行う
- ack を出すまで OpenClaw / run.py のサブコマンドは 1 つも叩かない
- ack 後に Session start confirmation（次節）→ Stateless reconstruction → 実作業
- もし brief / channel 未確定で確認が要る場合、ack の続きで「ところで、どの brief で
  進めますか？」と質問する形にする（**先に ack だけは必ず出す**）
- 「ping」「生きてる？」「status」のような軽量問い合わせは、ack 不要で 5 秒以内に
  本回答を返す（§15-B 参照）

**なぜ必須か**: ユーザーは Slack で命令を投げた後「届いた？動いてる？」と不安になる。
無音時間を絶対に作らない設計が、OpenClaw 系プロダクトの操作可能感を担保する。


## Session start: brief & channel confirmation (MANDATORY)

新規セッションで fetch / campaign / draft / send など **データ生成・送信を伴う**
リクエストの前に Slack で brief と channel（linkedin / jp_form / 両方）を確認する。
`python3 -m _outreach_core.helpers.brief list` で一覧。確定後は **全 `run.py` に `--brief <id>`** を付与。

| ユーザー発話 | 行動 |
|---|---|
| brief 一覧 / 人格教えて | `brief list` |
| `<id> で` | セッション brief 確定 |
| 今どの brief？ | `brief list` + `data/channel_state/<channel_id>.json` |
| brief を `<id>` に変えて | `brief bind --channel-id $CH --brief <id>` |
| 進捗どう？ | § Stateless context reconstruction（`brief status` / `events.jsonl`） |
| 品質ポイント教えて | `../report draft-quality --since 7d` |
| 送信ファネル見せて | `../report send-funnel --since 7d` |
| needs_attention まとめて | `../report needs-attention` |
| 新しい brief を作って | §14-N onboarding → `brief write-from-json` |
| 全部止めて | `brief stop-run --brief <id>` |
| **ping** / **生きてる？** | `cd ~/.openclaw/skills && ./healthcheck ping` |
| **status** / **詳しく** | `./healthcheck status` |
| **watchdog 元気？** | `tail -1 ~/.openclaw/skills/data/watchdog.log` |

**Slack ターンをブロックしない（最重要・§15）**: 長時間タスクは前景実行せず detached 起動:

```bash
cd ~/.openclaw/skills
./job start linkedin-outreach campaign --brief torana-line-crm --input targets/torana-line-crm.csv --limit 5 \
  --slack-channel-id "$DOORMAN_SLACK_CHANNEL_ID" --slack-thread-ts "$DOORMAN_SLACK_THREAD_TS"
# → 即 run_id 返却。開始🚀 / 心拍… / 終了✅❌ は Python が直接 Slack 投稿。
```

進捗・`history`・`brief list` のみ確認省略可。

**Slack バインド済みチャンネル**では brief 確認を省略（`data/channel_state/<channel_id>.json`）。環境変数 `DOORMAN_SLACK_CHANNEL_ID` / `DOORMAN_SLACK_THREAD_TS` を `run.py` に渡す。

## Stateless context reconstruction

新スレッドでも file から再構築（`jp-form-outreach/SKILL.md` と同手順）:

1. `data/channel_state/<channel_id>.json`
2. `data/briefs/<id>/active_run.lock`
3. `data/briefs/<id>/current_task.jsonl` 末尾
4. `data/briefs/<id>/events.jsonl` 直近
5. `data/briefs/<id>/needs_attention.jsonl` の open

```bash
python3 -m _outreach_core.helpers.brief status --brief torana-line-crm --skill linkedin-outreach
```

Cookie 同意バナーは page open 直後に自動 dismiss（`browser.cookie_consent` in brief YAML）。InMail 送信は Slack 確認後 `--auto-send`（`stage_send` は stdin 不使用）。

## Health check commands（§15-B）

`jp-form-outreach/SKILL.md` の Health check セクションと同じ。`./healthcheck ping` / `status` / `touch-command`。長時間タスク中は `HeartbeatSession` が `data/system_health/<host>.json` を自動更新。

This skill drives a full LinkedIn InMail outreach pipeline using the OpenClaw
browser plugin (Chrome with the user's signed-in profile) and the OpenClaw
inference CLI (Sonnet for personalization, with prompt caching).

## Model assumption (v4)

| Layer | Model | Where configured |
|---|---|---|
| Slack で会話する OpenClaw エージェント | **Opus 4.7（固定）** | OpenClaw gateway / agent profile（本リポジトリ外） |
| `run.py draft` / `_refine_draft` | **Opus 4.7** | `config.yaml` → `model.name`（`claude-cli/claude-opus-4-7`） |
| `verify.py` / webhook / heartbeat | **LLM なし** | `_outreach_core`（純 Python） |

リスト生成・承認・needs_attention への質問は **Opus エージェント**が担当。Python から Opus を呼ぶ CLI は作らない。

## OpenClaw エージェント: 進捗通知（必須）

長時間タスク（リサーチ・enrich・draft・send・campaign）では **ユーザーが不安にならないよう、約5分ごとに Slack へ状況を出す**。口だけ「進捗を共有します」は禁止。

### やること（この順）

1. **パイプライン起動** — 必ず次のいずれか（`--heartbeat auto` が既定）:
   ```bash
   cd ~/.openclaw/skills/linkedin-outreach
   .venv/bin/python research.py --clean --limit 10
   # または
   .venv/bin/python run.py campaign --search-url "..." --limit 10 --clean
   ```
   `research.py` は内部で `heartbeat_watch.py` も起動し、サブプロセス中も Slack に投稿する。

2. **手動で run.py を段階実行する場合** — 先にバックグラウンド watcher を起動:
   ```bash
   cd ~/.openclaw/skills/linkedin-outreach
   nohup .venv/bin/python heartbeat_watch.py >> /tmp/doorman-hb.log 2>&1 &
   # 完了後: kill %1  （またはプロセスを terminate）
   ```

3. **待機中・「進捗どう？」** — 5分経過ごと、またはユーザーが聞いたら:
   ```bash
   .venv/bin/python pipeline_status.py
   .venv/bin/python heartbeat_watch.py --once
   ```
   出力を **Slack スレッドに要約して投稿**（ログを貼るだけでも可）。

4. **終了時** — `research` / `campaign` のサマリ + sendable 件数 + 「送信はまだ。N番送ってと言ってください」。

### 投稿経路

- Python: `notify.py` → OpenClaw `channels.slack.botToken` + 直近セッションの `#general` 等
- ログ: `data/current_task.jsonl`（`pipeline_status.py` で人間可読）

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
| 3 | **Personalize** | Compose | enriched.jsonl + config.yaml | `data/drafts.jsonl` (+ draft.subject/body OR SKIP) | Opus (`model.name`), cached system prompt |
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
| "Run a 10-lead research" / "10件リサーチして" | `cd ~/.openclaw/skills/linkedin-outreach && .venv/bin/python research.py --clean --limit 10`（**進捗は自動で約5分毎 Slack**） |
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
| **"進捗どう？" / "今何してる？"** | `cd …/linkedin-outreach && .venv/bin/python pipeline_status.py` または `tail -n 20 data/current_task.jsonl` を要約 |
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

## List build flow (agent-led, Opus 4.7 前提)

User 例: 「EdTech 中堅 10 社、フォームと LinkedIn に振り分けてリスト化」

> Sonnet エージェントで動かすと候補品質が落ちます。本フローは Opus 4.7 エージェント前提です。

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

`run.py send --ids N --auto-send` 後、各件は **決定論的に** verify されます（LLM 不使用）。

Slack（incoming webhook 経由）のサイン:
- ✅ `<会社名>` 送信完了 → `sent_history.jsonl` 記録済
- ⚠️ 送信完了が確認できません → **Opus エージェント**が手動確認をユーザーに依頼
- ⚠️ 想定外の入力項目 → **Opus エージェント**が値を聞き出し → `run.py resolve --target-id <id> --field ...`

`data/needs_attention.jsonl` に保留。一覧: `run.py history needs-attention`

## Heartbeat behavior（5 分毎の進捗共有）

`heartbeat.enabled_for` に `all` を含めると（Slack 投稿は `incoming_webhook_url` または OpenClaw の `botToken` + セッション channel）、
`research.py` / `run.py fetch-leads|enrich|draft|send|campaign` は **デフォルト `--heartbeat auto`** で:

- `data/current_task.jsonl` に常時ログ
- 約 5 分毎に Slack へ「今どの段階・何件目か」を投稿（開始・終了も）

```bash
.venv/bin/python research.py --clean --limit 10          # auto heartbeat
.venv/bin/python run.py send --ids 1,2,3 --auto-send     # 同上（--heartbeat auto が既定）
.venv/bin/python run.py enrich --heartbeat off           # 無効化
```

エージェントは長時間タスクで **必ず** `research.py` / `run.py campaign` を使うか、`heartbeat_watch.py` をバックグラウンド起動する。待機中は **5分ごと** に `heartbeat_watch.py --once` または `pipeline_status.py` を実行し、結果を Slack に投稿する。

## バックグラウンド（headless）ブラウザ

OpenClaw の `openclaw` プロファイルは **headless Chrome** で動かせる（ウィンドウ非表示、ログイン状態は `~/.openclaw/browser/openclaw/` に保持）。

**Doorman 既定**（`sender_brief.yaml`）:

```yaml
browser:
  headless: true
```

`research.py` / パイプライン開始時に `openclaw browser start --headless` を試みる。

**初回だけ**、いまウィンドウ付き Chrome が既に動いている場合は headless に切り替わらない。一度止めてから:

```bash
openclaw browser --browser-profile openclaw stop
cd ~/.openclaw/skills/linkedin-outreach && .venv/bin/python research.py --clean --limit 10
```

恒久設定（OpenClaw 全体）:

```bash
openclaw config patch --stdin <<'EOF'
{"browser":{"profiles":{"openclaw":{"headless":true}}}}
EOF
```

環境変数だけで上書き: `DOORMAN_BROWSER_HEADLESS=1` または `OPENCLAW_BROWSER_HEADLESS=1`。

注意: LinkedIn / Sales Nav は headless 検知で挙動が変わることがある。失敗したら `browser.headless: false` に戻す。

## Troubleshooting「止まってるように見える」

1. **プロセス確認**: `pgrep -fl 'research.py|linkedin-outreach.*run.py'` — 何も出なければ既に終了
2. **成果物の時刻**: `ls -la data/*.jsonl` — `drafts.jsonl` が更新されていれば draft まで完了
3. **進捗ログ**: `tail data/current_task.jsonl` — 空なら heartbeat 未使用の古い実行
4. **preview でブロック**: 非 TTY では `research.py` が自動で `--no-send` を付ける。手動 `run.py preview` は `--no-send` 推奨
5. **Slack に何も来ない**: `incoming_webhook_url` が空でも、`~/.openclaw/openclaw.json` の `channels.slack.botToken` と直近の Slack セッション（例: #general）があれば `chat.postMessage` で投稿する。どちらも無い場合のみ no-op

## Notes

- Browser profile: `openclaw` (independent Chrome, login persists in `~/.openclaw`)
- Python `oc_infer`: `claude-cli/claude-sonnet-4-6` via `config.yaml` `model.name`（Opus にしない）
- State files in `data/*.jsonl` are append-only and resumable
- Rate limiting: `fetch-leads` and `enrich` sleep between page loads
