# CURSOR / Codex 指示書 v28 — 信頼性と可観測性の底上げ

> 正典は [`CURSOR_INSTRUCTIONS.md`](./CURSOR_INSTRUCTIONS.md)。本書はその差分指示（v28）。
> §3「守ってほしい不変項」は**全て継続**（特に *verify / DOM 走査での LLM 呼び出し禁止*、*純関数はテスト必須*、*per-lead 隔離で 1 社のクラッシュがバッチを殺さない*、*送信ジャーナルによる二重送信防止*）。
> 実行担当ホストは `MacMiniHome`（`_outreach_core/host_role.py`）。本書の対象スキルは `jp-form-outreach`。

## 0. 背景 — 解消したい 6 つの訴えと根本原因マップ

実運用での 6 つの不満を、コード上の原因とフェーズに割り付ける。**症状を潰すのではなく、まず「何が起きているか」を 1 つの真実源で測れるようにし、その上で検知・通知・改善する**順で進める。

| # | 訴え | 根本原因の仮説（コード位置） | フェーズ |
|---|---|---|---|
| 1 | リストの質が悪い | 投入前の品質ゲートが無い。`research_quality_summary`（report.py）は事後集計のみで、enrich 前足切りが無い。recruit/EC/ログイン専用 URL がそのまま enrich に流れる（memory: 投入の約67%が enrich で脱落、skip の約79%がフォーム誤分類） | P5 |
| 2 | フォーム投入の成功率が低い | フォーム発見・分類の誤り（recruit/login/no-textarea）＋必須/バリデーション rescue の取りこぼし。`_classify_form_type` / `submit_progress` / pre-form gate（v27 で着手）。shadow DOM/SPA は v23 で着手済みだが回帰計測が無い | P4 |
| 3 | 投げ込んだと認識できない | `verify.py` の証拠スコアが二値寄りで「不明(unknown)」が多い。成功シグナル辞書が WP 中心で Google Forms/HubSpot/formrun 等を取りこぼす。unknown が needs_attention に確実に積まれない | P3 |
| 4 | プロセスが詰まる・落ちる | stall 検知は run-level（`run_supervisor.py`）＋gateway（`watchdog.py`）はあるが、**per-target タイムアウトが無い**。1 社の無限待ち（captcha/SPA 描画待ち）でバッチ全体が固まる | P2 |
| 5 | 問題を Slack が通知してくれない | `notify.post` は呼ばれた所だけ。needs_attention 追加時・異常終了時に**必ず**通知する経路が無い。error/warn がスレッド外やサイレントに落ちる | P2 |
| 6 | Slack レポートのパターンが少なく実態が分からない | `report.py` は send-funnel / draft-quality 等あるが、**脱落理由の粒度が粗く・代表企業名が出ず・前回比が無い**。outcome の語彙が分散していて集計できない | P1 |

**実装順（優先度）**: **P1 計測・可視化 → P2 検知・通知 → P3 送信確認 → P4 投入成功率 → P5 リスト質**。
P1 が全ての土台（単一の outcome タクソノミ）。以降のフェーズは P1 のイベントを使って効果を計測する。各フェーズは独立 PR とし、純関数を必ずテストする。

---

## 1. 変更対象ファイル地図

| パス | 役割 | フェーズ |
|---|---|---|
| `_outreach_core/outcomes.py` （**新規**） | canonical な送信結果タクソノミ＋分類純関数（全レポートの基盤） | P1 |
| `_outreach_core/helpers/report.py` | レポート様式の拡充（脱落理由 TopN・代表企業・前回比・根本原因集計） | P1 |
| `jp-form-outreach/run.py` | ターゲット終了時の `send.target_outcome` emit、per-target タイムアウト、needs_attention→通知フック | P1,P2,P3 |
| `_outreach_core/notify.py` | `post_problem`（error/warn を必ずスレッドへ＋集約）の追加 | P2 |
| `_outreach_core/run_supervisor.py` | per-target watchdog の判定純関数（`decide_target` 等） | P2 |
| `_outreach_core/verify.py` | 成功シグナル辞書拡充・多段 verdict（strong/weak/unknown/fail）・unknown の確実な needs_attention 化 | P3 |
| `_outreach_core/send_state.py` / `contact_url.py` | フォーム種別の成功/失敗判定の共有強化（既存） | P3,P4 |
| `_outreach_core/list_quality.py` （**新規**） | 投入前の品質ゲート純関数（URL 健全性・ペルソナ一致・重複・除外理由） | P5 |
| `_outreach_core/tests/` | 各純関数のテスト（必須） | 全 |

