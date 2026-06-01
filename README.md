# openclaw_skills — Doorman

Slack 駆動のアウトバウンド・アウトリーチ運用基盤。エンドユーザーは **Slack だけ**で
操作し、その背後で OpenClaw エージェント（頭脳）と Python パイプライン（手足）が動く。

姉妹スキルは共通の **6 フェーズ・パイプライン**（Pull → Enrich → Personalize →
Approve → Send → Log）を実装し、`_outreach_core/` の共通ロジックを共有する。

## Skills

| Skill | Channel | Status |
|---|---|---|
| [`jp-form-outreach`](./jp-form-outreach) | 日本企業の問い合わせフォーム | v6 |
| [`linkedin-outreach`](./linkedin-outreach) | LinkedIn Sales Navigator InMail | v4 |

各スキルの仕様・使い方は `SKILL.md` を参照。jp-form-outreach は v5（自律運用）・v6（送信
レジリエンス：captcha精緻化 / 回避エンジン / URL拒否フォールバック / リゾルバ / タブ分離 /
run 自己復旧）まで実装済み。次期 v7 仕様は
[`CURSOR_INSTRUCTIONS_v7_submit_resilience.md`](./CURSOR_INSTRUCTIONS_v7_submit_resilience.md)。

## ランチャー（リポジトリ直下から実行）

```bash
cd ~/.openclaw/skills

./brief        list | show | status | bind | unbind | new | write-from-json | stop-run …
./job          start <skill> <stage> --brief <id> …     # 長時間タスクを detached 起動
./healthcheck  ping | status | write-heartbeat | touch-command
./report       improvements | draft-quality | send-funnel | needs-attention | prune
```

- **`./brief`** — マルチ brief（人格）管理。Slack チャンネルと brief のバインド、進捗の
  ファイル再構築（`status`）、暴走 run の停止（`stop-run`）など。
- **`./job start`** — `run.py` を**別プロセスで detached 起動**。エージェントの Slack
  ターンをブロックせず、開始 / 心拍 / 終了を Python が直接 Slack へ通知する（§15）。
- **`./healthcheck`** — 「生きてる？」「進捗どう？」に**ファイルから即答**。`ping` /
  `status` は呼ぶ度に再計算するため常に最新。
- **`./report`** — `events.jsonl` / traces から品質・送信ファネル・要対応をレポート。

## マルチ brief（人格）

1 リポジトリで複数の発信者人格（例: `torana-line-crm`）を切り替えて運用できる。

- brief 定義: `briefs/<id>.yaml`（テンプレ `briefs/_template.yaml`、雛形 `*.example.yaml`）
- brief ごとの状態: `<skill>/data/briefs/<id>/`（leads / enriched / drafts /
  sent_history / events など）
- Slack チャンネル → brief のバインド: `data/channel_state/<channel_id>.json`
  （`./brief bind`）。バインド済みチャンネルでは毎スレッドの brief 確認を省略できる。
- **ステートレス前提**: Slack スレッド単位で文脈がリセットされても、`./brief status`
  でファイルから状況を再構築できる（§14-G）。

新規 brief は Slack ネイティブ onboarding（`./brief write-from-json`、雛形
`briefs/onboarding_answers.example.json`）で作成可能。

## パイプライン（6 フェーズ）

```
  ┌────────┐  ┌────────┐  ┌────────────┐  ┌────────┐  ┌──────┐  ┌─────┐
  │ 1.PULL │→ │2.ENRICH│→ │3.PERSONALIZE│→│4.APPROVE│→ │5.SEND│→ │6.LOG│
  └────────┘  └────────┘  └────────────┘  └────────┘  └──────┘  └─────┘
   leads        enriched      drafts        (Slack 承認)  (browser) sent_history
   .jsonl       .jsonl        .jsonl                                .jsonl
```

契約:
- **冪等**: 同じフェーズの再実行で二重処理しない。SKIP は `skip_history.jsonl`、送信済みは
  `sent_history.jsonl` に記録し、Pull / Send 時に除外。
