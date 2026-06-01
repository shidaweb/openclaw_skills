# CURSOR 指示書 v7 — 送信ボタン進行性 ＆ ドラフト堅牢化

> 正典は [`CURSOR_INSTRUCTIONS.md`](./CURSOR_INSTRUCTIONS.md)。本書はその差分指示（v7）。
> §3「守ってほしい不変項」は**全て継続**（6フェーズ名・JSONL名・append-only履歴・browser profile
> `openclaw` 固定・model ピン・`verify.py` で LLM を呼ばない・既存サブコマンド表面互換）。
> 対象スキルは `jp-form-outreach`。`linkedin-outreach` は本書の対象外。

## 0. 背景（実ログから判明した2大原因）

1日のバッチ送信ログで、停止のほぼ全部が次の2系統に集約された。

- **A. 「送信ボタン未検出」の大半は“ボタンが無い”ではなく“バリデーションで前進できない”。**
  実例: chateraise=確認ボタン disabled / 元気GDC=候補が「プライバシーポリシー」のみ /
  JINS=「入力内容を確認する」が DOM に**有るのにクリックされない**（同意チェック未） /
  premier=**電話番号のハイフン**で前進不可（除去で送信成功） / ヤーマン=候補20個が全部ナビ
  メニュー（フォーム外を拾っている）。
- **B. run が途中で死ぬと成果が全消失し新規0件。**
  `draft.py` がループ末尾で**一括 atomic 書き込み**のため、17社 draft 済みでも crash で
  `drafts.jsonl` に0件。さらに LLM 応答が ```json フェンスで返ると parser 失敗→`{"raw":...}`
  →空 subject/body→リトライ長時間→タイムアウト（pigeon 事例）。

本書は A・B を最優先で潰し、付随する C〜F も指示する。

---

## 1. 変更対象ファイル地図

| パス | 役割 | v7 での扱い |
|---|---|---|
| `jp-form-outreach/run.py` | 送信フロー（fill / submit / verify） | §A, §C, §D 改修 |
| `_outreach_core/draft.py` | 汎用 draft ステージ | §B-1（incremental save）, §B-3（retry cap） |
| `_outreach_core/prompt.py` | `extract_first_json` 等 | §B-2（```json フェンス剥がし） |
| `_outreach_core/submit_progress.py` | **新規・純関数** | §A の判定ロジック（テスト可能に分離） |
| `_outreach_core/field_format.py` | **新規・純関数** | §A-3 tel/郵便番号の正規化バリアント |
| `_outreach_core/screening.py` | **新規・純関数** | §E B2B 適格性スクリーニング |
| `_outreach_core/tests/` | テスト | 各 § の受け入れテストを追加 |

> 既存の `_check_agreement()`（run.py 〜1440）、`checkboxes_to_check`（plan 経由, 〜1954）、
> `_enumerate_buttons()`, `_click_button()`, `_llm_pick_final_submit()`, `fill_form_for_target()`,
> `verify_send_completed()`（`_outreach_core/verify.py`）を再利用・強化する。**新規の重い専用 CLI は作らない。**

---

## 2. §A 送信ボタンの進行性（最優先）

**狙い**: 「送信ボタンが押せない」を “ボタン探索” ではなく **“前進を阻むバリデーションの解消”** として扱う。

### A-1. disabled ボタンの原因診断
- 確認/送信ボタン**候補は見つかったが clickable でない**（`disabled` / `aria-disabled=true` / `pointer-events:none`）
  場合に、**「なぜ disabled か」を構造化して返す**ヘルパーを `submit_progress.py` に作る:
  - 未チェックの **required チェックボックス**一覧（特に 同意/プライバシー/利用規約/個人情報）
  - 未充足の **required フィールド**一覧（name/label）
- run.py の submit 段で、`_click_button` が「該当テキストはあるが clickable でない」を返せるよう、
  クリック用 JS（`_CLICK_BUTTON_BY_TEXT_JS`）に `found_but_disabled: true/false` と該当要素情報を追加。

### A-2. required 同意チェックボックスの自動チェック（強化）
- `disabled` 検出時、**まず未チェックの required/同意系チェックボックスを全て自動チェック**してから
  ボタンの clickable 状態を**再評価して再クリック**する（最大2回ループ）。
- 既存の同意判定正規表現（run.py 〜1345 の `/同意|プライバシー|利用規約|個人情報|送信する$/`）を
  `submit_progress.py` 側の純関数に切り出し、`required` 属性のチェックボックスも対象に含める。

