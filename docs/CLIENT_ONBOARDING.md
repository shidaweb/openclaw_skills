# Doorman 導入ガイド — クライアントが必要なもの一式

このドキュメントは、クライアント企業が Doorman を**自社環境で稼働させる**ために必要な
「環境・アカウント・準備物・記入情報・手順」を1枚にまとめたものです。
記入式のヒアリングシートは [`INTAKE_SHEET.md`](./INTAKE_SHEET.md)、ローカル/Git の分割は
[`../DISTRIBUTION.md`](../DISTRIBUTION.md) を参照してください。

---

## 1. 必要なもの チェックリスト

### A. 環境（端末・ソフトウェア）

| # | 必要なもの | 補足 | 用意 |
|---|---|---|---|
| 1 | **macOS の端末**（常時起動推奨） | 自動再起動・常駐は launchd 前提。送信中はスリープさせない | ☐ |
| 2 | **OpenClaw ゲートウェイ + CLI**（`openclaw` コマンド） | Doorman の頭脳。インストール済み・起動済みであること | ☐ |
| 3 | **Chrome / Chromium**（OpenClaw 管理ブラウザ） | フォーム自動操作に使用。実ブラウザ・永続プロファイル | ☐ |
| 4 | **Python 3**（3.10+）+ `venv` | パイプライン実行環境 | ☐ |
| 5 | **Playwright（Chromium）** | ブラウザ自動化（`playwright install chromium`） | ☐ |
| 6 | **Git** | リポジトリ取得 | ☐ |

### B. アカウント・権限

| # | 必要なもの | 用途 | 用意 |
|---|---|---|---|
| 7 | **Slack ワークスペース + 専用チャンネル** | Doorman の操作UI（指示・進捗・承認） | ☐ |
| 8 | **`@openclaw/slack` プラグイン有効化** | Slack ↔ OpenClaw 連携 | ☐ |
| 9 | **Anthropic Claude 利用枠**（OpenClaw のモデルルーティング経由） | 文面生成・フォーム解析に使用 | ☐ |
| 10 | **Google アカウント**（営業用に1つ） | ブラウザにログインし reCAPTCHA 評価スコアを健全化。実在・正常なアカウントを固定使用 | ☐ |
| 11 | **日程調整URL**（Tenbin / Calendly 等） | 提案文末尾のCTA | ☐ |
| 12 | **（任意）Slack Incoming Webhook** | heartbeat / 通知の片方向投稿 | ☐ |
| 13 | **（LinkedInチャネル利用時）LinkedIn アカウント + Sales Navigator** | InMail 送信 | ☐ |

### C. 記入が必要な情報（→ [`INTAKE_SHEET.md`](./INTAKE_SHEET.md) で回収）

| # | 情報 | 反映先ファイル |
|---|---|---|
| 14 | **発信者情報**（会社名・氏名・役職・連絡先・住所・設立等） | `config.yaml` / `sender_brief.yaml` |
| 15 | **商材・提案内容**（一言要約・課題・解決策・実績・CTA） | `config.yaml` `pitch` |
| 16 | **ターゲット条件**（業種・規模・意思決定者・必須/除外シグナル） | `config.yaml` `target_persona` / `sender_brief.yaml` `target` |
| 17 | **（任意）自社作り込みプロンプト** | `prompts/system_persona.local.md` / `examples.local.md` |

---

## 2. クライアントが「用意・記入」する情報の中身

最低限、以下を埋めれば運用開始できます（詳細フォームは `INTAKE_SHEET.md`）。

- **発信者**: 会社名 / 氏名（＋カナ・ふりがな）/ 役職 / メール / 電話 / 郵便番号 / 住所 / 設立年月 /
  年商レンジ / 従業員数レンジ
- **商材（pitch）**: 一言要約 / 想定課題 / 解決策 / 実績・事例（proof_points）/ CTA（日程調整URL含む）
- **ターゲット条件**: 業種 / 売上・規模帯 / 設立年の目安 / 意思決定者の役割 /
  必須シグナル（直近IR・資金調達・新サービス等）/ 除外シグナル（B2C窓口のみ等）
- **運用**: 使用する Slack チャンネル / 営業用 Google アカウント / 日程調整URL

> これらは**すべてクライアント端末内のローカルファイル**に保存され、Git・外部には出ません
> （[`../DISTRIBUTION.md`](../DISTRIBUTION.md)）。

---

## 3. セットアップ手順（概要）