- **再開可能**: 各フェーズは前段の JSONL を読む。
- **追記のみ**の履歴（カラム削除・リネーム禁止）。
- **キャッシュ親和**: Personalize の system prompt はバイト安定（prompt-cache ヒット）。

## 信頼性レイヤー（§15）— Slack 応答と稼働を保証

「Slack で指示しても反応しない / タスクが動いているか不明 / run が止まる」を解消する 4 層。

| Layer | 役割 | 実装 |
|---|---|---|
| 1. Auto-ack + detached 起動 | ターンを即返し、長時間処理は別プロセスへ | `./job start` / `_outreach_core/helpers/run_job.py` |
| 2. Healthcheck | 「生きてる？」に即答、heartbeat 更新 | `./healthcheck` / `_outreach_core/helpers/healthcheck.py` |
| 3. Gateway watchdog | gateway の死活・無応答・チャンネル切断を検知し自動復旧 | `_outreach_core/helpers/watchdog.py` + launchd |
| 4. Run 自己復旧（v6） | run の stall/異常終了を検知し、冪等に自動再開（上限付き） | `_outreach_core/run_supervisor.py` + keepalive |

- **Run 自己復旧（v6 §15-B）**: 過去に draft フェーズが「600 秒 STDOUT 無出力 → stall 判定で強制 kill」
  された問題に対処。①`HeartbeatSession` が**常時 keepalive 行を stdout に出力**（既定 60 秒毎・
  `DOORMAN_KEEPALIVE_SEC`）し、長い Opus 呼び出し中も「無出力」にならない。②`run_job` の supervisor が
  子 run を **poll 監視**し、ログ/heartbeat が一定時間（既定 420 秒・`DOORMAN_RUN_STALL_SEC`）進まなければ
  stall とみなして kill→**冪等に再起動**（送信済みは除外して再開）。③異常終了も同様に自動再開。
  いずれも `MAX_RESTARTS`/ウィンドウで上限管理（再起動ループ防止）。`exit=3`（別 run 進行中）は再起動しない。

- **detached job runner**: `run.py` を別プロセスで起動。Python が 🚀開始 / …心拍（約5分毎、
  `HeartbeatSession`）/ ✅❌終了 を Slack に直接投稿するため、エージェントが無言でも届く。
- **heartbeat**: 実行中は実進捗（tick）ごとに `data/system_health/<host>.json` を更新。
  専用 cron は不要。
- **watchdog**（launchd 60 秒 tick、`scripts/install-watchdog.sh` で導入）:
  監視対象は launchd 管理の `ai.openclaw.gateway`。
  - プロセス**死亡**は gateway 自身の launchd `KeepAlive` が OS レベルで自動復旧。
  - **無応答（hung）** は `openclaw health` で検知し、連続失敗時のみ
    `launchctl kickstart -k` で強制再起動。
  - **チャンネル切断**（gateway 生存中に Slack が `running:false` のまま）を
    `openclaw channels status --json` で検知し、一定時間継続したら gateway を再起動して
    再接続（ネットワーク断後の固着を自動回復）。

詳細はエージェント運用ガイド [`docs/OPENCLAW_AGENT.md`](./docs/OPENCLAW_AGENT.md)。

## 自律運用（autonomous, v5）

brief 単位で「人にいちいち確認しない」運用に切り替えられる（既定は従来の supervised）。

- **品質は最初に1回だけ固める**: `mode: autonomous` の初回 campaign は brief＋リスト＋サンプル
  ドラフトを提示して承認待ちで停止。`./job ... run.py approve-autonomy --brief <id>` で解禁。
- **以降は確認なしで全件自動送信**。各ドラフトは送信前に自己採点（既定 threshold 0.75）され、
  未満は自動スキップ＋記録。
- **ブロッカーは自動スキップ＋記録**: 可視 reCAPTCHA v2 / 想定外フォーム / submit 不明 は人を待たず
  skip して継続（reCAPTCHA は v3 warmup で出さない方針。突破はしない）。
- **停止は自己復旧**: gateway の死活・hung・切断は watchdog が自動再起動、run は冪等で再開。

