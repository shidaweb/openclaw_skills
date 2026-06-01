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

## Auto-acknowledge (MANDATORY, 最優先ルール)

非自明なリクエスト（list-build / campaign / draft / send / preview / enrich /
status 確認 など）を Slack で受けたら、**他のどの SKILL.md 規約より先に**、
agent は 5 秒以内に必ず thread 返信する:

```
👍 受信しました。<brief_id> · <skill> で進めます。
推定所要 ~X 分です。
```

- ack 投稿は **thread 内**で行う（親階層は run 開始時の状態通知のみ）
- ack を出すまで OpenClaw / run.py のサブコマンドは 1 つも叩かない
- ack 後に Session start confirmation（次節）→ Stateless reconstruction → 実作業
- もし brief / channel 未確定で確認が要る場合、ack の続きで「ところで、どの brief で
  進めますか？」と質問する形にする（**先に ack だけは必ず出す**）
- 「ping」「生きてる？」「status」のような軽量問い合わせは、ack 不要で 5 秒以内に
  本回答を返す（§15-B 参照）

**なぜ必須か**: ユーザーは Slack で命令を投げた後「届いた？動いてる？」と不安になる。
無音時間を絶対に作らない設計が、OpenClaw 系プロダクトの操作可能感を担保する。


## Session start: brief & channel confirmation (MANDATORY)

新規セッションで list-build / campaign / draft / send / preview など
**データ生成・送信を伴う**リクエストを受けたら、起動前に Slack で次を確認する:

1. **brief** — `python3 -m _outreach_core.helpers.brief list` で一覧。例:
   「📇 どの brief で進めますか？ [既定] torana-line-crm — トラーナ LINE×CRM …」
2. **channel** — brief の `desired_channels` から jp_form / linkedin / 両方を選ばせる。

確定後は全 `run.py` 呼び出しに `--brief <id>` を付ける（省略時は `briefs/_active.txt`）。
進捗照会・`brief list`・`history show` だけは確認省略可。

| ユーザー発話 | 行動 |
|---|---|
| brief 一覧 / 人格教えて | `brief list` |
| `<id> で` | セッション brief 確定（`_active.txt` は変えない） |
| `<id> を既定に` | `brief set-active <id>` |
| 今どの brief？ | `brief list` + このチャンネルの `data/channel_state/<id>.json` |
| brief を `<id>` に変えて | `brief bind --channel-id $CH --brief <id>` |
| 進捗どう？ | § Stateless context reconstruction（ファイルから再構築） |
| 品質ポイント教えて | `../report draft-quality --since 7d` |
| 送信ファネル見せて | `../report send-funnel --since 7d` |
| needs_attention まとめて | `../report needs-attention` |
| 全部止めて | `brief stop-run --brief <id>` |
| **ping** / **生きてる？** | `cd ~/.openclaw/skills && ./healthcheck ping`（5 秒以内に 1 行返答） |
| **status** / **詳しく** | `./healthcheck status` |
| **watchdog 元気？** | `tail -1 data/watchdog.log` |

**Slack 経由**: チャンネルが `data/channel_state/<channel_id>.json` にバインド済みなら、毎スレッドの brief 確認は省略し、一行「`torana-line-crm` × jp_form で進めます」と明示してから実行。未バインドチャンネルは §14-N onboarding wizard。

**環境変数**（OpenClaw が `run.py` 起動時に設定）:
- `DOORMAN_SLACK_CHANNEL_ID` — brief 自動解決
- `DOORMAN_SLACK_THREAD_TS` — ハートビートを同スレッドに連投

```bash
# 例（CLI 直叩き）
.venv/bin/python run.py campaign --brief torana-line-crm --clean
# または
python3 ../brief bind --channel-id C09... --brief torana-line-crm
```

## Health check commands（§15-B）

Slack で **ping / 生きてる？ / status** を受けたら Auto-ack 不要で即答:

```bash
cd ~/.openclaw/skills
./healthcheck ping      # 1 行: heartbeat 経過秒・active runs・needs_attention
./healthcheck status    # ping + system_health JSON + events 末尾
./healthcheck touch-command   # Slack 受信時に last_command_at を更新（任意）
```

