# precis-mcp live smoke-test plan

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
- [ ] `mcp5_get(id='think:')` — stateless think landing
- [ ] `mcp5_search(query='precis')` — global search across all kinds

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

### 3.3 — Write surface

**⚠ destructive**: creates SKILL.md files on disk.  Skip in quick-smoke.
Pick a unique slug per run to avoid collision.

- [ ] `mcp5_put(id='skill:', text='---\nslug: smoke-test-<TIMESTAMP>\ndescription: Smoke test skill\n---\n# body', mode='append')`
    - expect: success + next-hint pointing at `get(id='skill:<slug>')`
- [ ] verify: `mcp5_get(id='skill:smoke-test-<TIMESTAMP>')` returns body
- [ ] `mcp5_put(id='skill:smoke-test-<TIMESTAMP>', text='updated body', mode='replace')`
    - expect: success; next `get` shows new body
- [ ] `mcp5_put(id='skill:smoke-test-<TIMESTAMP>', note='smoke test note')`
    - expect: note attached (check via a subsequent `get`)
- [ ] `mcp5_put(id='skill:smoke-test-<TIMESTAMP>', mode='delete')`
    - expect: success; subsequent `get` returns `ID_NOT_FOUND`

---

## 4.  quest  (PG-backed; read surface first, writes land in 12b)

Requires `DATABASE_URL` + Postgres + `papers` schema (from `acatome-quest-mcp`
migrations).

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

- [ ] `mcp5_get(id='paper:wang2020state/cite/bib')` — BibTeX
- [ ] `mcp5_get(id='paper:wang2020state/summary')` — enrichment summary

### 5.4 — Listing + filtering

- [ ] `mcp5_get(grep='MOF')` — paper list filtered by keyword
- [ ] `mcp5_get(grep='ingested:today')` — recent ingest filter
- [ ] `mcp5_get(grep='year:2020-2024')` — year-range filter
- [ ] `mcp5_get(grep='tag:review')` — tag filter

### 5.5 — Search

- [ ] `mcp5_search(query='anion exchange membrane')`
    - expect: ranked results across all papers (global)
- [ ] `mcp5_search(query='selectivity', scope='wang2020state')`
    - expect: results scoped to one paper
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

- [ ] `mcp5_get(id='todo:/')` — all todos
- [ ] `mcp5_get(id='todo:/open')` — open todos
- [ ] `mcp5_get(id='todo:/done')` — closed todos
- [ ] `mcp5_get(id='todo:/recent')` — recently modified

### 6.2 — Write

Use a unique test-prefix tag per run (e.g. `smoke-YYYYMMDDHHMM`) to
isolate test data.

- [ ] `mcp5_put(id='todo:', text='Smoke test [smoke-<TS>]', mode='append')`
    - expect: success + `next: get(id='todo:<N>')` where N is the new id
- [ ] `mcp5_get(id='todo:<N>')`
    - expect: the just-created todo
- [ ] `mcp5_put(id='todo:<N>', text='Updated [smoke-<TS>]', mode='replace')`
    - expect: text updated
- [ ] `mcp5_put(id='todo:<N>', mode='done')`  (or whatever the done mode is)
    - expect: status flipped; shows up in `/done`
- [ ] `mcp5_put(id='todo:<N>', mode='delete')`
    - expect: removed; subsequent `get` returns `ID_NOT_FOUND`

### 6.3 — Search

- [ ] `mcp5_search(type='todo', query='smoke')`
    - expect: match on current + recent test items

---

## 7.  flashcard (`fc`)

Aliased kind — `fc:` and `flashcard:` should both work.

### 7.1 — Read

- [ ] `mcp5_get(id='fc:/due')` — cards due for review
- [ ] `mcp5_get(id='fc:/recent')` — recently created
- [ ] `mcp5_get(id='flashcard:/')` — alias works the same as `fc:/`

### 7.2 — Write

- [ ] `mcp5_put(id='fc:', text='Q: What is 2+2?\nA: 4', mode='append')`
    - expect: success + next `get(id='fc:<N>')`
- [ ] `mcp5_get(id='fc:<N>')` — confirm content
- [ ] `mcp5_put(id='fc:<N>', mode='rate', grade=4)`  (SM-2 grade)
    - expect: scheduled interval updated
- [ ] `mcp5_put(id='fc:<N>', mode='delete')`

### 7.3 — Search

- [ ] `mcp5_search(type='fc', query='2+2')`
    - expect: hit on the just-created card

---

## 8.  memory

Long-lived agent notes.  Block-structured.

### 8.1 — Read

- [ ] `mcp5_get(id='memory:')` — recent memories
- [ ] `mcp5_search(query='precis', type='memory')`

