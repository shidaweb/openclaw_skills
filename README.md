# openclaw_skills — Doorman

Slack 指示で動くアウトリーチ運用基盤です。  
OpenClaw エージェントが対話を受け、Python パイプラインが実処理を実行します。

このリポジトリは、共通コア `'_outreach_core/'` を軸に、複数スキルの運用を同じ契約で回します。

## 現在のスキル

| Skill | 実行面 | 主な成熟度 |
|---|---|---|
| [`jp-form-outreach`](./jp-form-outreach) | 日本企業の問い合わせフォーム送信 | v13 相当（送信レジリエンス + 2タッチLLM + valid-form保持） |
| [`linkedin-outreach`](./linkedin-outreach) | LinkedIn InMail | v4 系 |

> 詳細仕様は各スキルの `SKILL.md` が正典です。

## 主要機能

- **6フェーズ運用**: Pull → Enrich → Personalize → Approve → Send → Log
- **マルチ brief**: チャンネルごとに発信人格/設定を切替
- **detached 実行**: 長時間ジョブを別プロセス化し、Slack 応答遅延を回避
- **自律運用モード**: 初回承認後は自己採点・自動スキップ方針で連続処理
- **送信レジリエンス（jp-form）**:
  - captcha ライブ判定 + 回避学習
  - confirm フローの phase-aware submit 選択
  - native submit（`requestSubmit` 優先）
  - inquiry type（select/radio/checkbox）補正
  - pre/post 2タッチポイント LLM ループ
  - valid form を捨てない enrich（404 検知 + best-known 復帰）

## 工夫点（設計上の意図）

- **決定論と LLM の分離**
  - `verify`、`progress`、`history`、`report` は原則 LLM なし
  - LLM は `draft` と `form analyzer` に集中し、失敗時は純関数で補正
- **append-only 履歴**
  - `sent_history.jsonl` / `skip_history.jsonl` / `events.jsonl` は追記中心
  - 再実行時の冪等・再開容易性を確保
- **安全優先 submit**
  - 生 `form.submit()` を避け、`requestSubmit` ベース
  - 検索/ログインフォームの誤送信を抑制
- **障害時も止めない**
  - ブロッカーは `needs_attention` と resolver queue へ分離
  - メインバッチは継続

## プロセス概要

### 1) Run 起動

- `./job start ...` が `_outreach_core/helpers/run_job.py` 経由で detached 起動
- 起動/心拍/終了を Slack へ通知し、対話ターンをブロックしない

### 2) Enrich（フォーム探索）

- seed URL を開いて `'_FORM_FIELDS_JS'` で構造抽出
- `'_outreach_core/contact_url.py'` で `classify_form_type`
- 非contact時は同一ドメイン候補を探索
- v13 で次を強化:
  - `is_error_page` で 404/エラーページを即除外
  - valid form を保持し、失敗候補から復帰（best-known）
  - textarea + submit の有効フォームを過剰に捨てない

### 3) Draft / Approve

- sender/brief 情報を元に草案生成
- 承認運用か自律運用かで分岐

### 4) Send（実送信）

- LLM plan + 純関数補正で入力
- confirm/single フロー判定
- submit ボタン探索:
  - pattern
  - LLM picker
  - native submit fallback（`requestSubmit`）
- verify で送信成否を判定

### 5) Log / Report

- 送信結果は履歴とイベントに集約
- `./report` で send-funnel / needs-attention / 品質系を可視化

## 監視・自己復旧

4レイヤーで運用可視性と継続性を担保します。

| Layer | 目的 | 実装 |
|---|---|---|
| Auto-ack + detached | Slack 返信遅延防止 | `./job`, `_outreach_core/helpers/run_job.py` |
| Healthcheck | 生存・進捗の即答 | `./healthcheck`, `_outreach_core/helpers/healthcheck.py` |
| Gateway watchdog | gateway hung/切断回復 | `_outreach_core/helpers/watchdog.py`, `scripts/install-watchdog.sh` |
| Run supervisor | stall/異常終了の再起動 | `_outreach_core/run_supervisor.py`, `_outreach_core/progress.py` |

主要環境変数:

- `DOORMAN_KEEPALIVE_SEC`
- `DOORMAN_RUN_STALL_SEC`
- `DOORMAN_RUN_MAX_RESTARTS`

## ログ・状態ファイル

### 運用状態

- `data/channel_state/<channel>.json`: Slack channel ↔ brief バインド
- `data/system_health/<host>.json`: heartbeat / health 状態
- `<skill>/data/briefs/<id>/current_task.jsonl`: 現在進行タスク
- `<skill>/data/briefs/<id>/active_run.lock`: 実行ロック

### パイプライン成果物（brief 単位）

- `leads.jsonl`
- `enriched.jsonl`
- `drafts.jsonl`
- `sent_history.jsonl`
- `skip_history.jsonl`
- `needs_attention.jsonl`

### 可観測性

- `events.jsonl`: 構造化イベント（stage/kind/payload）
- `.traces/<timestamp>/...`: snapshot, fill_plan, verify_evidence, diagnostics

## ランチャー

```bash
cd ~/.openclaw/skills

./brief        list | show | status | bind | unbind | new | write-from-json | stop-run ...
./job          start <skill> <stage> --brief <id> ...
./healthcheck  ping | status | write-heartbeat | touch-command
./report       improvements | draft-quality | send-funnel | needs-attention | prune
```

## セットアップ

```bash
git clone https://github.com/shidaweb/openclaw_skills.git ~/.openclaw/skills

cd ~/.openclaw/skills/jp-form-outreach
python3 -m venv .venv
.venv/bin/pip install pyyaml
.venv/bin/python -m playwright install chromium

cd ~/.openclaw/skills
cp briefs/torana-line-crm.example.yaml briefs/<your-id>.yaml
./brief bind --brief <your-id> --channel <slack_channel_id>
```

watchdog を有効化する場合:

```bash
bash scripts/install-watchdog.sh
```

## モデル方針

| 用途 | モデル | 設定 |
|---|---|---|
| Slack 対話エージェント | Opus（gateway 側） | OpenClaw 側 |
| draft / refine | Opus 系 | `model.name` |
| form analyzer | Sonnet 系（必要時エスカレーション） | `model.form_analyzer_name`, `model.form_analyzer_escalation_name` |
| verify / progress / report | LLM なし | Python 純関数 |

## どこを見るべきか（開発者向け）

- `jp-form-outreach/run.py`: enrich/draft/send の主制御
- `_outreach_core/contact_url.py`: form URL候補・フォーム分類・error判定
- `_outreach_core/submit_progress.py`: submit gate 純関数（radio/select/checkbox）
- `_outreach_core/verify.py`: 成功判定（LLM なし）
- `_outreach_core/helpers/report.py`: 実績集計
- `_outreach_core/tests/`: 回帰テスト群（仕様固定の要）

## ドキュメント

- [`docs/OPENCLAW_AGENT.md`](./docs/OPENCLAW_AGENT.md)
- [`docs/ARCHITECTURE_EXTERNAL.md`](./docs/ARCHITECTURE_EXTERNAL.md)
- [`_outreach_core/README.md`](./_outreach_core/README.md)
- [`briefs/README.md`](./briefs/README.md)
- [`CURSOR_INSTRUCTIONS.md`](./CURSOR_INSTRUCTIONS.md)

## Git 管理対象外

`.gitignore` により、以下はローカル専用です。

- 実運用 config / API keys / brief 実体
- channel 状態ファイル
- health/watchdog/job logs
- events/traces/current_task など運用ログ
