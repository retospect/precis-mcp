# Bare chain rungs spend dollars the meter never counts

**Found:** 2026-08-21, reviewing the `Rung.bare` ship (opus, this session).
**Status:** open. The *gate* half shipped with `bare`; this is the *meter* half.

## The gap

`Rung.bare` lets an operator put `{"bare": true}` on an `llm.chain.<tier>`
rung, so that rung runs `claude -p --bare` and authenticates with
`ANTHROPIC_API_KEY` — per-token billing instead of subscription quota.

`breaker.gate_tier` now takes `bare=` and dollar-gates such a rung (shipped).
But `meter.spent_usd` still filters the dollar total by *transport name*:

```sql
AND (transport IS NULL OR transport <> ALL(%s))   -- OAUTH_TRANSPORTS
```

`OAUTH_TRANSPORTS = ("claude_agent", "claude_p")`, and a bare call still logs
`transport='claude_p'`. So its real `cost_usd` is excluded from the rolling
dollar meter as "notional subscription spend".

**Consequence:** bare spend is gated on a cap it never accumulates toward. The
cap can only trip from *other* paid traffic (OpenRouter / paid fetches). An
operator who flips `bare: true` on a chain and walks away has no dollar rail
on it — which is the opposite of what someone enabling per-token billing
needs. Same blindness in `precis stats` and any $-per-tier reporting.

## Why it wasn't fixed in the same ship

The clean fix is to make the *logged* transport for a bare call distinct
(e.g. `claude_p_bare`), which fixes gate and meter together and makes bare
spend directly queryable. But `_record_dispatch` is typed on the `Transport`
enum (`router.py::_record_dispatch`), so a distinct label ripples through its
signature and callers, and migration `0107_rename_litellm_transport_to_local`
shows the column's value set is something we rewrite deliberately, not
casually. That is a considered change to billing accounting, not a rider on an
auth ship.

Verified during triage: nothing reconstructs a `Transport` enum from the log
column — the only reader formats it as text (`handlers/llm.py`, "via
{transport}") — so a new label is viable, not blocked.

## Options

1. **Distinct transport label** (`claude_p_bare`) written at
   `_record_dispatch` and passed to `gate_tier`. Fixes gate + meter + makes
   bare spend queryable with no schema change. Needs: threading `bare` to
   `_record_dispatch`, a backfill decision for pre-existing rows (none exist —
   the feature is new), and a `precis stats` grouping check.
2. **`features` jsonb flag** — `features->>'bare'`, and widen the `spent_usd`
   predicate. Smaller signature blast radius; makes the SQL predicate hairier
   and leaves the transport column lying.
3. Leave it and rely on the operator watching spend manually. Only acceptable
   while no chain has `bare: true`.

Option 1 is preferred.

## Acceptance

- A bare `claude_p` call's `cost_usd` counts toward `meter.spent_usd`.
- A non-bare `claude_p` call still does not.
- A test pins both directions.
- `breaker.gate_tier`'s ⚠ docstring note and this file are removed on landing.

## Until then

**Do not set `"bare": true` on a production chain without watching spend.**
The gate will stop bare calls once the dollar cap is tripped by other traffic,
but bare spend alone will never trip it.
