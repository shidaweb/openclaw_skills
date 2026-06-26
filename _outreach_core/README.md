# _outreach_core

Python utilities shared by `linkedin-outreach` and `jp-form-outreach`.

**Not included here:** Slack Socket Mode / Bolt workers, list-generation LLM CLIs, Opus via `oc_infer`. Those belong to the OpenClaw agent (Opus 4.7) or Sonnet sub-tasks in `config.yaml`.

## Model policy

| Component | Model |
|---|---|
| OpenClaw Slack agent | Opus 4.7 (outside repo) |
| `oc_infer` default | `claude-cli/claude-sonnet-4-6` |
| `verify` / `notify` / `progress` | No LLM |

## Modules

| Module | Role |
|---|---|
| `campaign.py` | shared List → Enrich → Draft → Send ordering, context guard, completion reconciliation |
| `persona.py` | reusable sender persona registry |
| `routing.py` / `channel_state.py` | Slack-thread campaign/persona/channel routing |
| `history.py` | sent/skip JSONL, `load_global_exclude_set()` |
| `infer.py` | `oc_infer` / `oc_browser` / `oc_evaluate` (Sonnet default for infer only) |
| `prompt.py` | cache-stable `build_system_block`, `extract_first_json` |
| `draft.py` / `preview.py` | Personalize / Approve helpers |
| `verify.py` | post-send verification, `needs_attention.jsonl` |
| `notify.py` | Slack one-way posts (webhook or OpenClaw `botToken` + session channel) |
| `openclaw_slack.py` | Read `~/.openclaw/openclaw.json` + sessions for bot/channel |
| `progress.py` | `current_task.jsonl` + optional heartbeat |
| `config.py` | skill defaults → campaign brief → selected persona merge |
| `helpers/outreach.py` | single resolve/bind/start router for both delivery Skills |
| `helpers/dump_exclude_set.py` | JSON exclude sets for agent |
| `helpers/append_targets.py` | append to targets.yaml / targets.csv |
| `helpers/backfill_canonical_ids.py` | one-shot history backfill |

## Verification

```bash
cd ~/.openclaw/skills
python3 -m pytest _outreach_core/tests

python3 -m _outreach_core.helpers.dump_exclude_set

echo '[{"id":"x","name":"テスト株式会社","industry":"EdTech"}]' \
  | python3 -m _outreach_core.helpers.append_targets --skill jp_form --input - --format jsonl

.venv/bin/python run.py send --ids 1 --auto-send --heartbeat slack   # webhook optional
.venv/bin/python run.py history needs-attention
.venv/bin/python run.py resolve --target-id <id> --field 業界=その他
```

## Runtime-only defaults

Copy `sender_brief.example.yaml` → `~/.openclaw/skills/sender_brief.yaml` (gitignored).

Sender identity should be stored in `personas/<id>.yaml`; this legacy file is
only for runtime defaults such as Slack and heartbeat settings.

```yaml
slack:
  incoming_webhook_url: "https://hooks.slack.com/services/..."
heartbeat:
  interval_sec: 600
```
