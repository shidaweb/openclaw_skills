# CURSOR 指示書 v13 — 有効フォームを捨てて推測パス(/contact 等)に行かない＋404検知

> 正典は [`CURSOR_INSTRUCTIONS.md`](./CURSOR_INSTRUCTIONS.md)。本書はその差分指示（v13）。
> §3「守ってほしい不変項」は**全て継続**。v8（form_url 補正）/ v10（種別選択）を**前提・修正対象**とする。
> 対象 `jp-form-outreach`。

## 0. 背景（jp-holdings 実フォーム）

`https://www.jp-holdings.co.jp/inquiry/` は **有効な B2B 問い合わせフォーム**（自由記述 textarea
「詳細をお書きください」＋ 問い合わせ種別 pulldown ＋ 送信）。期待挙動は **「このフォームに留まり、
種別を“その他”にして送信」**。

しかし実挙動は:
1. enrich が `/inquiry/` を開き `_classify_form_type` → **「非contact」と誤判定**。
2. 非contact なので候補探索（`_build_contact_candidates` → `common_contact_paths`）で **`/contact` を開く → 404**。
3. 404 ページも非contact → 最終的に `/inquiry/` が **`non_contact_form` で skip**。

**= 有効フォームを誤判定で捨て、推測パス /contact を追いに行って 404、結局 skip**。

**現状コードの問題点**（run.py `stage_enrich` 524–650 / `_classify_form_type` / `contact_url.py`）:
1. **分類が厳しすぎ**: textarea＋submit を持つ実フォームでも、種別 pulldown に明確な B2B 選択肢が無い等で
   `contact` 判定にならず**非contact扱い**になる。
2. **有効フォームでも候補探索に進む**: seed が「使えるフォーム」でも、`contact` 判定でないと
   `/contact` 等の推測パスを開く（**良いフォームを離れる**）。
3. **404/エラーページ検知が無い**（コードに 404/Not Found 判定なし）。404 を「ただの非contactページ」として
   扱い、無駄遷移＋既知の良フォームを失う。
4. seed が非contact 確定でも、**種別「その他」フォールバックで送れる**ケースを試さず skip。

→ 本書は **(U1) 404/エラー検知**、**(U2) 有効フォームを捨てない探索制御**、**(U3) 分類の緩和**、
**(U4) 種別“その他”で送信続行** を指示する。

## 1. 変更対象ファイル地図

| パス | 役割 | v13 |
|---|---|---|
| `_outreach_core/contact_url.py` | URL 候補/分類（純関数） | §U1（`is_error_page`）, §U3（分類緩和） |
| `jp-form-outreach/run.py` `stage_enrich` | seed/候補探索ループ（524–650） | §U1/§U2 配線（404 guard・known-good 復帰） |
| `_outreach_core/submit_progress.py` | 種別選択（既存 `choose_b2b_option` / その他） | §U4 その他フォールバック確実化 |
| `_outreach_core/tests/` | テスト | 各 § |

## 2. §U1 404/エラーページ検知（純関数＋遷移ガード）

**狙い**: どの URL を開いても **404/エラーは“フォーム無し”ではなく“無効遷移”として扱い、絶対に採用しない**。

- `contact_url.py` に純関数 `is_error_page(snapshot, url=None, http_status=None) -> bool`:
  - `http_status` が 4xx/5xx なら True（取得できる場合）。
  - snapshot/テキストに **「404」「Not Found」「ページが見つかりません」「お探しのページ…見つかり…」
    「存在しません」「Page not found」「Forbidden」「アクセスできません」** 等を含めば True。
  - `<title>` が 404/エラー系でも True。
- `stage_enrich` の **seed/候補を開いた直後**に `is_error_page` を判定:
  - **エラーなら**その URL を**候補から即除外**（`form_kind` 判定に進めない）。`enrich.nav.error_page` を emit。
  - 可能なら `oc_browser` の HTTP ステータス/`responsebody` でステータスも取得（取れなければテキスト判定のみ）。

### §U1 受け入れ条件
1. 「404 / ページが見つかりません」を含む snapshot で `is_error_page` が True、通常フォームで False（純関数テスト）。
2. 候補 `/contact` が 404 のダミーで、enrich が**その候補を採用せず**、`enrich.nav.error_page` を emit すること。

## 3. §U2 有効フォームを捨てない（探索制御＋known-good 復帰）

**狙い**: **使えるフォームがあるのに推測パスへ移動しない**。移動しても 404/失敗なら**良フォームに戻る**。

- **best-known-good** を保持: seed/候補で **`contact`（＝使えるフォーム）と判定された URL ＋その fields/snapshot**
  を保存。
- 探索（候補 probing）に進むのは **seed が「使えるフォームでない」場合のみ**（§U3 の緩和後）。
  - seed が `contact` なら**即 break**（候補探索しない）。
