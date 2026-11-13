---
id: whatneedsdoing
family: work
title: precis — triage what needs doing across both substrates
summary: one honest 'what needs doing' — declared repo dev work (backlog, gripes, GitHub PRs/Dependabot) on one side, the prod factory todo queue plus fleet health and latent-bug mining on the other; the bunched repo-hygiene scans live in whatneedsdoing-scans
answers:
  - what needs doing right now across the repo and the prod factory?
  - how do I keep repo dev work and the prod factory queue from merging into one flat list?
  - when is an open gripe actually fixed and closable?
  - how do I read the prod worker-log err/warn histogram for a hot pass?
  - how do I prove a mined LLM-confusion error is live and unfixed before filing it?
applies-to: one honest 'what needs doing' across the repo backlog + the prod factory queue
status: active
tags: [workflow]
kinds: [todo, gripe, alert, job, agentlog, draft]
governs: factory
---

# whatneedsdoing — one honest "what needs doing"

Work lives in **two different substrates** — do not merge them into one
flat list; that merge is the trap this view exists to avoid. Optional
focus: `$ARGUMENTS`.

- **Repo dev work** — the backlog dir + the `gripe` bug tracker. About
  *this codebase / product*: MCP-surface bugs, features, infra fixes. You
  act on them by **editing code in a worktree → [[go]]**. "Inert" here
  means: real, but no one is building it yet.
- **Prod factory queue** — `kind='todo'` rows in the **prod DB**, driven
  by the autonomous dispatch/planner loop on the cluster. *Content/ops
  output* ("write section X", "morning briefing", "import book Y"),
  **not code** — they self-run, or get retried / unblocked / halted **on
  prod**, never by editing this repo.
- **The bridge** — the only real overlap: a prod todo that keeps *failing
  because of a repo bug*. That failure is a symptom; the fix is dev work
  here. Call these out explicitly — they're where a [[go]] clears a prod
  backlog.

The backlog + gripes are *declared* repo dev work; beyond them is *latent*
work — bugs the prod LLM is hitting right now that nobody filed (the
bug-hunt step below). Every recurring tool-call error is a fix waiting in
a skill or the MCP surface; mining it feeds substrate 1 as gripes.

The session precis MCP writes PROD: the `search`/`get` reads below are
fine, but every close or file this pass suggests — a gripe resolution, a
new todo — needs the user's explicit go-ahead where your harness is not
the operator's own. (In the Devin variant, a `// turbo` line before a
command is Devin's auto-approve marker; the command itself is identical.)

Live state at invocation:

- Backlog index (generated `INDEX.md` under the backlog dir, one line per
  item with status):
  !`scripts/docs-index >/dev/null 2>&1; grep -vE '^(<!--|#|\s*$)' "$(find docs -path '*backlog*' -name INDEX.md -print -quit)" 2>/dev/null | head -60`
- GitHub — open PRs:
  !`gh pr list --state open 2>/dev/null || echo '(gh unavailable or no open PRs)'`
- GitHub — open Dependabot alerts (severity ⋅ package ⋅ #num ⋅ summary):
  !`gh api "repos/{owner}/{repo}/dependabot/alerts?state=open&per_page=50" --jq '.[] | "\(.security_advisory.severity)\t\(.dependency.package.name)\t#\(.number)\t\(.security_advisory.summary)"' 2>/dev/null || echo '(dependabot API unavailable — needs a token with repo security-read)'`

## Repo dev — backlog and gripes

1. **Backlog.** Read the generated backlog `INDEX.md` (one line per item
   with status). Skip any item whose front-matter
   `snooze-until: YYYY-MM-DD` date is still in the future — a deliberate
   park, not open work. **Prune done gunk as you go** (same as closing a
   fixed gripe in step 2): `scripts/backlog-lint` lists items whose
   *title* carries a done-marker (DONE/RESOLVED/✅/SHIPPED-fully-cut-over)
   yet whose file still sits in the dir — for each, confirm the work is on
   `main`, then **delete the item's file** (git log + the topic memory
   hold the record). Don't leave it marked "DONE": the backlog dir is the
   *active* list, not an archive — a left-in "DONE" item is the same
   append-only rot the docs triage cured. (The lint excludes
   partially-open items that merely *mention* something shipped.) The
   dark-factory workstream is active.
   **Thread files and the active set.** The active roster is the thread
   files carrying `roster: active` in front matter (`docs/roadmap.md`
   only links its generated view); `scripts/inflight` shows which have a
   session. Inflight has no thread column — match a tree to a thread by
   its purpose line and say the match is inferred. Flag both mismatches
   (active with no session, dormant with one). Then, for every `gr<id>` a
   thread file names, check live status (`get(kind='gripe', id=N)`): a
   closed gripe still ranked is a stale pointer to delete in the owner's
   file (keep "soft-deleted, do not reopen" tombstones) — half the thread
   files were stale this way at the 2026-09-30 pillar review. Open gripes
   that no thread and no item names are orphans; list them by pillar for
   [[pillar-review]] rather than placing them here.
