# precis-mcp — Open Items

Durable backlog. Replaces the per-issue gripe trail (gripes 3667 +
3681 retired 2026-05-02 after the seven-verb surface refactor closed
their original framing) with a single canonical entry-point.

The mcp-critic review at
[`docs/mcp-critic-review-2026-05-02.md`](docs/mcp-critic-review-2026-05-02.md)
remains as the historical observation log; this file tracks only
what's still open.

> **Convention**:
> - **Status**: `open` / `blocked` / `deferred` / `done`
> - **Severity**: `critical` (blocks release) / `feature` / `polish`
> - **Owner**: rough estimate of where the fix lives
> - **Test**: name of the regression test that pins it (when fixed)

---

## 🟢 Dark-factory build/deploy workstream

**Status**: in progress · **Severity**: feature · **Owner**: `scripts/`,
`.claude/commands/`, `CLAUDE.md`

North star: `claude -w <feature>` → describe the spec → `/go` → the change
is implemented → gated → merged → deployed, with the LLM asked only "OK?" or
handed a genuinely broken test. Every mechanical step is a script (token-cheap,
reproducible); the model spends tokens on judgment, not CI/CD plumbing.

- **`scripts/deploy` + `/go`** → **shipped this workstream.** `scripts/deploy`
  is the non-interactive ansible-redeploy backbone (twin of `scripts/ship`,
  no LLM in the loop); `/go` = `scripts/ship` then `scripts/deploy` on green
  (the one-keystroke ship+deploy). `/endsession` stays deploy-free.
- **Token-lean session boot** → **partly done.** `## Other live affordances`
  in CLAUDE.md compressed to a one-line-per-kind index (detail already in the
  `precis-*-help` skills) — ~33% fewer boot bytes. Ties into the existing
  cold-start work (`docs/design/mcp-cold-start-token-budget.md`,
  `PRECIS_STARTUP_SKILLS`). Next: apply the same discipline to the
  `~/work/cluster` CLAUDE.md; measure boot token delta.
- **`/whatneedsdoing`** → **shipped this workstream.** One triage view that
  merges the three work stores — `OPEN-ITEMS.md` + open gripes
  (`get(kind='gripe', id='/open')`) + open/doable todos — and flags which are
  autonomous (todos the loop runs) vs inert (backlog/gripes not yet todos).
- **Backlog groomer (close the loop)** → open. Today nothing *works* the
  backlog or gripes automatically — `/whatneedsdoing` only *reads* them. The
  dark-factory move: a `level:recurring` watch that reads `OPEN-ITEMS.md` +
  open gripes and mints `kind='todo'` rows with `meta.executor` (a `fix_gripe`
  job for bugs; a build tick for features), so `dispatch` actually builds them.
  Pairs with `/checklogs` + cheap-model tiering. Until this lands, the backlog
  is a level-3 artifact the factory can't act on.
- **`/testfeature <prompt>`** → open. Agent loop that exercises the precis MCP
  surface (`scripts/exercise-mcp` is a seed), finds bugs, applies fixes, then
  `/go`. Bounded by a turn/cost cap.
- **`/checklogs`** → open. Read the recent LLM-error surface (prod `agentlog` +
  `alert` + failed `kind='job'` + error `ref_events`; local `.claude` logs +
  `/var/log/precis-worker-agent.log`), cluster the top-N recurring failures,
  fix root cause, `/go`.
- **Cheap-model tiering** → open. Route mechanical LLM work (`llm_summarize`,
  triage children, CI-fix escalation) to a small 4B–14B model; reserve Opus for
  build/planner/reviewer judgment.
- **Widen `scripts/ship` auto-fix surface** → open (polish). Auto-fix + amend
  anything the gate can resolve without judgment (import sort, trivial mypy
  stubs); only real logic failures reach the model.

