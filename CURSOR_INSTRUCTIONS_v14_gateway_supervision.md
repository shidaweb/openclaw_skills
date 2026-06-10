# CURSOR 指示書 v14 — OpenClaw ゲートウェイの安定化（外販向け・黒箱監視）

> 正典は [`CURSOR_INSTRUCTIONS.md`](./CURSOR_INSTRUCTIONS.md)。本書はその差分指示（v14）。
> §3「守ってほしい不変項」は**全て継続**。対象は信頼性レイヤー（Layer 3 = gateway watchdog）。

## 0. 方針（最重要・外販前提）

**OpenClaw 本体を fork / パッチしない。** 第三者ランタイムを fork すると、(1) 全クライアントに自前ビルド配布、
(2) 上流更新で破綻、(3) 保守地獄、(4) ライセンス論点。外販プロダクトに載らない。

→ **stock の OpenClaw を“黒箱”のまま、OS ネイティブ（launchd）の外部 supervisor で叩き起こす**。
OpenClaw 側は fork でなく**設定（KeepAlive 等）で固める**。本書は既存 `_outreach_core/helpers/watchdog.py`
＋ `scripts/install-watchdog.sh` を**外販に耐える形に強化**する。

## 1. 現状の弱点（`watchdog.py` を読んでの整理）

1. **死んだ gateway を自分で起動できない**: watchdog は「launchd の `KeepAlive=true` が死亡を自動復旧する」
   前提で、自身は **hung のみ `launchctl kickstart -k`** する設計（6–9行）。クライアントの OpenClaw が
   **KeepAlive 付き launchd 管理になっていない**と、**プロセス死＝死にっぱなし**（最頻の不安定要因）。
2. **`GATEWAY_LABEL="ai.openclaw.gateway"` 固定**。クライアントの構成差に弱い（config 駆動でない）。
3. **スリープ/ウェイク非対応**。デスクトップ on-device で Mac がスリープすると launchd tick が止まり、
   復帰後に gateway/Slack が戻らないことがある。
4. **死んでも誰も気づかない**（外部アラート/デッドマンズスイッチ無し）＝「気づいたら死んでた」。
5. **watchdog 自身/インストールの自己検証が弱い**。

→ 本書は **(W1) dead→必ず起動**、**(W2) スリープ/ウェイク復帰**、**(W3) 自己検証・self-heal**、
**(W4) デッドマンズスイッチ＋アラート**、**(W5) config 駆動＋外販インストーラ** を指示する。

## 2. 変更対象ファイル地図

| パス | 役割 | v14 |
|---|---|---|
| `_outreach_core/helpers/watchdog.py` | gateway 監視（既存） | §W1〜W4 強化 |
| `_outreach_core/gateway_config.py` | **新規** gateway 設定（label/start/health/restart コマンド） | §W5 config 駆動 |
| `scripts/install-watchdog.sh` / `*.plist.template` | 導入 | §W5 idempotent 化・KeepAlive アサート |
| `_outreach_core/tests/` | テスト | 各 § の純関数（決定ロジック） |

> 既存 `is_gateway_loaded` / `is_gateway_healthy` / `restart_gateway` / `tick` / `can_restart`/`record_restart`
> を再利用・拡張。**OpenClaw 本体には触れない（黒箱）。**

## 3. §W1 「死んでたら必ず起動し直す」（dead→確実復帰）

**狙い**: KeepAlive に依存せず、watchdog 自身が **gateway を起動できる**状態にする。

- `gateway_config.py`（§W5）から **`start_cmd` / `health_cmd` / `restart_cmd` / `label`** を読む。
- `tick` の判定を拡張:
  - **未ロード/プロセス死**（`is_gateway_loaded()==False` または health 連続失敗かつ未ロード）→
    **`restart_cmd`（既定: `launchctl kickstart`）が効かない場合は `start_cmd`（既定: `openclaw gateway start`
    or `launchctl bootstrap`/`load`）で“起動”する**。
  - **hung（loaded だが health 連続失敗）** → 既存 `kickstart -k`。
  - いずれも `can_restart`（窓・上限）で多重再起動を抑止。
- 「launchd 未登録」のときに**警告だけで終わらない**（現状の弱点）。可能なら**登録＋起動**を試みる（best-effort）。

### §W1 受け入れ条件
1. **未ロード状態**のモックで、watchdog が `start_cmd` を呼んで gateway を起動しようとすること（純関数 `decide_action` ＋ コマンド層モック）。
2. **hung 状態**で `kickstart`、**死亡未ロード**で `start_cmd`、を**呼び分ける**こと。
3. `can_restart`（既定 10分/3回）を超えたら `abandoned` で**起動を試みず**アラート（§W4）に回ること。

## 4. §W2 スリープ/ウェイク復帰（desktop on-device）

**狙い**: Mac スリープ→ウェイク後に gateway/Slack を確実に戻す。

- **wake 検知**: 各 tick で前回 tick からの経過が **`interval × N`（例 3）を超えたら「スリープ復帰」**とみなす
  （launchd tick が飛んだ＝寝ていた）。
- wake 検知時は **通常判定より踏み込んだ復旧**: health 再確認 →（必要なら）gateway 起動/再起動 →
  Slack チャンネル `running` 再確認→再接続（既存 `configured_but_down_channels` 経路）。
- 任意: アクティブ run 中は別途 `caffeinate`（run 側）で**スリープ抑止**（gateway 監視とは別レイヤー）。