```bash
./job start jp-form-outreach campaign --brief <id>   # 初回は承認待ちで停止
# Slack/CLI で承認
cd jp-form-outreach && python run.py approve-autonomy --brief <id>
python run.py autonomy-status --brief <id>           # mode / 承認状態を確認
```

設定は `sender_brief.yaml` の `autonomy:` ブロック（brief で上書き可）。詳細は
[`CURSOR_INSTRUCTIONS.md`](./CURSOR_INSTRUCTIONS.md) §12。

## 送信レジリエンス（v6, jp-form-outreach）

実運用ログの障害を潰すために追加された、フォーム送信まわりの堅牢化レイヤー群（すべて
`sender_brief.yaml` で調整可。共通ロジックは純関数モジュール＋ユニットテストに分離）。

| 機能 | 役割 | 実装 |
|---|---|---|
| **reCAPTCHA ライブ判定** | enrich の「要素存在」ではなく送信直前に**実際に可視・ブロッキングか**を再判定し、submit 失敗の captcha 誤ラベルを撲滅 | `_outreach_core/captcha.py` |
| **回避エンジン** | warmup（v3 スコア健全化）＋ドメイン別 captcha 遭遇率学習（`unviable` 自動スキップ）＋適応 warmup ＋人間的ペーシング | `_outreach_core/avoidance.py`, `warmup.py` |
| **URL拒否フォールバック** | 「使用できない文字 / URLは記載できません」検知時に本文から URL を除去して自動再送、ドメインを `url_unfriendly` 学習 | `_outreach_core/content_guard.py` |
| **リゾルバキュー** | submit 未検出 / 誤フォームを**止めずにキュー化**し、別プロセス（`resolve-queue`）で深掘り再試行。Slack には候補ボタン＋スクショ付きの**正直な指示**（「進めて」廃止） | `_outreach_core/resolve_queue.py` |
| **タブ分離** | ターゲットごとに専用タブ。成功→閉じる / エラー→残して証拠保持、リゾルバが `focus` でその場解決（same-site ガードで誤送信防止） | `_outreach_core/tab_utils.py` |
| **run 自己復旧** | stall（無出力）/ 異常終了を検知し冪等に自動再起動。keepalive が長い LLM 呼び出し中も stdout を出す | `_outreach_core/run_supervisor.py`, `progress.py` |

```bash
cd jp-form-outreach
python run.py resolve-queue --brief <id> --status    # 保留ブロッカー一覧（候補ボタン付き）
python run.py resolve-queue --brief <id>             # 深掘り再試行（別プロセスでも可: ./job start … resolve-queue）
```

- **正直なメッセージ**: ブロッカー通知は実原因を表示（例「送信ボタンがDOMで特定できず停止（captchaではない）」）。
  reCAPTCHA は**突破しない**方針（warmup で出させない＋出たら迂回）。
- 関連環境変数: `DOORMAN_KEEPALIVE_SEC`（既定 60）, `DOORMAN_RUN_STALL_SEC`（既定 420）,
  `DOORMAN_RUN_MAX_RESTARTS`（既定 3）。`browser.tab_isolation: false` で従来挙動に即戻せる。
- 外販（オンデバイス）アーキテクチャ構想は [`docs/ARCHITECTURE_EXTERNAL.md`](./docs/ARCHITECTURE_EXTERNAL.md)。

## モデル方針（v4）

| 用途 | モデル | 設定場所 |
|---|---|---|
| Slack で会話する OpenClaw エージェント | **Opus 4.7**（固定） | OpenClaw gateway（本リポジトリ外） |
| `run.py` の `draft` / `_refine_draft` / char_limit 圧縮 | **Opus 4.7** | `model.name` |
| `_llm_analyze_form`（DOM → JSON 構造化） | **Sonnet 4.6** | `model.form_analyzer_name` |
| `verify` / `notify` / `progress` / dedup | **LLM なし**（決定論的 Python） | — |

## Install

前提: OpenClaw gateway（`openclaw` CLI）が稼働し、Slack チャンネルが設定済みであること。

