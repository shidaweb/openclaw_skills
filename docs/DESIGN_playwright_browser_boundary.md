# 設計書 — ブラウザ境界の Playwright 直結化（v21 設計）

> **実装状況（2026-06-11）**: Phase 0（seam＋OpenClawアダプタ・既定）と Phase 1
> （Playwrightアダプタ本実装・フラグopt-in）を実装済み。
> - `_outreach_core/adapters/`（base / openclaw_browser / playwright_browser / __init__）
> - `run.py` のブラウザ境界（`oc_browser` / `_evaluate` / tab関数）をアダプタ経由に配線。
>   **既定は `openclaw` で挙動は完全不変**。
> - 切替: `DOORMAN_BROWSER_BACKEND=playwright`（env）または `browser.backend: playwright`（config）。
> - 残: Phase 2–3（enrich→send の実ブラウザ突き合わせ検証）、snapshot案Aの本番調整、進捗可視化。
>   実機にPlaywright未導入なら `pip install playwright && python -m playwright install chromium`。


> 目的: 「落ちる・止まる・進捗が見えない」の主因である
> 「1アクション毎に `openclaw` CLI を spawn → 共有ゲートウェイ → Playwright」
> という境界を、**Python が Playwright を in-process で直接駆動する**形に置き換える。
> OpenClaw は Slack 受付（と当面 infer）に降格させる。
>
> 本書は実装前の設計確定用。コードはまだ書かない。

---

## 0. 結論（先に方針）

- **コンセプトは維持**: 決定論パイプライン ＋ 曖昧部のみ LLM ＋ 実プロファイルのブラウザ。
- **置換するのは境界だけ**: ブラウザI/Oは `_outreach_core/infer.py` の4関数＋`run.py` のtab系に集約済み。ここを `BrowserAdapter` で抽象化し、`PlaywrightBrowserAdapter` を差し込む。
- **同期のまま**: `run.py` は async ゼロ（確認済み）。Playwright は **sync API**（`playwright.sync_api`）を使い、制御フローを書き換えない。
- **段階移行＋即ロールバック**: アダプタを config/env で切替。enrich の1社→数社→送信、と広げる。問題が出たら旧アダプタにフラグで戻す。

---

## 1. 現状の境界（実測した表面積）

`run.py` 6000行のうち、ブラウザに触るのは下記の薄い層だけ。**ここだけ差し替えれば上は不変**。

| 入口 | 実体 | 呼び出し数 | Playwright 対応 |
|---|---|---|---|
| `_evaluate(js)` | `core_infer.oc_evaluate` | 49 | `page.evaluate(js)` |
| `oc_browser("snapshot")` | a11yツリーのテキスト | 13 | §4 snapshot 戦略 |
| `oc_browser("open", url)` / `_open_tab` | `oc_browser_json("open")` | 7 / — | `context.new_page(); page.goto(url)` |
| `oc_browser("screenshot")` | PNG保存 | 1 | `page.screenshot(path=...)` |
| `oc_browser("focus"/"close", id)` | tab操作 | 2 | `page.bring_to_front()` / `page.close()` |
| `oc_browser_json("tabs")` | tab一覧 | 1 | `context.pages` |
| `oc_infer(prompt, model)` | LLM呼び出し | 4 | §6 当面 OpenClaw 維持 |

tab管理は `_open_tab / _focus_tab / _close_tab / _list_tabs_payload / _enforce_tab_cap`（run.py 4691–4760）に閉じ、payload整形は `core_tab_utils`。**tab = Playwright の `Page`** に1:1で対応づく。

snapshot テキストの消費者は **`verify.py` と `contact_url.py` の2モジュールのみ**（captcha は `_evaluate(LIVE_CAPTCHA_JS)` 経由でsnapshot非依存）。移行の最重要リスクはここ（§4）。

---

## 2. ターゲット・アーキテクチャ

```
[Slack] ──> OpenClaw gateway（受付/エージェント・1台のみ接続：v20 primary-host）
                 │  コマンド起動（./job, run.py）
                 ▼
        Doorman Python パイプライン（同期）
                 │  BrowserAdapter（seam）
                 ├──────────────┬───────────────────────
                 │ OpenClawBrowserAdapter   PlaywrightBrowserAdapter ← 新
                 │ （現状維持・即ロールバック用） │
                 │                              ▼
                 │                    Playwright sync（in-process）
                 │                    launch_persistent_context(user_data_dir)
                 │                              ▼
                 │                          Chromium（Doorman専用プロファイル）
                 └ oc_infer → OpenClaw infer（当面）
```

**ゲートウェイがブラウザのホットパスから消える** = 「180s no-output stall」型ハングの主因が構造的に消滅。

---

## 3. アダプタ境界（seam）

空の `_outreach_core/adapters/` に実装する。最小プロトコル:

