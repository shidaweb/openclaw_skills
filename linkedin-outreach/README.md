# linkedin-outreach (v1)

A LinkedIn InMail outreach pipeline driven by OpenClaw browser + infer.

## Install

```bash
mkdir -p ~/.openclaw/skills
cp -r ./linkedin-outreach ~/.openclaw/skills/
cd ~/.openclaw/skills/linkedin-outreach

# Install Python deps (one-time)
pip install pyyaml --break-system-packages

# Reload OpenClaw skills index
openclaw skills list | grep linkedin-outreach
```

## First-time setup

1. **Configure your pitch**

```bash
cp config.example.yaml config.yaml
# Open config.yaml in your editor and fill in pitch / persona / value props
```

The contents of `config.yaml` become the cached system prompt. Keep this
file stable across runs to maintain prompt cache hits.

2. **Make sure Chrome is running with the openclaw profile, signed in to LinkedIn**

```bash
openclaw browser --browser-profile openclaw start
openclaw browser --browser-profile openclaw open https://www.linkedin.com/feed/
```

If not signed in, sign in once manually. Cookies persist.

## Usage

```bash
cd ~/.openclaw/skills/linkedin-outreach

# 1. Pull leads from a Sales Navigator saved search
python run.py fetch-leads \
  --search-url "https://www.linkedin.com/sales/search/people?savedSearchId=XXXXX" \
  --limit 5

# 2. Enrich each lead with profile detail + recent activity
python run.py enrich

# 3. Draft personalized InMails (Sonnet, with cached system prompt)
python run.py draft

# 4. Review all drafts in terminal
python run.py preview
```

## v1 limitations (and v2 roadmap)

**v1 is intentionally semi-automatic.** After `preview`, copy the body of
each approved draft into LinkedIn manually. This is the safest first run
because we can verify:
- The Sales Nav search parser actually finds your leads
- The profile parser pulls useful signals
- The drafts are personalized in a way you'd actually send

**Iterate against real DOM.** The first time you run `fetch-leads`, the
script saves a sample snapshot to `data/sample_search.txt`. The first
`enrich` saves `data/sample_profile.txt`. If parsing seems off, share
those files back and we'll tighten `parse_search_results` /
`parse_profile` against your actual Sales Nav layout.

**v2 will add:**
- Auto-send via `openclaw browser fill` + `click` on the InMail compose UI
- Slack approval flow (preview to channel → emoji react → batch send)
- Daily cron schedule (`openclaw cron add --schedule "0 10 * * 1-5" ...`)
- Reply detection + classification (Haiku) → templated/Sonnet replies

## State files

| File | Stage | Purpose |
|---|---|---|
| `data/leads.jsonl` | fetch-leads | One lead per line, basic fields |
| `data/enriched.jsonl` | enrich | + headline, about, recent_activity |
| `data/drafts.jsonl` | draft | + draft.subject, draft.body |
| `data/sample_search.txt` | fetch-leads | Last search-results snapshot, for parser iteration |
| `data/sample_profile.txt` | enrich | First profile snapshot, for parser iteration |

All `.jsonl` files are append-safe. To restart a stage, delete its output file.

## Cost

Per InMail draft, with the configured Sonnet model and cached system prompt:
- ~2KB cached input (10% of normal price)
- ~1KB variable input (full price)
- ~500 tokens output

100 InMails ≈ negligible API equivalent ($0.25–$0.50). On Claude CLI
subscription routing (which is what your OpenClaw is using), this is
effectively free within the subscription quota.
