# Outreach briefs (v4 §14)

Each YAML file defines one campaign: product, target ICP, evidence rules, and
sequence. Sender identity and voice live separately in `personas/<persona_id>.yaml`.
Runtime state lives under `{skill}/data/briefs/<brief_id>/`.

Slack thread routing uses the independent tuple
`campaign + persona + channel`. Use `outreach bind` and `outreach start` for
normal operation; channel-wide `brief bind` remains a legacy/default fallback.

## Quick start

`python3 -m _outreach_core.helpers.brief` は **skills ルート**（`~/.openclaw/skills`）でしか動きません。
`jp-form-outreach` などサブディレクトリからはルートのランチャーを使ってください。

```bash
# どの cwd からでも OK
~/.openclaw/skills/brief list
cd jp-form-outreach && python3 ../brief migrate-data --brief torana-line-crm

# または skills ルートで PYTHONPATH 付き -m
cd ~/.openclaw/skills
PYTHONPATH=. python3 -m _outreach_core.helpers.brief list
echo "torana-line-crm" > briefs/_active.txt   # if not already set

# First-time migration from sender_brief.yaml
./brief migrate \
  --from-legacy sender_brief.yaml \
  --from-config jp-form-outreach/config.yaml \
  --from-config linkedin-outreach/config.yaml \
  --to torana-line-crm \
  --display-name "トラーナ LINE×CRM"

./brief migrate-data --brief torana-line-crm
```

## CLI

- `brief list` — all brief ids
- `brief show <id>` — YAML summary
- `brief bind --channel-id C... --brief <id>` — Slack channel binding (main path)
- `brief unbind --channel-id C...` — remove binding
- `brief set-active <id>` — CLI fallback only (`briefs/_active.txt`)
- `brief new <id> --display-name "..."` — copy `_template.yaml`
- `brief migrate` / `migrate-data` — one-time setup
- `brief archive <id>` — move to `briefs/archived/<id>.yaml`
- `brief status [--brief <id>] [--channel-id C...]` — §14-G file-based progress summary
- `brief stop-run [--brief <id>]` — stop campaign via active_run.lock pid
- `brief write-from-json <id> --answers answers.json [--bind-channel C...]` — onboarding output
- `outreach personas` — available sender personas
- `outreach bind --brief <id> --persona <id> --channel <jp_form|linkedin> ...` — bind a Slack thread
- `outreach start ... -- campaign ...` — route to the correct channel Skill and run detached

All `run.py` subcommands accept `--brief <id>` and `--persona <id>`.
