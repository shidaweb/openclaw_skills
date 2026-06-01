# CURSOR 指示書 v8 — リサーチ品質（form_url 選定）の向上

> 正典は [`CURSOR_INSTRUCTIONS.md`](./CURSOR_INSTRUCTIONS.md)。本書はその差分指示（v8）。
> §3「守ってほしい不変項」は**全て継続**（6フェーズ名・JSONL名・append-only履歴・browser profile
> `openclaw` 固定・model ピン・`verify.py` で LLM を呼ばない・既存サブコマンド表面互換）。
> 対象スキルは `jp-form-outreach`。

## 0. 背景（実バッチログから判明した打率低下の真因）

新規10社バッチで **打率 2/10**。自動 skip 8件の内訳:
- **採用ページを form_url に指定** 7件（yamada_yohojo / monogatari_corp / sugi_hd / gakken_hd /
  royal_hd 以外 …）
- **textarea 無し** 1件（royal_hd）

**重要**: enrich の `_classify_form_type`（run.py 〜426）は採用ページ等を**正しく検出して skip**できて
いる。問題は**上流のリサーチ（agent-led list-build）が form_url に採用ページ等の誤URLを入れている**こと、
および **誤URLを踏んだら “正しい問い合わせフォームを探し直す” 自動補正が無い**こと。

→ 本書は **(R1) リサーチ仕様の厳格化** と **(R2) enrich での form_url 自動発見・補正** を二本柱とする。

---

## 1. 変更対象ファイル地図

| パス | 役割 | v8 での扱い |
|---|---|---|
| `jp-form-outreach/SKILL.md` | List build flow（agent-led 仕様） | §R1 大幅強化（form_url 選定基準＋検証手順） |
| `jp-form-outreach/prompts/list_build.md` | **新規** リスト生成用プロンプト（中立テンプレ） | §R1（`*.local.md` 上書き可、固有名詞を書かない） |
| `_outreach_core/contact_url.py` | **新規・純関数** 連絡先URL候補生成＋分類補助 | §R2/§R3 ロジック分離（テスト可能） |
| `jp-form-outreach/run.py` | `stage_enrich` / `_classify_form_type` | §R2（自動発見・補正）, §R3（分類精度） |
| `_outreach_core/helpers/report.py` | レポート | §R4 リサーチ品質レポート追加 |
| `_outreach_core/helpers/append_targets.py` | targets 追記 | §R5 複数候補URL対応（追加のみ） |
| `_outreach_core/tests/` | テスト | 各 § の受け入れテスト |

> 既存の `_classify_form_type` / `_NON_CONTACT_HEADING_KW`（run.py 〜418）、`_FORM_FIELDS_JS` の
> textarea 検出、`enrich.form.skipped_non_contact` イベントを再利用・強化する。**新規の重い専用 CLI は作らない。**

---

## 2. §R1 リサーチ仕様の厳格化（form_url 選定基準）

**狙い**: 「会社が見つかった」ではなく「**B2B 営業・取引のお問い合わせフォームURL** が検証済みで取れた」
を採用条件にする。

### R1-1. form_url の定義を厳格化（SKILL.md「List build flow」を全面拡充）
- form_url は **B2B（営業・取引・提携・取材）向けの、自由記述 textarea を含むお問い合わせフォーム**の
  URL に限る。
- **明示的に除外**（form_url に使わない。該当しかなければ `category` を立てて B2B 不可として扱う）:
  - 採用 / recruit / career / 求人 / エントリー / 応募 / 新卒・中途
  - IR / 投資家情報 / 株主 / 適時開示
  - B2C カスタマーサポート / お客様相談室 / 製品の使い方 / 修理・返品
  - 予約 / 来店予約 / 見学予約 / カウンセリング予約
  - 資料請求・ダウンロード（ゲート目的）/ メルマガ登録 / 会員登録 / ログイン
- **URL ヒューリスティック**（優先度の参考。最終判断は実ページ確認）:
  - 望ましい: `/contact`, `/inquiry`, `/toiaw-?ase`, `/company/contact`, `/business`, `/form`, `/otoiawase`
  - 避ける: `/recruit`, `/career(s)`, `/saiyo`, `/entry`, `/ir`, `/support`, `/faq`, `/reserve`, `/yoyaku`

### R1-2. 採用前の**実ページ検証を必須化**（agent 手順）
- リスト生成エージェントは form_url を targets に書く前に、**そのページを実際に開いて**
  「自由記述の本文 textarea があり、採用/IR/B2C/予約 等の非該当ページでない」ことを確認する。
