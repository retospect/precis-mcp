---
status: draft
title: NotFound should render HTTP 404, not 400, in the web app
prio: low
model: sonnet
---

# NotFound should render HTTP 404, not 400, in the web app

## Motivation / why

`precis_web/errors.py` maps every `PrecisError` (including `NotFound`) to
HTTP **400**. So a link to a genuinely absent ref (`/refs/<kind>/<bad-id>`,
a mistyped paper slug, etc.) surfaces "Request error (400)" instead of a
proper 404 — the exact confusing symptom that prompted the deleted-structure
tombstone work (shipped 38c6e05c). That ship fixed the *deleted-ref* case by
returning an explicit 404 tombstone, but left the global mapping alone
because many routes lean on the PrecisError→400 handler and a blanket change
is riskier than the one case in hand.

## In scope

Route `NotFound` specifically to 404 (a dedicated `@app.exception_handler(NotFound)`
that precedes the generic `PrecisError` handler), leaving `BadInput` and other
`PrecisError` subclasses on 400. A friendlier not-found body (near-match slug
hints — cf. gripe about the paper-slug NotFound not suggesting near matches) is
a natural rider but optional.

## Explicitly NOT in scope

Changing the status of `BadInput` / validation errors; the deleted-ref
tombstone (already shipped).

## Acceptance criteria

- `GET /refs/memory/<absent-id>` → 404 (currently 400).
- `BadInput`-driven routes still return 400.
- Existing route tests that assert 400 on a NotFound path are updated to 404.

## Target + blast radius

`src/precis_web/errors.py` (handler registration order), and any route test
asserting 400 on a not-found (e.g. `tests/precis_web/test_refs_tombstone.py::
test_genuinely_absent_ref_is_not_a_tombstone`, which currently asserts 400).
