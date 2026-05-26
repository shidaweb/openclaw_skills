# Cursor 向け指示書 — Doorman (openclaw_skills) ブラッシュアップ v2

このリポジトリ (`~/.openclaw/skills/`) を Cursor で改修してもらうための指示書です。
**OpenClaw（Claude 製のローカルエージェント）が司令塔、Python はリーフツール、Slack は OpenClaw の入出力チャネル** という前提で書いています。

> v1 では `slack_bolt` の自作ワーカーや `list_builder` 独立 CLI を作らせようとしていましたが、それは OpenClaw を回避する設計で誤りでした。v2 ではすべて撤回しています。

---

## 0. 全体像（読み飛ばさず読んでください）

```
[ ユーザー ] ──Slack DM──▶ [ OpenClaw Slack channel plugin ]
                              │
                              ▼
                         [ OpenClaw agent = Claude ]
                              │ ← SKILL.md を読んで意思決定
                              │ ← 既存の oc_infer / oc_browser ツールを使う
                              ▼
                ┌─────────────┴──────────────┐
                ▼                            ▼
   [ Python リーフツール群 ]        [ Slack incoming webhook ]
   (run.py / _outreach_core)          ← 一方向の状況報告のみ
        │
        ▼
   [ data/*.jsonl 状態 ]
```

**役割分担:**
- **意思決定（Claude / OpenClaw agent）**: 「誰をリスト化するか」「このドラフトを送るか skip するか」「想定外フィールドが出たので人に聞く」「5 分毎に状況をまとめる」など、判断と会話。
- **決定論的処理（Python）**: JSONL 読み書き、履歴 dedup、フォーム JS 入力、送信後の HTML 検証、ハートビート Webhook 投稿。
- **Slack 入力**: OpenClaw 既存の Slack channel plugin（Socket Mode）で受信。**自作しない。**
- **Slack 出力**: (a) 会話的な応答は OpenClaw plugin が自動でやる。(b) バックグラウンド処理からの一方向通知は **Slack incoming webhook** に POST する小さな `notify.py` 経由。

---

## 1. ゴール（事業者視点）

オーナーが Slack で会話的に Doorman を回す。1 リクエストの理想形:

```
User (Slack): "EdTech FCで売上50億〜200億の企業を10社リストして、
              5社ずつフォームと LinkedIn に振り分けて送って"

OpenClaw agent (Claude):
  1. sender_brief.yaml と global exclude set を読む
  2. WebSearch + 自分の context で 10 社を提案 → targets.yaml/csv に追記
  3. 各社にチャネル割り（form / linkedin）を決め、reasoning を Slack に出す
  4. run.py enrich → draft を順に走らせる
  5. ドラフトを 1 件ずつ Slack スレッドにプレビュー → yes/no/skip を待つ
  6. yes → run.py send --ids N --auto-send
     ├─ 成功 → ✅ を Slack
     ├─ 想定外フィールド検出 → ⚠️ "C列にプルダウン『業界』、何を選ぶ？" と聞く
     └─ 検証 NG → ⚠️ "送信完了画面が出ない。手動確認お願いします" + snapshot
  7. 長時間タスクなら 5 分毎に「今 X/10 件目、所要 Y 分」を Slack に投げる
  8. 終わったら全件サマリ
```

**スコープ:**
- チャネルは **form と linkedin の 2 つだけ**（既存）。Facebook / X は今回触らない
- メッセージング受信は OpenClaw Slack plugin、出力は plugin（会話）+ webhook（状況報告）
- LINE は何も実装しない（OpenClaw 側に将来 LINE plugin が入れば自動で乗る）

**コスト方針:**
- 「Claude が判断する作業」はエージェントのコンテキストで完結させる（=新規 API 呼び出しを増やさない）
- 既存の `oc_infer` 呼び出し（system プロンプトキャッシュ前提）は維持
- リスト生成は **エージェント本体（Opus）が WebSearch しながらやる**。Python から `claude-cli/claude-opus-4-6` を別途叩く CLI は **作らない**

---

## 2. リポジトリ地図（再掲）

| パス | 役割 | 今回の扱い |
|---|---|---|
| `linkedin-outreach/SKILL.md` | OpenClaw 用のスキル仕様＋Slack トリガー表 | **強化対象**（v2 のフロー追記） |
| `linkedin-outreach/run.py` | ~1750 行のパイプライン | 検証ハーネス追加、共通コアに分離 |
| `linkedin-outreach/ARCHITECTURE.md` | 設計ドキュメント | §11.2 / §12 を現実化したら更新 |
| `jp-form-outreach/SKILL.md` | 同上 | **強化対象** |
| `jp-form-outreach/run.py` | ~2000 行、フォーム JS 入力含む | 既存の `_llm_analyze_form` 等は活かす |
| `*/prompts/system_persona.md` | 既存の system プロンプト（キャッシュ命） | **末尾追記以外 NG** |
| `*/data/*.jsonl` | 状態 | 触らない（gitignore 済） |

