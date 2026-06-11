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
./report progress --brief torana-line-crm   # ★推奨: いまの送信進捗を1行で
                                            #   例: send 12/30 · 送信9 · スキップ2 · 要対応1 · 経過6m · 残り目安4m · 処理中: 株式会社X
./healthcheck ping        # heartbeat 経過秒 / active runs / needs_attention 件数
./healthcheck status      # 上記 + system_health JSON + events 末尾
./brief status --brief torana-line-crm   # 進行中 run の stage / 件数を file から再構築
```

**「進捗」「どこまで」「あと何件」と聞かれたら最優先で `./report progress --brief <id>` を返す**
（v22・run_progress.json を読むだけ・ターンを占有しない）。`--json` で構造化、
ブラウザで見たいなら `./report dashboard --brief <id>` が自動更新HTMLのパスを返す。
送信実行中は **5分毎の心拍にもこの進捗サマリ（送信/スキップ/要対応/残り目安）が
自動で載る**ので、エージェントからの追加投稿は不要。

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

---

## 4. 自律モード（autonomous）— 人にいちいち確認しない（v5 §12）

brief が `autonomy.mode: autonomous` のとき、エージェントの振る舞いは変わる。

**やらないこと**: ドラフトを 1 件ずつ「送っていい？」と聞かない。reCAPTCHA / 想定外フォーム /
submit 不明で人を待たない。skip 判断・スクリーニングを人に委ねない。

**やること**:
1. 受信即 ack（5 秒以内・従来どおり）。
2. `./job start jp-form-outreach campaign --brief <id>` を detached 起動。
3. **初回だけ**: campaign は brief＋リスト＋サンプルドラフトを 1 回提示して承認待ちで停止する。
   ユーザーが「承認」と言ったら **そのターンで** `run.py approve-autonomy --brief <id>` を実行し、
   再度 campaign を起動 → 全件自動送信。
4. 以降は確認の往復なし。送信中の判断（自己採点で skip、ブロッカーで auto-skip）は Python が自動で行い、
   結果のみ Slack に通知（heartbeat＋✅/⏭/❌ の終了サマリ）。
5. 「自律状態どう？」には `run.py autonomy-status --brief <id>` で即答。

**自己復旧**: run が落ちても campaign は冪等（送信済みを除外して再実行で再開）。gateway 自体の
死活・hung・切断は §15 watchdog が自動再起動。エージェントは「止まってる？」と聞かれたら
`./healthcheck ping` で確認し、必要なら campaign を再起動してよい（承認は維持されるので再承認は不要）。

> brief が supervised（既定）なら従来どおり 1 件ずつ Slack 承認。混在運用は brief 単位で切り替わる。

---

## 5. ブロッカーの自動処理 — タスクを止めない（v6 §16）

送信ボタンが自動特定できない / 想定外フォーム等のブロッカーは、**本体バッチを止めない**。
- 該当ターゲットは診断付きで **リゾルバキュー**（`data/briefs/<id>/resolve_queue.jsonl`）に積まれ、
  本体は次のターゲットへ進む。Slack には**正直で実行可能なメッセージ**が出る（候補ボタン一覧つき。
  もう「進めて」とは言わない＝同じ検出が再失敗するだけだから）。
- **autonomous**: campaign の最後に `stage_resolve_queue` が自動で深掘り再試行（document 全体の
  ボタン列挙＋LLM pick）。人手不要。
- **supervised / 明示的に回したい**: 別プロセス（サブエージェント）として起動できる:

```bash
cd ~/.openclaw/skills
./job start jp-form-outreach resolve-queue --brief <id>   # 別プロセスで深掘り再試行
# 状況だけ: (cd jp-form-outreach && python run.py resolve-queue --brief <id> --status)
```

エージェントは「ブロッカーが出た」通知を受けても**手を止めず**、本体 run の完了後（または並行制約が
無ければ）`resolve-queue` をサブエージェントとして spawn し、結果（自動送信 N / スキップ N）だけ受け取る。
ユーザーには「候補ボタンを見せて skip 可否だけ確認」すればよく、「進めて」の往復は不要。

> ブラウザは `openclaw` プロファイル共有のため、resolve-queue は本体 run と**同時に同一ブラウザを
> 使わない**こと（本体完了後に回す / autonomous は campaign 末尾で自動実行）。

### タブ分離（v6 §17）

`browser.tab_isolation: true`（既定）で、各ターゲットを**専用タブ**で処理する:
- 送信**成功 → タブを閉じる**（ブラウザを散らかさない）。
- **エラー（送信ボタン未検出/誤フォーム）→ タブを開いたまま保持**し、全ページ**スクリーンショット**を
  証拠保存。本体は新規タブで次へ（止まらない）。
- リゾルバは保持タブを **`focus` してその場で解決**（再ナビゲートせず、失敗時の正確なDOMで送信ボタン探索）。
  送信前に **same-site チェック**（registrable domain 一致）で**別会社タブへの誤送信を防止**。
- タブ総数は上限管理（保持中のエラータブは保護、古い順に閉じる）。
- 不具合時は `browser.tab_isolation: false` で即、従来の単純フローに戻せる。