> **不変項**: verify.py と DOM 走査では LLM を呼ばない（v15/v23 継続）。LLM は draft / form-analyzer / thread_control の意図判定など既存の許可箇所のみ。

---

## P1 — 計測・可視化（最優先・全フェーズの土台）

### §P1-A 単一の outcome タクソノミ（新規 `outcomes.py`・純関数）

現状、送信結果は `_submission_loop` の `done/validation_stuck/ineffective/too_deep/...`（run.py:6059 付近）、`handle_verify_result` の `sent_ok/...`、needs_attention の reason 文字列、`_classify_form_type` の kind がバラバラに存在し、横断集計できない。**canonical な enum に正規化する純関数を 1 つ作る**。

```python
# _outreach_core/outcomes.py
SENT            = "sent"               # 検証で強い成功
SENT_UNVERIFIED = "sent_unverified"    # 送信操作は完了だが成功証拠が弱い/不明
VALIDATION_STUCK= "validation_stuck"   # 必須/バリデーションで前進せず
SUBMIT_INEFFECTIVE = "submit_ineffective" # 送信クリックがページを進めない
NO_FORM         = "no_form"            # textarea 無し/問い合わせフォーム不在
RECRUIT_MISCLASS= "recruit_misclass"   # 採用/IR 等の非問い合わせ
LOGIN_REQUIRED  = "login_required"
CAPTCHA_BLOCKED = "captcha_blocked"
MULTISTEP_TOO_DEEP = "multistep_too_deep"
SHADOW_DOM_UNSEEN  = "shadow_dom_unseen"
NETWORK_ERROR   = "network_error"
SKIPPED_QUALITY = "skipped_quality"    # P5 の品質ゲートで除外
CRASHED         = "crashed"
UNKNOWN         = "unknown"

BUCKET = {  # ファネルの段。list / fill / submit / confirm / system
    SENT: "confirm", SENT_UNVERIFIED: "confirm",
    VALIDATION_STUCK: "fill", SUBMIT_INEFFECTIVE: "submit",
    NO_FORM: "list", RECRUIT_MISCLASS: "list", LOGIN_REQUIRED: "list",
    CAPTCHA_BLOCKED: "submit", MULTISTEP_TOO_DEEP: "submit",
    SHADOW_DOM_UNSEEN: "fill", NETWORK_ERROR: "system",
    SKIPPED_QUALITY: "list", CRASHED: "system", UNKNOWN: "system",
}

def classify_outcome(*, result_state: str | None, verify_verdict: str | None,
                     timeline: list[dict] | None) -> str:
    """run.py の生 state + verify の verdict + send_timeline から canonical へ正規化。
    優先順位: 強い成功 > timeline の first_failure(根本原因) > result_state。"""

def outcome_label_ja(code: str) -> str: ...
def outcome_bucket(code: str) -> str: ...
```

`first_failure` / `failure_headline` は既存（`send_timeline.py`）を再利用する。

**受け入れ条件**
- `classify_outcome` が上記 enum 以外を返さない（不明は必ず `UNKNOWN`）。
- 既存の `_submission_loop` の各終了 state と `handle_verify_result` の戻り値が、漏れなく canonical にマップされる。

**テスト**（`tests/test_outcomes.py`）
- (result_state, verdict, timeline) の代表組み合わせ表 → 期待 outcome を assert。
- バケット整合（全 enum が BUCKET に存在）。

### §P1-B ターゲット終了イベントを必ず 1 件 emit（run.py）

`stage_send` のループ末尾（`hb.tick` 付近、run.py:7153 周辺）で、**全ターゲットについて** `send.target_outcome` を emit する。

```python
ev.emit("send.target_outcome", stage="send", target_id=tid, payload={
  "outcome": outcomes.classify_outcome(...),
  "bucket": outcomes.outcome_bucket(outcome),
  "company": d.get("name"), "url": d.get("form_url") or d.get("url"),
  "root_cause": core_timeline.failure_headline(timeline),
  "elapsed_sec": ..., "retries": ..., "verify_verdict": ...,
})
```

