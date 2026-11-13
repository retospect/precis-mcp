---
id: whatneedsdoing-scans
family: work
title: precis — the bunched advisory hygiene scans
summary: the advisory-scan half of whatneedsdoing — migration collisions, backlog gunk, code anchors, memory index, rulings-reaching-skills, and the weekly/fortnightly/monthly cadence reviews; each scan is diagnosis-only, every fix its own worktree→ship
answers:
  - which repo-hygiene scans are due and what does a finding mean?
  - how do I read the cadence scripts' DUE/quiet output?
  - what do I do when migration-check, coderef or memory-lint flags something?
  - when do token-review, surface-review, db-thrash, skill-search, gripe-gc, fda-grant and secrets-check run?
applies-to: the repo-hygiene advisory scans bunched into the triage pass
status: active
tags: [workflow]
kinds: [memory, todo]
governs: factory
---

# whatneedsdoing-scans — the bunched advisory scans

The advisory-scan half of [[whatneedsdoing]] — each scan is
diagnosis-only; every fix is its own worktree→ship. Run the ones due and
fold findings into the triage report's repo-dev section. Where a scan
delegates to a named agent (`test-runner`), do that step yourself or run
it in your runtime's background when no subagent runtime exists; Kimi maps
commands via `/skill:<name>`.

Live scan preview at invocation (migration collisions ⋅ code anchors ⋅
memory index ⋅ skill-eval diff ⋅ backlog done-gunk ⋅ the cadence reviews ⋅
nightly build ⋅ main on CI):
!`scripts/migration-check --quiet 2>&1 || true; echo '— code anchors —'; uv run --quiet python scripts/coderef check docs 2>&1 | tail -6 || true; echo '— memory —'; scripts/memory-lint 2>&1 || true; echo '— skill-eval —'; scripts/skill-eval --runner live --diff 2>&1 || true; echo '— backlog —'; scripts/backlog-lint 2>&1 | head -1 || true; echo '— tokens —'; scripts/token-review 2>&1 || true; echo '— surface —'; scripts/surface-review 2>&1 || true; echo '— db-thrash —'; scripts/db-thrash-review 2>&1 || true; echo '— skill-search —'; scripts/skill-search-review 2>&1 || true; echo '— gripe-gc —'; scripts/gripe-gc-review 2>&1 || true; echo '— fda-grant —'; scripts/fda-grant-review 2>&1 || true; echo '— secrets-check —'; scripts/secrets-check-review 2>&1 || true; echo '— nightly —'; scripts/nightly --check 2>&1 || true; echo '— main on CI —'; uv run --quiet python scripts/main-ci-status 2>&1 || true`

## Structural scans

- **Migration collisions** (`scripts/migration-check`) — a flagged number =
  two worktrees will collide on ship; renumber the **unshipped** one above
  main's max. (Known live case: the `email` worktree's `0074`.)
- **Backlog gunk** (`scripts/backlog-lint`) — done-marked items are
  candidates for the [[docs-triage]] skill: verify shipped, then delete.
  Generated indexes (the `INDEX.md` files under the backlog dir and
  `docs/runbooks/`, plus `docs/codebase-map.md`) are gitignored and
  regenerate via `scripts/docs-index` at session start — never
  stale-in-git.
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
  or **promote** a decision to a docstring/doc — plus the compact+delete-landed
  pass, then append a dated line to `memory_consolidation_log.md`; on "done
  today", skip the heavy pass (**once/day at most** — constant re-auditing
  churns without benefit). The same `scripts/memory-lint` call also runs a
  **sibling-repo path check** (weekly, self-gated, no judgment pass needed —
  `~/work/<name>` / `~/work/project(s)/code/<name>` paths memory cites,
  verified to still exist on disk) and self-stamps its own log
  (`~/.cache/precis/memory-sibling-repos.log`, untracked; procedure in
  `docs/runbooks/memory-sibling-repos.md`); a finding there is a suspect
  for the SAME resolve step above (adjust / kill / promote), not a
  separate action.

## Rulings and eval cadences

- **Rulings reaching skills** — list in-force rulings with
  `search(kind='memory', tags=['ruling'], view='rulings')` (carrying
  `review_by` dates: `due today`, `overdue Nd`, or `in Nd, expiring`
  within 3 days), due-soon/overdue first. Rows include `decides:` targets
  when linked with that intent; this is not the full link list. For each
  due or overdue ruling, integrate it into the owning skill or convention
  through a linked gripe, then
  `edit(kind='memory', id='me<N>', meta={'status': 'absorbed'})`; or
  extend `review_by` with `reason=` explaining the delay; or archive a
  moot ruling with `meta={'status': 'archived'}`. Flag every in-force
  ruling with no linked gripe (me548860): check
  `get(kind='memory', id='me<N>', view='links')`, not just `decides:`.
  View contract: `src/precis/utils/memory_rulings.py::render_rulings`.
