# Outreach briefs (v4 §14)

Each YAML file defines one sender persona / product / target ICP. Runtime state lives under
`{skill}/data/briefs/<brief_id>/`.

## Quick start

```bash
cd ~/.openclaw/skills
python3 -m _outreach_core.helpers.brief list
echo "torana-line-crm" > briefs/_active.txt   # if not already set

# First-time migration from sender_brief.yaml
python3 -m _outreach_core.helpers.brief migrate \
  --from-legacy sender_brief.yaml \
  --from-config jp-form-outreach/config.yaml \
  --from-config linkedin-outreach/config.yaml \
  --to torana-line-crm \
  --display-name "トラーナ LINE×CRM"

python3 -m _outreach_core.helpers.brief migrate-data --brief torana-line-crm
```

## CLI

- `brief list` — all brief ids
- `brief show <id>` — YAML summary
- `brief set-active <id>` — write `briefs/_active.txt`
- `brief new <id> --display-name "..."` — copy `_template.yaml`
- `brief migrate` / `migrate-data` — one-time setup
- `brief archive <id>` — rename to `.yaml.archived`

All `run.py` subcommands accept `--brief <id>` (default: `_active.txt`).
