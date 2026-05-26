# Cursor 向け指示書 — Doorman (openclaw_skills) ブラッシュアップ v4

このリポジトリ (`~/.openclaw/skills/`) を Cursor で改修してもらうための指示書です。

**前提:**
- OpenClaw（Claude 製のローカルエージェント）が司令塔、Python はリーフツール、Slack は OpenClaw の入出力チャネル。
- **Slack で会話しているエージェント本体は `claude-opus-4-7` で固定運用します。**
- Python から `oc_infer` で呼ぶサブ LLM タスクは **タスク毎にモデルを使い分け**:
  - **`stage_draft` / `_refine_draft` = Opus 4.7**（v4 で Sonnet → Opus に昇格、品質改善のため）
  - **`_llm_analyze_form` = Sonnet 4.6**（構造変換タスク、コスト最適）

> **v4 の主な変更点:**
> 1. ドラフト生成プロセスを Opus 4.7 に昇格（品質課題の解消が主目的）
> 2. §11 新設: jp-form-outreach の送信成功率／ドラフト品質改善（実データから判明した 12 件の bug/改善）
>
> v3 から v4 への移行は **既存のリファクタを壊さない**。`_outreach_core/`、`sender_brief.yaml`、`verify.py`、`progress.py`、`notify.py` は既に実装済として扱い、上に変更を積む。

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

## 0.5 モデル割当トポロジー（v4 改訂版）

| レイヤー | 担当 | モデル | 理由 |
|---|---|---|---|
| Slack で会話する OpenClaw エージェント | 命令の解釈、リスト生成、戦略判断、想定外時の人への質問、進捗サマリ、enrich-research の Web 検索 | **Opus 4.7（固定）** | 推論品質と WebSearch を要する作業を一手に引き受ける |
| **`run.py draft`（Personalize 段）** | 1 件ずつの本文/件名生成 | **Opus 4.7（v4 で昇格）** | 文章バリエーション・キーガード強制・固有事実への接続が Sonnet では弱かったため。プロンプトキャッシュは継続 |
| **`_refine_draft`（refine 段）** | クリティーク→書き直し | **Opus 4.7（v4 で昇格）** | 自己批判ループは Opus の方が機能する |
| `run.py enrich` 内 `_llm_analyze_form`（form 用） | フォーム DOM → 入力プラン JSON | **Sonnet 4.6**（config.yaml で固定） | 構造的変換タスク。Sonnet で十分機能している（実データで確認済） |
| `run.py send` 後の `verify.py` | 成功画面検出 / required 未入力検出 / 想定外フィールド検出 | **LLM 呼び出しなし（純 Python）** | 決定論的に書ける範囲は LLM を使わない |
| 想定外フィールド/再 plan 必要時の判断・ユーザーへの質問文 | エージェント本体に委譲 | **Opus 4.7** | verify が `needs_attention` を吐いたら Slack に投げ、続きはエージェント |
| `list-build`（ターゲット企業候補生成） | エージェントが WebSearch + 自分の context で実施 | **Opus 4.7（=エージェント本体）** | 別 LLM セッションを作らない＝キャッシュミスを避ける |
| **enrich-research（v4 新設）** | 各社の PR TIMES / IR / プレスリリースを集めて direct_signals 拡充 | **Opus 4.7（=エージェント本体）** | WebSearch を伴うので agent in-context が適切 |

**設計原則:**
- 「判断・会話・調査・パーソナル文章生成」は Opus（エージェント or `oc_infer`）
- 「テンプレ的変換・成形」は Sonnet on `oc_infer`
- 「構造的に書ける処理」は LLM を一切使わない

---

## 1. ゴール（事業者視点）

オーナーが Slack で会話的に Doorman を回す。1 リクエストの理想形:

```
User (Slack): "EdTech FCで売上50億〜200億の企業を10社リストして、
              5社ずつフォームと LinkedIn に振り分けて送って"

OpenClaw agent (Opus 4.7):
  1. sender_brief.yaml と global exclude set を読む
  2. WebSearch で 10 社を抽出 → targets.yaml/csv に追記
  3. 各社の最新ニュース・IR を WebSearch で集め、direct_signals を埋める (v4 新設)
  4. run.py enrich → draft (Opus) を順に走らせる
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
- チャネルは **form と linkedin の 2 つだけ**。Facebook / X は今回触らない
- メッセージング受信は OpenClaw Slack plugin、出力は plugin（会話）+ webhook（状況報告）
- LINE は何も実装しない

---

## 2. リポジトリ地図

| パス | 役割 | 今回の扱い |
|---|---|---|
| `_outreach_core/` | v3 で導入済の共通コア | 既存。差分のみ追加 |
| `_outreach_core/verify.py` | 送信後検証 | **v4 で順序バグ修正**（§11-A-1） |
| `_outreach_core/draft.py` | 汎用 draft ステージ | **v4 で Opus 切替 + char_limit ハード強制**（§11-B-1） |
| `linkedin-outreach/SKILL.md` | OpenClaw 用スキル仕様 | 強化 |
| `linkedin-outreach/run.py` | パイプライン | 既存 |
| `jp-form-outreach/SKILL.md` | スキル仕様 | **v4 で大幅強化**（enrich-research / send escalation） |
| `jp-form-outreach/run.py` | パイプライン（フォーム JS 入力含む） | **v4 で送信ロジック強化**（§11-A） |
| `jp-form-outreach/prompts/system_persona.md` | ドラフト用 system プロンプト | **v4 で末尾セクション追記**（冒頭型ローテーション） |
| `jp-form-outreach/prompts/examples.md` | few-shot | **v4 で自己紹介バリエーション追加** |
| `sender_brief.yaml` | 共通の送信者ブリーフ | 既存 |

---

## 3. 守ってほしい不変項（リグレッション禁止）

1. **6 フェーズの名前と順序** (`Pull → Enrich → Personalize → Approve → Send → Log`)
2. **JSONL ファイル名**: `leads.jsonl` / `enriched.jsonl` / `drafts.jsonl` / `sent_history.jsonl` / `skip_history.jsonl` / `needs_attention.jsonl`
3. **既存サブコマンド**: `run.py campaign / preview / send --ids N --auto-send / history / resolve` 等は表面互換
4. **`sent_history` / `skip_history` / `needs_attention` は append-only**、必ず既存ヘルパー経由で書く
5. **`prompts/system_persona.md` は byte-stable**（追記は末尾のみ）。**Sonnet → Opus へのモデル切替時もキャッシュは別系統で立ち上がるだけで、文面は維持**
6. **Browser profile は `openclaw` 固定**、`oc_browser()` シグネチャ不変
7. **モデル指定は config 経由**、ハードコード禁止
8. **`oc_infer` のモデル指定はタスク毎:**
   - `stage_draft` / `_refine_draft` → **Opus 4.7 ピン留め**
   - `_llm_analyze_form` → **Sonnet 4.6 ピン留め**（変更しない）
   - その他の Python 呼び出しは Sonnet を既定とする
9. **`verify.py` 内で LLM を呼ばない**（純 Python）
10. **既存 JSONL のカラム削除・リネーム禁止**（追加のみ可）

---

## 4. 既存 v3 改修内容（リファレンス、変更なし）

§4 A-G は v3 で記述済。Cursor は既に実装済の前提。差分は §11 を参照。

要約:
- §4-A: `_outreach_core/` の切り出し
- §4-B: list-build flow（エージェント主導 + Python ヘルパー 2 本）
- §4-C: 送信後検証 + needs_attention + `run.py resolve`
- §4-D: 5 分毎ハートビート
- §4-E: `notify.py`（Slack incoming webhook）
- §4-F: `sender_brief.yaml`
- §4-G: `openclaw cron` でのスケジューリング例

---

## 5. SKILL.md 強化（v4 追加分）

両 SKILL.md に v3 で追記済のセクションに加えて:

1. **Draft model: Opus 4.7（v4）** — 「`stage_draft` および `_refine_draft` は Opus 4.7。`config.yaml model.name` を `claude-cli/claude-opus-4-7` に設定すること」
2. **Enrich research flow (jp-form 専用)** — 「enrich の前に Opus エージェントが各社の PR TIMES / IR を WebSearch で集めて direct_signals を埋める手順」（§11-B-2）
3. **Form scope rules（jp-form 専用）** — 「verify と form_fields は『textarea を含む最大の form』に限定する」（§11-A-2）
4. **`run.py resolve --action proceed` の使い方** — 「reCAPTCHA / confirm 待ちで Slack エスカレーションされたら、ユーザーの『進めて』を受けてエージェントが resolve」（§11-A-6）

Slack トリガー表に追記:

| ユーザー発話 | エージェントの行動 |
|---|---|
| 「丁寧モードで draft」/「refine ありで」 | `run.py draft --refine`（Opus 2 パス） |
| 「リサーチしてから draft」 | enrich-research フェーズを先に実行 → 通常 enrich → draft |
| 「<会社>進めて」（reCAPTCHA/confirm 待ち中） | `run.py resolve --target-id X --action proceed` |
| 「verify 緩め」 | `run.py send --ids N --verify-strict false`（成功マーカーのみで OK 判定） |

---

## 6. 受け入れ条件（v4 追加分のみ。v3 のものは既に通過済の前提）

11. **draft の既定モデルが Opus 4.7 であること**: `config.yaml model.name` の既定値が `claude-cli/claude-opus-4-7`、かつ `stage_draft` が config から model を読み込んでいる（ハードコード無し）
12. **verify 順序修正テスト**: `has_success_keyword=true` かつ `url_success=true` の入力で、サンクスページに無関係 required が残っていても `status="ok"` を返すこと（§11-A-1）
13. **char_limit ハード強制テスト**: Opus が char_limit を超える本文を返したケースをモックし、`stage_draft` が **自動的に圧縮リファインを 1 回回す**こと。最終出力が char_limit 以内に収まることを検証
14. **フォームスコープテスト**: ダミー HTML に「ログインフォーム」と「お問い合わせフォーム」が併存する場合、`_FORM_FIELDS_JS` が textarea を含む後者だけを返すこと
15. **`run.py resolve --action proceed` テスト**: needs_attention にある target_id を引数で受けて、保留中ブラウザに対し「次の submit ボタンをクリックする」操作を再開すること
16. **`input()` ハング撲滅テスト**: `stage_send(mode="interactive")` が stdin に依存せず、reCAPTCHA 検出時 / confirm flow 時に **`notify.post()` を呼んで return する**こと（`input()` への direct call を grep で 0 件）

---

## 7. やらないでほしいこと

- **slack_bolt や Socket Mode の自作受信ワーカー**。OpenClaw Slack plugin が双方向会話を担当
- **list 生成のための独立 LLM CLI**
- **新規スキル `facebook-outreach` / `x-outreach` の作成**
- **既存 JSONL のカラム削除・リネーム**
- **`prompts/system_persona.md` の既存セクション書き換え**。末尾追記のみ可
- **`_llm_analyze_form` を Opus に切り替えること**。Sonnet 維持
- **`verify.py` 内で LLM を呼ぶこと**
- **`sent_history.jsonl` / `skip_history.jsonl` の上書き**。append only
- **`config.yaml`（個人情報入り）の Git コミット**
- **`stage_send` 内に `input()` を新規追加すること**。Slack エスカレーション経由に統一

---

## 8. 作業順序（v4 追加分）

v4 は **既存 v3 実装の上に積む差分作業**:

1. **§11-A-1**: verify.py の判定順序バグ修正（30 分、最優先）
2. **§11-B-1**: char_limit ハード強制 + Opus 切替 + form maxlength 反映（1.5 時間）
3. **§11-A-2**: フォームスコープ絞り込み（`_FORM_FIELDS_JS` と `SCAN_REQUIRED_JS` の両方を直す）（30 分）
4. **§11-A-6**: `input()` 撲滅 + `resolve --action proceed` 追加（1 時間）
5. **§11-B-2**: enrich-research を SKILL.md に追記（30 分、Python 変更不要）
6. **§11-A-4**: ユニーク CSS セレクタ抽出（1-2 時間）
7. **§11-B-5**: refine 既定 ON フラグ + Slack トリガー追加（30 分）
8. **§11-A-3 軽量版**: フィールド適用後の再スキャン（2-3 時間）
9. **§11-A-5**: 郵便番号オートコンプリート対策（30 分）
10. **§11-B-3 / B-4**: examples / system persona 強化（1 時間）
11. **§11-B-6**: Opus 品質ゲートを SKILL.md に追記（30 分）

各ステップで **コミット分割**。1 PR ではなく段階レビュー。

---

## 9. 仕様で迷ったときの優先順位

1. **OpenClaw エージェントが今やっているフローを壊さない** > その他
2. **モデル選定**:
   - 判断・会話・調査・**長文の個別化された文章生成** → Opus（エージェント or `oc_infer` 経由）
   - 構造変換（DOM → JSON、フォームプラン）→ Sonnet on `oc_infer`
   - 検出・パース・履歴操作 → 純 Python
3. **Slack 投稿経路**: 会話的応答は OpenClaw plugin、状況通知は webhook
4. **新規 CLI vs SKILL.md 追記**: SKILL.md 追記を優先
5. **パフォーマンス vs シンプルさ**: シンプルさ優先

---

## 10. コスト見立て（v4 構成）

| 単位 | モデル | 想定回数/月 | 概算コスト |
|---|---|---|---|
| Opus エージェントの 1 会話セッション | Opus 4.7 | 数十〜100 | サブスク routing 内で吸収 |
| **`run.py draft` 1 件（v4 で Opus 化）** | **Opus 4.7（cached）** | **100〜500** | **~$0.04/件**（Sonnet 比 ~8 倍） |
| **`_refine_draft` 1 件**（refine 有効時） | **Opus 4.7** | **100〜300** | **~$0.04/件** |
| `_llm_analyze_form` 1 件 | Sonnet 4.6 | 100〜500 | ~$0.003/件 |
| `verify.py` 1 件 | なし | 同上 | $0 |
| ハートビート 1 件 | なし | 数十〜数百 | $0 |

合計の支配項は **`run.py draft` の Opus 呼び出し**。100 件 → ~$4-8/月、500 件 → ~$20-40/月。Doorman の volume では月数十ドル以内。

**コスト最適化メモ:**
- prompt cache は Opus でも有効。`prompts/system_persona.md` を byte-stable に保つことで 90%+ キャッシュヒット維持
- 安く回したい場合は `config.yaml model.name` を Sonnet に戻せば即座にコスト 1/8 へ復帰

---

## 11. v4 改修内容: jp-form-outreach の送信成功率と品質改善

**背景**: 実データ分析（11 件のドラフト、12 件の skip、1 件の needs_attention 誤検知）から以下が判明:

- 送信 2 件 / skip 10 件 / 誤検知 1 件（成功率が低い）
- **生成済みドラフト 11 件中 10 件が char_limit を超過**（400 字制限に対し 449〜566 字）
- **direct_signals が 10/11 件で空**（パーソナライズが薄い）
- 自己紹介段が 11 件中ほぼ全件で同一文

これに対する 12 件の改修を以下に列挙する。

### 11-A. 送信成功率を上げる（フォーム送信ハードル対策）

#### A-1. verify の判定順序バグ修正（最優先・30 分）

**現状**: `_outreach_core/verify.py` の jp_form セクションが以下の順:
```
error keyword → required scan → plan gaps → ★unresolved fields があれば needs_attention★ → success keyword → ok
```

これにより、サンクスページに `has_success_keyword=true`、`url_success=true` が出ているのに、ヘッダーのログインフォーム（`data[User][email]` 等）の required を拾って `needs_attention` を吐いてしまう（実データ nativecamp ケース）。

**修正**: 順序を変える:
```
error keyword          → needs_attention（既存）
★success keyword + url_success のセット → 即 ok を返す（新設）★
required scan          → unresolved_fields に追加
plan gaps              → unresolved_fields に追加
unresolved があれば    → needs_attention
それ以外 / 部分一致     → uncertain
```

**実装場所**: `_outreach_core/verify.py:verify_send_completed()` の jp_form 分岐。

**ガード:** success マーカーの「強い」シグナルを `has_success_keyword AND (url_success OR error_keyword_absent)` の AND 条件で判定。片方だけで ok を返さない（誤陽性回避）。

#### A-2. フォームスコープを 1 つに絞る（30 分）

**現状**: `_FORM_FIELDS_JS` と `SCAN_REQUIRED_JS` がページ全体の `[required]` を走査するため、サンクスページのログインフォーム / 検索フォーム / メルマガ登録の required まで拾う。

**修正**: 共通の「対象 form 特定」ヘルパー JS を作る:

```js
const _PICK_TARGET_FORM = () => {
  const forms = [...document.querySelectorAll('form')];
  const withTextarea = forms.filter(f => f.querySelector('textarea'));
  withTextarea.sort((a, b) =>
    b.querySelectorAll('input,select,textarea').length -
    a.querySelectorAll('input,select,textarea').length);
  return withTextarea[0] || forms[0] || document;
};
```

`_FORM_FIELDS_JS` と `SCAN_REQUIRED_JS` の両方で、root を `document` ではなく `_PICK_TARGET_FORM()` に置き換える。

**注意**: 確認画面では textarea が消えていることがあるので、`enriched.jsonl` に enrich 時の form の **CSS path** を保存し、verify 時はそれを優先的に探す。見つからなければ heuristics にフォールバック。

#### A-3. 動的に増える required フィールドへの対応（軽量版・2-3 時間）

**現状**: enrich 時の 1 ショット form_fields だけが plan の入力源。「法人 / 個人」ラジオを選んだ後に現れる会社名 input は plan に入らない。

**修正（軽量版）**: `fill_form_with_plan` の各フィールド適用後に **400ms 待機 + 軽量 required スキャン**を挟む。新規 required が現れたら:
1. plan に既にあれば実行
2. plan に無ければ `needs_attention.jsonl` に保留 + Slack 通知（Opus エージェントが値を聞く）

`_outreach_core/verify.py` に `scan_new_required_after_fill(targetForm)` を追加。

**重量版（後日）**: `--iterative-fill` フラグで「1 フィールド → 再スナップショット → LLM 再 plan」のループを実装。Opus エージェントに「このフォーム特殊だから 1 ステップずつ」と頼む形。今回はスコープ外。

#### A-4. ユニーク CSS セレクタ抽出（1-2 時間）

**現状**: `_apply_field_action` は `[name="X"]` または `#X` でしか要素を引けない。`name`/`id` が両方無い input は `"name": ""` で記録され、適用時に壊れる。