### A-3. フィールド形式バリアントの自動再試行
- 送信/確認ボタンが押せても**クライアント側バリデーションで前進できない**ケース（tel ハイフン等）に対応。
- `field_format.py`（純関数）に正規化を実装し、**バリデーション失敗 or disabled 継続時に**順に試す:
  - 電話番号: ハイフン除去 / 全角→半角 / 先頭0補完なし版 等
  - 郵便番号: ハイフン有無 / 全角→半角
  - 全角英数の半角化（メール・URL 以外の数値系）
- 1フィールドあたり試行は**上限付き**（例 3 バリアント）。成功したら確定、全滅なら次段へ。

### A-4. ボタン列挙をフォームにスコープ（ナビ除外）
- `_enumerate_buttons()` を強化:
  - **textarea を含む最大フォーム**（既存の form scope ルール §11-A-2 と同基準）**内のボタンを最優先**。
  - `<nav>` / `role="navigation"` / `<header>` / `<footer>` 配下、およびメニュー的リンク
    （大文字ラベル＋説明が続く “ABOUT私たちについて” 等）を**除外/降格**。
  - 返却に `scope: "form"|"document"` と各候補の `in_form: bool` を付与。
- LLM picker（`_llm_pick_final_submit`）にはフォーム内候補を優先提示する。

### A-5. `<button>` 以外の送信要素対応
- 送信候補に `a[href]` / `[role="button"]` / `input[type="image"]` / クリック可能 `div/span` を含める
  （表示テキスト or aria-label でマッチ）。クリックは既存の ref クリック機構を流用。

### §A 受け入れ条件
1. **チェックボックス・ゲート**: ダミー HTML で「同意 required チェックボックス未チェック→確認ボタン
   disabled」を再現し、フローが**自動チェック→ボタン enabled→クリック**まで到達すること（元気GDC/JINS/
   chateraise 型）。
2. **tel 形式**: tel に `090-1650-1629` を入れると前進不可、`09016501629` で前進可能なダミーで、
   `field_format` のバリアント再試行により**送信成功**に至ること（premier 型）。
3. **フォームスコープ**: ナビ20個＋フォーム内「送信する」1個のダミーで、`_enumerate_buttons` が
   **フォーム内ボタンを最優先返却**し、ナビを拾わないこと（ヤーマン型）。
4. `submit_progress.py` / `field_format.py` は**純関数ユニットテスト**で網羅（DOM dict を入力に取る形）。

---

## 3. §B ドラフトの永続化・パーサ堅牢化（最優先）

### B-1. per-target incremental save（`draft.py`）
- 現状: `stage_draft` は全件を `drafts` list に貯めて**ループ後に一括 `out_path.open("w")`**（〜281行）。
  → **1社ずつ `out_path` に追記**する方式へ変更（emit 直後に append）。crash でも completed は残る。
- **再開可能性**: 再実行時、`drafts.jsonl` に既存の id は**再 draft しない**（id で skip）。
- 既存の SKIP 記録（`append_skip_fn`）と最終的な重複排除は維持。`drafts.jsonl` のカラムは不変（追加のみ可）。
- 途中再実行で二重行が出ないこと（id 単位 upsert もしくは事前 skip）。

### B-2. ```json フェンス剥がし（`prompt.py` `extract_first_json`）
- LLM 応答が ` ```json … ``` ` / ` ``` … ``` ` で包まれていても**確実に中身の JSON を抽出**する。
- 先頭・末尾のプロローグ/エピローグ文を許容し、最初の `{...}`（または `[...]`）を JSON として解釈。
- 失敗時は `None` を返す（`{"raw":...}` を「成功」と誤認しない）。

### B-3. parse リトライ上限（`draft.py`）
- パース失敗時のリトライは**上限付き**（例 2 回）。上限到達で当該社を `draft SKIP`（reason=`parse_error`）
  として記録し**次へ進む**（無限/長時間ループ禁止）。
- 1社の draft 全体にも**ソフト時間ガード**を入れ、超過時は SKIP 化して継続。

### §B 受け入れ条件
5. **フェンス**: ` ```json\n{"subject":"x","body":"y"}\n``` ` を `extract_first_json` が正しく dict 化。
6. **incremental**: 5社中3社 emit 後に擬似 crash（例外）させ、`drafts.jsonl` に**3社が永続化**されている
   こと。再実行で残り2社だけ draft され、既存3社は再生成されないこと。
7. **retry cap**: 常にパース不能を返すモック `oc_infer_fn` で、当該社が上限回数で SKIP 化され**ループが
   有限時間で終了**すること。

---

## 4. §C スコープ厳守（暴走防止）

