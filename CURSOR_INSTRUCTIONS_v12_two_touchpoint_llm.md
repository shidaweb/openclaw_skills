# CURSOR 指示書 v12 — 2タッチポイント LLM でフォームを走破する（Pre-Form / Post-Form）

> 正典は [`CURSOR_INSTRUCTIONS.md`](./CURSOR_INSTRUCTIONS.md)。本書はその差分指示（v12）。
> §3「守ってほしい不変項」は**全て継続**。v7（送信進行性）/ v10（種別選択）/ v11（ネイティブsubmit）
> を**統合・前提**とする。対象 `jp-form-outreach`。

## 0. 背景（AOKI 実フォームの構造）

`aoki_hd`（`support.aoki-style.com/holdingscontact`）の入力ページは、**ワンショットの fill プランでは
解けない“有効化シーケンス”**だった（スクリーンショットで確認）:

1. **ルーティング radio「●お客様 / ○法人（新規提案）」** … 既定は「お客様(B2C)」。**「法人（新規提案）」を
   選ぶ**必要がある。
2. **法人を選ぶと「お問い合わせの種類」プルダウンの選択肢が変わり得る** … 選ぶ前に**再観測**が必要。
3. **「個人情報の取扱いに同意する」チェックボックス**（未チェック）。
4. 上記が全部揃って初めて **「お問い合わせ内容の確認」ボタンが activate**（現状 disabled / グレーアウト）。
5. 確認画面に遷移後、**最終送信**（テキスト判別不能な submit。v11 の領域）。

**現状コードの限界**:
- `_llm_analyze_form` は**ワンショットの静的 fill プラン**（field→value）。
  「ボタンが disabled で、何を満たせば有効化されるか」「選択で選択肢が変わる」を**理解していない**。
- `_click_button_with_gate_retry` は submit 失敗**後**に gate を埋めて再クリックする**事後の固定ループ**で、
  **LLM が状態を見て次の一手を決める**わけではない。

→ 本書は **(P) Pre-Form LLM＝入力ページの理解＋“有効化シーケンス”の反復実行**、
**(Q) Post-Form LLM＝確認ページの最終送信** の**2タッチポイント**に分けて設計する。

---

## 1. 変更対象ファイル地図

| パス | 役割 | v12 での扱い |
|---|---|---|
| `jp-form-outreach/run.py` | `_llm_analyze_form` / `fill_form_for_target` / confirm-flow | §P/§Q の配線・反復ループ |
| `jp-form-outreach/prompts/system_persona*.md` 近傍の `_FORM_ANALYZER_PROMPT_TEMPLATE` | プランナープロンプト | §P プロンプト改訂（順序＋ゲート理解） |
| `_outreach_core/submit_progress.py` | 純関数（gate/radio/select/native） | §P/§Q の判定を純関数で支援 |
| `_outreach_core/tests/` | テスト | 各 § 受け入れ |

> v7 §A（disabled 診断・同意自動チェック）、v10（種別 B2B 選択）、v11（ネイティブ submit）を**再利用**。
> 重複実装しない。**新規の重い専用 CLI は作らない。**

---

## 2. §P Pre-Form LLM — 入力ページの理解＋「有効化シーケンス」反復実行

**狙い**: 「fill して submit」ではなく、**「確認/送信ボタンを activate させるための順序ある操作」を
LLM が状態を見ながら決め、満たすまで反復**する。

### P-1. プランナーの出力を「順序付き行動列＋ゲート理解」に拡張
`_FORM_ANALYZER_PROMPT_TEMPLATE` を改訂し、`_llm_analyze_form` に次を出力させる:
- **`route_choice`**: お客様/個人 vs **法人/企業/取引（B2B）** のルーティング radio・トグルがあれば、
  **B2B 側 option を最初に選ぶ**指示（value）。無ければ null。
- **`enable_sequence`**: 確認/送信ボタンを有効化するための**順序付き行動列**。例:
  `[{select_radio: route=法人}, {RESCAN}, {select_option: 種別=取引・提携系}, {set_text: 詳細=__BODY__},
    {check: 個人情報同意}, {click: 確認ボタン}]`。
  - `RESCAN` トークンを含め、**選択で選択肢/必須が変わる箇所**を明示。
- **`submit_gate`**: 「確認/送信ボタンは disabled で、`required radio + required select + 同意 checkbox` を
  満たすと有効化される」等の**ゲート条件の理解**（人手 override 不要の自動判断）。
- 既存の field→value（fill プラン）も維持（追加のみ）。

### P-2. 反復実行ループ（observe → act → re-observe）
`fill_form_for_target`（または新規 `_drive_enable_sequence`）で:
1. `enable_sequence` を**順に実行**。`RESCAN` または各ゲート操作（radio/select/checkbox）の後は
   **`_FORM_FIELDS_JS` を再評価**して fields/options/required を更新（v10 §S2 の再スキャンを一般化）。
2. 各ステップ後、**確認/送信ボタンの `found_but_disabled`（v7 §A-1）を再判定**。
3. ボタンが **enabled になったらクリック**して入力ページ完了。まだ disabled なら、
   **残る未充足ゲート（未選択 required select / 未チェック同意 / 未選択 radio）を LLM に再提示し次の一手を取得**
   （観測→行動→再観測）。**上限 K ステップ**（例 4）。
4. K 到達でも disabled のままなら、`submit_progress` の純関数で**残ゲートを構造的に列挙**して
   `_queue_for_resolver(reason_class="submit_gate_unsatisfied", diagnostics=残ゲート)`。