**修正**: `_FORM_FIELDS_JS` 内で **常に安定セレクタを返す**:

```js
function getStableSelector(el, root) {
  if (el.name) return `[name="${CSS.escape(el.name)}"]`;
  if (el.id) return `#${CSS.escape(el.id)}`;
  if (el.dataset?.qa) return `[data-qa="${el.dataset.qa}"]`;
  // 最後の手段: root 起点の構造パス
  const path = [];
  let cur = el;
  while (cur && cur !== root && cur !== document.body) {
    let part = cur.tagName.toLowerCase();
    const sibs = [...(cur.parentElement?.children || [])]
                  .filter(s => s.tagName === cur.tagName);
    if (sibs.length > 1) part += `:nth-of-type(${sibs.indexOf(cur) + 1})`;
    path.unshift(part);
    cur = cur.parentElement;
  }
  return path.join(' > ');
}
```

各 input/textarea/select エントリに `"selector": "..."` フィールドを追加。`_apply_field_action` 内で `findEl()` をこの selector で呼ぶように書き換える。

**互換性**: 既存の `name` / `id` ベースの呼び出しは fallback として残す。

#### A-5. 郵便番号オートコンプリート対策（30 分）

**現状**: `_FORM_ANALYZER_PROMPT_TEMPLATE` の Rule 22 で warning は出すが、実行順は強制しない。郵便番号入力で住所が自動補完されて入力済みの値を上書きするケースが残る。

**修正**: `fill_form_with_plan` の冒頭で plan を sort:

```python
def _postal_priority(field):
    name = (field.get("name") or "").lower()
    label = (field.get("label") or "").lower()
    # 郵便番号系を最後に
    if any(k in name + label for k in ["postal", "zip", "〒", "郵便番号"]):
        return 1
    # 住所系を先に
    if any(k in name + label for k in ["address", "addr", "都道府県", "市区", "住所", "番地"]):
        return -1
    return 0

plan["fields"] = sorted(plan["fields"], key=_postal_priority)
```

#### A-6. `input()` を撲滅して Slack エスカレーションに置換（1 時間）

**現状**: `stage_send` 内に terminal `input("y/N")` が 2 箇所（reCAPTCHA 検出時、confirm flow 中間）。**OpenClaw が起動した場合 stdin が無いのでハング**する。

**修正**:
1. `input()` 呼び出しを **完全に削除**
2. reCAPTCHA 検出時 / confirm flow 中間で待機が必要な場合は:
   - `needs_attention.jsonl` に `"reason": "awaiting_user_proceed"`、`"action_needed": "proceed"` で記録
   - `notify.post(f"⚠️ {name} で reCAPTCHA / confirm 待ちです。Slack で『{id}番進めて』と返してください", level="warn")`
   - `return` で関数を抜ける（ブラウザは開いたまま）
3. `run.py resolve` に **`--action proceed`** フラグを追加:
   - `needs_attention` のエントリを引いて、保留中ブラウザに対し次の操作（CAPTCHA 後の送信ボタンクリック / 確認画面での送信クリック）を再開
   - 成功したら `close_needs_attention()` を呼んで sent_history に記録

**設計**: SKILL.md の Slack トリガー表に追記:

```markdown
| 「<会社名>進めて」/「<id>進めて」 | エージェントは needs_attention.jsonl で
  該当 target_id を特定 → `run.py resolve --target-id X --action proceed` を実行 |