- 候補を開いて **404/エラー（§U1）または非contact** だったら、**best-known-good に DOM を戻す**
  （`oc_browser("open", best_url)` で復帰）。**良フォームを失わない**。
- 探索を全部試しても改善しなければ、**best-known-good（あれば）で enrich/draft/send を続行**。
  best-known-good が無い時のみ `non_contact_form` skip。
- 探索の**オープン上限**（既存 5 件）と**同一 registrable domain 限定**は維持。

### §U2 受け入れ条件
3. seed が `contact` のダミーで、**候補探索（/contact 等）が一切呼ばれない**こと。
4. seed 非contact → 候補1が 404、候補2が valid contact のダミーで、**候補2を採用**し、404 候補で best-known-good に
   戻る挙動になること（モック oc_browser）。
5. seed が valid だが種別だけ未充足のダミー（§U3）で、**/contact に移動せず**その seed で続行すること。

## 4. §U3 分類の緩和（textarea＋submit は contact）

**狙い**: 「種別 pulldown に明確な B2B 選択肢が無い」だけで**有効フォームを非contactにしない**。

- `classify_form_type`（contact_url.py / `_classify_form_type`）を緩和:
  - **自由記述 textarea ＋ 送信系コントロール（submit/確認）があり、recruit/IR/予約/B2C専用 等の
    強い非contactシグナルが無ければ `contact`**。
  - 種別 pulldown が placeholder（「選択してください」）や B2B 選択肢不明でも**それだけで非contactにしない**
    （送信時に §U4「その他」で対応）。
- 非contact に落とすのは v8/§E の**明確な失格シグナル**（採用/IR/予約/B2C専用/営業お断り/textarea無し）に限定。

### §U3 受け入れ条件
6. 「textarea＋種別pulldown（placeholder）＋送信ボタン、recruit/IR等のシグナル無し」のダミーが
   **`contact` に分類**されること（jp-holdings 型回帰）。
7. 「採用見出し＋応募 textarea」「textarea 無し」は引き続き**非contact**であること（誤通過の回帰）。

## 5. §U4 種別「その他」フォールバックで送信続行

**狙い**: 種別 pulldown に強い B2B 選択肢が無い場合、**「その他」を選んで送信まで進む**（諦めない）。

- 既存 `submit_progress.choose_b2b_option` の「その他」優先（〜140/204行）を**確実化**:
  - B2B prefer マッチが無く、**`その他/その他のお問い合わせ/上記以外` が存在すれば、それを選ぶ**
    （placeholder は除外）。`confidence="low"`, `reason="fallback_sonota"` を付す。
  - 「その他」も無い場合のみ `None`（§S4 screen_skip 相当）。
- fill 段（v10 §S1）でこの値を適用し、**送信を継続**（種別が原因で送信に到達しないことを無くす）。

### §U4 受け入れ条件
8. options=「選択してください/個人のお客様/その他」で `choose_b2b_option` が **「その他」**を返すこと。
9. jp-holdings 型統合（valid form → 種別=その他 → 同意 → 送信）で**送信に到達**すること（LLM/oc_browser モック）。

## 6. 守ってほしい不変項（v13 追加分）

1. **有効な contact フォームを離れない**。離れた先が 404/非contact なら**必ず best-known-good に復帰**。
2. **404/エラーページを採用しない・留まらない**（`is_error_page` で除外）。
3. 分類の緩和は**誤通過を増やさない**（recruit/IR/予約/B2C専用/textarea無し は従来どおり非contact）。
4. 探索は**同一 registrable domain・オープン上限**を維持（誤遷移・無限探索の禁止）。
5. `enriched.jsonl`/イベントのカラムは**追加のみ**（`nav_error`/`best_known_url` 等）。判定核は**純関数＋テスト**。

## 7. やらないこと

- ❌ 使えるフォームがあるのに `/contact` 等の推測パスへ移動する。
- ❌ 404/エラーページを「フォーム無し」として通常処理する。
- ❌ 種別pulldownのB2B選択肢不在“だけ”で有効フォームを skip する。
- ❌ 外部ドメイン巡回／無上限探索／`verify.py` の LLM 化。

## 8. 実装順（推奨）

1. **§U3**（分類の緩和：textarea＋submit=contact）— jp-holdings の誤判定（=トリガ）を直接解消。
2. **§U2**（有効フォームを捨てない探索制御＋known-good 復帰）— /contact 追跡をそもそも止める。
3. **§U1**（404/エラー検知）→ **§U4**（その他フォールバックで送信続行）。

各 § 完了ごとに `python3 -m pytest _outreach_core/tests/ -q` を緑にしてから次へ。
```
要約: 「有効フォームを誤判定で捨て /contact へ→404→skip」を、
(U3) 分類を緩めて valid form を valid と認める →
(U2) valid なら推測パスへ行かない／行っても 404 なら良フォームに戻る →
(U1) 404 を検知して採用しない →
(U4) 種別は『その他』で送信まで進める、の4点で全般的に修正する。
```
