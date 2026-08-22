---
status: draft
title: Notation matching is one-directional — a canon query can't reach a not-yet-normalized row
model: sonnet
---

# ASCII→canon is covered; canon→ASCII isn't

Shipped 2026-08-22 (`utils/ref_hybrid.py`): when a query differs from
`normalize_notation(q)`, search runs the canonicalized form as an **extra
lexical leg**. So `40 kOhm` now finds a claim written `40 kΩ`.

That covers the direction agents actually type. The reverse does not work: a
query written `40 kΩ` will not lexically match a row still spelled `kOhm`, and
such rows exist — corpus notation normalization (step 3 of
`nanopub-corpus-remediation.md`) has **not** run. Two were found by accident
while scanning hub titles for markdown:

| hub | residue |
|---|---|
| fi176922 | `R_inter-grain of approximately 40 kOhm` |
| fi176924 | `R_Q = h/2e², approximately 12.9 kOhm` |

plus `mu_B` (fi191162), `Phi_AB` (fi177742), `n-to-pi*` (fi177475).

Today the semantic leg is the only thing bridging that gap, and it is a
similarity signal, not a guarantee.

## Why not just normalize the corpus and be done

Step 3 will fix these rows, but it does not close the class:

- Ingested paper/patent chunks are **not** claim sentences and are never
  notation-normalized — they keep whatever the publisher wrote.
- New hubs can drift between mint and the next normalization pass.
- The extra-leg approach costs a second FTS query per search whenever the
  query isn't already canonical.

## Proposal — a folded expression index

Fold **both** sides to a notation-neutral form (`µ`/`u`/`mu` → `u`,
`Ω`/`ohm` → `ohm`, `Å` → `a`, `±` → `+-`, strip the `°` spacing question) and
match on that:

- an `IMMUTABLE` SQL function `notation_fold(text)`;
- expression indexes: `to_tsvector('english', notation_fold(title))` on `refs`,
  same on `chunks.text`;
- search folds the query and matches the folded index.

Symmetric, one query instead of two legs, and index-speed.

## Risks to size before building

1. **`IMMUTABLE` is load-bearing** — an expression index requires it. The fold
   must never depend on collation or locale.
2. **Index build on a live table.** `chunks` is large (36k on `draft` alone).
   Needs `CREATE INDEX CONCURRENTLY`, which cannot run inside the migration
   transaction — check how the forward-only migration runner handles that
   before committing to the approach.
3. **Do not fold aggressively.** `6-311++G**` is a Pople basis set, not markup;
   `E_g`/`E_F`/`K_d`/`2^N` are canon-ASCII by design and must survive. A fold
   that eats `*`, `_` or `^` corrupts real chemistry and physics.
4. Decide whether the fold replaces the extra-leg path or backs it up. Keeping
   both is defensible — the leg is cheap insurance if the index is stale.

## Work

1. Write `notation_fold` + property tests over the canon table, including the
   must-survive cases above.
2. Forward-only migration: function + concurrent expression indexes.
3. Route the lexical legs in `utils/ref_hybrid.py` and
   `store/_blocks_ops.py::search_blocks_fused` through the folded column.
4. Re-run the prod smoke queries in `precis-search-help`'s notation section.