```

### 11-B. ドラフト品質を上げる

#### B-1. char_limit ハード強制 + Opus 切替（1.5 時間、最重要）

**現状**: 実データで 10/11 件が char_limit を 12〜39% 超過。送信時にフォームの maxlength で **本文末尾（締めの「何卒よろしくお願い申し上げます」+ カレンダー URL）が切られる致命的事故**。

**修正の組み合わせ**:

1. **モデルを Opus 4.7 に切替**（§3-8）:
   - `linkedin-outreach/config.yaml` / `jp-form-outreach/config.yaml` の `model.name` を `claude-cli/claude-opus-4-7` に変更
   - `config.example.yaml` 側も同じく更新
   - **`_llm_analyze_form` 専用に別キーを用意**:
     ```yaml
     model:
       name: "claude-cli/claude-opus-4-7"      # draft 用
       form_analyzer_name: "claude-cli/claude-sonnet-4-6"  # _llm_analyze_form 用
     ```
   - `_llm_analyze_form` 内で `config.get("model", {}).get("form_analyzer_name") or DEFAULT_FORM_MODEL` を読む

2. **Python 側でハード強制**: `_outreach_core/draft.py:stage_draft` に「生成後の本文を len で計測 → over なら自動圧縮リファインを 1 回回す」を実装:

   ```python
   def _enforce_char_limit(target, draft, config, max_chars):
       body = draft.get("body") or ""
       if len(body) <= max_chars:
           return draft
       compress_prompt = f"""
       以下のドラフトは {len(body)} 字で上限 {max_chars} 字を超えています。
       構造（固有事実→自己紹介→CTA→締め+URL）を保ったまま
       {max_chars} 字以内に圧縮してください。

       ## 原文
       {body}

       ## 出力（厳格 JSON）
       {{"subject": "...", "body": "<= {max_chars} chars>"}}
       """
       model = config.get("model", {}).get("name")  # Opus
       res = oc_infer(compress_prompt, model=model)
       refined = extract_first_json(res or "")
       if refined and len(refined.get("body", "")) <= max_chars:
           return refined
       # フォールバック: 単純切り詰め（CTA/URL 末尾保持）
       return _hard_truncate(draft, max_chars)
   ```

   `_hard_truncate` は本文末尾の `カレンダー：...` 行を必ず保持し、その上で 中盤を削る。

3. **char_limit を実態に合わせる**:
   - `_FORM_FIELDS_JS` が既に各 textarea の `max_length` を捕捉している。これを `enriched.jsonl` の `form_fields.textareas[].max_length` から取り出す
   - `stage_draft` での max_chars 解決順:
     ```
     enriched.form_fields.textareas[main].max_length
       OR targets.yaml の char_limit
       OR config.model.max_chars
       OR 400 (デフォルト)
     ```
   - これで、実際に 2000 字を受け付ける北の達人みたいなフォームでは伸び伸び書けるようになる

#### B-2. enrich-research フェーズの新設（30 分、Python 変更なし）

**現状**: jp-form の enrich は `form_fields` の DOM 構造しか取らない。`direct_signals` の自動充填がされず、targets.yaml に書き忘れたら空のまま。実データで 10/11 件が空。

**修正**: `SKILL.md` に **新セクション**を追加（Python 変更不要、エージェントの手順として定義）:

```markdown
## Enrich research flow (jp-form, agent-led, Opus 4.7 前提)

`run.py enrich`（form 構造取得）を走らせる **前に**、エージェントが各社の
コンテキスト情報を集めて targets.yaml の direct_signals と hook_context を
更新する。

エージェントの手順（社ごと）:

1. WebSearch:
   - "<会社名> プレスリリース 2025" "<会社名> 2026"
   - "<会社名> IR 売上"
   - "<会社名> 新サービス"
2. 上位 3-5 件のニュースから「数字つきの固有事実」を抽出
3. 売上 / 会員数 / 店舗数 / 直近 M&A / 新規事業 / 上場 等を direct_signals に
4. hook_context を 250-400 字に書き直し、以下 3 軸を含める:
   - 事業構造の特徴（toB/toC、サブスク有無、FC/直営、二重顧客モデルなど）
   - CRM の手前にある具体的な課題仮説（離脱率、休眠、加盟店間統合など）
   - LINE/CRM が刺さりそうな根拠
5. `python3 -m _outreach_core.helpers.append_targets` で targets.yaml を上書き更新
6. 全社終わったら通常の `run.py enrich` を走らせる
```

これで draft の入力品質が大きく上がる。**追加 API 呼び出しゼロ**（エージェント context で完結）。

#### B-3. 自己紹介段のバリエーション増（30 分）

**現状**: 全 11 件の自己紹介段が「株式会社トラーナの志田と申します。オンライン診療 MDOnline ... ソニーグローバルエデュケーション ...」のほぼコピペ。

**修正**: `prompts/examples.md` の末尾に **新セクション「自己紹介 5 バリエーション」**を追加:

```markdown
## 自己紹介段の差し替えバリエーション

同じ事実（MDOnline + ソニーグローバルエデュケーション）でも、相手企業の
文脈に応じて自己紹介の角度を変える。以下 5 パターンをローテーション:

### V1. 医療領域起点（ヘルスケア宛て）
「医療領域 MDOnline を立ち上げ、患者と家族の二重顧客への接点設計を
してまいりました者として」

### V2. こども × サブスク起点（教育/D2C 宛て）
「ソニーグローバルエデュケーションでこども向け定期サブスクの CRM 設計に
携わってまいりました者として」

### V3. 中堅企業 CRM 着眼起点（FC 型・本部運用宛て）
「中堅企業の本部主導 CRM が未整備という構造を、複数業界で見てきました者として」

### V4. 立場控えめ起点（大手宛て）
「弊社のような小さな組織から失礼します。御社の規模感には及ばないものの、
継続率・LTV の設計を業界横断でしてきた立場として」

### V5. 事業者起点（似た事業構造の企業宛て）
「私自身もサブスク事業の運営側におりました経験から、御社の…」

連続 2 件で同じ V を使うのは NG。
```

#### B-4. 冒頭型ローテーション（30 分）

**現状**: 5/11 件が「突然のご連絡で恐れ入ります」開き。残りも「[年月] の [出来事] を拝読いたしました」型でほぼ揃う。

**修正**: `prompts/system_persona.md` の **末尾に追記**（既存セクションは触らない）:

```markdown
## v4 追加: 冒頭型ローテーション規則

冒頭は以下 4 型から選ぶ。**直近 2 件で同じ型は使わない**:

1. **挨拶 → 固有事実型**「突然のご連絡で恐れ入ります。[事実] を拝読いたしました」
2. **直接固有事実型**（挨拶を省く）「[年月] の [出来事]、[所感]」
3. **質問起点型**「[業界トピック] について、貴社の [仮説] は…」
4. **共感ブリッジ型**「[業界の構造的痛み] を経験された企業として、…」
```

`_refine_draft` のクリティーク checklist に「冒頭型がどれか、直近と被っていないか」を追加。

#### B-5. refine 既定 ON のオプション化（30 分）

**現状**: `stage_draft(refine=False)` が既定。クリティーク機構は実装済だが、CLI フラグで明示しないと有効にならない。

**修正**:
1. `run.py campaign` と `run.py draft` に `--refine` フラグを追加
2. `SKILL.md` の Slack トリガー表に「丁寧モードで draft」「refine ありで」のマッピングを追加
3. **新フラグ `--refine-only-if-low-quality`**（オプション）: ドラフト生成後にエージェントが目視で品質スコアを付け、低品質のものだけ refine を回す（コスト削減）

#### B-6. Opus エージェントによる品質ゲート（30 分、SKILL.md のみ）

**現状**: targets.yaml → 一気に全件 draft → preview。各社の戦略はエージェントの目を通らない。

**修正**: `SKILL.md` に **新セクション「Draft quality gate (agent-led)」**を追加:

```markdown
## Draft quality gate

`run.py draft` 完了後、preview の前に Opus エージェントが drafts.jsonl を
読み、各 draft を以下の観点で 3 段階評価（high / mid / low）:

  1. 冒頭が型 1-4 のどれか、直近と被っていないか
  2. 自己紹介段の自己重複率（前 3 件と比較）
  3. 数字／固有事実が target の業界構造に接続されているか
  4. CTA に OUT or 具体的価値提供が含まれているか
  5. 本文長が char_limit 内に収まっているか

