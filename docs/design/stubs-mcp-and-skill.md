# Surface the "papers we still need to get" backlog to the agent

**Status**: done
**Slug**: `stubs-mcp-and-skill`

**Amendment (chase-queue + batch requeue).** Three follow-ups landed on
top of the original design below, all reusing its query/state-summary
core rather than forking it:

1. **DRY the stub predicate.** The "is this a fetchable stub" check
   (`kind='paper' AND pdf_sha256 IS NULL AND deleted_at IS NULL AND` an
   accepted external identifier exists) was hand-copied at three call
   sites — `Store.stub_backlog`, `.stub_backlog_count`
   (`store/_refs_ops.py`), and `fetch_oa.claim_stubs_to_fetch`. It now
   lives once, `src/precis/store/_stub_predicate.py::stub_predicate_sql
   (alias, id_kinds=…)`, whitelist-filtering `id_kinds` against the
   fixed `STUB_ID_KINDS = {doi, arxiv, s2}` before splicing into the SQL
   `IN (...)` list — the whitelist decides what text can appear, never a
   raw caller string. All three sites consume it; `fetch_oa`'s own
   backoff-interval clause, quest-weight `LATERAL` join, `ORDER BY`, and
   `FOR UPDATE ... SKIP LOCKED` are untouched.

2. **`view='chase-queue'`** — a DOI-only, never-tried-first slice of the
   same backlog for "what should I go find a PDF for right now" rather
   than "what's been waiting longest". `Store.stub_backlog` gained
   `id_kinds: tuple[str, ...] = ('doi', 'arxiv', 's2')` and `sort:
   Literal['oldest-request', 'last-tried'] = 'oldest-request'` (both
   default to prior behavior, so every existing caller is unaffected).
   `sort='last-tried'` orders by the latest `fetcher:%` attempt
   timestamp ASC with `NULLS FIRST` (never-attempted on top), tie-broken
   on `ref_id`; the deprioritized-stubs-sink-to-the-back rule stays the
   outermost `ORDER BY` term in both sort modes.
   `search(kind='paper', view='chase-queue')` intercepts before kind
   resolution exactly like `view='stubs'`
   (`runtime/dispatch.py`/`search.py::_dispatch_chase_queue`) and calls
   `store.stub_backlog(id_kinds=('doi',), sort='last-tried', limit=n)`.
   Same render shape as `view='stubs'` (paper-only, `q=` ignored,
   `n=`/`page_size=` caps rows), just a different header and backlog
   slice.

3. **"Fetch next N" batch requeue (Drive UI).** The `state=stub` queue
   (`/drive?state=stub`) offers a "Fetch next 25 ↑" button
   (`POST /drive/requeue-stubs`) that calls
   `Store.requeue_stubs_for_fetch(limit=25)`: selects the top never-tried
   DOI stubs (no `fetcher:%` event yet) and stamps each with
   `meta.oa_requeued` + a `ref_events` row (`source='paper_reconcile'`,
   `event='oa_requeued'`) — the same marker/stamping shape
   `paper_hygiene.requeue_stranded_fetches` uses, which `fetch_oa`'s
   claim query orders `jsonb_exists(meta, 'oa_requeued') DESC` first, so
   a stamped stub is claimed on the very next pass. Already-stamped
   stubs are excluded from selection (idempotent — a repeat click can't
   double-stamp or double-count). The route redirects back with
   `?requeued=<n>` for the existing `notice` banner to flash.

## Problem

The corpus tracks *stub* papers — `paper` refs with an external
identifier (DOI / arXiv / S2) registered but `pdf_sha256 IS NULL`.
Stubs are minted two ways:

- the **chase worker**, when a finding's citation chain reaches an
  unheld paper, and
- the gated dream **`acquire`** tool
  (`PaperHandler.acquire`), which mints a stub and tags it
  `DREAM:acquire`.