heartbeat は専用 cron 不要。実行中は `HeartbeatSession` が**実進捗（tick）ごと**に
`system_health/<host>.json` を更新し、`./healthcheck ping/status` は呼ばれる度に
その場で再計算するため常に最新を返す。

**watchdog（§15-C、インストール済み）**: `scripts/install-watchdog.sh` で launchd 60 秒 tick。
監視対象は launchd 管理の `ai.openclaw.gateway`（agent runtime）。

- gateway プロセス**死亡**は gateway 自身の launchd `KeepAlive` が自動復旧（OS レベル）。
- watchdog は `openclaw health` で**応答性**を確認し、連続失敗時のみ
  `launchctl kickstart -k` で**hung した gateway を強制再起動**（10 分 3 回まで、超過で手動エスカレーション）。
- 実行中 run の heartbeat が 5 分以上止まったら「タスク詰まり」を Slack 警告。
- 状態確認: `tail -5 data/watchdog.log` / 再インストール `scripts/install-watchdog.sh` / 解除 `scripts/uninstall-watchdog.sh`。

## Stateless context reconstruction

新スレッドで命令を受けたら、会話履歴に頼らず **必ず file から** 状況を再構築してから応答する:

1. `data/channel_state/<channel_id>.json` — brief と channels
2. `data/briefs/<id>/active_run.lock` — 進行中 run（pid / stage / thread_ts）
3. `data/briefs/<id>/current_task.jsonl` 末尾 — heartbeat 進捗
4. `data/briefs/<id>/events.jsonl` 直近 — draft/send イベント
5. `data/briefs/<id>/needs_attention.jsonl` の open — 判断待ち

未読のまま「何をしましょうか？」だけ返さない。

## Slack-native onboarding wizard

「新しい brief を作って」「セットアップして」→ Slack で 10〜15 問（sender / product / pitch / target / channels）。完了後:

```bash
# エージェントが answers JSON を組み立てたあと（例: briefs/onboarding_answers.example.json）
python3 -m _outreach_core.helpers.brief write-from-json <slug> \
  --answers /path/to/answers.json \
  --display-name "表示名" \
  --bind-channel $DOORMAN_SLACK_CHANNEL_ID
```

詳細は `CURSOR_INSTRUCTIONS.md` §14-N。

This skill drives a full Japanese B2B inquiry-form outreach pipeline using
the OpenClaw browser plugin (Chrome with the openclaw profile) and the
OpenClaw inference CLI (Sonnet for personalization, with prompt caching).

## Model assumption (v4)

| Layer | Model | Where configured |
|---|---|---|
| Slack で会話する OpenClaw エージェント | **Opus 4.7（固定）** | OpenClaw gateway（本リポジトリ外） |
| `run.py draft` / `_refine_draft` | **Opus 4.7** | `config.yaml` → `model.name` |
| `_llm_analyze_form` | **Sonnet 4.6** | `config.yaml` → `model.form_analyzer_name` |
| `verify.py` / webhook / heartbeat | **LLM なし** | `_outreach_core` |

リスト生成・enrich-research・承認・needs_attention への質問は Opus エージェントが担当。

### Enrich research（v4）

`enrich` の前に、エージェントが各社の PR TIMES / IR / プレスリリースを WebSearch で集め、`targets.yaml` または `enriched.jsonl` の `direct_signals` / `hook_context` を埋める。薄いまま draft すると `INSUFFICIENT_DATA` で SKIP されやすい。

### Form scope（v4）

`_FORM_FIELDS_JS` と `SCAN_REQUIRED_JS` は **textarea を含む最大の `<form>`** に限定。サンクスページのログインフォーム required は verify で無視（success 判定を先に行う）。

### resolve --action proceed

reCAPTCHA / 確認画面待ちで `needs_attention` になったら、ユーザーがブラウザ操作を終えたあと:

```bash
.venv/bin/python run.py resolve --target-id <id> --action proceed
```

### Draft quality gate（v4 B-6、エージェント主導）

`run.py draft` 完了後、**preview の前**に Opus エージェントが `data/drafts.jsonl` を読み、各 draft を high / mid / low で評価:

1. 冒頭が型 1–4 のどれか、直近2件と被っていないか
2. 自己紹介が直前3件とコピペになっていないか
3. 数字・固有事実が相手の業界構造に接続されているか
4. `char_limit` / `max_chars_used` 以内か
5. `INSUFFICIENT_DATA` でないか

**low** だけ再生成:

```bash
.venv/bin/python run.py draft --refine --from-targets   # または enriched から対象 id のみ
```

一括でコストを抑える場合は初回を `--refine-only-if-low-quality` にし、low 評価分だけ `--refine` を回す。

### 動的 required（v4 A-3）

ラジオ選択後に出る必須項目は `fill_form_with_plan` が検知する。plan にあれば自動入力、なければ `needs_attention` + Slack → `resolve --field key=value` で再送。

## OpenClaw エージェント: 応答保証と進捗通知（必須）

### Slack ターンをブロックしない（最重要・§15）

長時間タスク（campaign / send / enrich / draft）は **前景実行禁止**。
必ず detached 起動し、即座に run_id を返してターンを終える:

```bash
cd ~/.openclaw/skills
./job start jp-form-outreach campaign --brief torana-line-crm --limit 5 \
  --slack-channel-id "$DOORMAN_SLACK_CHANNEL_ID" --slack-thread-ts "$DOORMAN_SLACK_THREAD_TS"
```

`./job start` が **開始 🚀 / 心拍 … / 終了 ✅❌**（成功・失敗・例外いずれも）を
Python から直接 Slack 投稿する。エージェントは起動後すぐ自由になる。

- 受信したら **5 秒以内に一言 ack**（必要なら `./healthcheck touch-command`）
- 「進捗どう？」→ `./healthcheck ping` / `./brief status --brief <id>`（file から即答）
- 詳細は [`docs/OPENCLAW_AGENT.md`](../docs/OPENCLAW_AGENT.md)

### 段階実行（手動で stage を回す稀なケース）

```bash
cd ~/.openclaw/skills/jp-form-outreach
nohup .venv/bin/python heartbeat_watch.py >> /tmp/doorman-hb.log 2>&1 &
.venv/bin/python run.py send --ids 1 --auto-send --heartbeat auto
```

### 構造化ログ（v4 §13）

長時間キャンペーンでは `data/events.jsonl` と `data/traces/<run_id>/<target_id>/` にフルトレースが溜まる（Git 対象外）。`stage_campaign` 開始時に `run_id` が表示される。