low 評価の draft だけ `run.py draft --refine --ids X,Y,Z` で再生成。
mid 以上はそのまま preview へ。
```

Python 側に追加実装は不要（エージェントの判断のみ）。

---

## 13. v4 追加: ログ抽出と品質改善レポート

**目的**: ドラフト品質とフォーム送信のスムーズさを継続的に磨き上げるため、各段階の事実を構造化ログとして残し、Slack 経由で Opus エージェントが要約できるようにする。

**主な見手**: Slack で会話している Opus 4.7 エージェント。「今週の手直しポイント教えて」と聞いたら、エージェントが report CLI を叩いて自然文で返す。

**ログの深さ**: フルトレース。DOM スナップショット、LLM プロンプト / レスポンス、fill_plan、verify evidence をすべて保存する。

---

### 13-A. ストレージ設計

**2 層構造:**

```
data/
├── events.jsonl                              # 軽量イベントログ（クエリ用）
├── traces/
│   └── <run_id>/<target_id>/
│       ├── draft_prompt.json                 # 送った LLM プロンプト全文
│       ├── draft_response.json               # LLM の生レスポンス
│       ├── refine_prompt.json     (任意)
│       ├── refine_response.json   (任意)
│       ├── form_snapshot_pre.txt             # 入力前 DOM
│       ├── fill_plan.json                    # LLM が返した plan
│       ├── fill_diagnostics.json             # 各フィールドの ok/error
│       ├── form_snapshot_post.txt            # 送信後 DOM
│       └── verify_evidence.json              # verify の evidence 全文
└── current_task.jsonl                        # 既存（ハートビート用、§4-D）
```

- `events.jsonl` は **append-only、1 行 1 イベント、機械可読**
- `traces/` は **重いデータの置き場**。イベントから `trace_dir` フィールドで参照
- `run_id` は `YYYYMMDD-HHMMSS` のタイムスタンプ。1 回の campaign 実行で 1 個

### 13-B. イベントスキーマ

```json
{
  "v": 1,
  "ts": "2026-05-26T09:33:39.872Z",
  "run_id": "20260526-093320",
  "skill": "jp-form-outreach",
  "kind": "send.verify.completed",
  "stage": "send",
  "target_id": "nativecamp",
  "outcome": "needs_attention",
  "payload": {
    "status": "needs_attention",
    "reason": "想定外の必須入力項目 [text: ..]",
    "unresolved_count": 6
  },
  "trace_dir": "data/traces/20260526-093320/nativecamp/"
}
```

**必須フィールド:** `v` / `ts` / `kind` / `stage` / `skill`
**オプショナル:** `target_id` / `run_id` / `outcome` / `payload` / `trace_dir`

### 13-C. emit するイベント種別

**ドラフト側 (`stage_draft` / `_refine_draft`)**

| kind | 出すタイミング | payload の主項目 |
|---|---|---|
| `enrich.research.completed` | enrich-research が WebSearch を終えた | `signals_count`, `hook_context_chars` |
| `enrich.form.completed` | enrich が form_fields 取得を終えた | `field_count`, `has_captcha`, `detected_max_chars` |
| `draft.requested` | LLM 呼び出し前 | `model`, `max_chars`, `cache_estimated_hit` |
| `draft.emitted` | LLM が返した | `body_chars`, `opener_type` (1-4), `self_intro_variant` (V1-V5), `subject` |
| `draft.skipped` | LLM が SKIP を返した | `reason` |
| `draft.over_limit` | body > char_limit | `actual`, `limit`, `delta_pct` |
| `draft.compressed` | 自動圧縮実行後 | `new_chars`, `original_chars` |
| `refine.applied` | refine 実行後 | `critique_summary`, `changed_opener`, `changed_self_intro` |
| `quality.gated` | 品質ゲート判定後 | `score` (high/mid/low), `reasons` |

**送信側 (`stage_send` 内)**

| kind | 出すタイミング | payload の主項目 |
|---|---|---|
| `send.opened` | form_url オープン後 | `url`, `time_ms` |
| `send.entry_clicked` | entry_click_text 適用後 | `text`, `success` |
| `send.plan.generated` | `_llm_analyze_form` 戻り | `field_count`, `checkboxes_count`, `flow` |
| `send.fill.applied` | `fill_form_with_plan` 戻り | `filled`, `errors`, `skipped` |
| `send.fill.dynamic_required` | 動的 required を検知 | `field_label`, `field_type` |
| `send.button.clicked` | 1st submit ボタン押下 | `pattern_matched`, `text` |
| `send.confirm.reached` | confirm 画面到達 | `wait_user_ms` |
| `send.final.clicked` | 最終 submit 押下 | `pattern_matched`, `text` |
| `send.verify.completed` | verify 終了 | `status` (ok/uncertain/needs_attention), `reason`, `evidence_keys` |
| `send.escalated` | needs_attention 作成 | `field_count_unresolved`, `slack_posted` |
| `send.resolved` | `resolve --action proceed` 完了 | `previous_status`, `new_status` |

**システム**

| kind | 出すタイミング | payload |
|---|---|---|
| `heartbeat.tick` | progress.py の 5 分毎 | `current`, `total`, `elapsed_sec` |
| `slack.notified` | `notify.post()` 実行後 | `level`, `text_hash`, `ok` |

### 13-D. 実装方針

1. **新モジュール `_outreach_core/events.py`** を作る:

   ```python
   def emit(kind: str, *, stage: str, target_id: str | None = None,
            outcome: str | None = None, payload: dict | None = None,
            trace_dir: Path | None = None) -> None:
       """events.jsonl に 1 行 append。失敗しても呼び出し元を落とさない。"""

   def trace_dir_for(run_id: str, target_id: str) -> Path:
       """data/traces/<run_id>/<target_id>/ を mkdir -p して返す"""

   def dump_trace(trace_dir: Path, name: str, obj: Any) -> None:
       """JSON or テキストを trace_dir に書く"""
   ```

2. **既存 `_outreach_core/progress.py` は events.py を呼ぶように内部統合**。`current_task.jsonl` への書き込みは互換維持のために残すが、同じ内容を events.jsonl にも emit する。

3. **emit 呼び出しを stage 関数の出入り口に挿入**。たとえば `stage_draft` 内:

   ```python
   trace_dir = trace_dir_for(run_id, target_id)
   events.emit("draft.requested", stage="draft", target_id=target_id,
               payload={"model": model, "max_chars": max_chars})
   dump_trace(trace_dir, "draft_prompt.json", {"system": system_block, "user": user_block})
   response = oc_infer(...)
   dump_trace(trace_dir, "draft_response.json", response)
   ...
   events.emit("draft.emitted", stage="draft", target_id=target_id,
               outcome="ok", payload={"body_chars": len(body), ...},
               trace_dir=str(trace_dir))
   ```

4. **`run_id` の生成と伝播**:
   - `stage_campaign` の冒頭で `run_id = datetime.utcnow().strftime("%Y%m%d-%H%M%S")` を生成
   - 各 stage 関数の引数に `run_id: str | None = None` を追加。`None` なら "ad-hoc-<ts>" で埋める
   - SKILL.md にも記載

5. **プライバシー**:
   - sender PII (`email`, `phone`, `postal_code`, `name_furigana`, `full_address`) は **traces には保存しない**
   - `dump_trace` 側でセンダ辞書を受けたら `<sender.email>` 等のプレースホルダに置換するヘルパー `redact_sender(obj, sender_config)` を経由させる
   - LLM プロンプト内の sender 情報は **system block 側にあるので元々 traces には sender 個別値が混入しない**設計が望ましい。逆に user block には target データのみ。一度コードを検査して、redact が必要な箇所をすべて洗い出す

### 13-E. report CLI

**`_outreach_core/helpers/report.py`** を新設。サブコマンド:

```bash
# 直近 7 日のドラフト品質サマリ
python3 -m _outreach_core.helpers.report draft-quality --since 7d [--skill jp-form-outreach]

# 送信ファネル（どこで詰まったか）
python3 -m _outreach_core.helpers.report send-funnel --since 7d

# needs_attention の集計
python3 -m _outreach_core.helpers.report needs-attention --since 7d

