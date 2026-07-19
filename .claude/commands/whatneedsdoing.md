---
description: One honest "what needs doing" across the two work substrates — repo dev work (OPEN-ITEMS backlog + open gripes + open GitHub PRs + Dependabot alerts) and the prod factory queue (open/doable todos) — plus a repo-hygiene scan (migration-number collisions · orphan design docs · memory-index lint), a prod system-health read (per-host worker-log err/warn), and the latent LLM-confusion signal mined from prod agent transcripts.
argument-hint: "[optional focus, e.g. 'dark-factory' or 'drafts']"
allowed-tools: Read, Bash(grep:*), Bash(ssh:*), Bash(gh:*), Bash(scripts/migration-check:*), Bash(scripts/docs-orphans:*), Bash(scripts/memory-lint:*), Bash(scripts/backlog-lint:*), Bash(scripts/token-review:*), Bash(scripts/db-thrash-review:*), Bash(scripts/nightly:*), Bash(scripts/coderef:*), mcp__precis__get, mcp__precis__search
---

Work lives in **two different substrates** — do not merge them into one flat
list, that is the trap this view exists to avoid. Optional focus: `$ARGUMENTS`.

- **Repo dev work** — the `OPEN-ITEMS.md` backlog + the `gripe` bug tracker.
  These are about *this codebase / product*: MCP-surface bugs, features, infra
  fixes. You act on them by **editing code in a worktree → `/go`**. "Inert"
  here means: real, but no one is building it yet.
