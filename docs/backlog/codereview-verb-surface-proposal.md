# codereview: verb-surface flat signatures — needs a proposal, not a patch

> 2026-08-21: the demanded proposal's surface half SHIPPED — the frontier
> one-string profile (`PRECIS_MCP_PROFILE=command`,
> `tools/command_parser.py`); the typed surface stays for generic clients.
> This item keeps the *internal* half: args= migration of kind-specific
> params, typed per-verb signatures, retiring the `[override]` wall.

`tools/core.py` exposes `put` with 72 params (search 30, edit 37) — a flat
union of every kind's kwargs, widened by each new kind, shadowing builtins
(`property`, `min`, `max`, `id`). Downstream: `Handler` declares verbs as
`**kw: Any`, all ~55 subclasses narrow (181 `type: ignore[override]`), and
`runtime/dispatch.py::_accepted_kwargs` reconciles by reflection — so the
whole verb boundary is invisible to mypy.

Constraints (Reto, 2026-08-11): the narrow MCP surface is deliberate —
token-efficient minimal tool schema that "explodes with skills"; MCP means
backward compat is NOT the blocker, but any migration of existing
kind-specific params into the `args=` extras channel touches MCP schema +
associated skills together and needs a full proposal first.

Interim (can ship without the proposal): freeze the surface — a guard test
pinning the param count/roster of each verb so new kinds must use `args=`;
new kind-specific params don't widen the top-level verbs.

Proposal to write: migrate existing kind-specific params (material's
`property`/`min`/`max`/`maturity`, etc.) to `args=`; typed per-verb
signatures (Protocol or per-kind TypedDict kwargs) to retire the
`[override]` wall; skill-doc updates in the same change.