---

## 3. 守ってほしい不変項（リグレッション禁止）

1. **6 フェーズの名前と順序** (`Pull → Enrich → Personalize → Approve → Send → Log`)
2. **JSONL ファイル名**: `leads.jsonl` / `enriched.jsonl` / `drafts.jsonl` / `sent_history.jsonl` / `skip_history.jsonl`
3. **既存サブコマンド**: `run.py campaign / preview / send --ids N --auto-send / history` 等は表面互換
4. **`sent_history` / `skip_history` は append-only**、必ず既存ヘルパー経由で書く
5. **`prompts/system_persona.md` は byte-stable**（追記する場合は末尾のみ）
6. **Browser profile は `openclaw` 固定**、`oc_browser()` シグネチャ不変
7. **モデル指定は config 経由**、ハードコード禁止

---

## 4. 改修内容

### A. 共通コア `_outreach_core/` の切り出し（Python の整理のみ）

両スキルで重複している関数を抽出する。**ここに LLM 呼び出しの orchestration は入れない。あくまで Python ユーティリティの寄せ場。**

```
_outreach_core/
├── __init__.py
├── README.md                       # 使い方
├── history.py                      # load_skip_set / load_sent_set / append_* / load_global_exclude_set
├── infer.py                        # oc_infer / oc_browser / _run / _evaluate
├── prompt.py                       # build_system_block / extract_first_json
├── draft.py                        # stage_draft 汎用版
├── preview.py                      # stage_preview 汎用版
├── progress.py                     # ★ 新規（§D）
├── notify.py                       # ★ 新規（§E, Slack webhook 投稿のみ）
├── verify.py                       # ★ 新規（§C, 送信後検証）
├── helpers/
│   ├── dump_exclude_set.py         # ★ CLI: 全スキル横断の sent+skip ID を JSON で stdout に吐く
│   ├── append_targets.py           # ★ CLI: list を targets.yaml/csv に追記
│   └── backfill_canonical_ids.py   # 既存履歴に canonical_id を遡及付与（一度だけ実行）
└── scripts/
    └── (任意のメンテスクリプト)
```

各スキルの `run.py` 冒頭で `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` を入れて `from _outreach_core import history, prompt, ...` に書き換える。

**リファクタの検証ゲート**: 書き換え前後で `python run.py campaign --clean --skip-send` の出力 `drafts.jsonl` が **タイムスタンプ以外完全一致**すること（テスト 1 本）。

### B. リスト生成は SKILL.md と小さな Python ヘルパーで構成

**Python に list_builder CLI は作らない。** その代わり:

#### B-1. Python ヘルパー（決定論的な部分だけ）

- `_outreach_core/helpers/dump_exclude_set.py`
  - 標準出力に `{"linkedin": ["slug1", ...], "jp_form": ["yarukiswitch_hd", ...], "canonical": ["..."]}` を JSON で吐く
  - 中身は `load_global_exclude_set()` を呼ぶだけ
  - エージェントが `bash` ツールで実行 → 出力を context に読み込む

- `_outreach_core/helpers/append_targets.py`
  - `--skill linkedin --input - --format jsonl` 形式の CLI
  - 標準入力で JSON 配列を受け、各エントリを該当スキルの `targets.csv` または `targets.yaml` に **重複チェックしながら**追記
  - エージェントが「10 社決めた → これを targets に追加」のときに使う

#### B-2. SKILL.md にリスト生成フローを追記

両方の SKILL.md に **新セクション**:

```markdown
## List build flow (agent-led)

User 命令例:
- "EdTech 中堅 10 社、フォームと LinkedIn に振り分けてリスト化"
- "前回送ったところを除いて、追加で 5 社"

エージェント (Claude) の手順:

1. `cat ~/.openclaw/skills/sender_brief.yaml` で送信者文脈を取得
2. `python3 -m _outreach_core.helpers.dump_exclude_set` で除外 ID 集合を取得
3. WebSearch で sender_brief.target に合致する実在企業を N 社抽出
   - PR TIMES / IR / 公式サイトで存在を検証してから採用
   - 除外集合に含まれる企業は弾く
   - 各社に channel_hint (linkedin / jp_form) と why_fit (2-3 文) を付ける
4. 候補 N 件を Slack に表で投稿 → ユーザーに「これでいい？除外は？」と確認
5. OK が出たら:
   echo '<JSON array>' | python3 -m _outreach_core.helpers.append_targets \
       --skill jp_form --input - --format jsonl
   echo '<JSON array>' | python3 -m _outreach_core.helpers.append_targets \
       --skill linkedin --input - --format jsonl
6. 続けて enrich → draft → preview に進むかユーザーに確認
```

