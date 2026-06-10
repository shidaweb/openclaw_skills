# CURSOR 指示書 v9 — 単一デバイスで「1日最大50件」運用

> 正典は [`CURSOR_INSTRUCTIONS.md`](./CURSOR_INSTRUCTIONS.md)。本書はその差分指示（v9）。
> §3「守ってほしい不変項」は**全て継続**（6フェーズ名・JSONL名・append-only履歴・browser profile
> `openclaw` **固定（並列・複数プロファイル禁止）**・model ピン・`verify.py` で LLM を呼ばない・
> 既存サブコマンド表面互換）。対象スキルは `jp-form-outreach`。

## 0. ゴールと前提

- **単一デバイス・単一ブラウザプロファイル**のまま、**1日あたり最大 50 件**の送信を、
  **営業時間内に分散**して安全に出す。
- 上限 50 は **ハード上限**（再起動・再実行で超過しない）。ペースは reCAPTCHA 回避に安全な水準
  （50件を ~10時間に分散＝平均12分に1件）。
- 既に実装済みの **avoidance（送信時間帯 `send_window` / ペーシング / ドメイン学習）** を
  **送信ループに結線**し、不足している **日次バジェット・ガバナー** を追加するのが中心。
- 並列化・複数プロファイルは**やらない**（v6 §3 不変項。スループットは「上限を律速に余裕で収める」設計）。

> 前提となる打率改善（v7 §A 送信ボタン進行性 / v8 リサーチ form_url 補正）が効くほど、50件送信に
> 必要なリサーチ母数が減る。本書はそれらと併用する。

---

## 1. 変更対象ファイル地図

| パス | 役割 | v9 での扱い |
|---|---|---|
| `_outreach_core/rate_budget.py` | **新規・純関数** 日次バジェット/ペース判定 | §T1（テスト可能に分離） |
| `_outreach_core/avoidance.py` | 既存 window/pacing/domain | §T2 で**結線**（`within_send_window` 等を利用。改修は最小） |
| `jp-form-outreach/run.py` | `stage_send` | §T2（ガバナー結線・固定30秒sleep 置換・cooldown・cap 停止）, §T3（`--max`） |
| `_outreach_core/history.py` | `sent_history`（`sent_at`） | §T1 の日次カウント源（参照のみ・改修なし） |
| `_outreach_core/helpers/report.py` | レポート | §T5（`today` / budget 状況） |
| `docs/SCHEDULING.md` | cron レシピ | §T4（50/日の自動分散バッチ例を追記） |
| `sender_brief.yaml` / `briefs/_template.yaml` | 設定 | §T1 `throughput:` ブロック追加 |
| `_outreach_core/tests/` | テスト | 各 § の受け入れテスト |

---

## 2. §T1 日次バジェット・ガバナー（純関数・新規 `rate_budget.py`）

### T1-1. 設定（`throughput:` ブロック）
`sender_brief.yaml`（brief で上書き可）に追加。コードは未設定でも安全な既定値を持つこと。

```yaml
throughput:
  daily_cap: 50            # 1日のハード上限（送信成功ベース）
  min_gap_sec: 90          # 送信間隔の下限
  max_gap_sec: 900         # 送信間隔の上限（ジッター天井）
  batch_size: 5            # スケジュール1回あたりの処理社数
  per_domain_cooldown_days: 30   # 同一ドメインへの再送禁止期間
  # send_window は既存 avoidance.pacing.send_window（既定 [9,19]）を流用する
```

### T1-2. 純関数（すべて引数入力・ファイル参照は薄いラッパに限定）
- `sends_today(data_dir, now=None) -> int`
  - `sent_history.jsonl` の各行 `sent_at` を**ローカル日付**で集計し、今日の送信数を返す。
  - **再起動・再実行で増えない**（履歴ベースなので冪等）。
