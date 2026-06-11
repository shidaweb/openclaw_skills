# CURSOR 指示書 v23 — 進まない送信を見抜く・描画されないフォームに入る

> 正典は [`CURSOR_INSTRUCTIONS.md`](./CURSOR_INSTRUCTIONS.md)。本書はその差分指示（v23）。
> §3「守ってほしい不変項」は**全て継続**。本書は 2026-06-11 の torana-line-crm 実運用ログ＋コード監査に基づく。

## 0. 調査サマリ（証拠ベース）

2026-06-11 のリゾルバキュー入り 2 件は、**別々の構造的バグ**の代表例:

| 事例 | 症状（ログ） | 真の原因（コード上の位置） |
|---|---|---|
| bulk_homme（company.bulk.co.jp/contact） | `multi-step form exceeded 4 steps`。だが実態は単一フォーム。fill=10/unfilled=19、Turnstile widget あり、ボタン候補に「PRIVACY POLICYに同意の上送信します。」 | `_advance_wizard_steps`（run.py:5134〜）は**クリック回数を数えるだけで、ページが進んだかを見ていない**。送信が却下され（必須未入力 or 同意未チェック or Turnstile token 未発行）同じページに留まる → 同じボタンを4回押して `wizard_too_deep` と誤報告 |
| mtg_inc（mtg2.my.site.com — Salesforce Experience Cloud） | `state=gate_like, inputs=1, textareas=0, buttons=0`、fill 0/19（body textarea fill failed）、submit 候補 0 | LWC は **shadow DOM 内に描画**される。run.py の DOM 走査は素の `document.querySelectorAll`（51箇所）、_outreach_core 側 9箇所も同様 → **shadow root の中が一切見えない**。さらに redirect 先 SPA の描画完了を待たない |

同根でキューに滞留中の類例: studio_alice（kintoneapp = React SPA、confirm submit not found）、aoki_hd（Visualforce `id:form:contactRadio`）。**§B の修正でこれらも解消が見込める**。

## 1. 変更対象ファイル地図

| パス | v23 |
|---|---|
| `_outreach_core/submit_progress.py` | §A1 `page_fingerprint` / `is_same_step` / `diagnose_no_progress`（純関数） |
| `jp-form-outreach/run.py` | §A2 wizard ループに進捗判定 / §A3 Turnstile token 待ち / §A4 同意ラベル除外 / §B3 deep 走査への置換 |
| `_outreach_core/dom_deep.py`（**新規**） | §B1 shadow DOM / iframe 貫通の共有 JS |
| `_outreach_core/contact_url.py` | §B2 SPA ホストの再走査ポリシー |
| `_outreach_core/verify.py` | §B3 `FORM_VISIBILITY_JS` / `PAGE_EVIDENCE_JS` の deep 化（LLM 呼び出し禁止は維持） |
| `_outreach_core/tests/` | §C 各純関数のテスト |

実装順は **§A → §B → §C**（§A は誤診断の止血、§B がファネル改善）。

---

## 2. §A 「進まないステップ」の検知 — クリック回数ではなく進捗を数える

### §A1 ページフィンガープリント（純関数, submit_progress.py）

```python
def page_fingerprint(url: str, page_text: str, counts: dict) -> str:
    """url(クエリ除去) + 本文先頭2000字の sha1 + フィールド署名(inputs/textareas/buttons/checkboxes数)"""

def is_same_step(fp_before: str, fp_after: str) -> bool: ...
```

### §A2 wizard ループの進捗判定（run.py `_advance_wizard_steps`）

各クリックの前後でフィンガープリントを取り、**変化しなければステップとして数えない**。
同一フィンガープリントが2回続いたら即座にループを打ち切り、`diagnose_no_progress` で原因を特定する:

1. **インラインバリデーションエラーの収集**（新 JS）: `[aria-invalid="true"]` / `.error, .err, .invalid` の可視テキスト / 「必須」「入力してください」「選択してください」を含む可視要素 → ラベル付きで payload に列挙
2. **未チェックの必須/同意チェックボックス**: 既存 `pick_checkboxes_to_check` を再実行し残数を報告
3. **CAPTCHA token 未発行**（§A3）

needs_attention / resolve_queue の reason_class は `wizard_too_deep` ではなく **`submit_rejected_no_progress`** とし、payload に `{validation_messages, unchecked_gates, captcha_token, fingerprint_repeats}` を入れる。診断で直せるもの（同意チェック・必須 select 等）は**その場で1回だけ修復→再送信**してから諦める。