- 確認できたら `form_url_verified: true` を付す。確認できなければ form_url を**空にして** `category` で
  理由を残す（捏造・推測URLの書き込み禁止）。

### R1-3. `prompts/list_build.md`（新規・中立テンプレ）
- 上記基準・除外・ヒューリスティック・検証手順を**プロンプト化**して同梱（固有名詞・実URLは書かない）。
- 各クライアントは `list_build.local.md` で上書き可（`_prefer_local` と同方式。`.local.md` は git 管理外）。

### §R1 受け入れ条件
1. SKILL.md「List build flow」に form_url 定義・除外リスト・URLヒューリスティック・**実ページ検証必須**が明記される。
2. `prompts/list_build.md` が中立（固有名詞ゼロ）で存在し、`.gitignore` に `**/prompts/*.local.md` が含まれる（既存）。

---

## 3. §R2 enrich での form_url 自動発見・補正（最重要・コード）

**狙い**: 誤URL（採用ページ等）を踏んでも**即 skip せず、同一ドメインの正しい問い合わせフォームを探し直す**。
これが打率を最も上げる。

### R2-1. 連絡先URL候補生成（`contact_url.py`・純関数）
- `contact_link_candidates(page_links, base_url) -> list[str]`:
  - 現在ページ内のリンク（href＋表示テキスト）から、**お問い合わせ系**を抽出して優先度順に返す。
    - 採用対象テキスト: 「お問い合わせ」「お問合せ」「企業・法人のお問い合わせ」「ビジネスに関する」
      「取材・協業・提携」「コーポレート」「contact」「inquiry」
    - **除外**テキスト/href: 採用 recruit career 求人 応募 entry / IR 投資家 / よくある質問 faq /
      予約 reserve / サポート support / ログイン login。
  - 重複排除・絶対URL化して返す。
- `common_contact_paths(base_url) -> list[str]`:
  - ドメイン直下の定番パス `/contact`, `/contact/`, `/inquiry`, `/toiawase`, `/otoiawase`,
    `/company/contact`, `/business/contact`, `/form` を生成。

### R2-2. enrich 補正ループ（`stage_enrich`）
- `_classify_form_type` が **non-contact**（recruit/IR/unknown_no_textarea 等）を返したら、**skip する前に**:
  1. 現在ページのリンクから `contact_link_candidates` を生成（＋ `common_contact_paths`、合わせて**上限 N=5**）。
  2. 候補を順に `oc_browser("open", url)` → `_FORM_FIELDS_JS` → `_classify_form_type` で再判定。
  3. 最初に **contact**（自由記述 textarea あり）になった URL を採用し、`form_url` を差し替えて enrich 続行。
     - `t["form_url"] = corrected`, `t["form_url_corrected"] = True`, `t["form_url_original"] = 旧URL` を記録。
     - `enrich.form.url_corrected` イベントを emit。
  4. どれも contact にならなければ、従来どおり `non_contact_form` で skip（reason に「補正試行 K件 失敗」を付す）。
- **多重オープン上限**と**ドメイン外への遷移禁止**（同一 registrable domain のみ）を必ず守る（誤遷移・無限探索の防止）。

### §R2 受け入れ条件
3. `contact_url.py` は純関数ユニットテストで網羅:
   - 採用リンク・IRリンク・FAQ・予約は**候補から除外**され、「お問い合わせ」「企業のお問い合わせ」「contact」は**採用**される。
   - `common_contact_paths` が base_url の registrable domain 配下のみを返す（外部ドメインを生成しない）。
4. enrich 補正の統合テスト（モック oc_browser/_evaluate）: 1回目 recruit→2回目 contact のダミーで、
   `form_url` が補正され `form_url_corrected=True`・`enrich.form.url_corrected` emit、draft 対象に残ること。
5. 同一ドメイン縛り: 候補に外部ドメインURLが混じっていても**開かない**こと（テストで確認）。

---

## 4. §R3 非contact分類器の精度向上（`_classify_form_type`）

**狙い**: 誤判定（取りこぼし・誤通過）を減らす。

- **IR / 投資家** を `_NON_CONTACT_HEADING_KW` に追加（「IR」「投資家情報」「株主」「適時開示」）。
- **B2C サポート** を追加（「お客様相談室」「カスタマーサポート」「修理」「返品」）。ただし B2B 文脈
  （「法人」「取引」「協業」）が併存する場合は contact 寄りに残す。