### 8.2 — Write

- [ ] `mcp5_put(id='<paper-slug>', note='Smoke memory [smoke-<TS>]')`
    - expect: memory attached to the paper slug
    - alt: `mcp5_put(id='memory:', text='...', mode='append')` if memory
      has its own scheme
- [ ] `mcp5_search(query='smoke-<TS>', type='memory')`
    - expect: hit on the just-created memory

### 8.3 — Links

- [ ] `mcp5_put(id='<paper-slug>', link='<other-slug>:cites')`
    - expect: link recorded; surfaces in response footer

---

## 9.  conversation

Chat turn history.  Block-structured, slug-keyed.

### 9.1 — Read

- [ ] `mcp5_get(id='conversation:/recent')` — recent conversations
- [ ] `mcp5_get(id='conversation:<slug>')` — one conversation
- [ ] `mcp5_search(query='<term>', type='conversation')`

### 9.2 — Scoped search

- [ ] `mcp5_search(query='membrane', scope='conversation:<slug>')`
    - expect: results only from that conversation

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

## 14.  think  (stateless reflection)

No deps, no API, pure function.

- [ ] `mcp5_get(id='think:')` — landing / help
- [ ] other interactions per the handler's declared surface

---

## 15.  Cross-kind features

### 15.1 — Aliases

- [ ] `mcp5_get(id='doi:10...')` → resolves same as `paper:10...`
- [ ] `mcp5_get(id='fc:/due')` → resolves same as `flashcard:/due`
- [ ] record any alias that doesn't resolve

### 15.2 — Global search

- [ ] `mcp5_search(query='membrane', top_k=10)` (no `type=`)
    - expect: ranked hits across all searchable kinds
    - check: each hit shows which kind it came from

### 15.3 — Visibility / PRECIS_KINDS mask

- [ ] set `PRECIS_KINDS=paper,skill`, restart, `mcp5_stats()`
    - expect: `kinds by verb` shows ONLY paper + skill
- [ ] `mcp5_get(id='quest:/recent')`
    - expect: `KIND_UNKNOWN` with `options=['file', 'paper', 'skill']`

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

## 16.  File-type kinds  (`docx`, `tex`, `md`, `txt`)

File-based kinds dispatch by extension.  Need a writable working dir.

### 16.1 — Markdown

- [ ] `mcp5_put(id='/tmp/precis-smoke.md', text='# Title\n\nPara one.', mode='append')`
- [ ] `mcp5_get(id='/tmp/precis-smoke.md')` — TOC
- [ ] `mcp5_get(id='/tmp/precis-smoke.md', depth=1)` — H1 only
- [ ] `mcp5_put(id='/tmp/precis-smoke.md', text='## Section', mode='append')`
- [ ] `mcp5_put(id='/tmp/precis-smoke.md›<slug>', text='New para.', mode='replace')`
- [ ] `mcp5_put(id='/tmp/precis-smoke.md›<slug>', mode='delete')`

### 16.2 — Plaintext

- [ ] `mcp5_put(id='/tmp/precis-smoke.txt', text='line 1\nline 2', mode='append')`
- [ ] `mcp5_get(id='/tmp/precis-smoke.txt')`

### 16.3 — LaTeX

- [ ] `mcp5_put(id='/tmp/precis-smoke.tex', text='\\section{Foo}\n\nPara.', mode='append')`
- [ ] `mcp5_get(id='/tmp/precis-smoke.tex')` — section outline

### 16.4 — DOCX

Requires an existing .docx file (create one manually or use a fixture).

- [ ] `mcp5_get(id='<path>.docx')` — TOC
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

- [ ] **`skill:/kind/<name>` via MCP surface (fix: 2026-04-22)** —
  `get(type='skill', id='/kind/quest')` used to return
  `PARAM_INVALID: unexpected kwarg(s) on skill/kind: slug`.  Root
  cause: `precis.uri.parse` splits the leading `/` into
  `(path='', view='kind', subview='quest')`; `SkillHandler.read()`
  then fell through to the per-skill branch and passed `slug=''` to
  `_read_kind_view`, which rejected the unexpected kwarg.  Regression
  tests in
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_phase12b_skill.py`
  (`test_kind_view_via_parsed_uri_shape`,
   `test_topic_view_via_parsed_uri_shape`,
   `test_recent_view_via_parsed_uri_shape`) exercise the parsed-URI
  shape the live surface produces.  Live check: §3.1 step
  `get(type='skill', id='/kind/quest')` must return a skill list
  without error.

---

## Session log

<!-- prepend new sessions above existing ones -->