```python
class BrowserAdapter(Protocol):
    def open(self, url: str) -> str: ...            # returns tab_id
    def goto(self, tab_id: str, url: str) -> None: ...
    def evaluate(self, js: str, *, tab_id: str | None = None) -> Any: ...
    def snapshot(self, tab_id: str | None = None) -> str: ...   # §4
    def screenshot(self, path: str, *, tab_id=None) -> None: ...
    def focus(self, tab_id: str) -> bool: ...
    def close(self, tab_id: str) -> None: ...
    def list_tabs(self) -> list[str]: ...
    def shutdown(self) -> None: ...
```

- **実装A `OpenClawBrowserAdapter`**: 既存 `oc_*` をそのまま包む（挙動完全不変）。
- **実装B `PlaywrightBrowserAdapter`**: 下記。
- 選択: `config.browser.backend = "openclaw" | "playwright"`（env `DOORMAN_BROWSER_BACKEND` 優先）。既定は当面 `openclaw`。
- `_evaluate` / `oc_browser` / tab関数を **アダプタ呼び出しに薄く付け替える**だけ（run.pyの上位ロジックは不変）。

### tab_id の扱い
- Playwright側は `Page` オブジェクトを `id(page)` か自前UUIDで採番し、`{tab_id: Page}` のレジストリを持つ。`core_tab_utils` の payload整形は Playwright用の薄い shim を1つ足せば再利用可。

---

## 4. snapshot 戦略（移行の最重要ポイント）

現状 `oc_browser("snapshot")` は **アクセシビリティツリーのテキスト**を返し、`verify.py`（成功キーワード/エラー判定）と `contact_url.py`（フォーム種別/error_page判定）と LLM フォーム解析がこれを読む。

Playwright の `page.accessibility.snapshot()` は **JSONツリー**で、文字列フォーマットが異なる。3案:

- **案A（推奨）**: アダプタ側で **innerText + 構造化DOM情報を1回のJSで吸い出す**統一スナップショットを返す。実は消費側の大半（verify/contact_url/captcha）は「本文テキストに特定キーワードが含まれるか」「input/textarea/button の数と属性」を見ているだけ。a11yツリーの体裁は本質的に不要。
  - 具体: `snapshot()` は `{text: body.innerText[:16000], controls: [...], buttons:[...], url, title}` を1回の `page.evaluate` で取得し、テキスト部分を返す。`classify_page_form_state`（v17で既にfields+textで判定）と整合。
- 案B: a11y JSON を旧テキスト体裁に整形する変換器を書く（互換重視だが工数大・脆い）。
- 案C: 消費側を innerText/DOM直読みに寄せる（v17で既に `_PAGE_TEXT_HEAD_JS` 等あり、流れは案Aと同じ）。

→ **案A＋必要箇所だけ案C**。移行時に verify.py / contact_url.py のスナップショット入力を「新統一snapshotのtext」に差し替え、回帰テスト（既存の verify/contact_url テスト群）で固定する。

---

## 5. 永続プロファイル（cf_clearance/cookie 維持）

- `launch_persistent_context(user_data_dir=<Doorman専用dir>, channel="chrome", headless=...)` で**実プロファイルを保持**。cookie・cf_clearance が回をまたいで残る → v18 のCloudflare回避ハイジーンと相性良（むしろ改善）。
- **OpenClawのプロファイルdirは共有しない**: 同一 user-data-dir を2プロセスで開くと Chromium がロック競合する。Doorman専用 `data/pw_profile/` を新設し独立運用 → OpenClawからブラウザ面で完全デカップリング。
- 初回ログインが要るフォームは基本ないが、必要時は手動で一度このプロファイルにログインさせる運用（手順を残す）。
- headless: 既存 `browser.headless` 設定（`infer.browser_headless_preference`）をそのまま流用。

---

## 6. OpenClaw の役割（移行後）

- **残す**: Slack 受付/エージェント（gateway。v20 で1台に固定済み）、当面 `oc_infer`（4箇所・低頻度・低リスク）。
- **外す**: ブラウザ駆動。
- infer は将来 Anthropic API 直叩きに寄せられるが、**今回はスコープ外**（一度に2つ変えない）。watchdog/supervisor（v14）はゲートウェイ監視として継続有効。

---

## 7. クラッシュ耐性（in-process の懸念と対策）

- 懸念: ブラウザが死ぬとPythonごと巻き込まれる？ → Playwright は別プロセスのChromiumをCDPで駆動。`BrowserContext`/`Page` の例外は捕捉可能。
- 対策:
  - `evaluate/goto` に **Playwrightネイティブの `timeout=`**（サブプロセスkillでない正攻法のタイムアウト）。
  - `context.on("close")` / `page.is_closed()` を監視し、切断時は **アダプタが context を自動再生成**（lazy re-init）。
  - v15 §R1（per-lead try/except）と §R3（送信journal/再開）が**そのまま効く** → 1社のブラウザ事故でバッチは死なない、再開も既存機構。
