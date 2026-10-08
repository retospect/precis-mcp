---
status: draft
pillar: platform
---

# MCP verb schema diet: slice 2 (discovery + skill migration)

Slice 1 is on `main`: `put`/`edit`/`search` advertise only `CORE_PARAMS`
(`precis.runtime.dispatch`); kind-specific options ride `args={...}`. The
advertised verb schemas went 30.7k → 13.1k chars (`/context` MCP tools 14.5k →
6k tokens). A legacy top-level option still works and appends
`note: pass X inside args={...}; top-level X is deprecated` (leading the reply
when the call errors); stringified scalars are parsed by the handler's
annotation. Mechanics: `precis.tools.mcp_slim`.

## Dogfood record (2026-10-08, same 7-step prompt each run)

Opus 5.5, Fable 5.1 and Sonnet 5.5 each finished every step. The fixes
between runs: scalar coercion for undeclared top-level keys, the note leading
error envelopes, paper paging and byline hints echoing `args=`, `body` core on
`edit`, the unknown-key error listing only `args=` keys once, todo `title=`
pointing at `text=`, and `precis-search/put/todo-help` rewritten to `args=`.

Still seen on the last (Sonnet) run:
- `search(kind='skill', q='which args does a kind accept')` returns unrelated
  skills; agents learn a kind's keys by passing a junk key in `args`.
- Memory recall misses live mirrored nodes (gr474868).

The prompt named `args=` and the options to try, so it primed every run. The
next check is a blind prompt that names tasks only (author lookup, one hit
per paper, todo with priority and parent, todos sorted by priority).

## Remaining

- **Generated per-kind args reference.** Build it from each handler's
  `put`/`edit`/`search` signature (name, type, default, docstring line),
  served as `get(kind='skill', id='args', kinds='<kind>')`. Point the verb
  docstrings and the unknown-key `BadInput` at it.
- **Migrate the remaining skills.** About 106 files under
  `src/precis/data/skills/` and the job prompts under `src/precis/workers/`
  still show non-core options at top level. They work (with the note), but
  every example teaches the deprecated form.
- **Count deprecated top-level use.** `tool_calls.input_keys` is recorded
  after the slim wrapper flattens `args=`, so the two forms look the same.
  Record the deprecated keys (a column, or a marker key) in
  `precis.tools.mcp_slim` so the token review can count them by key.
- **Retire the top-level path** once that count is zero for 14 days: a moved
  option at top level becomes `BadInput`.

## Test

- `tests/test_mcp_slim_schema.py` holds the size ratchet and the dogfood
  regressions; add one asserting the args reference lists every handler key.
- A skill-example check: no example passes a non-core option at top level.

Related: `mcp-surface-economy`, `mcp-verb-kwarg-parity`,
`session-prefix-and-fleet-context-cost` (a).
