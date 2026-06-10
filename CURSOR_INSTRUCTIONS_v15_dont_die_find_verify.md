# CURSOR 指示書 v15 — 落ちない・見つける・確かめる

> 正典は [`CURSOR_INSTRUCTIONS.md`](./CURSOR_INSTRUCTIONS.md)。本書はその差分指示（v15）。
> §3「守ってほしい不変項」は**全て継続**。本書は Fable による実運用ログ＋コード監査（2026-06-10）に基づく。

## 0. 調査サマリ（なぜこの4テーマか・証拠ベース）

torana-line-crm brief の実績（events.jsonl 1,933件 / skip_history 119件 / needs_attention 91件 / sent 60件）を集計した結果:

| 課題 | 実測 | 根本原因（コード上の位置） |
|---|---|---|
| 途中で落ちる | run 異常終了・stall 多数 | ① `stage_send` の per-lead ループ（run.py:4604〜）に **try/except が無い**＝1社の例外で全バッチ死亡。② `infer.py` の `oc_browser_json`/`oc_evaluate` が **timeout 無し subprocess.run**（→ **適用済み**、§R2参照）。③ send に**途中再開チェックポイントが無い** |
| リスト精度 | skip 119件中 **約79%が非contactフォーム誤分類**（recruit 37 / login 18 / no-textarea 20 / 予約 4） | `classify_form_type`（contact_url.py:170〜）がヒューリスティクスのみ。`is_error_page` に **http_status が一度も渡されていない**（run.py:559,594）。bootstrap に URL 検証なし |
| ドラフト精度 | 生成成功96%だが、フォーム制約（maxlength等）を知らずに生成→後段で切り詰め | `build_user_block`（run.py:798〜）が form_fields を渡さない。char_limit 強制が**生成後**（draft.py:68〜） |
| フォーム対応 | first submit 不明 19 / confirm submit 不明 18 / 想定外フィールド 32 / verify 誤判定 15 / iframe 未対応 | 2段階(confirm)までしか想定しないフロー判定（run.py:2749〜）。iframe フォームは検知のみで**中に入らない**（run.py:374〜）。verify はキーワード一致中心（verify.py:16〜） |

**ファネル実測: 投入177 → enrich 通過 約33% → 送信成功 約31%**。最大のボトルネックは送信ではなく**フォーム発見・分類（enrich）**。

## 1. 変更対象ファイル地図

| パス | v15 |
|---|---|
| `jp-form-outreach/run.py` | §R1 per-lead 隔離 / §R3 送信ジャーナル / §F4 JS待ち / §F5 http_status / §S1 wizard ループ |
| `_outreach_core/infer.py` | §R2 **適用済み**（確認のみ） |
| `_outreach_core/contact_url.py` | §F1 LLM分類フォールバック / §F2 候補拡張 / §F3 iframe |
| `_outreach_core/verify.py` | §V1 証拠多層化 / §V2 LLM tiebreak / §V3 可視性チェック |
| `_outreach_core/draft.py` + `jp-form-outreach/prompts/` | §L2 フォーム制約注入・事実グラウンディング |
| `_outreach_core/tests/` | 各§の純関数テスト（決定ロジックは必ず純関数に切り出す） |

実装順は **§R → §F → §V → §S → §L**（R が最優先。F がファネル最大改善）。

---

## 2. §R 信頼性 — 「1社の失敗でバッチを殺さない」

### §R1 per-lead 例外隔離（最重要）

`stage_send`（run.py:4604〜5259）と `stage_enrich`（run.py:490〜）の per-lead ループ本体を、
それぞれ `_send_one_target(d, ...) -> dict` / `_enrich_one_target(t, ...) -> dict` に**関数抽出**し、
ループ側で次のように包む:

```python
for di, d in enumerate(targets):
    try:
        result = _send_one_target(d, ...)
    except Exception as exc:  # noqa: BLE001 — per-lead isolation
        tb = traceback.format_exc()
        _emit_event("send.lead_crashed", stage="send", target_id=tid,
                    payload={"error": str(exc)[:200], "tb_tail": tb[-800:]}, trace_dir=trace)
        append_needs_attention(DATA_DIR, {...reason: "lead_crashed", error...})
        _close_tab_safely(cur_tab_id)
        continue
```

- `hb.end(...)` は **try/finally** でループ全体を包んで必ず呼ぶ（現状 run.py:5259 は正常系のみ）。
- `continue`/`break` 用の制御は result dict（`{"outcome": "sent"|"skipped"|"crashed", ...}`）で返す。
- 関数抽出は**機械的な移動**に留め、ロジック変更を混ぜない（diff レビュー可能性のため）。

### §R2 subprocess タイムアウト（適用済み・確認のみ）

`_outreach_core/infer.py` の `oc_browser_json` / `oc_evaluate` は `_run()`（240s 壁、
`OUTREACH_SUBPROC_TIMEOUT_SEC` で上書き可）経由に**修正済み**。回帰テストを1本追加:
`_run` を monkeypatch して両関数が `_run` を経由することを assert。

### §R3 送信ジャーナル（二重送信防止＋途中再開）

