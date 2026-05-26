# OpenClaw エージェント向け — Doorman 進捗通知

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