Today the *full* backlog is only reachable from the CLI
(`precis stubs`, `src/precis/cli/stubs.py`) or raw SQL. An agent
(asa) driving the MCP verbs can only find the subset she explicitly
wanted, via `search(kind='paper', tags=['DREAM:acquire'])` — the
chase-minted stubs (no `DREAM:acquire` tag) are invisible to her.
There is also no skill teaching the concept, so the affordance is
undiscoverable.

## Goals

1. **MCP exposure** — let the agent list the full stub backlog over
   the existing `search` verb, without a new verb (the seven-verb
   surface is a constitutional promise; `thresholds.md` §API).
2. **Skill** — a `precis-stubs-help.md` reference skill teaching the
   backlog concept, the query, and the `DREAM:acquire`/`acquire`
   relationship.

Non-goals: no new CLI flags, no schema change, no new dependency, no
change to how stubs are minted or fetched.

## Design

### Share the query (no logic fork)

Lift the SQL + the per-row state summary out of `cli/stubs.py` into a
store method so the CLI and the MCP path render from one source:

```
Store.stub_backlog(*, limit: int, awaiting: bool) -> list[dict]
```

Returns the same row dicts the CLI already serializes
(`ref_id`, `cite_key`, `identifier`, `last_attempt`, `last_source`,
`last_event`, `state`). The `state` derivation (`"awaiting fetch"`,
`"no OA version"`, …) is deterministic business logic, fine to
colocate with the query. `cli/stubs.py` keeps its argparse surface
and TOON/JSON serialization; `run()` just calls `store.stub_backlog`.

### `view='stubs'` on `search`

Mirror the `view='dreamable'` precedent (`runtime.py` interception
before kind resolution):

- Intercept `verb == "search" and view == "stubs"` and route to a new
  `_dispatch_stubs`.
- It is paper-only and ignores `q=` (like `view='dreamable'` ignores
  `q=`). `n=`/`page_size=` caps the row count; an `awaiting=True`
  knob is *not* exposed at the MCP layer (the agent wants "what's
  outstanding", not the fetcher's next-pass filter — keep the MCP
  surface minimal; the CLI keeps `--awaiting` for the operator).
- Render a compact text `Response`: one line per stub
  (`ref_id`, identifier, cite_key, state), plus a `Next:` block
  pointing at `get(kind='paper', id=<ref_id>)` and
  `get(kind='skill', id='precis-stubs-help')`. Empty backlog renders
  a clear "no stubs" line.

`tools/core.py` already forwards `view=` untouched, so the only tool
change is one sentence in the `search` docstring advertising
`view='stubs'`. This is additive — a new *value* on an existing arg,
not a JSON-shape or verb-surface change, so no threshold trips.

### Skill

`src/precis/data/skills/precis-stubs-help.md`, `flavor:reference`,
`status: active`. Sections (goal-voice H2s, alias groups on the
high-traffic ones):

- list the backlog (`search(kind='paper', view='stubs')`)
- the subset a dream wanted (`tags=['DREAM:acquire']`)
- what a stub *is* (no PDF yet; fetcher auto-grabs OA; backlog-only
  when no external id)
- cross-links: `precis-paper-help`, `precis-finding-help`,
  `precis-search-help`, `precis-dreaming-help`.

Cross-reference the new skill from `precis-paper-help` and add a
`view='stubs'` row to the `precis-search-help` arg table.

## Tests

- `Store.stub_backlog`: stub predicate (pdf NULL + external id),
  `awaiting` filter, state summary, `limit`. (DB test.)
- `search(view='stubs')` dispatch: empty message, renders a minted
  stub, ignores `q=`, paper-only. (Through `runtime.dispatch`.)
- Skill example linter already covers the new skill structurally.

## Rollout

Version bump + CHANGELOG entry (user-visible: new search view + new
skill). No migration. No ADR — no new trade-off beyond the existing
`view='dreamable'` precedent.