**受け入れ条件**: 1 ターゲット = ちょうど 1 件の `send.target_outcome`。crash 経路（except 節）でも必ず emit。
**テスト**: emit 引数を組み立てる純関数（`build_target_outcome_payload`）を切り出してテスト（run.py 直書きはテストしづらいので関数化）。

### §P1-C レポート様式の拡充（report.py）

`send_period_summary` / `cmd_send_funnel` を `send.target_outcome` ベースに寄せ、以下を出す。

1. **ファネル各段の脱落理由 TopN**＋件数・比率＋**代表企業を 3 社まで**列挙（どの会社で何が起きたか分かる）。
2. **前回バッチ/前期間との差分**（�‑/＋、`_write_send_summary_snapshot` の既存スナップショットを前回値として利用）。
3. **根本原因（root_cause）別の集計**（`failure_headline` の頻度順）。
4. **時間帯別の成功率**（任意・あれば良い）。
5. 出力は Slack ブロックと CLI の両方。`report send-summary --post-slack` で投稿。

**受け入れ条件**
- `report send-funnel` が「段（list/fill/submit/confirm）→ outcome → 件数 / 比率 / 代表企業3社」を表示。
- 前回比が出る（初回はベースライン表記）。
- 既存テストを壊さない。

**テスト**（`tests/test_report_v28.py`）
- イベント配列の入力 → 集計辞書（純関数 `summarize_outcomes(events) -> dict`）を assert。
- 前回比計算（`diff_against_snapshot(curr, prev)`）の純関数テスト。

---

## P2 — 詰まり/落ちの検知と、問題の Slack 通知

### §P2-A per-target タイムアウト（run.py）

現状 stall 検知は run 全体（`run_supervisor`）と gateway（`watchdog`）のみで、**1 社の処理が無限に粘ると 30s スリープ前で固まる**。`_send_one_target` 呼び出しに**ソフトな実行時間上限**を設ける。

- 設定 `execution.per_target_timeout_sec`（既定 180）を追加。
- 経過が上限を超えたら、その社を `outcome=network_error`/`captcha_blocked` 等で打ち切り、needs_attention に積んで次へ（per-lead 隔離の不変項に従う）。実装は協調的チェック（フェーズ境界で経過確認）で良い。スレッド強制 kill は不要。
- 判定は純関数化: `should_abort_target(started_at, now, limit) -> bool`。

**受け入れ条件**: 1 社で上限超過 → その社のみ打ち切り、バッチは継続、`send.target_timeout` イベント＋needs_attention。
**テスト**: `should_abort_target` の境界（未満/同値/超過/limit<=0=無効）。

### §P2-B 「問題は必ず Slack に出す」経路（notify.py + run.py）

`notify.py` に **`post_problem(kind, target, detail, *, thread_ts)`** を追加し、以下で必ず呼ぶ:
- needs_attention 追加時（`append_needs_attention` のラッパ経由で 1 箇所に集約）。
- 異常終了 / per-target タイムアウト / validation_stuck / submit_ineffective。
- run の異常終了（`run_supervisor` の give-up）。

仕様:
- level=error/warn は**必ずスレッド（`DOORMAN_SLACK_THREAD_TS`）へ**。webhook 設定時はスレッド不可なので bot 経路を優先（既存 `post` の分岐に注意。webhook が設定されているとスレッド化されない点を `post_problem` 側で考慮）。
- **重複抑制**: 同一 target×kind は一定時間 1 回（`_recent_problem_seen(key, now)` 純関数＋メモリ/ファイル）。スパム防止。
- 1 通の本文に **根本原因(root_cause)＋会社名＋URL＋次アクション**（`resolve_queue.build_actionable_message` を流用）。

**受け入れ条件**
- needs_attention が増えたら必ず Slack に 1 通（重複抑制内）。
- error/warn がスレッドに出る（bot 経路時）。

