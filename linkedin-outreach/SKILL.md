---
name: linkedin-outreach
description: >-
  Run end-to-end LinkedIn prospecting from a curated list or Sales Navigator
  search: validate and enrich leads, create evidence-based tailored copy, send
  either connection requests or InMail according to the brief sequence, verify
  each action, and report exact completion counts. Use for LinkedInリスト作成,
  personalized outreach drafts, connection requests, Sales Nav InMail,
  campaign sends, progress checks, retries, and completion audits.
---

# LinkedIn outreach

Run one accountable workflow:

`LIST → RESOLVE → ENRICH → DRAFT → REVIEW/AUTHORIZATION → SEND → VERIFY → LOG`

Do not call a run complete until requested, verified-sent, pending, skipped, and
failed counts reconcile.

## Shared outreach architecture

Keep these selections independent:

- campaign (`brief`): product, target, evidence rules, sequence;
- persona: sender identity, voice, sign-off;
- channel: `jp_form` or `linkedin` delivery behavior.

Bind the tuple to the current Slack thread and start through the shared router:

```bash
cd ~/.openclaw/skills
./outreach bind --brief tenbin-link --persona tenbin-nori \
  --channel linkedin --slack-channel-id "$DOORMAN_SLACK_CHANNEL_ID" \
  --slack-thread-ts "$DOORMAN_SLACK_THREAD_TS"

./outreach start --brief tenbin-link --persona tenbin-nori \
  --channel linkedin --slack-channel-id "$DOORMAN_SLACK_CHANNEL_ID" \
  --slack-thread-ts "$DOORMAN_SLACK_THREAD_TS" -- \
  campaign --input targets/tenbin-link.csv --limit 10 --clean --skip-send
```

Use `./outreach resolve ...` before launch. The shared CampaignRunner owns
`LIST → ENRICH → DRAFT → SEND`; this skill supplies LinkedIn list, enrichment,
and send operations. When changing persona in an existing campaign workspace,
use `--clean`; the runner rejects cross-persona draft reuse without it.

## Conversation contract

For a non-trivial Slack request, acknowledge in the thread before starting:

```text
👍 受信しました。<brief_id> · linkedin-outreach で進めます。
次は <current phase>、完了条件は <requested count> 件の照合です。
```

Then act. Do not ask a question whose answer is already present in the brief,
target file, thread, or runtime state.

Interpret user intent precisely:

- 「候補を作って」: build and show the list; do not send.
- 「ドラフトを見せて」: run through preview; do not send.
- 「送信を開始」「このN名に送って」: this is send authorization. Run the
  authorized end-to-end path; do not ask “走らせていい？” again.
- A later change such as “JRだけ” narrows the authorized set. Do not keep the
  old broader job running.
- “B” or “後者” refers to the immediately preceding choices. Resolve it from
  thread context instead of asking again.

## Reconstruct state before acting

Never invent a brief id or target filename.

```bash
cd ~/.openclaw/skills
python3 -m _outreach_core.helpers.brief list
python3 -m _outreach_core.helpers.brief status --brief <exact-id> --skill linkedin-outreach
```

Read, in order:

1. `data/channel_state/<channel_id>.json`
2. `linkedin-outreach/data/briefs/<brief>/active_run.lock`
3. `current_task.jsonl` and recent `events.jsonl`
4. open `needs_attention.jsonl`
5. `sent_history.jsonl`

Use only ids printed by `brief list`. If `tenbin-link` exists, do not rename it
to `tenbin-linkedin`.

## Choose the outbound touchpoint

Use `--message-type auto` unless the user explicitly overrides the channel.
`auto` reads the first step of `brief.sequence.steps`:

- first step `cr` → connection request note, max 300 characters, no InMail
  credit, recipient is not yet connected;
- first step `inmail`/`m0` or a legacy brief without a sequence → Sales Nav
  InMail with transmitted subject and body.

Never describe an InMail-only implementation when the brief starts with a
connection request. Never write “Thanks for connecting” in a connection note.

## Build a send-ready list

Use one CSV row per person:

```csv
linkedin_url,name,company,note,evidence_url
```

Before appending a candidate, verify:

- the person exists and the URL belongs to that person;
- the role matches the brief;
- `note` contains at least one factual personalization hook (service, niche,
  post, client type, booking offer, or career fact);
- `evidence_url` points to the public source when the hook did not come from
  LinkedIn;
- the person is not in sent or skip history.

Do not throw away useful facts from the proposed list when writing the CSV.
The list is not ready if it contains only names and guessed company labels.

Public `/in/<slug>/` URLs are valid for connection requests. InMail requires a
real `/sales/lead/<id>` URL; resolve it with:

```bash
cd ~/.openclaw/skills/linkedin-outreach
.venv/bin/python run.py lookup-urls --brief <id> --input targets/<id>.csv --require-sales-nav
```

Reject a lookup result when its displayed name does not match the intended
person.

## Run the campaign

Always launch long work detached so Slack remains responsive. Put Slack flags
on the same command; both before and after the subcommand are accepted.

The launcher must post `🚀 受付` to the originating Slack channel/thread before
it spawns the campaign. If that acknowledgement cannot be delivered, it aborts
the launch instead of running silently. Delivery attempts are recorded beside
the job log as `data/job_logs/<skill>-<run_id>.notify.jsonl`; use that file to
separate a Slack routing failure from a campaign failure.

Preview-only request:

