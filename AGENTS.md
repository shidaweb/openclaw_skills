## Imported Claude Cowork project instructions

# Doorman Slack応答の最優先ルール

Slackで「ping」「生きてる？」「status」「進捗どう？」のような軽量確認を受けたら、長い作業や調査を始める前に必ず即答する。

- まず `cd ~/.openclaw/skills && ./healthcheck ping` を実行し、その1〜2行を日本語で返す。
- 5秒以内の返答を優先する。詳細調査はその後に行う。
- 長時間ジョブは前景実行せず、必ず `./job start ... --slack-channel-id "$DOORMAN_SLACK_CHANNEL_ID" --slack-thread-ts "$DOORMAN_SLACK_THREAD_TS"` で detached 起動する。
- Slackへの進捗通知は既定で約10分ごと。開始時に「約10分ごとに進捗を投稿します」と伝える。