```bash
# 1) Clone
git clone https://github.com/shidaweb/openclaw_skills.git ~/.openclaw/skills

# 2) スキルごとの Python 環境（例: jp-form-outreach）
cd ~/.openclaw/skills/jp-form-outreach
python3 -m venv .venv
.venv/bin/pip install pyyaml
.venv/bin/python -m playwright install chromium   # ブラウザ自動化が必要な場合
.venv/bin/python run.py --help

# 3) brief を用意（雛形からコピーして編集、または onboarding）
cd ~/.openclaw/skills
cp briefs/torana-line-crm.example.yaml briefs/<your-id>.yaml   # 発信者情報を編集
./brief bind --brief <your-id> --channel <slack_channel_id>

# 4) watchdog を有効化（任意・推奨。macOS / launchd）
bash scripts/install-watchdog.sh
```

### OpenClaw 側の注意

- Slack は OpenClaw の**プラグイン** `@openclaw/slack` として動作する。更新後などに
  Slack が無反応な場合は、プラグインが有効か確認する:

```bash
openclaw channels status            # Slack が running/connected か
openclaw plugins enable slack       # 無効なら有効化
openclaw gateway restart            # 反映
```

- gateway 自体は `launchctl list ai.openclaw.gateway` で管理状態を確認できる。

## What's NOT committed

`.gitignore` により以下は管理外（詳細は [`.gitignore`](./.gitignore)）:

- `.venv/`（マシン固有の Python 仮想環境）, `__pycache__/`, `.DS_Store` など
- 記入済み設定: `config.yaml` / `sender_brief.yaml` / `targets.yaml` /
  `briefs/*.yaml`。代わりに `briefs/_template.yaml` と `*.example.yaml` をコミットする
- Slack バインド: `data/channel_state/C*.json`
- 信頼性レイヤーのローカル状態: `data/system_health/`, `data/watchdog.*`, `data/job_logs/`
- ローカルログ系: `**/data/events.jsonl`, `**/data/needs_attention.jsonl`,
  `**/data/current_task.jsonl`, `**/data/traces/`, `data/*.jsonl`

各スキルは `config.example.yaml` / `targets.example.yaml` をテンプレとして同梱する。

## Docs

- [`docs/OPENCLAW_AGENT.md`](./docs/OPENCLAW_AGENT.md) — エージェント運用（ack / detached / 進捗保証 / 自律 / リゾルバ / タブ分離）
- [`docs/ARCHITECTURE_EXTERNAL.md`](./docs/ARCHITECTURE_EXTERNAL.md) — 外販（オンデバイス）アーキテクチャ構想
- [`docs/SCHEDULING.md`](./docs/SCHEDULING.md) — cron スケジューリング
- [`_outreach_core/README.md`](./_outreach_core/README.md) — 共通ロジック
- [`briefs/README.md`](./briefs/README.md) — brief の構造
- [`CURSOR_INSTRUCTIONS.md`](./CURSOR_INSTRUCTIONS.md) — v4 仕様（設計の正典）＋ §12 自律運用（v5）
- [`CURSOR_INSTRUCTIONS_v7_submit_resilience.md`](./CURSOR_INSTRUCTIONS_v7_submit_resilience.md) — v7 仕様（送信ボタン進行性 / ドラフト堅牢化、未実装）

## コアモジュール（`_outreach_core/`）

主要な共通ロジック（v5/v6 追加分を含む）:

- `autonomy.py` — 自律運用プロファイル（自己採点 / 事前承認 / blocker 方針）
- `avoidance.py` — reCAPTCHA 回避エンジン（warmup / ドメイン学習 / ペーシング）
- `captcha.py` — ライブ reCAPTCHA 判定・分類
- `content_guard.py` — 本文の文字/URL 拒否検知＋URL 除去
- `resolve_queue.py` — ブロッカーキュー＋実行可能メッセージ
- `tab_utils.py` — ブラウザタブ管理（純関数）
- `run_supervisor.py` — run の stall 検知＋上限付き自動再起動
- `progress.py` — heartbeat ＋ stdout keepalive
- `verify.py` / `draft.py` / `prompt.py` / `infer.py` / `warmup.py` ほか（既存）