# 特定 target のフルトレース
python3 -m _outreach_core.helpers.report inspect --target-id nativecamp [--run-id 20260526-093320]
```

**出力フォーマット**: Markdown + JSON (Slack で読みやすい)。

各 CLI は events.jsonl を **行単位で読み**（pandas 等は使わない、依存最小）、辞書集計するだけ。

**`draft-quality` の出力例:**

```markdown
# Draft Quality Report — 2026-05-19 〜 2026-05-26

drafts: 23 (15 ok / 6 skipped / 2 errored)

## char_limit compliance
- under limit: 18 / 23 (78%)
- over limit (auto-compressed): 4 / 23 (17%)
- over limit (compress failed → truncated): 1 / 23 (4%)

## opener type distribution
- 型1 挨拶→固有事実: 12 (52%)  ← 偏り気味、要バリエーション
- 型2 直接固有事実: 8 (35%)
- 型3 質問起点: 2 (9%)
- 型4 共感ブリッジ: 1 (4%)  ← もっと使ってよい

## self-intro variant distribution
- V1 医療: 9, V2 こども×サブスク: 11, V3 中堅CRM: 2, V4 立場控えめ: 1, V5 事業者: 0

## refine impact
- refine ON: 8 drafts. avg opener change: 5/8, self-intro change: 6/8
- 平均字数変化: -42 chars (refine で短くなる傾向)

## SKIP reasons (top 3)
- INSUFFICIENT_DATA hook_context 不足: 4
- category=b2c_only: 1
- 他: 1
```

**`send-funnel` の出力例:**

```markdown
# Send Funnel — 2026-05-19 〜 2026-05-26

opened:             12
entry_clicked:      8 (4 forms required no entry click)
plan.generated:     12 (3 plan_warnings 出ました: 郵便番号順序 2件、不明型 1件)
fill.applied:       12 (avg 8.5 fields filled, avg 1.2 errors)
fill.dynamic_req:   3 ← 動的フィールド検知。後述
button.clicked:     11 (1 件で pattern 不一致 → ストアカ、確認画面の文言が「内容を確認」のみ)
confirm.reached:    7 (flow=confirm 全件)
final.clicked:      7
verify.completed:   12 → ok=8 / uncertain=2 / needs_attention=2

## drop-off
- opened → fill.applied: 100%
- fill.applied → button.clicked: 92% (1件 pattern miss)
- → verify ok: 67%

## fill.dynamic_required の内訳
- 「会社名」が「法人」選択後に出現: 2件（パターン化候補）
- 「お問い合わせ詳細」がカテゴリ選択後に出現: 1件
```

**`needs-attention` の出力例:**

```markdown
# needs_attention Report — open: 3 / closed: 4

## open
- nativecamp / 2026-05-26 / 「ログインフォーム」誤検出疑い → verify A-1 修正後の再 verify 推奨
- straca / 2026-05-22 / confirm ボタン pattern 不一致 → resolve --action proceed 待ち
- bridal_co / 2026-05-20 / reCAPTCHA v2 出現 → 手動対応待ち

## 最頻出 unresolved field
- 「業界」プルダウン: 4社（共通の overrides を targets.yaml に追加すべき）
- 「お問い合わせ種別」ラジオ: 3社
```

### 13-F. Slack トリガー

両 SKILL.md の Slack トリガー表に追記:

| ユーザー発話 | エージェントの行動 |
|---|---|
| 「品質ポイント教えて」/「draft 品質どう？」 | `report draft-quality --since 7d` を実行し、要約を Slack に返す |
| 「送信ファネル見せて」/「フォームどこで詰まってる？」 | `report send-funnel --since 7d` |
| 「needs_attention まとめて」 | `report needs-attention` |
| 「<会社> のトレース見たい」 | `report inspect --target-id X` |
| 「今週の改善ポイント 3 つ」 | 3 つの report をマージしてエージェントが Top-3 を抽出 |

### 13-G. リテンション

- 既定: **すべて保持**（容量は volume が低いうちは問題なし）
- `report prune --keep 90d` で 90 日より古い `events.jsonl` エントリと `traces/<run_id>/` を削除（後日実装）
- `.gitignore` に `data/events.jsonl` と `data/traces/` を追加

### 13-H. 受け入れ条件（v4 §6 に追加）

17. `events.jsonl` のスキーマが §13-B 通り（v / ts / kind / stage 必須、ヘッダー無し JSONL）
18. `stage_draft` 実行で `draft.requested` → `draft.emitted` (or `draft.skipped`) のペアが必ず emit されること
19. `stage_send` 実行で `send.opened` → ... → `send.verify.completed` のチェーンが順序通り emit されること
20. trace_dir に **sender PII が含まれていない**こと（`redact_sender` 通過確認テスト）
21. `python3 -m _outreach_core.helpers.report draft-quality --since 7d` が 0 件でもエラーにならず空サマリを返すこと
22. `report inspect --target-id X` が trace_dir 内のファイル一覧と各ファイルの先頭 20 行を表示すること

### 13-I. 作業順序

1. `_outreach_core/events.py` を新設（emit / trace_dir_for / dump_trace / redact_sender）— 30 分
2. `stage_draft` / `_refine_draft` への emit 挿入 + traces 保存 — 1 時間
3. `stage_send` への emit 挿入 + traces 保存 — 1 時間
4. `progress.py` を events.py 経由に置き換え（互換維持）— 30 分
5. `_outreach_core/helpers/report.py` 実装（draft-quality / send-funnel / needs-attention / inspect の 4 サブコマンド）— 2 時間
6. SKILL.md に Slack トリガー追記 — 15 分
7. `.gitignore` 更新 — 5 分

**全体で 約 5 時間。** 最初のサンプルが events.jsonl に貯まったら、Slack で「品質ポイント教えて」と Opus に聞いて初回レポートを確認 → 必要なら kind を追加するイテレーション。

---

## 14. v4 追加: マルチ brief（人格）対応

### 14-A. 目的

1 つのリポジトリで **複数の送信者ブリーフ（人格）を切り替えて運用できる**ようにする:

- Brief 1: 「トラーナ — LINE×CRM コンサル（志田相談役）」
- Brief 2: 「CellCloud — 医療系 SaaS」
- Brief 3: 「個人事業として副業案件」 など

**新規セッション開始時、Opus エージェントは必ず以下 2 つを確認**する:
1. どの brief を使うか
2. どのチャネル（form / linkedin）を使うか

確認が済むまで `run.py` のサブコマンドは叩かない。

### 14-B. ファイル構成

```
~/.openclaw/skills/
├── briefs/
│   ├── README.md
│   ├── _active.txt                    # 既定 brief slug（1 行）
│   ├── _template.yaml                 # 新規作成時の雛形（コミット可）
│   ├── torana-line-crm.yaml           # 人格 1（移行後の旧 sender_brief 由来）
│   └── cellcloud-medical.yaml         # 人格 2（将来用）
├── sender_brief.yaml.deprecated       # ← 移行後はリネーム、参照禁止
├── jp-form-outreach/
│   ├── config.yaml                    # スキル固有設定（model, prompts, fill defaults）
│   ├── targets/
│   │   └── <brief_id>.yaml            # brief ごとに分割（既存 targets.yaml は torana-line-crm/ へ移行）
│   └── data/
│       └── briefs/
│           └── <brief_id>/
│               ├── leads.jsonl
│               ├── enriched.jsonl
│               ├── drafts.jsonl
│               ├── sent_history.jsonl
│               ├── skip_history.jsonl
│               ├── needs_attention.jsonl
│               ├── current_task.jsonl
│               ├── events.jsonl
│               ├── verify_snapshot_*.txt
│               ├── sample_form.txt
│               └── traces/
│                   └── <run_id>/<target_id>/...
└── linkedin-outreach/
    └── (同じ構造)