本物のウィザード（フィンガープリントが毎回変わる）が cap を超えた場合のみ従来どおり `wizard_too_deep`。

### §A3 Turnstile token 待ち

`kind=turnstile_widget, blocking=False` でも、**token 未発行のまま送信すればサーバ側で却下され §A2 の無進捗になる**。
送信クリック直前に `input[name="cf-turnstile-response"]`（deep 走査, §B1）の値を確認し、空なら最大 10 秒ポーリング。発行されなければ payload に `captcha_token: "missing"` を記録（既存 v18 のヒューリスティクスは維持）。

### §A4 同意ラベルを送信候補から除外

bulk_homme のボタン候補筆頭は「PRIVACY POLICYに**同意の上送信します。**」— これは同意 UI であって送信ボタンではない。
`_click_button` / `_click_button_with_gate_retry` の候補選定で、ラベルが `is_agreement_label`（submit_progress.py に既存）に一致する要素は**送信候補から除外し、ゲート（チェック対象）として扱う**。テスト必須（このラベル文字列をそのままフィクスチャに）。

---

## 3. §B 描画されないフォームに入る — shadow DOM / SPA 対応

### §B1 共有 deep 走査 JS（新規 `_outreach_core/dom_deep.py`）

1ファイルに JS 文字列として定義し、全 evaluator に注入する:

```js
// queryDeep(selector): document → 再帰的に shadowRoot → 同一オリジン iframe を貫通
// collectControlsDeep(): inputs/textareas/selects/buttons/checkboxes/radios を deep 収集
//   （Lightning: lightning-input / lightning-textarea / lightning-button も内部の
//    native 要素まで降りて収集する）
// setValueDeep(el, value): ネイティブ setter (HTMLInputElement.prototype.value 等) で代入し、
//   input / change を bubbles:true, composed:true で dispatch
//   （React / Vue / LWC が値を認識するために必須）
```

### §B2 SPA ホストの再走査ポリシー（contact_url.py）

現状 `empty_render` のみ1回再走査するが、mtg_inc は `gate_like`（inputs=1）で素通りした。純関数で判定を追加:

```python
def should_rewait_render(state: str, counts: dict, original_url: str, final_url: str) -> bool:
    """gate_like/no_form かつ (リダイレクトでホストが変わった or final_url が既知 SPA ホスト
    [my.site.com, force.com, kintoneapp.com, form.run, hsforms.com 等]) → True"""
```

True なら deep 走査（§B1）で 1 秒間隔・最大 12 秒、コントロール数が安定するまでポーリングしてから分類し直す。

### §B3 既存走査の deep 化

フィールドスキャン / fill（`_fill_textarea` 等。run.py:4448 の `body textarea fill failed` 経路）/ ボタン探索 / `FORM_VISIBILITY_JS` / `PAGE_EVIDENCE_JS` を §B1 の共有 JS 経由に置換。**51箇所の querySelectorAll を個別に直すのではなく、共通ヘルパー注入に寄せる**こと。

shallow で 0 件・deep で発見できた場合は `send.deep_dom_used` イベントを emit（効果測定用）。

---

## 4. §C テスト（純関数・ブラウザ不要）

- `page_fingerprint` / `is_same_step`: 同一ページ・微小差分・別ステップの3ケース
- `diagnose_no_progress`: validation あり / gate 残り / token missing → reason 分岐
- 同意ラベル除外: 「PRIVACY POLICYに同意の上送信します。」「個人情報の取扱いに同意する」が送信候補に**ならない**、「この内容で送信」はなること
- `should_rewait_render`: mtg_inc 実値（gate_like, inputs=1, mtg.gr.jp→mtg2.my.site.com）で True、通常静的ページで False
- dom_deep の JS 契約: 文字列に `shadowRoot` / `composed: true` / native setter が含まれること（スモーク）

## 5. 受け入れ基準

1. `python3 -m pytest _outreach_core/tests/` 全 pass（既存 556+ を壊さない）
2. `run.py resolve-queue --brief torana-line-crm` で再試行し:
   - bulk_homme: `wizard_too_deep` ではなく具体的診断（または送信成功）になる
   - mtg_inc: deep 走査で textarea・送信ボタンを検出し、本文 fill が成功する
3. 正典 §3 の不変項（JSONL append-only、verify.py に LLM なし、モデルピン留め、`oc_browser()` シグネチャ等）を全て維持