**テスト**（`tests/test_notify_problem.py`）
- `post_problem` をモックした上で「needs_attention 追加 → 通知 1 回」「同一 key 連続 → 抑制」を純関数 `_recent_problem_seen` で assert。
- 本文整形 `format_problem_message(...)` の純関数テスト。

### §P2-C run-level watchdog の通知強化（run_supervisor.py）

`decide` が `GIVE_UP_*` を返したら、`failure_headline` と直近 outcome 集計を添えて Slack へ（§P2-B 経由）。stall 再開時のメッセージは既存維持。

**受け入れ条件**: give-up/stall が必ず通知される（サイレント終了を無くす）。
**テスト**: 既存 `decide` テストに、give-up 時に通知ペイロードを生成する純関数を足して assert。

---

## P3 — 「送信できたか」を確実に認識する

### §P3-A 成功シグナル辞書の拡充（verify.py / send_state.py）

`PAGE_EVIDENCE_JS` / `FORM_VISIBILITY_JS` の成功キーワードは WP 中心。以下を**データ駆動の辞書**に集約し追加:
- フォームベンダ別サンクス: `mw_wp_form_complete`(済), `wpcf7mailsent`, Google Forms `freebirdFormviewerViewResponseConfirmationMessage`, HubSpot `submitted-message` / `hs-form` success, formrun `thanks`, Salesforce/SPA の遷移後 thanks, Wix/STUDIO の完了。
- 成功シグナルは「キーワード」「URL 遷移(`url_looks_like_success`)」「送信ボタン消失」「フォーム残存の否定」を**重み付き合算**（既存 `score_send_evidence` を拡張）。

> 不変項維持: ここでも LLM は使わない。辞書＋DOM 証拠のみ。

### §P3-B 多段 verdict と unknown の確実な可視化

`verdict_from_score` を 4 段に: `strong`（=SENT）/ `weak`（=SENT_UNVERIFIED, 要目視）/ `unknown` / `fail`。
- `weak`/`unknown` は **必ず** needs_attention（reason: `verify_unverified`）に積み、§P2-B で Slack 通知（「送信した可能性が高いが未確認、目視してほしい」）。
- 送信ジャーナル `PHASE_VERIFIED`（run.py:6737 付近）と整合させ、二重送信防止の不変項を壊さない（unverified は次回 needs_attention 経由で扱う）。

**受け入れ条件**
- フィクスチャ群（後述）で `unknown` 率が現状比で**低下**、かつ誤って `strong` にしない（false positive を増やさない）。
- weak/unknown が 100% needs_attention 化される。

**テスト**（`tests/test_verify_v28.py`）
- ベンダ別の成功/失敗/曖昧ページの HTML 断片フィクスチャ → `score_send_evidence` / `verdict_from_score` の期待値表。
- weak/unknown → needs_attention 化の純関数（`should_flag_unverified(verdict) -> bool`）。

---

## P4 — フォーム投入の成功率

### §P4-A 誤分類の削減（contact_url.py / 分類）

memory の実測「skip の約79%がフォーム誤分類（recruit/login/no-textarea）」を直接叩く。
- recruit/採用・IR・ログイン専用・EC カートの**否定シグナル辞書**を強化（v26 の「nav 採用リンクで本物の contact を汚さない」修正の延長）。
- pre-form gate（v27 `_advance_pre_form_phase`）の適用範囲を「法人/同意/種別」中間ページに広げる（パターン追加）。
- shadow DOM/SPA は v23 の deep 走査を**回帰計測**（成功率の before/after を P1 イベントで測る）。

### §P4-B 必須/バリデーション rescue の底上げ（submit_progress.py）

直近の検証 rescue（neutral/first-option、radio/select、`※`過検出修正）を継続強化:
- 未対応の必須コントロール（カスタム UI の擬似 select、トグル）への対応を**純関数の選択ロジック**として拡張。
- 「同じ検証エラーでループ」する場合の打ち切り条件を `submit_progress` 側の純関数に集約（run.py 直書きを減らす）。

**受け入れ条件**
- フォームコーパス（後述 §P4-C）で「fill 完了→submit 到達」率が改善、かつ誤送信（誤った値の自動投入）を増やさない。
- `※`単独で必須誤検知しない（既存修正の回帰）。

**テスト**: `tests/test_form_choice.py` にケース追加（擬似 select / トグル / ループ打ち切り）。