```

**核となる変更:**
- `sender_brief.yaml` は **廃止**、内容は `briefs/<id>.yaml` に統合
- `data/` 直下は使わず、必ず `data/briefs/<brief_id>/` 配下に書く
- `targets.yaml` は **brief 単位で分割**（人格が違えば狙う先も違う）
- skill 側 `config.yaml` に残るのは「prompts のパス」「model 設定」「フォーム固有 fill defaults」など、人格に依存しない要素のみ

### 14-C. Brief YAML スキーマ

```yaml
# briefs/torana-line-crm.yaml
brief:
  id: "torana-line-crm"
  display_name: "トラーナ — LINE×CRM コンサル"
  active_since: "2026-05-26"
  notes: |
    志田典道（相談役）のメイン業務。
    日本中堅企業の LINE×CRM 運用設計コンサル。

sender:
  name: "志田典道"
  name_kana: "シダノリミツ"
  name_furigana: "しだのりみつ"
  role: "相談役"
  company: "株式会社トラーナ"
  company_short: "トラーナ"
  email: "shida@torana.co.jp"
  phone: "09016501629"
  phone_hyphenated: "090-1650-1629"
  postal_code: "260-0003"
  postal_code_no_hyphen: "2600003"
  prefecture: "千葉県"
  city: "千葉市中央区"
  address_line: "鶴沢町20-16"
  building: "ユニバース千葉ビル1階"
  full_address: "千葉県千葉市中央区鶴沢町20-16 ユニバース千葉ビル1階"
  calendar_url: "https://tenbin.link/book/u-1302066f5d4f/torana-norimitsu-shida"
  founded_year_month: "2015年3月"
  annual_revenue_band: "5億円"
  employee_count_band: "10名"

product:
  name: "LINE公式アカウント運用×CRMコンサル"
  one_liner: "LINE公式アカウントを『売上ドライバー』として機能させるCRM運用設計支援"

problems_solved:
  - "..."

pitch:
  problem: |...
  solution: |...
  proof_points: [...]
  call_to_action: |...

target:
  industries: [...]
  size_band: "..."
  founding_year_sweet_spot: "1995-2012"
  decision_makers: [...]
  geo: "JP"
  must_have_signals: [...]
  must_not_signals: [...]

desired_channels:
  - "linkedin"
  - "jp_form"

personalization:
  must_reference: "..."
  avoid: [...]
  tone: "..."

# brief 固有の prompt override（任意。null は skill 既定を使う）
prompts_overrides:
  jp_form_system_persona: null    # 例: "prompts/personas/torana-line-crm/system_persona.md"
  jp_form_examples: null
  linkedin_system_persona: null

slack:
  incoming_webhook_url: ""
  channel_id: ""

heartbeat:
  interval_sec: 300
  enabled_for:
    - "send"
    - "enrich"

model:
  name: "claude-cli/claude-opus-4-7"
  form_analyzer_name: "claude-cli/claude-sonnet-4-6"
  max_chars: 400
  language: "ja"
```

### 14-D. マージ規則（更新）

```
briefs/<active>.yaml > skill_dir/config.yaml > defaults
```

`_outreach_core/config.py:load_merged_config(skill_dir, brief_id=None)` の動作:
1. `brief_id` が None なら `briefs/_active.txt` を読む
2. `briefs/<brief_id>.yaml` を読む
3. `skill_dir/config.yaml` を読む
4. deep merge: brief が勝つ
5. 結果を返す

**重要**: 既存の `sender_brief.yaml` を読む経路は **削除**。互換性のためのフォールバックも作らない（マイグレーションで一気に切り替える）。

### 14-E. セッション開始時の確認フロー（SKILL.md 追記）

両方の SKILL.md 冒頭近くに **新セクション**:

```markdown
## Session start: brief & channel confirmation (MANDATORY)

新規セッションで「アウトリーチ系」のリクエスト（list-build / campaign /
draft / send / preview など）を受けたら、agent は **データ生成・送信を
伴うアクション**を起動する前に、必ず以下 2 つを Slack で確認する:

### Step 1: brief 確認

  Slack 投稿例:
  ```
  📇 どの brief で進めますか？
    [既定] torana-line-crm — トラーナ LINE×CRM コンサル
    cellcloud-medical    — CellCloud 医療系 SaaS
  「torana で」「LINE×CRM で」と返してください。
  ```

  確認の手段:
  - `python3 -m _outreach_core.helpers.brief list` を bash で叩いて一覧取得
  - 1 件しか無い場合も「既定の torana-line-crm で進めます。よろしいですか？」と確認

### Step 2: channel 確認

  brief の `desired_channels` を Slack に出して選ばせる:
  ```
  チャネルは:
    [1] jp_form（フォーム送信）
    [2] linkedin（InMail）
    [両方] 並行で
  ```

### Step 3: 確定後の挙動

  ユーザー回答を受け取ったら:
  - `--brief <id>` を全ての run.py 呼び出しに付ける
  - 該当 channel の skill だけ起動
  - セッション中、再確認は不要（ユーザーが「brief 変えて」と明示しない限り）

### 確認を省略する場合

  以下の問い合わせ系コマンドは brief 確認なしで応答してよい:
  - 「進捗どう？」「sent_history 見せて」「needs_attention 一覧」
    → 全 brief の集計 or `_active.txt` の brief で応答
  - 「brief 一覧」「今 どの brief が active？」
```

### 14-F. CLI

新規モジュール `_outreach_core/helpers/brief.py`:

```bash
# 全 brief 一覧（id / display_name / 最終 send 日時）
python3 -m _outreach_core.helpers.brief list

# 1 件の詳細
python3 -m _outreach_core.helpers.brief show torana-line-crm

# 既定 brief を切替（briefs/_active.txt 上書き）
python3 -m _outreach_core.helpers.brief set-active cellcloud-medical

# 雛形からの新規作成（briefs/_template.yaml をコピーして開く）
python3 -m _outreach_core.helpers.brief new cellcloud-medical \
  --display-name "CellCloud 医療系"

# 既存単一 brief セットアップを brief 化（マイグレーション）
python3 -m _outreach_core.helpers.brief migrate \
  --from-sender-brief sender_brief.yaml \
  --from-config jp-form-outreach/config.yaml \
  --from-config linkedin-outreach/config.yaml \
  --to torana-line-crm \
  --display-name "トラーナ LINE×CRM"

# data/ 直下の旧ファイルを data/briefs/<id>/ に移動
python3 -m _outreach_core.helpers.brief migrate-data --brief torana-line-crm

# brief 削除（アーカイブとしてリネーム）
python3 -m _outreach_core.helpers.brief archive cellcloud-medical
# → briefs/cellcloud-medical.yaml.archived に rename
```

### 14-G. run.py への `--brief` フラグ追加

すべての subcommand に `--brief <id>` を追加:

```bash
# 既定 brief を使う（briefs/_active.txt）
python run.py campaign --clean

# 明示指定
python run.py campaign --brief torana-line-crm --clean

# 起動ログに brief を明示
[campaign] brief=torana-line-crm · skill=jp-form-outreach
```

`--brief` は **全 subcommand に必須**（既定値あり）:
- `campaign`, `bootstrap`, `enrich`, `draft`, `preview`, `send`, `resolve`, `mark-sent`, `history`

データパス解決:
- 旧: `DATA_DIR = SKILL_DIR / "data"`
- 新: `DATA_DIR = SKILL_DIR / "data" / "briefs" / brief_id`

`targets.yaml` パス解決:
- 新: `TARGETS_PATH = SKILL_DIR / "targets" / f"{brief_id}.yaml"`

### 14-H. 履歴の brief 単位独立（重要）

`_outreach_core/history.py` の関数を全て `brief_id` で分離:

```python
def sent_history_path(skill_dir: Path, brief_id: str) -> Path:
    return skill_dir / "data" / "briefs" / brief_id / "sent_history.jsonl"