2. **Gripes.** `get(kind='gripe', id='/open')` (the bug tracker). Tracked
   but **not auto-worked** — flag stale or high-impact ones. **Close the
   truly-fixed ones as you go:** if an open gripe's fix sha is in
   `origin/prod` (`git fetch origin prod`, then
   `git merge-base --is-ancestor <sha> origin/prod`), it is not
   open work — leave a one-line resolution comment naming the sha, then
   close it (`put(kind='gripe', id=N, text='resolved: <sha> (r<N>) — <how verified>',
   tags=['STATUS:done'])`; never delete a fixed gripe). Merged to `main` but
   not deployed: leave it open and say so. Leave it open too when its body or
   comments name a pending human or browser check that nobody has reported
   passing. A gripe stays open only if it is genuinely unfixed or unverified.
   Don't let resolved bugs inflate the backlog.

## Repo dev — GitHub, then hygiene

3. **GitHub (PRs + Dependabot).** Declared repo dev work that lives on
   GitHub, not in the backlog dir — the inline previews above are the
   fresh read; expand them here.
   - **Open PRs:** `gh pr list --state open` (add `--json
     number,title,author,isDraft,reviewDecision,statusCheckRollup` for CI +
     review state). Flag each as: green + approved (a one-action **ship/merge**
     item), stalled awaiting review, red CI, or a long-lived draft.
   - **Dependabot alerts:** `gh api
     "repos/{owner}/{repo}/dependabot/alerts?state=open"` — rank by
     `security_advisory.severity`. A `high`/`critical` on the default branch is
     **P0** repo dev work (a dependency bump / patch), fixed here → [[go]]. If
     the API 403s it needs a token with security-read scope — say so rather
     than silently reporting "none".
     - **Honor snoozes.** A snoozed alert lives as a backlog item with
       `snooze-until: YYYY-MM-DD` front-matter. **Suppress** any alert whose
       item's date is still in the future (a known-held item, not new work) —
       don't report it. If today ≥ its `snooze-until`, surface it as
       **recheck due**: re-probe the `Unblock-when` condition, then either act
       ([[go]]) or bump the date +2 weeks. This is what keeps a blocked-upstream
       alert (e.g. #44, `transformers` capped by marker-pdf) from re-nagging
       every triage.
3a. **Repo hygiene.** The bunched advisory scans — migration collisions,
    code anchors, memory index, cadence reviews — live in
    [[whatneedsdoing-scans]]; run the ones due and fold findings into the
    repo-dev section. Diagnosis only; every fix is its own worktree→ship.

## Prod factory queue

4. **Todos.** `search(kind='todo', view='attention')`
   (asking-user + failed children) and `search(kind='todo', view='doable')`
   (what the loop picks up next). NB: these are `search(...)` calls, not
   `get(...)`. This is the only substrate that acts on itself.
4-cfp. **WATCH-tagged calls (cfp re-check).** Run
   `scripts/prod-precis maintenance cfp-watch` (bounded: 25 fetches, refs checked
   <20h ago skipped), then list `search(kind='alert', q='cfp_watch')`.
   Each open alert is a call page whose text or deadline dates changed since
   the last check: surface it under the prod queue with the cfp id and the
   old -> new dates from the alert detail. The weekly `cfp_watch` scheduler
   cadence does the same without you.
4a. **The owner's queue — `waiting-for:<login>`.** Before mining memory or
   chat for "things Reto still has to do", list the durable queue:
   `search(kind='todo', tags=['waiting-for:reto'])` (skill
   `precis-user-queue-help`; the tag value is the person's login exactly as
   `refs.owner_login` stores it — the same string files and lists). Every
   decision, credential, approval or chore that only the owner can do
   belongs there as one row, never as a memory bullet or a chat reminder:
   file new ones you surface this pass, and shrink any memory "OPEN = Reto:
   …" bullet to the todo id. Report the queue as its own short section
   (count + the rows that unblock the most), not merged into either
   substrate — it is the human's list, the factory never dispatches it.

## Prod system health — worker-log err/warn

5. **Is the fleet solid?** The `/status` page footer shows a per-host
   `N err/warn 24h` count (`spark · … · 106`, `melchior · … · 7134`, …); this
   is the same signal, read from the `worker_logs` table. It is a
   **system-health** read, not a work queue — but a broken pass here is often
   the *root cause* of stalled todos in step 4 (the bridge), so mine it.
   **Read the latest doctor report first**: the `doctor_tick` agent authors a
   periodic health report as a `draft` ref (`kind='draft'`,
   `meta->>'author'='doctor'`, newest first — the same body the ops digest
   pushes); if fresh, its classification / diagnosis / needs-a-human sections
   ARE this step — fall back to the raw SQL when the report is stale, absent,
   or you need to drill deeper. Find it with
   `scripts/prod-psql "SELECT ref_id, created_at FROM refs WHERE
   kind='draft' AND meta->>'author'='doctor' ORDER BY created_at DESC LIMIT
   1"`, then `get(kind='draft', id='dr<ref_id>')` for the outline and
   `get(kind='draft', id='dc<chunk>')` for a full section (the
   `dr…~dc…` selector form returns NotFound, gr477862). The doctor
   classifies only what it alerts on: still run the per-host histogram
   below — it costs one query and catches a hot pass the report missed.
   Prod-hop and pull the per-host histogram:
   ```sql
   -- per-host err/warn in 24h (matches the /status footer)
   SELECT host, level, count(*) FROM worker_logs
   WHERE ts > now() - interval '24 hours' AND level IN ('WARNING','ERROR')
   GROUP BY host, level ORDER BY host, level;
   ```
   **Read it by ratio, not absolute count.** A host an order of magnitude above
   its peers is the signal (e.g. melchior 7000+ ERROR vs ~4 on the others).
   Drill the outlier down to the offending pass + message shape:
   ```sql
   SELECT pass, level, count(*) FROM worker_logs
   WHERE ts > now() - interval '24 hours' AND level='ERROR' AND host='<outlier>'
   GROUP BY pass, level ORDER BY count(*) DESC LIMIT 15;
   -- then one full traceback tail:
   SELECT message FROM worker_logs
   WHERE host='<outlier>' AND pass='<pass>' AND level='ERROR'
     AND ts > now() - interval '24 hours' ORDER BY ts DESC LIMIT 1;
   ```
   Then classify — this is the load-bearing judgement:
   - **Broken pass** — near-100% failure, *no* successes. Check the pass's
     success-write table over the same window (does it write *anything*?). This
     is a P0 repo bug or a downed backend; file a gripe or fix. **Zero INFO
     rows for the pass in `worker_logs` is not evidence of zero successes** —
     many passes log only on failure. Count the output table (`llm_summarize`
     → `chunk_summaries.created_at`, embeddings → `chunks.embedding IS NOT
     NULL`, …). On 2026-10-10 a delegated digest called `llm_summarize`
     "broken, 0 successes" from 0 INFO lines while it wrote 11,411
     `chunk_summaries` rows; demand the table count from any agent you
     delegate this to.
   - **Noisy-but-working** — failures *alongside* successes (e.g. `llm_summarize`
     wrote 5.7k `chunk_summaries` rows while logging 7k `empty summary` ERRORs —
     a ~50% parse-failure rate that floods the error surface and wastes ~half the
     compute, but is *not* down). Still a real bug (a false "on fire" reading +
     wasted work) — file a gripe to downgrade the log level / harden the parse,
     but don't page it as an outage.
   - **Baseline noise** — WARNING-heavy hosts (`news_poll` feed timeouts,
     `runner`/`chunk_keywords`/`tag_embeddings` soft-warnings). Report as green.

   Say plainly whether the fleet is **solid** (only baseline noise) or has a
   **hot pass** (broken / noisy), and fold any hot-pass root cause into
   substrate 1 as a gripe.

## LLM-confusion mining — the ledger

6. **The bug hunt (latent repo dev).** Start at the ledger, not the
   transcripts. `tool_calls` (migration 0133, `src/precis/tool_ledger.py`)
   takes one row per `runtime.dispatch()` at the chokepoint every verb call
   passes through — MCP server, CLI and in-process ticks alike — so it sees
   interactive sessions, which the transcript path never did. 30-day
   retention (the `PRECIS_TOOL_CALLS_RETENTION_DAYS` env); no payload
   content, ever (`input_keys` is argument *names* only). One prod-hop:
   ```sql
   SELECT verb, kind, error_type, count(*) n FROM tool_calls
   WHERE ts > now() - interval '7 days' AND outcome='error'
   GROUP BY 1,2,3 ORDER BY n DESC LIMIT 25;
   ```
   **Calibrate before you read it:** the measured error rate is ~0.8% (609
   errors in 73,472 calls over 10 days). Errors are not where the waste is —
   the signal is successful-but-clumsy calls. A full treatment of that is
   the 14-day [[surface-review]] pass (`docs/runbooks/surface-review.md`),
   which mines local transcripts + the ledger + `llm_call_log` through
   `scripts/mine-sessions/`. **If surface-review is inside its window, this
   step is just the ledger query above** — do not re-run the deep mine here.

## LLM-confusion mining — transcripts

   The transcript path below remains correct for windows predating 0133, and
   for the payload a ledger row cannot carry. It has thinned out sharply: a
   recent 10-day window held 2 `plan_tick`, 51 `quest_tick` and 30
   `doctor_tick` transcripts.

   Where the stream lives: every server-side agentic run opens a
   `kind='agentlog'` run-attribution record, but it carries only the
   assembled **input** prompt plus run telemetry — a catalog, not a
   transcript store. The full **output** stream — every tool call/result,
   where `[error:...]` shapes live — is captured for `plan_tick` alone,
   stamped on its own `kind='job'` ref as `meta.transcript`
   (`workers/executors/claude_inproc.py`'s `_run_plan_tick`; capped 1 MiB,
   GC'd after the `PRECIS_TRANSCRIPT_RETENTION_DAYS` window — 30 days by
   default, `workers/sweeper.py::_gc_transcripts`). `quest_tick` appends a
   condensed per-tick digest plus raw output as `meta.transcript_raw` on
   failure (same cap/GC, `quest/tick.py::_persist_job_transcript`) — its
   rows appear in the query but carry digests, not a tool-call stream.
   `dream`, `cad` and `structure` keep nothing minable. So `agentlog`
   outnumbering transcript-bearing `job` refs is expected, not a sign you
   are querying the wrong table. Every `[error:...]` in a transcript is
   the LLM getting a verb wrong — a fix waiting in a skill or the MCP
   surface. Prod-hop via `scripts/prod-psql` (read-only SELECT), pull the
   last 48h, rank error shapes:
   ```sql
   -- histogram of confusion, most-frequent first (plan_tick = full stream;
   -- quest_tick rows = condensed digests + transcript_raw on failure)
   WITH tx AS (
     SELECT meta->>'transcript' t FROM refs
     WHERE kind='job' AND meta ? 'transcript'
       AND created_at > now() - interval '48 hours'),
   m AS (SELECT (regexp_matches(t,
           '\[error:[A-Za-z]+\][^"\\]{0,140}', 'g'))[1] err FROM tx)
   SELECT err, count(*) FROM m GROUP BY err ORDER BY 2 DESC LIMIT 40;
   ```
   For the top shapes, pull one offending transcript and pair each error with
   the tool-call that produced it (walk the stream-json: map `tool_use.id` →
   `input`, join to the `tool_result` carrying `[error:`). That call+error pair
   tells you whether the fix is a **skill** edit (the LLM was never told the
   contract), an **MCP** fix (misleading error message, or a genuine handler
   bug), or a **task-template** fix (a stored todo instruction teaches a broken
   call). Watch for *spin*: one parent re-minting the same failing tick for
   days (dozens of transcripts, same error) is both expensive and a loud bug
   signal — treat it as P0.

## LLM-confusion mining — the live-and-unfixed gate

   **Before you file — prove the error is live AND unfixed** (the mining's two
   false-positive traps; gr51426 hit both):
   - **`created_at` filters the *job*, not when the error happened.** A
     transcript can carry `[error:...]` as *old* failure echoed or *quoted
     text* — most insidiously a **gripe body that itself contains the error
     string** read back into a fresh planner tick — even within the retention
     window, so a raw histogram over-counts. Two filters before trusting a
     count:
     - **Demand a real `tool_result`.** Only count a match that appears inside a
       `tool_result` block with `"is_error":true`, tied to a `tool_use.id` — not
       narration, a prior assistant message, or quoted gripe/backlog text.
       (`transcript LIKE '%tool_result%<err>%'` is a cheap first cut; confirm by
       walking the stream-json.)
     - **Pull the *real* occurrence timestamps.** `SELECT ref_id, created_at`
       for the jobs whose transcript carries the error, ordered `DESC`. If the
       newest is days old / clustered in the past, the error is **not** current.
   - **Cross-reference the fix + deploy date.** Once you've found the callsite,
     `git log -S'<callsite>'` for a fix and check it's deployed
     (`git merge-base --is-ancestor <fix_sha> <deployed_sha>`; deployed sha via
     `direct_url.json`, see the deploy-sha memory). If **every** real occurrence
     predates the fix's deploy, it is **already fixed** — a stale-transcript
     artifact, not new work. Resolve it per step 2 (comment naming the sha →
     soft-delete); do **not** file a gripe or write code. This is the mining
     analogue of the "spin-loop spike usually means redeploy, not new bug" rule.

   Only after both gates: file each distinct, *confirmed-live* root cause as a
   `gripe` (`put(kind='gripe', ...)`; `PRIO:` is one of
   low/normal/high/urgent) so it enters substrate 1, or fix it
   directly.

## Rank and call the gap

7. **Group by substrate, then rank.** Keep the two substrates visually
   separate; within each, highest-impact first with a one-line next action.
   Latent bugs from steps 5–6 (a hot pass, or a mined confusion root cause)
   fold into substrate 1 as new/unfiled repo dev work.
   Dedup the bridge (a gripe whose real cause is a failing todo, or vice
   versa). If `$ARGUMENTS` is set, scope to it.
8. **Call out the gap honestly.** Per substrate: which repo items are
   **actionable here** (fix → [[go]]) vs blocked; which todos are
   **autonomous** (the loop will run) vs **stalled** (bubbled/halted,
   needing a prod unblock or a repo bugfix); and whether the fleet is
   **solid** or has a hot pass (step 5). End with the single
   highest-leverage next action and which substrate it lives in — a [[go]]
   or a prod op. Keep it tight: this is a triage view, not a full read of
   every item.

## See also

- [[whatneedsdoing-scans]] — the bunched advisory hygiene scans (step 3a).
- [[pillar-review]] — where orphan gripes land, by pillar.
- [[surface-review]] — the deeper 14-day mine this pass's ledger read defers to.
- [[precis-todo-help]] — the todo kind the prod factory queue is made of.
