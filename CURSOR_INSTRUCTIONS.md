# Cursor 向け指示書 — Doorman (openclaw_skills) ブラッシュアップ v3

このリポジトリ (`~/.openclaw/skills/`) を Cursor で改修してもらうための指示書です。

**前提:**
- OpenClaw（Claude 製のローカルエージェント）が司令塔、Python はリーフツール、Slack は OpenClaw の入出力チャネル。
- **Slack で会話しているエージェント本体は `claude-opus-4-7` で固定運用します。** モデルを動的に切り替える設計は入れません。
- 一方、Python から `oc_infer` で呼ぶサブ LLM タスクは **Sonnet ピン留め**を既定とし、コストを抑えます。

> v1 では slack_bolt 自作ワーカー / list_builder 独立 CLI を作らせようとしていましたが撤回しました。v2 で OpenClaw ネイティブに直し、v3 で「エージェント = Opus 4.7」前提を明文化したのがこの版です。

---

## 0. 全体像

```
[ ユーザー ] ──Slack DM──▶ [ OpenClaw Slack channel plugin ]
                              │
                              ▼
                  [ OpenClaw agent = Claude Opus 4.7 ]
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

---

## 0.5 モデル割当トポロジー（最重要）

| レイヤー | 担当 | モデル | 理由 |
|---|---|---|---|
| Slack で会話する OpenClaw エージェント | 命令の解釈、リスト生成、戦略判断、想定外時の人への質問、進捗サマリ | **Opus 4.7（固定）** | 推論品質と WebSearch を要する作業を一手に引き受けるため |
| `run.py draft`（Personalize 段） | 1 件ずつの本文/件名生成 | **Sonnet 4.6**（config.yaml で固定） | 既存の prompt キャッシュ前提、コスト最小化 |
| `run.py enrich` 内 `_llm_analyze_form`（form 用） | フォーム DOM → 入力プラン JSON | **Sonnet 4.6**（同上） | 定型変換、キャッシュ効きやすい |
| `run.py send` 後の `verify.py` | 成功画面検出 / required 未入力検出 / 想定外フィールド検出 | **LLM 呼び出しなし（純 Python）** | 決定論的に書ける範囲は LLM を使わない |
| 想定外フィールドが見つかった後の判断・ユーザーへの質問文の組み立て | エージェント本体（Opus 4.7）に委譲 | **Opus 4.7** | verify が `needs_attention` を吐いたら Slack に投げ、続きはエージェントが Slack 会話で解決 |
| `list-build`（ターゲット企業候補生成） | エージェントが WebSearch + 自分の context で実施 | **Opus 4.7（=エージェント本体）** | 別 LLM セッションを作らない＝キャッシュミスを避ける |

**設計原則:**
- 「判断・会話・調査」は Opus エージェントの context で完結させ、追加 API 呼び出しを生やさない
- 「テンプレ的変換・成形」は Sonnet を `oc_infer` で 1 ショット
- 「決定論的に書ける処理」は LLM を一切使わない

---

## 1. ゴール（事業者視点）

オーナーが Slack で会話的に Doorman を回す。1 リクエストの理想形:

```
User (Slack): "EdTech FCで売上50億〜200億の企業を10社リストして、
              5社ずつフォームと LinkedIn に振り分けて送って"

OpenClaw agent (Opus 4.7):
  1. sender_brief.yaml と global exclude set を読む
  2. WebSearch + 自分の context で 10 社を提案 → targets.yaml/csv に追記
  3. 各社にチャネル割り（form / linkedin）を決め、reasoning を Slack に出す
  4. run.py enrich → draft を順に走らせる（中身は Sonnet）
  5. ドラフトを 1 件ずつ Slack スレッドにプレビュー → yes/no/skip を待つ
  6. yes → run.py send --ids N --auto-send
     ├─ verify ok      → ✅ を Slack
     ├─ verify uncertain → ⚠️ "送信完了画面が出ない。手動確認お願いします" + snapshot
     └─ verify needs_attention → ⚠️ "想定外のプルダウン『業界』、何を選ぶ？"
                                  → 回答受領 → run.py resolve
  7. 長時間タスクなら 5 分毎に「今 X/10 件目、所要 Y 分」を Slack に投げる
  8. 終わったら全件サマリ