Deferred (revisit later): **holdout scenarios** (StrongDM-style anti-overfit
eval outside the repo — not needed while Opus shows no test-gaming; ADR 0047
gold sets are the seed); **digital-twin fidelity** (richer stubs so
green-in-twin/red-in-prod gaps close — the current `FakeStore`/`MockEmbedder`/
`PRECIS_CLAUDE_BIN` twins are good enough for now); **auto-deploy as a daemon**
(vs `/go`-chained — only if chaining proves insufficient).

## 🔵 `serverInfo.title` not set

**Status**: blocked on upstream `FastMCP`
**Severity**: polish
**Owner**: `src/precis/server.py:129`
**Test**: `tests/test_server_init.py::test_serverinfo_carries_title`

MCP spec 2025-06-18 §A1 recommends a human-facing
`serverInfo.title` alongside the machine name. Today's
`FastMCP("precis-mcp", instructions=_INSTRUCTIONS)` constructor
takes no `title=` kwarg — we get `serverInfo.name = "precis-mcp"`
and no `title` field. One-line fix once `FastMCP` accepts
`title="Precis"`. Track upstream:

- https://github.com/modelcontextprotocol/python-sdk/issues — file
  the request when the next mcp-critic pass surfaces it again.

## Recently retired (kept here briefly for grep-ability)

The mcp-critic 2026-05-02 deep pass logged 14 findings; 13 are now
closed. Removed from the open list, traceable via git log + the
dated review document:

- precis-overview drift from live registry → fixed
- python callgraph entry resolution → fixed (separate session)
- think-kind reasoning trace leak → fixed (perplexity.py orphan-tag handling)
- view=links recovery hint pointing at `put(link=,rel=)` → fixed
- python empty-search lacking `Next:` → fixed
- soft-deleted vs never-existed conflated → fixed (`Gone` error class)
- calc parse-vs-evaluate envelope drift → fixed
- `tests/test_mcp_modalities.py` value-asserting `Overall: OK` → fixed
- web search-options listing unregistered kinds → fixed
- web slug not round-tripping through `get` → fixed
- paper search omitting score annotation → fixed (consistency with block-level kinds)
- gripe:3681 phase 2 — `tags=` on cache-backed `get` → **shipped 2026-05-02**
  (one-call bookmark; pre-validates so a bad axis no longer pays the
  upstream API cost before failing)
- gripe:3681 phase 4 — `mode='refresh'` + `WATCH:<interval>` axis →
  **shipped 2026-05-02**
  (`Store.update_cache_entry` preserves tags/links across re-fetches;
  `WATCH:hourly|daily|weekly|monthly` closed vocabulary on cache-backed
  kinds; `precis maintenance run` cron driver composes both)