クラッシュ→supervisor 再起動で同じ drafts.jsonl を頭から再処理する際、
sent_history のみが防波堤で、**「最終 submit クリック後・verify 前」に落ちた社は二重送信リスク**。

- 最終 submit クリックの**直前**に `data/briefs/<id>/send_journal.jsonl` へ
  `{"target_id", "ts", "phase": "submit_attempted", "form_url"}` を append。
- verify 確定後に `{"phase": "verified", "outcome": ...}` を append。
- `stage_send` 冒頭の pre-filter（run.py:4573〜）を拡張: journal に
  `submit_attempted` があり `verified` が無い target は**送信せず needs_attention へ**
  （reason: `unverified_prior_attempt` — 人が履歴を見て判断）。

### §R 受け入れ条件

1. `_send_one_target` 内で例外を起こすモックで、バッチが**継続**し needs_attention に記録されること。
2. ループ途中で KeyboardInterrupt 以外の例外が起きても `hb.end` が呼ばれること。
3. journal が `submit_attempted` のみの target は再実行時に送信されないこと（純関数 `should_skip_resume(journal_entries, target_id)` をテスト）。

---

## 3. §F フォーム発見・分類 — ファネル最大の改善点（skip の79%）

### §F1 分類の二段化: ヒューリスティクス → 不確実時のみ LLM

`classify_form_type` は維持しつつ、**結果が曖昧なケースだけ** Sonnet（`model.form_analyzer_name`）に委ねる:

- 曖昧の定義（純関数 `classification_is_uncertain(kind, reason, fields)`）:
  `unknown_no_textarea` / reason が `heading mentions ...` だが textarea あり / フィールド数 0。
- LLM 入力: snapshot 先頭 4,000字 + フィールド一覧。出力は構造化 JSON
  `{"form_type": "contact|recruit|login|b2c_support|ir|reservation|other", "confidence": 0-1, "b2b_contact_hint_url": str|null}`。
- `confidence < 0.6` は従来結果を採用（LLM を最終判定者にしない）。
- `b2b_contact_hint_url`（ページ内に「法人のお客様はこちら」等のリンクを LLM が見つけた場合）は
  enrich の候補キューに追加。
- 失敗時（timeout/parse不能）は**従来結果へフォールバック**（不変項: LLM 失敗で止めない）。

### §F2 候補 URL の拡張（決定論側）

`common_contact_paths` に追加: `/contact-us`, `/contactus`, `/inquiry/`, `/contact/business`,
`/business/inquiry`, `/company/inquiry`, `/support/inquiry`, `/お問い合わせ`（percent-encoded も）。
さらに `https://<root>/sitemap.xml` を1回 fetch し、URL に `contact|inquiry|toiawase|otoiawase` を
含むエントリを候補上位に挿入（純関数 `extract_contact_urls_from_sitemap(xml_text) -> list[str]`）。

### §F3 iframe / 外部フォームサービス対応

run.py:374〜 で iframe 検知時に skip している箇所を変更:

- iframe の `src` を取得し、ホストが既知フォームサービス
  （`form.run`, `formrun`, `tayori.com`, `hsforms.com`/`hubspot`, `kintoneapp.com`, `formzu`, `secure-link`, `synergy`, `formok`, `ssl-form` 等 — 定数リスト化）
  または同一 registrable domain なら、**iframe src を form_url として再 enrich**。
- 取れない/別物なら従来どおり `category: iframe` で needs_attention。

### §F4 JS レンダリング待ち

`_FORM_FIELDS_JS` 実行結果が **フィールド0件** かつページ本文が薄い場合、3秒 wait → 再snapshot を
**1回だけ**行う（SPA/遅延レンダリング対策）。再試行しても0件なら従来フロー。

### §F5 http_status を is_error_page に渡す

`is_error_page` は既に `http_status` 引数を持つが**全呼び出しで未使用**（run.py:559, 594）。
ページ open 後に `performance.getEntriesByType('navigation')[0].responseStatus`（取れない場合は null）
を `_evaluate` で取得し、渡す。

### §F 受け入れ条件

4. `classification_is_uncertain` の真偽がテーブルテストで固定されること。
5. LLM 分類が parse 不能のとき従来分類が返ること。
6. sitemap 抽出・iframe ホスト判定が純関数テストで固定されること。
7. 既存 `test_contact_url.py` / `test_enrich_v13.py` が**無修正で**通ること（後方互換）。

---

## 4. §V 送信検証 — 「送れたつもり」を無くす（誤判定15件）

### §V1 証拠の多層化＋必ず痕跡を残す

`verify_send_completed` の判定を重み付きスコアに再構成（純関数）:

| シグナル | 重み |
|---|---|
| URL が success 風（thanks/complete 等） | +2 |
| 成功キーワード（**フォーム領域の外**のテキストで） | +2 |
| 送信前に存在した form/textarea が**可視状態で消えた**（§V3） | +2 |
| エラーキーワード可視 | −3 |
| 入力フォームがそのまま残存 | −2 |

