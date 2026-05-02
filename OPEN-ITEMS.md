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

See [`CHANGELOG.md`](CHANGELOG.md) entry for 6.0.0 for the per-fix
landing record.

---

_Last updated: 2026-05-02_