- `remaining_today(data_dir, daily_cap, now=None) -> int` = `max(0, cap - sends_today)`
- `decide(*, sends_today, daily_cap, in_window, last_send_age_sec, min_gap_sec) -> str`（純）
  返り値:
  - `"stop_cap"` … `sends_today >= daily_cap`
  - `"defer_window"` … 営業時間外
  - `"defer_pacing"` … 直近送信から `min_gap_sec` 未満
  - `"send"` … 送信可
- `next_gap_sec(*, remaining, window_seconds_left, min_gap_sec, max_gap_sec, jitter=True) -> int`（純）
  - 残り送信数を**残り window 時間に均等割り**し、`[min_gap_sec, max_gap_sec]` にクランプ、±ジッター。
  - 残りが0 or window 残り0なら `min_gap_sec`。
- `domain_in_cooldown(data_dir, url, cooldown_days, now=None) -> bool`
  - 既存 avoidance のドメイン `last_at`＋送信実績（`sent>0`）から、cooldown 期間内の同一ドメインを True。

### §T1 受け入れ条件
1. `sends_today`：今日の `sent_at` のみ数え、昨日分・他日付は除外（タイムゾーン跨ぎのダミーで検証）。
2. `decide`：cap到達→`stop_cap` / 時間外→`defer_window` / 直近送信が近い→`defer_pacing` / それ以外→`send`。
3. `next_gap_sec`：残り10件×window6時間で**おおむね均等割り**になり、`min/max` でクランプされること。
4. `domain_in_cooldown`：cooldown 内の同一ドメインが True、期間外は False。
5. すべて**純関数ユニットテスト**（now/履歴をモック注入）。

---

## 3. §T2 送信ループへの結線（`stage_send`）

**狙い**: 上限・時間帯・ペースを送信1件ごとに尊重し、固定 `time.sleep(30)` を**バジェット連動の間隔**に置換。

- 各ターゲットの**送信直前**に `rate_budget.decide(...)` を評価:
  - `stop_cap` → **当該 run を正常終了**（`[send] 本日の上限 {cap} に到達したため停止` をログ＋Slack 1行）。エラーにしない。
  - `defer_window` → **当該 run を正常終了**（`営業時間外のため停止`）。スケジュール次バッチに委ねる。
  - `defer_pacing` → 不足秒だけ `sleep` してから送信。
  - `send` → 送信。
- **per-domain cooldown**: 送信前に `domain_in_cooldown` を見て、期間内なら**スキップ＋記録**（`skip_history`、reason `domain_cooldown`）。
- **送信成功後の待機**（`time.sleep(30)` を置換）: `next_gap_sec(...)` の秒数だけ待機（次が無ければ待たない）。
  - 待機は keepalive 配下（v6）で stdout を出し続け、stall 誤判定を避ける。
- **window 判定は既存 `avoidance.within_send_window(config, now)` を流用**（重複実装しない）。
- cap は `daily_cap`（throughput）を使用。`mode in ("auto","interactive")` の送信のみカウント対象。

> 既存挙動互換: `throughput` 未設定または `daily_cap` 未指定なら**従来どおり（上限なし・固定間隔）**で動くこと。

### §T2 受け入れ条件
6. `daily_cap=3` で4社送信を指示 → **3社で `stop_cap` 停止**、4社目は送信されない（sent_history は3件）。
7. window外（`within_send_window=False` をモック）で `defer_window` により**送信0**で正常終了すること。
8. cooldown 内ドメインのターゲットが `domain_cooldown` でスキップされること。
9. 送信間隔が固定30秒ではなく `next_gap_sec` 由来（min/max 範囲内）になること（sleep をモックして検証）。

---

## 4. §T3 バックログ消化（`send --max N`）

**狙い**: research/draft を**先にバルクで仕込み**、当日はドラフト在庫を**ペース上限まで**流す。

- `run.py send` に **`--max N`** を追加（既存 `--all` と併用可）。
  - 当日の `remaining_today(daily_cap)` と `--max` の**小さい方**を上限に、未送信 sendable ドラフトを送る。
  - `--all` 単独時も**暗黙に `remaining_today` を上限**にする（v9 では上限超過を構造的に防ぐ）。
- `campaign` の自律送信（`_run_autonomous_send`）も同様に `remaining_today` を上限として送る。

