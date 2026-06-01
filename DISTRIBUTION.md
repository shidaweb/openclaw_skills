# 配布ガイド — ローカル / Git の分割（multi-client distribution）

このリポジトリを **GitHub 経由で複数クライアント企業へ配布**する前提の、
「Git 共有（全社共通）」と「ローカル専用（各社固有・秘匿・実データ）」の境界。

各社は **Git から共有コードを取得 → ローカル専用ファイルを自分で用意**して運用する。
クライアント固有情報（屋号・氏名・連絡先・カレンダーURL・実ターゲット・送信履歴）は
**絶対に Git に入れない**。

## 1. ローカル専用（git 管理外。各社が自分で作成・編集）

`.gitignore` で除外済み。Git には入らない。

| ファイル / ディレクトリ | 中身 | 用意の仕方 |
|---|---|---|
| `<skill>/config.yaml` | 発信者情報・pitch・モデル設定（実値） | `config.example.yaml` をコピーして記入 |
| `sender_brief.yaml` | 全チャネル共通の発信者ブリーフ（実値） | `sender_brief.example.yaml` をコピー |
| `briefs/<id>.yaml` | 各 brief（人格）の実定義 | `briefs/_template.yaml` から作成 / Slack onboarding |
| `briefs/_active.txt` | この環境のアクティブ brief id | `briefs/_active.txt.example` を参考に1行記入 |
| `<skill>/targets/<id>.yaml` / `.csv` | **実ターゲット企業リスト** | リサーチで生成 / `targets.example.*` 参照 |
| `<skill>/prompts/system_persona.local.md` | 自社の作り込みペルソナ（氏名・実事例・実URL） | `system_persona.md` をコピーして編集 |
| `<skill>/prompts/examples.local.md` | 自社の few-shot（実クライアント事例） | `examples.md` をコピーして編集 |
| `**/data/` | 送信履歴 / ドラフト本文 / スナップショット / 学習統計 / run 状態 | 実行時に自動生成 |
| `data/channel_state/C*.json` | Slack チャンネル↔brief バインド | `./brief bind` で生成 |
| 秘匿値（Slack webhook / bot token / gateway token / solver API key） | — | 各社の config / OpenClaw 側に格納 |

### プロンプトの `.local` 上書き（重要）

`prompts/system_persona.md` と `examples.md` は **中立テンプレ**として Git で共有する。
ローダー（`_outreach_core/prompt.py` `_prefer_local`）は、同じフォルダに
`*.local.md` があれば**そちらを自動優先**する。各社は自社版を `*.local.md` として置く
（git 管理外）。→ 全社共通の中立テンプレを配りつつ、各社の作り込みはローカルに留まる。

## 2. Git 共有（全社共通。固有名詞・秘匿値を含めない）

- `_outreach_core/` 全ソース、`<skill>/run.py`、launcher（`brief`/`job`/`healthcheck`/`report`）
- テスト `_outreach_core/tests/`
- **テンプレ/例**: `*.example.yaml` / `*.example.csv` / `briefs/_template.yaml` /
  `briefs/onboarding_answers.example.json` / `briefs/_active.txt.example` /
  `prompts/system_persona.md` / `prompts/examples.md`（**中立**）
- ドキュメント（README / SKILL.md / docs / CURSOR_INSTRUCTIONS*）
- `.gitignore`

## 3. 新規クライアントのブートストラップ

```bash
git clone <repo> ~/.openclaw/skills && cd ~/.openclaw/skills
cp jp-form-outreach/config.example.yaml jp-form-outreach/config.yaml   # 記入
cp sender_brief.example.yaml sender_brief.yaml                          # 記入
cp briefs/_template.yaml briefs/<your-id>.yaml                          # 記入
echo "<your-id>" > briefs/_active.txt
# 自社の作り込みプロンプトを使う場合（任意）:
cp jp-form-outreach/prompts/system_persona.md jp-form-outreach/prompts/system_persona.local.md  # 編集
cp jp-form-outreach/prompts/examples.md       jp-form-outreach/prompts/examples.local.md        # 編集
```

## 4. 配布前に必ず対応（オーナー作業）

1. **git 履歴のスクラブ（重要）**: 初期コミットに過去の実データ（`**/data/*.jsonl` =
   実プロスペクトの連絡先・ドラフト本文）が**履歴として残存**している。フル履歴のまま
   公開/配布すると各社に渡る。対応はどちらか:
   - **推奨**: 現在のツリーから**新しい配布用リポジトリを作成**（履歴を持ち込まない / squash）。
   - または `git filter-repo` / BFG で該当データを履歴から除去。
2. **トラッキング解除**（gitignore に追加したが既にトラック済みのファイル）:
   ```bash
   git rm --cached briefs/_active.txt
   # （prompts/*.local.md と data/ 等は元々未トラックなので不要）
   ```
3. **残りの example/テンプレの中立化レビュー**（固有名詞が残っていないか）:
   - `sender_brief.example.yaml` / `briefs/torana-line-crm.example.yaml` を一読し、自社色を除去 or
     汎用サンプルに置換。
   - `linkedin-outreach/prompts/{system_persona,examples}.md` も jp-form と同様に **`.local` 分離**
     （本対応は jp-form を先行実施。linkedin は同方式で未実施）。
4. **秘匿値の混入チェック**（配布直前）:
   ```bash
   git grep -nI -e "@.*\.co\.jp" -e "hooks.slack.com/services" -e "xoxb-" -e "linkedin.com/in/" \
     -- $(git ls-files | grep -v tests/) || echo "clean"
   ```

> 本対応で jp-form の PII（実電話/メール/カレンダーURL/氏名/屋号）と実ターゲット・データは
> Git から除外済み。残タスクは上記4点（特に履歴スクラブ）。
