# jp-form-outreach (v1)

> **For the canonical skill spec (the file the OpenClaw skill loader reads)
> see [`SKILL.md`](./SKILL.md).** This README is the human-readable
> getting-started guide.

A B2B inquiry-form outreach pipeline driven by OpenClaw browser + infer.
Sister skill to `linkedin-outreach` — implements the same canonical
6-phase outreach pattern (Pull → Enrich → Personalize → Approve → Send
→ Log) with the same JSONL filenames and skip/sent history files.
Different target/execution surface (Japanese corporate inquiry forms
instead of LinkedIn InMail).

## What this is for

Cold outreach to Japanese mid-market companies via their corporate
inquiry forms. Designed for proposing services to companies that:

- Have a corporate inquiry form (`/contact/`, `/inquiry/`, etc.)
- Are mid-market scale (not so large that your message gets routed to
  /dev/null, not so small that they don't have a B2B窓口)
- You can ground a real personalized hook on (recent PR TIMES release,
  funding round, product launch, etc.)

## Install

```bash
# From the bundled install script:
bash install.sh

# Or manually:
mkdir -p ~/.openclaw/skills
cp -r ./jp-form-outreach ~/.openclaw/skills/
cd ~/.openclaw/skills/jp-form-outreach

# Install Python deps (one-time)
pip install pyyaml --break-system-packages
```

## First-time setup

1. **Configure your pitch**

```bash
cp config.example.yaml config.yaml
# Open config.yaml in your editor and fill in pitch / sender info
```

2. **Curate your target list**

```bash
cp targets.example.yaml targets.yaml
# Open targets.yaml, edit the 22 pre-loaded companies or add new ones
```

The 22 entries in `targets.example.yaml` are pre-populated from the
real-world May 2026 batch (Torana → JP mid-market for LINE×CRM consult)
so you have working examples to copy.

3. **Make sure Chrome is running with the openclaw profile**

```bash
openclaw browser --browser-profile openclaw start
```

(No login needed for most JP corporate inquiry forms.)

## Usage

### One-shot (recommended)

```bash
cd ~/.openclaw/skills/jp-form-outreach

# Full pipeline: pull → enrich → draft → preview (interactive send prompt)
python run.py campaign --clean

# Or stop at preview, send later:
python run.py campaign --clean --skip-send

# Or skip enrich (faster, uses targets.yaml directly):
python run.py campaign --clean --skip-enrich

# Or use research.py for the same one-shot with a final summary:
python research.py --clean
```

### Stage-by-stage

```bash
# 1. Load curated targets from targets.yaml (Pull)
python run.py bootstrap

# 2. Enrich each target with form structure detection
python run.py enrich

# 3. Draft personalized messages (Personalize, Sonnet with cached prompt)
python run.py draft

# 4. Review all drafts and prompt to send (Approve)
python run.py preview            # interactive — picks all/IDs/n at end
python run.py preview --no-send  # display only, no prompt

# 5. Send to specific targets (1-based indices among sendable drafts)
python run.py send --ids 1,2,3 --auto-send    # full auto
python run.py send --ids 1,2,3                 # interactive (y/N per draft)
python run.py send --ids 1,2,3 --no-confirm    # fill-only, manual click
```

## v1 limitations (and v2 roadmap)

**v1 handles 80% of the form variants we've seen**:
- Single-step forms (fill → send)
- Two-step forms (fill → confirm → send)
- Forms with `field_map` overrides in `targets.yaml` for non-standard
  field labels
- reCAPTCHA v3 invisible (passes through automatically)

**v1 does NOT handle automatically**:
- reCAPTCHA v2 visible checkbox — flagged in targets, mode falls back
  to `fill-only`, you complete the CAPTCHA + submit manually
- Forms inside iframes from third-party hosts (BowNow, kintone,
  Microsoft Forms, etc.) — needs per-host parser; flagged as
  `flow: iframe` in targets and skipped
- 郵便番号 auto-fill mismatches (some sites lookup 郵便番号→住所 and
  overwrite your input). Test the first send manually.

**v2 will add**:
- Auto-detect form flow on first enrich (currently you encode it in
  targets.yaml manually)
- iframe handlers for common form hosts (BowNow, kintone)
- Reply detection from `you@example.com` inbox
- Slack approval flow

## State files

| File | Stage | Purpose |
|---|---|---|
| `data/targets.jsonl` | bootstrap | One target per line, from targets.yaml |
| `data/enriched.jsonl` | enrich | + form_fields, char_limit, captcha, flow |
| `data/drafts.jsonl` | draft | + draft.subject, draft.body |
| `data/sent_history.jsonl` | send | Append-only log of sent companies |
| `data/skip_history.jsonl` | draft (auto) | SKIPs from Sonnet — filtered next run |
| `data/sample_form.txt` | enrich | First form snapshot, for parser iteration |

All `.jsonl` files are append-safe. To restart a stage, delete its
output file.

## v25: OTP検出・URLロック・決定の永続化（2026-06-12）

フェリシモ事案（フォーム消失／IRフォーム誤着地／180s stall／再質問ループ）への対策:

- **OTPゲート検出** — enrich/send 時に「確認コード(6桁)送信→入力」型のメール認証
  フローを検知し、即 `status: manual` + `blocker: email_verification_code` で記録。
  無駄な送信試行とリゾルバ再試行を行わない。
- **`form_url_locked`** — ユーザー確認済みURLの自動差し替えを禁止。さらに送信時の
  URLリカバリ先はフォーム種別を再分類し、IR/採用/ログイン等への誤着地を拒否。
- **`pin-url` / `decisions`** — `run.py pin-url --brief <id> --id <target> --url <URL>
  --note "..."` でURLを固定（targets.yaml と leads.jsonl に即反映、`--unlock` で解除）。
  決定は `data/briefs/<brief>/decisions.jsonl` に永続化され、`run.py decisions` で参照
  できる。Slackで合意済みの内容を別プロセスが再質問しないための共有記録。
- **stdoutハートビート** — enrich/send/campaign/draft/resolve-queue 実行中は60秒毎に
  生存ログを出力し、CLI watchdog の no-output stall (180s) を防止。stdout は行バッ
  ファリングへ強制切替。

| `data/decisions.jsonl` | pin-url 等 | ユーザー決定の追記ログ（再質問防止） |

## Differences vs linkedin-outreach

| linkedin-outreach | jp-form-outreach |
|---|---|
| Sales Nav saved search → leads | Curated targets.yaml → companies |
| Lead enrichment = profile + recent activity | Target enrichment = form structure |
| Open Profile detection (free InMail) | reCAPTCHA / iframe detection (skip flag) |
| 1800 chars (InMail body limit) | 400 chars default (most JP forms cap here) |
| 1 message per lead, 5-touch cadence | 1 form submission per company |
| Send via DOM `setValue` on textarea | Send via DOM `setValue` on multiple fields |
| Send button is "Send InMail" / 送信 | Send button is "送信" / "確認" / "送信する" |

## Cost

Per draft, with the configured Sonnet model and cached system prompt:
~negligible API equivalent. On Claude CLI subscription routing, this
is effectively free within the subscription quota.
