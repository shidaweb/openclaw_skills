# CURSOR 指示書 v11 — 確認画面の送信を「テキストでなく form/type」で確定する

> 正典は [`CURSOR_INSTRUCTIONS.md`](./CURSOR_INSTRUCTIONS.md)。本書はその差分指示（v11）。
> §3「守ってほしい不変項」は**全て継続**（6フェーズ名・JSONL名・append-only履歴・browser profile
> `openclaw` 固定・model ピン・`verify.py` で LLM を呼ばない・既存サブコマンド表面互換）。
> v7 §A-4/A-5（フォームスコープ・非button要素）の続き。対象 `jp-form-outreach`。

## 0. 背景（AOKI 型の実ログ）

`aoki_hd`（`https://support.aoki-style.com/holdingscontact?company=holdings`）:
- fill 15件OK・**確認画面遷移OK**、しかし**最終 submit が pattern/LLM 両方検出失敗**。
- ページ内ボタン候補（12）が**全部「こちら」**（汎用リンク）。

**現状コードの到達点**（既に実装済）:
- `_ENUMERATE_BUTTONS_JS`：`input[type=image]`/`[onclick]`/`.btn`/`[class*=submit]` も対象、text 無しでも
  submit/button/image 型は保持（run.py 〜1654/1890, `if (!txt && !['submit','button','image'].includes(btnType)) return;`）。
- `_NOISE_BTN_RE`（〜2036）に「こちら」等のノイズ除外。`_llm_pick_final_submit`（〜2126）も存在。

**それでも失敗する理由（=穴）**:
1. **テキストでは判別できない送信コントロール**（`<input type=submit value="">`・画像/アイコン submit・
   JSハンドラ要素）で、**ノイズ（「こちら」）を除くと候補が枯渇**→「not found」。
2. **確認画面には textarea が無い**ことが多く、`textarea を含む最大フォーム`スコープが効かない
   （確認画面は hidden input ＋ submit の構成）。
3. **ネイティブ submit フォールバックが存在しない**（`form.requestSubmit()` / form内 `[type=submit]` 直接起動が無い）。

> ここは **LLM でも解けない**（区別できるテキストが無い）。`form`/`type` を使う**構造的な確定**が必要。

→ 本書は **(N1) ネイティブ submit フォールバック**、**(N2) 確認画面のフォーム特定**、
**(N3) 候補に submit/in_form タグ付け＋picker 優先度**、**(N4) 全候補ノイズ時は即フォールバック** を指示する。

---

## 1. 変更対象ファイル地図

| パス | 役割 | v11 での扱い |
|---|---|---|
| `jp-form-outreach/run.py` | submit 探索（confirm 最終 submit） | §N1〜N4。新規 JS ＋ フォールバック配線 |
| `_outreach_core/submit_progress.py` | 純関数（候補スコア/ノイズ） | §N3（`is_submit_type`/`in_form` を考慮した選択・ノイズ判定の純関数化） |
| `_outreach_core/tests/` | テスト | 各 § の受け入れ |

> 既存 `_ENUMERATE_BUTTONS_JS` / `_NOISE_BTN_RE` / `_llm_pick_final_submit` / confirm-flow 最終 submit
> （run.py 〜3642/3748/4201）を強化。**新規の重い専用 CLI は作らない。**

---

## 2. §N1 ネイティブ submit フォールバック（最重要・新規）

**狙い**: pattern も LLM picker も外したとき、**テキストに頼らず form の送信機構を直接起動**する。

- 新規 JS `_SUBMIT_TARGET_FORM_JS`（run.py）:
  - 引数で**対象フォームの特定ヒント**（filled フィールドの name 群 / form_root_selector）を受け取り、
    §N2 の基準で**対象 form** を1つ決める。
  - その form 内の送信コントロールを**優先順**で探してクリック:
    1. `button[type=submit]` / `input[type=submit]` / `input[type=image]`（clickable なもの）
    2. 上記が無ければ **`form.requestSubmit(submitter)`**（submitter は上記候補があればそれ、無ければ未指定）。
  - **`form.submit()`（生submit）は使わない**（クライアントバリデーションを飛ばすため）。`requestSubmit` を使う。
  - 返り値: `{ method: "click_submit"|"requestSubmit"|"none", form_sig, control_text, reason }`。
- run.py 側 `_submit_native(d)` ラッパ:
  - confirm-flow の最終 submit で **pattern → LLM picker → `_submit_native`** の順に試す
    （`run.py` 〜3642/3748/4201 の「not found」に落ちる**直前**に挿入）。
  - 成功（method != "none"）なら送信続行（verify へ）。失敗時のみ従来どおり `_queue_for_resolver`。
  - `single` フローの「first submit not found」にも同様に最終手段として適用。

### §N1 受け入れ条件
1. 「確認画面に hidden inputs ＋ `<input type=submit>`（value空）＋『こちら』リンク×12」のダミーで、
   pattern/LLM が外しても `_submit_native` が **type=submit を click** して submit に至ること。
