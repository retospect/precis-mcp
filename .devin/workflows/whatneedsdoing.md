---
description: One honest "what needs doing" across the two work substrates — repo dev work (OPEN-ITEMS backlog + open gripes + GitHub PRs/Dependabot) and the prod factory queue (open/doable todos) — plus repo-hygiene scans, a prod fleet-health read, and LLM-confusion mining from prod transcripts.
---

Work lives in **two different substrates** — do not merge them into one flat
list. The user may pass an optional focus (e.g. 'dark-factory') to scope to.

- **Repo dev work** — `OPEN-ITEMS.md` backlog + the `gripe` bug tracker.
  About *this codebase*; acted on by editing code on a feature branch → /go.
- **Prod factory queue** — `kind='todo'` rows in the prod DB, driven by the
  autonomous dispatch/planner loop. Content/ops output, **not code** — they
  self-run or get retried/unblocked **on prod**.
- **The bridge** — a prod todo failing *because of a repo bug*. Call these
  out explicitly — they're where a /go clears a prod backlog.

NOTE (Devin): the session precis MCP writes PROD. All the reads below
(`search`/`get`) are fine; any *closing/filing* of gripes or todos it
suggests needs the user's explicit go-ahead first.

## Procedure

1. **Repo dev — backlog.** Read `OPEN-ITEMS.md` headings:
// turbo
   `grep -nE '^(## |- \*\*|- \[ \])' OPEN-ITEMS.md | head -60`
   Take only *open* items. Run `scripts/backlog-lint` — for each done-marker
   title it flags, confirm the work is on `main`, then **delete the entry**
   (git log holds the record; OPEN-ITEMS is the active list, not an archive).

2. **Repo dev — gripes.** `get(kind='gripe', id='/open')`. Tracked but not
   auto-worked — flag stale or high-impact ones. If an open gripe's fix has
   already merged to `main`, propose closing it (resolution comment naming
   the sha → soft-delete) — **prod write, ask the user first**.

3. **Repo dev — GitHub.**
// turbo
   `gh pr list --state open`
// turbo
   `gh api "repos/{owner}/{repo}/dependabot/alerts?state=open&per_page=50" --jq '.[] | "\(.security_advisory.severity)\t\(.dependency.package.name)\t#\(.number)\t\(.security_advisory.summary)"'`
   Flag each PR: green+approved (one-action merge), stalled, red CI, or stale
   draft. Rank alerts by severity — `high`/`critical` on the default branch is
   P0 repo dev work. **Honor snoozes**: suppress any alert listed in
   `OPEN-ITEMS.md` `## ⏸️ Snoozed` with a future `Recheck-after` date; if due,
   re-probe the `Unblock-when` condition, then act or bump the date +2 weeks.
   If the API 403s, say so — don't report "none".

4. **Repo hygiene (bunched advisory scans).** Diagnosis bunched; each fix is
   its own branch→ship.
// turbo
   `scripts/migration-check --quiet; echo '— docs —'; scripts/docs-orphans | sed -n '1,2p;/^ORPHAN/,/^ADR-linked/p'; echo '— anchors —'; scripts/coderef check docs | tail -6; echo '— memory —'; scripts/memory-lint; echo '— backlog —'; scripts/backlog-lint | head -1; echo '— tokens —'; scripts/token-review; echo '— db-thrash —'; scripts/db-thrash-review; echo '— nightly —'; scripts/nightly --check`
   - **Migration collisions** — renumber the *unshipped* file above main's max.
   - **Orphan design docs** — candidates for the `docs-triage` skill;
     load-bearing ones (src/anchor/sealed-migration refs) stay.
   - **Code anchors** — each `✗` = a doc cites a `file.py::Qual.name` that no
     longer resolves; fix the anchor (or leave if the code was removed).
   - **Memory index** — broken links/landed threads are quick fixes; on
     reconsolidation DUE run `scripts/memory-lint --currency` and
     judgment-resolve each suspect (once/day at most).
   - **Token-review cadence** — on DUE, scan recent large transcripts for
     repeated token-waste, file findings, append a dated line to
     `docs/runbooks/token-review.md`. Quiet inside the 7-day window.
   - **DB-thrash cadence** — on DUE, run the four `pg_stat_*` prod scans,
     interpret outliers by *ratio*, file findings, append a dated line to
     `docs/runbooks/db-thrash-review.md`. Quiet inside the 14-day window.
   - **Nightly build** — `✗ RED` = upstream dependency drift broke green main;
     investigate the named tests. `DUE` = kick off `scripts/nightly` as a
     **non-blocking background command** (it records to `.nightly-status.md`);
     don't run it inline.

5. **Prod factory queue.** `search(kind='todo', view='attention')` and
   `search(kind='todo', view='doable')` (searches, not gets). The only
   substrate that acts on itself.

6. **Prod fleet health.** Per-host err/warn histogram (read by *ratio*, not
   absolute count — an order-of-magnitude outlier is the signal):
   ```
   scripts/prod-psql "SELECT host, level, count(*) FROM worker_logs WHERE ts > now() - interval '24 hours' AND level IN ('WARNING','ERROR') GROUP BY host, level ORDER BY host, level;"
   ```
   Drill an outlier down to pass + message shape, then classify:
   **broken pass** (no successes → P0 gripe/fix), **noisy-but-working**
   (failures alongside successes → gripe to harden/downgrade, not an outage),
   **baseline noise** (report green). Say plainly: fleet **solid** or **hot
   pass**; fold hot-pass root causes into substrate 1.

7. **Latent repo dev — LLM-confusion mining.** Job transcripts
   (`refs.meta.transcript` on `kind='job'`) hold every `[error:...]` the prod
   LLM hit. Histogram the last 48h via `scripts/prod-psql` (regexp_matches on
   `\[error:[A-Za-z]+\][^"\\]{0,140}`, group + count). For top shapes, pull
   one transcript and pair error ↔ tool-call. **Before filing, prove it's
   live AND unfixed**: (a) only count matches inside a real
   `tool_result` with `is_error:true` (not quoted gripe text); (b) pull real
   occurrence timestamps — if clustered in the past, it's stale; (c)
   cross-reference the fix + deploy date (`git log -S`, ancestor check vs the
   deployed sha). Already-fixed → resolve per step 2, don't file. Watch for
   *spin* (one parent re-minting the same failing tick for days) — P0.

8. **Group by substrate, then rank.** Two substrates visually separate;
   highest-impact first with a one-line next action. Dedup the bridge. Call
   the gap honestly: actionable-here vs blocked; autonomous vs stalled; fleet
   solid vs hot. End with the single highest-leverage next action — and which
   substrate it lives in (a /go, or a prod op).

Keep it tight — this is a triage view, not a full read of every item.
