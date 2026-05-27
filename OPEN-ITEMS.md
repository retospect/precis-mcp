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

## 🔴 acatome `\ufffd` mojibake in served paper bodies

**Status**: open (different package — `acatome-extract`)
**Severity**: critical (paper bodies advertised as "clean markdown
safe to quote" but aren't, for affected papers)
**Owner**: `pips/packages/acatome-extract/src/acatome_extract/pipeline.py`
**Test**: `pips/packages/precis-mcp/tests/test_paper_blocks.py::test_no_replacement_chars_in_blocks`

`\ufffd` (`�`) replacement chars appear in some paper block
bodies — em-dash bytes lost during PDF→markdown extraction.
Live-probed 2026-05-02 in `acheson2026automated~118` and
`xie2016dissecting~1283`. Other papers ingest cleanly.

Fix lives in `acatome-extract`'s post-Marker pipeline: add a
UTF-8 round-trip check; replace `\ufffd` with em-dash when context
is `alpha-space-alpha`, else fail the bundle with a clear
diagnostic. Regression test stays in `precis-mcp` because that's
where the user-visible symptom lands — scan every served block,
assert `\ufffd` absent.

Cross-repo: file in `acatome-extract`'s issue tracker once the
package has one. Until then, this entry is the canonical record.

---

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

See [`CHANGELOG.md`](CHANGELOG.md) entry for 6.0.0 for the per-fix
landing record.

## 🔵 CI: wire up a real PostgreSQL service on Linux

**Status**: open
**Severity**: polish
**Owner**: `.github/workflows/check.yml`
**Test**: `tests/conftest.py::_pg_available`

For the v6.0.0 release the test job runs without postgres and the
`db`-tagged tests (≈ 41 % of the suite, 654 / 1594) skip
automatically via the new ``_pg_available()`` probe in
``conftest.py``. Lint + the 940 db-less tests still gate the
release. Re-enable the full suite by adding a
``services: postgres`` block (with the ``pgvector/pgvector:pg16``
image) on the ``ubuntu-latest`` matrix legs. macOS / Windows runners
don't support GHA services and are fine staying skipped.

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

## 🔵 OQ-16 — `KindSpec.requires_env` convergence (non-patent)

**Status**: open
**Severity**: polish
**Owner**: `src/precis/handlers/{oracle,math,web,youtube}.py`
**Plan artefact**: `docs/design/mcp-cold-start-token-budget.md` §Open questions
**ADR**: `docs/decisions/0013-mcp-session-context-env-vars.md` (mentions as deferred)
**Test**: none yet

Phase 4 of the MCP session-ergonomics rollout converted
`PatentHandler`'s inline `EPO_OPS_KEY` / `EPO_OPS_SECRET` env
gate from a boot-site if-block in `precis.dispatch.boot()` to a
declarative `KindSpec.requires_env` plus an `__init__`-time
`InitError`. The other env-gated handlers still read their env
vars inline:

- `oracle.py` — `OPENAI_API_KEY` (or whichever provider key).
- `math.py` — `WOLFRAM_APP_ID`.
- `web.py` — `FIRECRAWL_API_KEY`.
- `youtube.py` — `YOUTUBE_API_KEY`.

Each conversion is small (two-line spec + four-line `__init__`)
but each wants its own review since the existing code paths have
subtle differences (some swallow the missing-env case to a
`KindSpec.supports_*=False` shape; others raise inline). Track
as one ticket; convert one handler per follow-up commit.

When all four are done, the boot-site gate machinery in
`precis.dispatch.boot()` collapses to a single
`_gated(handler_cls)` call per handler regardless of env shape,
which is the design's "convergence" pay-off.


## 🔵 mypy errors on `tests/test_toon_roundtrip.py` + `tests/test_initial_migration.py`

**Status**: open (pre-existing on `feat/storage-v2-step-b`)
**Severity**: polish
**Owner**: tests as named
**Test**: `uv run mypy src tests` (currently 18 errors in 2 files)

Surfaced during the MCP session-ergonomics DoD pass when running
`uv run mypy src tests`. All 18 errors come from
`15a025b "B1: greenfield v2 schema in a single 0001_initial.sql"`
on this branch, not from any of the session-ergonomics work.

Two clusters:

- `tests/format/test_toon_roundtrip.py:86,92,100` — three calls
  pass `list[dict[str, str]]` to a function expecting
  `list[Mapping[str, Any]] | Mapping[str, Any]`. List invariance
  on a covariant payload. Fix: change the call-site annotation
  to `Sequence[Mapping[str, Any]]`, or cast.
- `tests/test_initial_migration.py:241..442` — 15 sites that
  iterate over `cur.fetchone()` results without checking for
  `None`. The driver returns `None` if no row matched; mypy is
  flagging the implicit `None`-iteration. Fix: assertion or
  defensive-`is not None` per call site.

Neither cluster gates merging the v7.1.0 release — they pre-date
this work and are scoped to a different branch's intended-clean-up.
Tracked here so they don't get lost between releases.

---

_Last updated: 2026-05-26 (OQ-17 closed)_
