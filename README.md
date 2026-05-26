# openclaw_skills

OpenClaw skills for outbound outreach. Sister skills share a canonical
6-phase pipeline (Pull → Enrich → Personalize → Approve → Send → Log).

## Skills

| Skill | Channel | Status |
|---|---|---|
| [`linkedin-outreach`](./linkedin-outreach) | LinkedIn Sales Navigator InMail | v1 |
| [`jp-form-outreach`](./jp-form-outreach) | Japanese corporate inquiry forms | v1 |

See each skill's `SKILL.md` for the spec and usage.

**OpenClaw エージェント向け（5分毎 Slack 進捗）:** [`docs/OPENCLAW_AGENT.md`](./docs/OPENCLAW_AGENT.md)

## Install

```bash
# Clone
git clone https://github.com/shidaweb/openclaw_skills.git ~/.openclaw/skills

# Per-skill setup (example: jp-form-outreach)
cd ~/.openclaw/skills/jp-form-outreach
python3 -m venv .venv
.venv/bin/pip install pyyaml
cp config.example.yaml config.yaml      # edit sender info
cp targets.example.yaml targets.yaml    # edit / extend
.venv/bin/python run.py --help
```

## What's NOT committed

Per `.gitignore`:
- `.venv/` (per-machine Python virtualenv)
- `data/*.jsonl` (skill local state — sent_history, drafts, leads, etc.)
- `config.yaml` (filled-in with personal sender info — commit `config.example.yaml` instead)
- `targets.yaml` (curated company list — usually personal/private — commit `targets.example.yaml` instead)
- `.DS_Store`, `__pycache__/`, etc.

Each skill ships `config.example.yaml` and `targets.example.yaml` as templates.

## Architecture

Both skills implement the same **canonical outreach pattern**:

```
  ┌─────────┐   ┌─────────┐   ┌─────────────┐   ┌─────────┐   ┌──────┐   ┌─────┐
  │ 1. PULL │ → │ 2. ENRI │ → │ 3. PERSONAL │ → │ 4. APPR │ → │5.SEND│ → │6.LOG│
  │         │   │   CH    │   │     IZE     │   │   OVE   │   │      │   │     │
  └─────────┘   └─────────┘   └─────────────┘   └─────────┘   └──────┘   └─────┘
   leads.jsonl   enriched      drafts.jsonl     (interactive)  (browser)  sent_history
                  .jsonl                                                    .jsonl
```

Contract:
- **Idempotent**: re-running any phase doesn't double-process. SKIP'd leads
  live in `skip_history.jsonl`; sent leads in `sent_history.jsonl`. Both
  filtered at Pull and Send phases.
- **Resumable**: each phase reads its predecessor's JSONL output.
- **Append-only history**.
- **Cache-friendly**: phase 3 system prompt is byte-stable for prompt-cache hits.

## Architecture (v3)

| Layer | Model |
|---|---|
| Slack で会話する OpenClaw エージェント | **Opus 4.7**（固定・本リポジトリ外で設定） |
| Python `run.py draft` / `_llm_analyze_form` | **Sonnet 4.6**（`config.yaml` `model.name`） |
| `verify` / `notify` / `progress` | **LLM なし** |

- **OpenClaw agent (Opus)**: リスト生成・承認・エスカレーション（WebSearch + SKILL.md）
- **Python**: JSONL、dedup、フォーム入力、決定論的 verify、Webhook 通知
- **Slack 受信**: OpenClaw Slack plugin
- **Slack 状況通知**: incoming webhook → `notify.py`

```bash
cp sender_brief.example.yaml sender_brief.yaml   # webhook URL を設定

python3 -m _outreach_core.helpers.dump_exclude_set
echo '[{"id":"co","name":"テスト株式会社"}]' \
  | python3 -m _outreach_core.helpers.append_targets --skill jp_form --input - --format jsonl

cd jp-form-outreach
.venv/bin/python run.py send --ids 1 --auto-send --heartbeat slack
.venv/bin/python run.py resolve --target-id co --field 業界=その他
```

See [`_outreach_core/README.md`](./_outreach_core/README.md) and [`docs/SCHEDULING.md`](./docs/SCHEDULING.md).