```bash
cd ~/.openclaw/skills
./job start linkedin-outreach campaign \
  --brief <id> --input linkedin-outreach/targets/<id>.csv --limit <N> --clean \
  --message-type auto --skip-send \
  --slack-channel-id "$DOORMAN_SLACK_CHANNEL_ID" \
  --slack-thread-ts "$DOORMAN_SLACK_THREAD_TS"
```

Explicitly authorized send:

```bash
cd ~/.openclaw/skills
./job start linkedin-outreach campaign \
  --brief <id> --input linkedin-outreach/targets/<id>.csv --limit <N> --clean \
  --message-type auto --auto-send \
  --slack-channel-id "$DOORMAN_SLACK_CHANNEL_ID" \
  --slack-thread-ts "$DOORMAN_SLACK_THREAD_TS"
```

For a narrowed subset, create/use a subset CSV or draft only that exact set.
Do not launch the original full list and promise to ignore the rest later.

## Enrichment quality gate

An opened URL is not an enriched profile. Treat empty, tiny, login, not-found,
and unresolved snapshots as failures. A draftable lead needs at least one
verified signal from headline, About, role description, activity, experience,
or the curated research note.

- 0 draftable profiles: fail the job before LLM drafting. Report the URL/data
  blocker and the corrective action.
- Partial readiness: draft ready rows; report every insufficient row by name.
- Never post “enrich完了 10/10” when 10 pages opened but 0 useful profiles were
  extracted.
- Support both Sales Navigator and regular `/in/` profiles in English or
  Japanese UI. Do not infer headless blocking when the snapshot visibly
  contains a headline, `About`/`自己紹介`, or activity text.
- Inspect `data/briefs/<id>/profile_snapshots/` and the non-ready reason
  breakdown before changing browser mode or retrying.
- Do not repeatedly retry the same invalid URL. Resolve it or add verified
  research evidence first.

## Draft quality gate

Each sendable draft must satisfy all of these:

- one verified, non-trivial hook unique to the recipient;
- hook → relevant pain → Tenbin/value proposition is logically connected;
- no fabricated metrics, praise, pain, or outcomes;
- one idea and one soft CTA;
- correct touchpoint and platform character limit;
- no post-accept language in a connection request;
- language matches the profile;
- the first line is not reused across the batch.

`INSUFFICIENT_DATA` is a data task, not a copywriting task. Improve the input;
do not merely tell the model to “think harder.”

Show previews with stable sendable indices, recipient, touchpoint, evidence
hook, character count, and full body. State clearly that the internal label is
not transmitted for connection requests.

## Send and verify

For an already-approved subset:

```bash
cd ~/.openclaw/skills
./job start linkedin-outreach send --brief <id> --ids 1,3 \
  --message-type auto --auto-send \
  --slack-channel-id "$DOORMAN_SLACK_CHANNEL_ID" \
  --slack-thread-ts "$DOORMAN_SLACK_THREAD_TS"
```

Per recipient, require:

1. correct profile opened;
2. correct UI (`Connect` or InMail `Message`) opened;
3. full note/body filled and within the DOM limit;
4. Send clicked only in authorized auto mode;
5. deterministic success evidence observed;
6. append to `sent_history.jsonl` only after verification.

Modal closure alone is not sufficient for a connection request; require a
success/pending signal. A failed or uncertain verification goes to
`needs_attention.jsonl` and is not counted as sent.

## Progress and terminal reporting

Use `pipeline_status.py`, `brief status`, and the detached job log. Do not
guess from a stale PID.

- Start acknowledgement is immediate and mandatory for Slack-routed launches.
- Periodic progress is posted while the campaign is still running.
- Exactly one terminal post is sent by the detached supervisor.
- After terminal completion, periodic Slack notifications stop.

The periodic timer belongs to the whole campaign, not each stage. Post at
`heartbeat.interval_sec` while the campaign process is alive. On process end,
stop the heartbeat before posting again; the detached supervisor owns the one
terminal success/failure notification. Do not post separate stage-completion
messages.

Progress updates contain facts only:

```text
run_id=<id> · phase=enrich · opened=4/10 · draftable=3 · blockers=1
```

Terminal success format:

```text
✅ 完了 run_id=<id>
対象 10 / ドラフト 10 / 送信確認 10 / 保留 0 / SKIP 0
touchpoint=connection-request
```

Partial/failed format:

```text
⚠️ 未完了 run_id=<id>
対象 10 / 送信確認 7 / 保留 2 / SKIP 1
保留: <names + exact reason>
次の処理: <one concrete recovery action>
```

Do not say “走らせた” after a non-zero exit. Do not say “完了” when the command
only reached preview. Do not claim future progress posts unless the job has
the Slack channel and thread ids attached.

## Recovery rules

- CLI exit 2: inspect the exact argparse/brief error before retrying. Correct
  the command; never replay it three times unchanged.
- Unknown brief: run `brief list`, select the exact id, and restart once.
- Active broader run after scope narrowing: stop that run before starting the
  subset.
- Empty enrich: fix profile URLs or curated evidence; do not draft generic
  fallbacks.
- Send uncertainty: leave unsent in history, log needs-attention, and report
  the recipient. Retry only after resolving the UI/evidence problem.
- “進捗は？”: answer from files immediately; acknowledgment is unnecessary.

Useful commands:

```bash
cd ~/.openclaw/skills/linkedin-outreach
.venv/bin/python pipeline_status.py
.venv/bin/python run.py preview --brief <id> --no-send
.venv/bin/python run.py history --brief <id> show
.venv/bin/python run.py history --brief <id> needs-attention
```
