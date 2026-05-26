# 定期実行レシピ（OpenClaw Opus 4.7 エージェント前提）

Doorman の定期実行は **OpenClaw エージェント（Opus 4.7）が Slack 文を読んでコマンドを組み立てる** 前提です。Python 側にスケジューラは入れません。

## 毎週月曜 9:00 — リスト化して preview まで

```bash
openclaw cron add --schedule "0 9 * * 1" \
  "doorman: sender_brief.yaml に基づき EdTech 中堅 10 社をリスト化し、
   python3 -m _outreach_core.helpers.dump_exclude_set で除外 ID を確認し、
   候補を Slack で提示したあと append_targets で targets に追加し、
   jp-form-outreach と linkedin-outreach で enrich → draft → preview を実行。送信は確認後"
```

## 毎日 18:00 — 未送信ドラフトのサマリ

```bash
openclaw cron add --schedule "0 18 * * *" \
  "doorman: 全スキルの data/drafts.jsonl から未送信（sent_history に無い）ドラフトを数え、
   Slack にサマリを返す。needs_attention があれば run.py history needs-attention も要約"
```

## 毎時 — needs_attention 再通知

```bash
openclaw cron add --schedule "0 * * * *" \
  "doorman: 全スキルの needs_attention.jsonl に status=open があれば Slack に要約して投げ直す"
```

## 長時間 send 時のハートビート

```bash
cd ~/.openclaw/skills/jp-form-outreach
.venv/bin/python run.py send --ids all --auto-send --heartbeat slack
```

`sender_brief.yaml` の `slack.incoming_webhook_url` が設定されているときのみ Webhook に投稿します（LLM 不使用、テンプレート文のみ）。
