# CURSOR 指示書 v10 — 問い合わせ種別プルダウン（select/radio）の攻略

> 正典は [`CURSOR_INSTRUCTIONS.md`](./CURSOR_INSTRUCTIONS.md)。本書はその差分指示（v10）。
> §3「守ってほしい不変項」は**全て継続**（6フェーズ名・JSONL名・append-only履歴・browser profile
> `openclaw` 固定・model ピン・`verify.py` で LLM を呼ばない・既存サブコマンド表面互換）。
> 対象スキルは `jp-form-outreach`。

## 0. 背景（現状コードと弱点）

例: `https://www.watami.co.jp/contact/` の様な **「お問い合わせ種別」プルダウン**を持つフォーム
（「法人のお客様／個人のお客様／採用／お取引・ご提案 …」等）が苦手。

**現状の実装（最新コード）**:
- B2B 向けの種別選択ロジックは **既に存在**する：`_outreach_core/submit_progress.py` の
  `pick_select_gate_actions` / `_pick_select_option`（`_PREFER`＝法人|企業|取引|提携|協業|提案…、
  `_AVOID`＝個人|採用|IR|予約|サポート…でスコアリング）。
- しかし呼ばれるのは **`run.py` `_auto_select_submit_selects()` → `_click_button_with_gate_retry()`**、
  つまり **「submit を押して詰まった後の gate-retry 時だけ」**。
- LLM planner（`_FORM_ANALYZER_PROMPT_TEMPLATE` rule 11/12）は **`overrides.category_select` /
  `category_radio`（人手指定）依存**。override が無いと種別を **fill 段で選ばない**。

**弱点（=苦手の正体）**:
1. **fill 段で能動的に種別を選ばない** → 既定値（「選択してください」や先頭の B2C 選択肢）のまま submit。
   送信できても**誤った窓口に届く**／既定値が invalid で**バリデーション停止**。
2. **種別選択で増える条件付き必須**（法人選択時に会社名・部署等が出現）を**再スキャンしていない**。
3. **B2B 向け選択肢が無い**種別（個人/お客様/採用のみ）でも draft・send まで進んで時間を浪費。
4. placeholder（「選択してください」「---」）を**誤って選ぶ**余地。

→ 本書は **(S1) fill 段での能動的 B2B 種別選択**、**(S2) 選択後の条件付き必須 再スキャン**、
**(S3) picker 精度向上**、**(S4) B2B 選択肢なし→screen_skip** を指示する。

### 設計方針（重要）：LLM 主導 ＋ 純関数ガードレール

種別選択は **正規表現スコアリングを“主”にしない**。長い裾野（「協業・アライアンス」「OEM・受託」
「お仕事のご依頼」「メディア関係者の方」等）を取り逃すため。**既に毎フォームで走る form-analyzer LLM
（`_llm_analyze_form` / Sonnet）に「最適な B2B 種別 option を選ばせる」のを主経路**とする（追加 LLM 呼び出し
は不要＝畳み込み）。純関数 `submit_progress.py` は次の3役の**安全弁**に回す:
- **ガードレール**: LLM の返りが**実在 option text かつ placeholder でない**ことを検証。
- **フォールバック**: LLM 不在/不正返答時に `_PREFER`/`_AVOID` スコアで選ぶ。
- **no-B2B 検知**: 妥当な B2B option が無い旨を判定（§S4 screen_skip）。

---

## 1. 変更対象ファイル地図

| パス | 役割 | v10 での扱い |
|---|---|---|
| `_outreach_core/submit_progress.py` | 種別選択スコアリング（既存） | §S3 強化（`_PREFER`/`_AVOID`/placeholder/曖昧判定）, §S1 用 API 追加 |
| `jp-form-outreach/run.py` | `fill_form_for_target` / `_auto_select_submit_selects` / `_llm_analyze_form` | §S1 能動選択を fill に前倒し, §S2 再スキャン |
| `jp-form-outreach/run.py` `stage_enrich` | enrich | §S4 B2B 種別なし→`screen_skip` |
| `jp-form-outreach/prompts/system_persona*.md` / planner template | — | §S5 LLM フォールバック（曖昧時のみ） |
| `_outreach_core/tests/` | テスト | 各 § の受け入れ（watami 型 option リストで純関数テスト） |

> 既存 `_INQUIRY_TYPE` 系正規表現（submit_progress.py 13–33行）、`_LIST_SELECT_GATES_JS`、
> `_apply_field_action`、`fill_form_with_plan`、`_FORM_FIELDS_JS` の `selects`/`radios` を再利用する。
> **新規の重い専用 CLI は作らない。**

---

## 2. §S1 fill 段での能動的 B2B 種別選択（最重要・LLM 主導の適用）

