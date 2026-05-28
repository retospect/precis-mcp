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

## XMP `dc:identifier` not set on PDF write-back

**Status**: open
**Severity**: polish
**Owner**: `src/precis/ingest/pdf_writer.py:_info_dict_from_patch`
**Test**: none yet — add when XMP path lands

ADR 0014 introduced PDF metadata write-back via the standard Info
dict (Title / Author / Subject / Keywords). DOI lands in
``Subject`` as ``"DOI: 10.x/y"`` and in ``Keywords`` as the
machine-readable ``"doi:10.x/y, arxiv:..."``. The publisher-canonical
home for the DOI is XMP ``dc:identifier``, which is what
``_read_existing_pdf_metadata`` already reads via exiftool
``-Identifier``. Today's write path doesn't touch XMP, so:

- Tools that strip the Info dict but preserve XMP (rare but real
  — some PDF post-processors do this) lose our written DOI.
- Round-trip "patched-PDF → fresh re-ingest" works via Keywords
  but is one fallback short of the canonical path.

Implementation: pymupdf exposes ``Document.set_xml_metadata(str)``;
construct a minimal RDF/XMP fragment with ``dc:identifier`` set to
``doi:10.x/y`` plus the existing fields and call that alongside
``set_metadata()``. Roughly 30 lines + a fixture-based round-trip
test.

## Signed-PDF detection skipped on write-back

**Status**: open
**Severity**: polish
**Owner**: `src/precis/ingest/pdf_writer.py:patch_pdf_metadata`
**Test**: none yet — needs a signed-PDF fixture

ADR 0014 documents that signed-PDF detection is intentionally not
implemented in the initial cut. Incremental save preserves the
existing byte range so signatures usually remain verifiable, but
"usually" isn't "always": a reader that re-validates and rejects
the appended trailer would surface a "signature broken" warning
even though we didn't touch the signed content. Academic corpora
rarely contain signed PDFs (mostly NIH grant docs and some
government corpora), so this is documented as a known gap rather
than a blocker.

Implementation: scan the PDF catalog for ``/AcroForm/SigFlags`` or
walk the form widgets looking for ``/FT /Sig``. PyMuPDF exposes
``Document.pdf_catalog()`` + ``Document.xref_get_key()``. Return
``PatchOutcome(..., skipped_reason="signed")`` on detection. ~20
lines + a fixture (the synthetic-sig case can be built with
pymupdf's own widget API).

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


---

_Last updated: 2026-05-27 (OQ-17 + acatome mojibake + merge-forward + mypy
closed; OQ-16 retracted as stale)_
