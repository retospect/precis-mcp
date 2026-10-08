---
status: draft
pillar: platform
---

# MCP verb schema diet: kind-specific kwargs move under args=

Every session — Claude Code main loops, subagents and every cluster agent —
carries the `precis` verb definitions in its prompt on every turn. `/context`
(2026-10-08) puts them at 14.5k tokens. Measured from
`tools/core.py` via `func_metadata(...).model_json_schema()`:

| verb | params | schema chars | share |
|---|---|---|---|
| put | 98 | 10650 | 39% |
| edit | 64 | 6853 | 25% |
| search | 46 | 5731 | 21% |
| get, delete, tag, link, more | 28 | 4148 | 15% |

Agents use a small core. 24h of local calls (2026-10-07): `put` 94 calls used
only kind, text, id, tags, args, link, rel, title, body; `edit` 39 calls used
id, kind, text, find, base_sha, reason, mode, body, ops, dry_run; `search` 35
calls used kind, q, page_size, k, status, tags, view, sort, page plus five
singletons. Everything else (`rxn_smiles`, `sequence`, `ref_designator`,
`seeds`, `llm_models`, `qty`, `value_low`, …) is one kind's vocabulary, paid
for by every turn of every session.

## Change

- Each verb in `tools/core.py` keeps a core set of top-level params; all
  kind-specific ones move under the existing `args: dict`. Pick the core set
  from 30 days of the prod `tool_calls` ledger (`arg_keys` frequency per verb),
  not from this 24h sample. Target: put and edit ≤ 15 params each, search
  ≤ 15, total schema ≤ 5k tokens.
- Dispatch flattens `args` into the handler call. Strictness stays: a key the
  handler's signature doesn't declare is `BadInput` naming the kind's accepted
  keys (today's `put(kind='gripe') does not accept ['title']` message is the
  model). This also retires the silent-drop class `mcp-verb-kwarg-parity`
  tracks: every handler kwarg becomes reachable through `args`.
- A call that passes a moved kwarg at top level must fail loudly with
  `next: put(kind=..., args={'<key>': ...})`, never be dropped. Check what
  FastMCP does with an undeclared top-level key before relying on it.
- Each kind's skill documents its `args` keys (the skills already carry the
  kind vocabulary; the verb docstrings point there). Update every skill
  example that passes a moved kwarg at top level — grep
  `src/precis/data/skills/` and the job prompts under `src/precis/workers/`.

## Test

- A schema-size ratchet: total verb schema chars stay under the target.
- Every handler `put`/`edit`/`search` kwarg is reachable through `args`
  (replaces the `_KNOWN_GAPS` ratchet in `tests/test_mcp_verb_kwarg_parity.py`).
- A moved kwarg passed at top level returns `BadInput` with the `args=` hint.
- Skill examples parse against the new signatures (no top-level moved keys).

## Risk

Cluster agents with cached prompts or job templates that pass moved kwargs at
top level break until updated; the loud error plus `next:` hint is the
migration path. Ship with `/go`, not `/qgo`: it changes the surface every
agent uses.

Related: `mcp-surface-economy`, `mcp-verb-kwarg-parity`,
`session-prefix-and-fleet-context-cost` (a).
