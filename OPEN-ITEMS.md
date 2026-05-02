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

##  gripe:3681 phase 2 — `tags=` on `get` for cache-backed kinds

**Status**: deferred
**Severity**: feature
**Owner**: `src/precis/handlers/_cache_base.CacheBackedHandler.get`
**Test**: `tests/test_cache_base.py::test_get_with_tags_applies_on_create`

One-call bookmark: `get(kind='web', q=URL, tags=['bookmark'])`
should fetch, cache, AND apply `bookmark` tag in a single round
trip. Today's pattern is two calls (`get` to cache, then `tag` to
annotate the auto-assigned slug). The slug round-trip already
works (fixed in 6.0.0 — see CHANGELOG), so the missing piece is
just the `tags=` kwarg flowing into `_cache_base.get` and being
applied on the cache-row write.

The original gripe (`refs.id = 3681`, soft-deleted 2026-05-02)
proposed this as part of a four-fold registry-driven change.
Phase 1 (search on cache-backed kinds) and phase 3 (cross-kind
fan-out) shipped. Phases 2 and 4 remain — see this entry and
the next.

## 🟢 gripe:3681 phase 4 — `mode='refresh'` + `watch:<interval>` axis

**Status**: deferred
**Severity**: feature
**Owner**: `src/precis/handlers/_cache_base.py`, `src/precis/store/types.py` (closed-axis tag), external cron driver
**Test**: `tests/test_cache_base.py::test_refresh_mode_bypasses_cache_preserves_tags`

Refresh-pinned-things affordance. Two parts:

1. **`mode='refresh'`** on `get` for cache-backed kinds — bypass
   cache, re-fetch upstream, **preserve** existing tags / links /
   slug. Today's only refresh path is `delete()` + `get()`, which
   loses annotations on the slug.
2. **`watch:<interval>`** as a closed-vocabulary tag axis —
   `watch:hourly`, `watch:daily`, `watch:weekly`, `watch:monthly`.
   External cron driver iterates `search(tags=['watch:daily'])`
   and calls `get(..., mode='refresh')` on each result. No new
   verb; composition of two existing primitives.

The pattern generalises the patent-watch CLI machinery that
already exists for `kind='patent'`, lifting it to every
cache-backed kind for free.

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

The mcp-critic 2026-05-02 deep pass logged 14 findings; 11 are now
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

See [`CHANGELOG.md`](CHANGELOG.md) entry for 6.0.0 for the per-fix
landing record.

---

_Last updated: 2026-05-02_