**ポイント:**
- リスト生成のための「別の LLM セッション」は作らない。エージェントが既に持っているコンテキスト + WebSearch で完結
- 検証可能性（PR TIMES / IR 等の出典）はエージェント側で必須化（SKILL.md の禁止事項に「実在しない企業を捏造しない」と明記）

### C. 送信後の自己検証と「想定外フィールド」エスカレーション

これが今回の中核。**LinkedIn より特にフォームで重要。**

#### C-1. `_outreach_core/verify.py`

```python
def verify_send_completed(target: dict, channel: str) -> dict:
    """
    Returns: {"status": "ok" | "uncertain" | "needs_attention",
              "reason": str,
              "evidence": {...},
              "snapshot_path": str | None,
              "unresolved_fields": list[dict] | None}
    """
```

**form チャネルの検証ロジック:**
1. 既存の成功キーワード検出 (`送信完了` / `ありがとうございました` / `THANKS` 等)
2. **追加: フォームに「`required` 属性 + 値が空 or 未選択」の要素が残っていないか走査**
3. **追加: `_llm_analyze_form` が plan に含めなかった required 要素 を列挙**
4. **追加: 確認画面で「入力エラー」「未入力」「正しく入力してください」等の文字列を検出**

返却が `needs_attention` のとき:
- `data/needs_attention.jsonl` に append（target_id / unresolved_fields / snapshot_path / timestamp）
- ブラウザは **閉じない**（ユーザーが手動で続行できるように）
- `notify.py` で Slack webhook に通知（§E）

#### C-2. `run.py send` への組み込み

各 ID の `--auto-send` 完了直後に `verify_send_completed()` を呼ぶ。

- `ok` → 既存通り `append_sent_history()` → 次へ
- `uncertain` → `notify.py` で「⚠️ 送信完了画面が確認できません。手動確認してください」を Slack に出す。`sent_history` には書かず、`needs_attention.jsonl` に保留
- `needs_attention` → `notify.py` で「⚠️ 想定外の入力項目 [プルダウン: 業界, テキスト: 紹介者] が必須です。Slack で値を教えてください」を投稿。**OpenClaw エージェントが Slack でユーザーの回答を受けて、`run.py resolve --target-id X --field 業界=その他 --field 紹介者=なし` を実行する流れ**

#### C-3. `run.py resolve` サブコマンドを新規追加

```bash
python run.py resolve --target-id <id> --field key=value [--field key=value ...]
```

- `needs_attention.jsonl` から該当エントリを引き、保留中のブラウザに戻る or `target.field_map_overrides` を更新して再 send
- 成功したら `needs_attention.jsonl` のエントリをクローズ済みフラグ付きで追記

#### C-4. SKILL.md への手順追記

両 SKILL.md に:

```markdown
## Send verification & escalation

`run.py send --ids N --auto-send` 実行後、各 ID は自動的に verify される。

Slack に届くサイン:
- ✅ `<会社名>` 送信完了 → `sent_history.jsonl` に記録済
- ⚠️ `<会社名>` 送信完了が確認できません: <reason> → エージェントは手動確認の有無を聞く
- ⚠️ `<会社名>` 想定外の入力項目: <fields> → エージェントはユーザーから値を聞き出し、
  `run.py resolve --target-id <id> --field key=value ...` を実行

needs_attention は `data/needs_attention.jsonl` で永続化、`run.py history needs-attention` で一覧可能。
```

### D. 5 分毎のハートビート（長時間タスクの状況共有）

**設計:**
- `_outreach_core/progress.py` が `data/current_task.jsonl`（append-only）に進捗イベントを書く
- `run.py send` / `run.py enrich` などの長時間処理は **開始時に progress.start()**、各 target 完了後に **progress.tick()**、終了時に **progress.end()**
- 同時にバックグラウンドスレッドが起動し、5 分毎に `notify.py` 経由で Slack webhook に「現在 X/Y 件目、経過 Z 分、最後のアクション: ...」を投げる
- 終了時にハートビートを止めて、最後にサマリ 1 通だけ投稿