```bash
cd ~/.openclaw/skills
PYTHONPATH=. jp-form-outreach/.venv/bin/python -m _outreach_core.helpers.report draft-quality --since 7d
PYTHONPATH=. jp-form-outreach/.venv/bin/python -m _outreach_core.helpers.report send-funnel --since 7d
PYTHONPATH=. jp-form-outreach/.venv/bin/python -m _outreach_core.helpers.report inspect --target-id <id>
PYTHONPATH=. jp-form-outreach/.venv/bin/python -m _outreach_core.helpers.report improvements --since 7d
PYTHONPATH=. jp-form-outreach/.venv/bin/python -m _outreach_core.helpers.report prune --keep 90 --dry-run
PYTHONPATH=. jp-form-outreach/.venv/bin/python -m _outreach_core.helpers.record_enrich_research --from-jsonl jp-form-outreach/data/enriched.jsonl
```

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
| 3 | **Personalize** | Compose | enriched.jsonl + config.yaml | `data/drafts.jsonl` (+ draft.subject/body OR SKIP) | Opus (`model.name`), cached system prompt |
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
| **「<id>進めて」**（reCAPTCHA/確認待ち） | `… run.py resolve --target-id <id> --action proceed` |
| **「丁寧モードで draft」/「refine ありで」** | `… run.py draft --refine`（既定でも refine ON — `draft.refine_default`） |
| **「refine なしで」** | `… run.py draft --no-refine` |
| **コスト抑えて refine** | `… run.py draft --refine-only-if-low-quality` → 品質ゲート後に low だけ `--refine` |
| **"全部止めて"** | `pkill -f "run.py send"` |
| **「品質ポイント教えて」/「draft 品質どう？」** | `python3 -m _outreach_core.helpers.report draft-quality --since 7d` |
| **「送信ファネル見せて」** | `python3 -m _outreach_core.helpers.report send-funnel --since 7d` |
| **「リサーチ品質どう？」/「form_url の質見せて」** | `python3 -m _outreach_core.helpers.report research-quality --since 7d`（誤URL率・補正成功率・打率） |
| **「今月は何件送りましたか？」/「今週・先月・累計の送信サマリ」** | `python3 -m _outreach_core.helpers.report send-summary --period this_month`（必要に応じて `--period this_week/last_month/all` or `--all-periods`）。会社/内容・試行/成功/失敗・失敗理由を要約返信 |
| **「needs_attention まとめて」** | `python3 -m _outreach_core.helpers.report needs-attention` |
| **「<会社>のトレース見たい」** | `python3 -m _outreach_core.helpers.report inspect --target-id <id>` |
| **「verify 緩め」** | `run.py send --ids N --verify-strict false` |
| **「今週の改善ポイント 3 つ」** | `report improvements --since 7d` を要約して Slack 返信 |
| **enrich-research 完了後** | `record_enrich_research --from-jsonl data/enriched.jsonl` |
| **ログ掃除（90日）** | `report prune --keep 90` |
| **特殊フォーム（再 plan）** | `run.py send --ids N --iterative-fill --auto-send` |
| **「自律モードにして」/「全部任せる」** | brief の `autonomy.mode: autonomous` を確認 → `run.py campaign --clean`（初回は事前承認待ちで停止） |
| **「承認」/「OK 進めて」**（事前承認待ち中） | `run.py approve-autonomy --brief <id>` → 再度 `run.py campaign`（全件自動送信） |
| **「自律状態どう？」** | `run.py autonomy-status --brief <id>`（mode/承認状態）。送信結果は `report send-funnel --brief <id>` の **autonomous (v5)** 節（自己採点 送信/ゲート・auto-skip 理由内訳）を要約 |
| **「自律やめて／戻して」** | `run.py approve-autonomy --brief <id> --revoke`（supervised ゲートに戻す） |
| **「保留（ブロッカー）どれ？」** | `run.py resolve-queue --brief <id> --status`（候補ボタン付きで一覧） |
| **「詰まった分を片付けて」/ ブロッカー再試行** | `./job start jp-form-outreach resolve-queue --brief <id>`（別プロセスで深掘り再試行） |
| **「<id> skip」**（ブロッカー通知に対して） | 当該ターゲットを `skip_history` 登録（リゾルバ対象から除外） |

## Autonomous mode（v5 §12） — 人にいちいち確認しない運用

`autonomy.mode: autonomous` の brief では、**最初の1回だけ**人手で品質を固め、以降は
エージェントが skip / スクリーニング / 送信を自分で判断する。確認の往復は無くなる。

1. **品質を最初に固める（唯一の人手チェックポイント）**: `run.py campaign` を回すと
   Pull→Enrich→Draft まで進み、**brief＋対象リスト＋サンプルドラフト**を Slack に1回提示して
   `awaiting_upfront_approval` で停止する（送信はしない）。
2. **承認**: ユーザーが「承認」と返したら `run.py approve-autonomy --brief <id>`。以降この brief は
   解禁され、`run.py campaign` 再実行で**全件を確認なしで自動送信**する。
3. **送信中の判断はすべて自動**（人を待たない）:
   - 各ドラフトを**自己採点**（`draft_self_score.threshold`、既定 0.75）。未満は自動スキップ＋`skip_history` 記録。
   - **reCAPTCHA は warmup で出さない**方針（§11-A-8 v3 passthrough_with_warmup）。それでも v2 が可視化された
     ターゲットは**自動スキップ＋記録**（CAPTCHA は突破しない）。
   - 想定外フォーム / 送信ボタン不明 / 確認画面 submit 不明 も**自動スキップ＋記録**して次へ。
   - **本文の文字／URL拒否**（「使用できない文字」「URLは記載できません」等）を送信後ページで検知したら、
     **本文からURLを除去して自動再送**（§3.6 URLは送らないルート）。拒否したドメインは `url_unfriendly`
     として学習し、次回以降は最初からURL除去で送る。`send.content_rejected` / `send.url_fallback_ok` を emit。
