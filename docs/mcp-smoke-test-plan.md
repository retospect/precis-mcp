# precis-mcp live smoke-test plan

> **⚠ Keep track of odd behaviour and report on it.**
>
> Anything that surprises you — error messages that are too vague or
> too verbose, successful responses that look empty, hint strings that
> don't parse, surfaces that hang, docstrings that lie about what a
> tool accepts, invocation patterns that "shouldn't work but do", cost
> footers that disappear, next-hints that point at non-existent ids —
> **record it in your session log**, even if you're not sure it's a
> bug.  Patterns in the aggregate are more informative than any single
> observation.  We'll triage on the way out.

A Cascade-executable plan for exercising the precis-mcp surface via its
MCP client tools (`mcp5_*`).  Reusable across sessions — the plan is
stateless; results go in a per-run log at the bottom.

**Audience**: Cascade.  Work through the sections you intend to cover,
tick boxes in your session log, and open bug tickets (either in-file
regression notes or separate commits) for anything that fails.

**Not a substitute for**: the unit-test suite in
`@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests`.
Those exercise internals; this exercises the live MCP surface.

---

## Handoff note for a fresh Cascade session

You are Cascade, picking up this plan from a previous session.  Here's
what you need to know before you start.

### What you're doing

Exercise the `precis-mcp` MCP server via the `mcp5_*` tools available
in your tool list.  Record pass/fail for each step in the session log
at the bottom of this file.  File bug tickets (as regression entries
in §17 or as separate commits with tests) for anything that fails.

### Invocation pattern — always use this form

The canonical way to address a kind is **`type=<kind>, id=<path>`**.
Example:

```
mcp5_get(type='skill', id='/kind/quest')        — GOOD
mcp5_get(type='quest', id='/recent')            — GOOD
mcp5_search(type='paper', query='membrane')     — GOOD

mcp5_get(id='skill:/kind/quest')                — works, but less consistent
mcp5_get(id='quest')                            — BAD (looks up a paper slug 'quest')
mcp5_get(type='quest')                          — BAD (missing id/path)
```

`id=` is a **path within the kind's namespace**, not the kind name.
When a skill body says ``get(id='quest:<short-uuid>')`` the right
translation is ``mcp5_get(type='quest', id='<short-uuid>')``.

### Don't rediscover these known bugs

- **`PoolTimeout: couldn't get a connection after 30.00 sec` on quest**
  means `DATABASE_URL` isn't set to a reachable Postgres.  The 30s
  hang is the real bug — fast-fail to 3s + `UNAVAILABLE` classification
  is queued for Milestone A.  If you hit this, record it but don't
  re-investigate.
- **`math` kind hidden at startup** when `WOLFRAM_APP_ID` is unset is
  working-as-designed (graceful degradation).  Not a bug.
- **YouTube pre-existing failures** — if `youtube-transcript-api` isn't
  in the venv, skip §12 rather than debug.

### If the MCP surface looks stale

Run `mcp5_stats()` first.  If `quest` and `skill` don't appear in the
`kinds by verb:` list, the MCP host is running an old precis wheel.
Ask the human to restart Windsurf's MCP host.  The expected shape
after restart:

```
kinds by verb:
  search conversation, flashcard, memory, paper, quest, research,
         skill, think, todo, web, youtube
  [same for get, put, move]
```

### How to record a failure

Use this shape in your session log:

```
| §4.2 | get quest:/recent | ✗ | PoolTimeout after 30s — DATABASE_URL issue, known, §17 |
```

For new bugs: add a regression entry at the bottom of §17 and (if you
have time + confidence) a unit test in
`@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests`
that captures the failure shape.

### When to stop

Quick-smoke takes ~2 minutes.  Full-regression (all sections) takes
~30-60 minutes.  If the human is waiting, do §2 first and report; then
drill into sections they care about.  Don't do §10 (web) or §11
(research) without asking — they cost real money.

### Write-roundtrip protocol

Every write-surface step in this plan follows a **four-step
roundtrip** so you actually verify the side-effect instead of trusting
the response string:

1. **Write** — ``put(type='<kind>', id='<unique-test-id>', text='...', mode='append')``.
   Use a unique id per run (e.g. ``smoke-<UNIX-TIMESTAMP>``) so the
   test can be re-run without collisions.
2. **Read back** — ``get(type='<kind>', id='<unique-test-id>')``.
   Assert the content you wrote is present.  A successful-looking put
   response that silently didn't persist is the bug pattern we're
   hunting.