**実装メモ:**
- ハートビート間隔は config 化（`heartbeat_interval_sec`、既定 300）
- 5 分未満で完了するタスクは start/end のみで中間ハートビートを出さない（うるさいので）
- Webhook 障害時は黙って続行（ログだけ残す）。タスクは止めない

`run.py send` の起動オプション:
```bash
python run.py send --ids 1,2,3 --auto-send --heartbeat slack
```
- `--heartbeat slack` を指定したときだけ webhook 投稿スレッドを起動
- 未指定 = 既存挙動（heartbeat なし）

### E. Slack incoming webhook 専用ユーティリティ `notify.py`

**目的: バックグラウンド処理から一方向の状況通知を送るためだけ。** OpenClaw Slack plugin（双方向の会話チャネル）は触らない。

```python
# _outreach_core/notify.py
def post(text: str, *, level: str = "info", thread_ts: str | None = None) -> bool:
    """
    Send to Slack incoming webhook URL configured in sender_brief.yaml:
        slack:
          incoming_webhook_url: "https://hooks.slack.com/services/..."
          channel_id: "C0123ABCD"   # OpenClaw plugin と同じチャンネルを推奨

    level: "info" | "warn" | "error" → 絵文字プレフィクス
    Returns True if 2xx, False otherwise. **Never raises.**
    """
```

- 依存は `requests` だけ
- リトライは 1 回まで、それ以上は黙って False を返す
- Slack 側で受信したメッセージを **エージェントが拾って次の会話アクションに使える**（OpenClaw plugin が同じチャンネルを購読しているなら自然に文脈に入る）

**重要: webhook URL は `sender_brief.yaml` の `slack.incoming_webhook_url` から読む。** 未設定の場合 `notify.post()` は no-op（False を返すだけ）。CI でも安全。

### F. `sender_brief.yaml` の新設

`~/.openclaw/skills/sender_brief.yaml`（gitignore）と `sender_brief.example.yaml`（コミット可）を追加:

```yaml
sender:
  name: "..."
  company: "..."
  role: "..."
product:
  name: "..."
  one_liner: "..."
  problems_solved:
    - "..."
target:
  industries: [...]
  size_band: "..."
  decision_makers: [...]
  geo: "JP"
  must_have_signals: [...]
  must_not_signals: [...]
desired_channels: ["linkedin", "jp_form"]
slack:
  incoming_webhook_url: ""    # 一方向通知用（空ならハートビート/エスカレーション無効）
  channel_id: ""              # OpenClaw plugin と揃えると会話文脈に乗る
heartbeat:
  interval_sec: 300
  enabled_for: ["send", "enrich"]
```

- 既存 `linkedin-outreach/config.yaml` / `jp-form-outreach/config.yaml` を **置き換えない**
- `_outreach_core/config.py` に `load_merged_config(skill_dir)` を追加して、`sender_brief.yaml`（共通）+ skill 側 `config.yaml`（チャネル固有）をマージして返す
- マージ規則: skill 側が勝つ（既存挙動を壊さない）
- `sender_brief.yaml` が存在しなければ skill 側 `config.yaml` 単独で動く（後方互換）

### G. スケジューリング (`openclaw cron`) の例を README に追記

新規コードは不要。README とは別に **`docs/SCHEDULING.md`** を 1 枚作って例を載せる:

```markdown
# 定期実行レシピ

毎週月曜 9:00 に EdTech 10 社をリスト化して preview まで:
openclaw cron add --schedule "0 9 * * 1" \
  "doorman: sender_brief.yaml に基づき EdTech 中堅 10 社をリスト化し、
   targets に追加し、enrich → draft → preview を実行。送信は確認後"

毎日 18:00 に未送信ドラフトのサマリ:
openclaw cron add --schedule "0 18 * * *" \
  "doorman: 全スキルの drafts.jsonl のうち未送信を Slack にサマリ"
```

エージェントが Slack 文を読んで対応するコマンドを組み立てる前提なので、Python 側に追加実装は不要。

---

## 5. SKILL.md 強化（v2）

両 SKILL.md に **以下のセクションを追記**（既存セクションは消さない）:

1. **List build flow (agent-led)** — §B-2
2. **Send verification & escalation** — §C-4
3. **Heartbeat behavior** — 「`--heartbeat slack` を渡すと 5 分毎に Slack に状況投稿、開始/終了通知も自動」
4. **needs_attention の取り扱い** — エージェントが見つけたときに `resolve` で解決する手順

Slack トリガー表に追記:

| ユーザー発話 | エージェントの行動 |
|---|---|
| 「リスト化して」/「N社見繕って」 | List build flow に従う |
| 「進捗どう？」/「今何してる？」 | `tail -n 20 data/current_task.jsonl` を要約して返す |
| 「<会社>に <値> で送って」（needs_attention の応答） | `run.py resolve --target-id ... --field ...` を実行 |
| 「全部止めて」 | `pkill -f "run.py send"` + `notify.post("中断しました")` |

---

## 6. 受け入れ条件（PR 出す前に通すこと）

1. `linkedin-outreach/run.py campaign --input targets.csv --limit 3 --clean --skip-send` の出力が、リファクタ前後で `drafts.jsonl` 完全一致（タイムスタンプ除く）
2. `jp-form-outreach/run.py campaign --clean --skip-send` も同様に一致
3. `python3 -m _outreach_core.helpers.dump_exclude_set` が `{"linkedin": [...], "jp_form": [...], "canonical": [...]}` を JSON で吐く
4. `echo '[{"name": "テスト株式会社", ...}]' | python3 -m _outreach_core.helpers.append_targets --skill jp_form --input -` が targets.yaml に追記（重複は弾く）
5. **想定外フィールド検出テスト**: ダミーの HTML で `required` 要素を 1 つ plan に入れずに verify を呼び、`needs_attention` 判定が出ること
6. **検証 NG テスト**: 成功キーワード不在の HTML を渡すと `uncertain` 判定で `needs_attention.jsonl` に書き込まれること
7. `run.py send --ids 1 --auto-send --heartbeat slack` で webhook URL 未設定時に **タスクが落ちず**（notify.post が no-op）、進行ログだけ stdout に出ること
8. webhook URL 設定時、ハートビートが約 5 分間隔で投稿されること（積分テストではなくモックで十分）
9. `run.py resolve --target-id X --field foo=bar` が `needs_attention.jsonl` の該当エントリをクローズし、再 send を試行すること
10. `SKILL.md` の既存 Slack トリガー行が依然として有効

---

## 7. やらないでほしいこと

- **slack_bolt や Socket Mode の自作受信ワーカー**。OpenClaw Slack plugin が双方向会話を担当。
- **list 生成のための独立 LLM CLI**。エージェントの context + WebSearch で生成する。Python は `dump_exclude_set` と `append_targets` のヘルパーだけ。
- **新規スキル `facebook-outreach` / `x-outreach` の作成**。スコープ外。
- **既存 JSONL のカラム削除・リネーム**。`canonical_id` 等は追加のみ。
- **`prompts/system_persona.md` の既存セクション書き換え**。末尾追記のみ可。
- **モデル名のハードコード**。
- **`sent_history.jsonl` / `skip_history.jsonl` の上書き**。append only。
- **`config.yaml`（個人情報入り）の Git コミット**。`.example.yaml` のみ更新。
- **OpenClaw のデフォルト browser profile `openclaw` 以外への切り替え**。
- **`--heartbeat slack` 未指定時にバックグラウンドスレッドを起動すること**。既存挙動を壊す。
- **needs_attention 時にブラウザを自動で閉じること**。ユーザーが手動で続けたい場合に困る。

---

## 8. 作業順序（推奨）

1. **§A 共通コアの切り出し**（無口なリファクタ）— §6 (1)(2) を通す
2. **§E `notify.py` と §F `sender_brief.yaml`** — webhook 投稿の土台
3. **§C 送信後検証 + needs_attention + `run.py resolve`** — §6 (5)(6)(9) を通す
4. **§D ハートビート** — §6 (7)(8) を通す
5. **§B-1 `dump_exclude_set` / `append_targets` ヘルパー** — §6 (3)(4) を通す
6. **SKILL.md 強化（§5）** + ARCHITECTURE.md / README 更新
7. **§G `docs/SCHEDULING.md`** を最後に書く

各ステップで **コミットを分ける**。1 PR ではなく段階レビューにする。

---

## 9. 仕様で迷ったときの優先順位

1. **OpenClaw エージェントが今やっているフローを壊さない** > その他
2. **「これは LLM の判断か / 決定論的処理か」で迷ったら** → 判断は LLM（エージェント）、データ処理は Python
3. **「Slack の投稿はどっち経由か」で迷ったら** → 会話的応答は OpenClaw plugin、状況通知は webhook
4. **「新しい CLI を生やすか / SKILL.md にフロー追記か」で迷ったら** → SKILL.md 追記を優先（エージェント主導の哲学に沿う）
5. **パフォーマンス vs シンプルさ** → シンプルさ優先（Doorman は volume より personalization）

以上。
