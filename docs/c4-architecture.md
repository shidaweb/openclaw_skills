# Doorman — C4アーキテクチャ図（L1 + L2）

ソースコードから抽出した [C4モデル](https://c4model.com/) のレベル1（System Context）とレベル2（Container）。
対象リポジトリ: `openclaw_skills`（共通コア `_outreach_core` + チャンネル別スキル）。

> 抽出元: `README.md`, `_outreach_core/`（infer / notify / adapters / paths ほか）, `jp-form-outreach/run.py`, CLI ランチャ（`brief` / `job` / `report` / `healthcheck`）。

---

## L1 — System Context

「Doorman が誰のために、どの外部システムとやり取りするか」の俯瞰図。内部構造は次の L2 で展開する。

```mermaid
C4Context
    title System Context — Doorman アウトリーチ運用基盤

    Person(operator, "運用者", "Slackで指示を出し、送信を承認する（Nori / チーム）")

    System(doorman, "Doorman", "Slack指示で動くアウトリーチ運用基盤。フォーム/InMailの下書き〜送信〜記録を6フェーズで自動化")

    System_Ext(slack, "Slack", "指示の受け口（OpenClawエージェント経由）と片方向の通知先")
    System_Ext(openclaw, "OpenClaw + Claude", "会話エージェント(Opus)とサブタスク推論(Sonnet)。下書き・フォーム解析を担当")
    System_Ext(jpforms, "日本企業の問い合わせフォーム", "送信先のWebサイト（jp-form-outreach）")
    System_Ext(linkedin, "LinkedIn", "InMail 送信先（linkedin-outreach）")

    Rel(operator, slack, "指示・承認", "チャット")
    Rel(slack, doorman, "コマンド起動", "OpenClawエージェント")
    Rel(doorman, slack, "進捗・結果を通知", "Webhook / chat.postMessage")
    Rel(doorman, openclaw, "下書き生成・フォーム解析", "openclaw infer (subprocess)")
    Rel(doorman, jpforms, "問い合わせフォーム送信", "ブラウザ自動操作")
    Rel(doorman, linkedin, "InMail 送信", "ブラウザ自動操作")

    UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="2")
```

**要点**

- 入力経路は Slack のみ（運用者 → OpenClawエージェント → Doorman）。Doorman 側からも Slack へ片方向通知を返す（`notify.py` / `openclaw_slack.py`）。
- LLM は2系統。会話エージェントは Opus、Python パイプライン内のサブタスク（下書き・フォーム解析）は Sonnet 固定（`infer.py` の `DEFAULT_MODEL`）。
- 送信先は2系統。日本企業の問い合わせフォームと LinkedIn InMail。どちらもブラウザ自動操作で到達する。

---

## L2 — Container

Doorman を「実行単位（コンテナ）」に分解する。`_outreach_core` を軸に、薄い CLI ランチャ、チャンネル別パイプライン、ブラウザ抽象層、append-only な状態ストアで構成される。

```mermaid
C4Container
    title Container — Doorman

    Person(operator, "運用者", "Slackで指示・承認")
    System_Ext(slack, "Slack", "指示の受け口 / 通知先")
    System_Ext(openclaw_llm, "OpenClaw + Claude", "Opus(エージェント) / Sonnet(サブタスク)")
    System_Ext(websites, "送信先サイト", "日本企業フォーム / LinkedIn")

    System_Boundary(doorman, "Doorman") {
        Container(agent, "OpenClawエージェント", "OpenClaw + Opus", "Slack会話を解釈し、CLIランチャを起動するフロント層")
        Container(cli, "CLIランチャ", "Python (brief/job/report/healthcheck)", "PYTHONPATHを通して _outreach_core のモジュールを起動する薄いラッパ")
        Container(jobrunner, "detached ジョブランナー", "Python (run_job)", "長時間ジョブを別プロセス化しSlack応答遅延を回避。supervisor/watchdogで監視")
        Container(core, "共通コア _outreach_core", "Python ライブラリ", "6フェーズ運用の本体: draft / contact_url / form_validation / verify / progress / history / send_state など")
        Container(jp, "jp-form-outreach パイプライン", "Python (run.py, research.py)", "日本企業フォーム送信。bootstrap→enrich→draft→preview→send→mark-sent")
        Container(li, "linkedin-outreach パイプライン", "Python (run.py, build_search.py)", "LinkedIn InMail 送信パイプライン")
        Container(browser, "ブラウザアダプタ層", "Python (adapters/*)", "OpenClawBrowser / Playwright を同一プロトコルで切替。get_browser()で選択")
        Container(notify, "通知モジュール", "Python (notify, openclaw_slack)", "片方向のSlack通知。Webhook または chat.postMessage")
        ContainerDb(state, "状態ストア", "append-only JSONL + YAML", "sent/skip/events 履歴, channel_state, ターゲットリスト, job_logs, system_health")
    }

    Rel(operator, slack, "指示・承認")
    Rel(slack, agent, "メッセージ", "Slack API")
    Rel(agent, cli, "コマンド起動", "subprocess")
    Rel(cli, jobrunner, "detachedジョブ開始", "job start")
    Rel(jobrunner, jp, "パイプライン実行", "subprocess")
    Rel(jobrunner, li, "パイプライン実行", "subprocess")
    Rel(jp, core, "下書き/検証/履歴を利用", "import")
    Rel(li, core, "下書き/検証/履歴を利用", "import")
    Rel(core, browser, "ページ操作", "evaluate / browser()")
    Rel(browser, websites, "フォーム入力・送信", "自動操作")
    Rel(core, openclaw_llm, "下書き・フォーム解析", "oc_infer (subprocess)")
    Rel(core, state, "読み書き", "JSONL / YAML")
    Rel(core, notify, "結果通知を依頼", "import")
    Rel(notify, slack, "通知", "Webhook / Bot")

    UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
```

**コンテナの責務メモ**

- **OpenClawエージェント** — Slack 会話を解釈する唯一のフロント。Opus 4.7 系（リポジトリ外で設定）。ここから下は決定論的な Python に渡る。
- **CLIランチャ** (`brief` / `job` / `report` / `healthcheck`) — どのディレクトリからでも `PYTHONPATH=.` を通して `_outreach_core.helpers.*` を `runpy` で起動するだけの薄い層。
- **detached ジョブランナー** (`run_job`) — 長時間ジョブを別プロセス化し、Slack の応答遅延を回避。`run_supervisor` / `watchdog` / `heartbeat_watch` が死活監視する。
- **共通コア `_outreach_core`** — 本体。下書き(`draft`)とフォーム解析だけが LLM を使い、`verify` / `progress` / `history` / `report` は原則 LLM なしの純関数。今回修正した `contact_url`（フォーム種別判定）もここ。
- **チャンネル別パイプライン** — `jp-form-outreach` と `linkedin-outreach`。同じ契約（コアの import）で、実行面（フォーム送信 / InMail）だけ差し替える。
- **ブラウザアダプタ層** (`adapters/`) — `BrowserAdapter` プロトコルの背後で OpenClawBrowser と Playwright を切替。呼び出し側は `get_browser()` だけを見る（具体アダプタを import しない）。
- **状態ストア** — `sent_history.jsonl` / `skip_history.jsonl` / `events.jsonl` を中心に append-only。再実行の冪等性と再開容易性を確保。ターゲットリスト(YAML)・`channel_state`・`job_logs`・`system_health` もここ。

---

## 設計上の不変条件（コードから読み取れる方針）

1. **決定論と LLM の分離** — LLM は `draft` と form analyzer に限定し、失敗時は純関数で補正する。
2. **append-only 履歴** — 状態は追記中心。再開・冪等性を最優先。
3. **安全優先 submit** — 生 `form.submit()` を避け `requestSubmit` ベース。
4. **アダプタ seam** — ブラウザバックエンド（OpenClaw / Playwright）は差し替え可能で、ロールバック先が常に存在する。
