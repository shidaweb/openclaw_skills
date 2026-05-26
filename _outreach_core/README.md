# _outreach_core

Python utilities shared by `linkedin-outreach` and `jp-form-outreach`.

**Not included here:** Slack Socket Mode / Bolt workers, list-generation LLM CLIs, Opus via `oc_infer`. Those belong to the OpenClaw agent (Opus 4.7) or Sonnet sub-tasks in `config.yaml`.

## Model policy (v3)

| Component | Model |
|---|---|
| OpenClaw Slack agent | Opus 4.7 (outside repo) |
| `oc_infer` default | `claude-cli/claude-sonnet-4-6` |
| `verify` / `notify` / `progress` | No LLM |

## Modules

| Module | Role |
|---|---|
| `history.py` | sent/skip JSONL, `load_global_exclude_set()` |
| `infer.py` | `oc_infer` / `oc_browser` / `oc_evaluate` (Sonnet default for infer only) |
| `prompt.py` | cache-stable `build_system_block`, `extract_first_json` |
| `draft.py` / `preview.py` | Personalize / Approve helpers |
| `verify.py` | post-send verification, `needs_attention.jsonl` |
| `notify.py` | Slack one-way posts (webhook or OpenClaw `botToken` + session channel) |
| `openclaw_slack.py` | Read `~/.openclaw/openclaw.json` + sessions for bot/channel |
| `progress.py` | `current_task.jsonl` + optional heartbeat |
| `config.py` | `load_merged_config`, `sender_brief.yaml` merge |
| `helpers/dump_exclude_set.py` | JSON exclude sets for agent |
| `helpers/append_targets.py` | append to targets.yaml / targets.csv |
| `helpers/backfill_canonical_ids.py` | one-shot history backfill |

## Verification

```bash
cd ~/.openclaw/skills/linkedin-outreach
.venv/bin/python -m unittest discover -s ../_outreach_core/tests -v

python3 -m _outreach_core.helpers.dump_exclude_set

echo '[{"id":"x","name":"テスト株式会社","industry":"EdTech"}]' \
  | python3 -m _outreach_core.helpers.append_targets --skill jp_form --input - --format jsonl

.venv/bin/python run.py send --ids 1 --auto-send --heartbeat slack   # webhook optional
.venv/bin/python run.py history needs-attention
.venv/bin/python run.py resolve --target-id <id> --field 業界=その他
```

## sender_brief.yaml

Copy `sender_brief.example.yaml` → `~/.openclaw/skills/sender_brief.yaml` (gitignored).

```yaml
slack:
  incoming_webhook_url: "https://hooks.slack.com/services/..."
heartbeat:
  interval_sec: 300
```