- 「5社」と指示しても `enrich`/`draft` を引数なしで叩くと**入力全件**を処理してしまう（37社暴走の原因）。
- `enrich` / `draft` に **`--limit N`** を必須相当で扱う運用に寄せ、**入力が N を超える場合は先頭 N 件のみ
  処理**して `[enrich] --limit N 適用（全 M 件中）` をログ。`campaign` は limit を下流へ伝播。
- SKILL.md のトリガー表に「件数指定時は必ず `--limit` を付ける」を明記。

### §C 受け入れ条件
8. 37 行の `enriched.jsonl` に対し `draft --limit 5` が**5社のみ**処理すること。

---

## 5. §D 送信前 完全性ゲート（空送信の防止）

- 実害: U-NEXT が iframe 制約で 10 フィールド未取得のまま「送信完了」＝**本文スカスカで送信**。
- submit クリック**直前**に検査:
  - 本文 textarea が**空**、または fill 診断の **required 未充足が1件以上** → **送信しない**。
  - `_queue_for_resolver(reason_class="incomplete_fill")` に回す（§16 リゾルバ既存導線）。
- `verify_send_completed` 側でも、status=ok の確定に「本文が実際に送られた形跡」を要件として補強（§F）。

### §D 受け入れ条件
9. 本文 textarea が未充足のターゲットで、submit が**実行されず** `incomplete_fill` でキュー登録されること。

---

## 6. §E B2B 適格性スクリーニング（前倒し）

- 実害: SFP(予約)/シャトレーゼ(原材料提案専用)/JINS(営業お断り明記)/元気GDC(B2C) が **draft・send まで
  進んでから**弾かれ、時間を浪費。
- `screening.py`（純関数）に、ページテキスト/フォーム文言から**失格シグナル**を検出する関数を実装:
  - 「営業/勧誘お断り」「お取引のご提案はお受けして…ません」等
  - 「予約」「ご予約」フォーム、「採用/応募/求人/エントリー」
  - B2C 顧客窓口のみ（「お客様相談室」等）で B2B 提案窓口が無い
- `enrich` 段でこれを評価し、`screen_skip`（reason 付き）を立てて **draft 対象から除外**。
  既存の送信時 `WRONG_FORM_TYPE` 判定は最終防壁として残す（前倒しが主、後段が保険）。

### §E 受け入れ条件
10. 「当社への営業のご案内はお受けしておりません」を含むページで、enrich が `screen_skip` を立て、
    後続 draft が当該社を**生成しない**こと。

---

## 7. §F verify 堅牢化（補強・低優先）

- 既実装の success-keyword 優先（「メッセージは送信されました」等）は維持。
- 追加: status=ok の確定に **(成功シグナル) AND (本文が空でない=実送信された形跡)** を要件化し、
  §D と整合させる（U-NEXT 型の「ok だが中身空」を `uncertain` に落とす）。`verify.py` は引き続き**純 Python**。

### §F 受け入れ条件
11. 成功キーワードはあるが本文未送信が疑われるスナップショットで、status が `ok` ではなく
    `uncertain` になること（§D と二重で空送信を防ぐ回帰テスト）。

---

## 8. 守ってほしい不変項（v7 追加分）

1. `draft.jsonl` / `enriched.jsonl` 等の**カラム削除・リネーム禁止**（`screen_skip` 等の**追加のみ**可）。
2. 6フェーズ名・順序、`run.py` 既存サブコマンドの**表面互換**維持。
3. `verify.py` で **LLM を呼ばない**（§F も純 Python）。
4. ブラウザ profile は `openclaw` 固定、`oc_browser`/`oc_evaluate` シグネチャ不変。
5. 送信系の新ロジックは**誤送信を増やさない**こと（§D/§F は安全側に倒す。疑わしきは送らない）。
6. 重い専用 CLI・新スキルを増やさない。判定ロジックは**純関数モジュール＋ユニットテスト**に寄せる。

## 9. やらないこと

- ❌ reCAPTCHA の機械的自動回答・ソルバー・fingerprint 偽装（方針外）。
- ❌ ブラウザ profile の差し替え／並列同一プロファイル使用。
- ❌ `verify.py` の LLM 化。
- ❌ スコープ無視の全件処理（§C で明示的に禁止）。

## 10. 実装順（推奨）

1. **§B**（incremental save ＋ フェンス剥がし ＋ retry cap）— 0送信・成果消失を即止血。
2. **§A**（チェックボックス・ゲート → tel 形式 → フォームスコープ → 非button要素）— escalation の最大要因。
3. **§D**（空送信ゲート）→ **§C**（スコープ）→ **§E**（前倒しスクリーニング）→ **§F**（verify 補強）。

各 § 完了ごとに `python3 -m pytest _outreach_core/tests/ -q` を緑にしてから次へ。
