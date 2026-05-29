# OpenClaw エージェント向け — Doorman 進捗通知と応答保証

## 0. 最重要ルール: Slack ターンを絶対にブロックしない（§15 信頼性）

長時間パイプライン（campaign / send / enrich / draft / research）を
**前景で実行してはならない**。前景実行はエージェントのターンを数分〜十数分
占有し、その間 Slack に応答できず「反応しない」状態を生む（最大の障害原因）。

**必ず `./job start` で detached 起動する:**

```bash
cd ~/.openclaw/skills
./job start jp-form-outreach campaign --brief torana-line-crm --limit 5 \
  --slack-channel-id "$DOORMAN_SLACK_CHANNEL_ID" \
  --slack-thread-ts "$DOORMAN_SLACK_THREAD_TS"
# → 即座に run_id / pid を返す。ターンはすぐ終えてよい。
```

`./job start` が保証すること（エージェントが無言でも届く）:
1. 🚀 **開始**通知（起動直後に Python が同期投稿）
2. … 心拍（run.py の HeartbeatSession が約5分毎に投稿）
3. ✅/❌ **終了**通知（成功・失敗・例外いずれでも supervisor が投稿）

### 受信即 ack（必須）

ユーザーの指示を受けたら **5 秒以内に一言** 返す（「了解、torana-line-crm で
campaign を起動します」）。受信記録を残すなら:

```bash
cd ~/.openclaw/skills && ./healthcheck touch-command
```

### 「進捗どう？」「生きてる？」への即答（file ベース・ターンを占有しない）

```bash
cd ~/.openclaw/skills
./healthcheck ping        # heartbeat 経過秒 / active runs / needs_attention 件数
./healthcheck status      # 上記 + system_health JSON + events 末尾
./brief status --brief torana-line-crm   # 進行中 run の stage / 件数を file から再構築
```

---

## 1. 旧: 進捗通知（detached 起動なら自動で担保）

長時間の Doorman タスクでは **約5分ごとに Slack へ進捗を投稿する**（ユーザー不安の解消）。これは Python パイプラインと **エージェント自身のフォロー** の両方で担保する。

## 1. Python 側（自動）

| コマンド | 進捗 |
|----------|------|
| `linkedin-outreach/research.py` | `heartbeat_watch.py` をバックグラウンド起動 + 各 stage の `--heartbeat auto` |
| `run.py enrich\|draft\|send\|campaign` | 既定 `--heartbeat auto`（`sender_brief.yaml` の `heartbeat.enabled_for: [all]`） |
| `heartbeat_watch.py` | `current_task.jsonl` を監視して約5分毎に Slack 投稿 |

Slack 投稿先: `sender_brief.yaml` の webhook、または `~/.openclaw/openclaw.json` の `channels.slack.botToken` + 直近 Slack セッションのチャンネル。

## 2. エージェント側（必須フォロー）

パイプラインを **バックグラウンドや複数コマンド** で回すとき:

1. 開始前: `nohup .venv/bin/python heartbeat_watch.py &`（スキルディレクトリ内）
2. **5分ごと**（またはユーザーが「進捗？」）:  
   `python heartbeat_watch.py --once` と `python pipeline_status.py` → **要約を Slack に投稿**
3. 終了時: サマリ + 次アクション（送信は別途 `send`）

**禁止**: 「進捗を共有します」とだけ言って、heartbeat / watcher を起動しないこと。

## 3. 確認コマンド

```bash
cd ~/.openclaw/skills/linkedin-outreach
.venv/bin/python pipeline_status.py
.venv/bin/python heartbeat_watch.py --once
tail -5 data/current_task.jsonl
```

詳細は `linkedin-outreach/SKILL.md` の「OpenClaw エージェント: 進捗通知（必須）」を参照。