- "eager skill cache" critic finding → **retracted** (was based on
  incorrect storage-model assumption; skill kind is file-backed,
  not DB-backed, so there's no async tsvector to make eager)
- OQ-17 — `PRECIS_DEFAULT_TAGS` × `workspace` auto-tag layering →
  **shipped 2026-05-26** (`PlaintextHandler.put` now accepts and
  applies `tags=` via `apply_tag_ops`, so the runtime's default-tags
  merge actually lands on prose-file refs alongside the
  `workspace` flag; regression test in
  `tests/test_default_tags.py::test_default_tags_layer_with_workspace_on_prose_handlers`)
- acatome U+FFFD mojibake → **shipped 2026-05-27**
  (`precis.ingest.pipeline._repair_or_fail_mojibake` auto-repairs the
  alpha-space-FFFD-space-alpha em-dash loss pattern and fails the
  bundle with paper_id + page + 60-char context on any other FFFD;
  mirrored upstream in `acatome_extract.pipeline`; regression test in
  `tests/ingest/test_pipeline.py::TestRepairOrFailMojibake`)
- ingest: tiny-block embedding noise → **shipped 2026-05-27**
  (`marker._merge_small_blocks` absorbs `section_header` blocks
  forward into the next body block and merges adjacent same-type
  small blocks within a `(section_path, page)` window; addresses
  bge-m3's tendency to embed tiny chunks near the centroid where
  short generic queries also land; regression test in
  `tests/ingest/test_marker.py::TestMergeSmallBlocks`)
- OQ-16 — `KindSpec.requires_env` convergence → **retracted 2026-05-27**
  (description was stale: math already had `requires_env`; oracle has no
  env reads at all; web uses trafilatura, not Firecrawl; youtube has no
  env reads. The planned `OPENAI_API_KEY` / `FIRECRAWL_API_KEY` /
  `YOUTUBE_API_KEY` gates never materialised because the implementation
  went with free/local alternatives or doesn't need API access for those
  kinds. No work needed.)
- mypy errors on `test_toon_roundtrip.py` + `test_initial_migration.py` →
  **shipped 2026-05-27** (`dump()` parameter relaxed from `list[Mapping]`
  to `Sequence[Mapping]` to honour covariance; the 15 `cur.fetchone()`
  unpacking sites route through a new `_one(cur)` helper that asserts
  not-None. `mypy src tests` clean. All 24 migration tests still pass
  against postgres.)
- Persistent discovery layer → **shipped 2026-05-31** (ADR 0018).
  `view='toc'` reads from `ref_segments` + `ref_segment_sentences`
  instead of recomputing DP + KeyBERT at request time; search-result
  rows carry indented `excerpt @ ~N: "..."` sub-lines drawn from a
  query-aligned pgvector cosine rerank. Migrations 0005 / 0006 / 0007.
  New worker: `precis worker --only segments`. Smoketest verified
  end-to-end on a real paper (`butlin26` → 3 segments + 544 sentences
  rendered as designed); test suite covered in
  `tests/workers/test_segment_toc.py`, `tests/test_toc_db.py`.
- `chunks.numerics TEXT[]` lexical numeric-token index →
  **shipped 2026-05-31** (path-2 from the tables-curveball discussion;
  ingest extracts every `<number><unit>` token from a closed unit
  vocab. Structured `paper_facts` extraction — path-3 — remains
  tracked separately in `docs/design/storage-v2.md § Open questions`.)
- References pollution of search → **shipped 2026-05-31** (ingest now
  tags bibliography blocks `chunk_kind='references'`; embed + RAKE
  workers carry `skip_chunk_kinds=('references',)` which extends the
  claim SQL so references never enter the queue. Bibliography stops
  diluting search rankings.)
- Mid-abbreviation chunk splits ("et al.", "Fig.", "i.e.") →
  **shipped 2026-05-31** (pysbd-backed sentence splitter wired into
  the chunker's fallback chain via a sentinel; abbreviation-aware
  rules eliminate the naive `". "` literal split.)
- Hyphenated line breaks corrupting verbatim quotes →
  **shipped 2026-05-31** (regex pass in `marker._clean_text` joins
  `-\s*\n\s*` when both sides are lowercase ASCII; preserves
  semantically-significant compounds with uppercase boundaries.)
- New `citation` kind (verifier-workflow scaffold) →
  **shipped 2026-05-31** (`CitationHandler`, migration 0007;
  `precis-citation-help` documents the agent surface.)
- Retraction status invisible on paper views → **shipped 2026-05-31**
  (`view='overview'`, `view='toc'`, and chunk drill-in all lead with
  a `> [!] RETRACTED` (or EoC / corrected) banner when
  `refs.retraction_status` is set; carries date, reason, and a
  pointer at `get(kind='provenance', id='<doi>')`.)

See the git history (`git log`, around the 6.0.0 tag) for the per-fix
landing record.

## ✅ CI: wire up a real PostgreSQL service on Linux

**Status**: done (2026-06-27)
**Severity**: polish
**Owner**: `.github/workflows/check.yml`
**Test**: `tests/conftest.py::_pg_available`

The `check.yml` test job was split into `test-linux` (ubuntu, with a
`pgvector/pgvector:pg16` `services: postgres` block + `PRECIS_TEST_PG_URL`
pointing at it as the `postgres` superuser) and `test-other`
(macOS/Windows, no service — db-tagged tests auto-skip via the
`_pg_available()` probe). The db-tagged tests (~41% of the suite) now
gate the release on Linux. macOS/Windows GHA runners don't support
service containers, so they stay db-less by design.

## 🔵 Platform-specific test bugs (Windows + macOS Python 3.12)

**Status**: open
**Severity**: polish
**Owner**: `tests/test_python_handler_writes.py`,
`tests/test_python_runtrace.py`,
`tests/test_python_config_wire.py`
**CI workaround**: `continue-on-error` on the affected matrix legs
in `.github/workflows/check.yml` (Linux + macOS-3.11/3.13 still
gate the release).

**Windows** — 27 tests fail because the python-handler write path
opens directory FDs with `os.O_DIRECTORY` for fsync, and that
constant is Unix-only:

- `test_python_handler_writes.py::*` (26 tests) —
  `AttributeError: module 'os' has no attribute 'O_DIRECTORY'`.
  Fix: branch on `sys.platform`; on Windows, fall back to a
  no-op fsync (or open the parent file by handle).
- `test_python_config_wire.py::test_parse_expands_tilde` —
  test asserts `~` expands to a Linux-style path; Windows expands
  to `C:/Users/runneradmin`.  Fix: assert against
  `os.path.expanduser("~")` instead of a hardcoded prefix.

**Python 3.12 setprofile + urllib.parse circular import** — 5
runtrace tests fail because the spawned tracer subprocess raises
`AttributeError: partially initialized module 'urllib.parse' …
(most likely due to a circular import)`.  First spotted on
`/Library/Frameworks/Python.framework/Versions/3.12/`; as of
2026-05-22 also reproduces in the Linux ``precis-dev`` container's
Python 3.12.  3.11 and 3.13 are unaffected; Homebrew Python 3.12
also works.  Suspect: `sys.setprofile` hook intercepts an internal
``urllib.parse`` import during a partially-initialised module
state when the user entry triggers ``argparse`` (which lazy-imports
urllib for help-text fallbacks).  Likely fix: defer the profile
install until after ``urllib.parse`` has been imported by the
bootstrap, or run the tracer in a fresh interpreter via ``-S`` +
explicit ``site.main()``.

The five subprocess-spawning tests carry
``@pytest.mark.xfail(strict=False)`` gated on Python 3.12 so they
still execute (we notice an XPASS on a non-bugged interpreter)
but don't fail the suite on bugged ones:

- ``tests/test_python_runtrace.py::test_runtrace_captures_call_tree``
- ``tests/test_python_runtrace.py::test_runtrace_argv_is_forwarded``
- ``tests/test_python_runtrace.py::test_runtrace_collapses_stdlib_by_default``
- ``tests/test_python_runtrace.py::test_runtrace_expand_stdlib_keeps_full_tree``
- ``tests/test_python_runtrace.py::test_runtrace_max_events_truncates``

Both clusters are tracked here so we don't lose them between
release and the post-release patch window.

## 🔵 OQ-11 — verify FastMCP server-pinned-prompt support

**Status**: open (verification only; design ships either way)
**Severity**: polish
**Owner**: `src/precis/mcp_modalities.py::register_skill_prompts`
**Plan artefact**: `docs/design/mcp-cold-start-token-budget.md` §Open questions
**Test**: none yet

Phase 3 of the MCP session-ergonomics rollout
(`PRECIS_STARTUP_SKILLS`) tags pinned skills on `prompts/list` and
also surfaces them via a `Pinned skills:` line in
`serverInfo.instructions` as a belt-and-suspenders fallback. The
question is whether MCP 2025-06-18 + FastMCP 1.x lets a server
flag a `prompts/list` entry as "render at session start", or
whether the tag is purely a client-side convention.

Action: read FastMCP source for `prompts/list` handler shape,
read MCP 2025-06-18 §prompts. Either way the design ships — the
banner notice carries the discovery channel — but the answer
determines whether we can stop carrying the redundant banner
line in a future cleanup.


---

_Last updated: 2026-05-27 (OQ-17 + acatome mojibake + merge-forward + mypy
closed; OQ-16 retracted as stale)_