```

**スコープ:**
- チャネルは **form と linkedin の 2 つだけ**（既存）。Facebook / X は今回触らない
- メッセージング受信は OpenClaw Slack plugin、出力は plugin（会話）+ webhook（状況報告）
- LINE は何も実装しない（将来 OpenClaw 側に LINE plugin が入れば自然に乗る）

---

## 2. リポジトリ地図（再掲）

| パス | 役割 | 今回の扱い |
|---|---|---|
| `linkedin-outreach/SKILL.md` | OpenClaw 用のスキル仕様＋Slack トリガー表 | **強化対象**（v3 のフロー追記） |
| `linkedin-outreach/run.py` | ~1750 行のパイプライン | 検証ハーネス追加、共通コアに分離 |
| `linkedin-outreach/ARCHITECTURE.md` | 設計ドキュメント | §11.2 / §12 を現実化したら更新 |
| `jp-form-outreach/SKILL.md` | 同上 | **強化対象** |
| `jp-form-outreach/run.py` | ~2000 行、フォーム JS 入力含む | 既存の `_llm_analyze_form` 等は活かす |
| `*/prompts/system_persona.md` | 既存の system プロンプト（Sonnet キャッシュ命） | **末尾追記以外 NG** |
| `*/data/*.jsonl` | 状態 | 触らない（gitignore 済） |

---

## 3. 守ってほしい不変項（リグレッション禁止）

1. **6 フェーズの名前と順序** (`Pull → Enrich → Personalize → Approve → Send → Log`)
2. **JSONL ファイル名**: `leads.jsonl` / `enriched.jsonl` / `drafts.jsonl` / `sent_history.jsonl` / `skip_history.jsonl`
3. **既存サブコマンド**: `run.py campaign / preview / send --ids N --auto-send / history` 等は表面互換
4. **`sent_history` / `skip_history` は append-only**、必ず既存ヘルパー経由で書く
5. **`prompts/system_persona.md` は byte-stable**（追記は末尾のみ）
6. **Browser profile は `openclaw` 固定**、`oc_browser()` シグネチャ不変
7. **モデル指定は config 経由**、ハードコード禁止
8. **Python から呼ぶ `oc_infer` は Sonnet 既定**。Opus に切り替える可能性のあるコードでも、デフォルト引数は Sonnet にしておく

---

## 4. 改修内容

### A. 共通コア `_outreach_core/` の切り出し（Python の整理）

両スキルで重複している関数を抽出する。**ここに LLM 呼び出しの orchestration は入れない。あくまで Python ユーティリティの寄せ場。**

```
_outreach_core/
├── __init__.py
├── README.md                       # 使い方
├── history.py                      # load_skip_set / load_sent_set / append_* / load_global_exclude_set
├── infer.py                        # oc_infer / oc_browser / _run / _evaluate（既定 model = Sonnet）
├── prompt.py                       # build_system_block / extract_first_json
├── draft.py                        # stage_draft 汎用版（Sonnet 呼び出し）
├── preview.py                      # stage_preview 汎用版
├── progress.py                     # ★ 新規（§D）
├── notify.py                       # ★ 新規（§E, Slack webhook 投稿のみ、LLM 呼ばない）
├── verify.py                       # ★ 新規（§C, 純 Python、LLM 呼ばない）
├── config.py                       # ★ 新規（§F, sender_brief + skill config をマージ）
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

**Python に list_builder CLI は作らない。** リスト生成は Opus エージェントの context で WebSearch しながらやる。

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
## List build flow (agent-led, Opus 4.7 を前提)

User 命令例:
- "EdTech 中堅 10 社、フォームと LinkedIn に振り分けてリスト化"
- "前回送ったところを除いて、追加で 5 社"

エージェント (Claude Opus 4.7) の手順:

1. `cat ~/.openclaw/skills/sender_brief.yaml` で送信者文脈を取得
2. `python3 -m _outreach_core.helpers.dump_exclude_set` で除外 ID 集合を取得
3. WebSearch で sender_brief.target に合致する実在企業を N 社抽出
   - PR TIMES / IR / 公式サイトで存在を検証してから採用（捏造禁止）
   - 除外集合に含まれる企業は弾く
   - 各社に channel_hint (linkedin / jp_form) と why_fit (2-3 文) を付ける
4. 候補 N 件を Slack に表で投稿 → ユーザーに「これでいい？除外は？」と確認
5. OK が出たら:
   echo '<JSON array>' | python3 -m _outreach_core.helpers.append_targets \
       --skill jp_form --input - --format jsonl
   echo '<JSON array>' | python3 -m _outreach_core.helpers.append_targets \
       --skill linkedin --input - --format jsonl
6. 続けて enrich → draft → preview に進むかユーザーに確認

注: Sonnet エージェントで動かすと候補品質が落ちます。本フローは
    Opus 4.7 エージェント前提です。
```

### C. 送信後の自己検証と「想定外フィールド」エスカレーション

#### C-1. `_outreach_core/verify.py`（**純 Python、LLM 呼び出しなし**）

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

**form チャネルの検証ロジック（すべて DOM 走査ベース、LLM 不使用）:**
1. 既存の成功キーワード検出 (`送信完了` / `ありがとうございました` / `THANKS` 等)
2. **追加: フォームに「`required` 属性 + 値が空 or 未選択」の要素が残っていないか走査**
3. **追加: `_llm_analyze_form` が plan に含めなかった required 要素を列挙**
4. **追加: 確認画面で「入力エラー」「未入力」「正しく入力してください」等の文字列を検出**

返却が `needs_attention` のとき:
- `data/needs_attention.jsonl` に append（target_id / unresolved_fields / snapshot_path / timestamp）
- ブラウザは **閉じない**（ユーザーが手動で続行できるように）
- `notify.py` で Slack webhook に通知（§E）
- **続きの判断（ユーザーへの質問文作成・回答解釈）は Opus エージェント本体に委ねる**。verify.py はあくまで「何が起きたか」を構造化して投げるだけ

#### C-2. `run.py send` への組み込み

各 ID の `--auto-send` 完了直後に `verify_send_completed()` を呼ぶ。

- `ok` → 既存通り `append_sent_history()` → 次へ
- `uncertain` → `notify.py` で「⚠️ 送信完了画面が確認できません。手動確認してください」を Slack に出す。`sent_history` には書かず、`needs_attention.jsonl` に保留
- `needs_attention` → `notify.py` で「⚠️ 想定外の入力項目 [プルダウン: 業界, テキスト: 紹介者] が必須です。Slack で値を教えてください」を投稿。**Opus エージェントが Slack でユーザーの回答を受けて、`run.py resolve --target-id X --field 業界=その他 --field 紹介者=なし` を実行する流れ**

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

`run.py send --ids N --auto-send` 実行後、各 ID は自動的に verify される（決定論的、LLM 不使用）。

Slack に届くサイン:
- ✅ `<会社名>` 送信完了 → `sent_history.jsonl` に記録済
- ⚠️ `<会社名>` 送信完了が確認できません: <reason> → エージェント(Opus)が手動確認の有無を聞く
- ⚠️ `<会社名>` 想定外の入力項目: <fields> → エージェント(Opus)がユーザーから値を聞き出し、
  `run.py resolve --target-id <id> --field key=value ...` を実行

needs_attention は `data/needs_attention.jsonl` で永続化、`run.py history needs-attention` で一覧可能。
```

### D. 5 分毎のハートビート（長時間タスクの状況共有）

**設計:**
- `_outreach_core/progress.py` が `data/current_task.jsonl`（append-only）に進捗イベントを書く
- `run.py send` / `run.py enrich` などの長時間処理は **開始時に progress.start()**、各 target 完了後に **progress.tick()**、終了時に **progress.end()**
- 同時にバックグラウンドスレッドが起動し、5 分毎に `notify.py` 経由で Slack webhook に「現在 X/Y 件目、経過 Z 分、最後のアクション: ...」を投げる
- 終了時にハートビートを止めて、最後にサマリ 1 通だけ投稿
- **LLM 呼び出しは行わない**。サマリ文は Python のテンプレートで組む

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
- Slack 側で受信したメッセージを **エージェント(Opus)が拾って次の会話アクションに使える**（OpenClaw plugin が同じチャンネルを購読しているなら自然に文脈に入る）

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
# モデル設定（参考。Python 側 oc_infer はここを読まない。
# 各 skill の config.yaml の model.name を参照する）
notes_on_models: |
  - Slack で会話するエージェント本体は Opus 4.7 で運用する前提。
  - Python から呼ぶ oc_infer は Sonnet 既定。コスト最適化のため変更しない。
```

- 既存 `linkedin-outreach/config.yaml` / `jp-form-outreach/config.yaml` を **置き換えない**
- `_outreach_core/config.py` に `load_merged_config(skill_dir)` を追加して、`sender_brief.yaml`（共通）+ skill 側 `config.yaml`（チャネル固有）をマージして返す
- マージ規則: skill 側が勝つ（既存挙動を壊さない）
- `sender_brief.yaml` が存在しなければ skill 側 `config.yaml` 単独で動く（後方互換）

### G. スケジューリング (`openclaw cron`) の例を docs に追記

新規コードは不要。`docs/SCHEDULING.md` を 1 枚作って例を載せる:

```markdown
# 定期実行レシピ（OpenClaw Opus エージェント前提）

毎週月曜 9:00 に EdTech 10 社をリスト化して preview まで:
openclaw cron add --schedule "0 9 * * 1" \
  "doorman: sender_brief.yaml に基づき EdTech 中堅 10 社をリスト化し、
   targets に追加し、enrich → draft → preview を実行。送信は確認後"

毎日 18:00 に未送信ドラフトのサマリ:
openclaw cron add --schedule "0 18 * * *" \
  "doorman: 全スキルの drafts.jsonl のうち未送信を Slack にサマリ"

毎時 needs_attention チェック:
openclaw cron add --schedule "0 * * * *" \
  "doorman: 全スキルの needs_attention.jsonl に未解決があれば Slack に投げ直す"
```

エージェントが Slack 文を読んで対応するコマンドを組み立てる前提なので、Python 側に追加実装は不要。

---

## 5. SKILL.md 強化（v3）

両 SKILL.md に **以下のセクションを追記**（既存セクションは消さない）:

1. **List build flow (agent-led, Opus 4.7 前提)** — §B-2
2. **Send verification & escalation** — §C-4
3. **Heartbeat behavior** — 「`--heartbeat slack` を渡すと 5 分毎に Slack に状況投稿、開始/終了通知も自動」
4. **needs_attention の取り扱い** — エージェントが見つけたときに `resolve` で解決する手順
5. **Model assumption** — 「Slack 側エージェントは Opus 4.7。Python 側 `oc_infer` は Sonnet 既定」と明記

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
5. **想定外フィールド検出テスト**: ダミーの HTML で `required` 要素を 1 つ plan に入れずに verify を呼び、`needs_attention` 判定が出ること（**LLM を呼ばずに**）
6. **検証 NG テスト**: 成功キーワード不在の HTML を渡すと `uncertain` 判定で `needs_attention.jsonl` に書き込まれること
7. `run.py send --ids 1 --auto-send --heartbeat slack` で webhook URL 未設定時に **タスクが落ちず**（notify.post が no-op）、進行ログだけ stdout に出ること
8. webhook URL 設定時、ハートビートが約 5 分間隔で投稿されること（積分テストではなくモックで十分）
9. `run.py resolve --target-id X --field foo=bar` が `needs_attention.jsonl` の該当エントリをクローズし、再 send を試行すること
10. `SKILL.md` の既存 Slack トリガー行が依然として有効
11. `_outreach_core/infer.py` の `oc_infer` 既定 model が `claude-cli/claude-sonnet-4-6` であること（grep テスト）
12. `verify.py` 内に `oc_infer` / `openclaw infer` / `subprocess.*infer` の呼び出しが存在しないこと（grep テスト = LLM 不使用の担保）

---

## 7. やらないでほしいこと

- **slack_bolt や Socket Mode の自作受信ワーカー**。OpenClaw Slack plugin が双方向会話を担当。
- **list 生成のための独立 LLM CLI**。Opus エージェントの context + WebSearch で生成する。Python は `dump_exclude_set` と `append_targets` のヘルパーだけ。
- **新規スキル `facebook-outreach` / `x-outreach` の作成**。スコープ外。
- **既存 JSONL のカラム削除・リネーム**。`canonical_id` 等は追加のみ。
- **`prompts/system_persona.md` の既存セクション書き換え**。末尾追記のみ可（Sonnet キャッシュ前提）。
- **モデル名のハードコード**。
- **`oc_infer` の既定モデルを Opus にすること**。Sonnet 既定を維持。
- **`sent_history.jsonl` / `skip_history.jsonl` の上書き**。append only。
- **`config.yaml`（個人情報入り）の Git コミット**。`.example.yaml` のみ更新。
- **OpenClaw のデフォルト browser profile `openclaw` 以外への切り替え**。
- **`--heartbeat slack` 未指定時にバックグラウンドスレッドを起動すること**。既存挙動を壊す。
- **needs_attention 時にブラウザを自動で閉じること**。ユーザーが手動で続けたい場合に困る。
- **verify.py 内で LLM を呼ぶこと**。決定論的に書ける範囲を LLM に置き換えるとコストと不安定さが両方増える。

---

## 8. 作業順序（推奨）

1. **§A 共通コアの切り出し**（無口なリファクタ）— §6 (1)(2)(11) を通す
2. **§E `notify.py` と §F `sender_brief.yaml`** — webhook 投稿の土台
3. **§C 送信後検証 + needs_attention + `run.py resolve`** — §6 (5)(6)(9)(12) を通す
4. **§D ハートビート** — §6 (7)(8) を通す
5. **§B-1 `dump_exclude_set` / `append_targets` ヘルパー** — §6 (3)(4) を通す
6. **SKILL.md 強化（§5）** + ARCHITECTURE.md / README 更新
7. **§G `docs/SCHEDULING.md`** を最後に書く

各ステップで **コミットを分ける**。1 PR ではなく段階レビューにする。

---

## 9. 仕様で迷ったときの優先順位

1. **OpenClaw エージェントが今やっているフローを壊さない** > その他
2. **「これは Opus エージェントの判断か / Sonnet サブタスクか / 純 Python か」で迷ったら**:
   - 判断・会話・調査・人への質問文 → Opus エージェント（追加 API なし）
   - テンプレ的変換・成形（draft 文章、フォームプラン JSON）→ Sonnet on `oc_infer`
   - 構造的に書ける検出・パース・履歴操作 → 純 Python
3. **「Slack の投稿はどっち経由か」で迷ったら** → 会話的応答は OpenClaw plugin、状況通知は webhook
4. **「新しい CLI を生やすか / SKILL.md にフロー追記か」で迷ったら** → SKILL.md 追記を優先（エージェント主導の哲学に沿う）
5. **パフォーマンス vs シンプルさ** → シンプルさ優先（Doorman は volume より personalization）

---

## 10. コスト見立て（v3 構成）

| 単位 | モデル | 想定回数/月 | 大まかなコスト感 |
|---|---|---|---|
| Opus エージェントの 1 会話セッション | Opus 4.7 | 数十〜100 | サブスク routing 内で吸収（実 API 換算で本流） |
| `run.py draft` 1 件 | Sonnet 4.6（cached） | 100〜500 | ~$0.005/件 |
| `_llm_analyze_form` 1 件 | Sonnet 4.6 | 100〜500 | ~$0.003/件 |
| `verify.py` 1 件 | なし | 同上 | $0 |
| ハートビート 1 件 | なし | 数十〜数百 | $0（Slack webhook のみ） |

合計の支配項は **Opus エージェントの会話**。これは Cowork/Claude desktop のサブスクで吸収されるので、Python 側の追加 API コストは月数ドル以内に収まる見込み。

---

以上。