### P-3. ルーティング radio の B2B 選択（順序最優先）
- お客様/法人・個人/企業・一般/取引 等の**二択ルーティング**は **B2B 側を最初に選ぶ**
  （submit_progress の `_PREFER`＝法人|企業|取引|提携… を流用）。**選択後は必ず RESCAN**（§P-2）。
- override（`route_radio` 等）があれば最優先。

### P-4. 同意・種別の統合
- 同意 checkbox（個人情報/プライバシー/利用規約）は v7 §A-2 の自動チェックを enable_sequence に統合。
- 種別 select/radio の B2B 選択は **v10（LLM 主導＋純関数ガードレール）**を流用。RESCAN 後の最新 options で判断。

### §P 受け入れ条件
1. **AOKI 型ダミー**（route=お客様既定／法人選択で種別 options 変化／同意未チェック→確認ボタン disabled）で、
   ループが **法人選択 → RESCAN → 種別=B2B → 詳細 → 同意チェック → 確認ボタンが enabled → クリック** まで
   到達すること（LLM をモックした統合テスト）。
2. 各ゲート操作後に **fields が再観測**され、disabled→enabled の遷移で**初めてクリック**されること。
3. K ステップで未充足なら `submit_gate_unsatisfied` で**残ゲート診断つき**でキュー登録されること。
4. ルーティング radio が**B2B 側**を選び、選択後に RESCAN が走ること（純関数 `_PREFER` ＋ループ統合テスト）。

---

## 3. §Q Post-Form LLM — 確認ページの最終送信

**狙い**: 確認画面（textarea 無し・hidden inputs）の**最終送信を確定**する。テキスト判別不能ケースを潰す。

- 確認画面に遷移したら:
  1. **Post-Form LLM**（`_llm_analyze_form` の confirm フェーズ呼び出し / 既存 `phase` 引数）で、
     **「この確認画面の送信ボタンはどれか」「未充足ゲート（同意の再掲等）はあるか」**を判断。
     候補には v11 の `is_submit_type`/`in_form` タグ＋型情報を渡し、**テキストが無くても型で選べる**ようにする。
  2. LLM が特定できない/候補が全ノイズ（「こちら」等）なら、**v11 §N1 ネイティブ submit フォールバック**
     （対象 form の `[type=submit]` クリック → `form.requestSubmit()`）。
  3. 送信後 verify（v7 §F：成功シグナル＋本文送信形跡）。
- **安全**: `form.submit()` 生呼び出し禁止（requestSubmit）。検索/ログイン form を送らない（v11 §N2）。

### §Q 受け入れ条件
5. 確認画面で「候補が全部『こちら』」のダミーでも、Post-Form LLM 不成立 → v11 ネイティブ submit で**送信に至る**こと。
6. 確認画面に**同意の再掲ゲート**がある場合、Post-Form LLM がそれを検出→チェック→送信に進むこと。

---

## 4. 全体フロー（Pre/Post の位置づけ）

```
入力ページ:  enrich → [Pre-Form LLM: enable_sequence + submit_gate 理解]
            → observe→act→re-observe ループ（route radio→RESCAN→種別→詳細→同意）
            → 確認/送信ボタンが enabled → クリック
確認ページ:  [Post-Form LLM: 最終送信ボタン特定 / 残ゲート] → 不成立なら v11 native submit
            → verify（v7 §F）
失敗時:      submit_gate_unsatisfied / native submit 失敗 → 残ゲート診断つきで resolver キュー
```

## 5. 守ってほしい不変項（v12 追加分）

1. **観測→行動→再観測**を徹底（選択で選択肢・必須・disabled が変わる前提）。one-shot で submit しない。
2. 反復ループは**上限 K ステップ**（無限ループ禁止）。未充足は**残ゲート診断つき**で resolver へ。
3. ルーティング/種別は **B2B 妥当性最優先**（個人/お客様/採用/placeholder を選ばない）。override 最優先。
4. 確認画面は **v11 の安全 submit**（requestSubmit・対象 form 限定・検索/ログイン除外）。誤送信を増やさない。
5. LLM 呼び出しは**既存 `_llm_analyze_form`（form_analyzer ピン）への畳み込み**（Pre/Post の phase 切替）。
   新規の重い LLM 経路を作らない。判定の核は**純関数＋テスト**に寄せる。`verify.py` 不変。
6. `enriched.jsonl`/イベントのカラムは**追加のみ**（`enable_steps`/`submit_gate`/`route_choice` 等）。

## 6. やらないこと

- ❌ ゲートを無視した強引 submit（disabled のまま JS で押す等）。
- ❌ `form.submit()` 生呼び出し / 検索・ログイン form の送信。
- ❌ 個人/お客様/採用/placeholder の選択。
- ❌ 反復ループの無上限化。`verify.py` の LLM 化。

## 7. 実装順（推奨）

1. **§P-3/P-4**（ルーティング radio の B2B 選択＋同意/種別の統合）— 既存純関数の流用で土台。
2. **§P-1/P-2**（プランナー出力の順序化＋observe→act→re-observe ループ）— AOKI 型の主因解消。
3. **§Q**（Post-Form LLM ＋ v11 native submit 結線）— 確認画面の最終送信。

各 § 完了ごとに `python3 -m pytest _outreach_core/tests/ -q` を緑にしてから次へ。
```
要約: 入力ページは「Pre-Form LLM が“有効化シーケンス”を反復実行して disabled ボタンを開ける」、
確認ページは「Post-Form LLM＋型ベースのネイティブ submit で最終送信する」。
```