スコア ≥3 → sent_ok、≤−2 → failed、中間 → uncertain（§V2へ）。
**sent_ok でも** `post_submit_evidence.txt`（URL+title+本文先頭）を trace に必ず保存
（現状は needs_attention 時のみ。誤陽性の事後監査ができない）。

### §V2 uncertain のみ LLM tiebreak

uncertain のとき**だけ** Sonnet に page evidence（URL/title/本文 4,000字）を渡し
`{"verdict": "sent|not_sent|unclear", "quote": "判断根拠の原文引用"}` を取得。
`quote` が実際に本文に含まれない場合は採用しない（幻覚ガード）。
unclear/失敗 → 従来どおり needs_attention。**verify の一次判定は決定論のまま**（不変項）。

### §V3 フォーム消失は「可視性」で判定

DOM 存在チェックを `offsetParent !== null` / `getComputedStyle().display` ベースに変更。
`display:none` で隠れて待機中（「確認中…」等）のフォームを「消えた」と誤認しない。

### §V 受け入れ条件

8. スコアリング純関数のテーブルテスト（成功/失敗/uncertain 境界）。
9. quote 不一致の LLM 応答が棄却されること。
10. sent_ok パスで trace に post_submit_evidence が書かれること。

---

## 5. §S 想定外フォーム — wizard と特殊フィールド

### §S1 多段 wizard の一般化（confirm 二値からの脱却）

`flow: single|confirm` の二値を維持しつつ、send 本体を**ステップループ**化:

```
for step in range(MAX_FORM_STEPS=4):
    再scan → 未入力 required を埋める（既存 fill + guardrails 再利用）
    成功キーワード/フォーム消失 → break（verifyへ）
    次へ/確認/送信 ボタンを click（既存 3層探索を流用）
    click 不能 → 既存 resolver 行きロジック
```

既存の confirm フロー（2段）はこのループの特殊ケースとして吸収。`MAX_FORM_STEPS` 超過は
needs_attention（reason: `wizard_too_deep`）。

### §S2 フィールド対応の追加

- `type="date"` を `_FORM_FIELDS_JS` の収集対象に含め、fill plan で `preferred_contact_date` 系は
  「7営業日後」を ISO 形式で入れる（純関数 `default_date_value(today)`）。
- 住所分割: SENDER_FIELD_PATTERNS に 都道府県/市区町村/番地/建物 のパターンを追加し、
  brief の `sender.address` を純関数 `split_jp_address(addr)` で分割。
- チェックボックス選別（submit_progress.py:74〜）: required でなく、ラベルに
  `メルマガ|ニュースレター|案内を希望|配信` を含む任意チェックは**チェックしない**。

### §S 受け入れ条件

11. wizard ループが 2段 confirm の既存テスト（test_jp_form_send.py 等）を**無修正で**通すこと。
12. `split_jp_address` / `default_date_value` / メルマガ非チェックの純関数テスト。

---

## 6. §L リスト・ドラフト品質

### §L1 bootstrap 検証

`stage_bootstrap`（run.py:176〜）に追加:

- URL 形式検証（scheme/netloc 必須、`_normalize_http_url` 再利用）→ 不正は `invalid_url` で skip 記録。
- **registrable domain 単位**の dedup（sent_history / skip_history / 同一バッチ内）。
  `same_registrable_domain` を再利用し、trailing slash / query 差を吸収。

### §L2 ドラフトへのフォーム制約注入＋事実グラウンディング

- `build_user_block`（run.py:798〜）に enriched の `form_fields` から
  textarea の `maxlength`（あれば）と「お問い合わせ種別の選択肢一覧」を追加 →
  **生成時点で**文字数上限・文脈に適合させ、`enforce_char_limit` の事後切り詰めを例外化。
- system プロンプト（prompts/）に追記: 「企業固有の事実は `direct_signals` / `hook_context` に
  **書かれているものだけ**使う。無い場合は業界一般の課題提起に留める。憶測の実績・数値は禁止」。
- 直近送信 N=20 件との**冒頭1文の重複チェック**（純関数、正規化後の編集距離 or 共通prefix長）→
  類似時は refine を1回強制。

### §L 受け入れ条件

13. 不正 URL / 重複 domain が leads.jsonl に入らないこと。
14. maxlength 付きフォームで生成ドラフトが上限内であること（モック LLM）。
15. 既存 `test_draft.py` 系が通ること。

---

## 7. 優先順位と分割 PR

| PR | 内容 | 期待効果 |
|---|---|---|
| 1 | §R1 + §R3（§R2 は適用済みの確認） | 「途中で落ちて全滅」の根絶・二重送信防止 |
| 2 | §F1〜F5 | skip 79%（誤分類）の大幅削減 = 送達数の直接増 |
| 3 | §V1〜V3 | 送信完了精査の誤判定15件タイプの解消 |
| 4 | §S1〜S2 | wizard/想定外フィールドの取りこぼし回収 |
| 5 | §L1〜L2 | リスト・ドラフト精度の底上げ |

各 PR で `python -m pytest _outreach_core/tests/ -q` 全通過を必須とする。
LLM 追加箇所（§F1/§V2）は必ず「決定論フォールバック」「呼び出し回数の event 記録」をセットで実装すること。