- 純益: 共有ゲートウェイという単一ハング点が消える分、現状より確実に堅くなる。

---

## 8. 同期モデル

- `run.py` は完全同期（async 0件・確認済み）。`playwright.sync_api.sync_playwright()` を**モジュール内シングルトンで lazy 起動**し、`_evaluate` 等から同期呼び出し。
- 注意点: sync API は実行中の asyncio ループ内では使えない。Doormanは同期CLIなので問題なし（要・実装時の確認事項に明記）。

---

## 9. 段階移行とロールバック

1. **Phase 0**: `BrowserAdapter` seam 導入＋`OpenClawBrowserAdapter`（挙動不変）。全テスト緑を確認。← リスクほぼゼロ
2. **Phase 1**: `PlaywrightBrowserAdapter` 実装（§3–5）。`evaluate/open/snapshot/tab` の単体テスト。【実装済み】
3. **Phase 2**: パリティ検証ハーネスで OpenClaw と Playwright の「見え方」を突き合わせる。【ツール実装済み】
   - 両バックエンドが動く実機で:
     ```bash
     # 実機にPlaywright未導入なら一度だけ:
     pip install playwright && python3 -m playwright install chromium
     # 過去に詰まったURLで突き合わせ（exit 0=一致 / 1=乖離）:
     python3 -m _outreach_core.tools.backend_probe https://www.gakkyusha.co.jp/contact/
     python3 -m _outreach_core.tools.backend_probe https://cart.duskin.jp/inquiry_co_jp?shop_cd=04 --json
     ```
   - 比較対象は**パイプラインが実際に判断に使う信号**: page-form-state（v17）、captcha kind/blocking（v18）、textarea/submit/radio 数。
   - 乖離が出たURLは snapshot 案A（§4）の調整対象。一致が揃ったら send へ。
4. **Phase 3**: send を Playwright で。**fill-only → 本番テストフォーム → 実送信**の順。v20 primary-host ガード下で安全に。
5. **Phase 4**: 既定を `playwright` に。OpenClawブラウザを停止。
- **ロールバック**: いつでも `backend=openclaw` に戻すだけ（旧アダプタは残置）。

---

## 10. 進捗可視化（本移行が前提を整える）

- 現状 `events.jsonl`＋v17タイムライン（1社単位）はある。足りないのは **run全体のライブ進捗**。
- Playwright化で結果が構造化される＝イベントが正確になり、可視化が容易に。
- 候補（移行と独立に着手可）:
  - **Cowork artifact**: `events.jsonl` を読むライブ進捗ページ（再オープンで最新化）。
  - `./report --watch`: ターミナルでの簡易ライブ集計。
  - `data/run_progress.json`: Slackエージェントが定期読みして「12/30 送信済・3 needs_attention」を返す。
- 推奨: まず `run_progress.json`（Slack親和）→ 余力でartifact。

---

## 11. 未決事項（実装前に確定したい）

1. **snapshot案Aの統一フォーマット**: text に加え controls/buttons をどこまで含めるか（LLMフォーム解析の入力品質に直結）。→ 既存 `_FORM_FIELDS_JS` を流用して1JSに統合できるか要検証。
2. **Doorman専用プロファイルの初期化手順**: cookie同意やログインが要るドメインの扱い。
3. **Chromium調達**: 既に `playwright install chromium` 済み（setup）。CI/他マシンでの版固定。
4. **infer をOpenClaw維持で良いか**（4箇所）。当面Yes。
5. **headless方針**: Cloudflare対策上、当面 headful 継続が無難か。
6. **並列化**: 当面シーケンシャル維持（2つ同時に変えない）。将来 context並列の余地。

---

## 12. 影響ファイル地図

| パス | 変更 |
|---|---|
| `_outreach_core/adapters/__init__.py` | seam＋backend選択 |
| `_outreach_core/adapters/openclaw_browser.py` | 既存 `oc_*` ラッパ（挙動不変） |
| `_outreach_core/adapters/playwright_browser.py` | **新規**・本体 |
| `jp-form-outreach/run.py` | `_evaluate`/`oc_browser`/tab関数をアダプタ呼びに付け替え（上位ロジック不変） |
| `_outreach_core/verify.py`, `contact_url.py` | snapshot入力を新統一snapshotへ（§4） |
| `_outreach_core/tab_utils.py` | Playwright payload用 shim |
| `_outreach_core/tests/` | アダプタ単体＋snapshot回帰 |

---

## 13. 工数感（ラフ）

- Phase 0（seam＋旧アダプタ）: 小（半日）
- Phase 1（Playwrightアダプタ＋snapshot案A）: 中（1–2日、snapshot整合が山）
- Phase 2–3（enrich→send検証）: 中（実フォームでの突き合わせ次第）
- 進捗可視化（run_progress.json）: 小

最小リスクは Phase 0 を先に入れて「いつでも戻せる土台」を作ること。次に Phase 1 をスパイクとして enrich 1社で体感確認。