def load_global_exclude_set(brief_id: str) -> set[str]:
    """この brief の send/skip 履歴を全 skill 横断で集める。
    他 brief の履歴は含めない（独立運用のため）。"""
    s = set()
    for skill_dir in SKILL_DIRS:
        s |= load_sent_set(sent_history_path(skill_dir, brief_id))
        s |= load_skip_set(skip_history_path(skill_dir, brief_id))
    return s
```

→ 同じ company に brief A と brief B からそれぞれ outreach できる。dedup は brief 内のみ。

### 14-I. プロンプトの brief 単位 override（任意）

brief 固有のトーン・サインオフが欲しい場合、`prompts_overrides` で skill 既定をオーバーライド:

```
~/.openclaw/skills/
├── jp-form-outreach/
│   ├── prompts/
│   │   ├── system_persona.md             # スキル既定
│   │   ├── examples.md
│   │   └── personas/                     # brief 固有
│   │       ├── torana-line-crm/
│   │       │   ├── system_persona.md     # 既定を完全置換
│   │       │   └── examples.md
│   │       └── cellcloud-medical/
│   │           └── system_persona.md
```

brief.yaml で:
```yaml
prompts_overrides:
  jp_form_system_persona: "prompts/personas/torana-line-crm/system_persona.md"
```

null（既定）なら skill 直下の `prompts/system_persona.md` を使う。

### 14-J. Slack トリガー追加

両 SKILL.md の Slack トリガー表に:

| ユーザー発話 | エージェントの行動 |
|---|---|
| 「brief 一覧」/「人格教えて」 | `brief list` を bash で実行、結果を整形して投稿 |
| 「<id> で」/「<display_name> で」 | このセッションでの brief を確定（_active.txt は変えない） |
| 「<id> を既定に」 | `brief set-active <id>` |
| 「<新id> 新規」 | `brief new <id>` を実行し、Slack で対話的にフィールドを埋める |
| 「今 どの brief？」 | active brief と現セッション選択を返す |
| 「<id> アーカイブ」 | `brief archive <id>` |

### 14-K. マイグレーション手順

既存の単一 brief セットアップ（今の Shida-san のもの）を `torana-line-crm` brief に移行:

```bash
# 1. brief を作成
python3 -m _outreach_core.helpers.brief migrate \
  --from-sender-brief sender_brief.yaml \
  --from-config jp-form-outreach/config.yaml \
  --from-config linkedin-outreach/config.yaml \
  --to torana-line-crm \
  --display-name "トラーナ LINE×CRM"

# 2. 既存 data/ を data/briefs/torana-line-crm/ に移動（両 skill とも）
python3 -m _outreach_core.helpers.brief migrate-data --brief torana-line-crm

# 3. targets.yaml / targets.csv を移動
mv jp-form-outreach/targets.yaml jp-form-outreach/targets/torana-line-crm.yaml
mv linkedin-outreach/targets.csv linkedin-outreach/targets/torana-line-crm.csv

# 4. 既定 brief を設定
echo "torana-line-crm" > briefs/_active.txt

# 5. 旧ファイルを deprecated 化
mv sender_brief.yaml sender_brief.yaml.deprecated

# 6. テスト
python3 -m _outreach_core.helpers.brief list
python3 jp-form-outreach/run.py preview --brief torana-line-crm --no-send
```

**マイグレーションは Cursor が冪等な migrate スクリプトとして実装**。複数回実行しても壊れない設計。

### 14-L. 受け入れ条件

23. `briefs/<id>.yaml` を作成・編集できる CLI（`brief new`, `brief show`, `brief list`）が動作する
24. 既存単一 brief セットアップを `torana-line-crm` brief に移行後、`jp-form-outreach/run.py preview --brief torana-line-crm` が **マイグレーション前と同じ drafts** を表示できる
25. 2 つめの brief（例: `test-brief`）を作って `campaign --brief test-brief` を回すと、`data/briefs/test-brief/` 配下に独立した sent_history / drafts が書かれる
26. 同じ `target_id` が `torana-line-crm` で sent でも、`test-brief` から見ると pending として扱われる
27. SKILL.md に Session start confirmation セクションがあり、新規セッションで Opus エージェントが必ず brief/channel を確認する流れになっている
28. `run.py` の全 subcommand に `--brief` フラグがあり、未指定なら `_active.txt` を読む
29. `_active.txt` が存在しないか、その内容が `briefs/` 配下に無い場合、明確なエラーメッセージで起動拒否
30. `sender_brief.yaml` への参照が **コードから完全削除**（grep で 0 件、ドキュメント側の言及のみ残る）

### 14-M. 作業順序（推奨）

1. `_outreach_core/helpers/brief.py` の CLI 雛形を作る（list / show / new / set-active）— 1 時間
2. `_outreach_core/config.py` を brief 対応に書き換え（`load_brief()`, `load_merged_config(skill_dir, brief_id)`）— 30 分
3. `_outreach_core/history.py` の関数に `brief_id` 引数を追加 — 30 分
4. `run.py`（両 skill）に `--brief` フラグ追加、データパス書き換え — 2 時間
5. マイグレーション CLI（`brief migrate`, `brief migrate-data`）実装 — 1 時間
6. SKILL.md に Session start confirmation 追記 — 30 分
7. テスト追加（受け入れ条件 23-30） — 1 時間
8. 実マイグレーション実行（torana-line-crm 移行）— 15 分
9. 旧 `sender_brief.yaml` 参照を全削除 — 15 分

**全体で 約 7 時間。** マイグレーション CLI を堅牢に作るのが肝。

### 14-N. やらないこと（再確認）

- **brief 間で履歴を共有しない**（独立運用が選定された決定事項）
- **brief 切替を会話途中で勝手にしない**（必ずユーザー確認）
- **brief を作る時に LLM で対話的に埋める機能は今回スコープ外**（テンプレ + 手動編集で十分）
- **brief を Web UI で管理する機能は今回スコープ外**（CLI + YAML 編集で十分）

---

## 15. v4 まとめ・チェックポイント（v3 §12 を統合）

### v4 で増えたモデル料金
- draft: Sonnet → **Opus 4.7** に切替（~8 倍のコスト、月数十ドル）
- `_llm_analyze_form`: Sonnet 維持
- refine（オプション）: Opus（手動 ON 時のみ）

### v4 で増えた機能
- ✅ verify の誤検知撲滅（A-1, A-2）
- ✅ char_limit ハード強制（B-1）
- ✅ `input()` 撲滅 + Slack エスカレーション統一（A-6）
- ✅ enrich-research フロー（B-2, SKILL.md のみ）
- ✅ ユニーク CSS セレクタ（A-4）
- ✅ 動的フィールド対応 軽量版（A-3）
- ✅ 郵便番号順序対策（A-5）
- ✅ 冒頭型 / 自己紹介バリエーション（B-3, B-4）
- ✅ refine フラグ + 品質ゲート（B-5, B-6）
- ✅ ログ抽出・レポート（§13）
- ✅ **マルチ brief（人格）対応（§14、新設）**

### v4 で **増えてない**ことの確認
- ❌ slack_bolt 自作（v2 で撤回済）
- ❌ list_builder CLI（v3 で撤回済）
- ❌ Facebook / X チャネル（スコープ外）
- ❌ verify.py の LLM 化（純 Python 維持）
- ❌ `_llm_analyze_form` の Opus 化（Sonnet 維持）
- ❌ brief 間の履歴共有（独立運用が決定）

---

以上。