4. **停止の自己復旧**: gateway の死活・hung・チャンネル切断は watchdog が自動再起動（§15）。run の
   stall（draft 長時間無出力）や異常終了は run_supervisor が**冪等に自動再起動**（上限付き、§15-B）。
   keepalive が長い Opus 呼び出し中も stdout を出すので false stall kill を防ぐ。
5. **承認後にリストや brief を大きく変えたら** `--revoke` で再度 事前承認に戻すこと。
6. **結果の可視化**: `report send-funnel --brief <id>` の **autonomous (v5)** 節で、自己採点の
   送信/ゲート件数・平均スコア・auto-skip 理由内訳（captcha/wrong-form/submit 不明 等）を確認できる。
   「自律で何件送って何件スキップした？」には必ずこれを引いて Slack 返信する。

> supervised（既定）の brief は従来どおり 1 件ずつ Slack 承認・reCAPTCHA は「<id>進めて」で resolve。

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

## List build flow (agent-led, Opus 4.7 前提)

（linkedin-outreach/SKILL.md と同手順。`append_targets --skill jp_form` / `linkedin` を使い分け）

**禁止:** 実在しない企業の捏造。PR TIMES / IR / 公式サイトで検証してから採用。
**必須:** `form_url` は「B2B 営業/取引/提携/取材向け問い合わせフォーム（自由記述 textarea あり）」のみ。

### form_url 採用基準（v8）

- 採用してよい:
  - `お問い合わせ` / `法人のお問い合わせ` / `取材・提携` 等の B2B 連絡窓口
  - 本文 textarea（お問い合わせ内容）がある
- form_url に使わない（該当しか無い場合は `category` を立てる）:
  - 採用/recruit/career/entry
  - IR/投資家
  - B2Cサポート（お客様相談室、修理、返品等）
  - 予約フォーム
  - 資料請求・DLゲート、会員登録、ログイン
- URL ヒューリスティック:
  - 望ましい: `/contact`, `/inquiry`, `/toiawase`, `/otoiawase`, `/company/contact`, `/business/contact`, `/form`
  - 避ける: `/recruit`, `/career`, `/entry`, `/ir`, `/support`, `/faq`, `/reserve`, `/yoyaku`

### 実ページ検証（必須）

targets に書く前に必ず form_url を開き、次を確認:

1. 自由記述 textarea がある
2. 採用/IR/B2C/予約ページではない
3. 会社名・窓口文脈が一致している

確認できたら `form_url_verified: true` を付ける。確認できなければ `form_url` は空にして `category` で理由を残す。

> Sonnet エージェントでは候補品質が落ちるため、リスト生成は Opus 4.7 前提。

## Send verification & escalation

`run.py send --ids N --auto-send` 後に **決定論的** verify（LLM 不使用）。想定外フィールドは `needs_attention` へ。

```bash
.venv/bin/python run.py history needs-attention
.venv/bin/python run.py resolve --target-id <id> --field 業界=その他 --field 紹介者=なし
```

Webhook 通知（`briefs/<id>.yaml` の `slack.incoming_webhook_url`）:
- ✅ 送信完了
- ⚠️ 完了画面未確認 / 想定外フィールド

## Heartbeat behavior

```bash
.venv/bin/python run.py send --ids all --auto-send --heartbeat slack
```

`--heartbeat slack` 未指定時は従来どおり（5 分毎投稿なし）。

## needs_attention の取り扱い

1. **Opus エージェント**が Slack でユーザーに不足フィールドの値を聞く
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
- Python `oc_infer`: `claude-cli/claude-sonnet-4-6` via `config.yaml` `model.name`（Opus にしない）
- State files in `data/*.jsonl` are append-only and resumable
- Rate limiting: enrich and send sleep between page loads / sends
- Forms hosted in iframes (kintone / BowNow / Microsoft Forms) flagged
  as `category: iframe` and skipped — handle manually for now
