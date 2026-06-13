# Surface the "papers we still need to get" backlog to the agent

**Status**: done
**Slug**: `stubs-mcp-and-skill`

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