- **Skill-eval diff** (`scripts/skill-eval --runner live --diff`) — a
  budgeted planned-call `claude -p` dry run over the skills HEAD's
  commit touched (`src/precis/data/skills/`): red = the edited skill
  teaches a verb path its YAML cases don't plan — a misleading-skill
  edit. Advisory, same standing as `scripts/mutate-diff`: a red is a
  to-do for the skill author, never a `scripts/ship` gate stage. No
  touched skills, or a missing/logged-out `claude`, reports `skipped`
  and exits 0 — a skip is not a red. Convention:
  `docs/conventions/skill-evals.md`.
- **Token-review cadence** (`scripts/token-review`) — on **DUE** (last pass
  >7 days ago) run the session-tightness scan: read recent large local
  transcripts for repeated token-waste (context bloat, wrong-tier agents,
  un-`rtk`'d firehoses, redundant calls), file findings as backlog items /
  gripes, then `scripts/token-review --stamp "<note>"`. Inside the 7-day
  window it's quiet — skip it.

## Surface, db and search cadences

- **Surface-review cadence** (`scripts/surface-review`) — on **DUE** (last
  pass >14 days ago) run the [[surface-review]] pass: mine the window with
  `scripts/mine-sessions/run.sh`, read the scoreboard, fan `forensics`
  agents over the evidence cards (plus the random arm), gate for
  live-and-unfixed, then attach the measurement to the existing backlog
  item / gripe — mint only what nothing covers. Then
  `scripts/surface-review --stamp "<note>"`. Inside the 14-day window it's
  quiet — skip it. Remit: the precis MCP surface (verbs, error messages, renders,
  vocabulary, capability gaps) — distinct from token-review's harness
  behaviour and skill-search-review's discovery ranking.
- **DB-thrash cadence** (`scripts/db-thrash-review`) — on **DUE** (last pass
  >14 days ago) run the prod index/thrashing review: prod-hop and run the four
  `pg_stat_*` scans (long-running queries · seq-scan-heavy tables · never-used
  indexes · dead-tuple bloat), interpret outliers by *ratio* (lifetime
  `idx_scan=0` = safe to drop; a small table full-scanned often = throttle the
  caller, not an index; an unindexed fleet-wide GC = single-flight + index),
  file findings as backlog items / gripes, then
  `scripts/db-thrash-review --stamp "<note>"`. Inside the 14-day window it's
  quiet — skip it. (This cadence exists because an unindexed GC pegged caspar for
  hours unnoticed; migs 0077/0078.)
- **Skill-search cadence** (`scripts/skill-search-review`) — on **DUE** (last
  pass >30 days ago) run the skill-search discoverability audit: reconstruct
  `search(kind='skill')` calls from local + prod job transcripts (there's no
  query log — skill-search tool calls aren't durably logged), segment out the
  never-executed (permission-blocked / rejected / hung) noise, stat the
  executed queries, then LLM-eval whether each returned menu + the caller's
  selection made sense, and propose "better search bits" (skill H2/vocabulary
  edits, or a matcher change in `src/precis/handlers/skill.py`). File findings
  as backlog items / gripes, then
  `scripts/skill-search-review --stamp "<note>"`. Inside the 30-day window
  it's quiet — skip it. (This cadence exists because skill search is agents' only
  discovery fallback when they don't know a slug, yet nothing logs how well it
  works; the first pass found the matcher was doing atomic whole-query
  substring matching. Runbook has the pipeline.)

## Tracker and host cadences

- **Gripe-GC cadence** (`scripts/gripe-gc-review`) — on **DUE** (last pass
  >7 days ago) run the weekly gripe-GC backstop: list shipped-marked open
  gripes (the script prints a best-effort count if prod is reachable), and
  close the genuinely-fully-shipped ones — a one-line resolution comment
  naming the sha, then soft-delete (same convention as
  [[whatneedsdoing]]'s gripe step). **Leave open** any gripe that shipped
  *with* open follow-ups — that's still real work, just mislabeled. Then
  `scripts/gripe-gc-review --stamp "<note>"`. Inside the 7-day window it's
  quiet — skip it. (This cadence exists because `issue-closer` only
  inspects the single just-shipped commit, so multi-commit fixes,
  pre-issue-closer gripes, and shipped commits that don't name the gripe
  all slip past it — see the runbook.)
- **FDA-grant cadence** (`scripts/fda-grant-review`) — on **DUE** (last pass
  >30 days ago) audit the macOS Full-Disk-Access grants that let each Mac's
  launchd/cron daemons reach the NAS: per host (melchior/balthazar/caspar;
  spark is Linux → immune), resolve every venv interpreter and confirm it
  is still a python.org framework build (Developer-ID signed) and its
  resolved path is granted (`auth_value=2`). Re-grant (System Settings →
  Full Disk Access — TCC can't be set from CLI) + `launchctl kickstart -k`
  any drift, then `scripts/fda-grant-review --stamp "<note>"`. No brew
  pinning: old `brew pin` markers are vestigial since the python.org
  migration. Inside the 30-day window it's quiet — skip it. The nursery
  `nas-denied` detector is the real-time backstop (alerts in minutes when a
  host is *actually* locked out — the script also prints any open count);
  this cadence is the proactive drift catch. (This exists because a
  `brew upgrade python` cdhash bump silently broke melchior's grant and
  dark-ingested for days; see the runbook + the backlog item on melchior's
  daemon NAS lockout.)
- **Credential-check cadence** (`scripts/secrets-check-review`) — on **DUE**
  (last check >7 days ago) run `scripts/prod-precis secret check`: the
  `/secrets` page's probes against the prod vault, one line per registry
  secret as `ok` / `failed` (HTTP status) / `unprobed` / `missing`, plus the
  last succeeded job for an unprobed secret jobs depend on. Never print,
  paste or store a value — the output has none. File each `failed` or
  non-optional `missing` credential as one `waiting-for:reto` todo with the
  key URL from its line (the owner-queue step in [[whatneedsdoing]]), then
  `scripts/secrets-check-review --stamp "<note>"`. Inside the 7-day window
  it's quiet — skip it. (An expired key otherwise surfaces only as a failing
  job, e.g. `pourbaix_bulk` with `failure_class=config`; runbook has the
  details.) The weekly `secret_check` scheduler cadence runs the same probes
  and raises one alert per `failed` credential — check
  `search(kind='alert', q='secret_check')` for an already-filed finding
  before writing a todo.

## Build and landing-page health

- **Nightly build** (`scripts/nightly --check`) — the LOCAL full-suite health
  read (not GitHub). **`✗ RED`** means green main was broken by upstream
  dependency drift (the ship gate can't catch it — no code changed);
  investigate the named failing tests. **`DUE`** (last run >24h ago) →
  **delegate the refresh to a background `test-runner` agent** (haiku) running
  `scripts/nightly` — the suite takes ~25 min (30k tests, 1413 s on
  2026-10-10; tell the agent so it does not call a quiet run hung — the
  log prints nothing until the end), so it runs off the main loop
  and records green/red to `.nightly-status.md` for the next `--check`. Do
  **not** run the suite inline here (it would block the report). A fresh
  `✓ green`, or a `DUE` you've just delegated, needs nothing more.
- **main on CI** (`scripts/main-ci-status`) — the GitHub read: the newest
  completed `check` run on main's own post-merge push (cancelled runs are
  superseded, not verdicts). **🔴 RED** = a sibling merge landed bytes the
  gated tree never saw (ruff-format drift is the usual case; gr346534).
  The line names the failing jobs and whether an in-flight worktree has
  **claimed** it (its `.claude/purpose` says `red main`/`fix main`). Claimed
  → leave it, sync after their ship. Unclaimed → write the printed purpose
  line first, then fix and ship; the claim is what stops three trees
  shipping the same four-file format fix. "newer run in progress" → wait
  for it before acting. Green needs nothing.
- **Landing page** (GitHub front door: `README.md` plus the docs it links,
  e.g. `docs/setup-single-machine.md` and `docs/mission.md`). Two checks,
  weekly at most; skip when the last pass is under 7 days old. The
  records are `git log -1 --format=%cI -- README.md docs/setup-single-machine.md`
  (last page edit) and `git log --since=<that date> --oneline origin/main`
  filtered to `feat(`/`fix(` subjects (feature changes since).
  - **Age.** If the page is more than 7 days older than main's newest
    `feat(` commit, or `feat(` commits since the page edit touch a
    capability the README lists (or should list), report it as stale
    and name the commits.
  - **Two-persona proofread.** Read the README top to bottom twice.
    (1) A scientist who wants to install and use precis: do the install
    steps run as written, is the capability list accurate, is there a
    quick start that ends in a visible result? (2) A grant reviewer:
    are there screenshots, a capability overview, and evidence of rigor
    (tests, gates, validation numbers)? Dig a little into the tree:
    every claimed feature exists (grep the kind or script it names), and
    every number matches `docs/mission.md` § Facts.
  - **Style.** Readable for agents and humans; no emojis, no em-dashes.
  - **Output.** One PR-sized docs commit per pass (`docs(readme): ...`,
    from a worktree, shipped as usual). A finding you can't fix in that
    commit becomes a gripe. Nothing is sent outward; the page is only
    changed in the repo. Feeds the AI for Science and Claude for OSS
    applications.

## See also

- [[whatneedsdoing]] — the triage pass these scans feed.
- [[surface-review]] — the full MCP-surface mine behind its cadence line.
- [[docs-triage]] — the verdict flow for done-marked backlog gunk.
- [[reconsolidate]] — the memory-cleanup skill behind the memory-index scan.