**狙い**: 「submit 失敗後の gate-retry」に頼らず、**fill のタイミングで最適な B2B 種別を先に選ぶ**。
選択の**意思決定は §S5（LLM 主導）**、本節はその結果を fill に**適用する配線**。

- `submit_progress.py` に**純関数 API**（安全弁）を追加:
  - `is_inquiry_type_field(field) -> bool`：select/radio の name/label が `_INQUIRY_TYPE` 群（お問い合わせ種別/
    ご用件/区分/カテゴリ/種別/件名/subject 等）にマッチ。
  - `validate_choice(options, chosen) -> bool`：`chosen` が**実在 option text かつ非 placeholder**か検証。
  - `choose_b2b_option(options) -> {value, score, confidence, reason} | None`：**フォールバック用**スコアラー
    （`_PREFER`/`_AVOID`）。prefer 皆無なら `None`。
- `run.py fill_form_for_target` で、**`overrides.category_select`/`category_radio` が無い場合に限り**:
  - `is_inquiry_type_field` を満たす select/radio について、**§S5 の決定値**（LLM 主・純関数フォールバック）を
    取得し、`validate_choice` 通過後に `_apply_field_action(name, "select_option"/"select_radio", value)` を実行。
  - 選んだ値を `diagnostics["filled"]` に `inquiry_type=<value>(src=llm|fallback, conf=...)` で記録し、
    `send.inquiry_type` を emit。
- **override 最優先は維持**（人が `category_select` を指定したらそれに従う。自動選択は override 不在時のみ）。
- gate-retry 側（`_auto_select_submit_selects`）は**保険として残す**（fill で取り切れなかった必須 select 用）。

### §S1 受け入れ条件
1. options=「選択してください/個人のお客様/法人のお客様/採用について/お取引・ご提案」で、
   **決定値が「お取引・ご提案」または「法人のお客様」**になり、「選択してください」「採用」が選ばれないこと
   （LLM をモックした統合テスト＋純関数フォールバック単体テストの両方）。
2. override `category_select` 指定時は**LLM/自動選択せず override を使う**こと。
3. fill 段で種別が選択され、`send.inquiry_type`（`src=llm|fallback`）が emit されること。

---

## 3. §S2 種別選択後の条件付き必須の再スキャン

**狙い**: 種別選択で**動的に出現する必須項目**（会社名/部署/取引内容 等）を submit 前に埋める。

- `fill_form_for_target` で **種別 select/radio を選んだ直後に**、`_FORM_FIELDS_JS` を**再評価**し、
  - 新たに出現した required フィールドを検出。
  - 既知のもの（会社名・部署・氏名・連絡先等）は sender 値で fill、未知のものは既存の動的必須導線
    （`_escalate_dynamic_required` / `checkboxes_to_check`）に委譲。
- 再スキャンは**1回**（無限ループ防止）。出現フィールドを `diagnostics` と `enrich`/send イベントに記録。

### §S2 受け入れ条件
4. 「法人選択で会社名 required が出現」するダミー（1回目 fields に無く、種別選択後の再評価で出現）で、
   会社名が sender 値で埋まり submit 前提が満たされること（モック evaluate で2回目の fields を差し替え）。

---

## 4. §S3 picker 精度向上（`submit_progress.py`）

- **placeholder 除外**: option text が「選択してください」「選択して下さい」「---」「—」「please select」
  「指定なし」等、または value が空のものは**選ばない**（候補から除外）。
- `_PREFER` 強化（STRONG）: 「法人のお客様」「企業・団体」「お取引(先)」「お仕事のご依頼」「業務提携」
  「協業」「ビジネス」「OEM/卸/代理店」。
- `_AVOID` 強化（STRONG）: 「個人のお客様」「商品・サービスについて」「ご意見・ご感想」「お客様相談」
  「店舗について」「採用」「アルバイト」「予約」。
- **曖昧判定**: 最良スコアが閾値未満、または上位2件が僅差なら `confidence="low"` を返す（§S5 で LLM へ）。
- `_pick_radio_option` にも同じ placeholder/STRONG ルールを反映。

### §S3 受け入れ条件
5. placeholder（「選択してください」/空 value）が**絶対に選ばれない**こと。
6. watami 型 option セット（法人/個人/採用/お取引）で「法人」系が選ばれ、`confidence` が付与されること。
7. prefer マッチ皆無（個人/採用/お客様のみ）の場合 `choose_b2b_option` が `None` を返すこと。

---

## 5. §S4 B2B 種別が無いフォームは screen_skip（enrich）

**狙い**: B2B 向け選択肢が存在しない種別フォームは、**draft・send 前に除外**して工数を節約。

- `stage_enrich` で、`is_inquiry_type_field` を満たす select/radio があり、かつ
  **LLM が `inquiry_type_no_b2b: true` を返す**（主）／**純関数 `choose_b2b_option` が `None`**（フォールバック）
  のとき、`screen_skip`（reason `no_b2b_inquiry_type`）を立てて draft 対象から除外。