```bash
# 1) 取得
git clone <配布リポジトリ> ~/.openclaw/skills && cd ~/.openclaw/skills

# 2) Python 環境（スキルごと。例: jp-form-outreach）
cd jp-form-outreach
python3 -m venv .venv
.venv/bin/pip install pyyaml
.venv/bin/python -m playwright install chromium
cd ..

# 3) 設定ファイルをテンプレからコピーして記入（INTAKE_SHEET の内容を転記）
cp jp-form-outreach/config.example.yaml jp-form-outreach/config.yaml      # 記入
cp sender_brief.example.yaml           sender_brief.yaml                  # 記入
cp briefs/_template.yaml               briefs/<your-id>.yaml             # 記入
echo "<your-id>" > briefs/_active.txt

# 4) （任意）自社の作り込みプロンプトを使う場合
cp jp-form-outreach/prompts/system_persona.md jp-form-outreach/prompts/system_persona.local.md  # 編集
cp jp-form-outreach/prompts/examples.md       jp-form-outreach/prompts/examples.local.md        # 編集

# 5) Slack チャンネルと brief をバインド
./brief bind --brief <your-id> --channel <slack_channel_id>

# 6) 営業用 Google アカウントへログイン（reCAPTCHA スコア健全化）
openclaw browser --browser-profile openclaw stop
openclaw browser --browser-profile openclaw start            # 可視ウィンドウ
openclaw browser --browser-profile openclaw open "https://accounts.google.com/"
#   → 開いたウィンドウで営業用Googleアカウントにサインイン（パスワード/2FAはご本人が入力）

# 7) （推奨）自動再起動 watchdog を有効化
bash scripts/install-watchdog.sh
```

### 動作確認（本番前のスモークテスト）

1. Slack で「2〜3社だけリストして、送らずにドラフトまで」と指示（`campaign --skip-send` 相当）。
2. 生成された文面の品質・自社情報の反映を確認。
3. 1社だけ実送信して、送信完了・記録（`sent_history`）を確認。
4. 問題なければ本番運用へ。

---

## 4. 日常運用（Slack だけで完結）

| やりたいこと | Slack での指示例 |
|---|---|
| キャンペーン実行 | 「EdTechで売上50億〜の企業を10社、フォームに送って」 |
| 送らずに下書きまで | 「…10社、送らずにドラフトまで作って」 |
| 進捗確認 | 「進捗どう？」「生きてる？」 |
| 自律モードの承認 | 「承認」（事前承認待ちのとき） |
| 個別スキップ | 「<id> skip」 |
| 詰まった分の再試行 | 「詰まった分を片付けて」 |

- **自律モード**：最初に1回だけ「リスト＋サンプル文面」を承認すれば、以降は確認なしで自動送信。
- **supervised モード（既定）**：1件ずつ Slack で確認しながら送信。

---

## 5. 運用上の前提・注意

- 送信は**クライアント端末・実ブラウザ・実IP**から行われます（正当性を構造で担保）。
- **送信中は端末をスリープ／シャットダウンしない**でください（自動再起動はしますが稼働は端末依存）。
- reCAPTCHA は**突破しません**。出させない運用＋出たら迂回（規約順守）。一部の厳格なフォームは
  自動送信対象外として迂回・記録します（カバレッジは100%ではありません）。
- 送信先リスト・送信履歴・文面は**端末内**に保存され、外部に送信されません。

---

## 6. 困ったときに見る場所

- **進捗・稼働**: Slack で「ping」/「進捗どう？」。端末で `./healthcheck status`。
- **詰まった案件**: `cd jp-form-outreach && python run.py resolve-queue --brief <id> --status`（候補ボタン付きで一覧）。
- **Slack 無反応時**: `openclaw channels status` / `openclaw plugins enable slack` / `openclaw gateway restart`。
- **詳細**: [`../README.md`](../README.md) / [`OPENCLAW_AGENT.md`](./OPENCLAW_AGENT.md)。

---

## 7. クライアント準備物 早見表（再掲）

```
[環境]   macOS端末 / OpenClaw(CLI) / Chrome / Python3+venv / Playwright / Git
[権限]   Slackワークスペース+チャンネル / @openclaw/slack / Claude利用枠 / 営業用Googleアカウント
[情報]   発信者情報 / 商材(pitch) / ターゲット条件 / 日程調整URL / (任意)自社プロンプト
[任意]   Slack Webhook / LinkedIn(Sales Navigator) / watchdog自動再起動
```
