# Slack channel ↔ brief bindings

Each `C*.json` file maps a Slack channel to a Doorman brief.

Created via:

```bash
~/.openclaw/skills/brief bind \
  --channel-id C09D38UGJTC \
  --brief torana-line-crm \
  --default-channels jp_form,linkedin
```

When OpenClaw runs `run.py` with `DOORMAN_SLACK_CHANNEL_ID` set, brief resolution uses this file instead of `briefs/_active.txt`.

These files may contain workspace metadata only (no PII). Commit example bindings only if appropriate for your team.