### §T3 受け入れ条件
10. `sends_today=48, daily_cap=50` の状態で `send --all` を実行 → **最大2件**だけ送信されること。
11. `send --max 5`（remaining=2）→ **2件**で停止（小さい方が効く）。

---

## 5. §T4 スケジュール分散（cron・hands-off 50/日）

`docs/SCHEDULING.md` に、**営業時間内の小バッチ自動実行**で 50/日に収めるレシピを追記。
（Python 側にスケジューラは入れない。`openclaw cron` ＋ 自律モード＋本書のガバナーで律速する。）

```bash
# 早朝: 当日分のドラフト在庫を仕込む（送信しない）
openclaw cron add --schedule "0 7 * * 1-5" \
  "doorman: torana-line-crm で ~100社を research→enrich→draft（送信なし, --skip-send）。
   v8 リサーチ品質で form_url を厳格化。在庫を作る"

# 営業時間: 30分毎に小バッチ送信（自律・ガバナーが上限/時間帯/ペースを律速）
openclaw cron add --schedule "*/30 9-18 * * 1-5" \
  "doorman: ./job start jp-form-outreach send --from-backlog --max 5 --auto-send --brief torana-line-crm
   （日次上限50・送信時間帯・per-domain cooldown はガバナーが自動適用。上限到達後は即終了）"
```

- 1回5社 × 営業時間の試行回数で、**打率×試行が 50 に達した時点で以降のバッチは即 `stop_cap` 終了**する。
- 失敗/stall は v6 run_supervisor が冪等再起動。上限は sent_history ベースで超過しない。

### §T4 受け入れ条件
12. SCHEDULING.md に「早朝の在庫仕込み」＋「営業時間の小バッチ」＋「上限/時間帯はガバナー律速」が明記される。

---

## 6. §T5 当日の可視化（`report today` / budget 状況）

- `report today`（または `run.py budget-status`）を追加:
  - 本日送信 `sends_today` / 上限 `daily_cap` / 残り `remaining_today`
  - 現在 window 内か / 次送信までの目安（`next_gap_sec`）
  - cooldown スキップ件数・打率（今日）
- Slack「今日あと何件？」「今日のペースは？」に即答できるよう SKILL.md トリガー表へ1行追記。

### §T5 受け入れ条件
13. ダミー sent_history で `report today` が 本日送信/残り/上限 を正しく表示すること（集計部の純関数テスト）。

---

## 7. 守ってほしい不変項（v9 追加分）

1. **日次上限は sent_history ベースのハード上限**。再起動・並走でも超過しない（カウントは履歴から都度再計算）。
2. **単一ブラウザ・単一プロファイル**を維持（並列・複数プロファイル禁止）。スループットは上限を余裕で下回る設計。
3. ペースは**営業時間に分散＋ジッター**（バースト禁止）。reCAPTCHA 安全圏を崩さない。
4. `sent_history`/`skip_history` のカラムは不変（`domain_cooldown` 等は reason 文字列で表現、**追加のみ**）。
5. `throughput` 未設定時は**従来挙動（無制限・固定間隔）**で互換。判定は**純関数＋テスト**に寄せる。
6. `verify.py` は不変・LLM 不使用。

## 8. やらないこと

- ❌ 並列送信／複数ブラウザプロファイル／別IPでの水増し。
- ❌ 営業時間外・上限超過の強行送信。
- ❌ per-domain cooldown を無視した同一企業への連投。
- ❌ ガバナー判定の LLM 化（純 Python 維持）。

## 9. 実装順（推奨）

1. **§T1**（`rate_budget.py` 純関数＋テスト）— 律速ロジックの土台。
2. **§T2**（stage_send 結線：cap停止／window／cooldown／ペース置換）— 本体。
3. **§T3**（`--max` / `remaining_today` 上限）→ **§T4**（cron 分散）→ **§T5**（可視化）。

各 § 完了ごとに `python3 -m pytest _outreach_core/tests/ -q` を緑にしてから次へ。