- **Prod factory queue** — `kind='todo'` rows in the **prod DB**, driven by the
  autonomous dispatch/planner loop on the cluster. These are *content/ops
  output* ("write section X", "morning briefing", "citation audit", "import
  book Y"), **not code**. You do **not** fix these by editing this repo — they
  self-run, or get retried / unblocked / halted **on prod**.
- **The bridge** — the only real overlap: a prod todo that keeps *failing
  because of a repo bug*. That failure is a symptom; the fix is dev work here.
  Call these out explicitly — they're where a `/go` clears a prod backlog.

The backlog + gripes are *declared* repo dev work. Beyond them is *latent*
repo dev work — bugs the LLM is hitting on prod right now that nobody has
filed yet (step 5, the bug-hunt). Every recurring tool-call error is a fix
waiting in a skill or the MCP surface; mining it feeds new items into
substrate 1 (as gripes).

Live backlog headings (repo `OPEN-ITEMS.md`):
!`grep -nE '^(## |- \*\*|- \[ \])' OPEN-ITEMS.md 2>/dev/null | head -60`

Live GitHub — open PRs:
!`gh pr list --state open 2>/dev/null || echo '(gh unavailable or no open PRs)'`

Live GitHub — open Dependabot alerts (severity ⋅ package ⋅ #num ⋅ summary):
!`gh api "repos/{owner}/{repo}/dependabot/alerts?state=open&per_page=50" --jq '.[] | "\(.security_advisory.severity)\t\(.dependency.package.name)\t#\(.number)\t\(.security_advisory.summary)"' 2>/dev/null || echo '(dependabot API unavailable — needs a token with repo security-read)'`

Live repo hygiene — migration collisions ⋅ orphan design docs ⋅ code anchors ⋅ memory index ⋅ done-gunk ⋅ token-review cadence ⋅ db-thrash cadence ⋅ nightly build:
!`scripts/migration-check --quiet 2>&1 || true; echo '— docs —'; scripts/docs-orphans 2>&1 | sed -n '1,2p;/^ORPHAN/,/^ADR-linked/p' || true; echo '— code anchors —'; scripts/coderef check docs 2>&1 | tail -6 || true; echo '— memory —'; scripts/memory-lint 2>&1 || true; echo '— backlog —'; scripts/backlog-lint 2>&1 | head -1 || true; echo '— tokens —'; scripts/token-review 2>&1 || true; echo '— db-thrash —'; scripts/db-thrash-review 2>&1 || true; echo '— nightly —'; scripts/nightly --check 2>&1 || true`

## Procedure

1. **Repo dev — backlog.** Read `OPEN-ITEMS.md`. Take only *open* items. **Prune
   done gunk as you go** (same as closing a fixed gripe in step 2): `scripts/backlog-lint`
   lists items whose *title* carries a done-marker (DONE/RESOLVED/✅/SHIPPED-fully-
   cut-over) yet still sit in the file — for each, confirm the work is on `main`,
   then **delete its entry** (git log + the topic memory hold the record). Don't
   leave it marked "DONE": OPEN-ITEMS is the *active* list, not an archive — a
   left-in "DONE" bullet is the same append-only rot the docs triage cured. (The
   lint excludes partially-open items that merely *mention* something shipped.)
   The dark-factory workstream is active.
2. **Repo dev — gripes.** `get(kind='gripe', id='/open')` (the bug tracker).
   Tracked but **not auto-worked** — flag stale or high-impact ones. **Close
   the truly-fixed ones as you go:** if an open gripe's fix has already merged
   to `main` (and ideally deployed), it is not open work — leave a one-line
   resolution comment naming the sha, then soft-delete it
   (`put(kind='gripe', id=N, text='resolved in <sha> …')` →
   `delete(kind='gripe', id=N)`; history is preserved). A gripe stays open only
   if it is genuinely unfixed or unverified. Don't let resolved bugs inflate
   the backlog.
3. **Repo dev — GitHub (PRs + Dependabot).** Declared repo dev work that
   lives on GitHub, not in `OPEN-ITEMS.md` — the inline previews above are the
   fresh read; expand them here.
   - **Open PRs:** `gh pr list --state open` (add `--json
     number,title,author,isDraft,reviewDecision,statusCheckRollup` for CI +
     review state). Flag each as: green + approved (a one-action **ship/merge**
     item), stalled awaiting review, red CI, or a long-lived draft.
   - **Dependabot alerts:** `gh api
     "repos/{owner}/{repo}/dependabot/alerts?state=open"` — rank by
     `security_advisory.severity`. A `high`/`critical` on the default branch is
     **P0** repo dev work (a dependency bump / patch), fixed here → `/go`. If
     the API 403s it needs a token with security-read scope — say so rather
     than silently reporting "none".
     - **Honor snoozes.** Read the `## ⏸️ Snoozed` section of `OPEN-ITEMS.md`
       first. **Suppress** any alert whose `#num` is listed there with a
       `Recheck-after` date still in the future (a known-held item, not new
       work) — don't report it. If today ≥ its `Recheck-after`, surface it as
       **recheck due**: re-probe the `Unblock-when` condition, then either act
       (`/go`) or bump the date +2 weeks. This is what keeps a blocked-upstream
       alert (e.g. #44, `transformers` capped by marker-pdf) from re-nagging
       every triage.
3a. **Repo hygiene (bunched structural checks).** Read the inline preview above
   — three cheap scans, all advisory. This is *diagnosis bunched*; each fix is
   its own worktree→ship.
   - **Migration collisions** (`scripts/migration-check`) — a flagged number =
     two worktrees will collide on ship; renumber the **unshipped** one above
     main's max. (Known live case: the `email` worktree's `0074`.)
   - **Orphan design docs** (`scripts/docs-orphans`) — ORPHAN / ADR-linked
     buckets are candidates for the `docs-triage` skill; load-bearing ones (src
     / anchor / sealed-migration refs) are fine, leave them.
   - **Code anchors** (`scripts/coderef check docs`, drift-only) — each `✗` = a
     doc cites a `file.py::Qual.name` whose symbol no longer resolves (renamed/
     removed → fix the anchor, or if the code was deliberately removed leave it).
     High-signal, tree-wide. For the bare-`file.py:line`-refs-that-will-rot
     upgrade nudges, run `scripts/coderef check --bare <file>` on a doc you're
     editing (kept off the tree-wide run so it isn't a firehose). Advisory.
     Convention: `docs/conventions/code-anchors.md`.
   - **Memory index** (`scripts/memory-lint`) — a broken link / unindexed file,
     a flagged **landed thread** (a `## Threads` bullet whose cited commits are
     all in main → verify + delete), or over-budget are quick fixes. On
     **reconsolidation DUE** (last full pass an earlier day) run
     `scripts/memory-lint --currency` for the per-claim ledger (gone kebab
     branch/worktree naming unshipped work · repo path missing on main), then
     judgment-resolve each suspect — **adjust** the anchor, **kill** the memory,
     or **promote** a decision to an ADR/doc — plus the compact+delete-landed
     pass, then append a dated line to `memory_consolidation_log.md`; on "done
     today", skip the heavy pass (**once/day at most** — constant re-auditing
     churns without benefit).
   - **Token-review cadence** (`scripts/token-review`) — on **DUE** (last pass
     >7 days ago) run the session-tightness scan: read recent large local
     transcripts for repeated token-waste (context bloat, wrong-tier agents,
     un-`rtk`'d firehoses, redundant calls), file findings to OPEN-ITEMS /
     gripes, then append a dated line to `docs/runbooks/token-review.md`. Inside
     the 7-day window it's quiet — skip it.
   - **DB-thrash cadence** (`scripts/db-thrash-review`) — on **DUE** (last pass
     >14 days ago) run the prod index/thrashing review: prod-hop and run the four
     `pg_stat_*` scans (long-running queries · seq-scan-heavy tables · never-used
     indexes · dead-tuple bloat), interpret outliers by *ratio* (lifetime
     `idx_scan=0` = safe to drop; a small table full-scanned often = throttle the
     caller, not an index; an unindexed fleet-wide GC = single-flight + index),
     file findings to OPEN-ITEMS/gripes, then append a dated line to
     `docs/runbooks/db-thrash-review.md`. Inside the 14-day window it's quiet —
     skip it. (This cadence exists because an unindexed GC pegged caspar for
     hours unnoticed; migs 0077/0078.)
   - **Nightly build** (`scripts/nightly --check`) — the LOCAL full-suite health
     read (not GitHub). **`✗ RED`** means green main was broken by upstream
     dependency drift (the ship gate can't catch it — no code changed);
     investigate the named failing tests. **`DUE`** (last run >24h ago) →
     **delegate the refresh to a background `test-runner` agent** (haiku) running
     `scripts/nightly` — the suite takes minutes, so it runs off the main loop
     and records green/red to `.nightly-status.md` for the next `--check`. Do
     **not** run the suite inline here (it would block the report). A fresh
     `✓ green`, or a `DUE` you've just delegated, needs nothing more.

4. **Prod factory queue — todos.** `search(kind='todo', view='attention')`
   (asking-user + failed children) and `search(kind='todo', view='doable')`
   (what the loop picks up next). NB: these are `search(...)` calls, not
   `get(...)`. This is the only substrate that acts on itself.
5. **Prod system health — worker-log err/warn (is the fleet solid?).** The
   `/status` page footer shows a per-host `N err/warn 24h` count
   (`spark · … · 106`, `melchior · … · 7134`, …); this is the same signal, read
   from the `worker_logs` table. It is a **system-health** read, not a work
   queue — but a broken pass here is often the *root cause* of stalled todos in
   step 4 (the bridge), so mine it. Prod-hop and pull the per-host histogram:
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
     is a P0 repo bug or a downed backend; file a gripe or fix.
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
6. **Latent repo dev — LLM-confusion mining (the bug hunt).** The server-side
   agent runs (`plan_tick`, dream, cad/structure propose) store their full
   `claude -p` tool-call transcript in `refs.meta.transcript` on the
   `kind='job'` ref. Every `[error:...]` in a transcript is the LLM getting a
   verb wrong — a fix waiting in a skill or in the MCP surface. There is **no**
   interactive tool-call ledger (the live `precis serve` path logs nothing), so
   these job transcripts are the signal. Prod-hop (`agent_rw` has SELECT; see
   CLAUDE.md "Peeking at prod"), pull the last 48h, and rank error shapes:
   ```sql
   -- histogram of confusion, most-frequent first
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

   **Before you file — prove the error is live AND unfixed** (the mining's two
   false-positive traps; gr51426 hit both):
   - **`created_at` filters the *job*, not when the error happened.** Transcripts
     are persisted **forever** on their `kind='job'` ref, so `[error:...]` in a
     "recent" transcript may be an *old* failure echoed or *quoted text* — most
     insidiously a **gripe body that itself contains the error string** read back
     into a fresh planner tick. A raw histogram over-counts. Two filters before
     trusting a count:
     - **Demand a real `tool_result`.** Only count a match that appears inside a
       `tool_result` block with `"is_error":true`, tied to a `tool_use.id` — not
       narration, a prior assistant message, or quoted gripe/OPEN-ITEMS text.
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
   `gripe` (`put(kind='gripe', ...)`) so it enters substrate 1, or fix it
   directly.
7. **Group by substrate, then rank.** Keep the two substrates visually
   separate; within each, highest-impact first with a one-line next action.
   Latent bugs from steps 5–6 (a hot pass, or a mined confusion root cause)
   fold into substrate 1 as new/unfiled repo dev work.
   Dedup the bridge (a gripe whose real cause is a failing todo, or vice
   versa). If `$ARGUMENTS` is set, scope to it.
8. **Call out the gap honestly.** Per substrate: which repo items are
   **actionable here** (fix → `/go`) vs blocked; which todos are **autonomous**
   (the loop will run) vs **stalled** (bubbled/halted, needing a prod unblock
   or a repo bugfix); and whether the fleet is **solid** or has a hot pass
   (step 5). End with the single highest-leverage next action — and
   say which substrate it lives in, so the reader knows whether it's a `/go` or
   a prod op.

Keep it tight — this is a triage view, not a full read of every item.