3. **Delete** — ``put(type='<kind>', id='<unique-test-id>', mode='delete')``.
4. **Read-gone** — ``get(type='<kind>', id='<unique-test-id>')``.
   Assert ``ID_NOT_FOUND`` (or the kind's equivalent absent-state).  A
   delete that reported success but left the row is also the bug
   pattern we're hunting.

If step 2 fails after step 1 reported success, that's a **write-path
bug**: record verbatim, file a regression entry.  If step 4 fails
after step 3 reported success, that's a **delete-path bug**: same
treatment.  Either way, the "successful" response from step 1 or 3 is
misleading and should be captured in the session log.

For kinds with a replace / mode-change mutation (e.g. ``fc`` rate,
``todo`` done, ``memory`` add-block-to-existing-ref), add a fifth step
between 2 and 3:

* **2a.** Apply the mutation; read back and assert the mutation
  landed (not just that the row still exists).

The quick-smoke path (§2) skips all of this — it's read-only by
design.  Full-regression runs of write sections MUST do the roundtrip.

---

## 0.  Prerequisites

Before running, confirm what's wired up.  Each section notes its own
requirements; this is the global picture.

### Environment

| var              | used by        | behaviour when unset                         |
| ---------------- | -------------- | -------------------------------------------- |
| `DATABASE_URL`   | quest          | first call errors (soft); kind still listed  |
| `QUEST_SCHEMA`   | quest          | defaults to `papers`                         |
| `WOLFRAM_APP_ID` | math           | kind hidden at startup with warning          |
| `PERPLEXITY_API_KEY` | web, research | first call errors with UPSTREAM_ERROR   |

### Optional packages

| package                   | used by    | behaviour when missing                  |
| ------------------------- | ---------- | --------------------------------------- |
| `youtube-transcript-api`  | youtube    | first call errors with UPSTREAM_ERROR   |
| `acatome-store`           | paper, memory, conversation, flashcard, todo | kind hidden at startup |
| `acatome-quest-mcp`       | quest      | kind hidden at startup                  |

### Filesystem

| path                                                                   | used by  | note                                         |
| ---------------------------------------------------------------------- | -------- | -------------------------------------------- |
| bundled `precis/skills/*/SKILL.md`                                     | skill    | 6 skills ship in the wheel                   |
| any user-configured skill scan paths                                   | skill    | via `kinds_config.py` `skill.scan_paths`     |
| document targets for put/get with ext `.docx` / `.tex` / `.md` / `.txt` | file types | need a writable working dir                |

### Startup check

Run before anything else in a fresh session:

```
mcp5_stats()
```

Expected: `kinds by verb` lists (at minimum) `paper, todo, flashcard,
memory, conversation, web, research, think, youtube, quest, skill`.
Record startup warnings in the session log.

---

## 1.  Session log template

Copy this block to the bottom of the file under `## Session log` when
you start a new run; fill it in as you go.

```
### Session: YYYY-MM-DD HH:MM  (Cascade / human notes)

- precis-mcp version: <git sha or tag>
- MCP host: <which one>
- Scope: quick-smoke | full-regression | section X only
- Kinds visible at startup: <paste from mcp5_stats>
- Startup warnings: <paste from mcp5_stats>

| § | step | result | note |
|---|------|--------|------|
| 2.1 | skill list    | ✓ | 6 skills returned |
| 2.2 | skill get     | ✓ | find-paper body renders |
| ... | ...           | ✗ | <paste error> |
```

---

## 2.  Quick-smoke path  (non-destructive, ~2 minutes)

Use when you just want "is it alive?"  All calls are reads.  No DB
state mutated, no files written.

- [ ] `mcp5_stats()` — record visible kinds + warnings
- [ ] `mcp5_get(id='skill:/')` — skill list
- [ ] `mcp5_get(id='skill:find-paper')` — render one skill body
- [ ] `mcp5_get(id='paper:')` — bare paper landing (may be empty/help)
- [ ] `mcp5_search(query='membrane', type='paper', top_k=3)` — paper search
- [ ] `mcp5_get(id='quest:/recent')` — quest recent view (read-only)
- [ ] `mcp5_get(id='think:')` — think landing (Perplexity-backed; bare
  call is free, shows model + usage)
- [ ] `mcp5_search(query='precis', type='paper')` — paper-corpus search
  (global-no-type search is rejected by design; see §15.1)

If every one returns a response that isn't `!! UNEXPECTED`, the server
is alive and wire-protocol-correct.  Move on to targeted sections.

---

## 3.  skill  (filesystem-native, always available)

No deps.  No DB.  Non-destructive except `put(type='skill', ...)`
section.

### 3.1 — Read surface

- [ ] `mcp5_get(id='skill:/')`
    - expect: listing of all skills with header `📋 Skills (N)` where
      N ≥ 6 (bundled); each line has slug + first description line
- [ ] `mcp5_get(id='skill:/recent')`
    - expect: same shape, sorted by mtime desc
    - expect: header `📋 Recent skills (N)`
- [ ] `mcp5_get(id='skill:/kind/quest')`
    - expect: the skills whose frontmatter `applies_to:` includes
      `quest` — should include `find-paper` and `quest-disambiguate`
- [ ] `mcp5_get(id='skill:/kind/does-not-exist')`
    - expect: `No skills apply to kind 'does-not-exist'.`  NOT an error
- [ ] `mcp5_get(id='skill:/topic/papers')`
    - expect: skills tagged with `papers` in frontmatter
- [ ] `mcp5_get(id='skill:find-paper')`
    - expect: full SKILL.md body rendered; frontmatter stripped or shown
      as a header
- [ ] `mcp5_get(id='skill:handle-dropped-pdf')`
    - expect: full body, similar shape
- [ ] `mcp5_search(type='skill', query='acquire paper')`
    - expect: hit on `find-paper` (description match)
- [ ] `mcp5_search(type='skill', query='zzz-unmatchable-xyz')`
    - expect: `No skills match 'zzz-unmatchable-xyz'.`

### 3.2 — Negative path

- [ ] `mcp5_get(id='skill:does-not-exist')`
    - expect: `PrecisError(ID_NOT_FOUND)` rendered, listing up to 10
      options + a `next: get(id='skill:/')` hint
- [ ] `mcp5_get(id='skill:/kind/')`  (missing kind name)
    - expect: `PARAM_INVALID` with clear cause + next-hint
- [ ] `mcp5_get(id='skill:/topic/')`  (missing topic)
    - expect: `PARAM_INVALID` with clear cause

### 3.3 — Write surface (roundtrip)

**⚠ destructive**: creates SKILL.md files in `~/.precis/skills/`.  Skip
in quick-smoke.  Pick a unique slug per run using the session timestamp
(``SLUG=smoke-skill-$(date +%s)``) so re-runs don't collide.

Follow the write-roundtrip protocol from the preamble.  With `id=''`
the handler reads the destination slug from the frontmatter: it prefers
`name:` (the canonical Agent Skills field, interops with Claude Code +
Cursor) and falls back to `slug:` (this plan's historical convention).
Either works; use `name:` for new authoring.

1. **Write**
    - [ ] `mcp5_put(type='skill', id='', text='---\nname: <SLUG>\ndescription: Smoke test skill — delete me\n---\n# body', mode='append')`
        - expect: success + next-hint pointing at `get(type='skill', id='<SLUG>')`
    - [ ] optional: also verify the `slug:` fallback with a second
      unique slug: `text='---\nslug: <SLUG2>\ndescription: ...\n---\n# body'`
2. **Read back**
    - [ ] `mcp5_get(type='skill', id='<SLUG>')`
        - expect: full body rendered; description line present
3. **Replace mutation** (skill-specific)
    - [ ] `mcp5_put(type='skill', id='<SLUG>', text='---\nslug: <SLUG>\ndescription: UPDATED\n---\n# body v2', mode='replace')`
        - expect: success
    - [ ] `mcp5_get(type='skill', id='<SLUG>')`
        - expect: description now contains `UPDATED` and body is v2
4. **Delete**
    - [ ] `mcp5_put(type='skill', id='<SLUG>', mode='delete')`
        - expect: success
5. **Read-gone**
    - [ ] `mcp5_get(type='skill', id='<SLUG>')`
        - expect: `ID_NOT_FOUND` with `options:` listing existing slugs

### 3.4 — Write-surface negative paths

- [ ] `mcp5_put(type='skill', id='find-paper', text='x', mode='replace')`
  (find-paper is a bundled ecosystem skill, not writable)
    - expect: `DENIED` — ecosystem skills live in the wheel, not
      ``~/.precis/skills/``, and can't be overwritten through the MCP
      surface
- [ ] `mcp5_put(type='skill', id='<already-existing-slug>', text='...', mode='append')`
    - expect: `ID_AMBIGUOUS` (collision)

---

## 4.  quest  (PG-backed; read surface first, writes land in 12b)

Requires `DATABASE_URL` + Postgres + `papers` schema (from `acatome-quest-mcp`
migrations).

### 4.0 — First-time bootstrap  (run once per DB)

Skip if the DB already has the schema and at least one request.  Verify
with `psql "$DATABASE_URL" -c '\dt papers.*'` — if `papers.requests`
shows up, skip to §4.1.

**Migrate the schema**:  every `acatome-quest` subcommand calls
``db.migrate()`` at startup, so any harmless command bootstraps.

```bash
/Users/bots/Documents/openclaw-cluster/pips/.venv/bin/acatome-quest status --count
```

Expected: creates `papers.requests` (+ related tables), prints zero
counts.  Check with the `\dt papers.*` call above.

**Seed a real paper**:  submits one row so the read views have
something to show.  Pick any DOI / arXiv id you have handy; the
current runner may or may not be up, so the request lands in
`queued` (fine for read-surface testing).

```bash
# by DOI (preferred — resolver has the highest confidence path)
/Users/bots/Documents/openclaw-cluster/pips/.venv/bin/acatome-quest submit 10.1021/jacs.2c01234

# OR by arXiv id
/Users/bots/Documents/openclaw-cluster/pips/.venv/bin/acatome-quest submit arxiv:2301.12345

# OR by free-form title  (likely lands in needs_user with candidates)
/Users/bots/Documents/openclaw-cluster/pips/.venv/bin/acatome-quest submit --title "Metal-organic frameworks for carbon capture"
```

Record the returned UUID in the session log — §4.3 needs it.

**Cleanup afterwards**  (when test data gets distracting):

```bash
psql "$DATABASE_URL" -c "truncate papers.requests cascade;"
```

### 4.1 — Startup surface

- [ ] `mcp5_stats()` — confirm `quest` appears in `kinds by verb`
- [ ] if not listed: check startup warnings for ImportError hint

### 4.2 — Views (read surface)

- [ ] `mcp5_get(id='quest:')`
    - expect: bare recent list (last ~20 requests, any status), or
      empty-state prose if DB is empty
- [ ] `mcp5_get(id='quest:/recent')`
    - expect: same shape as bare
- [ ] `mcp5_get(id='quest:/queued')`
    - expect: requests awaiting runner pickup
- [ ] `mcp5_get(id='quest:/needs-user')`
    - expect: requests awaiting disambiguation; empty-state is fine
- [ ] `mcp5_get(id='quest:/failed')`
    - expect: union of `failed` + `extract_failed`
- [ ] `mcp5_get(id='quest:/ingesting')`
    - expect: union of `fetching` + `ingesting`
- [ ] `mcp5_get(id='quest:/agent/cascade')`  (or whatever your agent id is)
    - expect: filter by `created_by`; empty-state acceptable
- [ ] `mcp5_get(id='quest:/help')`
    - expect: full `find-paper` SKILL.md body inlined (onboarding skill)

### 4.3 — Single-card surface

Requires at least one row in DB.  If DB is empty, seed via quest-mcp
CLI first (`acatome-quest submit`) and note the UUID in the session log.

- [ ] `mcp5_get(id='quest:<full-uuid>')`
    - expect: rendered card with status, title, created_at, candidates
      count, misconceptions count
- [ ] `mcp5_get(id='quest:<first-8-hex>')`
    - expect: same card — short-uuid prefix resolution
- [ ] `mcp5_get(id='quest:<id>/candidates')`
    - expect: list of candidate resolved refs; empty-state if not yet
      resolved
- [ ] `mcp5_get(id='quest:<id>/misconceptions')`
    - expect: attached misconception codes + messages
- [ ] `mcp5_search(type='quest', query='<substring-of-title>')`
    - expect: case-insensitive title substring match

### 4.4 — Negative path

- [ ] `mcp5_get(id='quest:zzzzzzzz')`  (8-hex that matches nothing)
    - expect: `ID_NOT_FOUND` with clear cause
- [ ] `mcp5_get(id='quest:12')` (short prefix, ambiguous if multiple match)
    - expect: on match: `ID_AMBIGUOUS` listing matches; on none: `ID_NOT_FOUND`
- [ ] `mcp5_get(id='quest:/agent/')`  (missing agent id)
    - expect: `PARAM_INVALID`

### 4.5 — PG-down check (only if PG is deliberately offline)

- [ ] stop Postgres, restart MCP host, `mcp5_get(id='quest:/recent')`
    - expect today: `UNEXPECTED: OperationalError: could not connect...`
    - expect after Milestone A: `UNAVAILABLE` with a structured
      `next:` hint pointing at config

### 4.6 — Write surface (Phase 12b — placeholder)

Not yet implemented.  Sketch for when it lands:

- [ ] `mcp5_put(id='quest:', doi='10.1021/jacs.2c01234')`
- [ ] `mcp5_put(id='quest:<id>', mode='pick-candidate', index=0)`
- [ ] `mcp5_put(id='quest:<id>', mode='retry')`
- [ ] `mcp5_put(id='quest:<id>', mode='flag', text='wrong paper')`

---

## 5.  paper  (acatome-store corpus)

Requires `acatome-store` and a non-empty store.

### 5.1 — Read by slug / DOI / arXiv

Bare slugs (e.g. `mcp5_get(id='wang2020state')` without `type=` or a
scheme prefix) are **no longer auto-routed to paper** — that fallback
was retired in the Apr 2026 default-to-paper cleanup.  Always use
`type='paper'` or an explicit `paper:` / `doi:` / `arxiv:` prefix.

- [ ] `mcp5_get(id='paper:wang2020state')`  (or any known slug)
    - expect: paper overview — title, abstract (if present), hints
- [ ] `mcp5_get(id='paper:wang2020state/toc')`
    - expect: chunk index
- [ ] `mcp5_get(id='paper:wang2020state/abstract')`
    - expect: abstract body
- [ ] `mcp5_get(id='paper:wang2020state›38')`
    - expect: chunk 38 full text
- [ ] `mcp5_get(id='paper:wang2020state›38..42')`
    - expect: chunks 38-42
- [ ] `mcp5_get(id='paper:wang2020state›38/summary')`
    - expect: chunk summary
- [ ] `mcp5_get(id='doi:10.1021/jacs.2c01234')`  (substitute real DOI)
    - expect: DOI-alias resolution; same paper
- [ ] `mcp5_get(id='arxiv:2301.12345')`  (substitute real arxiv id)
    - expect: arxiv-alias resolution
- [ ] `mcp5_get(id='10.1021/jacs.2c01234')`  (bare DOI, auto-detected)
    - expect: same as `doi:` prefix

### 5.2 — Figures

- [ ] `mcp5_get(id='paper:wang2020state/fig')` — list figures
- [ ] `mcp5_get(id='paper:wang2020state/fig/3')` — figure 3 overview
- [ ] `mcp5_get(id='paper:wang2020state/fig/3/legend')` — caption only
- [ ] `mcp5_get(id='paper:wang2020state/fig/3/image')` — encoded image data

### 5.3 — Citations

The citation formatter was rewritten in Apr 2026 to handle the three
regressions found on a fresh store: JSON-array authors, Unicode
escapes, and inline HTML/JATS tags.  The bullets below exercise each.

- [ ] `mcp5_get(id='paper:wang2020state/cite/bib')` — BibTeX smoke
- [ ] `mcp5_get(id='paper:wang2020state/summary')` — enrichment summary
- [ ] `mcp5_get(id='paper:marquessilva1999grasp/cite/bib')`
    - regression check: author field must read
      `author = {Marques-Silva, J.P. and Sakallah, K.A.}` (joined by
      ``and``, not a raw JSON list like `[{"name":"..."}]`)
- [ ] `mcp5_get(id='paper:mikladal2013l/cite/bib')`
    - regression check: title must be a **single line** with no `<i>`
      / `</i>` markup and no multi-line indentation; the author field
      must contain `Bjørn` (literal ø), not a `\u00f8` escape sequence
- [ ] `mcp5_get(id='paper:<any>/cite/ris')` — RIS style
    - check: one `AU  -` line per author; titles tag-stripped; no
      BibTeX backslash-escapes leaking in
- [ ] `mcp5_get(id='paper:<any>/cite/acs')` — inline ACS style
    - check: `FirstSurname et al., Journal YYYY` with whitespace
      collapsed (no multi-line journal strings)

### 5.4 — Listing + filtering

`type='paper'` is **required** — bare `grep=` calls without a kind
return `KIND_UNKNOWN` since the default-to-paper fallback was
retired.  `grep=` is a metadata filter (title/author/tag substring),
not a semantic vector search; pair with `query=` for both.

- [ ] `mcp5_get(type='paper', grep='MOF')` — paper list filtered by keyword
- [ ] `mcp5_get(type='paper', grep='ingested:today')` — recent ingest filter
- [ ] `mcp5_get(type='paper', grep='year:2020-2024')` — year-range filter
- [ ] `mcp5_get(type='paper', grep='tag:review')` — tag filter
- [ ] regression check: `mcp5_get(grep='MOF')` (no `type=`)
    - expect: `KIND_UNKNOWN` with `options:` listing visible kinds
- [ ] regression check: `mcp5_search(type='paper', query='membrane', grep='tag:review')`
    - expect: vector search **pre-filtered** by the `tag:review`
      metadata filter, not the other way around

### 5.5 — Search

Bare `mcp5_search(query='...')` with no `type=` and no `scope=` is
rejected by design (see §15.2) — pass one of them explicitly.

- [ ] `mcp5_search(query='anion exchange membrane')`  (no `type=`, no `scope=`)
    - expect: `KIND_UNKNOWN` with `options:` listing visible kinds
- [ ] `mcp5_search(query='anion exchange membrane', type='paper')`
    - expect: ranked results across the paper corpus
- [ ] `mcp5_search(query='selectivity', scope='wang2020state')`
    - expect: results scoped to one paper (scope= implies paper kind)
- [ ] `mcp5_search(query='CO2 capture', type='paper', top_k=5)`
    - expect: 5 hits max, all `type='paper'`

### 5.6 — Negative path

- [ ] `mcp5_get(id='paper:zzz-nonexistent-xyz')`
    - expect: `ID_NOT_FOUND`
- [ ] `mcp5_get(id='paper:wang2020state›9999')`  (out-of-range chunk)
    - expect: `ID_NOT_FOUND` or `PARAM_INVALID`
- [ ] `mcp5_get(id='paper:wang2020state/fig/9999')`  (out-of-range figure)
    - expect: `ID_NOT_FOUND`

### 5.7 — Write (ingestion path)

Paper writes go through a separate ingestion pipeline, not direct `put`.
In precis the paper kind is `write_policy="ingestion"` — direct `put()`
should refuse.

- [ ] `mcp5_put(id='paper:wang2020state', text='foo', mode='append')`
    - expect: `DENIED` or similar with a redirect hint pointing at
      `quest:` or `acatome-store` CLI

---

## 6.  todo  (acatome-store, direct writes)

### 6.1 — Read

Collection views added in Apr 2026 — before the fix these all
returned `PARAM_INVALID: missing slug`.

- [ ] `mcp5_get(id='todo:/')` — all todos
- [ ] `mcp5_get(id='todo:/open')` — open todos
- [ ] `mcp5_get(id='todo:/done')` — closed todos
- [ ] `mcp5_get(id='todo:/today')` — todos due today (state-agnostic)
- [ ] `mcp5_get(id='todo:/recent')` — recently modified
- [ ] `mcp5_get(id='todo:/cancelled')` — any other state string also works

### 6.2 — Write surface (roundtrip)

Unique test-prefix tag per run (e.g. ``TAG=smoke-$(date +%s)``) to
isolate test data.  Record the integer id returned by the write step
in your session log — call it ``<N>`` below.

1. **Write**
    - [ ] `mcp5_put(type='todo', id='', text='Smoke test [<TAG>]', mode='append')`
        - expect: success + `next: get(type='todo', id='<N>')`
2. **Read back**
    - [ ] `mcp5_get(type='todo', id='<N>')`
        - expect: just-created todo with status `open`
3. **Mutations** (todo-specific)
    - [ ] `mcp5_put(type='todo', id='<N>', text='Updated [<TAG>]', mode='replace')`
        - expect: success
    - [ ] `mcp5_get(type='todo', id='<N>')`
        - expect: text is `Updated [<TAG>]`
    - [ ] `mcp5_put(type='todo', id='<N>', mode='done')`
        - expect: success
    - [ ] `mcp5_get(type='todo', id='<N>')`
        - expect: status is `done`; appears in `todo:/done` view
4. **Delete**
    - [ ] `mcp5_put(type='todo', id='<N>', mode='delete')`
        - expect: success
5. **Read-gone**
    - [ ] `mcp5_get(type='todo', id='<N>')`
        - expect: `ID_NOT_FOUND`

### 6.3 — Search

- [ ] `mcp5_search(type='todo', query='<TAG>')`
    - expect: empty result (we deleted in §6.2) or matches any
      leftovers from earlier runs
    - regression check: this must hit **only** todo refs, not bleed
      into paper chunks.  Before the bug #4 fix, `type=` was ignored
      and non-paper searches used the shared vector index.
- [ ] run §6.2 through step 2 only (leave a todo in place), then:
    - [ ] `mcp5_search(type='todo', query='<TAG>')` must hit
    - [ ] clean up with `mcp5_put(type='todo', id='<N>', mode='delete')`

### 6.4 — Slug-prefix retry (regression — bug #1)

Todos are stored with slug `todo:<N>` in the corpus.  Callers pass
the bare int (`id='<N>'`); the handler retries with the `todo:` prefix
automatically.

- [ ] write a todo (§6.2 step 1), then `mcp5_get(type='todo', id='<N>')`
  must succeed — no `ID_NOT_FOUND` even though the stored slug is
  actually `todo:<N>`
- [ ] same for `mcp5_put(type='todo', id='<N>', mode='done')` — mutate
  by bare id, not the prefixed form

---

## 7.  flashcard

Single canonical scheme: `flashcard:`.  The short `fc` alias has been
retired — every URI and `type=` lookup uses the long form.

### 7.1 — Read

- [ ] `mcp5_get(id='flashcard:/due')` — cards due for review
- [ ] `mcp5_get(id='flashcard:/recent')` — recently created
- [ ] `mcp5_get(id='flashcard:/')` — bare list / overview

### 7.2 — Write surface (roundtrip)

1. **Write**
    - [ ] `mcp5_put(type='flashcard', id='', text='Q: Smoke <TAG> 2+2?\nA: 4', mode='append')`
        - expect: success + next `get(type='flashcard', id='<N>')`
2. **Read back**
    - [ ] `mcp5_get(type='flashcard', id='<N>')`
        - expect: Q/A rendered; SM-2 state shows as "new / not yet due"
3. **Mutation** (SM-2 rate)
    - [ ] `mcp5_put(type='flashcard', id='<N>', mode='rate', grade=4)`
        - expect: success
    - [ ] `mcp5_get(type='flashcard', id='<N>')`
        - expect: interval advanced, next-review date in the future
4. **Delete**
    - [ ] `mcp5_put(type='flashcard', id='<N>', mode='delete')`
        - expect: success
5. **Read-gone**
    - [ ] `mcp5_get(type='flashcard', id='<N>')`
        - expect: `ID_NOT_FOUND`

### 7.3 — Search

- [ ] As in §6.3: write a card with a unique `<TAG>`, confirm
  `mcp5_search(type='flashcard', query='<TAG>')` finds it, then clean up.

### 7.4 — Slug-prefix retry (regression — bug #1 + bug #8)

Flashcards are stored with slug `flashcard:<N>`.  Bare int ids must
resolve via the handler's `_slug_prefix` retry.

- [ ] write then bare-id read: `mcp5_get(type='flashcard', id='<N>')`
  succeeds without `ID_NOT_FOUND`
- [ ] confirm the old `fc:` scheme is **gone**: `mcp5_get(id='fc:<N>')`
  must error — the canonical scheme was renamed to `flashcard:` and
  `fc` was retired.  If this succeeds, the old alias table leaked.

---

## 8.  memory

Long-lived agent notes.  Block-structured.

### 8.1 — Read

- [ ] `mcp5_get(id='memory:')` — recent memories
- [ ] `mcp5_search(query='precis', type='memory')`

### 8.2 — Write surface (roundtrip)

Memory refs have string slugs (not integer ids).  Use a unique slug per
run: ``SLUG=smoke-memory-$(date +%s)``.

1. **Write**
    - [ ] `mcp5_put(type='memory', id='<SLUG>', text='Smoke memory — delete me [<TAG>]', mode='append', title='Smoke <TAG>')`
        - expect: success + next `get(type='memory', id='<SLUG>')`
2. **Read back**
    - [ ] `mcp5_get(type='memory', id='<SLUG>')`
        - expect: body text + title present; block structure shown
3. **Mutation** (append a second block)
    - [ ] `mcp5_put(type='memory', id='<SLUG>', text='second block', mode='append')`
        - expect: success
    - [ ] `mcp5_get(type='memory', id='<SLUG>')`
        - expect: two blocks now present
4. **Delete**
    - [ ] `mcp5_put(type='memory', id='<SLUG>', mode='delete')`
        - expect: success
5. **Read-gone**
    - [ ] `mcp5_get(type='memory', id='<SLUG>')`
        - expect: `ID_NOT_FOUND`

### 8.3 — Slug-prefix retry (regression — bug #1)

Memories are stored with slug `memory:<SLUG>`.  The handler retries
with the `memory:` prefix when the caller passes a bare slug.

- [ ] after §8.2 step 1, read back with `mcp5_get(type='memory', id='<SLUG>')`
  — must succeed on the bare slug, no need to pre-prefix

### 8.4 — Links

These are annotations on existing refs; non-destructive to the refs
themselves.  If a paper slug is available from §5, use it here;
otherwise create a memory in §8.2 first and link to that.

- [ ] `mcp5_put(type='paper', id='<paper-slug>', link='<other-slug>:cites')`
    - expect: link recorded; count surfaces in response footer
- [ ] `mcp5_get(type='paper', id='<paper-slug>')` and confirm the link
  shows up in the response (usually in a `Links:` footer)
- [ ] `mcp5_put(type='paper', id='<paper-slug>', unlink='<other-slug>:cites')`
    - expect: link removed; footer no longer shows it

---

## 9.  conversation

Chat turn history.  Block-structured, slug-keyed.  Canonical scheme
is `conversation:` — the short `conv:` alias was retired; `conv:`
lookups now error `KIND_UNKNOWN` by design.

### 9.1 — Read

- [ ] `mcp5_get(id='conversation:/recent')` — recent conversations
    - regression check: before the bug #2 fix this threw
      `AttributeError: type object 'Block' has no attribute 'id'`
- [ ] `mcp5_get(id='conversation:<slug>')` — one conversation
- [ ] `mcp5_search(query='<term>', type='conversation')`

### 9.2 — Scoped search

- [ ] `mcp5_search(query='membrane', scope='conversation:<slug>')`
    - expect: results only from that conversation

### 9.3 — Write surface (roundtrip)

Conversation slugs are string-valued (not integers).  Use a date-
stamped slug per run: ``SLUG=2026-04-22-smoke-$(date +%s)``.  The
handler normalises bare slugs by prefixing `conversation:` on write.

1. **Write**
    - [ ] `mcp5_put(type='conversation', id='<SLUG>', text='First turn.', mode='append')`
        - expect: success + next-hint pointing at `get(type='conversation', id='<SLUG>')`
        - confirm the stored slug is `conversation:<SLUG>` (verify via
          DB if in doubt; the response footer cites the canonical id)
2. **Read back**
    - [ ] `mcp5_get(type='conversation', id='<SLUG>')`
        - expect: the single turn rendered with speaker + timestamp
          block structure
3. **Mutation** (append a second turn)
    - [ ] `mcp5_put(type='conversation', id='<SLUG>', text='Second turn.', mode='append')`
    - [ ] `mcp5_get(type='conversation', id='<SLUG>')`
        - expect: two turns now present
4. **Delete**
    - [ ] `mcp5_put(type='conversation', id='<SLUG>', mode='delete')`
        - expect: success (soft-delete: `meta.deleted` flag)
5. **Read-gone**
    - [ ] `mcp5_get(type='conversation', id='<SLUG>')`
        - expect: `ID_NOT_FOUND` or the soft-deleted render, depending
          on handler policy — record which it is

### 9.4 — Slug-prefix retry + retired alias (regression — bugs #1, alias cleanup)

- [ ] bare-slug read after §9.3 step 1: `mcp5_get(type='conversation', id='<SLUG>')`
  must succeed without manual `conversation:` prefixing
- [ ] `mcp5_get(type='conv', id='/recent')` — expect `KIND_UNKNOWN`
  (alias was retired; no fallback)

---

## 10.  web  (Perplexity Sonar)

Requires `PERPLEXITY_API_KEY`.  Stateless; every call hits the API.

### 10.1 — Search

- [ ] `mcp5_search(type='web', query='what is a metal-organic framework?')`
    - expect: Perplexity answer with citations; cost line in footer
- [ ] `mcp5_search(type='web', query='<term>', recency='week')`
    - expect: recency-filtered answer
- [ ] `mcp5_search(type='web', query='<term>', focus='academic')`
    - expect: academic-sources-only answer
- [ ] `mcp5_search(type='web', query='<term>', focus='finance')`
    - expect: SEC/filings-focused answer

### 10.2 — Error path

- [ ] with `PERPLEXITY_API_KEY=invalid`, retry a query
    - expect: `UPSTREAM_ERROR` with HTTP status + truncated body

---

## 11.  research  (deep Perplexity)

Long-running (2–10 min).  Use sparingly.

- [ ] `mcp5_search(type='research', query='compare AEM vs PEM for CO2 reduction')`
    - expect: long comprehensive answer; cost-hint = `$$$` tier

---

## 12.  youtube

Requires `youtube-transcript-api`.

- [ ] `mcp5_get(id='youtube:<video-id>')` — full transcript
- [ ] `mcp5_get(id='youtube:<video-id>/languages')` — available langs
- [ ] `mcp5_get(id='youtube:<video-id>/transcript?lang=es')` — localised
- [ ] negative: `mcp5_get(id='youtube:not-a-real-id')` → `UPSTREAM_ERROR`

---

## 13.  math  (Wolfram Alpha)

Requires `WOLFRAM_APP_ID`.  Hidden at startup when unset.

- [ ] `mcp5_get(id='math:integral of x^2')`
    - expect: Wolfram-rendered answer
- [ ] `mcp5_get(id='math:fourier transform sin(x)')`
    - expect: symbolic result
- [ ] negative: invalid expression → `UPSTREAM_ERROR` or rendered error

---

## 14.  think  (Perplexity Sonar Reasoning Pro)

Requires `PERPLEXITY_API_KEY`.  Same provider as §10 (web) and §11
(research) — `ThinkHandler` lives in
`@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/handlers/web.py`
alongside them and wraps the `sonar-reasoning-pro` model (5-30 s
latency, ~$0.005/call).  Use think for detailed analysis with
reasoning traces; the faster `web` tier suits factual lookups, and the
slower `research` tier is for multi-step synthesis.

- [ ] `mcp5_get(id='think:')` — landing: no API call, `[cost: free]`,
  lists the model + usage examples (fixed in Apr 2026; previously
  errored `PARAM_INVALID: empty Perplexity query`).
- [ ] `mcp5_get(id='think:compare Postgres SKIP LOCKED vs SELECT FOR UPDATE')` —
  real query.  Expect a reasoning answer with citations; cost footer
  reflects the Perplexity tier.
- [ ] negative: with `PERPLEXITY_API_KEY=invalid`, retry a query →
  `DENIED` (HTTP 401) with the upstream body truncated for diagnosis.

---

## 15.  Cross-kind features

### 15.1 — Paper identifier schemes

Paper kind accepts alternative identifier URIs that route to the same
handler via its scheme list.  These are not `type=` aliases (the
LLM-facing shortcut aliases were removed on purpose) — they are real
URI forms for the different identifier formats.

- [ ] `mcp5_get(id='doi:10.1021/jacs.2c01234')` → resolves same as
  `paper:10.1021/jacs.2c01234`
- [ ] `mcp5_get(id='arxiv:2301.12345')` → resolves same as
  `paper:arxiv:2301.12345`
- [ ] `mcp5_get(id='10.1021/jacs.2c01234')` (bare DOI, auto-detected)
  → resolves same as `paper:10.1021/jacs.2c01234`
- [ ] record any identifier-scheme that doesn't resolve

Other kinds **do not** have aliases — `type='fc'`, `type='conv'`
must both fail with `KIND_UNKNOWN`.  Exercise both:

- [ ] `mcp5_get(type='fc', id='/due')` → `KIND_UNKNOWN`
- [ ] `mcp5_get(type='conv', id='/recent')` → `KIND_UNKNOWN`

### 15.2 — Kind-scoped search

- [ ] `mcp5_search(query='membrane', top_k=10)` (no `type=`)
    - expect: `KIND_UNKNOWN` listing the visible kinds (bare-search
      was retired alongside the default-to-paper routing)
- [ ] `mcp5_search(query='membrane', type='paper', top_k=10)`
    - expect: ranked paper hits; each line prefixed with its slug
- [ ] `mcp5_search(query='<TAG>', type='todo')` (after writing a
  tagged todo in §6.3)
    - expect: the single match, no paper chunks bleeding in

### 15.3 — Visibility / PRECIS_KINDS mask

File-type kinds (`word`, `tex`, `markdown`, `plaintext`) now appear
in `stats()` as independent kinds — there is no longer a single
catch-all `file` kind.  Update mask strings accordingly.

- [ ] set `PRECIS_KINDS=paper,skill`, restart, `mcp5_stats()`
    - expect: `kinds by verb` shows ONLY paper + skill
- [ ] `mcp5_get(id='quest:/recent')`
    - expect: `KIND_UNKNOWN` with `options` containing at least
      `paper` and `skill` (may also include any file-type kinds that
      are always-on regardless of mask — record the exact shape)
- [ ] with the full mask unset, `mcp5_stats()`
    - expect: `kinds by verb` contains word, tex, markdown, plaintext
      alongside the DB-backed kinds

### 15.4 — Error envelope consistency

For any failing call, the response should:

- start with `!! <ERROR_CODE>` on line 1
- include a `cause:` line
- include `options:` (when the handler knows them) or `next:` (guidance)
- include a gripe hint for codes in `GRIPE_HINT_CODES` (UNEXPECTED,
  TIMEOUT, UNAVAILABLE, RATE_LIMITED, UPSTREAM_ERROR)

Check:

- [ ] a `KIND_UNKNOWN` error has `options:` with the list of registered kinds
- [ ] an `ID_NOT_FOUND` error has `options:` with up to 10 similar ids
- [ ] an `ID_AMBIGUOUS` error lists the candidate matches
- [ ] an `UPSTREAM_ERROR` error has a gripe-next-hint

`KIND_UNKNOWN` must fire across all three verbs (regression — the
default-to-paper fallback was retired in Apr 2026, so every verb's
no-type path returns the same structured error):

- [ ] `mcp5_get(id='some-slug')` — no `type=`, no scheme prefix
    - expect: `KIND_UNKNOWN` with `options:` + next-hint suggesting
      `type='paper'` or a scheme prefix
- [ ] `mcp5_search(query='membrane')` — no `type=`, no `scope=`
    - expect: `KIND_UNKNOWN` (same envelope)
- [ ] `mcp5_put(text='some text')` — no `type=`, no `id=`
    - expect: `KIND_UNKNOWN` (same envelope)

### 15.5 — Response footer cost line

- [ ] any successful call: last line is `— cost: <tier>` or similar
- [ ] expensive calls (`research`, `web`) show a higher-tier indicator
- [ ] free calls (`skill`, `think`, most state-backed kinds) show `free`

### 15.6 — Caching sanity

The registry memoises handler instances.  Between two consecutive calls
to the same kind, the handler's warm state should persist.  Observable
effects (if the handler caches):

- [ ] back-to-back `mcp5_get(id='skill:/')` — second should feel faster
  (index built on first call, reused on second)
- [ ] back-to-back `mcp5_get(id='paper:<slug>')` — same (store reused)

Not directly testable without timing instrumentation, but note any
pathological latency variations.

---

## 16.  File-type kinds  (`word`, `tex`, `markdown`, `plaintext`)

Canonical kind names mirror the handler: `word` (.docx), `tex` (.tex),
`markdown` (.md/.markdown), `plaintext` (.txt/.text).  Each is both a
scheme and a kind — you can dispatch two ways:

1. **auto-classification**: pass a raw path with an extension; the
   server stamps the `file:` scheme and routes via FILE_TYPES.
   ``mcp5_put(id='/tmp/foo.md', text=...)``
2. **explicit kind**: pass `type=<kind>`; the server stamps the
   matching scheme directly.
   ``mcp5_put(type='markdown', id='/tmp/foo.md', text=...)``

Both routes hit the same handler instance.  The LLM-facing discovery
surface (`mcp5_stats()`, ambiguous-kind error options) lists each
kind explicitly.

Use `FILE=/tmp/precis-smoke-$(date +%s).<ext>` for unique paths per
run.  After each section, remove the file: ``rm "$FILE"``.

### 16.1 — Markdown (roundtrip)

Exercise both dispatch routes — step 1 via auto-classification, step 3
via explicit `type=` — so both entry points stay covered.

1. **Write** (auto-classified by `.md` extension)
    - [ ] `mcp5_put(id='<FILE>.md', text='# Title\n\nFirst para.', mode='append')`
2. **Read back**
    - [ ] `mcp5_get(id='<FILE>.md')` — TOC shows Title + the para slug
    - [ ] `mcp5_get(id='<FILE>.md', depth=1)` — H1 only
3. **Mutations** (explicit `type=` this time)
    - [ ] `mcp5_put(type='markdown', id='<FILE>.md', text='## Section', mode='append')`
    - [ ] `mcp5_get(type='markdown', id='<FILE>.md')` — Section H2 now present
    - [ ] `mcp5_put(id='<FILE>.md›<slug>', text='Revised para.', mode='replace')`
    - [ ] `mcp5_get(id='<FILE>.md›<slug>')` — text is `Revised para.`
4. **Delete** (node-level)
    - [ ] `mcp5_put(id='<FILE>.md›<slug>', mode='delete')`
5. **Read-gone**
    - [ ] `mcp5_get(id='<FILE>.md›<slug>')`
        - expect: `ID_NOT_FOUND`
    - [ ] `mcp5_get(id='<FILE>.md')` — confirm slug absent from TOC

### 16.2 — Plaintext (roundtrip)

1. **Write** (explicit `type='plaintext'`)
    - [ ] `mcp5_put(type='plaintext', id='<FILE>.txt', text='line 1\nline 2', mode='append')`
2. **Read back**
    - [ ] `mcp5_get(id='<FILE>.txt')`
        - expect: both lines present
3. **Mutation**
    - [ ] `mcp5_put(id='<FILE>.txt', text='line 3', mode='append')`
    - [ ] `mcp5_get(id='<FILE>.txt')` — three lines
4. **Delete** — plaintext: remove the file from disk
    - [ ] ``rm '<FILE>.txt'``
5. **Read-gone**
    - [ ] `mcp5_get(id='<FILE>.txt')`
        - expect: an error distinguishing "file does not exist" from
          handler-level errors

### 16.3 — LaTeX (roundtrip)

1. **Write** (explicit `type='tex'`)
    - [ ] `mcp5_put(type='tex', id='<FILE>.tex', text='\\section{Foo}\n\nPara.', mode='append')`
2. **Read back**
    - [ ] `mcp5_get(id='<FILE>.tex')` — section outline, para slug visible
3. **Mutation**
    - [ ] `mcp5_put(id='<FILE>.tex›<slug>', text='Revised para.', mode='replace')`
    - [ ] `mcp5_get(id='<FILE>.tex›<slug>')`
4. **Delete**
    - [ ] `mcp5_put(id='<FILE>.tex›<slug>', mode='delete')`
5. **Read-gone**
    - [ ] `mcp5_get(id='<FILE>.tex›<slug>')`
        - expect: `ID_NOT_FOUND`

### 16.4 — Word / DOCX

Canonical kind name is `word`.  Requires an existing .docx file
(create one manually or use a fixture).

- [ ] `mcp5_get(id='<path>.docx')` — TOC (auto-classified)
- [ ] `mcp5_get(type='word', id='<path>.docx', depth=2)` — explicit route
- [ ] `mcp5_put(id='<path>.docx', text='New para.', mode='append')`
- [ ] `mcp5_put(id='<path>.docx›<slug>', text='Edited.', mode='replace', tracked=True)`
    - expect: tracked change inserted
- [ ] `mcp5_put(id='<path>.docx›<slug>', text='Note', mode='comment')`
    - expect: comment inserted

### 16.5 — Move (DOCX reordering)

- [ ] `mcp5_move(id='<path>.docx›SLUG1', after='<path>.docx›SLUG2')`
    - expect: node moved; paths recomputed; slugs preserved

---

## 17.  Regression log

Bugs found + fixed.  Each entry is a test to re-run on every full
regression pass.  If a bug re-appears, it goes back into the active
test matrix until re-fixed.

- [ ] **PG-down classification (fix: Phase 12b Milestone A)** — when
  `DATABASE_URL` is unreachable, `quest:/recent` currently errors
  `UNEXPECTED: OperationalError`.  After the fix, must error
  `UNAVAILABLE` with a structured `next:` hint naming the config var.

- [ ] **Handler instance caching (fix: 2026-04-22 consolidation)** —
  two consecutive `resolve(scheme, ...)` calls must return the same
  Python object.  Unit test in
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_registry.py`
  covers this; live check: any observable "warm" state (e.g. SkillHandler's
  `_index`) survives across calls.

- [ ] **Slug-prefix retry across acatome-store kinds (fix: 2026-04-22,
  bug #1)** — callers pass bare ids (`id='42'`, `id='my-note'`) but
  refs are stored with a kind prefix (`todo:42`, `memory:my-note`).
  Live checks: §6.4, §7.4, §8.3, §9.4 (all four "slug-prefix retry"
  subsections).  If any regresses, the `_slug_prefix` class attribute
  on the affected `RefHandler` subclass is missing.

- [ ] **Bare search/get/put returns `KIND_UNKNOWN` (fix: 2026-04-22,
  default-to-paper cleanup)** — see §15.4 per-verb checklist.  The
  silent fallback to `paper` was retired; any no-type call must now
  surface a structured error with the visible-kinds list.

- [ ] **BibTeX citation hygiene (fix: 2026-04-22, bug #5)** — see §5.3.
  Three concrete regression cases: `marquessilva1999grasp` (JSON
  authors), `mikladal2013l` (Unicode escapes + `<i>` tags), plus the
  backslash-escape sanity check via any paper with `&` / `%` / `_` in
  the title.  Unit coverage:
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_paper_handler.py:138-265`.

- [ ] **Todo collection views (fix: 2026-04-22, bug #7)** — §6.1
  exercises `/open /done /today /recent /<state>`.  Before the fix,
  these errored `PARAM_INVALID: missing slug`.

- [ ] **`type=` filter honoured in search (fix: 2026-04-22, bug #4)**
  — see §6.3 regression-check bullet.  Non-paper `type=` calls must
  use corpus-scoped grep, not the shared paper vector index.

- [ ] **Conversation `/recent` no AttributeError (fix: 2026-04-22,
  bug #2)** — see §9.1.  Before the fix the view errored
  `AttributeError: type object 'Block' has no attribute 'id'`.

- [ ] **Retired kind aliases stay retired (fix: 2026-04-22, alias
  cleanup)** — see §7.4 (`fc:`) and §9.4 (`conv:`) bullets.  If either
  resolves, the old `KindSpec.aliases` wiring has regressed.

- [ ] **File-type kinds discoverable via `stats()` (fix: 2026-04-22,
  bug #10)** — `mcp5_stats()` lists `word`, `tex`, `markdown`,
  `plaintext` as independent kinds.  Live check: §15.3 bullet 3
  ("with the full mask unset").  Explicit `type=<kind>` dispatch
  exercised via §16.1 / §16.2 / §16.3 / §16.4.

- [ ] **Skill `id=''` + frontmatter convention (fix: 2026-04-22, bug
  #11)** — §3.3 step 1 creates a skill with `id=''` and the slug
  carried in either `name:` (canonical) or `slug:` (historical) of the
  posted frontmatter.  Unit coverage:
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_phase12b_skill.py:430-475`.

- [ ] **`think:` bare landing is free (fix: 2026-04-22, bug #9)** —
  `mcp5_get(id='think:')` returns a help view with `[cost: free]`
  footer.  Before the fix it errored `PARAM_INVALID: empty Perplexity
  query`.  Live check: §14 bullet 1, also touched by the §2 quick-
  smoke path.

- [ ] **BUG-A (discovered 2026-04-22 19:30)** — `get(type='paper',
  grep='<anything>')` crashes with `TypeError: sequence item 4:
  expected str instance, NoneType found`.  Reproduced for plain
  keyword (`MOF`), year-range (`year:2020-2024`), and tag (`tag:review`).
  Root cause likely in the list-renderer string-join when a ref has a
  `None` field.  Live check: §5.4 first three bullets must all return
  a ref list, not the TypeError.

- [ ] **BUG-B (discovered 2026-04-22 19:30)** — `fc:` alias not
  retired despite the 17:40 claim.  Two distinct live symptoms:
  `mcp5_get(type='fc', id='/due')` returns the full flashcard /due
  view; `mcp5_get(id='fc:<slug>')` strips the prefix and looks up in
  the flashcard corpus (errors `item '<slug-without-fc>' not in
  corpus` when the stored slug has `fc:` baked in).  Live check: both
  §7.4 bullets must error `KIND_UNKNOWN` / `unknown scheme`.

- [ ] **BUG-C (discovered 2026-04-22 19:30)** — `mcp5_get(id='some-slug')`
  with no `type=` and no scheme still routes to paper and returns
  `ID_NOT_FOUND: paper 'some-slug' not in corpus`.  The
  default-to-paper cleanup retired this path for `search` and for
  `get(grep=…)` and for `put`, but left bare-id get wired to the
  paper fallback.  Live check: §15.4 bullet "`mcp5_get(id='some-slug')`"
  must return `KIND_UNKNOWN`, same shape as the other two verbs.

- [ ] **BUG-D (discovered 2026-04-22 19:30)** — Paper overview
  renderer displays authors as raw JSON (`[{"name": "..."}, {"name":
  "..."}]`).  The 2026-04-22 cite-formatter cleanup (bug #5) fixed
  `/cite/bib`, `/cite/ris`, `/cite/acs` but did not reach the landing
  page header used by `get(id='paper:<slug>')`.  Visible on every
  paper with JSON-encoded authors.  Live check: §5.1
  `paper:marquessilva1999grasp` must show `Marques-Silva, J.P.; Sakallah, K.A.`
  (or similar cleaned form), not the JSON array.

- [ ] **BUG-E (discovered 2026-04-22 19:30)** — Error envelope format
  inconsistent across code paths.  Structured errors emit
  `ERROR [<code>]: <msg>\n  where: …\n  cause: …\n  options: …\n  next: …`.
  A second path emits raw `!! ERROR PrecisError: <msg>` with no
  structure.  Seen on `mcp5_get(type='conv', id='/recent')` and
  `mcp5_get(id='fc:<slug>')`.  Plan §15.4 expects `!! <ERROR_CODE>`
  shape which matches neither.  Live check: every error path must
  emit the same shape; pick one and enforce.

- [ ] **BUG-F (discovered 2026-04-22 19:30)** —
  `mcp5_search(type='paper', query='membrane', grep='tag:review')`
  returns the same top-5 as unfiltered membrane search, suggesting
  `grep=` is silently dropped when paired with `query=` on paper.
  Plan §5.4 regression-checks this ("vector search pre-filtered by
  the tag:review metadata filter, not the other way around") and the
  17:40 log claims bug #6 covered it.  Live check: repeat with a tag
  known to have matches; result set must be distinct from the
  unfiltered case.

- [ ] **BUG-G (discovered 2026-04-22 19:30)** — skill
  description-search multi-word tokenizer broken.
  `mcp5_search(type='skill', query='acquire paper')` returns 0 hits;
  singles `acquire` (1 hit) and `paper` (2 hits) both work.  No
  AND/OR across terms.  Live check: §3.1 bullet
  "`search(type='skill', query='acquire paper')`" must hit find-paper.

- [ ] **BUG-H (discovered 2026-04-22 19:30)** — `quest:/recent` with
  schema missing errors `UNEXPECTED: UndefinedTable: relation
  "papers.requests" does not exist` instead of `UNAVAILABLE` with a
  migration next-hint.  Distinct from the `DATABASE_URL`-unreachable
  flavour queued for Milestone A (that one is `PoolTimeout`; this one
  is schema-gone, reachable DB).  Live check: §4.5 after migration
  rolled back, `quest:/recent` must return `UNAVAILABLE` pointing at
  `acatome-quest migrate`.

- [ ] **BUG-I (discovered 2026-04-22 19:30)** —
  `mcp5_search(type='<web|research|think>', query=…)` crashes with
  `TypeError: _WebBase.read() got an unexpected keyword argument
  'top_k'` for all three Perplexity-backed kinds.  The server-side
  search dispatcher forwards `top_k` to `_WebBase.read()` which
  doesn't accept it; the web/research/think handlers should either
  accept-and-ignore `top_k` or the dispatcher should strip it before
  forwarding to kinds that don't advertise `top_k` support.  `get()`
  is unaffected — works for all three.  No upstream cost, error
  fires pre-Perplexity.  Live check: §10.1 first bullet, §11 first
  bullet, and §14 bullet 2 must all return Perplexity answers via
  `search()`, not this TypeError.

<!-- Retired regression entries (fix verified + unit test in place,
     re-check not required on every smoke run):
     - 2026-04-22  skill:/kind/<name> parsed-URI slug-leak bug.
       Unit tests in test_phase12b_skill.py cover it; the live
       §3.1 `skill:/kind/quest` step already exercises the same path.
     - 2026-04-22  skill frontmatter parsing too strict (bug #3).
       `_parse_skill_md` now falls back to directory-name for `name:`
       and first body line for `description:`; broken-YAML skills
       index rather than disappearing.  Unit tests in
       test_phase12b_skill.py lock the leniency contract.
     - 2026-04-22  grep= vs query= plumbing on paper list (bug #6).
       Handled as distinct kwargs in `_ref_base.py`; `_list_refs`
       prefers metadata filtering when both are supplied.  Covered by
       §5.4 active bullet "vector search pre-filtered by the
       tag:review metadata filter" — leave as active smoke check for
       one run, retire after first green verification.
     - 2026-04-22  flashcard canonical rename (bug #8).
       Every URI, skill body, and test mentions `flashcard:` now; the
       `fc:` alias is caught by §7.4 active bullet.  Full unit coverage
       in test_flashcard_handler.py. -->

---

## Session log

<!-- prepend new sessions above existing ones -->

### Session: 2026-04-22 20:30  (Cascade / Reto — second fix-pass, not a live run)

Follow-on to the 19:30 live run.  All 9 bugs (A–I) discovered in that
pass are now fixed with regression coverage; wheel rebuild + MCP host
restart are still owed before the next live run.

- Scope: source + tests; no new live MCP calls.
- Tests: full suite passes (**978 tests, up from 953**), ruff + mypy
  clean.
- **Live verification still pending** — the installed wheel still
  reproduces the 19:30 bugs (notably BUG-A `TypeError` on every paper
  `grep=`, BUG-I on every `search(type='<web|research|think>', …)`).
  Rebuild + restart before rerunning §5.4 / §10.1 / §11 / §14.

#### Fixes delivered

| bug | fix summary | live check | unit test |
|---|---|---|---|
| BUG-A | Coerce `None` ref-fields to `""` before join in `_list_refs._matches`; same defence in `_list_entry` | §5.4 bullet 1-3 | `TestListRendererTolerateNones` in `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_paper_handler.py:268-344` |
| BUG-B | Retired the legacy `fc` entry point in `pyproject.toml [project.entry-points."precis.schemes"]`; added `flashcard` entry for parity; registry regression asserts `fc` + `conv` stay absent from SCHEMES | §7.4 bullets | `test_retired_scheme_aliases_do_not_leak` in `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_registry.py:84-110` |
| BUG-C | New `_has_identifier_hint` helper + `get()` dispatch check: bare slug without `type=` and without scheme / file-ext / DOI-ish hint emits `KIND_UNKNOWN` for parity with `search`/`put` | §15.4 bullet "`get(id='some-slug')`" | `test_get_with_bare_slug_errors` + 4 siblings in `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_server_phase1.py:273-339` |
| BUG-D | Route `authors` through `_author_names` in `_read_overview` so JSON-array authors decode for the landing page (same normalisation as the cite formatters) | §5.1 `marquessilva1999grasp` | `TestOverviewAuthorsNormalisation` in `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_paper_handler.py:347-418` |
| BUG-E | `_dispatch` raw-fallback now emits the structured `ERROR [<code>]: …` envelope via `_format_error` — `PrecisError` passes options/next through verbatim, other exceptions become `UNEXPECTED` | §15.4 per-verb `KIND_UNKNOWN` bullets, §9.4 `conv:` | `test_dispatch_unknown_kind_emits_structured_envelope` + `test_dispatch_precis_error_preserves_options_and_next` in `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_server_phase1.py:287-332` |
| BUG-F | Added `grep=` kwarg to `server.search()` signature; new `_search_with_grep` in `_ref_base` runs metadata pre-filter then vector search over the filtered subset (paper kind over-fetches + post-filters) | §5.4 regression check "vector search pre-filtered by tag:review" | `TestSearchWithGrep` + `TestSearchToolForwardsGrep` in `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_paper_handler.py:421-536` |
| BUG-G | Skill `_search` now tokenises on whitespace and AND-matches: every token must appear in the `name + description` blob | §3.1 `search(type='skill', query='acquire paper')` | `test_search_ands_across_tokens` + 2 siblings in `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_phase12b_skill.py:402-425` |
| BUG-H | New `_handle_pg_errors` wrapper on `QuestHandler._db_*`: `psycopg.errors.UndefinedTable` → `UNAVAILABLE` with `acatome-quest status --count` next-hint; `OperationalError` → `UNAVAILABLE` with `DATABASE_URL` next-hint | §4.5 after migration rollback | `TestPgErrorTranslation` (3 tests) in `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_phase12_quest.py:476-538` |
| BUG-I | `_WebBase.read()` accepts `**_ignore` so `top_k` forwarded by the search dispatcher is absorbed instead of raising `TypeError`.  Docstring notes the rationale | §10.1, §11, §14 `search(…)` bullets | `test_read_absorbs_top_k_kwarg` + `test_read_absorbs_arbitrary_unknown_kwargs` in `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_phase3_web.py:337-355` |

#### Collateral changes

- `server.get()` docstring rewritten to show `type='paper'` on every
  bare-slug example — the LLM-live test suite picks up tool schemas
  from the MCP server, so the docstring changes teach the LLM the new
  convention.  `test_llm_live.py::TestGetPaper` all pass (LLM emits
  `type='paper'` correctly on 6/6 prompts).
- Existing tests updated for the BUG-C semantic change:
  - `test_phase2_cost.py::TestServerDispatchFooter` — every
    `server.get(id='wang2020state')` call now passes `type='paper'`.
  - `test_server_phase1.py::test_get_with_id_still_works` retired;
    replaced by the 5-test `TestAmbiguousKindErrors::test_get_*` suite
    that locks the new behaviour + the scheme-prefix / DOI / file-ext
    carve-outs.
  - `test_llm_live.py::TestGetPaper` tests wrap the URI-capture
    assertions in `_require_dispatched_paper_uri` so an LLM emitting a
    bare slug gets skipped rather than failing the regression.

#### Not touched this run

- The four pre-existing-at-17:40 items (BUG-E, F, G, H per the
  19:30 log) are all fixed here.  The original triage #1–#11 from the
  16:42 run remain fixed.  Next live run should re-verify §5.3
  (marquessilva + mikladal), §5.4 (grep +/- query), §6.1 (todo views),
  §9.1 (conversation `/recent`), §14 (think landing), and the new §15.4
  per-verb `KIND_UNKNOWN` bullets.

#### Next-run checklist

1. Rebuild wheel, restart Windsurf MCP host.
2. §2 quick-smoke — expect all 6 bullets green.
3. §5.4 — expect `get(type='paper', grep='MOF')` to list papers (not
   TypeError), bare `get(grep='MOF')` to emit `KIND_UNKNOWN`.
4. §9.4 — `get(type='fc', id='/due')` must error `KIND_UNKNOWN`
   (BUG-B); `get(type='conv', id='/recent')` must already error
   (BUG-E envelope).
5. §10.1 / §11 / §14 — `search(type='<web|research|think>', query=…)`
   must return a Perplexity answer (BUG-I), not TypeError.
6. §15.4 per-verb KIND_UNKNOWN — bare `get(id='some-slug')` must
   error like `search()` and `put()` (BUG-C).
7. Record a fresh session-log entry above this one.

---

### Session: 2026-04-22 19:30  (Cascade — live run, post-fix verification)

First live run against the MCP host since the 17:40 fix pass.  Scope:
§2 quick-smoke + read-heavy reconnaissance over §3, §5, §6.1, §7.1,
§7.4, §8.1, §9.1, §9.4, §15.1, §15.2, §15.4.  Write roundtrips (§3.3
/ §6.2 / §7.2 / §8.2 / §9.3 / §16) were **not** exercised in this
run.  §10 (web) / §11 (research) / §12 (youtube) / §13 (math) all
skipped — §13 is WAD, the rest weren't requested.

- precis-mcp version: unknown (wheel shipped with current Windsurf MCP
  host); `mcp4_stats()` reports all 15 kinds visible at startup
- MCP host: Windsurf `mcp4_*`
- Scope: §2 quick-smoke + reads across §3/§5/§6.1/§7.1/§7.4/§8.1/§9.1/
  §9.4/§15.1/§15.2/§15.4
- Kinds visible at startup: conversation, flashcard, markdown, memory,
  paper, plaintext, quest, research, skill, tex, think, todo, web,
  word, youtube
- Startup warnings: `kind 'math' hidden — missing env: WOLFRAM_APP_ID`
  (WAD)

| § | step | result | note |
|---|------|--------|------|
| 2.0 | `stats()` | ✓ | 15 kinds; math hidden (WAD) |
| 2.1 | `skill:/` | ✓ | 7 skills (≥6 expected) |
| 2.2 | `skill:find-paper` body | ✓ | renders |
| 2.3 | `paper:` landing | ✓ | 3452 papers |
| 2.4 | `search membrane type=paper top_k=3` | ✓ | 3 hits |
| 2.5 | `quest:/recent` | ✗ | **BUG-H** — `UndefinedTable: relation "papers.requests" does not exist`, envelope is `ERROR [unexpected]:`; should be `UNAVAILABLE` with migration next-hint |
| 2.6 | `think:` landing | ✓ | bug #9 fix live — help view, `[cost: free]` |
| 2.7 | `search precis type=paper` | ⚠ | corrupt-chunk surfacing (xie2016dissecting `�` noise) — not precis-mcp's bug |
| 3.1 | `skill:/recent` | ✓ | 7 by mtime |
| 3.1 | `skill:/kind/quest` | ✓ | 3 skills |
| 3.1 | `skill:/kind/does-not-exist` | ✓ | friendly no-match |
| 3.1 | `skill:/topic/papers` | ✓ | 5 skills |
| 3.1 | `skill:handle-dropped-pdf` body | ✓ | renders |
| 3.1 | `search skill "acquire"` | ✓ | find-paper hit |
| 3.1 | `search skill "paper"` | ✓ | 2 hits |
| 3.1 | `search skill "acquire paper"` | ✗ | **BUG-G** — 0 hits despite both words in find-paper description |
| 3.1 | `search skill zzz-unmatchable-xyz` | ✓ | no-match |
| 3.2 | `skill:does-not-exist` | ✓ | `ID_NOT_FOUND` + options + next-hint |
| 3.2 | `skill:/kind/` | ✓ | `PARAM_INVALID` + next-hint |
| 3.2 | `skill:/topic/` | ✓ | `PARAM_INVALID` + next-hint |
| 5.1 | `paper:marquessilva1999grasp` | ⚠ | **BUG-D** — landing-page author field still raw JSON `[{"name": "..."}, ...]` (cite formatter cleaned, overview renderer did not) |
| 5.1 | `/toc` | ✓ | 20 sections |
| 5.1 | `/abstract` | ✓ | friendly "No abstract" (paper has none) |
| 5.1 | `doi:10.1109/12.769433` | ✓ | resolves to marquessilva1999grasp |
| 5.1 | bare `10.1109/12.769433` | ✓ | auto-detected DOI resolves |
| 5.2 | `/fig` | ✓ | 5 figures listed |
| 5.3 | `marquessilva1999grasp/cite/bib` | ✓ | **Bug #5 regression fix live** — `author = {Marques-Silva, J.P. and Sakallah, K.A.}` |
| 5.3 | `mikladal2013l/cite/bib` | ✓ | **Bug #5 regression fix live** — literal `Bjørn`, single-line title, no `<i>` tags |
| 5.3 | `marquessilva1999grasp/cite/ris` | ✓ | one `AU  -` per author |
| 5.3 | `marquessilva1999grasp/cite/acs` | ✓ | `Marques-Silva et al., IEEE Transactions on Computers 1999` — single-line |
| 5.4 | `get(type='paper', grep='MOF')` | ✗ | **BUG-A** — `TypeError: sequence item 4: expected str instance, NoneType found` |
| 5.4 | `get(type='paper', grep='year:2020-2024')` | ✗ | **BUG-A** — same TypeError |
| 5.4 | `get(type='paper', grep='tag:review')` | ✗ | **BUG-A** — same TypeError |
| 5.4 | `get(grep='MOF')` (no type=) | ✓ | `KIND_UNKNOWN` + options + next-hint |
| 5.4 | `search(type='paper', query='membrane', grep='tag:review')` | ⚠ | **BUG-F** — returns same top-5 as unfiltered membrane search; grep= filter appears silently dropped (or store has zero `tag:review` papers and the implementation doesn't distinguish "filter matched nothing" from "filter was skipped" — either way it's a surprising smoke result) |
| 5.5 | `search(query=…)` no type/scope | ✓ | `KIND_UNKNOWN` |
| 5.5 | `search(query='selectivity', scope='marquessilva1999grasp')` | ⚠ | returned 5 paper chunks, but **none from marquessilva1999grasp** — scope= not honoured or this paper's blocks don't contain "selectivity"; ambiguous from output alone (scoping-regression worth a closer look) |
| 5.6 | `paper:zzz-nonexistent-xyz` | ✓ | `ID_NOT_FOUND` + next-hint |
| 5.6 | `paper:marquessilva1999grasp›9999` | ✓ | friendly "No blocks in range" (not an error envelope — acceptable) |
| 5.7 | `put(id='paper:…', text='foo', mode='append')` | ✓ | `READONLY` (corpus is ingestion-only) + redirect hint |
| 6.1 | `todo:/` `/open` `/done` `/today` `/recent` | ✓ | **Bug #7 fix live** — all 5 views work |
| 7.1 | `flashcard:/due` `/recent` `/` | ✓ | 1 card (`fc:smoke-fc-…` — pre-existing from earlier run, still orphaned per 17:40 migration note) |
| 7.4 | `get(type='fc', id='/due')` | ✗ | **BUG-B** — returned full flashcard /due view; `fc` alias **not retired** despite 17:40 claim |
| 7.4 | `get(id='fc:smoke-fc-1776872623-2-2-4')` | ✗ | **BUG-B** + raw envelope — `!! ERROR PrecisError: item '…' not in corpus`; scheme partially recognised (prefix stripped), different error shape from structured errors |
| 8.1 | `memory:` | ✓ | 2 memories |
| 9.1 | `conversation:/recent` | ✓ | **Bug #2 fix live** — empty-state clean, no AttributeError |
| 9.4 | `get(type='conv', id='/recent')` | ✓ | `PrecisError: unknown scheme: 'conv'` — retired correctly, but **BUG-E** envelope shape differs from structured errors |
| 15.1 | `doi:…` + bare DOI | ✓ | both resolve |
| 15.2 | bare `search(query=…)` | ✓ | `KIND_UNKNOWN` |
| 15.4 | bare `get(id='some-slug')` no type no scheme | ✗ | **BUG-C** — returns `ID_NOT_FOUND: paper 'some-slug' not in corpus`; default-to-paper still routes for bare-id gets (retired for search and grep-only get, but not for `id=`-only get) |
| 15.4 | `put(id='', text='…')` no type | ✓ | `KIND_UNKNOWN` + options + next-hint |
| 15.4 | `search(query='…')` no type no scope | ✓ | `KIND_UNKNOWN` + options + next-hint |
| 15.4 | `quest:zzzzzzzz` | ✓ | `ID_MALFORMED` + next-hint ("8-char prefix") |
| 10.1 | `search(type='web', query=…)` | ✗ | **BUG-I** — `TypeError: _WebBase.read() got an unexpected keyword argument 'top_k'` — dispatcher passes default `top_k` through, server-side `_WebBase.read()` doesn't accept it.  No cost incurred (died before upstream call) |
| 10.1 | `get(type='web', id='what is a metal-organic framework?')` | ✓ | Full Perplexity answer with 9 numbered citations, `sonar` model, `[cost: ~$0.001/call]` footer, full Phase 4 ToS attribution footer |
| 10.1 | recency / focus kwargs | ⊘ | MCP client schema only exposes `query, top_k, scope, type` — no way to test `recency=` or `focus=` through the client surface.  Worth flagging: either expose these kwargs in the MCP tool schema or document them as query-string modifiers |
| 10.2 | invalid-key error path | ⊘ | skipped — can't mutate `PERPLEXITY_API_KEY` from the MCP-client side |
| 11 | `search(type='research', query=…)` | ✗ | **BUG-I** — same TypeError as §10.1, no cost |
| 11 | `get(type='research', id='compare AEM vs PEM for CO2 reduction')` | ✓ | Full deep-research answer, `sonar-deep-research` model, 35 numbered citations, `[cost: ~$0.50/call]` footer (`$$$` tier ✓), Phase 4 attribution footer.  Response ~35 KB saved to `/var/folders/…/T/windsurf/mcp_output_<hash>.txt` by the MCP client — this auto-spill behaviour is nice for large responses |
| 14 | `search(type='think', query=…)` | ✗ | **BUG-I** — same TypeError, confirms bug scope is all three `_WebBase` subclasses |

#### Fixes verified live

- Bug #2 — conversation `/recent` AttributeError: gone (empty-state clean)
- Bug #5 — BibTeX author hygiene: `marquessilva1999grasp` + `mikladal2013l` both render cleanly; RIS/ACS also clean
- Bug #7 — todo collection views: all of `/`, `/open`, `/done`, `/today`, `/recent` work
- Bug #9 — `think:` bare landing: free help view with `[cost: free]`
- "Default-to-paper" cleanup: retired on the `search` verb and on `get(grep=…)` and on `put`.  **Not** retired for bare `get(id='slug')` — see BUG-C.
- `conv:` alias retired: `type='conv'` errors `PrecisError: unknown scheme`

#### New bugs found in this run

Logged as active regression entries below in §17.

- **BUG-A** — `get(type='paper', grep=…)` crashes with `TypeError: sequence item 4: expected str instance, NoneType found` for every `grep=` form (plain keyword, `year:`, `tag:`).  Rendering code is hitting a `None` field in a ref row it didn't expect.  **Blocks §5.4 entirely.**
- **BUG-B** — `fc:` alias is not retired despite 17:40 claim.  `type='fc'` still routes to flashcard, `id='fc:<slug>'` still strips the prefix and looks up in the flashcard corpus.  Alias table must still contain `fc`.
- **BUG-C** — `get(id='some-slug')` with no `type=` and no scheme still routes to paper (returns `paper '…' not in corpus`) instead of emitting `KIND_UNKNOWN`.  Default-to-paper retired for search/grep-only get/put but not this path.
- **BUG-D** — Paper overview renderer displays authors as raw JSON (`[{"name": "..."}, ...]`) — cite formatter fix did not reach the landing-page header.  Visible on every paper with JSON-encoded authors (e.g. `marquessilva1999grasp`).
- **BUG-E** — Error-envelope format inconsistent across paths.  Structured path: `ERROR [<code>]: <msg>\n  where: …\n  cause: …\n  options: …\n  next: …`.  Raw path (seen on `type='conv'` and `id='fc:<slug>'`): `!! ERROR PrecisError: <msg>`.  Plan §15.4 expects `!! <ERROR_CODE>` which matches neither.  Pick one shape and enforce across all verbs.
- **BUG-F** — `search(type='paper', query='membrane', grep='tag:review')` returns the same top-5 results as the unfiltered membrane search, suggesting `grep=` is silently dropped when paired with `query=` on the paper kind.  Plan explicitly regression-checks this path (§5.4 "vector search pre-filtered by the tag:review metadata filter").  May be confounded by the store having zero `tag:review` papers — needs investigation with a tag that definitely has matches.
- **BUG-G** — Skill description-search multi-word tokenizer broken.  `search(type='skill', query='acquire paper')` returns 0 hits; singles `acquire` (1 hit) and `paper` (2 hits) both work.  No AND/OR across terms.
- **BUG-H** — `quest:/recent` with schema missing errors `UNEXPECTED: UndefinedTable` instead of `UNAVAILABLE` with a migration next-hint.  Distinct from the `DATABASE_URL`-unreachable flavour of the known Milestone A gap (that one is PoolTimeout; this one is schema-gone).
- **BUG-I** — `search(type='<web|research|think>', query=…)` crashes with `TypeError: _WebBase.read() got an unexpected keyword argument 'top_k'` for all three Perplexity-backed kinds.  The search dispatcher passes the default `top_k` through to handlers that don't accept it.  `get(type='…', id='…')` works for all three as a workaround.  No upstream cost incurred because the error fires before the Perplexity call.

#### Observations (not bugs)

- `search(query='precis', type='paper')` surfaces `xie2016dissecting` chunks filled with Unicode replacement characters (`�`) at rank 1-5.  Not a precis-mcp bug — the paper is corrupt in the store — but the vector index happily returns zero-information chunks.  Worth raising in `acatome-extract` / ingestion.
- Pre-existing test leftovers from the last smoke run (`smoke-*-1776872623`) still in todo, flashcard, memory.  The flashcard one uses the retired `fc:` slug and blocks the 17:40 "data migration owed" checklist item.
- `search(query='selectivity', scope='marquessilva1999grasp')` returned 5 hits, **none from marquessilva1999grasp** itself.  Either scope= is dropped or the paper's blocks don't contain the term — ambiguous from output.  Needs investigation with a query guaranteed to hit (e.g. use a term from the TOC we already have).

#### Not exercised this run

- Write roundtrips across all kinds (§3.3, §6.2, §7.2, §8.2, §9.3, §16)
- File-type kinds (§16) — no write surface exercised, no move
- `youtube` / `math` (§12 / §13) — §13 WAD, §12 needs `youtube-transcript-api` in venv
- `web` / `research` / `think` `search()` verb — blocked by BUG-I; `get()` covered for web + research (§10 / §11 / §14)
- `web` `recency=` / `focus=` kwargs (§10.1 bullets 2-4) — blocked by MCP client schema
- `PRECIS_KINDS` mask check (§15.3) — requires MCP-host restart

---

### Session: 2026-04-22 19:20  (Cascade / Reto — plan refresh, not a live run)

Follow-on to the 17:40 post-fix sweep.  Audited the plan against the
new source surface and applied a full refresh so the next live run
can be executed verbatim without guessing which steps are stale.

- Scope: plan edits only; no source changes.
- Sections touched: §3.3, §5.1, §5.3, §5.4, §5.5, §6.1, §6.3, §6.4 (new),
  §7.4 (new), §8.3 (renamed + new content), §8.4 (renumbered Links),
  §9 (rewritten header + §9.1 regression note + §9.3 write roundtrip
  new + §9.4 new), §15.3, §15.4, §16 header + §16.1-4 explicit `type=`
  bullets, §17 regression log (10 new active entries + 4 retired in
  the hidden block).
- Every new regression entry in §17 cross-links the live-check
  section, the unit-test file+line range, and the source fix where
  relevant, so a future reader can reach the coverage without
  rediscovering it.
- **Do not mistake this for live verification**: the listed unit
  tests pass (953 total), but nothing in this session exercised the
  running MCP host.  The §17 active entries remain active.

---

### Session: 2026-04-22 17:40  (Cascade / Reto — post-fix sweep, not a live run)

This is not an MCP-live session.  It records the source-side work done
between the 16:42 discovery run and the next MCP-host restart, so the
next live run has a clear baseline to verify against.

- precis-mcp version: source tree after the 2026-04-22 bug pass (pre-
  release, wheel not yet rebuilt).
- Scope: fix-pass for every entry in the 16:42 log's triage list plus
  the "default-to-paper" and "alias fluff" redesigns the human added
  mid-session.
- Tests: full suite passes (953 tests, up from 934), ruff + mypy clean.
- **Live verification still pending**: the MCP host must be restarted
  against the new wheel before the next smoke run; the installed wheel
  still reproduces the 16:42 bugs (e.g. `marquessilva1999grasp/cite/bib`
  still returns the JSON-array author field until reload).

#### Source-side fixes delivered

| triage # | area | implementation |
|---|---|---|
| 1 | slug-keyed get/put across acatome-store kinds | `_slug_prefix` class attr on RefHandler + prefixed retry in `_resolve_ref`; subclass prefixes set for todo/flashcard/memory/conversation |
| 2 | `conversation:/recent` AttributeError | `func.count(Block.node_id)` in place of the nonexistent `Block.id` |
| 3 | skill write-then-read mismatch (misdiagnosed as cache invalidation) | lenient `_parse_skill_md`: name falls back to directory, description to first body line; broken-YAML skills now index rather than disappear |
| 4 | search `type=` filter ignored for non-paper corpora | `_search_or_grep` routes non-paper kinds through corpus-scoped grep instead of the shared vector index |
| 5 | BibTeX author field raw JSON | `_author_names()` decodes JSON-encoded arrays and joins with " and " (BibTeX) / one `AU  -` per author (RIS) |
| — | BibTeX beyond the author field | `_clean_title` strips inline HTML tags, decodes entities, collapses multi-line whitespace, escapes reserved chars; applied to title/journal/DOI/year; RIS mirrors the cleanup with its own field rules |
| — | default-to-paper removed | `search(query=)` / `get(grep=)` / `put(text=)` now return `KIND_UNKNOWN` listing visible kinds when no `type=` and no disambiguating id/scope are given |
| 6 | `grep=` on paper list vector-search instead of corpus filter | plumbed as a distinct kwarg from `query`; bare-list calls prefer `_list_refs(grep=...)` over vector search when both are supplied |
| 7 | todo path views (`/open /done /today /recent /<state>`) rejected | `collection_views` registry on RefHandler + per-state / per-date methods on TodoHandler |
| 8 | `flashcard:` scheme missing (after alias cleanup) | renamed canonical from `fc` → `flashcard`: scheme, `_slug_prefix`, `_slugify`, bundled SKILL.md, every user-facing string |
| — | alias fluff retired | removed every `KindSpec.aliases` entry (`fc`, `conv`, `doi`/`arxiv`/`pmid`/`pmcid`/`isbn`/`issn`); identifier-type URI schemes on PaperHandler kept as real schemes, not alias shortcuts |
| 9 | plan §14 think docs wrong | rewrote §14 and the quick-smoke bullet to reflect Perplexity-backed reality; landing fix in `_WebBase` also shipped so the bare call no longer errors |
| 10 | file-type kinds missing from `stats()` | word/tex/markdown/plaintext each get a KindSpec + matching scheme; URI auto-classification via extension still works as before |
| 11 | `id=''` convention for skill writes | skill-append now falls back to frontmatter `name:` / `slug:` when neither `id=` nor `title=` carries a slug |
| — | `think:` bare landing | `_WebBase.read` returns a help view on empty query; `cost_of` reports `free` for the landing path so the footer doesn't lie about cost |

#### Regression coverage added

- `TestAmbiguousKindErrors` in
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_server_phase1.py:220-298` —
  7 tests locking the `KIND_UNKNOWN` response shape across search/get/put.
- `TestCitation` in
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_paper_handler.py:138-266` —
  7 tests for the BibTeX/RIS/ACS cleanup (covers the
  `marquessilva1999grasp` JSON-array, `mikladal2013l` Unicode/tag
  regressions, reserved-char escaping, missing-authors fallback).
- Landing-page tests for `web`, `think`, `research` in
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_phase3_web.py:302-357`.
- Skill-append frontmatter-slug fallback + strict error path in
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_phase12b_skill.py:430-475`.

#### Data migration owed

- Existing `fc:*` flashcard slugs in the store become orphaned — the
  handler's new `_slug_prefix` retry only looks for `flashcard:*`.
  Reingest or `UPDATE flashcards SET slug = REPLACE(slug, 'fc:', 'flashcard:')`
  before the next smoke run exercises pre-existing cards.
- Same for `conv:*` conversation slugs.
- `papers.requests` schema still not applied on this host (unchanged
  from the 16:42 run); quest writes will error until the migration
  lands.

#### Next run checklist

1. Rebuild + install the wheel, restart Windsurf's MCP host.
2. Re-run §2 quick-smoke — `think:` bare, `search(query='…', type='paper')`,
   and the rest must all succeed.
3. Drill into §3.3 (skill write roundtrip), §5.3 (cite/bib on
   `marquessilva1999grasp` and `mikladal2013l`), §6 / §7 / §8 / §9
   (slug-keyed write/read roundtrips on todo/flashcard/memory/
   conversation) to verify the fixes live.
4. Record a fresh session-log entry above this one.