2. submit ボタンが**アイコンのみ（text 無し・`button[type=submit]`）**でも検出・クリックされること。
3. 送信コントロールが DOM に無いが form がある場合、**`requestSubmit` が呼ばれる**こと（生 `form.submit()` を呼ばない）。

---

## 3. §N2 確認画面の対象フォーム特定（textarea が無い前提）

**狙い**: 確認画面で「どの form を送るか」を**誤らない**（検索/ログイン/ナビ form を送らない）。

- 対象 form の選定基準（`_SUBMIT_TARGET_FORM_JS` 内、純粋に DOM 構造で判定）:
  - **加点**: filled フィールドの name/値を**hidden input として保持**している form / `[type=submit]` を持つ form /
    本文（メイン領域）内の form。
  - **除外**: `input[type=password]` を持つ（ログイン）/ `role=search` ・ `action` に `search|login|logout` を含む /
    `<header>`/`<footer>`/`<nav>` 配下のみの form。
  - 候補が複数なら**加点最大**を1つ選ぶ。0 件なら `method:"none"`。
- form_root_selector（enrich 由来）があれば最優先で使う。

### §N2 受け入れ条件
4. 「検索 form（role=search）＋ 問い合わせ確認 form（hidden inputs ＋ submit）」併存ダミーで、
   **検索 form を選ばず**問い合わせ form を選ぶこと。
5. password を含む form は**対象外**になること（誤ログイン submit 防止）。

---

## 4. §N3 候補タグ付け＋picker 優先（submit/in_form）

- `_ENUMERATE_BUTTONS_JS` の各候補に **`is_submit_type`**（type ∈ submit/image、または `button[type=submit]`）と
  **`in_form`**（closest form が §N2 の対象 form）を付与。
- `submit_progress.py` の純関数で、最終 submit 候補のランキングを:
  1. `is_submit_type && in_form` を**最優先**
  2. 次に「送信/確認」系テキスト一致
  3. **`_NOISE_BTN_RE`（こちら/詳細/戻る/一覧/プライバシー…）は最下位/除外**
- `_llm_pick_final_submit` には**フォーム内・submit型候補を上位**に並べて渡す（テキスト無し候補も `is_submit_type`
  を明示して渡し、LLM が型で選べるようにする）。

### §N3 受け入れ条件
6. 候補が「こちら×12 ＋ in_form の type=submit×1」のとき、純関数ランキングが**type=submit を1位**に返すこと。
7. ノイズのみ（type=submit 無し・text 全部「こちら」）のとき、ランキングが**空**を返し §N4 に委ねること。

---

## 5. §N4 全候補ノイズ時は「not found」にせず即フォールバック

- confirm-flow 最終 submit で、**列挙候補が全て `_NOISE_BTN_RE`（または type 非 submit のリンクのみ）**のとき、
  「not found」と判定する前に **§N1 `_submit_native` を必ず1回試す**。
- フォールバックも失敗した場合のみ `_queue_for_resolver`。その際の診断に
  「候補N件は全て汎用リンク（例: こちら×12）／native submit: <method/reason>」を含める（リゾルバ/人への手掛かり）。

### §N4 受け入れ条件
8. 全候補ノイズのダミーで、`_queue_for_resolver` の**前に** `_submit_native` が呼ばれること（モックで確認）。
9. キュー登録時の reason/診断に「全候補が汎用リンク」「native submit 試行結果」が含まれること。

---

## 6. 守ってほしい不変項（v11 追加分）

1. **`form.submit()`（生submit）禁止**。必ず `requestSubmit`（クライアントバリデーション維持）。
2. **検索/ログイン/ナビ form を送らない**（§N2 の除外を厳守）。誤送信・誤ログインを増やさない。
3. ネイティブ submit は**対象 form 1つ**に限定（複数 form 同時 submit 禁止）。
4. `enriched.jsonl`/イベントのカラムは不変（`native_submit_method` 等は**追加のみ**）。
5. ランキング/フォーム選定の判定は**純関数（`submit_progress.py`）＋ユニットテスト**に寄せる。`verify.py` 不変。
6. フォールバックは confirm/single の**最終手段**（pattern→LLM→native の順）。順序を崩さない。

## 7. やらないこと

- ❌ `form.submit()` の生呼び出し（バリデーション飛ばし）。
- ❌ 検索/ログイン/別フォームの submit。
- ❌ テキスト一致だけに依存し続ける（型/フォーム構造を優先）。
- ❌ `verify.py` の LLM 化。

## 8. 実装順（推奨）

1. **§N3**（候補に `is_submit_type`/`in_form` タグ＋純関数ランキング）— 土台。テストで固める。
2. **§N2**（対象フォーム特定 JS）→ **§N1**（`_submit_native` フォールバック）— AOKI 型の主因解消。
3. **§N4**（全候補ノイズ時の即フォールバック＋診断）。

各 § 完了ごとに `python3 -m pytest _outreach_core/tests/ -q` を緑にしてから次へ。