- **資料請求/DLゲート** を追加（「資料請求」「資料ダウンロード」のみで本文 textarea が無い）。
- **textarea の真正性チェック**: 「自由記述の本文欄」であることを要件化。
  - textarea の name/label に お問い合わせ内容/本文/ご相談/内容/メッセージ/message/inquiry 等を含む、
    または「name/email を持つフォーム内の最大 textarea」であること。**サイト内検索 textarea**（検索/search）
    は本文欄とみなさない。
- **recruit + textarea の誤通過を修正**: 現状は非contact見出し検出時に textarea があると `continue`（通過）して
  しまう。**強い recruit シグナル**（採用見出し ＋ 応募/エントリー/履歴書 系フィールド）がある場合は、
  textarea があっても `recruit` と判定する。

### §R3 受け入れ条件
6. ダミー fields/snapshot で: IR・B2Cサポート・資料請求DL・サイト内検索textarea が**非contact**に、
   「お問い合わせ内容」textarea を持つ法人問い合わせは **contact** に分類されること（純関数テスト）。
7. 「採用見出し＋応募 textarea」のダミーが **recruit** と判定されること（誤通過の回帰テスト）。

---

## 5. §R4 リサーチ品質の計測（`report.py`）

**狙い**: 打率をバッチ単位で可視化し、改善を定量で追えるようにする。

- `report research-quality --since <range>` を追加:
  - **form_url 妥当率** = contact 判定 / enrich 試行
  - **誤URL率** = `non_contact_form` skip / enrich 試行（カテゴリ別: recruit / IR / B2C / no_textarea）
  - **補正成功率** = `enrich.form.url_corrected` / 補正試行
  - **打率** = 送信成功 / 新規対象
- 出力は events（`enrich.form.skipped_non_contact` / `enrich.form.url_corrected`）＋ sent/skip history から集計。

### §R4 受け入れ条件
8. ダミー events で `research-quality` が誤URL率・補正成功率・打率を正しく集計すること（純関数集計部のテスト）。

---

## 6. §R5 targets スキーマ: 複数候補URL（追加のみ・任意）

- `targets.yaml` の各社に **`contact_url_candidates: [url, ...]`**（任意）を許可。`form_url` 未確定でも
  候補を複数渡せる。enrich は `form_url` → `contact_url_candidates` の順で R2 の検証を行い、最初の有効URLを採用。
- `append_targets.py` は候補配列を**追加カラムとして**取り込む（既存カラムは不変）。

### §R5 受け入れ条件
9. `form_url` 空 ＋ `contact_url_candidates` 2件のダミーで、enrich が候補を順に検証して有効URLを採用すること。

---

## 7. 守ってほしい不変項（v8 追加分）

1. `targets.yaml` / `enriched.jsonl` の**カラム削除・リネーム禁止**（`form_url_corrected` / `form_url_original` /
   `contact_url_candidates` 等の**追加のみ**可）。
2. enrich の URL 探索は **同一 registrable domain 内・オープン上限付き**（誤遷移・無限探索の禁止）。
3. **捏造・未検証URLを form_url に書かない**（R1-2）。疑わしきは空＋category。
4. `_classify_form_type` の強化は**誤通過を増やさない**（採用/IR/B2C を contact に通さない方向のみ強化）。
5. 判定ロジックは**純関数モジュール（`contact_url.py`）＋ユニットテスト**に寄せる。`verify.py` は不変。

## 8. やらないこと

- ❌ 外部ドメインへの自動巡回（同一ドメイン内の問い合わせフォーム探索のみ）。
- ❌ 実在しない企業/URLの捏造。
- ❌ enrich での無制限なページオープン（上限必須）。
- ❌ `_classify_form_type` を LLM 化（純 Python 維持）。

## 9. 実装順（推奨）

1. **§R2**（enrich の form_url 自動発見・補正）— 打率を最も上げる。`contact_url.py` ＋ enrich 補正ループ。
2. **§R3**（分類器の精度）— 誤判定削減。R2 の再判定品質も上がる。
3. **§R1**（リサーチ仕様・プロンプト厳格化）— 誤URLの発生源を絞る。
4. **§R4**（品質レポート）→ **§R5**（複数候補URL）。

各 § 完了ごとに `python3 -m pytest _outreach_core/tests/ -q` を緑にしてから次へ。