### §W2 受け入れ条件
4. 「前回 tick から 5 分経過（interval=60s）」のモックで `woke_from_sleep=True` と判定し、
   **強復旧パス（health 再確認＋必要時 restart＋channel 再接続）**を通ること（純関数判定のテスト）。

## 5. §W3 watchdog 自身の self-heal ＋ 自己検証

- 各 tick の冒頭で **自分の launchd 登録を検証**し、未ロードなら**再ロード**（idempotent）。
- gateway の **launchd `KeepAlive=true` をアサート**（外せていたら再設定 / 設定できなければ §W1 で代替）。
- watchdog plist は `RunAtLoad=true` ＋ `StartInterval`＋（可能なら）`KeepAlive` 併用で、tick の取りこぼしを減らす。

### §W3 受け入れ条件
5. watchdog 未ロードのモックで**自己再ロード**を試みること。gateway KeepAlive が false のモックで**再アサート**を試みること。

## 6. §W4 デッドマンズスイッチ＋アラート（「死んだら気づける」）

**狙い**: 復旧不能を**人に通知**し、「気づいたら死んでた」を撲滅。

- watchdog は **last_ok（最後に gateway healthy だった時刻）** を記録。
- **連続再起動失敗 or `abandoned`** かつ **last_ok から M 分以上**経過したら**エスカレーション**:
  - **オペレーターへ Slack 通知**（既存 `notify_slack`）: 「ゲートウェイが M 分復帰できません。手動確認/再起動を」。
  - **任意・opt-in: ベンダー側 liveness ping**（外販時の運用監視）: HTTPS で **死活のみ**（install_id ＋
    timestamp ＋ status）を送る。**PII・送信データは一切送らない**。既定オフ、config で opt-in。
- 復旧したら「復帰しました」を1回通知（フラッピング抑制つき）。

### §W4 受け入れ条件
6. 「abandoned かつ last_ok から M 分超」のモックで**エスカレーション通知**が1回出ること（重複抑止つき）。
7. ベンダー ping は**既定オフ**で、opt-in 時のみ呼ばれ、**PII を含まない**こと（payload を検証）。

## 7. §W5 config 駆動 ＋ 外販インストーラ

**狙い**: クライアント構成差に強く、ワンコマンドで確実導入。

- `gateway_config.py`（または `briefs`/runtime config の `gateway:` ブロック）に:
  ```yaml
  gateway:
    label: "ai.openclaw.gateway"
    health_cmd: "openclaw health"
    start_cmd:  "openclaw gateway start"     # or launchctl bootstrap ...
    restart_cmd: "launchctl kickstart -k gui/$UID/ai.openclaw.gateway"
    watchdog: { interval_sec: 60, max_restarts: 3, window_min: 10, dead_alert_min: 15 }
    vendor_ping: { enabled: false, url: "" }   # 外販運用監視（opt-in・死活のみ）
  ```
  既定値はコードに持ち、未設定でも動く。**ハードコード `ai.openclaw.gateway` を撤廃**。
- `install-watchdog.sh` を **idempotent ＋ 検証付き**に:
  - plist 生成 → load → `launchctl list` で**登録を検証**して結果表示。
  - gateway の launchd **KeepAlive をアサート**（可能な範囲で）。
  - 再実行しても安全。`CLIENT_ONBOARDING.md` / `DISTRIBUTION.md` に導入手順を記載。

### §W5 受け入れ条件
8. `gateway.label` を変えた config で watchdog が**その label を使う**こと（ハードコード非依存の回帰）。
9. `install-watchdog.sh` を2回実行しても**壊れず**、登録検証メッセージを出すこと。

## 8. 守ってほしい不変項（v14 追加分）

1. **OpenClaw 本体を fork/パッチしない**（黒箱・OS ネイティブ監視のみ）。設定（KeepAlive 等）で固める。
2. 再起動は **窓＋上限**（フラッピング/再起動ループ禁止）。超過は `abandoned`＋アラート。
3. ベンダー ping は **opt-in・死活のみ・PII/送信データ非送信**。既定オフ。
4. gateway 構成は **config 駆動**（label/コマンドをハードコードしない）。
5. 監視の決定ロジックは**純関数＋ユニットテスト**（`decide_action` / wake 判定 / エスカレ判定）。`verify.py` 不変。
6. Doorman の Python venv が壊れていても **supervisor は独立に動ける**よう、依存を最小化。

## 9. やらないこと

- ❌ OpenClaw の fork / バイナリ改変。
- ❌ 無上限の再起動ループ。
- ❌ ベンダー ping での PII・送信内容の送信（死活のみ・opt-in）。
- ❌ `GATEWAY_LABEL` 等のハードコード継続。

## 10. 実装順（推奨）

1. **§W5**（config 駆動 ＋ idempotent インストーラ）— 土台。ハードコード撤廃。
2. **§W1**（dead→必ず起動）— 最頻の「死にっぱなし」を解消。
3. **§W2**（スリープ/ウェイク）→ **§W3**（self-heal）→ **§W4**（デッドマンズスイッチ＋アラート）。

各 § 完了ごとに `python3 -m pytest _outreach_core/tests/ -q` を緑にしてから次へ。
```
要約: OpenClaw は fork せず黒箱のまま、launchd 外部 supervisor を
「死んだら必ず起動／スリープ復帰／自己修復／死んだら通知」へ強化し、
config 駆動＋idempotent インストーラで外販に載せる。
```
