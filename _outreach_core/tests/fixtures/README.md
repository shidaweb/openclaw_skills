# Fixtures — regression-locked production patterns

v30 §WS-G. These files are **synthesized** aria-tree / golden samples that
reproduce production failure modes without including client PII or real
prospect data (the actual `resolve_snapshot_*.txt` files under
`jp-form-outreach/data/briefs/*/` are gitignored).

Each fixture exists because a real run failed in a way that needed code
changes. Keeping the synthesized pattern under version control means a
future refactor cannot accidentally re-open the same hole — the test
loads the file at runtime and asserts the parser / wizard / formatter
returns the expected verdict.

## Layout

```
fixtures/
├── aria_snapshots/   - Playwright aria-tree snippets that triggered bugs
│                       (parser, classifier, wizard)
└── slack_golden/     - Per-target Slack line / actionable-payload golden text
```

## Conventions

- One concern per file. A fixture should target ONE bug pattern, not bundle
  several.
- Filenames start with the production target id ("fujisoft_…",
  "super_studio_…") so the trace from log → needs_attention → fixture is
  easy to follow even months later.
- Each fixture has a sibling comment block at the top of the file
  explaining (1) what production run produced the pattern, (2) which code
  change closed the hole, (3) what the test asserts.
- Do NOT include real email / phone / company-internal info from production
  logs. Mask or synthesize.