### §P4-C 回帰用フォームコーパス（テスト基盤）

`_outreach_core/tests/fixtures/forms/` に代表的な実フォームの **DOM スナップショット（静的 HTML/JSON）** を 15〜30 件用意し、分類・fill・verify をオフラインで回せるようにする（ネットワーク不要）。これが P3/P4 の「成功率」を数値で言えるようにする唯一の手段。

**受け入れ条件**: `pytest tests/test_form_corpus.py` で分類精度・fill 到達率・verify 精度の集計が出る。
**テスト**: コーパス→期待 outcome の表。

---

## P5 — リストの質（投入前ゲート）

### §P5-A 投入前 品質ゲート（新規 `list_quality.py`・純関数）

enrich に流す前に、ターゲット行を判定して足切り/フラグする。

```python
def assess_target(row: dict, *, persona: dict) -> dict:
    """returns {"verdict": "pass"|"review"|"drop", "reasons": [...], "score": int}
    判定軸:
      - URL 健全性（スキーム, 明らかな 404/採用/IR/ログイン/EC カート URL パターン）
      - ペルソナ/業種一致（briefs の target_persona と突合）
      - 重複（正規化ドメインで dedup）
      - 連絡導線の有無の事前推定（/contact, /inquiry, お問い合わせ 等）
    """
def dedup_targets(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(kept, dropped_dups)。ドメイン正規化で重複除去。"""
```

- `drop` は投入しない（outcome=`skipped_quality` として P1 に記録）。`review` は投入するが Slack に要確認として要約。
- 既存 `research_quality_summary`（report.py）を**事前**にも使えるようにする。

**受け入れ条件**
- 投入リストに対して「pass / review / drop」内訳と drop 理由 TopN が出る。
- enrich 到達率（投入→enrich 通過）が改善（P1 イベントで before/after）。
- 既存の投入フローを壊さない（ゲートは設定 `list_quality.enabled` で OFF 可能、既定 ON）。

**テスト**（`tests/test_list_quality.py`）
- URL パターン（採用/IR/ログイン/EC/正常）→ verdict の表。
- `dedup_targets` の重複/正規化（www, 末尾スラッシュ, http/https）。

---

## 3. 共通の受け入れ・不変項（全フェーズ）

1. 純関数は**必ず**テスト（`outcomes` / `list_quality` / `should_abort_target` / 通知抑制 / verify スコア / report 集計）。
2. verify と DOM 走査で **LLM を呼ばない**（既存不変項）。
3. **per-lead 隔離**: どの変更も「1 社の失敗でバッチが死なない」を壊さない。
4. **二重送信防止**: 送信ジャーナル `PHASE_VERIFIED` の意味を変えない。unverified は needs_attention 経由で扱う。
5. host ガード（`MacMiniHome` のみ実行）・送信スレッド進捗（10分・v27）・スレッド停止制御（v27）を壊さない。
6. すべての送信ターゲットが**ちょうど 1 件**の `send.target_outcome` を出す（計測の単一真実源）。
7. `python3 -m unittest discover -s _outreach_core/tests -q` が緑（既知の macOS 専用 `test_resolve_argv_makes_launchctl_absolute` を除く）。

## 4. 実装順とコミット粒度

P1 →（PR1: outcomes + emit + report）／ P2 →（PR2: per-target timeout + post_problem + supervisor 通知）／ P3 →（PR3: verify 辞書 + 多段 verdict + unknown 可視化 + フィクスチャ）／ P4 →（PR4: 誤分類削減 + rescue + フォームコーパス）／ P5 →（PR5: list_quality ゲート）。
各 PR はコミットメッセージ冒頭に `v28(P{n}):` を付ける。各 PR 単体で `unittest` 緑＋既存機能無回帰。

## 5. まず最初の 1 歩（Codex への着手指示）

**PR1 = P1 全体**から着手すること。理由: outcome タクソノミと `send.target_outcome` イベントが無いと、以降のフェーズの効果（成功率・unknown 率・脱落理由）を数値で言えない。PR1 完了後、現行運用で 1 バッチ回し、`report send-funnel` の新様式で「いま実際に何が起きているか」を Slack に出した状態を作ってから P2 以降へ進む。