- 既存の送信時 `WRONG_FORM_TYPE` / `_classify_form_type` は最終防壁として残す（前倒しが主・後段が保険）。
- v7/v8 の screening と同じ枠組みで記録（`enrich.form.screen_skipped` 等）。

### §S4 受け入れ条件
8. 種別 option が「個人のお客様/採用/お客様相談室」のみのダミーで、enrich が `no_b2b_inquiry_type` で
   `screen_skip` を立て、後続 draft が当該社を**生成しない**こと。

---

## 6. §S5 LLM 主導の種別選択（主経路・既存 analyzer に畳み込み）

**種別選択の意思決定はここが主**。追加の LLM 呼び出しは作らず、**既存 `_llm_analyze_form`（form-analyzer /
Sonnet）に種別選択を担わせる**（畳み込み）。

- planner プロンプト（`_FORM_ANALYZER_PROMPT_TEMPLATE` rule 11/12 を改訂）:
  - 「**お問い合わせ種別/区分/カテゴリ等の select・radio がある場合、`overrides.category_*` が無ければ、
    B2B 営業・取引・提携・協業の問い合わせを送る意図に最も合う option を“既存の選択肢テキストの中から”
    1つ選べ**」と指示。
  - **制約**: 自由生成禁止・**必ず実在 option text を返す**・placeholder（「選択してください」「---」等）・
    `個人/採用/IR/予約/サポート` 等の非 B2B は選ばない・**妥当な B2B option が無ければ
    `"inquiry_type_no_b2b": true` を出力**（§S4 で screen_skip）。
  - 出力は plan の該当フィールド action（`select_option`/`select_radio` の value）に反映。
- **ガードレール＋フォールバック**（§S1/§S3 の純関数）:
  - LLM の返り値を `validate_choice` で検証（実在＆非 placeholder）。NG なら `choose_b2b_option`（純関数）にフォールバック。
  - LLM が `個人/採用` 等を選んだが、明確に prefer な option が別に存在する場合は `confidence="low"` 記録＋
    純関数最良に置換（誤ルーティングの最終防御）。
- LLM 不在/タイムアウト時は**純関数 `choose_b2b_option` 単独**で決定（処理は止めない）。

### §S5 受け入れ条件
9. LLM が**実在しない/placeholder の option** を返したケースで、`validate_choice` が弾き
   **純関数フォールバックの妥当値**になること。
10. LLM が**妥当 B2B option 無し**（`inquiry_type_no_b2b: true`）を返したら §S4 の `screen_skip` に繋がること。
11. LLM 呼び出しは新設せず **`_llm_analyze_form` への畳み込み**であること（モデルは form_analyzer ピン＝Sonnet）。

---

## 7. §S6 学習・記録（任意・小）

- 選んだ種別をドメイン単位で記録（再送時の一貫性）。`send.inquiry_type` / `enrich.inquiry_type_selected`
  を emit し、`report` で「種別自動選択の件数・confidence 分布・no_b2b_inquiry_type 件数」を見られるようにする。

---

## 8. 守ってほしい不変項（v10 追加分）

1. **override 最優先**（`category_select`/`category_radio` が指定されたら自動選択しない）。
2. **placeholder/無効値は絶対に選ばない**（送信先の誤ルーティング防止）。
3. 再スキャンは**有限回**（種別選択後1回）。無限ループ禁止。
4. `enriched.jsonl`/`drafts.jsonl` のカラムは不変（`inquiry_type` 等は**追加のみ**、reason 文字列で表現可）。
5. 判定は**純関数（`submit_progress.py`）＋ユニットテスト**に寄せる。`verify.py` は不変・LLM 不使用。
6. 誤送信を増やさない（B2B 不確実なら送らず screen_skip/needs_attention 側に倒す）。

## 9. やらないこと

- ❌ 種別を「とにかく埋めて submit を通す」ための無差別選択（B2B 妥当性を最優先）。
- ❌ placeholder/「選択してください」の選択。
- ❌ LLM による option 自由生成（実在 text のみ）。
- ❌ `_classify_form_type`/`verify.py` の LLM 化。

## 10. 実装順（推奨）

1. **§S3**（純関数の picker＝placeholder 除外＋`validate_choice`＋フォールバックスコア）— 安全弁の土台。
2. **§S5**（form-analyzer LLM に種別選択を畳み込み＝**主経路**）— 苦手の主因を直接解消。
3. **§S1**（決定値を fill 段で適用＝override 不在時）→ **§S2**（選択後の条件付き必須 再スキャン）
   → **§S4**（B2B 種別なし screen_skip）→ **§S6**（記録）。

各 § 完了ごとに `python3 -m pytest _outreach_core/tests/ -q` を緑にしてから次へ。
